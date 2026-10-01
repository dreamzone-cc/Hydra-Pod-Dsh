# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lessons memory (technical paper §3.2, phase 2): what went wrong once, written down
so the next ticket that touches the same code does not repeat it.

The manager records a lesson after a REWORK, a rejected or valid finding worth
remembering, or an accepted reviewer conclusion. Lessons live in
`_receipts/lessons.md` (committed with the tickets, readable by people), one
line each:

    - 2026-10-01 · WF-T6 · paths: src/text.py, tests/ · tags: unicode · <the lesson>

`relevant` picks the lessons whose paths overlap a ticket's files or whose tags
appear in its text; only those go into the context pack, never the whole file.
"""

import re
import time
from pathlib import Path

LINE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) · (\S+) · paths: (.*?) · tags: (.*?) · (.+)$")
HEADER = "# Lessons\n\nWritten by the manager (hydra-pod-dsh lesson add). One line per lesson; edit with care.\n\n"
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def path(project) -> Path:
    return Path(project) / "_receipts" / "lessons.md"


def _clean(text: str) -> str:
    return " ".join(text.replace("·", "-").split())


def add(project, text: str, paths: list[str] | None = None, tags: list[str] | None = None,
        workflow_id: str | None = None, now: float | None = None) -> dict:
    text = _clean(text or "")
    if len(text) < 10:
        raise ValueError("a lesson needs at least 10 characters of text")
    if len(text) > 400:
        raise ValueError("keep a lesson under 400 characters: one rule, not a story")
    paths = [_clean(p).replace(",", "") for p in paths or [] if p.strip()]
    tags = [t.strip().lower() for t in tags or [] if t.strip()]
    bad = [t for t in tags if not TAG_RE.match(t)]
    if bad:
        raise ValueError(f"tags must be lowercase words (a-z0-9_-): {bad}")
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    entry = {"date": day, "workflow_id": workflow_id or "-", "paths": paths, "tags": tags, "text": text}
    f = path(project)
    f.parent.mkdir(parents=True, exist_ok=True)
    existing = f.read_text() if f.exists() else HEADER
    if not existing.endswith("\n"):
        existing += "\n"
    f.write_text(existing + f"- {day} · {entry['workflow_id']} · paths: {', '.join(paths) or '-'} · "
                 f"tags: {', '.join(tags) or '-'} · {text}\n")
    return entry


def load(project) -> list[dict]:
    f = path(project)
    if not f.exists():
        return []
    out = []
    for line in f.read_text(errors="replace").splitlines():
        m = LINE.match(line.strip())
        if m:
            split = lambda s: [] if s.strip() == "-" else [x.strip() for x in s.split(",") if x.strip()]  # noqa: E731
            out.append({"date": m.group(1), "workflow_id": m.group(2), "paths": split(m.group(3)),
                        "tags": split(m.group(4)), "text": m.group(5)})
    return out


def _overlap(a: str, b: str) -> bool:
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def relevant(project, paths: list[str], text: str = "", limit: int = 8) -> list[dict]:
    """Lessons for a ticket: path overlap counts double, a tag found in the ticket text once; newest first on ties."""
    words = set(re.findall(r"[a-z0-9_-]+", text.lower()))
    scored = []
    for i, les in enumerate(load(project)):
        score = 2 * sum(any(_overlap(p, q) for q in paths) for p in les["paths"])
        score += sum(t in words for t in les["tags"])
        if score:
            scored.append((score, i, les))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [les for _, _, les in scored[:limit]]
