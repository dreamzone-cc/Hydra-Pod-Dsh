# SPDX-License-Identifier: AGPL-3.0-or-later
"""Router and policy checks (architecture §11, §12, §19, §37a rev 2).

Hard constraints, checked before any preference:
1. the agent's billing route is allowed for the role, and not forbidden;
2. the runtime can honour a requested model (capability flags);
3. the Manager/Verifier model is never the Executor's or the Reviewer's;
4. a Reviewer runs under enforced read-only.
`classify_provider` maps a DSH session provider id to a billing route so the
Manager's real route (seen in its session log) can be checked too.

Registry v2 (technical paper §5, phase 3) adds pools: several agents per role.
`choose` keeps every hard constraint above, then ranks the pool's members by
the ticket's complexity, the agent's availability on this machine and its
subscription window (adapters.py), and the pool's fallback order. A reviewer
pool is a cascade: `review_plan` says which stages a ticket needs (always, on
high risk, or when the manager overruled the reviewer on a serious point).
Extensions add agents and pools in agents.d/*.json; they cannot change the
policy, and their agents pass the same checks.
"""

import json
import re
from pathlib import Path

REGISTRY = Path(__file__).resolve().parent.parent / "agents.json"
WHEN = ("always", "risk:high", "disagreement", "never")
COMPLEXITY_RANK = {"S": 0, "M": 1, "L": 2}
STRATEGIES = ("score", "cascade", "panel")
READ_ONLY_ROLES = ("reviewer", "advisor")   # roles whose agents may never change the code


def accepts(pool_role: str, pool: dict | None, a: dict) -> bool:
    """Whether agent `a` may serve in the pool of `pool_role`.

    By default an agent serves its own role, and a code-reviewing specialist may
    review. A pool can widen that with `accepts: {roles, capability}`, e.g. a
    tester pool of executors that can write tests."""
    acc = (pool or {}).get("accepts")
    if acc:
        return a["role"] in acc.get("roles", []) and (not acc.get("capability")
                                                      or acc["capability"] in a.get("capabilities", []))
    if a["role"] == pool_role:
        return True
    return pool_role == "reviewer" and a["role"] == "specialist" and "code-review" in a.get("capabilities", [])


def needs_read_only(pool_role: str, pool: dict | None) -> bool:
    acc = (pool or {}).get("accepts") or {}
    return pool_role in READ_ONLY_ROLES or pool_role == "security" or \
        any(r in READ_ONLY_ROLES for r in acc.get("roles", []))


class PolicyError(Exception):
    """No agent satisfies the hard constraints."""


def load(path: Path | None = None) -> dict:
    """The registry, with agents.d/*.json merged in (sorted by file name)."""
    path = path or REGISTRY
    reg = json.loads(path.read_text())
    reg.setdefault("pools", {})
    reg["_extensions"] = []
    for ext in sorted((path.parent / "agents.d").glob("*.json")):
        data = json.loads(ext.read_text())
        reg["_extensions"].append({"file": ext.name, "sets_policy": "policy" in data,
                                   "duplicates": sorted(set(data.get("agents", {})) & set(reg["agents"]))})
        for name, agent in data.get("agents", {}).items():
            reg["agents"].setdefault(name, {**agent, "source": ext.name})
        for role, pool in data.get("pools", {}).items():
            reg["pools"][role] = pool
        for name, rt in data.get("runtimes", {}).items():
            reg["runtimes"].setdefault(name, rt)
    return reg


def dsh_provider_defs(home: Path | None = None) -> dict[str, dict]:
    """Provider definitions from the DSH profiles (`cordis.patch.yml`): {name: {displayName, apiKeyEnv, baseURL}}.

    A provider's name says little about how it is billed: in a profile, `anthropic` may be
    an OAuth bridge to a Claude subscription ("Anthropic (Claude OAuth)", apiKeyEnv
    ANTHROPIC_OAUTH_TOKEN) rather than an API key. The definition says which. Read with a
    small line scanner (the package has no YAML dependency); the `web` profile wins a clash."""
    import os
    root = Path(home or os.environ.get("DSH_HOME") or Path.home() / ".dsh") / "profiles"
    files = sorted(root.glob("*/cordis.patch.yml"), key=lambda f: (f.parent.name != "web", f.parent.name))
    out: dict[str, dict] = {}
    for f in files:
        try:
            lines = f.read_text().splitlines()
        except OSError:
            continue
        owner: dict[int, str] = {}    # indent -> the key opened at that indent
        found: dict[str, dict] = {}
        for line in lines:
            m = re.match(r"^(\s*)([A-Za-z0-9_.-]+):\s*$", line)
            if m:
                owner[len(m.group(1))] = m.group(2)
                for deeper in [i for i in owner if i > len(m.group(1))]:
                    del owner[deeper]
                continue
            fld = re.match(r"^(\s*)(displayName|apiKeyEnv|baseURL):\s*(.+?)\s*$", line)
            key = fld and owner.get(len(fld.group(1)) - 2)
            if key:
                found.setdefault(key, {})[fld.group(2)] = fld.group(3).strip("'\"")
        for key, fields in found.items():
            out.setdefault(key, fields)       # files are in priority order: the first definition wins
    return {k: v for k, v in out.items() if v}


