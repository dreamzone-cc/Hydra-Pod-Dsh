# Hydra-Pod-Dsh

*Hydra-Pod, managed from DeepSeek Harness*

[Hydra-Pod](https://github.com/dreamzone-cc/Hydra-Pod) runs its manager inside Claude Code. Hydra-Pod-Dsh lets [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`) host the manager instead. The model running the dsh session plans tickets, reads diffs and validates review findings. The builder (OpenCode Go, DeepSeek V4.1 Flash) and the reviewer (Z.ai Lite, GLM-5.3) are unchanged: they still run through `hydra-pod-dispatch` and opencode.

```
dsh session (manager) → Plan → hydra-pod-dispatch build → DeepSeek → Tests → hydra-pod-dispatch review → GLM-5.3
                      → Validate findings → Fix ticket → … → Final verification → Done
```

| Role | Hydra-Pod | Hydra-Pod-Dsh |
|---|---|---|
| **Manager and verifier** | Claude Code + skill `opus-manager` (Opus 5.5) | dsh + skills `hydra-pod`, `opus-manager` (the dsh session's model) |
| **Builder** | `opencode run`, `opencode-go/deepseek-v4.1-flash` | same |
| **Reviewer** (read-only) | `opencode run --agent reviewer`, `zai-coding-plan/glm-5.3` (or ZCode) | same |

This repository adds only the harness side. It depends on a Hydra-Pod checkout at `~/Hydra-Pod` (or `$HYDRA_POD_HOME`) and does not change it.

## Status

Built and tested (106 tests: 98 Python and 8 Node, with mutation checks on the escalation limit, the ledger lock, the ledger hash chain, the budget block, the reviewer read-only rule and work-vs-cache token accounting). Every ledger transition (fold, check, append) runs under one lock, so two concurrent commands cannot both validate against the same state; operational failures (a typo'd workflow id, an unreadable config file or record) print one clean line with the documented exit code instead of a traceback. **Validated end to end with the real workers** (OpenCode Go builder, Z.ai GLM-5.3 reviewer) in `docs/04-validation-scripted-w1.md`; that run found two integration defects, both fixed. What is built:

- the skills, the live stage and usage plugin;
- the workflow ledger and state machine;
- cost attribution by billing route;
- router and policy checks;
- budgets;
- findings;
- health and recovery;
- git checkpoints;
- manager usage from the DSH logs;
- DSH human commands;
- the five-view dashboard in the composer popover.

**Not yet run: a real ticket with the manager (Claude Opus 5.5) in dsh.** That is Phase 0.5, and it needs the operator: an Anthropic API key and the approvals in the dsh UI. See `docs/03-validation-w1.md`.

After editing `plugin/`, reinstall it (`dsh plugin --profile web add file:…/plugin`) and restart dsh.

## Live stage and usage (dsh-hydra-pod plugin)

A pill above the composer shows who is working now and the usage of the subscription in use. Click it to see every window of both subscriptions, with its reset countdown:

```
● Hydra-Pod · T6 · Builder: opencode · deepseek-v4.1-flash (OpenCode Go) · Go 5h 12% · resets 3h 10m
```

- **Who is working** comes from two sources. Running workers are detected from their processes (`opencode run -m …`, `hydra-pod-connect review zcode-lite`, `hydra-pod-dispatch build|review <T>`). The manager's own stage is recorded with `hydra-pod-dsh stage` (planning, accepting, validating, …).
- **Z.ai Lite (GLM Coding Plan, also used by ZCode)** is exact. It comes from the official quota endpoint (5-hour and weekly windows, with reset times) and is cached for one minute.
- **OpenCode Go is an estimate.** The plan has no usage API: usage is visible only in the web console ([opencode#31084](https://github.com/anomalyco/opencode/issues/31084) was closed). The estimate is built from the cost opencode records for every message in its local database, against the documented per-model limits in `limits.json` (5 h = 20% and a week = 50% of the monthly limit). It counts only this machine, and the windows are treated as rolling, which the docs do not confirm.
- The same picture is available in a terminal: `hydra-pod-dsh status` (or `--json`).

The plugin is installed from this checkout with `dsh plugin --profile web add file:$HOME/Hydra-Pod/Hydra-Pod-Dsh/plugin`. It needs no build and declares no peer dependencies, so it passes dsh's version check. Its only route, `GET /api/hydra-pod/status`, is read-only and requires the dsh session: without it the route returns 401.

## Workflow ledger

Every task is a workflow in `<project>/_receipts/ledger.jsonl`: an append-only, versioned event log committed with the tickets. It is the single source of truth for state; nothing is kept in memory, so a crash loses nothing (see `docs/02-data-model.md`).

```bash
hydra-pod-dsh wf start T6 --objective "count_vowels with tests"
hydra-pod-dsh wf advance WF-T6 PLANNING        # … ASSIGNING, EXECUTING, TESTING, REVIEWING, VERIFYING
hydra-pod-dsh wf decide WF-T6 REWORK --reason "RF-1 valid" --task T6b
hydra-pod-dsh wf decide WF-T6 RE_REVIEW --reason "…" --directive '{"findings_to_recheck":["RF-2"]}'
hydra-pod-dsh wf resources WF-T6               # cost log → ledger, totals per role/model/billing route
hydra-pod-dsh wf timeline WF-T6
```

- **Illegal moves** are refused (exit status 2). Errors print one clean line: exit 1 for environment problems (unreadable paths), 2 for refused moves and bad input, 3 for policy/budget blocks, 4 for drift. Ticket and workflow ids are validated (one safe path component), so a malformed id never reaches a file name or a glob.
- **Limits:** a decision past its limit (`max_rework_attempts` 3, `max_review_cycles` 4, `max_replans` 2) becomes `ESCALATE` → `BLOCKED` and waits for the user (`wf human … resume|cancel`).
- **Live view:** every `wf` call also sets the live stage, and the pill's popover shows the workflow state, its counters and the timeline.

## Router, policy, budgets, findings, recovery

| Command | What it does |
|---|---|
| `hydra-pod-dsh policy` | Checks the agent registry (`agents.json` plus extensions in `agents.d/*.json`, which may add agents and pools but never change the policy): billing route per role, reviewers read-only, a manager model different from the workers, pools that name real agents of the right role, and a later review stage from a vendor other than the executor's. Exit 3 on a violation |
| `hydra-pod-dsh route <role> [--capability C] [--model M]` | The first compliant agent. It refuses a model the runtime cannot select (ZCode, ACP, Claude Code children) |
| `wf start … --budget-cost/--budget-credits/--budget-minutes/--budget-manager-tokens` | Budgets. Dispatch warns at 80%; at 100% it blocks (exit 3, `BLOCKED` by policy) |
| `wf findings WF` | The reviewer's findings, conclusions (`RC-n`) and suggestions (`RS-n`) with the manager's verdicts. `wf decide APPROVE` is refused while any of them is unanswered |
| `wf health WF` / `wf recover WF --reason …` | Detects a stalled build or review, then retries it |
| `wf manager WF` | The manager's own token usage from the DSH session logs, with its billing route checked. `pi-anthropic` means Claude Pro via OAuth — an operator-approved manager billing route (since 2026-09-30) |
| `wf stages WF` | Per stage: which model worked (manager, builder, reviewer), its tokens (work = input + output + reasoning; cache apart), its cost or Z.ai credits, and its share of the 5 h / weekly windows with their reset times. The same table is in the popover and in `wf report` |
| `wf diffsum WF [--task T] [--base REV] [--head REV]` | Deterministic summary of a ticket's change (files, line counts, symbols added or removed, files outside `allowed_files`, new untracked files), so the manager reads the raw diff only where judgment is needed. No model call |
| `wf brief WF` | Resume brief in under 30 lines: state, counters, unverified findings, recent events and the moves allowed next. For the manager after a context compaction |
| `bench report [--project DIR …] [--save FILE]` / `bench compare BASE.json NEW.json` | Gate metrics per workflow and their medians (manager work tokens and cache hit ratio, worker tokens, review tokens per 100 changed lines, first-try approval, finding precision, PLAN_READY→DONE time); `compare` shows each metric's change against a baseline |
| `wf pack WF [--task T] [--tokens N]` | Writes `_receipts/<ticket>.context.md`: the files the ticket changes (whole when small, else an outline), its `read_hints:`, a repository map focused on them, the matching lessons, and the reviewer's request for conclusions (`RC-n`) and suggestions (`RS-n`). The ticket body points to it; Hydra-Pod is unchanged |
| `map [--focus F …] [--tokens N]` | Ranked outline of the repository's definitions within a token budget (stdlib only: regex extraction, reference graph, personalized PageRank; cached by git state) |
| `lesson add --text … [--path P] [--tag T] [--wf WF]` / `lesson list` | The project's lessons memory in `_receipts/lessons.md`; matching lessons go into later context packs |
| `pick <role> [--complexity S\|M\|L] [--wf WF]` | The best available agent of the role's pool (registry v2): hard policy constraints first, then the ticket's complexity, the agent's availability on this machine and its subscription window, and the pool's fallback order. `--wf` records the choice (`hydra/route-decision`) |
| `wf reviewers WF` | The review cascade for this ticket: which stages run (always, on `risk: high`, or when the manager rejected a high finding or a conclusion), who runs each, and the dispatch command |
| `profile list\|show\|set NAME` / `/hydra-pod-profile` | The project's team profile in `.hydra/profile`: `economy` (never a second review), `balanced` (default), `max-quality` (every stage). A profile never widens the policy |
| `plan approve NAME --ticket T1 --ticket T2:T1 --reason …` / `plan show NAME` / `plan list` | An approved multi-ticket plan (`hydra/plan-approved`): dependency waves in which no two tickets share files, the state of each ticket, and what may start now. `wf advance … EXECUTING` is refused while a ticket's dependencies are not DONE |
| `wf check [WF]` | Compares the ledger with the ticket folders (`claim`/`close` move files, `wf` records state). Exit 4 on drift |
| `wf report WF [--write]` | Closing report from the ledger, the cost log, the review reports and the DSH logs; `--write` saves `_receipts/WF-<T>.report.md` |
| `/hydra-pod-status`, `/hydra-pod-timeline`, `/hydra-pod-control` | DSH commands (no model message). The control command records your decision in the ledger |

Optional: put per-million-token prices in `pricing.json` to turn manager tokens into cost. Without them the cost stays unknown.

## Quick start

```bash
# 0. Prerequisites: Hydra-Pod installed (its install.sh puts the receipt template and
#    hydra-pod-dispatch/-connect in place), dsh on PATH, and python3 3.14+
#    (reads the DSH session logs; doctor.sh warns on older Python).
~/Hydra-Pod/scripts/install.sh

# 1. Link the two skills into ~/.dsh/skills (never replaces an unrelated entry without --force)
~/Hydra-Pod/Hydra-Pod-Dsh/scripts/install.sh

# 2. Show the live stage and usage in dsh
dsh plugin --profile web add file:$HOME/Hydra-Pod/Hydra-Pod-Dsh/plugin

# 3. Check everything without spending quota
~/Hydra-Pod/Hydra-Pod-Dsh/scripts/doctor.sh

# 4. Prepare the project, exactly as for Hydra-Pod
~/Hydra-Pod/scripts/init-project.sh /path/to/project

# 5. Start dsh in the project and pick the manager model in the model menu
cd /path/to/project && dsh web
#    then, in a new session:
/hydra-pod add remember-me to the login page
```

## What is different from Claude Code

- **The manager model is Claude Opus 5.5, through an Anthropic API key** added in dsh (Settings → Models, provider `anthropic`), billed per use. **Operator decision 2026-09-30:** the Claude Pro subscription reached through an OAuth bridge (`pi-anthropic` models, billing `oauth/claude-pro`) is also an allowed manager billing route; the API key stays the default. The bridge is the operator's own plugin and subscription (see `~/Hydra-Pod/docs/07-security.md` for the original terms note). The manager plans, runs the workflow, and gives the final approval. The skill warns if the manager is the same model as a worker.
- **Sandbox.** dsh confines shell commands to the project folder (`workspace-write`). `hydra-pod-dispatch`, `hydra-pod-connect` and opencode also write to their own state directories, so each such call is denied once and then retried with `danger-full-access` after you approve it. To avoid the prompts, switch the session to full access in the UI, or start dsh with `DSH_PERMISSION_MODE=danger-full-access`. Full access means that no command in that session is confined.
- **Long steps.** The bash tool times out after 60 s. Builds and reviews therefore run as dsh background jobs, and the skill tells the manager to wait for them with `job_output` rather than restarting them.
- **No shell state between calls.** Every call is a fresh `bash -c`, so the skill puts `cd <project> &&` and `HYDRA_POD_COMMIT_TRAILER=…` on each command line.
- **`opus-manager` loader.** Hydra-Pod's vendored `skill/opus-manager/SKILL.md` has an unquoted `: ` in its description. Claude Code accepts that, but dsh's YAML parser rejects it and silently skips the skill. `skills/opus-manager` here is a small loader with valid frontmatter that tells the model to read the vendored file, so there is still a single source of instructions.

## Repository contents

```
skills/hydra-pod/SKILL.md      the /hydra-pod entry point (slash-only: disable-model-invocation)
skills/opus-manager/SKILL.md   loader for Hydra-Pod's vendored opus-manager skill
scripts/install.sh             links both skills into $DSH_HOME/skills (backups go outside skills/)
scripts/doctor.sh              no-quota readiness check (also checks python3 3.14+)
scripts/test.sh                runs both suites: python3 -B -m unittest, node --test
scripts/check-skill.mjs        validates SKILL.md frontmatter with dsh's own YAML parser
hydra_pod_dsh/, bin/hydra-pod-dsh  live stage and usage (live, usage), workflow ledger and state machine
                               (ledger, workflow), cost attribution (resources); CLI in cli.py
limits.json                    OpenCode Go per-model monthly limits (from the official docs)
plugin/                        the dsh-hydra-pod plugin: a status route and the composer pill (no build step)
docs/01-design.md              design, constraints found in dsh, the ACP investigation
docs/02-data-model.md          Phase 0: entities, event schema v1, state machine, DSH reuse decisions
docs/03-validation-w1.md       Phase 0.5 runbook: the first real ticket with the manager in dsh
docs/04-validation-scripted-w1.md  scripted W1 with real workers: result, defects found and fixed, closing report
HYDRA-POD-DSH-ARCHITECTURE.md  target architecture, revision 2 (reviewed against both repositories)
tests/                         98 Python tests + 8 Node tests (or just scripts/test.sh)
```

## License

GNU Affero General Public License v3.0 or later (see `LICENSE`), the same license as Hydra-Pod.
