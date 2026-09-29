# Hydra-Pod + DeepSeek Harness

## Technical Architecture & Implementation Plan

**Document:** `HYDRA-POD-DSH-ARCHITECTURE.md`\
**Status:** Proposed / Implementation Blueprint, **revision 2: reviewed against the source**\
**Target:** Hydra-Pod integration with DeepSeek Harness (`dsh`)\
**Date:** 2026-09-28 (rev 2: 2026-09-28)

------------------------------------------------------------------------

## 0. Review (revision 2)

Revision 1 was written from DSH's public documentation and a general picture of Hydra-Pod. Revision 2 checks every technical claim against the two repositories as they are:

- `deepseek-ai/deepseek-harness` @ `4878cda` (dsh `0.2.0-rc.1`), built locally;
- `dreamzone-cc/Hydra-Pod` @ `2137d10` (v1.0.0), 73 tests.

It also checks the plan against what has already been built and measured in `Hydra-Pod-Dsh`. The overall direction survives: Hydra-Pod is the control plane, DSH is the execution plane, and they are joined through an adapter. The gaps are listed in 0.3 and closed in the sections they affect; each fix is marked **[rev 2]**.

### 0.1 Verified baseline

| Claim in rev 1 | Verdict | Evidence |
|---|---|---|
| `ctx.agents`, `ctx.sessions`, `ctx.tools`, `ctx.subagents`, `ctx.llm`, `ctx.jobs`, `ctx.fs`, `ctx.sandbox`, `ctx.commands` exist | **Confirmed** | declared and used across `packages/` (e.g. `ctx.tools` 1568 uses, `ctx.commands` 146) |
| Subagent backends: in-process, ACP, Codex, Claude Code, DSH SDK; one-shot and continuable | **Confirmed** | `packages/subagent/*`, `docs/subsystems/subagent.md` |
| The Router can pick the model of any child agent | **False** | only in-process and DSH-SDK children accept `agentOptions`; ACP, Codex and Claude Code **reject** it before starting (`subagent.md`) |
| Sessions are append-only logs that plugins can extend | **Confirmed, with a condition** | new event types via `SessionEventMap` declaration merging; a restored log keeps unknown events only when they are marked `ignorable: true` (`persistence.md`) |
| "DSH Provider" runs DeepSeek | **Incomplete** | the Python SDK (`deepseek-harness-sdk`) runs an isolated runtime with its own `dsh_home`, never reads `~/.dsh`, and needs `DEEPSEEK_API_KEY`: pay-per-use DeepSeek API, **not** the OpenCode Go subscription |
| Hydra-Pod needs a new task graph, agent registry, workflow engine | **Partly duplicated** | DSH already ships `ctx.workflowEngine` (script-driven orchestration of subagents), an experimental `agent-team` (roster, task board, mailbox), `goal`, `plan`, `todo` and `token-meter`. Rev 1's own rule in §47 ("Does DSH already provide this?") was not applied to them |
| OpenCode as a provider via ACP | **Rejected by measurement** | `opencode acp` (2.0.16) starts on an unrelated OpenRouter model, ignores `OPENCODE_CONFIG(_CONTENT)`, offers no `opencode-go/*` model and no read-only `reviewer` mode, and hangs with `--standalone` (see `docs/01-design.md`) |
| ZCode as a provider | **No ACP** | ZCode exposes only its own `app-server` protocol, so it stays behind `hydra-pod-connect review zcode-lite` |
| Role permissions such as `shell: tests_only` via the DSH sandbox | **False** | DSH sandbox modes govern file effects only, per session: `read-only`, `workspace-write`, `danger-full-access`. Network and per-command rules are outside its vocabulary |

### 0.2 What already exists (not in rev 1)

The plan must start from the running system, not from zero:

| Concern | Hydra-Pod v1.0.0 (Python, stdlib only) | Hydra-Pod-Dsh (this repository) |
|---|---|---|
| Roles | Manager (Opus 5.5) = Leader + Planner + Verifier; Builder (`opencode-go/deepseek-v4.1-flash`); Reviewer (`zai-coding-plan/glm-5.3`, read-only agent), optional ZCode reviewer | same roles; the Manager runs inside dsh |
| Workflow state | ticket files in `_tickets/{open,doing,done,blocked,dropped}` with a structured front matter (R2); `_tickets/STATE.md` (R4) | live stage in `~/.cache/hydra-pod-dsh/stage.json` |
| Steps | `hydra-pod-dispatch preflight/claim/build/accept/review/probes/close/status/costs` (R3, R5) | same, called from dsh's bash tool as background jobs |
| Findings | reviewer report with severity \| file:line \| evidence \| fix, `PROBE:` lines, Manager verdicts appended | same |
| Cost | `_receipts/<T>.costs.jsonl` per run (R6) | subscription windows: Z.ai official API; OpenCode Go **estimated** from opencode's local database |
| UI | none | `dsh-hydra-pod` plugin: composer pill with the current worker and its usage |
| Validation | T1, T2/T2b, T3/T3b end to end (`docs/06-validation.md`) | install, discovery, status and plugin tests (30); **no end-to-end ticket yet** |

### 0.2a Implementation status (rev 2)

| Item (§44 rev 2) | Status | Where |
|---|---|---|
| MVP-0: manager in DSH, dispatch workers, live stage + usage | **done** | `skills/`, `hydra_pod_dsh/{live,usage}.py`, `plugin/` |
| Phase 0: data model, event schema v1, reuse decisions | **done** | `docs/02-data-model.md` |
| MVP-2: JSON contract (`schema_version`), ledger as source of truth | **done**; session mirror **rejected for now** (no public way to mark plugin events `ignorable`) | `hydra_pod_dsh/ledger.py` |
| MVP-3: state machine, rework / re-review / replan, limits → ESCALATE, human override, replay recovery | **done** (unit and CLI tests) | `hydra_pod_dsh/workflow.py` |
| Resource attribution per role / model / billing route | **done**: workers from the cost log, the manager from the DSH session logs (`manager_usage.py`) | `hydra_pod_dsh/resources.py` |
| Router, agent registry, policy (§11, §12, §19, §37a) | **done**: billing routes, capability flags, read-only and role-separation checks; the manager's real route is checked from its log | `agents.json`, `router.py` |
| Budgets (§17) | **done**: 80% warns, 100% → `hydra/policy-decision` → `BLOCKED` | `budget.py` |
| Findings (§5.4) | **done**: parsed from the existing review format, with the manager's verdicts | `findings.py` |
| Recovery (§21, Phase 8) | **done**: stalled / waiting_for_user / ready / running, and `wf recover` | `health.py`, `workflow.recover` |
| Checkpoints (§20, §22) | **done**: `git_head` on every state event | `workflow.py` |
| Human commands (§25, §43) | **done**: `/hydra-pod-status`, `/hydra-pod-timeline`, `/hydra-pod-control` via `ctx.commands` | `plugin/lib/index.js` |
| UI (§26) | **done** in the composer popover: activity, workflow, agents, resources, findings, timeline. Sidebar tab rejected (needs a TS build) | `plugin/lib/client.js` |
| Consistency ledger ↔ `_tickets/` (§6) | **done**: `wf check` (exit 4 on drift) | `consistency.py` |
| Closing report (§27, §46) | **done**: `wf report --write` | `report.py` |
| Tamper evidence | **done**: hash chain (`prev`) over the ledger; the ledger lives in `_receipts/` because hydra-pod-dispatch's scope check exempts only manager records there | `ledger.py` |
| Manager ticket amendments | **done**: `hydra/task-amended` + `wf amend` (a wrong acceptance expectation is not a worker defect) | `workflow.amend` |
| Scripted W1 (real workers, manager steps scripted) | **run 2026-09-28**, see `docs/04-validation-scripted-w1.md` | |
| Phase 0.5: W1 real ticket with Opus in DSH | **pending: needs the operator** (a trial run on 2026-09-28 showed the manager on `pi-anthropic`, i.e. OAuth, which is now detected as a policy violation) | `docs/03-validation-w1.md` |
| Dynamic DSH children (Phase 6) | registry entry and routing only (`specialist-dsh`). The child itself is started by the manager with DSH's own `subagent` tool; nothing is re-implemented | `agents.json` |

### 0.3 Gap register