_DEFS: dict | None = None


def classify_provider(provider: str | None, defs: dict | None = None) -> str:
    """Billing route of a DSH provider id as it appears in session logs.

    The provider's definition in the DSH profile decides when there is one (an OAuth token
    variable, or "OAuth" in its display name, is a subscription route); the name is only
    the fallback."""
    global _DEFS
    p = (provider or "").lower()
    if defs is None:
        if _DEFS is None:
            _DEFS = {k.lower(): v for k, v in dsh_provider_defs().items()}
        defs = _DEFS
    d = defs.get(p)
    if d:
        oauth = "oauth" in d.get("apiKeyEnv", "").lower() or "oauth" in d.get("displayName", "").lower()
        claude = "anthropic.com" in d.get("baseURL", "") or "anthropic" in p or "claude" in d.get("displayName", "").lower()
        if oauth:
            if claude:
                return "oauth/claude-pro"   # a Claude subscription via OAuth: operator-approved for the manager
            return f"oauth/{p.removeprefix('oauth-').removeprefix('pi-')}"
        if claude:
            return "api/anthropic"
    if p in ("pi-anthropic",) or (p.startswith("pi-") and "anthropic" in p):
        return "oauth/claude-pro"        # consumer OAuth bridged into DSH: operator-approved for the manager (2026-09-30)
    if p.startswith("pi-"):
        return f"oauth/{p[3:]}"
    if p == "anthropic":
        return "api/anthropic"
    if p in ("deepseek-official", "deepseek"):
        return "api/deepseek"
    return f"api/{p}" if p else "unknown"


def violations(reg: dict | None = None) -> list[str]:
    """Policy problems in the registry itself."""
    reg = reg or load()
    pol, out = reg["policy"], []
    by_role: dict[str, list] = {}
    for name, a in reg["agents"].items():
        by_role.setdefault(a["role"], []).append(a)
        allowed = pol["allowed_billing"].get(a["role"], [])
        if a["billing"] not in allowed:
            out.append(f"{name}: billing {a['billing']} not allowed for role {a['role']}")
        if a["billing"] in pol["forbidden_billing"]:
            out.append(f"{name}: billing {a['billing']} is forbidden")
        if a["runtime"] not in reg["runtimes"]:
            out.append(f"{name}: unknown runtime {a['runtime']}")
        if a["role"] in READ_ONLY_ROLES and not a.get("read_only"):
            out.append(f"{name}: {a['role']} without enforced read-only")
    managers = {a["model"] for a in by_role.get("manager", [])}
    for role in ("executor", "reviewer"):
        for a in by_role.get(role, []):
            if a["model"] in managers:
                out.append(f"manager model {a['model']} is also a {role}")
    for ext in reg.get("_extensions", []):
        if ext["sets_policy"]:
            out.append(f"agents.d/{ext['file']}: only agents.json may set the policy (ignored)")
        for name in ext["duplicates"]:
            out.append(f"agents.d/{ext['file']}: agent {name} already exists (the agents.json entry is kept)")
    out += pool_violations(reg)
    return out


def _members(pool: dict) -> list[dict]:
    return pool.get("members") or pool.get("stages") or []


