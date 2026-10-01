# SPDX-License-Identifier: AGPL-3.0-or-later
"""Context pack per ticket (technical paper §3.1, phase 2).

In the scripted W1 run the builder used 230k tokens of which 34k were work:
the rest was re-reading context it had to discover by itself. The pack hands
both workers that context up front, in one file the ticket points to:

- the files the ticket changes (whole when small, else their outline);
- the files in the ticket's `read_hints:` (outline, or whole when small);
- a repository map focused on those files (repomap.py);
- the lessons whose paths or tags match the ticket (lessons.py);
- for the reviewer, the request for conclusions (RC-n) and suggestions (RS-n).

It is written to `_receipts/<ticket>.context.md`. Hydra-Pod is not changed:
its worker prompt says "read the whole ticket, then every file it lists", the
reviewer reads the ticket too, `preflight` ignores untracked files, and
`accept` commits `_receipts/<ticket>.*` with the work. The ticket only needs
the line `render_pointer` returns. The pack is a primer, not a fence: the
worker may still read anything in scope, and the ticket wins on any conflict.

Header keys used (plain `key: value`, so Hydra-Pod's own ticket parser accepts
them): `read_hints: a.py, b.py`, `complexity: S|M|L`, `risk: low|medium|high`.
"""

import hashlib
import time
from pathlib import Path

from . import blackboard, diffsum, lessons, repomap, shards, skills, steward, workflow

DEFAULT_TOKENS = 6000
WHOLE_FILE_LINES = 250       # a file to change is included whole up to this size
WHOLE_HINT_LINES = 80        # a read hint is included whole up to this size
COMPLEXITY = ("S", "M", "L")
RISK = ("low", "medium", "high")
REVIEWER_REQUEST = """After your findings, end the report with exactly these two sections. The manager answers every
line by its id, so keep one point per line and write `none` when a section is empty.

## Conclusions
RC-1 | <an observation that is not a defect: a design risk, a wrong assumption in the ticket, a gap in the tests> | <code evidence, file:line>

## Suggestions
RS-1 | <one concrete improvement> | <expected benefit> | <cost: S, M or L>

Report conclusions and suggestions only when they are about this change or the code it touches."""


def tokens_of(text: str) -> int:
    return len(text) // repomap.CHARS_PER_TOKEN


def split_list(value) -> list[str]:
    if isinstance(value, list):
        return [v.strip() for v in value if v.strip()]
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def outline(project, rel: str) -> str:
    try:
        defs, _ = repomap.extract((Path(project) / rel).read_text())
    except (UnicodeDecodeError, OSError):
        return "(not readable as text)"
    return "\n".join(f"{d['line']:>5} {d['sig']}" for d in defs) or "(no definitions found)"


def _file_block(project, rel: str, whole_up_to: int, budget_chars: int) -> str:
    p = Path(project) / rel
    if not p.exists():
        return f"### {rel}\n\n(does not exist yet: the ticket creates it)\n"
    if p.is_dir():
        return f"### {rel}/\n\n(a directory: files under it are in scope)\n"
    try:
        text = p.read_text()
    except (UnicodeDecodeError, OSError):
        return f"### {rel}\n\n(binary or unreadable)\n"
    n = len(text.splitlines())
    fence = "````" if "```" in text else "```"
    if n <= whole_up_to and len(text) <= budget_chars:
        return f"### {rel} ({n} lines, whole)\n\n{fence}\n{text.rstrip()}\n{fence}\n"
    return f"### {rel} ({n} lines, outline: read the parts you change)\n\n```\n{outline(project, rel)}\n```\n"