| # | Severity | Gap in rev 1 | Closure (section) |
|---|---|---|---|
| G1 | **Critical** | Billing and terms are not modelled. `provider: dsh, model: deepseek` silently switches the Builder from the OpenCode Go subscription to the pay-per-use DeepSeek API. `verifier: claude-code/opus` contradicts the decision that Opus runs inside dsh on an **Anthropic API key**. Subscriptions may only be used through their supported clients | New attribute **billing route** in §2.3, §11, §18; new §37a |
| G2 | **Critical** | The Router is assumed to choose any model for any child agent; false for ACP, Codex and Claude Code children | Capability flags in §12; routing constraint in §19 |
| G3 | **High** | Workers run outside DSH (opencode, ZCode), so the DSH session log never sees them. Rev 1 makes the DSH session the single source of truth without saying how external work gets into it, or where a workflow that spans sessions lives | §15: one source of truth per fact; ledger rules |
| G4 | **High** | Persisted `hydra/*` session events would make sessions unreadable if the plugin were removed, unless they are marked `ignorable: true` | §15, §36 |
| G5 | **High** | The plan re-implements DSH's workflow engine, agent-team and goal packages without evaluating them | §14 (reuse table), §31 Phase 0 decision gate |
| G6 | **High** | Existing Hydra-Pod mechanisms (tickets, dispatch, receipts, PROBE, cost log, preflight, watchdog) are not mapped; migration starts from zero | §0.2, §34 Stage 0 |
| G7 | **High** | The per-role permission matrix cannot be enforced by the DSH sandbox (file modes only, per session); external workers are confined by their own tool | §37 |
| G8 | Medium | State machine inconsistent: `DONE` vs `COMPLETED`, `APPROVED` state missing from the type, `ABORTED` in §6 but not in §29, no transition for `ESCALATE`/`ABORT`, no behaviour when `max_rework_attempts` is exceeded, `TESTING` not mapped to acceptance | §6, §29 |
| G9 | Medium | Language boundary undefined: Hydra-Pod core is Python (stdlib), DSH plugins are JS/TS. Rev 1 mixes Python and TypeScript types with no bridge | §13.3 (JSON CLI contract, as `hydra-pod-dsh status --json` already does) |
| G10 | Medium | Usage accounting assumes tokens and tool calls per agent are available. Subagent results do not carry the child's usage to the parent; external CLIs report cost differently; OpenCode Go has no usage API | §16, §18 |
| G11 | Medium | Concurrency (§41, T8) ignores that parallel executors share one git working tree | §41 |
| G12 | Medium | Repository structure (§30) puts the plugin inside Hydra-Pod; the decided layout is a separate `Hydra-Pod-Dsh` repository | §30 |
| G13 | Medium | Test names T1–T10 collide with Hydra-Pod's ticket ids and its recorded validations T1–T3 | §32 renamed W1–W10 |
| G14 | Medium | Phases have no exit criteria; the MVP (12 items) is larger than what a first release can validate | §31, §44 |
| G15 | Low | DSH is pre-stable (`0.2.0-rc.1`); third-party plugins meet a version gate on every DSH update; no pinning policy | §36 |
| G16 | Low | Human commands `/hydra …` vs the existing `/hydra-pod` skill; DSH `ctx.commands` runs commands without a model message, which fits `status`/`pause` but not judgment steps | §25 |
| G17 | Low | Sandbox escalations and account logins keep the human on the critical path; not reflected in the timeline or budgets | §21, §43 |

------------------------------------------------------------------------

## 1. Executive Summary

This document defines the target architecture and implementation plan
for integrating **Hydra-Pod** with **DeepSeek Harness (DSH)**.

The central architectural decision is:

> **Hydra-Pod remains the orchestration and resource-management layer;
> DeepSeek Harness becomes a first-class agent runtime/provider.**

Hydra-Pod already contains the core orchestration concept: role-based
execution, provider routing, review/fix/verification cycles, dispatch,
prompts, skills, validation, and resource-aware provider selection.
DeepSeek Harness already provides a highly extensible agent runtime
based on Cordis, with plugin-based composition, sessions, agents, tools,
subagents, skills, MCP, workflows, jobs, sandboxing, and LLM adapters.

The integration must therefore **reuse DSH capabilities instead of
rebuilding them**.

### Target relationship

``` text
                         USER
                           |
                           v
                 +----------------------+
                 |      HYDRA-POD       |
                 | Orchestration Layer  |
                 +----------+-----------+
                            |
             +--------------+---------------+
             |              |               |
          Planning        Routing        Resources
             |              |               |
             +--------------+---------------+
                            |
                     Agent Role Model
                            |
        +-------------------+-------------------+
        |                   |                   |
      Leader             Executor            Reviewer
        |                   |                   |
        +-------------------+-------------------+
                            |
                         Verifier
                            |
                  +---------+---------+
                  |                   |
               APPROVE             REWORK
                                      |
                                      v
                                   EXECUTOR
                                      |
                                      v
                                   REVIEWER
                                      |
                                      v
                                   VERIFIER
                            |
                            v
                 +----------------------+
                 |   DEEPSEEK HARNESS   |
                 |     Agent Runtime    |
                 +----------+-----------+
                            |
       +--------------------+--------------------+
       |                    |                    |
     Agents             Subagents             Tools
       |                    |                    |
    Sessions              Skills                MCP
       |                    |                    |
       +--------------------+--------------------+
```

------------------------------------------------------------------------

# 2. Architectural Principles

## 2.1 Hydra-Pod is the control plane

Hydra-Pod owns:

-   workflow orchestration
-   role assignment
-   agent selection
-   provider/model routing
-   planning policy
-   review policy
-   verification policy
-   rework/re-review decisions
-   resource budgets
-   quotas
-   retry policy
-   checkpoints
-   workflow audit
-   cross-agent coordination

## 2.2 DeepSeek Harness is the execution plane

DSH owns:

-   agent runtime
-   agent loop
-   sessions
-   session event persistence
-   tool registry
-   LLM adapters
-   subagent runtime
-   skills
-   MCP
-   sandbox
-   jobs
-   runtime lifecycle
-   UI/runtime integration

The DSH architecture explicitly treats the product as a Cordis plugin
tree: model adapters, tool registry, session log, and agent loop are all
replaceable/configurable components. New behavior should normally attach
through documented extension points rather than patching the agent loop
directly.

## 2.3 Roles are not providers

The architecture must strictly separate:

``` text
Role
  -> Agent
      -> Provider/Runtime
          -> Model
```

Example:

``` yaml
role: reviewer
agent: reviewer-01
provider: dsh
model: deepseek
```

The same role can later use another runtime:

``` yaml
role: reviewer
agent: reviewer-02
provider: claude-code
model: opus
```

This separation is mandatory for long-term extensibility.

**[rev 2] A fifth attribute is mandatory: the billing route.** The same model can be paid for in different ways, and the choice is a policy decision, not a technical detail (G1):

``` text
Role -> Agent -> Runtime -> Model -> Billing route
```

``` yaml
role: executor
agent: builder-01
runtime: opencode                       # official client of the subscription
model: opencode-go/deepseek-v4.1-flash
billing: subscription/opencode-go       # NOT api/deepseek: same model family, different bill
```

A Router may change the runtime or the model only within the billing routes the policy allows for that role (§18, §37a).

## 2.4 Avoid unnecessary duplication

Do not rebuild inside Hydra-Pod:

-   session persistence
-   agent loop
-   generic subagent runtime
-   tool execution pipeline
-   MCP infrastructure
-   skill loading
-   generic LLM streaming
-   DSH agent lifecycle

Hydra-Pod should consume these capabilities through stable adapters and
documented DSH extension points.

------------------------------------------------------------------------

# 3. Why DeepSeek Harness is the Runtime Target

DeepSeek Harness provides the technical primitives required by
Hydra-Pod:

### Cordis plugin architecture

DSH uses plugins that contribute:

-   services
-   typed events
-   reversible effects
-   configuration
-   providers

There is no privileged monolithic core that Hydra-Pod should fork.

### Agent registry

DSH exposes agent creation and lifecycle through `ctx.agents`.

### Session event sourcing

Sessions are append-only event logs and are the source of truth for
model-visible context.

### Tool registry

`ctx.tools` provides a scoped and guarded tool execution pipeline.

### Subagent registry

`ctx.subagents` supports multiple provider implementations under one
interface, including in-process, ACP, Codex, Claude Code, and DSH SDK
backends.

### Extension events

