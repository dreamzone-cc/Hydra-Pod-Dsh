# SPDX-License-Identifier: AGPL-3.0-or-later
"""Approved plans and their dependency graph (technical paper §4.1 step 3, phase 4).

A task the manager splits into several tickets becomes a plan: the tickets and
which ones each depends on. The manager approves it once, before any build,
with `plan approve`; that writes a `hydra/plan-approved` event under the
pseudo-workflow `PLAN-<name>` (ignorable, so older readers skip it).

From then on:
- `waves` orders the tickets into waves; tickets in one wave may run at the
  same time because they depend on nothing outside earlier waves and their
  `allowed_files` do not overlap (overlapping tickets are pushed to a later
  wave, never run together);
- `ready` says which tickets can start now, from their workflows' states;
- `blocked_by` is the gate `wf advance … EXECUTING` applies: a ticket of an
  approved plan cannot be built before every ticket it depends on is DONE.

A later approval of the same name replaces the earlier plan (a REPLAN).
"""

import re

from . import diffsum, ledger, workflow

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EVENT = "hydra/plan-approved"


def _overlap(a: list[str], b: list[str]) -> bool:
    return any(diffsum.in_scope(x, b) or diffsum.in_scope(y, a) for x in a for y in b)


def parse_spec(specs: list[str]) -> dict[str, list[str]]:
    """["T1", "T2:T1", "T3:T1,T2"] -> {ticket: [dependencies]}."""
    deps: dict[str, list[str]] = {}
    for spec in specs:
        ticket, _, after = spec.partition(":")
        workflow.validate_ticket_id(ticket)
        if ticket in deps:
            raise ValueError(f"ticket {ticket} listed twice")
        deps[ticket] = [d for d in after.split(",") if d] if after else []
        for d in deps[ticket]:
            workflow.validate_ticket_id(d)
    for t, ds in deps.items():
        unknown = [d for d in ds if d not in deps]
        if unknown:
            raise ValueError(f"{t} depends on {unknown}, which are not in the plan")
    return deps


def waves(deps: dict[str, list[str]], files: dict[str, list[str]]) -> list[list[str]]:
    """Topological waves; within a wave, no two tickets share files. Raises on a cycle."""
    done: set[str] = set()
    left = dict(deps)
    out = []
    while left:
        ready = sorted(t for t, ds in left.items() if set(ds) <= done)
        if not ready:
            raise ValueError(f"dependency cycle among {sorted(left)}")
        wave: list[str] = []
        for t in ready:
            if all(not _overlap(files.get(t, []), files.get(u, [])) for u in wave):
                wave.append(t)
        out.append(wave)
        done |= set(wave)
        for t in wave:
            del left[t]
    return out


def approve(project, name: str, specs: list[str], reason: str, actor: dict | None = None) -> dict:
    if not NAME.match(name or ""):
        raise ValueError(f"bad plan name {name!r}")
    if not reason or not reason.strip():
        raise ValueError("approving a plan needs a reason")
    deps = parse_spec(specs)
    files = {}
    for t in deps:
        header = diffsum.ticket_header(project, t)
        if not header:
            raise ValueError(f"no ticket {t} under _tickets/: write the tickets before approving the plan")
        files[t] = header.get("allowed_files") or []
    order = waves(deps, files)  # refuses a cycle before anything is written
    event = ledger.append(project, EVENT, workflow_id=f"PLAN-{name}", actor=actor or {"kind": "manager"},
                          reason=reason, ignorable=True,
                          payload={"name": name, "deps": deps, "files": files, "waves": order})
    return {"name": name, "deps": deps, "files": files, "waves": order, "seq": event["seq"]}


def plans(project) -> dict[str, dict]:
    """The latest approval of every plan, by name."""
    out = {}
    for e in ledger.read(project):
        if e["type"] == EVENT:
            out[e["payload"]["name"]] = {**e["payload"], "approved_at": e["at"], "reason": e.get("reason")}
    return out


def _state(wfs: dict, ticket: str) -> str:
    w = wfs.get(workflow.workflow_id_for(ticket))
    return w.state if w else "NOT_STARTED"


def status(project, name: str) -> dict:
    p = plans(project).get(name)
    if p is None:
        raise ValueError(f"no approved plan {name!r}")
    wfs = workflow.load(project)
    states = {t: _state(wfs, t) for t in p["deps"]}
    ready = [t for t in p["deps"] if states[t] in ("NOT_STARTED", "NEW", "PLANNING", "PLAN_READY", "ASSIGNING")
             and all(states[d] == "DONE" for d in p["deps"][t])]
    # what may run together now: ready tickets whose files overlap no running ticket nor each other
    running = [t for t, s in states.items() if s in ("EXECUTING", "TESTING", "REVIEWING", "VERIFYING", "REWORK",
                                                     "RE_REVIEW")]
    batch: list[str] = []
    for t in ready:
        if all(not _overlap(p["files"].get(t, []), p["files"].get(u, [])) for u in running + batch):
            batch.append(t)
    return {**p, "states": states, "ready": ready, "start_now": batch, "running": running,
            "done": all(s == "DONE" for s in states.values())}


def blocked_by(project, ticket: str) -> list[str]:
    """Dependencies of `ticket` in any approved plan that are not DONE yet."""
    wfs = None
    out = []
    for p in plans(project).values():
        for d in p["deps"].get(ticket, []):
            wfs = wfs if wfs is not None else workflow.load(project)
            if _state(wfs, d) != "DONE":
                out.append(d)
    return sorted(set(out))
