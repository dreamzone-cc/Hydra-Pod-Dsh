# SPDX-License-Identifier: AGPL-3.0-or-later
"""The skill smith (technical paper §5-b, phase 6): from recurring patterns to skill drafts.

`mine` is deterministic and spends nothing. It looks for what repeats across
workflows in the project's own records:
- lessons that share a file, a directory or a tag;
- REWORK decisions whose reasons are alike;
- valid reviewer findings whose problems are alike;
- reviewer suggestions the manager adopted more than once in a similar form.
Each group of two or more becomes a proposal with its evidence; a group already
covered by a skill (same paths) is left out.

`draft` hands one proposal to a cheap read-only model (the `skillsmith` pool),
which writes a skill in a fixed format. The draft enters the library as a
`candidate` only: the manager reviews and approves it like any other skill
(skills.py), and the usual outcomes then promote or demote it.
"""

import fnmatch
import hashlib
import re
from pathlib import Path

from . import diffsum, findings, ledger, lessons, router, runner, skills, workflow

STOP = frozenset("the a an and or of to in on for with is are was be it this that not no by as at from must "
                 "should when then than into after before".split())
SIMILAR = 0.35


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]{3,}", (text or "").lower()) if w not in STOP}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _cluster(items: list[dict], key: str) -> list[list[dict]]:
    """Greedy single-link clusters of items whose `key` texts are alike."""
    groups: list[list[dict]] = []
    for it in items:
        w = _words(it[key])
        for g in groups:
            if any(_jaccard(w, _words(x[key])) >= SIMILAR for x in g):
                g.append(it)
                break
        else:
            groups.append([it])
    return [g for g in groups if len({x.get("wf") for x in g}) >= 2 or len(g) >= 3]


def _covered(project, paths: list[str]) -> bool:
    for s in skills.load(project)["skills"].values():
        if s["state"] != "retired" and paths and all(
                any(fnmatch.fnmatchcase(p, g) or p.startswith(g.rstrip("/") + "/") for g in s["paths"]) for p in paths):
            return True
    return False


def _proposal(kind: str, evidence: list[dict], paths: list[str], topic: str) -> dict:
    pid = "P-" + hashlib.sha256((kind + "".join(sorted(e["text"] for e in evidence))).encode()).hexdigest()[:8]
    return {"id": pid, "kind": kind, "topic": topic, "paths": sorted(set(paths)), "evidence": evidence}


def mine(project) -> list[dict]:
    out = []
    les = [{"text": x["text"], "wf": x["workflow_id"], "paths": x["paths"], "tags": x["tags"]}
           for x in lessons.load(project)]
    seen: set[int] = set()
    for i, a in enumerate(les):
        if i in seen:
            continue
        group = [a]
        for j in range(i + 1, len(les)):
            b = les[j]
            dirs_a = {p.rsplit("/", 1)[0] for p in a["paths"]}
            if set(a["tags"]) & set(b["tags"]) or set(a["paths"]) & set(b["paths"]) or \
                    dirs_a & {p.rsplit("/", 1)[0] for p in b["paths"]}:
                group.append(b)
                seen.add(j)
        if len(group) >= 2:
            paths = [p for g in group for p in g["paths"]]
            tags = sorted({t for g in group for t in g["tags"]})
            out.append(_proposal("lessons", group, paths, ", ".join(tags) or ", ".join(sorted(set(paths)))[:80]))
    events = ledger.read(project)
    rework = [{"text": e.get("reason") or "", "wf": e["workflow_id"], "paths": []} for e in events
              if e["type"] == "hydra/verification-decision" and (e.get("payload") or {}).get("decision") == "REWORK"]
    for g in _cluster(rework, "text"):
        out.append(_proposal("rework", g, [], " ".join(sorted(_words(g[0]["text"])))[:80]))
    wfs = workflow.load(project)
    found, adopted = [], []
    for w in wfs.values():
        for r in findings.summary(project, w.tasks)["reports"]:
            for f in r["findings"]:
                if f["status"] in ("valid", "partial"):
                    found.append({"text": f["problem"], "wf": w.id, "paths": [f["location"].split(":")[0]]})
            for i in r["items"]:
                if i["kind"] == "suggestion" and i["status"] in ("adopt-now", "backlog"):
                    adopted.append({"text": i["text"], "wf": w.id, "paths": []})
    for kind, items in (("findings", found), ("suggestions", adopted)):
        for g in _cluster(items, "text"):
            out.append(_proposal(kind, g, [p for x in g for p in x["paths"]],
                                 " ".join(sorted(_words(g[0]["text"])))[:80]))
    return [p for p in out if not _covered(project, p["paths"])]


NAME = re.compile(r"^NAME:\s*([a-z0-9][a-z0-9-]{0,47})\s*$", re.M)
DESC = re.compile(r"^DESCRIPTION:\s*(.+)$", re.M)
PATHS = re.compile(r"^PATHS:\s*(.*)$", re.M)
KEYWORDS = re.compile(r"^KEYWORDS:\s*(.*)$", re.M)
BODY = re.compile(r"^BODY:\s*\n(.+)", re.M | re.S)


def draft(project, proposal_id: str, agent: str | None = None, wid: str | None = None,
          reg: dict | None = None) -> dict:
    props = {p["id"]: p for p in mine(project)}
    p = props.get(proposal_id)
    if p is None:
        raise ValueError(f"no open proposal {proposal_id!r} (hydra-pod-dsh skill mine)")
    reg = reg or router.load()
    pool = reg.get("pools", {}).get("skillsmith")
    name = agent or (router._members(pool)[0]["agent"] if pool else None)
    a = reg["agents"].get(name or "")
    why = "no skillsmith pool and no --agent" if a is None else router._passes(name, a, "skillsmith", reg)
    if why:
        raise ValueError(f"skill smith {name}: {why}")
    evidence = "\n".join(f"- ({e.get('wf', '-')}) {e['text']}" for e in p["evidence"])
    prompt = (runner.template("skillsmith.md").replace("<kind>", p["kind"]).replace("<evidence>", evidence)
              .replace("<paths>", ", ".join(p["paths"]) or "none given"))
    report = Path(project) / "_receipts" / "skillsmith" / f"{proposal_id}.{name}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    stem = "SKILLS"
    if wid:
        w = workflow.get(project, wid)
        stem = (diffsum.ticket_path(project, w.task_id) or Path(w.task_id)).stem
    runner.run(project, name, a, prompt, report, stem, "draft")
    text = report.read_text()
    m_name, m_desc, m_body = NAME.search(text), DESC.search(text), BODY.search(text)
    if not (m_name and m_desc and m_body):
        raise ValueError(f"the draft is not in the expected format; see {report.relative_to(project)}")
    paths = (PATHS.search(text).group(1) if PATHS.search(text) else "") or ",".join(p["paths"])
    keywords = KEYWORDS.search(text).group(1) if KEYWORDS.search(text) else ""
    return skills.add(project, m_name.group(1), m_desc.group(1).strip()[:200], m_body.group(1).strip(),
                      paths, keywords, f"drafted by {name} from {proposal_id} ({p['kind']}, {len(p['evidence'])} cases)")