DSH provides durable session events and live agent/tool/capability
events that can be consumed by Hydra-Pod.

These capabilities make DSH a strong execution substrate while Hydra-Pod
remains responsible for higher-level orchestration.

------------------------------------------------------------------------

# 4. Target System Architecture

``` text
+--------------------------------------------------------------------+
|                           HYDRA-POD                                |
|                                                                    |
|  +-----------+   +-------------+   +----------------------------+ |
|  | Leader    |   | Planner     |   | Workflow / Task Graph      | |
|  +-----------+   +-------------+   +----------------------------+ |
|                                                                    |
|  +-----------+   +-------------+   +----------------------------+ |
|  | Router    |   | Review Dir. |   | Verification Engine        | |
|  +-----------+   +-------------+   +----------------------------+ |
|                                                                    |
|  +-----------+   +-------------+   +----------------------------+ |
|  | Resource  |   | Policy      |   | Checkpoint / Audit         | |
|  | Manager   |   | Engine      |   |                            | |
|  +-----------+   +-------------+   +----------------------------+ |
|                                                                    |
|                     Provider Abstraction                          |
+------------------------------+-------------------------------------+
                               |
              +----------------+----------------+
              |                |                |
              v                v                v
        DSH Provider     OpenCode Provider   Claude Provider
              |                |                |
              v                v                v
      DeepSeek Harness      OpenCode         Claude Code
              |
              v
+--------------------------------------------------------------------+
|                         DSH Runtime                                |
|                                                                    |
| Agent Registry | Sessions | Tools | Subagents | Skills | MCP       |
| LLM Adapters   | Jobs     | Sandbox | Events | UI                 |
+--------------------------------------------------------------------+
```

------------------------------------------------------------------------

# 5. Hydra-Pod Core Responsibilities

## 5.1 Leader

The Leader is the top-level workflow authority.

Responsibilities:

-   interpret the user objective
-   select planning strategy
-   resolve workflow conflicts
-   authorize re-planning
-   approve exceptional resource use
-   decide escalation paths
-   maintain the overall objective

The Leader should not duplicate the DSH agent loop.

## 5.2 Planner

The Planner converts the objective into a structured execution graph.

Responsibilities:

-   decompose the objective
-   create tasks
-   define dependencies
-   assign roles
-   identify required capabilities
-   estimate complexity
-   propose resource budgets
-   request specialist agents when necessary

Output must be structured rather than free-form prose.

Example:

``` yaml
workflow:
  objective: "Implement authentication"

  tasks:
    - id: T1
      role: executor
      capability: rust
      priority: high

    - id: T2
      role: security
      capability: security
      depends_on: [T1]

    - id: T3
      role: tester
      capability: testing
      depends_on: [T1]

    - id: T4
      role: reviewer
      depends_on: [T2, T3]
```

## 5.3 Executor

The Executor performs implementation work.

Responsibilities:

-   modify code
-   run tools
-   execute tests
-   produce implementation artifacts
-   report changes
-   preserve task context

Execution must be performed through the selected runtime/provider.

## 5.4 Reviewer

The Reviewer performs technical review.

Responsibilities:

-   inspect implementation
-   inspect diff
-   inspect tests
-   identify defects
-   classify findings
-   recommend corrections
-   identify missing validation

Review output must be structured.

Example:

``` yaml
finding:
  id: RF-021
  severity: high
  category: security
  file: src/auth/session.rs
  line: 184
  description: "..."
  recommendation: "..."
  required_action: rework
```

## 5.5 Verifier

The Verifier is the Review Director and Quality Gate.

It must not merely return PASS/FAIL.

It evaluates:

-   original requirements
-   implementation
-   review findings
-   tests
-   previous attempts
-   review history
-   resource policy

Possible decisions:

``` text
APPROVE
REWORK
RE_REVIEW
REPLAN
ESCALATE
ABORT
```

Example directive:

``` yaml
review_directive:
  action: re_review

  scope:
    - src/auth/

  focus:
    - ownership
    - concurrency
    - error handling

  required_checks:
    - static_analysis
    - integration_tests
    - concurrency_tests
```

------------------------------------------------------------------------

# 6. Workflow State Machine

The canonical workflow is:

``` text
NEW
 |
 v
PLANNING
 |
 v
PLAN_READY
 |
 v
ASSIGNING
 |
 v
EXECUTING
 |
 v
TESTING
 |
 v
REVIEWING
 |
 v
VERIFYING
 |
 +-------------------+-------------------+-------------------+
 |                   |                   |                   |
 v                   v                   v                   v
APPROVED           REWORK             RE_REVIEW           REPLAN
 |                   |                   |                   |
 v                   v                   v                   v
DONE             EXECUTING           REVIEWING          PLANNING
```

Additional terminal states:

``` text
CANCELLED
ABORTED
FAILED
BLOCKED
```

**[rev 2] Consistency rules (G8):**

- The one success state is `DONE`, reached through `APPROVED`. `COMPLETED` is not used.
- `ESCALATE` moves the workflow to `BLOCKED` with a question for the human. `ABORT` moves it to `ABORTED`. Both are recorded with their reason.
- When `max_rework_attempts`, `max_review_cycles` or `max_replans` (§7) would be exceeded, the transition is replaced by `ESCALATE`. It is never silently retried.
- `TESTING` is the Manager's acceptance step (`hydra-pod-dispatch accept`): the acceptance commands are re-run by the manager, not taken from the worker's receipt.
- Mapping to the ticket folders that exist today: `NEW/PLANNING/PLAN_READY` → `_tickets/open/`; `ASSIGNING` … `VERIFYING` → `_tickets/doing/`; `DONE` → `_tickets/done/`; `BLOCKED` → `_tickets/blocked/`; `CANCELLED/ABORTED` → `_tickets/dropped/`.

------------------------------------------------------------------------

# 7. Rework Cycle

The core quality loop is:

``` text
EXECUTOR
   |
   v
REVIEWER
   |
   v
VERIFIER
   |
   +---- APPROVE ----> DONE
   |
   +---- REWORK -----> EXECUTOR
   |
   +---- RE_REVIEW --> REVIEWER
   |
   +---- REPLAN -----> PLANNER
```

This cycle must support a maximum retry/rework limit.

Example:

``` yaml
workflow_policy:
  max_rework_attempts: 3
  max_review_cycles: 4
  max_replans: 2
```

------------------------------------------------------------------------

# 8. Re-Review Protocol

When the Verifier requests re-review, it must provide an explicit
directive.

``` yaml
re_review:
  reason: "Reviewer did not validate concurrency behavior"

  scope:
    - src/network/
    - src/session/

  required_method:
    - static_analysis
    - stress_test
    - integration_test

  findings_to_recheck:
    - RF-014
    - RF-017
```

The Reviewer must then report:

``` yaml
review_result:
  finding_id: RF-014
  status: resolved
  evidence:
    - test: concurrency_suite
    - commit: abc123
```

------------------------------------------------------------------------

# 9. Re-Planning Protocol

Re-planning is required when the current plan is invalid or incomplete.

Triggers include:

-   architecture mismatch
-   dependency discovery
-   blocked implementation
-   conflicting requirements
-   repeated failed fixes
-   resource exhaustion
-   missing capability
-   major security finding
-   incompatible test results

Flow:

``` text
Verifier
   |
   v
REPLAN
   |
   v
Planner
   |
   v
New Task Graph
   |
   v
Assignment
```

The original plan must remain immutable in the audit history.

------------------------------------------------------------------------

# 10. Dynamic Agent System

Hydra-Pod must support dynamic specialist spawning.

Example:

``` yaml
spawn_request:
  role: security_reviewer
  capability: security
  priority: high
  lifetime: task
  reason: "Authentication changes detected"
```

The Router selects a provider/runtime.

Possible providers:

``` text
DSH
OpenCode
Claude Code
Codex
ACP
Other compatible runtimes
```

The DSH implementation should use the native `ctx.subagents` seam rather
than creating a parallel subagent framework.

DSH supports multiple subagent providers under one named registry and
supports both one-shot and continuable children.

------------------------------------------------------------------------

# 11. Agent Registry

Hydra-Pod maintains the logical registry:

