"""The workflow state machine (architecture §6-§9, §29, rev 2).

State is never held in memory between commands: `fold()` rebuilds every
workflow from the ledger, so a crash or restart loses nothing (§21). Commands
validate a transition against the folded state, then append one event.

Mapping to Hydra-Pod: a workflow starts from one ticket (`WF-<ticket>`); fix
tickets (T6b, ...) are later tasks of the same workflow. The Manager is Leader,
Planner and Verifier; builders and reviewers are recorded as workers.
"""

import re
import subprocess
from dataclasses import dataclass, field

from . import ledger

# Ticket ids come from the manager model and flow into file names and glob
# patterns (`<task>-*.costs.jsonl`, `_receipts/<WF>.report.md`): they must stay
# one safe path component, never a traversal or a metacharacter.
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

STATES = ("NEW", "PLANNING", "PLAN_READY", "ASSIGNING", "EXECUTING", "TESTING", "REVIEWING",
          "VERIFYING", "REWORK", "RE_REVIEW", "REPLAN", "APPROVED", "DONE",
          "FAILED", "BLOCKED", "CANCELLED", "ABORTED")
TERMINAL = frozenset({"DONE", "FAILED", "CANCELLED", "ABORTED"})
DECISIONS = ("APPROVE", "REWORK", "RE_REVIEW", "REPLAN", "ESCALATE", "ABORT")

# Ordinary forward moves. Decisions (below) and human overrides are separate.
FORWARD = {
    "NEW": {"PLANNING"},
    "PLANNING": {"PLAN_READY"},
    "PLAN_READY": {"ASSIGNING"},
    "ASSIGNING": {"EXECUTING"},
    "EXECUTING": {"TESTING", "FAILED"},
    "TESTING": {"REVIEWING"},
    "REVIEWING": {"VERIFYING"},
    "REWORK": {"EXECUTING"},
    "RE_REVIEW": {"REVIEWING"},
    "REPLAN": {"PLANNING"},
    "APPROVED": {"DONE"},
}
# Where a Verifier decision leads, and which state it may be taken in.
DECISION_TARGET = {"APPROVE": "APPROVED", "REWORK": "REWORK", "RE_REVIEW": "RE_REVIEW",
                   "REPLAN": "REPLAN", "ESCALATE": "BLOCKED", "ABORT": "ABORTED"}
DECIDE_IN = {"VERIFYING": set(DECISIONS),
             # a failed acceptance run is a rework, not a review (§6: TESTING = manager acceptance)
             "TESTING": {"REWORK", "ESCALATE", "ABORT"}}
# Which counter a decision consumes, and the policy key that bounds it (§7).
LIMITED = {"REWORK": ("rework_attempts", "max_rework_attempts"),
           "RE_REVIEW": ("review_cycles", "max_review_cycles"),
           "REPLAN": ("replans", "max_replans")}
DEFAULT_POLICY = {"max_rework_attempts": 3, "max_review_cycles": 4, "max_replans": 2}

# Ticket folder for each state (§6 rev 2), so the ledger and _tickets/ agree.
FOLDER = {**{s: "open" for s in ("NEW", "PLANNING", "PLAN_READY")},
          **{s: "doing" for s in ("ASSIGNING", "EXECUTING", "TESTING", "REVIEWING", "VERIFYING",
                                  "REWORK", "RE_REVIEW", "REPLAN", "APPROVED")},
          "DONE": "done", "BLOCKED": "blocked", "FAILED": "dropped", "CANCELLED": "dropped",
          "ABORTED": "dropped"}

EVENT_TYPES = frozenset({"hydra/workflow-created", "hydra/workflow-state", "hydra/verification-decision",
                         "hydra/task-created", "hydra/agent-started", "hydra/agent-completed",
                         "hydra/human-decision", "hydra/resource-usage", "hydra/checkpoint-created",
                         "hydra/policy-decision", "hydra/task-amended"})
STATE_EVENTS = frozenset({"hydra/workflow-state", "hydra/verification-decision", "hydra/human-decision",
                          "hydra/policy-decision"})
