# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-stage accounting: which model worked in each stage, its tokens, and its quota share.

A workflow is cut into stages by its ledger state events (each entry into a
state opens a stage, so a second EXECUTING after REWORK is its own row). Then:
- worker runs (cost log, mirrored as `hydra/resource-usage`) land in the stage
  that was open when the run ended (`run_at`); older records without `run_at`
  fall back to the last EXECUTING (build) or REVIEWING (review) stage;
- manager messages (DSH session logs) land in the stage open at their time;
- each worker's share of its subscription is shown against the current windows:
  Z.ai credits vs the official 5 h / weekly limits (with their reset times),
  OpenCode Go list cost vs the per-model 5 h / weekly limits (an estimate).
  The manager's API route has no windows (pay per use).
"""

import time

from . import manager_usage, resources, router, tokens, usage, workflow

PHASE_STATE = {"build": "EXECUTING", "review": "REVIEWING"}


def _intervals(project, wid: str, end_now: float) -> list[dict]:
    w = workflow.get(project, wid)
    stages = [{"n": 1, "state": "NEW", "start": w.created_at, "end": None, "reason": None}]
    for e in workflow.timeline(project, wid):
        p = e.get("payload") or {}
        if e["type"] in workflow.STATE_EVENTS and "to" in p:
            stages[-1]["end"] = e["at"]
            stages.append({"n": len(stages) + 1, "state": p["to"], "start": e["at"], "end": None,
                           "reason": e.get("reason"), "event": e["type"].split("/", 1)[1]})
    last = stages[-1]
    last["end"] = last["start"] if last["state"] in workflow.TERMINAL else None
    for s in stages:
        s["seconds"] = round((s["end"] if s["end"] is not None else end_now) - s["start"], 1)
        s["open"] = s["end"] is None
        s["actors"] = {}
    return stages


def _find(stages, t: float | None, phase: str | None):
    if t is not None:
        for s in stages:
            if s["start"] <= t and (s["end"] is None or t < s["end"] or s is stages[-1]):
                return s
    want = PHASE_STATE.get(phase or "")
    return next((s for s in reversed(stages) if s["state"] == want), None)


def _actor(stage, key, **base):
    return stage["actors"].setdefault(key, {**base, "tokens": tokens.zero(), "runs": 0, "messages": 0,
                                            "list_cost_usd": None, "zai_credits": None})


def _add_num(a: dict, k: str, v):
    if isinstance(v, (int, float)):
        a[k] = round((a[k] or 0) + v, 6)


def _window(u: dict, name: str) -> dict | None:
    return next((w for w in u.get("windows", []) if w["window"] == name), None)


def _quota(actor: dict, go: dict, zai: dict) -> dict | None:
    """This actor's share of its subscription windows, and when they reset."""
    out = {}
    if actor["billing"] == "subscription/zai-lite" and actor["zai_credits"] is not None:
        for name in ("5h", "week"):
            w = _window(zai, name)
            if w and w.get("limit"):
                out[name] = {"used": actor["zai_credits"], "limit": w["limit"],
                             "percent": round(100 * actor["zai_credits"] / w["limit"], 2),
                             "resets_at": w["resets_at"], "window_percent_now": w["percent"]}
        return {"source": "official", "unit": "credits", **out} if out else None
    if actor["billing"] == "subscription/opencode-go" and actor["list_cost_usd"] is not None:
        for name in ("5h", "week"):
            w = _window(go, name)
            if w and w.get("limit_usd"):
                out[name] = {"used": actor["list_cost_usd"], "limit": w["limit_usd"],
                             "percent": round(100 * actor["list_cost_usd"] / w["limit_usd"], 2),
                             "resets_at": w["resets_at"], "window_percent_now": w["percent"]}
        return {"source": "estimate", "unit": "usd", **out} if out else None
    return None