``` yaml
agents:
  manager:                  # [rev 2] Leader + Planner + Verifier, as in Hydra-Pod today
    runtime: dsh            # the session the user works in
    model: anthropic/claude-opus-5.5
    billing: api/anthropic  # decided 2026-09-28; Claude Pro stays in official clients
    capabilities: [planning, decomposition, verification, architecture]

  executor:
    runtime: opencode       # via hydra-pod-dispatch build
    model: opencode-go/deepseek-v4.1-flash
    billing: subscription/opencode-go
    capabilities: [coding, terminal, tests]

  reviewer:
    runtime: opencode       # project agent `reviewer`: edit denied, bash allow-listed
    model: zai-coding-plan/glm-5.3
    billing: subscription/zai-lite
    capabilities: [code-review]

  reviewer-alt:
    runtime: zcode          # hydra-pod-connect review zcode-lite; Read/Glob/Grep only
    model: glm-5.3
    billing: subscription/zai-lite   # same credit pool as `reviewer`
    capabilities: [code-review]
```

**[rev 2]** Rev 1 listed `provider: dsh, model: deepseek` for three roles. That would move them onto the pay-per-use DeepSeek API and give up the reviewer's enforced read-only mode. Such an entry is allowed only as an explicit, opt-in billing route (§18).

DSH remains responsible for runtime-level child creation and lifecycle.

------------------------------------------------------------------------

# 12. Provider Abstraction

Hydra-Pod should define a provider contract.

Conceptual interface:

``` python
class AgentProvider:
    def capabilities(self):
        ...

    def spawn_agent(self, request):
        ...

    def send(self, agent_id, message):
        ...

    def stream(self, agent_id):
        ...

    def interrupt(self, agent_id):
        ...

    def resume(self, agent_id):
        ...

    def status(self, agent_id):
        ...

    def dispose(self, agent_id):
        ...
```

Provider implementations:

``` text
DSHProvider
OpenCodeProvider
ClaudeCodeProvider
CodexProvider
ZCodeProvider
```

The contract must be asynchronous and event-aware.

**[rev 2] Not every runtime supports every operation (G2).** Each provider declares capability flags, and the Router may only request what is declared:

| Runtime | model override | stream | interrupt | resume | read-only enforcement | usage reported |
|---|---|---|---|---|---|---|
| DSH in-process child (`ctx.subagents`) | yes (`agentOptions`) | yes | yes | yes (continuable) | DSH sandbox mode | DSH session / token meter |
| DSH SDK child (Python/TS) | yes | final result | abort | **no** (only in-process children implement `prepareContinuable`) | DSH sandbox mode | child session log (separate `dsh_home`) |
| ACP child | **no** | no (final text only) | abort | no | `permission: reject/allow` only | **no** |
| Claude Code child | **no** per request (one fixed `model` per provider instance) | no | abort | no | `permissionMode` | **no** (stays in the child) |
| opencode CLI (`opencode run -m`) | yes (flag) | JSON events | kill | `--session` | project `reviewer` agent | cost per message (JSON events, local DB) |
| ZCode CLI | no | no | kill | `--resume` | tool set (Read/Glob/Grep) | Z.ai credits (official quota API) |

`send`/`stream`/`resume` in the interface above are optional capabilities, not guarantees.

------------------------------------------------------------------------

# 13. DSH Integration Strategy

## 13.1 Native plugin

Implement a Hydra-Pod DSH plugin for capabilities that belong inside the
DSH runtime:

-   Hydra commands
-   Hydra tools
-   Hydra session events
-   Hydra status projection
-   agent lifecycle observation
-   UI integration
-   runtime context injection

## 13.2 SDK adapter

Use the DSH SDK when Hydra-Pod needs to control a separate DSH runtime
process.

Rule:

``` text
Same DSH runtime
    -> Native plugin

Separate DSH runtime
    -> DSH SDK
```

This keeps the integration flexible.

## 13.3 [rev 2] Language boundary (G9)

Hydra-Pod core is Python (standard library only). DSH plugins are JavaScript/TypeScript running in the DSH host and browser. Neither imports the other. The boundary is a **versioned JSON contract over a CLI**, which is already in use:

``` text
DSH plugin (JS)  --execFile-->  hydra-pod-dsh status --json   (Python)
                 <--stdout----  {"now", "active", "workers", "manager_stage", "usage"}
```

Each command that crosses the boundary gets a `schema_version` field and a contract test on both sides. HTTP is not introduced until a need for push updates is shown.

------------------------------------------------------------------------

# 14. DSH Extension Points to Use

Based on the current DSH architecture, Hydra-Pod should primarily use:

``` text
ctx.agents
ctx.sessions
ctx.tools
ctx.subagents
ctx.llm
ctx.jobs
ctx.fs
ctx.sandbox
ctx.commands
agent/*
session/event
tools/*
```

Avoid direct modifications to `agent-loop` unless an explicit DSH
limitation requires it.

**[rev 2] Existing DSH packages that overlap the planned Hydra components (G5).** Each one gets a documented reuse or reject decision in Phase 0 before any Hydra equivalent is written:

| Planned in Hydra | Existing in DSH | Note |
|---|---|---|
| Workflow / task graph engine | `ctx.workflowEngine` (`dsh-workflow`, `workflow-ptc`, `tool-workflow`, opt-in `tool-ralph`) | model-written scripts that start subagents; one engine per context; disabled in the stock web profile |
| Agent registry, task board, messaging | experimental `agent-team` (roster, `task-<n>` board, mailbox, shared checkout) | experimental: an adapter must isolate it |
| Objective tracking | `goal` (event-sourced, revisioned) | candidate for the workflow objective |
| Plan state | `plan-mode`, `todo` | soft guidance only; does not enforce |
| Human commands | `ctx.commands` | runs without a model message (§25) |
| Token accounting | `token-meter`, session telemetry | DSH-side only; external workers are not counted |

DSH's architecture documentation specifically recommends attaching new
behavior through documented extension points.

------------------------------------------------------------------------

# 15. Session and Event Architecture

DSH sessions are append-only event logs and act as the source of truth
for model-visible context.

Hydra-Pod should extend this event model rather than creating an
unrelated parallel history.

Proposed Hydra event families:

``` text
hydra/workflow-created
hydra/workflow-state
hydra/task-created
hydra/task-assigned
hydra/task-started
hydra/task-completed
hydra/task-failed

hydra/agent-assigned
hydra/agent-spawned
hydra/agent-started
hydra/agent-completed
hydra/agent-failed

hydra/review-started
hydra/review-finding
hydra/review-completed

hydra/verification-started
hydra/verification-decision
hydra/rework-requested
hydra/re-review-requested
hydra/replan-requested
hydra/approved

hydra/resource-usage
hydra/checkpoint-created
hydra/policy-decision
hydra/workflow-error
```

**[rev 2] One source of truth per fact (G3).** Workers that run outside DSH never write to a DSH session. The rules are:

| Fact | Source of truth | Mirrored to |
|---|---|---|
| Ticket state, acceptance, verdicts | the ticket files and `_receipts/` in the project's git repository (Hydra-Pod today) | DSH session events (display, audit) |
| External worker runs and cost | `_receipts/<T>.costs.jsonl` | DSH session events |
| Manager reasoning and tool calls | the manager's DSH session log | none |
| Live stage | process detection plus `stage.json` (Hydra-Pod-Dsh) | plugin UI |

A workflow spanning several DSH sessions is keyed by `workflow_id` in the project repository, never by a session id.

**[rev 2] Persistence safety (G4).** `hydra/*` events are declared through `SessionEventMap` declaration merging and **must be marked `ignorable: true`**. Otherwise a session written while the plugin was installed cannot be restored after it is removed. They must not be message-projecting (they never enter the model transcript).

Every event must contain:

``` yaml
event:
  id: EVT-001
  workflow_id: WF-001
  task_id: T-001
  agent_id: agent-01
  role: executor
  timestamp: "..."
  type: hydra/task-started
  payload: {}
```

------------------------------------------------------------------------

# 16. Resource Management

Resource accounting is a first-class Hydra-Pod responsibility.

Track per:

-   workflow
-   task
-   role
-   agent
-   provider
-   model
-   attempt
-   review cycle

Metrics:

``` text
runtime
input_tokens
output_tokens
total_tokens
tool_calls
retries
errors
files_changed
tests_run
estimated_cost
```

Example:

``` yaml
resource:
  workflow_id: WF-001
  task_id: T-104
  agent_id: executor-01

  runtime_ms: 183420
  input_tokens: 21442
  output_tokens: 8311
  total_tokens: 29753

  tool_calls: 34
  retries: 1
  tests_run: 17
```