BUDGET_KEYS = ("max_cost_usd", "max_zai_credits", "max_runtime_minutes", "max_manager_tokens")
# Where a stalled worker state goes back to for a retry (§21, Phase 8).
RECOVERY = {"EXECUTING": "ASSIGNING", "REVIEWING": "TESTING"}
DISPATCH_STATES = frozenset({"EXECUTING", "REVIEWING"})


def git_head(project) -> str | None:
    """HEAD of the project's git repository, recorded as the checkpoint of each move (§20, §22)."""
    try:
        r = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"], capture_output=True,
                           text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


class TransitionError(Exception):
    """The requested move is not allowed from the current state."""


@dataclass
class Workflow:
    id: str
    objective: str
    state: str = "NEW"
    task_id: str | None = None
    tasks: list = field(default_factory=list)
    policy: dict = field(default_factory=lambda: dict(DEFAULT_POLICY))
    budget: dict = field(default_factory=dict)
    recoveries: int = 0
    git_head: str | None = None
    rework_attempts: int = 0
    review_cycles: int = 0
    replans: int = 0
    blocked_from: str | None = None
    last_decision: dict | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    last_seq: int = 0

    @property
    def folder(self) -> str:
        return FOLDER[self.state]

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["folder"] = self.folder
        d["terminal"] = self.state in TERMINAL
        return d


def fold(events: list[dict]) -> dict[str, Workflow]:
    """Rebuild every workflow from ledger events (pure; the only way state is derived)."""
    wfs: dict[str, Workflow] = {}
    for e in events:
        wid, t, p = e["workflow_id"], e["type"], e.get("payload") or {}
        if t == "hydra/workflow-created":
            wfs[wid] = Workflow(id=wid, objective=p.get("objective", ""), task_id=e.get("task_id"),
                                tasks=[e.get("task_id")] if e.get("task_id") else [],
                                policy={**DEFAULT_POLICY, **(p.get("policy") or {})},
                                budget=dict(p.get("budget") or {}), created_at=e["at"])
        w = wfs.get(wid)
        if w is None:
            continue  # events of a workflow whose creation is missing are not guessed at
        if p.get("git_head"):
            w.git_head = p["git_head"]
        if p.get("recovery"):
            w.recoveries += 1
        if t in STATE_EVENTS and "to" in p:
            if p["to"] == "BLOCKED" and w.state != "BLOCKED":
                w.blocked_from = w.state
            w.state = p["to"]
        if t == "hydra/verification-decision":
            w.last_decision = {"decision": p.get("decision"), "requested": p.get("requested"),
                               "reason": e.get("reason"), "limit_note": p.get("limit_note"), "seq": e["seq"]}
            counter = LIMITED.get(p.get("decision"), (None,))[0]
            if counter and p.get("to") == DECISION_TARGET[p["decision"]]:
                setattr(w, counter, getattr(w, counter) + 1)
        if t == "hydra/task-created" and e.get("task_id"):
            w.tasks.append(e["task_id"])
            w.task_id = e["task_id"]
        w.updated_at, w.last_seq = e["at"], e["seq"]
    return wfs


def load(project) -> dict[str, Workflow]:
    return fold(ledger.read(project, EVENT_TYPES))


def get(project, workflow_id: str) -> Workflow:
    return _require(load(project), project, workflow_id)


def _require(wfs: dict, project, workflow_id: str) -> Workflow:
    w = wfs.get(workflow_id)
    if w is None:
        raise TransitionError(f"no workflow {workflow_id!r} in {ledger.ledger_path(project)}")
    return w


def validate_ticket_id(ticket: str) -> None:
    if not ID_RE.match(ticket or ""):
        raise TransitionError(f"ticket id {ticket!r} must be 1-64 chars of A-Za-z0-9._- "
                              "(first char alphanumeric)")


def validate_workflow_id(wid: str) -> None:
    if not (wid or "").startswith("WF-") or not ID_RE.match(wid[3:]):
        raise TransitionError(f"workflow id {wid!r} must be WF-<ticket> with ticket chars A-Za-z0-9._-")


def workflow_id_for(ticket: str) -> str:
    return f"WF-{ticket}"


