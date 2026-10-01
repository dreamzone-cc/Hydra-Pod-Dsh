# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic diff summary for the manager (technical paper §3.1, phase 1).

The manager used to read every builder diff in full, on the most expensive
model. `summarize` gives it the shape of the change instead: files with their
line counts, which of them fall outside the ticket's `allowed_files`, the
top-level symbols added or removed, and which files are tests. The manager
then reads the raw diff only for the files that need judgment (`git diff
<base> <head> -- <file>`). No model is called; nothing is written.

The range comes from the ticket header (`base` set by `hydra-pod-dispatch
claim`, `head` set by `accept`), and can be overridden.
"""

import fnmatch
import re
import subprocess
from pathlib import Path

from . import consistency

# Top-level definitions in the languages Hydra-Pod projects use most. A changed
# line that matches is reported as an added or removed symbol.
SYMBOL = re.compile(
    r"^(?:async\s+)?def\s+(\w+)|^class\s+(\w+)"                          # Python
    r"|^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\*?\s+(\w+)"   # JS/TS
    r"|^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\("   # JS arrow functions
    r"|^(?:export\s+)?(?:interface|type|enum)\s+(\w+)"                   # TS
    r"|^func\s+(?:\([^)]*\)\s*)?(\w+)"                                   # Go
    r"|^(?:pub(?:\([^)]*\))?\s+)?(?:fn|struct|enum|trait)\s+(\w+)")      # Rust
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+$|[._-](test|spec)\.[^/]+$")
HEADER_LIST_KEYS = ("allowed_files",)


def ticket_path(project, task: str) -> Path | None:
    """`_tickets/<folder>/<task>.md` or `<task>-<slug>.md`, or None."""
    folder = consistency.ticket_folder(project, task)
    if folder is None:
        return None
    d = Path(project) / "_tickets" / folder
    return d / f"{task}.md" if (d / f"{task}.md").exists() else sorted(d.glob(f"{task}-*.md"))[0]


def ticket_header(project, task: str) -> dict:
    """The ticket's header: `allowed_files` as a list, every other key as text ({} when absent)."""
    path = ticket_path(project, task)
    if path is None:
        return {}
    text = path.read_text(errors="replace")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return {}
    header, key = {}, None
    for line in text[4:text.index("\n---\n", 4)].splitlines():
        item = re.match(r"^\s+-\s?(.*)$", line)
        if item and key in HEADER_LIST_KEYS:
            header[key].append(item.group(1).strip())
            continue
        kv = re.match(r"^([a-z_]+):\s*(.*?)\s*(?:#.*)?$", line)
        if kv:
            key = kv.group(1)
            header[key] = [kv.group(2)] if key in HEADER_LIST_KEYS and kv.group(2) else \
                [] if key in HEADER_LIST_KEYS else kv.group(2)
    return header


def _git(project, *args) -> str:
    r = subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise ValueError(f"git {' '.join(args[:2])} failed: {r.stderr.strip() or r.returncode}")
    return r.stdout


def in_scope(path: str, allowed: list[str]) -> bool:
    """Whether `path` is covered by an allowed_files entry (exact, a directory, or a glob)."""
    for a in allowed:
        a = a.rstrip("/")
        if path == a or path.startswith(a + "/") or fnmatch.fnmatchcase(path, a):
            return True
    return False


