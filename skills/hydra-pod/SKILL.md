---
name: hydra-pod
description: Run a task in Hydra-Pod ticket mode inside DeepSeek Harness (this session manages; DeepSeek builds; GLM-5.3 reviews). Invoke as /hydra-pod followed by the task.
disable-model-invocation: true
user-invocable: true
---

# Hydra-Pod in DeepSeek Harness

Use the `opus-manager` skill for this task (load it with the `skill` tool), in managed mode: you are the manager. You plan, dispatch, accept and verify; you do not write the implementation yourself. Wherever that skill or the Hydra-Pod docs say "Opus" or "Claude Code", read "the manager": the model running this DeepSeek Harness session.

Task: the rest of the user's message that invoked `/hydra-pod`. If it contains no task, ask for one before doing anything else.

Paths: `~/Hydra-Pod` in these instructions means the Hydra-Pod checkout — `$HYDRA_POD_HOME` when that variable is set, else `~/Hydra-Pod`.

Governing flow: the manager plans tickets with acceptance criteria → DeepSeek implements and adds tests → tests → GLM-5.3 reviews read-only → the manager runs the reviewer's `PROBE:` lines with `~/Hydra-Pod/scripts/probe-run.py <report> --ref <head> [--run]` and validates every finding against the code (valid → explicit fix ticket for DeepSeek; invalid → one-line rejection; never forward a reviewer's proposed fix unchecked) → DeepSeek fixes within scope → tests → the manager's final verification → done. Keep roles strictly separate. The manager must not be the builder or the reviewer model: if this session runs the same model as a worker, say so and let the user decide.

## Running commands in this harness

These rules replace Claude Code habits that do not hold here:

- **No shell state survives between `bash` calls.** Start every call with `cd <project root> &&`, and put environment variables on the same line, e.g. `cd <root> && HYDRA_POD_COMMIT_TRAILER='<line>' hydra-pod-dispatch accept T3`.
- **Long steps run as background jobs.** `hydra-pod-dispatch build` and `review`, and every `opencode run`, take minutes. Start them with `run_in_background: true`, then wait with `job_output` (or for the completion notice). A foreground call that outlives the bash timeout (60 s by default) also turns into a job; collect it with `job_output`, do not start it again. Never report a result before the job has finished and you have read its receipt or report.
- **Sandbox.** The default file policy is `workspace-write`: only the project folder is writable. `hydra-pod-dispatch`, `hydra-pod-connect`, `opencode` and `zcode` also write outside it (their state, cache and log directories under `~/.local`, `~/.cache`, `~/.config`). When such a call reports `[sandbox: file access denied ...]`, retry the exact same command once with `sandbox_permissions: danger-full-access` and a one-sentence justification; the user approves it. If the user prefers, they can switch this session to full access in the UI instead. Never widen access for any other command without a real denial.
- Every `opencode run` you start must have stdin closed (`< /dev/null`).

## Workflow ledger, live stage and usage

Every task is a workflow recorded in `<project>/_receipts/ledger.jsonl`, an append-only, versioned log that is the source of truth for state (commit it with the tickets). Drive it with `hydra-pod-dsh wf` from the project root; each call also updates the live stage the `dsh-hydra-pod` plugin shows above the composer. These calls write only inside the project and `~/.cache/hydra-pod-dsh`, and need no sandbox escalation.