------------------------------------------------------------------------

**[rev 2] Where each metric can actually come from (G10):**

| Worker | Tokens | Cost | Subscription window |
|---|---|---|---|
| Manager in DSH (Anthropic API) | DSH session / `token-meter` | API price × tokens | n/a (pay per use) |
| opencode Builder | opencode JSON events, local `opencode.db` | list-price cost per message | OpenCode Go: **estimate only** (no usage API; per-model limits in `limits.json`, rolling windows assumed) |
| opencode Reviewer (Z.ai) | opencode JSON events | 0 at list price (plan) | Z.ai: **official** 5 h / weekly credits |
| ZCode Reviewer | not exposed | n/a | Z.ai credits (same pool) |
| ACP / Claude Code child | **not returned to the parent** | unknown | unknown |

A metric that cannot be measured is reported as unknown. It is never estimated silently, and an estimate is always labelled as one.

# 17. Budget Policies

Workflows must support:

``` yaml
budget:
  max_runtime_minutes: 30
  max_tokens: 150000
  max_cost: 2.00
  max_agents: 8
  max_retries: 3
```

Threshold behavior:

``` text
80%  -> warning
90%  -> routing optimization
100% -> pause / policy decision
```

The Leader or policy engine can then choose:

``` text
continue
reduce_scope
switch_model
switch_provider
spawn_specialist
abort
```

------------------------------------------------------------------------

# 18. Provider Quotas

Hydra-Pod must maintain provider-level accounting.

Example:

``` yaml
providers:
  dsh:
    quota:
      tokens: 1000000

  opencode:
    quota:
      policy: subscription

  claude:
    quota:
      policy: subscription
```

**[rev 2]** Replace the example with billing routes and their real windows:

``` yaml
billing_routes:
  subscription/opencode-go:
    windows: {5h: 20%, week: 50%, month: 100%}   # of the per-model monthly limit
    source: estimate
  subscription/zai-lite:
    windows: {5h: 2000 credits, week: 10000 credits}   # from the official API
    source: official
  api/anthropic:
    budget: {max_cost_usd_per_workflow: <set by the user>}
    source: dsh-session
  api/deepseek:
    enabled: false          # opt-in only (G1)
```

The Router must avoid providers that are unavailable or outside policy.

------------------------------------------------------------------------

# 19. Model Routing

Routing decisions should consider:

``` text
role
task complexity
required capability
latency
resource budget
provider availability
model availability
review requirements
previous failure history
```

Example policy:

``` yaml
routing:
  planner:
    preference:
      - reasoning

  executor:
    preference:
      - coding

  reviewer:
    preference:
      - code_review
      - reasoning

  verifier:
    preference:
      - reasoning
      - architecture
```

The policy must remain configurable.

**[rev 2] Hard constraints (G1, G2), checked before any preference:**

1. The billing route is allowed for the role.
2. The runtime can honour the requested model (capability flags, §12).
3. The Verifier/Manager model is never the Executor or the Reviewer model.
4. The Reviewer runs under enforced read-only.

------------------------------------------------------------------------

# 20. Checkpoints

Checkpoint after each significant phase:

``` text
CP-001 Planning
CP-002 Assignment
CP-003 Execution
CP-004 Testing
CP-005 Review
CP-006 Rework
CP-007 Re-review
CP-008 Verification
```

A checkpoint records:

``` yaml
checkpoint:
  id: CP-006
  workflow_id: WF-001
  state: REWORK_COMPLETE

  git:
    commit: abc123

  tasks:
    completed: [T1, T2]

  agents:
    active: [reviewer-01]

  resources:
    total_tokens: 48221
```

------------------------------------------------------------------------

# 21. Crash Recovery

Recovery must be based on persisted state/events.

On restart:

``` text
Load workflow
   |
   v
Read event log
   |
   v
Reconstruct state
   |
   v
Identify active tasks
   |
   v
Inspect agent sessions
   |
   v
Resume / retry according to policy
```

Never assume an in-memory state survived process termination.

**[rev 2] (G17)** A workflow can also be waiting for a human rather than crashed: a sandbox escalation approval, an account login (`hydra-pod-connect connect`), or an `ESCALATE`. Recovery distinguishes `waiting_for_user` from `failed`, and the waiting time is excluded from runtime budgets.

------------------------------------------------------------------------

# 22. Git Integration

For software-engineering workflows, Git state is part of the execution
context.

Capture:

``` text
branch
commit
parent commit
diff
files changed
working tree status
tests
review findings
fix commits
```

Recommended lifecycle:

``` text
Task Start
   |
Snapshot
   |
Execute
   |
Test
   |
Review
   |
Fix
   |
Test
   |
Verify
```

The original execution and subsequent fixes must remain traceable.

------------------------------------------------------------------------

# 23. Skills

Hydra-Pod-specific skills should be implemented as DSH skills.

Recommended:

``` text
skills/
├── hydra-planner/
├── hydra-executor/
├── hydra-reviewer/
├── hydra-verifier/
├── hydra-debugger/
├── hydra-security/
├── hydra-testing/
└── hydra-resource-management/
```

Skills provide:

-   role instructions
-   domain knowledge
-   review checklists
-   policies
-   output schemas

The plugin provides:

-   state
-   routing
-   lifecycle
-   tools
-   events
-   resource accounting

------------------------------------------------------------------------

# 24. Hydra Tools

Recommended model-facing tools:

``` text
hydra_status
hydra_plan
hydra_assign
hydra_spawn
hydra_delegate
hydra_review
hydra_review_directive
hydra_verify
hydra_rework
hydra_replan
hydra_resume
hydra_pause
hydra_cancel
hydra_resources
hydra_graph
hydra_checkpoint
```

Tools must be policy-gated.

An Agent should not be able to arbitrarily mutate workflow state.

Example:

``` text
Agent
  |
  v
hydra_rework()
  |
  v
Policy Engine
  |
  +-- allowed -> transition
  |
  +-- denied -> reject
```

------------------------------------------------------------------------

# 25. Human Commands

Recommended commands:

``` text
/hydra status
/hydra agents
/hydra graph
/hydra resources
/hydra review
/hydra verify
/hydra pause
/hydra resume
/hydra retry
/hydra rework
/hydra replan
/hydra cancel
/hydra spawn <role>
/hydra assign <role> <agent>
```

These should use DSH command registration where possible.

**[rev 2] (G16)** `ctx.commands` runs a plugin command directly, without a model message. That fits mechanical commands (`status`, `agents`, `resources`, `pause`, `cancel`). Judgment steps (`review`, `verify`, `rework`, `replan`) must go through the Manager model, so they stay model-facing (the `/hydra-pod` skill). Keep the existing `/hydra-pod` name, and add `/hydra-pod-status` and similar names for commands. A skill and a command with the same name resolve to the command (`dsh-client-ui-skill`), so names must not collide.

------------------------------------------------------------------------

# 26. UI Architecture

The first UI should expose five views.

## Current Activity

``` text
CURRENT AGENT
Role: Executor
Provider: DSH
Model: DeepSeek
Task: T-104
Status: Running
Runtime: 03:21
Tokens: 21,442
Tool Calls: 31
```

## Workflow Graph

Display:

``` text
Planner
   |
Executor
   |
Reviewer
   |
Verifier
 /     \
Done   Rework
          |
       Executor
```

## Agent Panel

``` text
Leader       Ready
Planner      Complete
Executor     Running
Reviewer     Waiting
Verifier     Waiting
Security     Idle
```

## Resource Panel

``` text
Tokens
Runtime
Tool Calls
Retries
Agents
Estimated Cost
```

## Findings Panel

``` text
HIGH     Security issue
MEDIUM   Error handling
LOW      Documentation
```

------------------------------------------------------------------------

# 27. Activity Timeline

Every workflow should expose a timeline:

``` text
20:31:02  Planner started
20:31:19  Plan created

20:31:22  Executor spawned
20:34:51  Executor completed

20:34:53  Reviewer started
20:36:12  3 findings

20:36:15  Verifier started
20:36:31  Rework requested

20:36:35  Executor resumed
20:38:44  Fix completed

20:38:47  Reviewer resumed
20:39:32  Review passed

20:39:35  Verifier started
20:39:48  Approved
```

------------------------------------------------------------------------

# 28. Data Model

Core entities:

