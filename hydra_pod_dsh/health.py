"""Workflow health (architecture §21, rev 2 G17): running, waiting, stalled or ready.

A worker state (EXECUTING, REVIEWING) is `running` while its process is alive in
the project, `ready` once the worker's receipt/report is newer than the state
change, and `stalled` when neither holds after a grace period (the worker died,
or the host restarted). BLOCKED is `waiting_for_user`, not a failure, and is
excluded from runtime budgets. Nothing here changes state; `wf recover` does.
"""

import time
from pathlib import Path

from . import live, workflow

GRACE_SECONDS = 120
ARTIFACT = {"EXECUTING": "receipt", "REVIEWING": "review"}


def _entered_at(project, wid: str, state: str) -> float | None:
    at = None
    for e in workflow.timeline(project, wid):
        if (e.get("payload") or {}).get("to") == state:
            at = e["at"]
    return at


def _artifact_after(project, task: str | None, kind: str, since: float) -> bool:
    if not task:
        return False
    rec = Path(project) / "_receipts"
    pats = [f"{task}.{kind}*.md", f"{task}-*.{kind}*.md"]
    return any(f.stat().st_mtime >= since for pat in pats for f in rec.glob(pat))


def classify(project, wid: str, workers=None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    w = workflow.get(project, wid)
    out = {"workflow_id": wid, "state": w.state}
    if w.state in workflow.TERMINAL:
        return {**out, "health": "finished"}
    if w.state == "BLOCKED":
        return {**out, "health": "waiting_for_user", "blocked_from": w.blocked_from}
    if w.state not in ARTIFACT:
        return {**out, "health": "manager"}  # the manager's own step
    since = _entered_at(project, wid, w.state) or w.updated_at
    workers = live.running_workers() if workers is None else workers
    proj = str(Path(project).resolve())
    if any(str(Path(x.get("cwd") or "/").resolve()) == proj for x in workers):
        return {**out, "health": "running", "since": since}
    if _artifact_after(project, w.task_id, ARTIFACT[w.state], since):
        return {**out, "health": "ready", "since": since}
    if now - since < GRACE_SECONDS:
        return {**out, "health": "starting", "since": since}
    return {**out, "health": "stalled", "since": since, "recover_to": workflow.RECOVERY[w.state]}
