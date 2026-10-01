# SPDX-License-Identifier: AGPL-3.0-or-later
"""Context shards and their allocation to models (technical paper §5-a, phase 6).

No model can hold a large project at once, and none needs to. The project is
split into shards (modules, by directory and by the reference graph of the
repository map), each small enough for one model's window. For a ticket, the
allocator gives the shards it changes to the executor, and the shards it only
needs to read either to the executor too (when everything fits its window) or
to read-only "keepers" that answer questions about them (blackboard.py). The
pod then works on more code than any one window holds, while the manager sees
only the plan, the facts and the summaries.

- `partition` is deterministic: the same tree gives the same shard ids.
- A shard's fingerprint is the hash of its files' git blob ids, working tree
  included, so a fact about a shard can tell when its files changed.
- `allocate` records `hydra/context-allocation`; a shard being changed by one
  running workflow is locked against a second one.

Windows come from the registry (`context_window`, `effective_window` per agent).
An agent without them gets ASSUMED_WINDOW, and the allocation says so: fill the
real figures from the provider's documentation rather than trusting the default.
"""

import hashlib
import subprocess
from pathlib import Path

from . import diffsum, repomap, router, workflow

DOC_EXT = {".md", ".rst", ".txt", ".adoc"}
SKIP = ("_receipts/", "_tickets/", ".hydra/")
CHARS_PER_TOKEN = repomap.CHARS_PER_TOKEN
MAX_SHARD_TOKENS = 40_000
ASSUMED_WINDOW = 128_000
EFFECTIVE_SHARE = 0.5          # past about half the window, recall degrades: plan on half
ACTIVE = ("ASSIGNING", "EXECUTING", "TESTING", "REVIEWING", "VERIFYING", "REWORK", "RE_REVIEW")
EVENT = "hydra/context-allocation"


def _files(project) -> list[str]:
    out = set(repomap.source_files(project))
    listed = subprocess.run(["git", "-C", str(project), "ls-files", "-z", "--cached", "--others",
                             "--exclude-standard"], capture_output=True, text=True).stdout.split("\0")
    out |= {f for f in listed if f and Path(f).suffix in DOC_EXT and not f.startswith(SKIP)
            and (Path(project) / f).is_file()}
    return sorted(out)


def _tokens(project, f: str) -> int:
    try:
        return (Path(project) / f).stat().st_size // CHARS_PER_TOKEN
    except OSError:
        return 0


def _key(f: str, depth: int) -> str:
    parts = f.split("/")[:-1]
    if not parts:
        return "root"
    if parts[0] in ("tests", "test", "__tests__", "spec", "docs", "doc"):
        depth = min(depth, 1)
    return "/".join(parts[:depth])


def _slug(key: str) -> str:
    return "S-" + "".join(c if c.isalnum() else "-" for c in key).strip("-").lower()


def partition(project, max_tokens: int = MAX_SHARD_TOKENS) -> list[dict]:
    """Shards: [{id, key, files, tokens}], each at most max_tokens unless one file is larger."""
    files = _files(project)
    size = {f: _tokens(project, f) for f in files}
    groups: dict[str, list[str]] = {}
    for f in files:
        groups.setdefault(_key(f, 2), []).append(f)
    # split what is too large, one directory level deeper, then by chunks of files
    final: dict[str, list[str]] = {}
    pending = list(groups.items())
    while pending:
        key, fs = pending.pop()
        if sum(size[f] for f in fs) <= max_tokens or len(fs) == 1:
            final[key] = fs
            continue
        depth = key.count("/") + 2 if key != "root" else 1
        sub: dict[str, list[str]] = {}
        for f in fs:
            sub.setdefault(_key(f, depth) if key != "root" else key, []).append(f)
        if len(sub) > 1:
            pending += list(sub.items())
            continue
        chunk, n, total = [], 1, 0
        for f in sorted(fs):
            if chunk and total + size[f] > max_tokens:
                final[f"{key}#{n}"] = chunk
                chunk, total, n = [], 0, n + 1
            chunk.append(f)
            total += size[f]
        final[f"{key}#{n}" if n > 1 else key] = chunk
    # merge small shards into the neighbour they reference most, when the result still fits
    graph = repomap.edges(repomap.index(project))
    small = max_tokens // 8
    changed = True
    while changed:
        changed = False
        for key in sorted(final, key=lambda k: sum(size[f] for f in final[k])):
            fs = final[key]
            if sum(size[f] for f in fs) >= small or len(final) == 1:
                continue
            weight: dict[str, float] = {}
            for other, ofs in final.items():
                if other == key:
                    continue
                w = sum(graph.get(a, {}).get(b, 0) + graph.get(b, {}).get(a, 0) for a in fs for b in ofs)
                if w and sum(size[f] for f in fs + ofs) <= max_tokens:
                    weight[other] = w
            if weight:
                target = max(sorted(weight), key=weight.get)
                merged = sorted(final[target] + fs)
                # the merged shard keeps the name of its larger part (production code, not its tests)
                keep = key if sum(size[f] for f in fs) > sum(size[f] for f in final[target]) or \
                    (target.split("/")[0] in ("tests", "test", "spec", "docs", "doc") and key != "root") else target
                del final[key], final[target]
                final[keep] = merged
                changed = True
                break
    return sorted(({"id": _slug(k), "key": k, "files": sorted(v), "tokens": sum(size[f] for f in v)}
                   for k, v in final.items()), key=lambda s: s["id"])


