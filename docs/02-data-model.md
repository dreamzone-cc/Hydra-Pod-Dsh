# 2. Data model and DSH reuse decisions (Phase 0)

The Phase 0 deliverable of `HYDRA-POD-DSH-ARCHITECTURE.md` §31. It freezes the entities, the persisted event schema v1, the JSON contract, and one reuse or reject decision for each overlapping DSH package.

## Entities

| Entity | Identity | Where it lives |
|---|---|---|
| Workflow | `WF-<first ticket>` (e.g. `WF-T6`) | folded from `_receipts/ledger.jsonl` |
| Task | ticket id (`T6`, fix ticket `T6b`) | `_tickets/<folder>/<ticket>.md` (Hydra-Pod) |
| Attempt | `rework_attempts` counter of the workflow | ledger |
| Review cycle | `review_cycles` counter | ledger |
| Finding | line of `_receipts/<T>.review.md` (Hydra-Pod format), with Manager verdicts appended | receipts |
| Directive | `payload.directive` of a `RE_REVIEW` decision | ledger |
| Verification | `hydra/verification-decision` event | ledger |
| Resource record | `hydra/resource-usage` event, mirrored from `_receipts/<T>.costs.jsonl` | ledger (source: cost log) |
| Agent / Role / Model / Billing route | `actor` of an event; `role`, `model`, `billing` of a resource record | ledger |
| Live stage | `~/.cache/hydra-pod-dsh/stage.json` + worker processes | cache (not durable, by design) |

## Event schema v1

One JSON object per line in `<project>/_receipts/ledger.jsonl`:

```json
{"v": 1, "seq": 17, "id": "EVT-000017-a3f9", "type": "hydra/verification-decision", "at": 1790619060.3,
 "workflow_id": "WF-T6", "task_id": "T6", "actor": {"kind": "manager", "name": "Claude Opus 5.5 (DeepSeek Harness)"},
 "reason": "RF-1 is real: off-by-one at src/x.py:42",
 "payload": {"decision": "REWORK", "requested": "REWORK", "from": "VERIFYING", "to": "REWORK",
             "limit_note": null, "directive": {}}}
```

