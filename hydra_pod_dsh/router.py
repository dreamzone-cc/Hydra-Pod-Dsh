# SPDX-License-Identifier: AGPL-3.0-or-later
"""Router and policy checks (architecture §11, §12, §19, §37a rev 2).

Hard constraints, checked before any preference:
1. the agent's billing route is allowed for the role, and not forbidden;
2. the runtime can honour a requested model (capability flags);
3. the Manager/Verifier model is never the Executor's or the Reviewer's;
4. a Reviewer runs under enforced read-only.
`classify_provider` maps a DSH session provider id to a billing route so the
Manager's real route (seen in its session log) can be checked too.
"""

import json
from pathlib import Path

REGISTRY = Path(__file__).resolve().parent.parent / "agents.json"


class PolicyError(Exception):
    """No agent satisfies the hard constraints."""


def load(path: Path | None = None) -> dict:
    return json.loads((path or REGISTRY).read_text())


def classify_provider(provider: str | None) -> str:
    """Billing route of a DSH provider id as it appears in session logs."""
    p = (provider or "").lower()
    if p in ("pi-anthropic",) or (p.startswith("pi-") and "anthropic" in p):
        return "oauth/claude-pro"        # consumer OAuth bridged into DSH: operator-approved for the manager (2026-09-30)
    if p.startswith("pi-"):
        return f"oauth/{p[3:]}"
    if p == "anthropic":
        return "api/anthropic"           # unless an OAuth bridge rewrote this provider (check plugins)
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
        if a["role"] == "reviewer" and not a.get("read_only"):
            out.append(f"{name}: reviewer without enforced read-only")
    managers = {a["model"] for a in by_role.get("manager", [])}
    for role in ("executor", "reviewer"):
        for a in by_role.get(role, []):
            if a["model"] in managers:
                out.append(f"manager model {a['model']} is also a {role}")
    return out


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
