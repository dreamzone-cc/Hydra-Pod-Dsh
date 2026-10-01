# SPDX-License-Identifier: AGPL-3.0-or-later
"""Project skills library (technical paper §5-b, phase 6).

Skills specific to one project live in `<project>/.hydra/skills/<name>/SKILL.md`
(committed with the project). Their state and record live in
`.hydra/skills/index.json`; every change is also a `hydra/skill-changed` event
in the ledger (ignorable), so the history is auditable and revertible.

Lifecycle (no step widens permissions; the manager, the strongest model,
approves every promotion):

    candidate --approve--> trial --3 successes, no failure--> active
    trial --failure--> review          active --success rate < 60% over 5--> review
    review --approve (new version)--> trial          any --retire--> retired

Matching is deterministic: a skill declares `paths` (globs) and `keywords`;
the context pack attaches the trial and active skills whose paths cover a
ticket's files or whose keywords appear in its text. Outcomes come from the
manager's decisions on the tickets the skill was attached to (APPROVE without
rework = success, REWORK = failure), recorded by `wf decide` automatically.
"""

import fnmatch
import json
import re
import time
from pathlib import Path

from . import ledger

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
STATES = ("candidate", "trial", "active", "review", "retired")
PROMOTE_AFTER = 3
WINDOW, MIN_RATE = 5, 0.6
EVENT = "hydra/skill-changed"


def root(project) -> Path:
    return Path(project) / ".hydra" / "skills"


def _index_path(project) -> Path:
    return root(project) / "index.json"


def load(project) -> dict:
    f = _index_path(project)
    return json.loads(f.read_text()) if f.exists() else {"skills": {}}


def _save(project, idx: dict) -> None:
    f = _index_path(project)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, indent=1, sort_keys=True) + "\n")
    tmp.replace(f)


def _log(project, name: str, change: str, reason: str | None, **payload) -> None:
    ledger.append(project, EVENT, workflow_id="SKILLS", actor={"kind": "manager"}, reason=reason,
                  ignorable=True, payload={"skill": name, "change": change, **payload})


def _get(idx: dict, name: str) -> dict:
    s = idx["skills"].get(name)
    if s is None:
        raise ValueError(f"no skill {name!r}")
    return s


def _csv(v) -> list[str]:
    return [x.strip() for x in (v if isinstance(v, list) else (v or "").split(",")) if x.strip()]


def add(project, name: str, description: str, body: str, paths=None, keywords=None, reason: str = "") -> dict:
    if not NAME.match(name or ""):
        raise ValueError(f"skill name {name!r}: lowercase words joined by '-'")
    if not description or len(description) > 200:
        raise ValueError("a skill needs a description of at most 200 characters (it is what matching shows)")
    paths, keywords = _csv(paths), [k.lower() for k in _csv(keywords)]
    if not paths and not keywords:
        raise ValueError("a skill needs --paths or --keywords, or it can never be matched")
    if len(body.strip()) < 40:
        raise ValueError("the skill body is too short to be useful")
    idx = load(project)
    if name in idx["skills"] and idx["skills"][name]["state"] != "retired":
        raise ValueError(f"skill {name} exists; revise it with `skill revise`")
    d = root(project) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {json.dumps(description)}\n---\n\n{body.strip()}\n")
    idx["skills"][name] = {"state": "candidate", "description": description, "paths": paths, "keywords": keywords,
                           "version": idx["skills"].get(name, {}).get("version", 0) + 1, "outcomes": [],
                           "created_at": time.time()}
    _save(project, idx)
    _log(project, name, "added", reason or None, state="candidate")
    return idx["skills"][name]


def revise(project, name: str, body: str, reason: str) -> dict:
    """A new version of a skill in review (or a candidate): back to candidate, outcomes reset."""
    idx = load(project)
    s = _get(idx, name)
    if s["state"] not in ("candidate", "review"):
        raise ValueError(f"{name} is {s['state']}: only a candidate or a skill in review can be revised")
    if len(body.strip()) < 40:
        raise ValueError("the skill body is too short to be useful")
    (root(project) / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {json.dumps(s['description'])}\n---\n\n{body.strip()}\n")
    s.update(state="candidate", version=s["version"] + 1, outcomes=[])
    _save(project, idx)
    _log(project, name, "revised", reason, version=s["version"])
    return s


def approve(project, name: str, reason: str) -> dict:
    if not reason or not reason.strip():
        raise ValueError("approving a skill needs a reason")
    idx = load(project)
    s = _get(idx, name)
    if s["state"] != "candidate":
        raise ValueError(f"{name} is {s['state']}: only a candidate can be approved")
    s["state"] = "trial"
    _save(project, idx)
    _log(project, name, "approved", reason, state="trial", version=s["version"])
    return s


def retire(project, name: str, reason: str) -> dict:
    idx = load(project)
    s = _get(idx, name)
    s["state"] = "retired"
    _save(project, idx)
    _log(project, name, "retired", reason, state="retired")
    return s


def outcome(project, name: str, success: bool, workflow_id: str, pack_seq: int | None = None) -> dict:
    """Record one use; apply the promotion and demotion rules. One outcome per context pack."""
    idx = load(project)
    s = _get(idx, name)
    if s["state"] not in ("trial", "active"):
        return s  # only skills in use are scored
    if pack_seq is not None and any(o.get("pack") == pack_seq and o["wf"] == workflow_id for o in s["outcomes"]):
        return s
    s["outcomes"].append({"wf": workflow_id, "ok": bool(success), "at": time.time(), "pack": pack_seq})
    before = s["state"]
    recent = [o["ok"] for o in s["outcomes"][-WINDOW:]]
    if s["state"] == "trial":
        if not success:
            s["state"] = "review"
        elif len(s["outcomes"]) >= PROMOTE_AFTER and all(o["ok"] for o in s["outcomes"]):
            s["state"] = "active"
    elif len(recent) >= WINDOW and sum(recent) / len(recent) < MIN_RATE:
        s["state"] = "review"
    _save(project, idx)
    _log(project, name, "outcome", None, workflow=workflow_id, ok=bool(success), state=s["state"],
         **({"from": before} if before != s["state"] else {}))
    return s


def match(project, files: list[str], text: str = "") -> list[str]:
    """Trial and active skills whose paths cover one of `files` or whose keywords appear in `text`."""
    words = set(re.findall(r"[a-z0-9_-]+", text.lower()))
    out = []
    for name, s in sorted(load(project)["skills"].items()):
        if s["state"] not in ("trial", "active"):
            continue
        by_path = any(fnmatch.fnmatchcase(f, g) or f.startswith(g.rstrip("/") + "/") for f in files for g in s["paths"])
        if by_path or any(k in words for k in s["keywords"]):
            out.append(name)
    return out


def body(project, name: str) -> str:
    text = (root(project) / name / "SKILL.md").read_text()
    return text.split("\n---\n", 1)[1].strip() if text.startswith("---\n") else text.strip()