def summarize(project, task: str | None = None, base: str | None = None, head: str | None = None) -> dict:
    """Summary of `base..head` (head None = the working tree) for the manager."""
    header = ticket_header(project, task) if task else {}
    base = base or header.get("base") or None
    head = head or header.get("head") or None
    if not base:
        raise ValueError("no base revision: pass --base, or claim the ticket first (it sets base:)")
    rng = [base] + ([head] if head else [])
    allowed = header.get("allowed_files") or []
    files: dict[str, dict] = {}
    for line in _git(project, "diff", "--numstat", "-M", *rng).splitlines():
        added, removed, path = line.split("\t", 2)
        if " => " in path:  # a rename: report the new path
            path = re.sub(r"\{([^{}]*) => ([^{}]*)\}", r"\2", path).split(" => ")[-1].replace("//", "/")
        binary = added == "-"
        files[path] = {"path": path, "added": 0 if binary else int(added), "removed": 0 if binary else int(removed),
                       "binary": binary, "test": bool(TEST_PATH.search(path)),
                       "in_scope": in_scope(path, allowed) if allowed else None,
                       "symbols_added": [], "symbols_removed": []}
    current = None
    for line in _git(project, "diff", "-U0", "-M", *rng).splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
            continue
        if current not in files or line.startswith("--- ") or line[:1] not in "+-" or not line[1:].strip():
            continue
        m = SYMBOL.match(line[1:])
        if m:
            name = next(g for g in m.groups() if g)
            files[current]["symbols_added" if line[0] == "+" else "symbols_removed"].append(name)
    if not head:
        # Working tree: a builder's new files are untracked until `accept` commits them,
        # and `git diff` does not see them. Count them as wholly added.
        for path in _git(project, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
            if not path or path in files or path.startswith(("_receipts/", "_tickets/")):
                continue
            try:
                text = (Path(project) / path).read_text()
            except (UnicodeDecodeError, OSError):
                text = None
            body = text.splitlines() if text is not None else []
            syms = [next(g for g in m.groups() if g) for m in map(SYMBOL.match, body) if m]
            files[path] = {"path": path, "added": len(body), "removed": 0, "binary": text is None,
                           "test": bool(TEST_PATH.search(path)), "untracked": True,
                           "in_scope": in_scope(path, allowed) if allowed else None,
                           "symbols_added": syms, "symbols_removed": []}
    for f in files.values():
        # a symbol both removed and added was edited in place, not added or removed
        both = set(f["symbols_added"]) & set(f["symbols_removed"])
        f["symbols_changed"] = sorted(both)
        f["symbols_added"] = sorted(set(f["symbols_added"]) - both)
        f["symbols_removed"] = sorted(set(f["symbols_removed"]) - both)
    rows = sorted(files.values(), key=lambda f: (f["in_scope"] is not False, f["test"], -(f["added"] + f["removed"])))
    out_of_scope = [f["path"] for f in rows if f["in_scope"] is False]
    return {"task": task, "base": base, "head": head or "WORKTREE", "allowed_files": allowed,
            "files": rows, "out_of_scope": out_of_scope,
            "totals": {"files": len(rows), "added": sum(f["added"] for f in rows),
                       "removed": sum(f["removed"] for f in rows),
                       "test_files": sum(f["test"] for f in rows)},
            "read_first": [f["path"] for f in rows if f["in_scope"] is False or
                           (not f["test"] and (f["symbols_removed"] or f["added"] + f["removed"] > 80))]}


def render(s: dict) -> str:
    t = s["totals"]
    head = s["head"] if s["head"] == "WORKTREE" else s["head"][:12]
    lines = [f"{s['task'] or 'diff'}: {s['base'][:12]}..{head} — {t['files']} file(s), "
             f"+{t['added']}/-{t['removed']}, {t['test_files']} test file(s)"]
    if not s["allowed_files"]:
        lines.append("  scope: the ticket lists no allowed_files (not checked)")
    elif s["out_of_scope"]:
        lines.append(f"  OUT OF SCOPE: {', '.join(s['out_of_scope'])}")
    else:
        lines.append("  scope: every file is in allowed_files")
    for f in s["files"]:
        tag = " [test]" if f["test"] else " [binary]" if f["binary"] else ""
        tag += " [new, untracked]" if f.get("untracked") else ""
        tag += " [OUT]" if f["in_scope"] is False else ""
        syms = [f"+{', +'.join(f['symbols_added'])}" if f["symbols_added"] else "",
                f"~{', ~'.join(f['symbols_changed'])}" if f["symbols_changed"] else "",
                f"-{', -'.join(f['symbols_removed'])}" if f["symbols_removed"] else ""]
        syms = "  ".join(x for x in syms if x)
        lines.append(f"  {f['path']}{tag} +{f['added']}/-{f['removed']}" + (f"  {syms}" if syms else ""))
    if s["read_first"]:
        lines.append(f"  read in full: {', '.join(s['read_first'])}")
    return "\n".join(lines)