def create(project, ticket: str, objective: str, policy: dict | None = None, actor: dict | None = None,
           budget: dict | None = None) -> Workflow:
    validate_ticket_id(ticket)
    wid = workflow_id_for(ticket)
    bad = set(policy or {}) - set(DEFAULT_POLICY)
    if bad:
        raise TransitionError(f"unknown policy keys: {sorted(bad)}")
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in (policy or {}).values()):
        raise TransitionError("policy limits must be positive integers")
    bad = set(budget or {}) - set(BUDGET_KEYS)
    if bad:
        raise TransitionError(f"unknown budget keys: {sorted(bad)}")
    if any(not isinstance(v, (int, float)) or v <= 0 for v in (budget or {}).values()):
        raise TransitionError("budget limits must be positive numbers")
    with ledger.transaction(project) as txn:
        if wid in fold(txn.events(EVENT_TYPES)):
            raise TransitionError(f"workflow {wid} already exists")
        txn.append("hydra/workflow-created", workflow_id=wid, task_id=ticket, actor=actor,
                   payload={"objective": objective, "policy": policy or {}, "budget": budget or {},
                            "git_head": git_head(project)})
    return get(project, wid)


def advance(project, wid: str, to: str, *, actor: dict | None = None, reason: str | None = None,
            payload: dict | None = None) -> Workflow:
    """An ordinary forward move (e.g. EXECUTING -> TESTING)."""
    with ledger.transaction(project) as txn:
        w = _require(fold(txn.events(EVENT_TYPES)), project, wid)
        if w.state in TERMINAL:
            raise TransitionError(f"{wid} is {w.state} (terminal)")
        if to not in FORWARD.get(w.state, ()):
            allowed = sorted(FORWARD.get(w.state, ()))
            hint = " (use decide)" if w.state in DECIDE_IN else ""
            raise TransitionError(f"{wid}: {w.state} -> {to} not allowed; allowed: {allowed}{hint}")
        txn.append("hydra/workflow-state", workflow_id=wid, task_id=w.task_id, actor=actor,
                   reason=reason, payload={**(payload or {}), "from": w.state, "to": to,
                                           "git_head": git_head(project)})
    return get(project, wid)


def policy_block(project, wid: str, reason: str, detail: dict | None = None) -> Workflow:
    """The policy engine stops the workflow (budget exhausted, §17): -> BLOCKED."""
    with ledger.transaction(project) as txn:
        w = _require(fold(txn.events(EVENT_TYPES)), project, wid)
        if w.state in TERMINAL or w.state == "BLOCKED":
            return w
        txn.append("hydra/policy-decision", workflow_id=wid, task_id=w.task_id,
                   actor={"kind": "policy"}, reason=reason,
                   payload={"action": "block", "from": w.state, "to": "BLOCKED", "detail": detail or {},
                            "git_head": git_head(project)})
    return get(project, wid)


def recover(project, wid: str, reason: str, *, actor: dict | None = None) -> Workflow:
    """Retry a stalled worker step: EXECUTING -> ASSIGNING, REVIEWING -> TESTING (§21)."""
    with ledger.transaction(project) as txn:
        w = _require(fold(txn.events(EVENT_TYPES)), project, wid)
        if w.state not in RECOVERY:
            raise TransitionError(f"{wid}: nothing to recover in {w.state} (only {sorted(RECOVERY)})")
        txn.append("hydra/agent-completed", workflow_id=wid, task_id=w.task_id, actor=actor,
                   reason=reason, payload={"status": "failed", "state": w.state})
        txn.append("hydra/workflow-state", workflow_id=wid, task_id=w.task_id, actor=actor,
                   reason=reason, payload={"from": w.state, "to": RECOVERY[w.state], "recovery": True,
                                           "git_head": git_head(project)})
    return get(project, wid)


