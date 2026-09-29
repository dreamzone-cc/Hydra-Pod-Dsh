"""The workflow ledger: an append-only, versioned JSONL event log in the project.

It lives at `<project>/_receipts/ledger.jsonl`, with the other manager-owned
records, so it is versioned with the code and survives any process (ADR-011: the
project repository is the ledger; DSH session events are only a mirror).
`_receipts/` is where hydra-pod-dispatch's scope check expects manager records;
a ledger anywhere else under `_tickets/` makes `build`/`accept` abort (found by
the scripted W1 run). A ledger at the old path `_tickets/ledger.jsonl` is moved
once on first use.

Rules (architecture §15, §36):
- one JSON object per line; never rewritten, only appended;
- every event carries `v` (schema version). A reader refuses a version newer
  than it knows instead of reinterpreting it; an unknown `type` at a known
  version is skipped only when the event says `ignorable: true`;
- appends take an exclusive lock and assign `seq` under it, so concurrent
  writers (the manager and a dispatch step) cannot interleave or reuse a seq;
  a command that folds state, checks a rule and appends wraps the whole
  sequence in one lock with `transaction()`, so two of them cannot both
  validate against the same folded state;
- tamper evidence: every event carries `prev`, a hash of the previous line.
  `_receipts/` is exempt from the worker scope check, so an edited, removed or
  reordered line must be detectable: `read()` verifies the chain and refuses a
  broken one. Limitation: the chain is an unkeyed hash inside the same file, so
  it detects interior edits, removals and reorders, not a truncation of the
  final line(s) or a full rewrite; committing `_receipts/` to git is the
  external checkpoint that covers those.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

SCHEMA_VERSION = 1
LEDGER_REL = Path("_receipts/ledger.jsonl")
LEGACY_REL = Path("_tickets/ledger.jsonl")
GENESIS = "0" * 16
REQUIRED_KEYS = ("seq", "at", "workflow_id", "type")


class LedgerError(Exception):
    """The ledger cannot be read faithfully (newer schema, or a damaged line)."""


def ledger_path(project: str | Path) -> Path:
    new, old = Path(project) / LEDGER_REL, Path(project) / LEGACY_REL
    if not new.exists() and old.exists():
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)
    return new


def line_hash(line: str) -> str:
    return hashlib.sha256(line.rstrip("\n").encode("utf-8")).hexdigest()[:16]


def _tail(f) -> tuple[int, str]:
    """(highest seq, hash of the last complete line) of an open ledger."""
    f.seek(0)
    last, prev = 0, GENESIS
    for line in f:
        if not line.strip() or not line.endswith("\n"):
            continue  # a torn final line is not part of the chain
        try:
            last = max(last, int(json.loads(line).get("seq", 0)))
        except (ValueError, TypeError):
            continue  # a damaged line is reported by read(), not here
        prev = line_hash(line)
    return last, prev


def _write_event(f, etype: str, *, workflow_id: str, task_id: str | None = None,
                 actor: dict | None = None, reason: str | None = None, payload: dict | None = None,
                 ignorable: bool = False, now: float | None = None) -> dict:
    """Append one event to a locked, seekable ledger file and return it."""
    last, prev = _tail(f)
    seq = last + 1
    event = {
        "v": SCHEMA_VERSION,
        "seq": seq,
        "id": f"EVT-{seq:06d}-{secrets.token_hex(2)}",
        "type": etype,
        "at": time.time() if now is None else now,
        "workflow_id": workflow_id,
        "task_id": task_id,
        "actor": actor or {"kind": "manager"},
        "reason": reason,
        "payload": payload or {},
        "prev": prev,
    }
    if ignorable:
        event["ignorable"] = True
    f.seek(0, os.SEEK_END)
    f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    f.flush()
    os.fsync(f.fileno())
    return event


def append(project: str | Path, etype: str, *, workflow_id: str, task_id: str | None = None,
           actor: dict | None = None, reason: str | None = None, payload: dict | None = None,
           ignorable: bool = False, now: float | None = None) -> dict:
    """Append one event and return it (with its assigned `seq` and `id`)."""
    path = ledger_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            return _write_event(f, etype, workflow_id=workflow_id, task_id=task_id, actor=actor,
                                reason=reason, payload=payload, ignorable=ignorable, now=now)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class _Txn:
    """Read/append helpers bound to one exclusively-locked ledger file."""

    def __init__(self, f):
        self.f = f

    def events(self, known_types: frozenset[str] | None = None) -> list[dict]:
        self.f.seek(0)
        return _parse(Path(self.f.name), self.f.readlines(), known_types)

    def append(self, etype: str, **kw) -> dict:
        return _write_event(self.f, etype, **kw)


@contextlib.contextmanager
def transaction(project: str | Path):
    """Hold the ledger's exclusive lock across a fold-check-append sequence.

    `append()` alone is atomic, but a command that folds the state, validates a
    rule against it and then appends races against a concurrent command doing
    the same, unless the whole sequence runs under one lock. Inside the `with`
    block use only `txn.events(...)` / `txn.append(...)`; module-level `read`/
    `append` on the same ledger would self-deadlock on the lock.
    """
    path = ledger_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield _Txn(f)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _parse(path: Path, lines: list[str], known_types: frozenset[str] | None = None) -> list[dict]:
    events, prev = [], GENESIS
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            if n == len(lines) and not line.endswith("\n"):
                break  # torn final write from a crash: ignore the partial line
            raise LedgerError(f"{path}:{n}: not valid JSON")
        v = e.get("v")
        if not isinstance(v, int) or v > SCHEMA_VERSION:
            raise LedgerError(f"{path}:{n}: schema v{v} is newer than this reader (v{SCHEMA_VERSION})")
        missing = [k for k in REQUIRED_KEYS if k not in e]
        if missing:
            raise LedgerError(f"{path}:{n}: line missing required key(s): {', '.join(missing)}")
        if not isinstance(e["seq"], int) or isinstance(e["seq"], bool):
            raise LedgerError(f"{path}:{n}: 'seq' must be an integer")
        if not isinstance(e["at"], (int, float)) or isinstance(e["at"], bool):
            raise LedgerError(f"{path}:{n}: 'at' must be a number")
        if "prev" in e and e["prev"] != prev:
            raise LedgerError(f"{path}:{n}: hash chain broken (a line before it was edited, removed or reordered)")
        prev = line_hash(line)
        if known_types is not None and e["type"] not in known_types:
            if e.get("ignorable"):
                continue
            raise LedgerError(f"{path}:{n}: unknown event type {e['type']!r} (not ignorable)")
        events.append(e)
    events.sort(key=lambda e: e["seq"])
    return events


def read(project: str | Path, known_types: frozenset[str] | None = None) -> list[dict]:
    """All events in seq order. Raises LedgerError rather than guess."""
    path = ledger_path(project)
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            lines = f.readlines()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return _parse(path, lines, known_types)
