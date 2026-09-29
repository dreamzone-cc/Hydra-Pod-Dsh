# SPDX-License-Identifier: AGPL-3.0-or-later
"""Workflow budgets (architecture §17): 80% warns, 100% blocks further dispatch.

Limits are set at `wf start` (`--budget-cost`, `--budget-credits`, `--budget-minutes`,
`--budget-manager-tokens`) and stored in the workflow's creation event. Usage comes
from the ledger's resource records (external workers) and, for manager tokens,
from the DSH session logs. A dimension without a limit is not checked; a limit
whose usage is unknown is reported as unknown, never as within budget (it does not
block dispatch, but the overall level is `unknown` so the manager must look).
"""

from . import manager_usage, resources, workflow

WARN, BLOCK = 0.8, 1.0
DIMENSIONS = {"max_cost_usd": "list_cost_usd", "max_zai_credits": "zai_credits",
              "max_runtime_minutes": "seconds", "max_manager_tokens": None}


def check(project, wid: str, manager=None) -> dict:
    w = workflow.get(project, wid)
    limits = w.budget
    totals = resources.summary(project, wid)["total"]
    rows = []
    for key, limit in limits.items():
        if key == "max_manager_tokens":
            m = manager if manager is not None else manager_usage.usage_for(
                str(project), w.created_at, None if w.state not in workflow.TERMINAL else w.updated_at)
            used = sum(r["input"] + r["output"] for r in m["by_model"]) if m["sessions"] else None
        else:
            # No run yet means nothing spent; a run that did not report the metric stays unknown.
            used = 0 if totals["runs"] == 0 else totals.get(DIMENSIONS[key])
            if key == "max_runtime_minutes" and used is not None:
                used = used / 60
        ratio = None if used is None or not limit else used / limit
        level = "unknown" if ratio is None else "block" if ratio >= BLOCK else "warn" if ratio >= WARN else "ok"
        rows.append({"dimension": key, "limit": limit, "used": None if used is None else round(used, 4),
                     "percent": None if ratio is None else round(100 * ratio, 1), "level": level})
    levels = {r["level"] for r in rows}
    worst = next((lv for lv in ("block", "warn", "unknown") if lv in levels), "ok")
    return {"workflow_id": wid, "level": worst, "dimensions": rows}