def decide(project, wid: str, decision: str, reason: str, *, actor: dict | None = None,
           task_id: str | None = None, directive: dict | None = None) -> Workflow:
    """A Verifier decision. A limited decision past its policy bound becomes ESCALATE (§6 rev 2)."""
    if decision not in DECISIONS:
        raise TransitionError(f"unknown decision {decision!r}; one of {DECISIONS}")
    if not reason or not reason.strip():
        raise TransitionError("a decision needs a reason")
    if task_id:
        validate_ticket_id(task_id)
    with ledger.transaction(project) as txn:
        w = _require(fold(txn.events(EVENT_TYPES)), project, wid)
        if decision not in DECIDE_IN.get(w.state, ()):
            raise TransitionError(f"{wid}: decision {decision} not allowed in {w.state}")
        effective, note = decision, None
        if decision in LIMITED:
            counter, key = LIMITED[decision]
            if getattr(w, counter) >= w.policy[key]:
                effective = "ESCALATE"
                note = f"{decision} refused: {key}={w.policy[key]} reached"
        if task_id and task_id not in w.tasks:
            txn.append("hydra/task-created", workflow_id=wid, task_id=task_id, actor=actor,
                       reason=f"fix ticket for {decision}")
        txn.append("hydra/verification-decision", workflow_id=wid, task_id=task_id or w.task_id,
                   actor=actor, reason=reason,
                   payload={"decision": effective, "requested": decision, "from": w.state,
                            "to": DECISION_TARGET[effective], "limit_note": note, "directive": directive or {},
                            "git_head": git_head(project)})
    return get(project, wid)


def human(project, wid: str, action: str, reason: str, *, to: str | None = None) -> Workflow:
    """User override (§43): cancel, resume (from BLOCKED), approve, reject, pause."""
    if not reason or not reason.strip():
        raise TransitionError("a human decision needs a reason")
    with ledger.transaction(project) as txn:
        w = _require(fold(txn.events(EVENT_TYPES)), project, wid)
        actor = {"kind": "user"}
        if w.state in TERMINAL:
            raise TransitionError(f"{wid} is {w.state} (terminal)")
        targets = {"cancel": "CANCELLED", "pause": "BLOCKED", "approve": "APPROVED", "reject": "REWORK"}
        if action == "resume":
            if w.state != "BLOCKED":
                raise TransitionError(f"{wid}: resume needs BLOCKED, not {w.state}")
            target = to or w.blocked_from
            if target is None or target in TERMINAL or target == "BLOCKED":
                raise TransitionError(f"{wid}: resume needs a target state (--to)")
        elif action in targets:
            target = targets[action]
        else:
            raise TransitionError(f"unknown action {action!r}")
        txn.append("hydra/human-decision", workflow_id=wid, task_id=w.task_id, actor=actor,
                   reason=reason, payload={"action": action, "from": w.state, "to": target,
                                           "git_head": git_head(project)})
    return get(project, wid)


def record(project, wid: str, etype: str, *, actor: dict | None = None, payload: dict | None = None,
           reason: str | None = None) -> dict:
    """Non-state facts: agent start/finish, resource usage, checkpoints."""
    if etype not in EVENT_TYPES or etype == "hydra/workflow-created" or etype in STATE_EVENTS:
        raise TransitionError(f"{etype} cannot be recorded directly")
    with ledger.transaction(project) as txn:
        w = _require(fold(txn.events(EVENT_TYPES)), project, wid)
        return txn.append(etype, workflow_id=wid, task_id=w.task_id, actor=actor,
                          reason=reason, payload=payload)


def amend(project, wid: str, reason: str, *, field: str, before: str | None = None, after: str | None = None,
          actor: dict | None = None) -> Workflow:
    """The manager corrects its own ticket (e.g. a wrong acceptance expectation found at TESTING).

    State does not change: the fault is the ticket's, not the worker's, so this is
    neither REWORK nor a decision. The correction is auditable with before/after.
    """
    if not reason or not reason.strip():
        raise TransitionError("an amendment needs a reason")
    with ledger.transaction(project) as txn:
        w = _require(fold(txn.events(EVENT_TYPES)), project, wid)
        if w.state in TERMINAL:
            raise TransitionError(f"{wid} is {w.state} (terminal)")
        txn.append("hydra/task-amended", workflow_id=wid, task_id=w.task_id, actor=actor, reason=reason,
                   payload={"field": field, "before": before, "after": after, "state": w.state,
                            "git_head": git_head(project)})
    return get(project, wid)


def timeline(project, wid: str | None = None, limit: int | None = None) -> list[dict]:
    if limit is not None and limit < 1:
        raise TransitionError("--limit must be at least 1")
    events = [e for e in ledger.read(project, EVENT_TYPES) if wid is None or e["workflow_id"] == wid]
    return events[-limit:] if limit else events
