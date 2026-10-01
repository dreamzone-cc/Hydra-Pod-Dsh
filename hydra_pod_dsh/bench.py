# SPDX-License-Identifier: AGPL-3.0-or-later
"""Benchmark report: the gate metrics of the technical paper (§7.1), per workflow.

Phase 0 runs a fixed set of tickets and saves this report as the baseline;
every later phase runs the same tickets and `compare`s against it. Everything
is read from records that already exist (ledger, cost log, DSH session logs,
review reports, git); nothing is estimated and a metric that cannot be measured
stays None, never zero.
"""

import statistics
import subprocess

from . import findings, manager_usage, resources, tokens, workflow

# metric -> (label, True when lower is better)
GATES = {"manager_work_tokens": ("manager work tokens", True),
         "manager_cache_hit_ratio": ("manager cache hit ratio", False),
         "executor_work_tokens": ("executor work tokens", True),
         "reviewer_work_tokens": ("reviewer work tokens", True),
         "review_tokens_per_100_lines": ("review tokens / 100 changed lines", True),
         "first_try_rate": ("first-try approval rate", False),
         "finding_precision": ("reviewer finding precision", False),
         "plan_to_done_seconds": ("PLAN_READY → DONE seconds", True)}


def changed_lines(project, base: str | None, head: str | None) -> int | None:
    if not base or not head or base == head:
        return None
    r = subprocess.run(["git", "-C", str(project), "diff", "--numstat", base, head],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return None
    total = 0
    for line in r.stdout.splitlines():
        a, d, _ = line.split("\t", 2)
        total += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    return total


def workflow_metrics(project, w: "workflow.Workflow") -> dict:
    events = workflow.timeline(project, w.id)
    heads = [(e.get("payload") or {}).get("git_head") for e in events]
    heads = [h for h in heads if h]
    at = {}
    for e in events:
        to = (e.get("payload") or {}).get("to")
        if to and to not in at:
            at[to] = e["at"]
    roles = {}
    for r in resources.summary(project, w.id)["by_route"]:
        roles[r["role"]] = roles.get(r["role"], 0) + r["work_tokens"]
    m = manager_usage.usage_for(str(project), w.created_at, w.updated_at if w.state in workflow.TERMINAL else None)
    mt = tokens.zero()
    for row in m["by_model"]:
        mt = tokens.add(mt, row["tokens"])
    st = findings.summary(project, w.tasks)["by_status"]
    judged = st["valid"] + st["partial"] + st["rejected"]
    lines = changed_lines(project, heads[0] if heads else None, heads[-1] if heads else None)
    reviewer = roles.get("reviewer")
    done = w.state == "DONE"
    return {"project": str(project), "workflow_id": w.id, "state": w.state, "tasks": len(w.tasks),
            "first_try": (w.rework_attempts == 0 and w.replans == 0) if done else None,
            "manager_work_tokens": tokens.work(mt) if m["sessions"] else None,
            "manager_cache_hit_ratio": tokens.cache_hit_ratio(mt) if m["sessions"] else None,
            "executor_work_tokens": roles.get("executor"), "reviewer_work_tokens": reviewer,
            "changed_lines": lines,
            "review_tokens_per_100_lines": round(100 * reviewer / lines, 1) if reviewer and lines else None,
            "finding_precision": round((st["valid"] + st["partial"]) / judged, 3) if judged else None,
            "plan_to_done_seconds": round(at["DONE"] - at["PLAN_READY"], 1)
            if "DONE" in at and "PLAN_READY" in at else None}


def report(projects: list) -> dict:
    rows = []
    for p in projects:
        rows += [workflow_metrics(p, w) for w in workflow.load(p).values()]
    done = [r for r in rows if r["state"] == "DONE"]
    summary = {}
    for key in GATES:
        if key == "first_try_rate":
            vals = [r["first_try"] for r in done]
            summary[key] = round(sum(vals) / len(vals), 3) if vals else None
            continue
        vals = [r[key] for r in done if r[key] is not None]
        summary[key] = round(statistics.median(vals), 3) if vals else None
    return {"workflows": rows, "done": len(done), "summary": summary}


def compare(base: dict, new: dict) -> list[dict]:
    """Per gate metric: baseline median, new median, relative change, and whether it improved."""
    out = []
    for key, (label, lower_better) in GATES.items():
        a, b = base["summary"].get(key), new["summary"].get(key)
        change = None if a in (None, 0) or b is None else round((b - a) / a, 3)
        better = None if change is None else (change < 0 if lower_better else change > 0) or change == 0
        out.append({"metric": key, "label": label, "baseline": a, "new": b, "change": change, "ok": better})
    return out


def render(r: dict) -> str:
    def v(x):
        if isinstance(x, bool):
            return "yes" if x else "no"
        return "-" if x is None else f"{x:.1%}" if isinstance(x, float) and x <= 1 else f"{x:,}"
    lines = [f"{len(r['workflows'])} workflow(s), {r['done']} done"]
    for w in r["workflows"]:
        lines.append(f"  {w['workflow_id']:<16} {w['state']:<10} first_try={v(w['first_try'])} "
                     f"mgr={v(w['manager_work_tokens'])} hit={v(w['manager_cache_hit_ratio'])} "
                     f"exec={v(w['executor_work_tokens'])} rev={v(w['reviewer_work_tokens'])} "
                     f"lines={v(w['changed_lines'])} t={v(w['plan_to_done_seconds'])}s")
    lines.append("  median over done workflows:")
    for key, (label, _) in GATES.items():
        lines.append(f"    {label:<36} {v(r['summary'][key])}")
    return "\n".join(lines)


def render_compare(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        ch = "-" if r["change"] is None else f"{r['change']:+.1%}"
        mark = "" if r["ok"] is None else "ok" if r["ok"] else "WORSE"
        lines.append(f"  {r['label']:<36} {r['baseline']!s:>12} → {r['new']!s:<12} {ch:>8} {mark}")
    return "\n".join(lines)