- Before the first dispatch, run `hydra-pod-dsh policy` (the agent registry passes billing, read-only and role-separation checks) and `hydra-pod-dsh route executor` / `route reviewer` (who may do the work, on which billing route). Exit status 3 means no compliant agent exists: stop and tell the user.
- Start: `hydra-pod-dsh wf start <T> --objective '<one line>' --by '<your model> (DeepSeek Harness)'` → workflow `WF-<T>`. If the user gave a budget, add `--budget-cost USD`, `--budget-credits N` (Z.ai), `--budget-minutes M` and/or `--budget-manager-tokens N`.
- Move forward with `hydra-pod-dsh wf advance WF-<T> <STATE>` at each step: `PLANNING` → `PLAN_READY` (ticket written) → `ASSIGNING` (claim) → `EXECUTING` (build started) → `TESTING` (your acceptance rerun) → `REVIEWING` (review started) → `VERIFYING` (you validate findings).
- Decide as Verifier with a reason: `hydra-pod-dsh wf decide WF-<T> APPROVE|REWORK|RE_REVIEW|REPLAN|ESCALATE|ABORT --reason '<why>'`.
  - REWORK: add `--task <T>b` for the fix ticket.
  - RE_REVIEW: add `--directive '{"scope":[...],"findings_to_recheck":[...]}'`.
  - A failed acceptance in `TESTING` is `REWORK`, unless the ticket itself is wrong (for example a wrong expected value). Then reproduce the correct value yourself, fix the ticket, record it with `hydra-pod-dsh wf amend WF-<T> --field acceptance --before '…' --after '…' --reason '…'`, and re-run `hydra-pod-dispatch accept`. Never amend to make a real defect pass.
  - When a policy limit is reached, the tool turns the decision into ESCALATE (`BLOCKED`). Then stop and ask the user; never work around it.
- After `APPROVE`: `hydra-pod-dsh wf advance WF-<T> DONE`, after `hydra-pod-dispatch close`. Then `hydra-pod-dsh wf check` must say `consistent`, and `hydra-pod-dsh wf report WF-<T> --write` saves the closing report. `hydra-pod-dispatch close` does not commit the ledger or the report, so commit `_receipts/ledger.jsonl` and `_receipts/WF-<T>.report.md` (plus any remaining `_receipts/<T>*` logs) yourself. Give the user its summary: decision, costs by billing route, findings.
- Run `hydra-pod-dsh wf check` after every `claim` and `close` too. Exit 4 means the ledger and `_tickets/` disagree: find out which side is wrong before going on.
- After every build or review: `hydra-pod-dsh wf resources WF-<T>` mirrors the cost log into the ledger and prints totals per role, model and billing route. Report them when you close.
- `hydra-pod-dsh wf stages WF-<T>` shows each stage with the model that worked in it: its tokens (work = input + output + reasoning, with cache shown apart; never report cache as work), its cost or Z.ai credits, and the share of the 5 h and weekly windows it used, with the time each window resets. After each build and review, tell the user the stage's model, work tokens and window share in one line.
- Budgets are enforced when you advance to `EXECUTING` or `REVIEWING`. At 80% you get a warning on stderr: pass it on to the user. At 100% the move is refused with exit status 3, and the workflow is `BLOCKED` by policy. Stop and ask the user; do not retry.
- Findings: after a review, `hydra-pod-dsh wf findings WF-<T>` lists the reviewer's findings with your verdicts (valid, rejected or unverified). None may stay `unverified` when you decide.
- A worker that died: `hydra-pod-dsh wf health WF-<T>` says `stalled` when a build or review has no process and no receipt/report. Then `hydra-pod-dsh wf recover WF-<T> --reason '…'` returns to `ASSIGNING` (or `TESTING` for a review) so the step can be dispatched again. It refuses when the step is not stalled.
- Your own usage: `hydra-pod-dsh wf manager WF-<T>` reads this session's usage from the DSH logs and checks its billing route. Exit status 3 means the manager ran on a route outside policy (for example Claude Pro via OAuth). Report that to the user.
- `hydra-pod-dsh status` shows who is working, the workflow and the subscription windows. Run it before dispatching a build or a review and include its usage lines in your report. Warn the user when a window the next step draws on is at 80% or more. OpenCode Go figures are an estimate (the plan has no usage API); Z.ai figures are official.
- Illegal moves exit with status 2 and say what is allowed. Treat that as a signal to re-read the state (`hydra-pod-dsh wf show`), not something to retry blindly.
- The user can override with `/hydra-pod-control WF-<T> cancel|pause|resume|approve|reject <reason>` (a DSH command that sends no model message) or with `hydra-pod-dsh wf human …`. Do not issue those yourself. `/hydra-pod-status` and `/hydra-pod-timeline` are the user's read-only views.

