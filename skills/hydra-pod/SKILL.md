---
name: hydra-pod
description: Run a task in Hydra-Pod ticket mode inside DeepSeek Harness (this session manages; DeepSeek builds; GLM-5.3 reviews). Invoke as /hydra-pod followed by the task.
disable-model-invocation: true
user-invocable: true
---

# Hydra-Pod in DeepSeek Harness

Use the `opus-manager` skill for this task (load it with the `skill` tool), in managed mode: you are the manager. You plan, dispatch, accept and verify; you do not write the implementation yourself. Wherever that skill or the Hydra-Pod docs say "Opus" or "Claude Code", read "the manager": the model running this DeepSeek Harness session.

Task: the rest of the user's message that invoked `/hydra-pod`. If it contains no task, ask for one before doing anything else.

Paths: `~/Hydra-Pod` in these instructions means the Hydra-Pod checkout — `$HYDRA_POD_HOME` when that variable is set, else `~/Hydra-Pod`. `references/` means this skill's resource folder.

Governing flow: the manager plans tickets with acceptance criteria → DeepSeek implements and adds tests → tests → GLM-5.3 reviews read-only → the manager runs the reviewer's `PROBE:` lines with `~/Hydra-Pod/scripts/probe-run.py <report> --ref <head> [--run]` and validates every finding against the code (valid → explicit fix ticket for DeepSeek; invalid → one-line rejection; never forward a reviewer's proposed fix unchecked) → DeepSeek fixes within scope → tests → the manager's final verification → done. Keep roles strictly separate. The manager must not be the builder or the reviewer model: if this session runs the same model as a worker, say so and let the user decide.

## Read on demand

Read these with your file-read tool when the step comes up, not before:

- `references/first-use.md` — when the project has no `_tickets/workers.md` yet, or a provider is not connected.
- `references/memory.md` — when you record a lesson, update the steward's memory, or create, approve or revise a project skill.
- `references/ledger.md` — the full workflow ledger rules: every state, decision, amendment, budget, recovery and closing step. Read it before your first `wf decide` in this session, and whenever a `wf` command refuses a move (exit 2, 3 or 4).

## Running commands in this harness

- **No shell state survives between `bash` calls.** Start every call with `cd <project root> &&`, and put environment variables on the same line, e.g. `cd <root> && HYDRA_POD_COMMIT_TRAILER='<line>' hydra-pod-dispatch accept T3`.
- **Long steps run as background jobs.** `hydra-pod-dispatch build` and `review`, and every `opencode run`, take minutes. Start them with `run_in_background: true`, then wait with `job_output` (or for the completion notice). A foreground call that outlives the bash timeout (60 s by default) also turns into a job; collect it with `job_output`, do not start it again. Never report a result before the job has finished and you have read its receipt or report.
- **Sandbox.** The default file policy is `workspace-write`: only the project folder is writable. `hydra-pod-dispatch`, `hydra-pod-connect`, `opencode` and `zcode` also write outside it (their state, cache and log directories under `~/.local`, `~/.cache`, `~/.config`). When such a call reports `[sandbox: file access denied ...]`, retry the exact same command once with `sandbox_permissions: danger-full-access` and a one-sentence justification; the user approves it. If the user prefers, they can switch this session to full access in the UI instead. Never widen access for any other command without a real denial.
- Every `opencode run` you start must have stdin closed (`< /dev/null`).

## Workflow ledger (essentials; full rules in `references/ledger.md`)

- **A task of several tickets is a plan:** write all its tickets, then `hydra-pod-dsh plan approve` it before any build (how: `references/ledger.md`, Plans).
- Before the first dispatch: `hydra-pod-dsh policy`, then `hydra-pod-dsh pick executor --complexity <S|M|L> --wf WF-<T>` (the best available builder of the pool, skipping any whose subscription window is exhausted; it records the choice). Exit 3 = no compliant or available agent: stop and tell the user.
- Before each review: `hydra-pod-dsh wf reviewers WF-<T>` lists the review stages this ticket needs (a later stage runs on high risk, or when you overruled the reviewer on a serious point, per the active profile) with the command for each. Run every stage marked RUN; exit 3 = a needed stage has no available agent: tell the user.
- `hydra-pod-dsh wf start <T> --objective '<one line>' --by '<your model> (DeepSeek Harness)'`, then `wf advance WF-<T> <STATE>` at each step: `PLANNING` → `PLAN_READY` → `ASSIGNING` → `EXECUTING` → `TESTING` → `REVIEWING` → `VERIFYING`.
- Decide with `wf decide WF-<T> APPROVE|REWORK|RE_REVIEW|REPLAN|ESCALATE|ABORT --reason '<why>'`. No finding may stay unverified when you decide. A limit or budget block (exit 3, `BLOCKED`) means stop and ask the user; never work around it.
- After each build and review: `wf resources WF-<T>`, and one line to the user with the stage's model, work tokens and window share (`wf stages WF-<T>`).
- Close: `hydra-pod-dispatch close`, `wf advance WF-<T> DONE`, `wf check` (must say `consistent`), `wf report WF-<T> --write`, then commit `_receipts/ledger.jsonl` and the report yourself.
- The user's `/hydra-pod-control` and `wf human` overrides are theirs; never issue them yourself.