``` text
Workflow
Task
Agent
Role
Provider
Model
Session
Attempt
Review
Finding
Directive
Verification
Checkpoint
ResourceRecord
Event
Policy
Budget
```

Relationships:

``` text
Workflow
  ├── Tasks
  │    ├── Attempts
  │    ├── Reviews
  │    └── Verifications
  │
  ├── Agents
  │    └── Sessions
  │
  ├── Checkpoints
  ├── ResourceRecords
  └── Events
```

------------------------------------------------------------------------

# 29. Proposed Type Definitions

Conceptual TypeScript model:

``` typescript
type WorkflowState =
  | "NEW"
  | "PLANNING"
  | "PLAN_READY"
  | "ASSIGNING"
  | "EXECUTING"
  | "TESTING"
  | "REVIEWING"
  | "VERIFYING"
  | "REWORK"
  | "RE_REVIEW"
  | "REPLAN"
  | "APPROVED"
  | "DONE"          // [rev 2] was COMPLETED; one success state (§6)
  | "FAILED"
  | "BLOCKED"       // also the target of ESCALATE
  | "CANCELLED"
  | "ABORTED";      // [rev 2] was missing

type VerificationDecision =
  | "APPROVE"
  | "REWORK"
  | "RE_REVIEW"
  | "REPLAN"
  | "ESCALATE"
  | "ABORT";

interface Workflow {
  id: string;
  objective: string;
  state: WorkflowState;
  tasks: string[];
  leaderAgentId: string;
  createdAt: string;
  updatedAt: string;
}

interface Task {
  id: string;
  workflowId: string;
  role: string;
  status: string;
  dependencies: string[];
  assignedAgentId?: string;
  attempt: number;
}

interface ReviewFinding {
  id: string;
  taskId: string;
  severity: "critical" | "high" | "medium" | "low" | "info";
  category: string;
  location?: string;
  description: string;
  recommendation: string;
  status: "open" | "resolved" | "rejected";
}
```

------------------------------------------------------------------------

# 30. Repository Target Structure

Recommended Hydra-Pod structure:

``` text
Hydra-Pod/
├── hydra_pod/
│   ├── orchestration/
│   ├── workflow/
│   ├── agents/
│   ├── providers/
│   │   ├── base.py
│   │   ├── dsh.py
│   │   ├── opencode.py
│   │   ├── claude.py
│   │   └── codex.py
│   ├── routing/
│   ├── review/
│   ├── verification/
│   ├── resources/
│   ├── checkpoints/
│   ├── policy/
│   ├── events/
│   └── git/
│
├── plugins/
│   └── hydra-pod-dsh/
│       ├── src/
│       ├── package.json
│       └── README.md
│
├── skills/
│   ├── hydra-planner/
│   ├── hydra-reviewer/
│   ├── hydra-verifier/
│   └── ...
│
├── prompts/
├── templates/
├── tests/
│   ├── unit/
│   ├── provider/
│   ├── workflow/
│   ├── integration/
│   ├── recovery/
│   └── e2e/
│
└── docs/
    ├── architecture/
    ├── workflow/
    ├── providers/
    └── operations/
```

The exact directory names may be adapted to the existing Hydra-Pod
repository conventions; the architectural boundaries are the important
part.

**[rev 2] (G12)** The decided layout keeps the two repositories apart:

``` text
~/Hydra-Pod/                    dreamzone-cc/Hydra-Pod (public): core, dispatch, providers, prompts, skill
~/Hydra-Pod/Hydra-Pod-Dsh/      separate repository (local, excluded from the parent)
├── skills/                     /hydra-pod + opus-manager loader
├── hydra_pod_dsh/, bin/        status / stage (the JSON boundary, §13.3)
├── plugin/                     dsh-hydra-pod (no build step)
├── scripts/, tests/, docs/
└── HYDRA-POD-DSH-ARCHITECTURE.md
```

Hydra-Pod-Dsh depends on Hydra-Pod and never modifies it. A change that belongs in Hydra-Pod's core (e.g. a provider contract) goes to that repository as its own change.

------------------------------------------------------------------------

# 31. Implementation Phases

**[rev 2] Every phase has an exit criterion. The next phase starts only when it passes (G14):**

| Phase | Exit criterion (measurable) |
|---|---|
| 0 | data model doc merged; a reuse/reject decision for each DSH package in §14 |
| 0.5 (new) | one real ticket (W1) closed with the Manager in DSH on the current integration; costs recorded |
| 1 | a DSH-backed executor completes W1 through the provider contract; its billing route is shown in the ledger |
| 2 | plugin loads on the pinned DSH version; `hydra/*` events survive plugin removal (restore test) |
| 3–5 | W1–W4 pass; every transition is in the ledger and mirrored to the session |
| 6 | W5 passes with a child whose model the Router selected (in-process or SDK only) |
| 7 | usage for every worker is attributed, or reported as unknown |
| 8 | W6 passes after killing the executor mid-run |
| 9 | UI shows the §26 views from ledger data only |

## Phase 0.5 --- [rev 2] Validate the current integration

Before building anything new, run one real ticket end to end with the manager in DSH (skills + `hydra-pod-dispatch` + `dsh-hydra-pod`). Record the approvals needed, the costs and every deviation from the flow. This gives the baseline that every later phase is compared with.

## Phase 0 --- Architecture Freeze

Define and freeze:

-   Role
-   Agent
-   Provider
-   Model
-   Workflow
-   Task
-   Attempt
-   Finding
-   Directive
-   Verification
-   Checkpoint
-   Resource
-   Event

Deliverable:

``` text
docs/architecture/data-model.md
```

## Phase 1 --- DSH Provider

Implement:

``` text
DSHProvider
```

Prove:

``` text
Hydra-Pod
  -> DSH
  -> Agent
  -> Result
```

No advanced orchestration yet.

## Phase 2 --- Native DSH Plugin

Implement:

-   plugin bootstrap
-   Hydra commands
-   Hydra tools
-   Hydra events
-   status bridge

## Phase 3 --- Workflow Engine

Implement:

-   state machine
-   task graph
-   event transitions
-   checkpointing

## Phase 4 --- Core Roles

Implement:

-   Leader
-   Planner
-   Executor
-   Reviewer
-   Verifier

## Phase 5 --- Review Director

Implement:

-   findings
-   directives
-   rework
-   re-review
-   replan

## Phase 6 --- Dynamic Agents

Implement:

-   capability matching
-   DSH subagent integration
-   specialist spawning
-   agent lifecycle
-   depth/retry policy

## Phase 7 --- Resource Manager

Implement:

-   token tracking
-   runtime tracking
-   tool-call tracking
-   retry tracking
-   quota
-   budgets
-   provider accounting

## Phase 8 --- Recovery

Implement:

-   pause
-   resume
-   retry
-   checkpoint recovery
-   agent crash recovery
-   workflow recovery

## Phase 9 --- UI

Implement:

-   current activity
-   workflow graph
-   agent list
-   resource dashboard
-   findings
-   timeline

## Phase 10 --- External Providers

Normalize:

-   DSH
-   OpenCode
-   Claude Code
-   Codex
-   ZCode
-   future providers

------------------------------------------------------------------------

# 32. Testing Strategy

## Unit Tests

Test:

-   state transitions
-   routing
-   policies
-   budgets
-   retry limits
-   finding classification
-   verifier decisions
-   graph validation

## Provider Tests

For each provider:

``` text
spawn
send
stream
status
interrupt
resume
dispose
```

## Workflow Tests

**[rev 2] (G13)** Workflow scenarios are named W1–W10, so they do not collide with ticket ids (`T<N>`) or with Hydra-Pod's recorded validations T1–T3.

### W1 --- Happy path

``` text
Planner
 -> Executor
 -> Reviewer
 -> Verifier
 -> APPROVE
 -> DONE
```

### W2 --- Rework

``` text
Reviewer
 -> FAIL
 -> Verifier
 -> REWORK
 -> Executor
 -> Reviewer
 -> Verifier
 -> APPROVE
```

### W3 --- Re-review

``` text
Reviewer
 -> Verifier
 -> RE_REVIEW
 -> Reviewer
 -> Verifier
 -> APPROVE
```

### W4 --- Re-plan

``` text
Reviewer
 -> Verifier
 -> REPLAN
 -> Planner
 -> New Graph
```

### W5 --- Specialist