## Mechanical steps

Mechanical steps go through `hydra-pod-dispatch` from the project root. Set `HYDRA_POD_COMMIT_TRAILER` on each call that commits (`accept`, `close`) to a line naming this harness and model, e.g. `Managed-By: DeepSeek Harness (<model id>)`.

- Write each ticket from `_tickets/TICKET.template.md` (structured header: allowed_files, acceptance, test_command, earlier_specs, reviewers). In its `from:` line write `DeepSeek Harness (<model id>)`.
- `hydra-pod-dispatch claim <T>` (runs `hydra-pod-dispatch preflight <T>` first: accounts, Z.ai credits, clean tree; if it fails, tell the user exactly which `hydra-pod-connect connect <provider>` to run in a separate terminal and stop) → `hydra-pod-dispatch build <T>` (background job) → read the diff yourself → `hydra-pod-dispatch accept <T>` → `hydra-pod-dispatch review <T> [--reviewer zcode]` (background job) → `hydra-pod-dispatch probes <T> --run` → validate every finding and append Manager verdicts → a fix ticket (same header) or `hydra-pod-dispatch close <T>`.
- `hydra-pod-dispatch status` shows every ticket with its cost totals and writes `_tickets/STATE.md`; `hydra-pod-dispatch costs [<T>]` shows the per-run cost log. Report the cost of each ticket to the user when you close it.
- `hydra-pod-dispatch` only automates checks; the judgment steps (tickets, diff reading, verdicts, fix tickets) stay yours.

## Accounts

Before dispatching, check accounts without spending quota: `hydra-pod-connect status` (hydra-pod-dispatch also stops a build or review if the provider is not connected). If a provider needed for this task is not Connected, ask the user to run `hydra-pod-connect connect <provider>` in their own terminal (browser login; zcode-lite must be approved within about 5 minutes) instead of trying to authenticate yourself. Never read or copy credentials.

## First use in a project

If this project has no `_tickets/workers.md` yet, run the `opus-manager` skill's first-use setup. If `~/Hydra-Pod/scripts/init-project.sh` exists, offer to run it first (it installs the verified reviewer agent, the dispatch watchdog and a workers.md draft). Propose this adopted baseline, but still confirm each choice with the user as the skill requires:

- Builder: `opencode run --standalone --format json -m opencode-go/deepseek-v4.1-flash --auto`
- Reviewer: `opencode run --standalone --format json -m zai-coding-plan/glm-5.3 --agent reviewer` (Z.ai Lite via the Z.AI Coding Plan provider; the project-level `reviewer` agent enforces read-only)
- Wrap every opencode dispatch in `_tickets/run-watchdog.sh`.
- Build every review prompt from `~/Hydra-Pod/prompts/reviewer.md` (or `reviewer-zcode.md`) with its fixed context block filled in: current time, exact test command, earlier specs. Never drop that block.
- Optional alternative reviewer (only if the user chooses it): ZCode, via `~/Hydra-Pod/scripts/zcode-review-prep.sh <ticket> <base> <head>` then `hydra-pod-connect review zcode-lite --prompt-file _receipts/<ticket>.zcode-prompt.txt --out _receipts/<ticket>.review.md`. It is read-only (Read/Glob/Grep only, MCP off) and cannot run git or tests.
- Never use `opencode-go/glm-5.3` (bills OpenCode Go) or `zai/glm-5.3` (pay-as-you-go) for review, and never use `pi` for Z.ai.

Full framework docs: `~/Hydra-Pod/README.md`. Harness-specific notes: `~/Hydra-Pod/Hydra-Pod-Dsh/README.md`.