| Type | Payload | Changes state |
|---|---|---|
| `hydra/workflow-created` | `objective`, `policy` | → `NEW` |
| `hydra/workflow-state` | `from`, `to` | yes (forward moves only) |
| `hydra/verification-decision` | `decision`, `requested`, `from`, `to`, `limit_note`, `directive` | yes |
| `hydra/human-decision` | `action`, `from`, `to` | yes |
| `hydra/task-created` | none (the event's `task_id` is the new task) | current task |
| `hydra/task-amended` | `field`, `before`, `after`, `state` (added after scripted W1) | no |
| `hydra/policy-decision` | `action: block`, `from`, `to: BLOCKED`, `detail` (budget dimensions) | yes |
| `hydra/resource-usage` | `run_key`, `task_id`, `phase`, `role`, `provider`, `model`, `billing`, `tokens` (normalized: `input`, `output`, `reasoning`, `cache_read`, `cache_write`), `run_at` (when the run ended), `seconds`, `tool_calls`, `list_cost_usd`, `zai_credits`, `exit` | no |
| `hydra/agent-started`, `hydra/agent-completed`, `hydra/checkpoint-created` | free | no (reserved) |

Rules:

- **Append-only.** `seq` is assigned under an exclusive `flock`, so concurrent writers never share or reuse one. A command that folds state, checks a rule and appends (`create`, `advance`, `decide`, …) runs the whole sequence inside one `ledger.transaction()`, so two concurrent commands cannot both validate against the same folded state.
- **Required keys.** Every line must carry `seq` (integer), `at` (number), `workflow_id` and `type`; a structurally valid JSON line without them is refused with `LedgerError`, not a KeyError.
- **Versioned.** A reader refuses `v` newer than it knows (`LedgerError`); it never reinterprets.
- **Unknown types.** An unknown type is skipped only when it carries `ignorable: true`; otherwise it is refused.
- **Crash safety.** A torn final line (a crash mid-write) is ignored. Damage anywhere else is refused.
- **Replay.** State is always `fold(read(ledger))`. Nothing is cached between commands, so a restart loses nothing (§21).

## State machine

Implemented in `hydra_pod_dsh/workflow.py`, and tested in `tests/test_workflow.py`, including a mutation check that the limit test fails when the limit is disabled.

- **Forward moves:** `NEW → PLANNING → PLAN_READY → ASSIGNING → EXECUTING → TESTING → REVIEWING → VERIFYING`, `REWORK → EXECUTING`, `RE_REVIEW → REVIEWING`, `REPLAN → PLANNING`, `APPROVED → DONE`, `EXECUTING → FAILED`.
- **Decisions:** in `VERIFYING`, any of `APPROVE`, `REWORK`, `RE_REVIEW`, `REPLAN`, `ESCALATE`, `ABORT`. In `TESTING` (the manager's acceptance run), only `REWORK`, `ESCALATE` or `ABORT`.
- **Limits:** `max_rework_attempts` (3), `max_review_cycles` (4) and `max_replans` (2). A decision past its limit is recorded as `ESCALATE` → `BLOCKED`, with `requested` and `limit_note` kept.
- **Human actions:** `cancel` (→ `CANCELLED`), `pause` (→ `BLOCKED`), `resume` (→ the state before `BLOCKED`, or `--to`), `approve` (→ `APPROVED`), `reject` (→ `REWORK`).
- **Terminal states:** `DONE`, `FAILED`, `CANCELLED`, `ABORTED`. Every move out of them is refused.
- **Checkpoints:** every state event carries `git_head`, the project's HEAD at that move (§20, §22).
- **Recovery:** a stalled worker step (no process, no receipt or report after `GRACE_SECONDS`, see `health.py`) is retried with `wf recover`: `EXECUTING` → `ASSIGNING` or `REVIEWING` → `TESTING`, recorded as `hydra/agent-completed {status: failed}` plus a `recovery: true` state event.
- **Stages** (`stages.py`, `wf stages`): each entry into a state opens a stage. Worker runs are placed by `run_at` (fallback: the last EXECUTING/REVIEWING for the run's phase), and manager messages by their time in the DSH log. Each worker's spend is shown as a share of the current 5 h / weekly window of its subscription (Z.ai: official credits; OpenCode Go: estimated USD). **Work tokens = input + output + reasoning; cache is never added to them** (`tokens.py`).
- **Consistency** (`consistency.py`, `wf check`): the ledger state must match the ticket folder (`missing-ticket`, `folder-mismatch`, `earlier-task-open`, `untracked-ticket`). It reports only; it never moves files or appends events.
- **Budgets** (`budget.py`): `max_cost_usd`, `max_zai_credits`, `max_runtime_minutes`, `max_manager_tokens`. They are checked on every advance into `EXECUTING` or `REVIEWING`: 80% warns, 100% appends a `hydra/policy-decision` → `BLOCKED` and exits with status 3. A metric that was never reported makes the level `unknown`, not `ok`.
- **Ticket folders:** each state maps to a ticket folder (`open`, `doing`, `done`, `blocked`, `dropped`); `wf show` prints it.

## JSON contract (Python ↔ JS)

`hydra-pod-dsh status --json` and `hydra-pod-dsh wf show|timeline|resources --json` print `schema_version: 1`. `status` carries `project`, `workflows` (non-terminal), `timeline` (last 8 non-resource events), `ledger_error`, `active`, `workers`, `manager_stage`, `usage`, plus `agents` (the registry with each agent's activity) and `now` (the reading's epoch time). The plugin (`plugin/lib/client.js`) reads only these fields.

## DSH reuse decisions

Each decision is taken against dsh `0.2.0-rc.1` @ `4878cda` and is reviewed when that pin changes.

| DSH package | Decision | Reason (evidence) |
|---|---|---|
| `ctx.workflowEngine` (`dsh-workflow`, `workflow-ptc`, `tool-workflow`) | **Reject for now** | It runs model-written scripts that start DSH subagents. Hydra-Pod's workers are external CLIs on their own subscriptions (ADR-010), which the engine cannot start. It is also disabled in the stock web profile |
| `agent-team` (experimental) | **Reject for now** | Experimental. Its roster and mailbox coordinate DSH sessions, not external workers. Revisit for W8 (concurrency, shared checkout) |
| `goal` | **Defer** | Could hold the workflow objective inside the manager's session. Not needed while the ledger holds it |
| `plan-mode`, `todo` | **Use as-is (no integration)** | Soft guidance in the manager's own session, and harmless next to the ledger |
| `ctx.commands` | **Adopted** | `/hydra-pod-status`, `/hydra-pod-timeline [WF]` and `/hydra-pod-control WF action reason` run without a model message. A control command is written to the ledger as a `user` decision (§43) |
| `token-meter`, session telemetry | **Replaced by reading the session log** | The manager's usage comes from `assistant/message` `usage` in `$DSH_HOME/sessions/**/session.v*.jsonl(.zstd)` (`manager_usage.py`). It is read-only and works while DSH is stopped. The session's `source.provider` gives the billing route checked by the Router |
| Right-sidebar tab (`sidebar.right.pane.tab`) | **Reject for now** | It needs a typed tab-type registration (`ctx.sidebarRightTabs`) and a TypeScript build, which would end the plugin's no-build, no-peer-dependency property. The five §26 views are served by the composer popover instead |
| Session events mirror (`hydra/*` in the DSH log) | **Reject for now** | Events from an out-of-repo plugin survive restore only with the envelope field `ignorable: true`. `session.append()` exposes no option to set it, and DSH's own note (`2026-08-30-retain-ignorable-external-session-events.md`) marks the field as removable once a replacement exists. Mirroring could make a user's sessions unrestorable after a DSH update. The ledger plus the plugin view meet the audit requirement without that risk |
| `ctx.subagents` (in-process / SDK) | **Defer to Phase 6** | The only backends where the Router can choose the model. They bill the DSH session's provider route |
| ACP subagents with opencode | **Rejected (measured)** | See `01-design.md` |
