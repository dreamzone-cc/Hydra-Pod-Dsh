# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ledger ↔ ticket-folder consistency (architecture §6 rev 2, ADR-011).

Hydra-Pod moves ticket files between `_tickets/{open,doing,done,blocked,dropped}`
(hydra-pod-dispatch claim/close); the ledger records the workflow state. They
are written by different tools, so they can drift (a step run without its `wf`
call, or the reverse). `check` reports every disagreement; it never moves a file
or appends an event — the manager decides which side is right.
"""

from pathlib import Path

from . import workflow

FOLDERS = ("open", "doing", "done", "blocked", "dropped")


def ticket_folder(project, task: str) -> str | None:
    """Folder holding `<task>.md` or `<task>-<slug>.md`, or None."""
    base = Path(project) / "_tickets"
    for folder in FOLDERS:
        d = base / folder
        if (d / f"{task}.md").exists() or any(d.glob(f"{task}-*.md")):
            return folder
    return None


def check(project, wid: str | None = None) -> list[dict]:
    """Disagreements between the ledger and the ticket folders (empty list = consistent)."""
    problems = []
    wfs = workflow.load(project)
    if wid is not None and wid not in wfs:
        raise workflow.TransitionError(f"no workflow {wid!r}")
    items = [wfs[wid]] if wid else list(wfs.values())
    for w in items:
        current = w.task_id
        actual = ticket_folder(project, current) if current else None
        expected = w.folder
        if actual is None:
            if w.state not in ("NEW", "PLANNING"):  # the ticket file may not be written yet
                problems.append({"workflow_id": w.id, "task_id": current, "kind": "missing-ticket",
                                 "detail": f"state {w.state} expects _tickets/{expected}/{current}*.md"})
        elif actual != expected:
            # APPROVED waits for `hydra-pod-dispatch close`, which moves the ticket to done/.
            if not (w.state == "APPROVED" and actual == "done"):
                problems.append({"workflow_id": w.id, "task_id": current, "kind": "folder-mismatch",
                                 "detail": f"ledger state {w.state} expects {expected}/, ticket is in {actual}/"})
        for t in w.tasks:
            if t != current and ticket_folder(project, t) in ("open", "doing"):
                problems.append({"workflow_id": w.id, "task_id": t, "kind": "earlier-task-open",
                                 "detail": f"earlier task {t} is still in {ticket_folder(project, t)}/"})
    known = {t for w in wfs.values() for t in w.tasks}
    base = Path(project) / "_tickets" / "doing"
    for f in sorted(base.glob("T*.md")) if base.exists() else []:
        tid = f.stem.split("-")[0]
        if tid not in known and f.stem not in known:
            problems.append({"workflow_id": None, "task_id": f.stem, "kind": "untracked-ticket",
                             "detail": f"_tickets/doing/{f.name} has no workflow in the ledger"})
    return problems