def pool_violations(reg: dict) -> list[str]:
    """Pools reference real agents of the right role; a later review stage is independent of the executor."""
    out, agents = [], reg["agents"]
    executor_vendors = {a.get("vendor") for a in agents.values() if a["role"] == "executor"}
    for role, pool in reg.get("pools", {}).items():
        if pool.get("strategy") not in STRATEGIES:
            out.append(f"pool {role}: strategy must be one of {STRATEGIES}")
        for i, m in enumerate(_members(pool)):
            for name in [m.get("agent")] + list(m.get("fallback", [])):
                a = agents.get(name)
                if a is None:
                    out.append(f"pool {role}: unknown agent {name!r}")
                    continue
                reviewing = role == "reviewer"
                if not accepts(role, pool, a):
                    out.append(f"pool {role}: {name} has role {a['role']}")
                if needs_read_only(role, pool) and not a.get("read_only"):
                    out.append(f"pool {role}: {name} has no enforced read-only")
                if reviewing and i > 0 and (not a.get("vendor") or a.get("vendor") in executor_vendors):
                    out.append(f"pool {role} stage {i + 1}: {name} must come from a vendor other than the "
                               f"executor's ({', '.join(sorted(v for v in executor_vendors if v))})")
            bad = [w for w in m.get("when", []) if w not in WHEN]
            if bad:
                out.append(f"pool {role}: unknown when {bad} (one of {WHEN})")
            if m.get("for") and any(c not in COMPLEXITY_RANK for c in m["for"]):
                out.append(f"pool {role}: 'for' must list S, M or L")
    return out


def _passes(name: str, a: dict, role: str, reg: dict) -> str | None:
    """Why an agent fails a hard constraint for the pool of `role`, or None.

    Billing is always checked against the agent's own role: a pool can borrow
    an agent, never widen what that agent may be billed to."""
    pol = reg["policy"]
    pool = reg.get("pools", {}).get(role)
    if not accepts(role, pool, a):
        return f"role {a['role']} cannot serve as {role}"
    allowed = pol["allowed_billing"].get(a["role"], [])
    if a["billing"] not in allowed or a["billing"] in pol["forbidden_billing"]:
        return f"billing {a['billing']} not allowed"
    if needs_read_only(role, pool) and not a.get("read_only"):
        return "no enforced read-only"
    return None


def choose(role: str, complexity: str | None = None, reg: dict | None = None, quota=None,
           available=None) -> dict:
    """The best agent of a role's pool for a ticket: hard constraints, then rank.

    `quota(agent) -> {"exhausted", "percent", ...}` and `available(agent) -> (ok, why)`
    default to the runtime's adapter; tests pass their own."""
    from . import adapters
    reg = reg or load()
    if complexity and complexity not in COMPLEXITY_RANK:
        raise PolicyError(f"complexity must be S, M or L, not {complexity!r}")
    pool = reg.get("pools", {}).get(role)
    if pool is None:  # a v1 registry, or a role without a pool
        return {**route(role, reg=reg), "considered": [], "pool": None}
    members = _members(pool)[:1] if pool.get("strategy") == "cascade" else _members(pool)  # panel: ranked like score
    entries = []
    for order, m in enumerate(members):
        for k, name in enumerate([m["agent"]] + list(m.get("fallback", []))):
            entries.append((order, k, m, name))
    considered, ok = [], []
    for order, k, m, name in entries:
        a = reg["agents"].get(name)
        why = "unknown agent" if a is None else _passes(name, a, role, reg)
        if not why and complexity and m.get("for") and complexity not in m["for"]:
            why = f"not for complexity {complexity} (for {', '.join(m['for'])})"
        q = None
        if not why:
            ad = adapters.for_runtime(a["runtime"])
            if ad is None:
                why = f"no adapter for runtime {a['runtime']}"
            else:
                up, reason = (available or ad.available)(a)
                if not up:
                    why = reason
                else:
                    q = (quota or ad.quota)(a)
                    if q.get("exhausted"):
                        why = f"subscription window at {q['percent']}% ({q['source']})"
        considered.append({"agent": name, "ok": not why, "why": why, "quota": q})
        if not why:
            fit = 0 if not (complexity and m.get("tier")) else \
                abs(COMPLEXITY_RANK[complexity] - (2 if m["tier"] == "strong" else 0))
            ok.append(((m.get("fallback_order", order), k, fit, (q or {}).get("percent") or 0), name))
    if not ok:
        raise PolicyError(f"no available agent for role {role!r}"
                          + "; " + "; ".join(f"{c['agent']}: {c['why']}" for c in considered))
    ok.sort()
    name = ok[0][1]
    return {"agent": name, **reg["agents"][name], "considered": considered, "pool": role}