def build(project, wid: str, now: float | None = None, go: dict | None = None, zai: dict | None = None,
          manager: dict | None = None, sync: bool = True) -> dict:
    """`sync=False` keeps it read-only (the UI polls `status`, which must never write the ledger)."""
    now = time.time() if now is None else now
    if sync:
        resources.sync(project, wid)
    w = workflow.get(project, wid)
    stages = _intervals(project, wid, now)
    unplaced = []
    models = set()
    for e in workflow.timeline(project, wid):
        if e["type"] != "hydra/resource-usage":
            continue
        p = e["payload"]
        s = _find(stages, p.get("run_at"), p.get("phase"))
        if s is None:
            unplaced.append(p.get("run_key"))
            continue
        a = _actor(s, ("worker", p.get("role"), p.get("model")), kind="worker", role=p.get("role"),
                   model=p.get("model"), provider=p.get("provider"), billing=p.get("billing"))
        a["runs"] += 1
        a["tokens"] = tokens.add(a["tokens"], tokens.normalize(p.get("tokens")))
        _add_num(a, "list_cost_usd", p.get("list_cost_usd"))
        _add_num(a, "zai_credits", p.get("zai_credits"))
        if p.get("model", "").startswith("opencode-go/"):
            models.add(p["model"])
    end = w.updated_at if w.state in workflow.TERMINAL else None
    mgr = manager if manager is not None else manager_usage.records_for(str(project), w.created_at, end)
    prices = manager_usage.pricing()
    for r in mgr["records"]:
        s = _find(stages, r["time"], None)
        if s is None:
            continue
        a = _actor(s, ("manager", r["provider"], r["model"]), kind="manager", role="manager", model=r["model"],
                   provider=r["provider"], billing=router.classify_provider(r["provider"]))
        a["messages"] += 1
        a["tokens"] = tokens.add(a["tokens"], r["tokens"])
    zai = zai if zai is not None else usage.zai_usage(now)
    go_by_model = go if go is not None else {m: usage.go_usage(m, now) for m in models}
    rows = []
    for s in stages:
        actors = []
        for a in s["actors"].values():
            a["work_tokens"] = tokens.work(a["tokens"])
            if a["kind"] == "manager":
                a["cost_usd"] = manager_usage.cost(prices, a["provider"], a["model"], a["tokens"])
            gu = go_by_model.get(a["model"], {}) if isinstance(go_by_model, dict) else {}
            a["quota"] = _quota(a, gu if "windows" in gu else {}, zai)
            actors.append(a)
        rows.append({k: s[k] for k in ("n", "state", "start", "end", "seconds", "open", "reason")} |
                    {"actors": actors, "work_tokens": sum(a["work_tokens"] for a in actors)})
    return {"workflow_id": wid, "manager_sessions": mgr.get("sessions", []), "stages": rows,
            "unplaced_runs": unplaced}


def render(st: dict, now: float | None = None) -> str:
    now = time.time() if now is None else now
    lines = [f"{st['workflow_id']}: {len(st['stages'])} stage(s)"]
    for s in st["stages"]:
        dur = f"{int(s['seconds'] // 60)}m{int(s['seconds'] % 60):02d}s"
        lines.append(f"  #{s['n']:<2} {s['state']:<11} {time.strftime('%H:%M:%S', time.localtime(s['start']))} "
                     f"{dur:>7}{'  (open)' if s['open'] else ''}")
        for a in s["actors"]:
            who = f"{a['role']}: {a['model']} [{a['billing']}]"
            extra = []
            if a["runs"]:
                extra.append(f"{a['runs']} run(s)")
            if a["messages"]:
                extra.append(f"{a['messages']} msg")
            if a.get("list_cost_usd") is not None:
                extra.append(f"list ${a['list_cost_usd']:.4f}")
            if a.get("zai_credits") is not None:
                extra.append(f"{int(a['zai_credits'])} credits")
            if a["kind"] == "manager":
                extra.append("cost " + ("unknown" if a.get("cost_usd") is None else f"${a['cost_usd']:.4f}"))
            lines.append(f"       {who}")
            lines.append(f"         tokens {tokens.fmt(a['tokens'])} (work {tokens.human(a['work_tokens'])})"
                         + (" · " + " · ".join(extra) if extra else ""))
            q = a.get("quota")
            if q:
                for name in ("5h", "week"):
                    if name in q:
                        x = q[name]
                        lines.append(f"         {name:<4} this stage {x['percent']}% of the window "
                                     f"({q['source']}); window now {x['window_percent_now']}%, "
                                     f"resets in {usage.left(x['resets_at'], now)}")
    if st["unplaced_runs"]:
        lines.append(f"  unplaced runs: {len(st['unplaced_runs'])}")
    return "\n".join(lines)