def blob_ids(project, files: list[str]) -> dict[str, str]:
    """git blob id of each file as it is in the working tree ("" for a missing file)."""
    present = [f for f in files if (Path(project) / f).is_file()]
    ids = subprocess.run(["git", "-C", str(project), "hash-object", "--stdin-paths"], input="\n".join(present),
                         capture_output=True, text=True).stdout.split() if present else []
    out = dict(zip(present, ids))
    return {f: out.get(f, "") for f in files}


def fingerprint(project, files: list[str]) -> str:
    ids = blob_ids(project, sorted(files))
    return hashlib.sha256("".join(f"{f}\0{ids[f]}\n" for f in sorted(files)).encode()).hexdigest()[:16]


def shard_of(shards: list[dict], path: str) -> dict | None:
    for s in shards:
        if path in s["files"]:
            return s
    # a file the ticket will create: the shard whose files share its directory
    d = path.rsplit("/", 1)[0] if "/" in path else ""
    for s in shards:
        if any(f.rsplit("/", 1)[0] == d if "/" in f else d == "" for f in s["files"]):
            return s
    return None


def shards_for(shards: list[dict], paths: list[str]) -> list[dict]:
    found = {}
    for p in paths:
        if p.endswith("/"):
            for s in shards:
                if any(f.startswith(p) for f in s["files"]):
                    found[s["id"]] = s
        else:
            s = shard_of(shards, p)
            if s:
                found[s["id"]] = s
    return [found[k] for k in sorted(found)]


def window(agent: dict) -> tuple[int, bool]:
    """(effective window in tokens, whether it is assumed rather than registered)."""
    if agent.get("effective_window"):
        return int(agent["effective_window"]), False
    if agent.get("context_window"):
        return int(agent["context_window"] * EFFECTIVE_SHARE), False
    return int(ASSUMED_WINDOW * EFFECTIVE_SHARE), True


def latest_allocations(project) -> dict[str, dict]:
    """The latest allocation of each workflow, by workflow id."""
    out = {}
    for e in workflow.timeline(project):
        if e["type"] == EVENT:
            out[e["workflow_id"]] = e["payload"]
    return out


def allocate(project, wid: str, task: str | None = None, reg: dict | None = None, quota=None,
             available=None, record: bool = True, max_shard_tokens: int = MAX_SHARD_TOKENS) -> dict:
    reg = reg or router.load()
    w = workflow.get(project, wid)
    task = task or w.task_id
    header = diffsum.ticket_header(project, task)
    if not header:
        raise ValueError(f"no ticket {task} under _tickets/")
    allowed = header.get("allowed_files") or []
    hints = [h.strip() for h in (header.get("read_hints") or "").split(",") if h.strip()]
    complexity = (header.get("complexity") or "").upper() or None
    shards = partition(project, max_shard_tokens)
    touch = shards_for(shards, allowed)
    reads = [s for s in shards_for(shards, hints) if s["id"] not in {t["id"] for t in touch}]
    executor = router.choose("executor", complexity, reg, quota, available)
    ex_window, ex_assumed = window(executor)
    touch_tokens = sum(s["tokens"] for s in touch)
    out = {"task": task, "executor": executor["agent"], "executor_window": ex_window,
           "touch": [s["id"] for s in touch], "reads": [], "keepers": {}, "assumed_windows": [],
           "shard_tokens": {s["id"]: s["tokens"] for s in touch + reads}, "status": "ok", "notes": [],
           "max_shard_tokens": max_shard_tokens}
    if ex_assumed:
        out["assumed_windows"].append(executor["agent"])
    if touch_tokens > ex_window:
        out["status"] = "split-needed"
        out["notes"].append(f"the shards this ticket changes hold {touch_tokens} tokens, more than "
                            f"{executor['agent']}'s effective window ({ex_window}): split the ticket")
    if touch_tokens + sum(s["tokens"] for s in reads) <= ex_window:
        out["reads"] = [s["id"] for s in reads]   # everything fits: no keeper, no questions
    else:
        for s in reads:
            keeper = _keeper_for(s, reg, quota, available)
            if keeper is None:
                out["status"] = "no-keeper" if out["status"] == "ok" else out["status"]
                out["notes"].append(f"no read-only keeper has a window for {s['id']} ({s['tokens']} tokens)")
                continue
            out["keepers"][s["id"]] = keeper["agent"]
            if window(keeper)[1] and keeper["agent"] not in out["assumed_windows"]:
                out["assumed_windows"].append(keeper["agent"])
    # one running workflow per shard it changes
    wfs = workflow.load(project)
    for other, alloc in latest_allocations(project).items():
        if other == wid or other not in wfs or wfs[other].state not in ACTIVE:
            continue
        shared = sorted(set(alloc.get("touch", [])) & set(out["touch"]))
        if shared:
            out["status"] = "locked"
            out["notes"].append(f"{other} ({wfs[other].state}) is changing {', '.join(shared)}")
    out["capacity_tokens"] = ex_window + sum(window(reg["agents"][k])[0] for k in set(out["keepers"].values()))
    out["fingerprints"] = {s["id"]: fingerprint(project, s["files"]) for s in touch + reads}
    if record:
        workflow.record(project, wid, EVENT, ignorable=True, payload=out)
    return out


def _keeper_for(shard: dict, reg: dict, quota, available) -> dict | None:
    from . import adapters
    pool = reg.get("pools", {}).get("keeper")
    if not pool:
        return None
    for m in router._members(pool):
        name = m["agent"]
        a = reg["agents"].get(name)
        if a is None or router._passes(name, a, "keeper", reg):
            continue
        ad = adapters.for_runtime(a["runtime"])
        if ad is None or not (available or ad.available)(a)[0] or (quota or ad.quota)(a).get("exhausted"):
            continue
        if window(a)[0] >= shard["tokens"]:
            return {"agent": name, **a}
    return None
