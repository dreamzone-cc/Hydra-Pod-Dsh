# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repository map (technical paper §3.1, phase 2), after Aider's repo map.

A ranked outline of the repository's definitions (path, line, signature) that
fits a token budget, so a worker sees the structure of the code without
reading it. Standard library only, like the rest of this package:

1. definitions and identifier references are extracted per tracked source
   file with regular expressions (no tree-sitter dependency);
2. files form a graph: A → B when A references a name defined in B (names
   defined in many files are too generic to count);
3. PageRank over that graph, personalized toward the ticket's files, ranks
   the files; a definition's score is its file's rank times how often other
   files use its name;
4. the outline is cut to the budget (≈ 4 characters per token).

Extraction is cached under ~/.cache/hydra-pod-dsh/repomap, keyed by HEAD and
the working tree's status, so only the ranking runs on a warm cache.
"""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

SOURCE_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".rb", ".php",
              ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".scala", ".sh"}
SKIP_DIRS = ("node_modules/", "vendor/", "dist/", "build/", ".venv/", "venv/", "third_party/", "_receipts/",
             "_tickets/")
MAX_BYTES = 200_000
# Indented definitions count too (methods); the capture is the defined name.
DEF = re.compile(
    r"^\s*(?:async\s+)?def\s+(\w+)|^\s*class\s+(\w+)"
    r"|^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\*?\s+(\w+)"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>"
    r"|^\s*(?:export\s+)?(?:abstract\s+)?(?:interface|type|enum)\s+(\w+)"
    r"|^\s*func\s+(?:\([^)]*\)\s*)?(\w+)"
    r"|^\s*(?:pub(?:\([^)]*\))?\s+)?(?:fn|struct|enum|trait|impl)\s+(\w+)"
    r"|^\s*(?:public|private|protected)\s+(?:static\s+)?[\w<>\[\],\s]+?\s+(\w+)\s*\(")
IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
KEYWORDS = frozenset("""self this None True False null true false return import from export const let var def class
function async await for while if else elif try except finally with yield lambda pass break continue raise new
delete typeof instanceof static public private protected interface type enum struct impl trait func package
default case switch throw catch super extends implements string number boolean void int float bool str dict list
print len range""".split())
GENERIC_DEFS = 5     # a name defined in more files than this does not link files
CHARS_PER_TOKEN = 4


def _cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "hydra-pod-dsh" / "repomap"


def _git(project, *args) -> str:
    r = subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise ValueError(f"git {args[0]} failed in {project}: {r.stderr.strip() or r.returncode}")
    return r.stdout


def source_files(project) -> list[str]:
    files = []
    for f in _git(project, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0"):
        if not f or Path(f).suffix not in SOURCE_EXT or any(f.startswith(d) or f"/{d}" in f for d in SKIP_DIRS):
            continue
        p = Path(project) / f
        try:
            if p.is_file() and p.stat().st_size <= MAX_BYTES:
                files.append(f)
        except OSError:
            continue
    return sorted(set(files))


def extract(text: str) -> tuple[list[dict], dict[str, int]]:
    """Definitions (name, line, signature) and identifier counts of one file."""
    defs, refs = [], {}
    for n, line in enumerate(text.splitlines(), 1):
        m = DEF.match(line)
        if m:
            name = next(g for g in m.groups() if g)
            sig = line.strip().rstrip("{:").strip()
            defs.append({"name": name, "line": n, "sig": sig[:120], "indent": len(line) - len(line.lstrip())})
        for word in IDENT.findall(line):
            if word not in KEYWORDS:
                refs[word] = refs.get(word, 0) + 1
    return defs, refs


def _state_key(project) -> str:
    head = _git(project, "rev-parse", "HEAD").strip() if _has_head(project) else "no-head"
    status = _git(project, "status", "--porcelain", "-z")
    return hashlib.sha256(f"{head}\0{status}".encode()).hexdigest()[:24]


def _has_head(project) -> bool:
    return subprocess.run(["git", "-C", str(project), "rev-parse", "--verify", "-q", "HEAD"],
                          capture_output=True).returncode == 0


def index(project) -> dict:
    """{file: {"defs": [...], "refs": {...}}}, from the cache when the tree has not changed."""
    project = Path(project).resolve()
    key = _state_key(project)
    cache = _cache_dir() / f"{hashlib.sha256(str(project).encode()).hexdigest()[:16]}-{key}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except ValueError:
            pass
    out = {}
    for f in source_files(project):
        try:
            text = (project / f).read_text()
        except (UnicodeDecodeError, OSError):
            continue
        defs, refs = extract(text)
        out[f] = {"defs": defs, "refs": refs}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        for old in cache.parent.glob(f"{cache.name.split('-')[0]}-*.json"):
            old.unlink(missing_ok=True)  # one cached state per project
        tmp = cache.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(out))
        tmp.replace(cache)
    except OSError:
        pass  # read-only cache (DSH's workspace-write sandbox): the index is rebuilt next time
    return out


def edges(idx: dict) -> dict[str, dict[str, float]]:
    """The reference graph: edges[A][B] > 0 when A uses a name defined in B (weighted by use)."""
    defined_in: dict[str, set] = {}
    for f, d in idx.items():
        for x in d["defs"]:
            defined_in.setdefault(x["name"], set()).add(f)
    out: dict[str, dict[str, float]] = {f: {} for f in idx}
    for f, d in idx.items():
        for name, count in d["refs"].items():
            targets = defined_in.get(name)
            if not targets or len(targets) > GENERIC_DEFS:
                continue
            for t in targets:
                if t != f:
                    out[f][t] = out[f].get(t, 0) + count ** 0.5
    return out


def rank(idx: dict, focus: list[str] | None = None, iterations: int = 30, damping: float = 0.85) -> dict[str, float]:
    """PageRank over the reference graph, personalized toward `focus` files when given."""
    files = list(idx)
    if not files:
        return {}
    edges_ = edges(idx)
    focus = [f for f in (focus or []) if f in idx]
    personal = {f: (1.0 / len(focus) if f in focus else 0.0) for f in files} if focus else \
        {f: 1.0 / len(files) for f in files}
    score = dict(personal)
    for _ in range(iterations):
        nxt = {f: (1 - damping) * personal[f] for f in files}
        sink = 0.0
        for f in files:
            out = edges_[f]
            total = sum(out.values())
            if not total:
                sink += score[f]
                continue
            for t, w in out.items():
                nxt[t] += damping * score[f] * w / total
        for f in files:
            nxt[f] += damping * sink * personal[f]
        score = nxt
    return score


def build(project, focus: list[str] | None = None, max_tokens: int = 1500) -> dict:
    """The ranked outline cut to `max_tokens`; focus files first, with all their definitions."""
    idx = index(project)
    scores = rank(idx, focus)
    used_by: dict[str, int] = {}
    for f, d in idx.items():
        for name, c in d["refs"].items():
            used_by[name] = used_by.get(name, 0) + c
    order = sorted(idx, key=lambda f: (f not in (focus or []), -scores.get(f, 0), f))
    budget = max_tokens * CHARS_PER_TOKEN
    lines, shown, size = [], [], 0
    for f in order:
        defs = idx[f]["defs"]
        if not defs:
            continue
        if f not in (focus or []):
            # outside the focus: the most used definitions, top level first
            defs = sorted(defs, key=lambda x: (x["indent"] > 0, -used_by.get(x["name"], 0)))[:12]
            defs.sort(key=lambda x: x["line"])
        block = [f"{f}:"] + [f"  {x['line']:>5} {x['sig']}" for x in defs]
        cost = sum(len(b) + 1 for b in block)
        if size + cost > budget:
            if f in (focus or []) or not shown:
                block = block[: max(2, (budget - size) // 60)]  # a focus file is never dropped
                cost = sum(len(b) + 1 for b in block)
            else:
                break
        lines += block
        shown.append(f)
        size += cost
    return {"files_indexed": len(idx), "files_shown": len(shown), "tokens": size // CHARS_PER_TOKEN,
            "focus": [f for f in (focus or []) if f in idx], "text": "\n".join(lines)}
