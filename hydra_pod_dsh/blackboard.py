# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared blackboard (technical paper §5-a, phase 6): facts the pod has established.

Models that hold different shards exchange short facts here instead of loading
each other's files. A fact is a question, its answer, and the file:line
references it rests on. It is stored with the git blob id of every file it
cites (or the shard's fingerprint when it cites none), so a fact whose files
changed is reported stale and never fed to a model again.

`ask` answers a question about a shard: from a still-valid fact for the same
question when there is one (no model call at all), else from the shard's
keeper, a read-only model given only that shard's files. Each workflow has a
question cap, and every model answer goes to the cost log like any other run,
so budgets cover it. The manager can also write facts it verified (`add`).
"""

import json
import re
import time
from pathlib import Path

from . import diffsum, repomap, router, runner, shards, workflow

MAX_QUESTIONS = 6
ANSWER = re.compile(r"^ANSWER:\s*(.+?)\s*(?=^REFS:|^CONFIDENCE:|\Z)", re.S | re.M)
REFS = re.compile(r"^REFS:\s*(.*)$", re.M)
CONF = re.compile(r"^CONFIDENCE:\s*(low|medium|high)", re.M | re.I)
REF = re.compile(r"([\w./-]+\.\w+):(\d+)")


def path(project) -> Path:
    return Path(project) / "_receipts" / "blackboard.jsonl"


def qkey(question: str) -> str:
    return " ".join(re.findall(r"[a-z0-9_]+", question.lower()))


def load(project) -> list[dict]:
    f = path(project)
    if not f.exists():
        return []
    out = []
    for line in f.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _append(project, fact: dict) -> dict:
    f = path(project)
    f.parent.mkdir(parents=True, exist_ok=True)
    fact = {"id": f"F-{len(load(project)) + 1}", "at": time.time(), **fact}
    with open(f, "a") as fh:
        fh.write(json.dumps(fact, ensure_ascii=False) + "\n")
    return fact


def parse_refs(project, text: str) -> tuple[list[dict], list[str]]:
    """References that point at a real line of a real file, and the ones that do not."""
    good, bad = [], []
    for p, line in REF.findall(text or ""):
        f = Path(project) / p
        try:
            n = len(f.read_text(errors="replace").splitlines())
        except OSError:
            n = 0
        (good if 1 <= int(line) <= n else bad).append({"path": p, "line": int(line)} if 1 <= int(line) <= n
                                                      else f"{p}:{line}")
    return good, bad


def evidence(project, refs: list[dict], shard: dict | None) -> dict:
    if refs:
        return {"ref_blobs": shards.blob_ids(project, sorted({r["path"] for r in refs}))}
    return {"shard_fingerprint": shards.fingerprint(project, shard["files"]) if shard else None}


def valid(project, fact: dict, shard_index: dict | None = None) -> bool:
    if fact.get("ref_blobs"):
        return shards.blob_ids(project, list(fact["ref_blobs"])) == fact["ref_blobs"]
    if fact.get("shard_fingerprint"):
        s = (shard_index or {}).get(fact.get("shard"))
        return bool(s) and shards.fingerprint(project, s["files"]) == fact["shard_fingerprint"]
    return False


def facts(project, shard_ids: list[str] | None = None, only_valid: bool = True,
          max_shard_tokens: int = shards.MAX_SHARD_TOKENS) -> list[dict]:
    """Facts about these shards: filed under one of them, or citing one of their files."""
    index = {s["id"]: s for s in shards.partition(project, max_shard_tokens)}
    files = {f for sid in shard_ids or [] for f in index.get(sid, {}).get("files", [])}
    out = []
    for f in load(project):
        if shard_ids is not None and f.get("shard") not in shard_ids and \
                not any(r["path"] in files for r in f.get("refs") or []):
            continue
        f = {**f, "valid": valid(project, f, index)}
        if f["valid"] or not only_valid:
            out.append(f)
    return out


def add(project, wid: str, shard_id: str, question: str, answer: str, refs_text: str = "") -> dict:
    """A fact the manager established itself."""
    workflow.get(project, wid)
    index = {s["id"]: s for s in shards.partition(project)}
    if shard_id not in index:
        raise ValueError(f"no shard {shard_id!r} (hydra-pod-dsh shards list)")
    if len(answer.strip()) < 5:
        raise ValueError("a fact needs an answer")
    refs, bad = parse_refs(project, refs_text)
    if bad:
        raise ValueError(f"references that do not point at a real line: {', '.join(bad)}")
    return _append(project, {"workflow_id": wid, "shard": shard_id, "question": question.strip(),
                             "qkey": qkey(question), "answer": answer.strip(), "refs": refs, "by": "manager",
                             "source": "manager", "confidence": "high", **evidence(project, refs, index[shard_id])})


def guess_shard(project, question: str, shard_list: list[dict]) -> dict | None:
    """The shard defining the most identifiers the question names."""
    idx = repomap.index(project)
    words = {w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", question)} - repomap.KEYWORDS
    q = question.lower()
    score: dict[str, int] = {}
    for s in shard_list:
        score[s["id"]] = sum(1 for f in s["files"] for d in idx.get(f, {}).get("defs", [])
                             if d["name"].lower() in words)
        score[s["id"]] += sum(1 for f in s["files"] if Path(f).name.lower() in q or f.lower() in q)
    best = max(score, key=score.get, default=None)
    return next(s for s in shard_list if s["id"] == best) if best and score[best] else None


def ask(project, wid: str, question: str, shard_id: str | None = None, agent: str | None = None,
        max_questions: int = MAX_QUESTIONS, reg: dict | None = None,
        max_shard_tokens: int | None = None) -> dict:
    w = workflow.get(project, wid)
    if len(question.strip()) < 10:
        raise ValueError("ask a full question")
    # the workflow's allocation fixed the shard size: the same ids everywhere for this ticket
    max_shard_tokens = max_shard_tokens or shards.latest_allocations(project).get(wid, {}).get(
        "max_shard_tokens", shards.MAX_SHARD_TOKENS)
    shard_list = shards.partition(project, max_shard_tokens)
    index = {s["id"]: s for s in shard_list}
    key = qkey(question)
    for f in reversed(load(project)):   # an answer still valid for the same question costs nothing
        if (shard_id is None or f.get("shard") == shard_id) and f.get("qkey") == key and valid(project, f, index):
            return {**f, "cached": True}
    shard = index.get(shard_id) if shard_id else guess_shard(project, question, shard_list)
    if shard is None:
        raise ValueError(f"no shard {shard_id!r}" if shard_id else
                         "could not tell which shard the question is about: pass --shard (hydra-pod-dsh shards list)")
    asked = [e for e in workflow.timeline(project, wid) if e["type"] == "hydra/blackboard-ask"]
    if len(asked) >= max_questions:
        raise ValueError(f"{wid} has used its {max_questions} questions: read the files, or ask the manager")
    reg = reg or router.load()
    allocated = shards.latest_allocations(project).get(wid, {}).get("keepers", {}).get(shard["id"])
    name = agent or allocated or (shards._keeper_for(shard, reg, None, None) or {}).get("agent")
    if not name:
        raise ValueError(f"no read-only keeper is available for {shard['id']}")
    a = reg["agents"].get(name)
    why = "not in the registry" if a is None else router._passes(name, a, "keeper", reg)
    if why:
        raise ValueError(f"keeper {name}: {why}")
    prompt = runner.keeper_prompt(shard, question)
    n = len(load(project)) + 1
    report = Path(project) / "_receipts" / "blackboard" / f"F-{n}.{name}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    stem = (diffsum.ticket_path(project, w.task_id) or Path(w.task_id)).stem
    run = runner.run(project, name, a, prompt, report, stem, "ask")
    text = report.read_text()
    m = ANSWER.search(text)
    answer = (m.group(1) if m else text).strip()[:1200]
    rm, cm = REFS.search(text), CONF.search(text)
    refs, bad = parse_refs(project, rm.group(1) if rm else "")
    conf = cm.group(1).lower() if cm else "low"
    unknown = answer.lower().startswith("unknown")
    workflow.record(project, wid, "hydra/blackboard-ask", ignorable=True,
                    payload={"shard": shard["id"], "agent": name, "question": question.strip()[:300],
                             "unknown": unknown, "bad_refs": bad})
    if unknown:
        return {"answer": answer, "refs": [], "cached": False, "stored": False, "agent": name, "shard": shard["id"]}
    fact = _append(project, {"workflow_id": wid, "shard": shard["id"], "question": question.strip(), "qkey": key,
                             "answer": answer, "refs": refs, "bad_refs": bad, "by": name, "source": "keeper",
                             "confidence": conf if refs else "low", "report": run["report"],
                             **evidence(project, refs, shard)})
    return {**fact, "cached": False, "stored": True}