``` text
Planner
 -> Security Agent
 -> Executor
 -> Reviewer
 -> Verifier
```

### W6 --- Agent failure

``` text
Executor
 -> crash
 -> recovery
 -> resume/retry
```

### W7 --- Budget exhaustion

``` text
Agent
 -> 100% budget
 -> pause
 -> routing decision
```

### W8 --- Concurrent agents

Run:

``` text
Executor
Security
Tester
```

concurrently and merge results.

### W9 --- Multiple reviewers

``` text
Reviewer A
Reviewer B
Security Reviewer
      |
      v
   Verifier
```

### W10 --- Full E2E

Complete real software change from user request to verified result.

------------------------------------------------------------------------

# 33. Acceptance Criteria

The integration is not complete when the plugin merely loads.

MVP acceptance requires:

1.  Hydra-Pod can start a DSH-backed agent.
2.  Planner creates a structured task graph.
3.  Executor performs a real implementation.
4.  Reviewer creates structured findings.
5.  Verifier produces a structured decision.
6.  Rework returns execution to the Executor.
7.  Re-review returns execution to the Reviewer.
8.  Re-plan returns execution to the Planner.
9.  Every transition is persisted/auditable.
10. Resource usage is attributed to workflow/task/agent/provider/model.
11. A workflow can resume after interruption.
12. Dynamic DSH subagents can be created.
13. Provider selection is independent from role selection.
14. No core DSH agent-loop fork is required.
15. Existing Hydra-Pod workflows remain usable during migration.
16. **[rev 2]** No subscription is used outside its supported client, and every run's billing route is recorded.
17. **[rev 2]** No component reads, copies or logs a credential. Logins stay with the official tools (`hydra-pod-connect connect`).
18. **[rev 2]** Removing the plugin leaves every DSH session restorable (G4).

------------------------------------------------------------------------

# 34. Migration Strategy

Do not perform a destructive rewrite.

Use incremental migration.

### Stage 0 --- [rev 2] Current state (G6)

Hydra-Pod v1.0.0 plus Hydra-Pod-Dsh: the manager runs in DSH, the workers run through `hydra-pod-dispatch`, and the plugin shows the live stage and usage. Each later stage must keep the ticket files, receipts, `PROBE:` lines, preflight, watchdog and cost log working, or replace them with a proven equivalent.

### Stage A

Keep existing Hydra-Pod providers.

Add:

``` text
DSHProvider
```

### Stage B

Run:

``` text
existing Hydra workflow
+
DSH executor
```

### Stage C

Move Reviewer to DSH.

### Stage D

Move Verifier to DSH.

### Stage E

Enable dynamic DSH subagents.

### Stage F

Enable Hydra resource manager.

### Stage G

Enable native DSH plugin UI/events.

### Stage H

Remove only duplicated legacy runtime code after equivalent behavior is
proven.

------------------------------------------------------------------------

# 35. Compatibility Rules

Because DSH currently describes its public APIs as pre-stable, Hydra-Pod
must isolate DSH-specific integration behind an adapter.

Do not scatter direct imports of DSH internals throughout Hydra-Pod.

Preferred:

``` text
Hydra Core
    |
Provider Interface
    |
DSH Adapter
    |
DSH API
```

Not:

``` text
Hydra Core
    |
    +-- direct DSH internals
    +-- direct DSH session internals
    +-- direct DSH agent-loop internals
    +-- direct DSH tool internals
```

This minimizes migration cost when DSH APIs change.

------------------------------------------------------------------------

# 36. Versioning and Schema Policy

All persistent Hydra events should be versioned.

Example:

``` yaml
schema:
  name: hydra/workflow-created
  version: 1
```

If an event schema changes:

``` text
v1 remains readable
v2 is introduced
```

Never silently reinterpret old persisted events.

Provider adapters should declare:

``` yaml
provider:
  name: dsh
  adapter_version: 1
  supported_dsh_versions:
    - "0.x"
```

**[rev 2] (G15)** `0.x` is too wide for a pre-stable runtime. Pin the exact tested version (today `0.2.0-rc.1`, commit `4878cda`), and re-run the plugin and adapter tests on every DSH update. DSH's plugin manager refuses a plugin whose peer ranges exclude the running DSH, so declare only the peers actually imported. `dsh-hydra-pod` declares none.

------------------------------------------------------------------------

# 37. Security and Permissions

Agents must not automatically receive unrestricted access.

Permissions should be role-based.

Example:

``` yaml
permissions:
  planner:
    filesystem: read
    shell: none
    git: read

  executor:
    filesystem: read_write
    shell: allowed
    git: allowed

  reviewer:
    filesystem: read
    shell: tests_only
    git: read

  verifier:
    filesystem: read
    shell: tests_only
    git: read
```

Use DSH sandbox and capability mechanisms rather than implementing a
second sandbox where possible.

**[rev 2] What enforces each rule (G7).** The DSH sandbox is a per-session file policy (`read-only` / `workspace-write` / `danger-full-access`, via bwrap on Linux). It does not restrict network access and has no per-command rules such as `tests_only`. Enforcement therefore sits where the work runs:

| Role | Runs in | Enforced by |
|---|---|---|
| Manager | DSH session | DSH sandbox (`workspace-write`, with an approval per escalation) and the approval policy |
| Executor | opencode | `--auto` inside the ticket's boundaries; acceptance diff review; no commit rights |
| Reviewer | opencode `reviewer` agent | `edit: deny`, bash allow-list (re-verified per project) |
| Reviewer (alt) | ZCode | tool set Read/Glob/Grep, MCP off |
| DSH children | DSH | sandbox mode, tool filters (in-process only), ACP `permission` |

## 37a. [rev 2] Billing and terms policy (G1)