def disagreement(fsummary: dict) -> bool:
    """The manager overruled the reviewer on a serious point: a rejected high/critical finding or conclusion."""
    for r in fsummary.get("reports", []):
        if any(f["status"] == "rejected" and f["severity"] in ("critical", "high") for f in r["findings"]):
            return True
        if any(i["status"] == "rejected" and i["kind"] == "conclusion" for i in r.get("items", [])):
            return True
    return False


def review_plan(risk: str | None, fsummary: dict, reg: dict | None = None, second_review: str | None = None,
                quota=None, available=None) -> list[dict]:
    """Which review stages this ticket needs, each with the agent that would run it.

    `second_review` comes from the active profile: "never", "always", or None
    to use each stage's own `when`."""
    reg = reg or load()
    pool = reg.get("pools", {}).get("reviewer")
    if not pool or pool.get("strategy") != "cascade":
        return [{"stage": 1, "needed": True, "why": "always", **_pick(["reviewer"], reg, quota, available, True)}]
    plan = []
    disagree = disagreement(fsummary)
    for i, st in enumerate(pool["stages"]):
        when = st.get("when", ["always"])
        if i > 0 and second_review in ("never", "always"):
            when = [second_review]
        reasons = [w for w in when if w == "always" or (w == "risk:high" and risk == "high")
                   or (w == "disagreement" and disagree)]
        plan.append({"stage": i + 1, "needed": bool(reasons), "why": ", ".join(reasons) or f"not needed ({', '.join(when)})",
                     **_pick([st["agent"]] + list(st.get("fallback", [])), reg, quota, available, bool(reasons))})
    return plan


def _pick(names: list[str], reg: dict, quota, available, needed: bool) -> dict:
    from . import adapters
    tried = []
    for name in names:
        a = reg["agents"].get(name)
        why = "unknown agent" if a is None else _passes(name, a, "reviewer", reg)
        ad = None if why else adapters.for_runtime(a["runtime"])
        if not why and ad is None:
            why = f"no adapter for runtime {a['runtime']}"
        if not why and needed:
            up, reason = (available or ad.available)(a)
            q = None if not up else (quota or ad.quota)(a)
            why = reason if not up else (f"subscription window at {q['percent']}%" if q.get("exhausted") else None)
        if not why:
            return {"agent": name, "runtime": a["runtime"], "model": a["model"], "billing": a["billing"],
                    "tried": tried}
        tried.append({"agent": name, "why": why})
    return {"agent": None, "tried": tried}


def route(role: str, capability: str | None = None, model: str | None = None,
          reg: dict | None = None, exclude: tuple = ()) -> dict:
    """The first agent for `role` that passes every hard constraint.

    A requested model is honoured first by an agent whose own model matches it
    (so its billing route matches too), then by a runtime that can select any
    model (model_override)."""
    reg = reg or load()
    pol = reg["policy"]
    reasons = []
    passing = []
    for name, a in reg["agents"].items():
        if a["role"] != role or name in exclude:
            continue
        if a["billing"] not in pol["allowed_billing"].get(role, []) or a["billing"] in pol["forbidden_billing"]:
            reasons.append(f"{name}: billing {a['billing']} not allowed")
            continue
        if capability and capability not in a.get("capabilities", []):
            reasons.append(f"{name}: lacks {capability}")
            continue
        if model and model != a["model"] and not reg["runtimes"][a["runtime"]]["model_override"]:
            reasons.append(f"{name}: runtime {a['runtime']} cannot select model {model}")
            continue
        if role == "reviewer" and not a.get("read_only"):
            reasons.append(f"{name}: no enforced read-only")
            continue
        passing.append((name, a))
    for name, a in passing:
        if not model or a["model"] == model:
            return {"agent": name, **a}
    if passing:
        name, a = passing[0]
        return {"agent": name, **a, **({"model": model} if model else {})}
    raise PolicyError(f"no agent for role {role!r}" + (f" ({capability})" if capability else "")
                      + ("; " + "; ".join(reasons) if reasons else ""))


def check_manager_routes(by_model: list[dict], reg: dict | None = None) -> list[str]:
    """Policy problems in the Manager's actual usage (from manager_usage)."""
    reg = reg or load()
    allowed = reg["policy"]["allowed_billing"]["manager"]
    out = []
    for row in by_model:
        billing = classify_provider(row.get("provider"))
        row["billing"] = billing
        if billing not in allowed or billing in reg["policy"]["forbidden_billing"]:
            out.append(f"manager used {row.get('provider')}/{row.get('model')} → {billing} (allowed: {allowed})")
    return out