## Spend your context where judgment is needed

You are the most expensive model in the pod. Let deterministic tools do the reading:

- **After a build, run `hydra-pod-dsh wf diffsum WF-<T>` first** (files, line counts, symbols added/removed, files outside `allowed_files`). Read the raw diff (`git diff <base> <head> -- <file>`) only for the files it lists under `read in full`, files out of scope, and files a finding points at. Read everything when the ticket is high risk or the summary leaves you unsure. Never accept a change you have not judged.
- **After a context compaction, or when you lose track, run `hydra-pod-dsh wf brief WF-<T>`** instead of re-reading the ledger, tickets and reports: it gives the state, counters, unverified findings, recent events and the moves allowed next.
- Prefer one-line command outputs; ask for `--json` only when you need a field.

## Mechanical steps

Mechanical steps go through `hydra-pod-dispatch` from the project root. Set `HYDRA_POD_COMMIT_TRAILER` on each call that commits (`accept`, `close`) to a line naming this harness and model, e.g. `Managed-By: DeepSeek Harness (<model id>)`.

- Write each ticket from `_tickets/TICKET.template.md` (structured header: allowed_files, acceptance, test_command, earlier_specs, reviewers). In its `from:` line write `DeepSeek Harness (<model id>)`. Also add, as plain `key: value` lines (never `- item` lists, which Hydra-Pod's parser rejects for these keys): `complexity: S|M|L`, `risk: low|medium|high`, and `read_hints: <comma-separated files the worker should read but not change>`.
- **Context pack.** End the ticket body with the line `Context pack: _receipts/<ticket>.context.md (worker: read it before the code; reviewer: answer its "For the reviewer" section)`, then run `hydra-pod-dsh wf pack WF-<T>` before `claim` (and again for every fix ticket, with `--task <T>b`). The pack gives both workers the files to change, the read hints, a repository map focused on the ticket and the relevant lessons, so they do not rediscover the code.
- `hydra-pod-dispatch claim <T>` (runs `preflight` first: accounts, Z.ai credits, clean tree; if it fails, tell the user exactly which `hydra-pod-connect connect <provider>` to run in a separate terminal and stop) → `hydra-pod-dispatch build <T>` (background job) → `wf diffsum` and the reads it calls for → `hydra-pod-dispatch accept <T>` → `hydra-pod-dispatch review <T> [--reviewer zcode]` (background job) → `hydra-pod-dispatch probes <T> --run` → validate every finding and append Manager verdicts → a fix ticket (same header) or `hydra-pod-dispatch close <T>`.
- **GLM-5.3's conclusions (`RC-n`) and suggestions (`RS-n`).** The pack asks the reviewer for them after its findings. Weigh each one on its merits and answer it by id in your Manager verdicts section: `RC-1: ACCEPTED|REJECTED|NEEDS-EVIDENCE — <one-line reason>` and `RS-1: ADOPT-NOW|BACKLOG|REJECTED — <reason>`. An accepted conclusion changes the plan (`wf amend`, a fix ticket, or REPLAN); an adopted suggestion becomes a ticket now; a backlog one goes into your closing summary. `wf decide APPROVE` is refused while any finding, conclusion or suggestion has no verdict.
- **Memory.** Read `.hydra/steward/core.md` at the start of a session. After closing a ticket, decide whether a lesson, a steward rule or a project skill should be recorded: how, in `references/memory.md`.
- `hydra-pod-dispatch status` shows every ticket with its cost totals and writes `_tickets/STATE.md`; `hydra-pod-dispatch costs [<T>]` shows the per-run cost log. Report the cost of each ticket to the user when you close it.
- `hydra-pod-dispatch` only automates checks; the judgment steps (tickets, diff reading, verdicts, fix tickets) stay yours.
- Review only through `zai-coding-plan/glm-5.3` (or ZCode): never `opencode-go/glm-5.3` (bills OpenCode Go) or `zai/glm-5.3` (pay-as-you-go), and never `pi` for Z.ai.
- Accounts: `hydra-pod-connect status` before dispatching (no quota spent); never authenticate yourself or read credentials (`references/first-use.md`).