- Every model runs through a client its provider supports: OpenCode Go through opencode, the Z.ai GLM Coding Plan through opencode (`zai-coding-plan`) or ZCode, and Claude Pro through Claude Code or Anthropic apps only.
- The manager in DSH uses an Anthropic **API key** (decided 2026-09-28). Plugins that bridge consumer OAuth into DSH (for example ones that reuse Claude Code's login) are out of policy.
- New billing routes (e.g. `api/deepseek`) are opt-in, per project, and recorded in `workers.md`.
- Excluded: `opencode-go/glm-5.3` for review (it bills OpenCode Go), `zai/glm-5.3` (pay-as-you-go), and `pi` with Z.ai.

------------------------------------------------------------------------

# 38. Observability

Every workflow should expose:

``` text
Workflow ID
Task ID
Agent ID
Session ID
Provider
Model
Role
Attempt
State
Duration
Tokens
Tools
Cost
Findings
Decision
Checkpoint
```

Correlation IDs:

``` text
workflow_id
task_id
attempt_id
agent_id
session_id
event_id
```

These IDs must be propagated through all provider adapters.

------------------------------------------------------------------------

# 39. Operational Logging

Use structured logs.

Example:

``` json
{
  "event": "hydra/review-completed",
  "workflow_id": "WF-001",
  "task_id": "T-104",
  "agent_id": "reviewer-01",
  "decision": "FAIL",
  "findings": 3,
  "duration_ms": 91342
}
```

Do not rely on parsing human-readable terminal output for workflow
state.

------------------------------------------------------------------------

# 40. Resource Optimization Strategy

The Router should optimize:

``` text
quality
latency
cost
quota
availability
```

Do not optimize cost alone.

Recommended decision order:

``` text
1. Capability compatibility
2. Policy compliance
3. Required quality
4. Availability
5. Resource budget
6. Latency
7. Cost
```

------------------------------------------------------------------------

# 41. Concurrency Policy

Independent tasks should run concurrently.

Example:

``` text
T1 Architecture
 |
 +---- T2 Implementation
 |
 +---- T3 Test Design
 |
 +---- T4 Security Analysis
          |
          v
       T5 Review
```

The Planner must explicitly mark dependencies.

Never infer concurrency from textual descriptions alone.

**[rev 2] (G11)** Parallel executors must not share one working tree. Each concurrent write task gets its own git worktree (or DSH `agent-team`'s shared-checkout rules, if adopted in Phase 0), and merging is a separate, verified step. Until then, `hydra-pod-dispatch preflight` requires a clean tree, which serialises builds.

------------------------------------------------------------------------

# 42. Failure Handling

Failures must be classified:

``` text
AGENT_FAILURE
PROVIDER_FAILURE
MODEL_FAILURE
TOOL_FAILURE
TEST_FAILURE
REVIEW_FAILURE
POLICY_FAILURE
RESOURCE_EXHAUSTION
INFRASTRUCTURE_FAILURE
```

Each class has a policy.

Example:

``` text
MODEL_FAILURE
  -> retry
  -> alternate model
  -> alternate provider

TOOL_FAILURE
  -> retry tool
  -> alternate tool
  -> escalate

REVIEW_FAILURE
  -> verifier
  -> re-review
```

------------------------------------------------------------------------

# 43. Human Override

The user must be able to intervene.

Commands:

``` text
pause
resume
cancel
approve
reject
retry
replan
assign
reassign
```

Human decisions must be persisted as workflow events.

Example:

``` yaml
decision:
  actor: user
  action: approve
  reason: "Accept current implementation"
```

------------------------------------------------------------------------

# 44. Recommended MVP Scope

Do not implement every feature initially.

MVP:

``` text
[1] DSH Provider
[2] Native DSH Plugin
[3] Planner
[4] Executor
[5] Reviewer
[6] Verifier
[7] Workflow state machine
[8] Rework
[9] Re-review
[10] Resource tracking
[11] Event/audit
[12] Checkpoint
```

**[rev 2] (G14)** A first release that can actually be validated is smaller:

``` text
MVP-0 (exists)   manager in DSH, dispatch workers, live stage + usage
MVP-1            Phase 0.5 validation (W1 real ticket)
MVP-2            JSON contract + ledger mirrored to ignorable hydra/* events
MVP-3            W2 rework and W3 re-review on the existing ticket files
```

Items [1]–[12] above follow in that order. None starts before the previous exit criterion passes (§31).

Post-MVP:

``` text
[13] Dynamic specialists
[14] Multi-reviewer
[15] Re-plan
[16] Provider optimization
[17] UI
[18] advanced quotas
[19] concurrent workflows
```

------------------------------------------------------------------------

# 45. Final Architecture Decision Record

## ADR-001 --- Hydra-Pod remains the Orchestrator

**Decision:** Accepted.

Hydra-Pod owns workflow policy and orchestration.

## ADR-002 --- DSH is a first-class Runtime Provider

**Decision:** Accepted.

DeepSeek Harness is integrated through an adapter/plugin rather than
forked.

## ADR-003 --- Reuse DSH Subagent

**Decision:** Accepted.

Dynamic child agents use `ctx.subagents`.

## ADR-004 --- Reuse DSH Sessions

**Decision:** Accepted.

Hydra events should integrate with the DSH session/event architecture.

## ADR-005 --- Do not fork agent-loop

**Decision:** Accepted.

Only change the DSH loop if an explicit capability gap is demonstrated.

## ADR-006 --- Separate Role / Agent / Provider / Model

**Decision:** Mandatory.

This is required for dynamic routing and future providers.

## ADR-007 --- Verifier is a Review Director

**Decision:** Accepted.

Verifier controls approve/rework/re-review/replan decisions.

## ADR-008 --- Resource management belongs to Hydra-Pod

**Decision:** Accepted.

Hydra-Pod aggregates resource usage across runtimes and providers.

------------------------------------------------------------------------

## ADR-009 --- [rev 2] Billing route is part of routing

**Decision:** Mandatory. Role → Agent → Runtime → Model → Billing route; the Router never changes the billing route on its own (G1).

## ADR-010 --- [rev 2] Workers stay on their official clients

**Decision:** Accepted. OpenCode Go and Z.ai run through opencode/ZCode via `hydra-pod-dispatch`. ACP children are not used for them until opencode's ACP server supports model and agent selection (measured 2026-09-28).

## ADR-011 --- [rev 2] Project repository is the ledger

**Decision:** Accepted. Ticket files and receipts are the source of truth. DSH session events are an `ignorable` mirror (G3, G4).

------------------------------------------------------------------------

# 46. Final Target

The completed system should behave as follows:

``` text
                         USER
                           |
                           v
                       LEADER
                           |
                           v
                       PLANNER
                           |
                    TASK GRAPH
                           |
          +----------------+----------------+
          |                |                |
          v                v                v
       EXECUTOR        SPECIALIST         TESTER
          |                |                |
          +----------------+----------------+
                           |
                           v
                        REVIEWER
                           |
                           v
                        VERIFIER
                           |
            +--------------+--------------+
            |              |              |
            v              v              v
         APPROVE        REWORK         RE_REVIEW
            |              |              |
            v              v              v
           DONE         EXECUTOR        REVIEWER
                           |              |
                           +------+-------+
                                  |
                                  v
                               VERIFIER
                                  |
                         +--------+--------+
                         |                 |
                      APPROVE            REPLAN
                         |                 |
                         v                 v
                        DONE             PLANNER
```

At every point the system knows:

``` text
WHO is working?
WHAT are they doing?
WHY are they doing it?
WHICH runtime is executing it?
WHICH model is being used?
HOW much time has been consumed?
HOW many tokens were used?
HOW many tools were called?
WHAT findings were produced?
WHO verified them?
WHY did the workflow move to the next state?
WHAT should happen next?
```

That is the defining objective of the Hydra-Pod + DeepSeek Harness
integration.

------------------------------------------------------------------------

# 47. Implementation Rule of Thumb

When adding a feature, ask:

``` text
Does DSH already provide this?
```

If yes:

``` text
Reuse DSH.
```

If the feature is about:

``` text
workflow policy
role assignment
routing
verification
resource management
cross-agent orchestration
```

implement it in:

``` text
Hydra-Pod.
```

If it is about:

``` text
agent execution
session
tool
skill
subagent transport
LLM adapter
sandbox
MCP
```

prefer:

``` text
DeepSeek Harness.
```

If the feature crosses both:

``` text
Hydra-Pod policy
        +
DSH extension point
```

Use an adapter/plugin boundary.

------------------------------------------------------------------------

# 48. Primary Deliverables

The implementation program should produce:

``` text
1. Hydra-Pod DSH Provider
2. Hydra-Pod DSH Plugin
3. Workflow State Machine
4. Task Graph
5. Agent Registry
6. Provider Router
7. Review Engine
8. Verification Engine
9. Resource Manager
10. Checkpoint Manager
11. Hydra Event Schema
12. DSH Skills
13. Hydra Tools
14. CLI Commands
15. Workflow Dashboard
16. Provider Adapter Tests
17. E2E Workflow Tests
18. Recovery Tests
19. Architecture Documentation
20. Migration Documentation
```

------------------------------------------------------------------------

# 49. Definition of Done

The project is considered production-ready for the initial release when:

-   the existing Hydra-Pod workflow still works;
-   DSH can act as Executor, Reviewer, Planner, or Verifier;
-   roles can be reassigned without changing workflow code;
-   dynamic DSH subagents work;
-   rework and re-review work reliably;
-   re-plan is supported;
-   every workflow transition is auditable;
-   resource usage is attributed correctly;
-   failed agents can be resumed/retried;
-   budgets are enforced;
-   provider routing is policy-driven;
-   no unnecessary DSH fork exists;
-   the DSH integration is isolated behind an adapter;
-   automated unit, integration, recovery, and E2E tests pass.

------------------------------------------------------------------------

## 50. Reference Sources

-   DeepSeek Harness architecture and extension model:
    `https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md`
-   DeepSeek Harness core subsystem:
    `https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/core.md`
-   DeepSeek Harness subagent subsystem:
    `https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/subagent.md`
-   DeepSeek Harness subsystem index:
    `https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/README.md`
-   DeepSeek Harness developer guidance:
    `https://github.com/deepseek-ai/deepseek-harness/blob/master/AGENTS.md`
-   Hydra-Pod: `https://github.com/dreamzone-cc/Hydra-Pod`

------------------------------------------------------------------------

## Final Recommendation

The implementation should **not** be approached as "adding a few Hydra
skills to DSH."

It should be implemented as:

``` text
Hydra-Pod
=
Orchestration + Policy + Routing + Verification + Resources

DeepSeek Harness
=
Runtime + Agents + Sessions + Tools + Subagents + Skills + MCP
```

with:

``` text
Hydra-Pod-Dsh
=
the integration boundary
```

This preserves the proven Hydra-Pod workflow, maximizes reuse of
DeepSeek Harness capabilities, minimizes coupling to unstable DSH
internals, and creates a foundation capable of supporting multiple agent
runtimes and providers in the future.
