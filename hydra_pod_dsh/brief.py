# SPDX-License-Identifier: AGPL-3.0-or-later
"""A short resume brief of one workflow (technical paper §3.1, phase 1).

After a context compaction (or in a new session) the manager needs the state
of a workflow, not its history. `build` folds what `wf show`, `wf timeline`,
`wf findings` and `wf budget` would print into one block of at most ~30 lines,
ending with the moves allowed next, so the manager can continue without
re-reading the ledger, the tickets or earlier reports. Read-only.
"""

import time

from . import budget, findings, workflow

RECENT = 5


def next_moves(w: "workflow.Workflow") -> list[str]:
    """What the state machine accepts from the current state (advance targets, then decisions)."""
    moves = sorted(workflow.FORWARD.get(w.state, ()))
    moves += sorted(workflow.DECIDE_IN.get(w.state, ()), key=workflow.DECISIONS.index)
    if w.state == "BLOCKED":
        moves.append("waiting for the user (wf human … resume|cancel)")
    return moves


def build(project, wid: str) -> dict:
    w = workflow.get(project, wid)
    events = [e for e in workflow.timeline(project, wid) if e["type"] != "hydra/resource-usage"]
    fs = findings.summary(project, w.tasks)
    open_items = [{"task": r["task_id"], "severity": f["severity"], "location": f["location"],
                   "problem": f["problem"][:100]}
                  for r in fs["reports"] for f in r["findings"] if f["status"] == "unverified"]
    open_items += [{"task": r["task_id"], "severity": i["id"], "location": i["kind"], "problem": i["text"][:100]}
                   for r in fs["reports"] for i in r["items"] if i["status"] == "unverified"]
    b = budget.check(project, wid) if w.budget else None
    amended = [e for e in events if e["type"] == "hydra/task-amended"]
    return {"workflow": w.to_dict(), "next": next_moves(w), "findings": {"by_status": fs["by_status"],
            "unverified": open_items}, "budget": b, "amendments": len(amended),
            "recent": [{"at": e["at"], "type": e["type"].split("/", 1)[1],
                        "move": (e.get("payload") or {}).get("to"),
                        "what": (e.get("payload") or {}).get("decision") or (e.get("payload") or {}).get("action"),
                        "reason": (e.get("reason") or "")[:100]} for e in events[-RECENT:]]}


def render(b: dict) -> str:
    w = b["workflow"]
    pol = w["policy"]
    lines = [f"{w['id']}: {w['state']} — {w['objective']}",
             f"  task {w['task_id']} (tasks {', '.join(w['tasks'])}); rework {w['rework_attempts']}/"
             f"{pol['max_rework_attempts']}, review {w['review_cycles']}/{pol['max_review_cycles']}, "
             f"replan {w['replans']}/{pol['max_replans']}; git {(w['git_head'] or '-')[:12]}"]
    d = w.get("last_decision")
    if d:
        lines.append(f"  last decision: {d['decision']} — {(d.get('reason') or '')[:100]}")
    if b["amendments"]:
        lines.append(f"  ticket amended {b['amendments']} time(s) (wf timeline for details)")
    st = {k: v for k, v in b["findings"]["by_status"].items() if v}
    lines.append(f"  findings: {st or 'none'}")
    for f in b["findings"]["unverified"][:5]:
        lines.append(f"    UNVERIFIED [{f['severity']}] {f['task']} {f['location']} — {f['problem']}")
    if b["budget"]:
        worst = [f"{d['dimension']} {d['percent']}%" for d in b["budget"]["dimensions"] if d["percent"] is not None]
        lines.append(f"  budget: {b['budget']['level']}" + (f" ({', '.join(worst)})" if worst else ""))
    lines.append("  recent:")
    for e in b["recent"]:
        what = " ".join(x for x in (e["what"], f"→ {e['move']}" if e["move"] else "") if x)
        lines.append(f"    {time.strftime('%m-%d %H:%M', time.localtime(e['at']))} {e['type']} {what}"
                     + (f" — {e['reason']}" if e["reason"] else ""))
    lines.append(f"  next: {', '.join(b['next']) or 'none (terminal)'}")
    return "\n".join(lines)