def build(project, task: str, max_tokens: int = DEFAULT_TOKENS) -> dict:
    path = diffsum.ticket_path(project, task)
    if path is None:
        raise ValueError(f"no ticket {task!r} under {project}/_tickets/")
    header = diffsum.ticket_header(project, task)
    body = path.read_text(errors="replace")
    allowed = split_list(header.get("allowed_files"))
    hints = [h for h in split_list(header.get("read_hints")) if h not in allowed]
    complexity, risk = (header.get("complexity") or "").upper() or None, (header.get("risk") or "").lower() or None
    if complexity and complexity not in COMPLEXITY:
        raise ValueError(f"complexity must be one of {COMPLEXITY}, not {complexity!r}")
    if risk and risk not in RISK:
        raise ValueError(f"risk must be one of {RISK}, not {risk!r}")
    budget = max_tokens * repomap.CHARS_PER_TOKEN
    focus = [f for f in allowed + hints if not f.endswith("/")]
    sections, used = [], 0

    def take(title: str, text: str) -> None:
        nonlocal used
        sections.append((title, text))
        used += len(text)

    memory = steward.read_sections(project)
    if memory:  # first, and the same for every ticket: a stable prefix
        take("Project memory (steward)", "".join(f"### {k}\n\n{v}\n\n" for k, v in memory.items()))
    attached = skills.match(project, allowed + hints, body)
    if attached:
        take("Project skills that apply to this ticket",
             "".join(f"### {n}\n\n{skills.body(project, n)}\n\n" for n in attached))

    files = "".join(_file_block(project, f, WHOLE_FILE_LINES, max(0, budget // 2 - used)) + "\n" for f in allowed)
    take("Files this ticket changes", files or "(the ticket lists no allowed_files)\n")
    if hints:
        take("Read these first (read_hints)",
             "".join(_file_block(project, f, WHOLE_HINT_LINES, max(0, (budget - used) // 3)) + "\n" for f in hints))
    found = lessons.relevant(project, allowed + hints, body)
    if found:
        take("Lessons from earlier tickets",
             "\n".join(f"- {x['text']} ({x['workflow_id']}, {x['date']})" for x in found) + "\n")
    wid = workflow.workflow_id_for(task)
    alloc = shards.latest_allocations(project).get(wid, {}) if (Path(project) / "_receipts").exists() else {}
    shard_ids = alloc.get("touch", []) + alloc.get("reads", []) + list(alloc.get("keepers", {}))
    size = alloc.get("max_shard_tokens", shards.MAX_SHARD_TOKENS)
    if not shard_ids:
        shard_ids = [s["id"] for s in shards.shards_for(shards.partition(project, size), allowed + hints)]
    known = blackboard.facts(project, shard_ids, max_shard_tokens=size)[-15:]
    if known:
        take("Established facts (shared blackboard; each still matches its files)",
             "\n".join(f"- {f['answer']} ({_refs(f)})" for f in known) + "\n")
    if alloc.get("keepers"):
        take("Ask instead of reading",
             "These parts of the project are held by other models. Do not read their files; ask, at most "
             f"{blackboard.MAX_QUESTIONS} questions for this ticket, one fact each:\n\n"
             + "".join(f"- {sid}: `hydra-pod-dsh ask \"<question>\" --wf {wid} --shard {sid}`\n"
                       for sid in alloc["keepers"]))
    map_tokens = max(300, min(1500, (budget - used) // repomap.CHARS_PER_TOKEN - 400))
    rmap = repomap.build(project, focus, map_tokens)
    take("Repository map (definitions ranked by relevance to this ticket: line, signature)",
         f"```\n{rmap['text']}\n```\n")
    take("For the reviewer", REVIEWER_REQUEST + "\n")
    head = workflow.git_head(project)
    text = (f"# Context pack: {path.stem}\n\n"
            f"Generated by hydra-pod-dsh for git {(head or 'no commit')[:12]}"
            + (f", complexity {complexity}" if complexity else "") + (f", risk {risk}" if risk else "")
            + ". Reference only: the ticket wins on any conflict, and you may read any file in scope. "
              "Do not edit this file.\n\n"
            + "\n".join(f"## {t}\n\n{s}" for t, s in sections))
    return {"ticket": path.stem, "path": f"_receipts/{path.stem}.context.md", "text": text,
            "tokens": tokens_of(text), "budget_tokens": max_tokens, "complexity": complexity, "risk": risk,
            "files": allowed, "read_hints": hints, "lessons": len(found), "map_files": rmap["files_shown"],
            "skills": attached,
            "sha256": hashlib.sha256(text.encode()).hexdigest()[:16], "git_head": head,
            "ticket_points_to_pack": f"_receipts/{path.stem}.context.md" in body}


def write(project, wid: str, task: str | None = None, max_tokens: int = DEFAULT_TOKENS) -> dict:
    w = workflow.get(project, wid)
    task = task or w.task_id
    workflow.validate_ticket_id(task)
    p = build(project, task, max_tokens)
    out = Path(project) / p["path"]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(p["text"])
    workflow.record(project, wid, "hydra/context-pack", ignorable=True,
                    payload={k: p[k] for k in ("ticket", "path", "tokens", "budget_tokens", "complexity", "risk",
                                               "files", "read_hints", "lessons", "map_files", "skills", "sha256",
                                               "git_head")}
                    | {"at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    return p


def _refs(fact: dict) -> str:
    return ", ".join(f"{r['path']}:{r['line']}" for r in fact["refs"]) or fact["shard"]


def render_pointer(p: dict) -> str:
    """The line the ticket body must carry so both workers read the pack."""
    return (f"Context pack: {p['path']} (worker: read it before the code; "
            "reviewer: answer its \"For the reviewer\" section)")
