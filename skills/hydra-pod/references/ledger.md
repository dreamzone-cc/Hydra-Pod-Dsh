# Workflow ledger: full rules

Every task is a workflow recorded in `<project>/_receipts/ledger.jsonl`, an append-only, versioned log that is the source of truth for state (commit it with the tickets). Drive it with `hydra-pod-dsh wf` from the project root; each call also updates the live stage the `dsh-hydra-pod` plugin shows above the composer. These calls write only inside the project and `~/.cache/hydra-pod-dsh`, and need no sandbox escalation.

## Start and moves

- Before the first dispatch, run `hydra-pod-dsh policy` (the agent registry passes billing, read-only and role-separation checks) and `hydra-pod-dsh route executor` / `route reviewer` (who may do the work, on which billing route). Exit status 3 means no compliant agent exists: stop and tell the user.
- Start: `hydra-pod-dsh wf start <T> --objective '<one line>' --by '<your model> (DeepSeek Harness)'` → workflow `WF-<T>`. If the user gave a budget, add `--budget-cost USD`, `--budget-credits N` (Z.ai), `--budget-minutes M` and/or `--budget-manager-tokens N`.
- Move forward with `hydra-pod-dsh wf advance WF-<T> <STATE>` at each step: `PLANNING` → `PLAN_READY` (ticket written) → `ASSIGNING` (claim) → `EXECUTING` (build started) → `TESTING` (your acceptance rerun) → `REVIEWING` (review started) → `VERIFYING` (you validate findings).
- Illegal moves exit with status 2 and say what is allowed. Treat that as a signal to re-read the state (`hydra-pod-dsh wf brief WF-<T>`), not something to retry blindly.

## Plans

A task of several tickets is a plan. Write all its tickets first, then approve the plan once, before any build: `hydra-pod-dsh plan approve <name> --ticket T1 --ticket T2:T1 --reason '<why this split>'` (`T2:T1` = T2 depends on T1). `hydra-pod-dsh plan show <name>` lists the waves and what may start now. `wf advance … EXECUTING` is refused for a ticket whose dependencies are not DONE. Builds still run one at a time (Hydra-Pod's preflight needs a clean tree). To change the split, approve the plan again under the same name.

## Decisions

Decide as Verifier with a reason: `hydra-pod-dsh wf decide WF-<T> APPROVE|REWORK|RE_REVIEW|REPLAN|ESCALATE|ABORT --reason '<why>'`.

- REWORK: add `--task <T>b` for the fix ticket.
- RE_REVIEW: add `--directive '{"scope":[...],"findings_to_recheck":[...]}'`.
- A failed acceptance in `TESTING` is `REWORK`, unless the ticket itself is wrong (for example a wrong expected value). Then reproduce the correct value yourself, fix the ticket, record it with `hydra-pod-dsh wf amend WF-<T> --field acceptance --before '…' --after '…' --reason '…'`, and re-run `hydra-pod-dispatch accept`. Never amend to make a real defect pass.
- When a policy limit is reached, the tool turns the decision into ESCALATE (`BLOCKED`). Then stop and ask the user; never work around it.

## Closing

- After `APPROVE`: `hydra-pod-dsh wf advance WF-<T> DONE`, after `hydra-pod-dispatch close`. Then `hydra-pod-dsh wf check` must say `consistent`, and `hydra-pod-dsh wf report WF-<T> --write` saves the closing report.
- `hydra-pod-dispatch close` does not commit the ledger or the report, so commit `_receipts/ledger.jsonl` and `_receipts/WF-<T>.report.md` (plus any remaining `_receipts/<T>*` logs) yourself.
- Give the user its summary: decision, costs by billing route, findings.
- Run `hydra-pod-dsh wf check` after every `claim` and `close` too. Exit 4 means the ledger and `_tickets/` disagree: find out which side is wrong before going on.

## Costs, usage and budgets

- After every build or review: `hydra-pod-dsh wf resources WF-<T>` mirrors the cost log into the ledger and prints totals per role, model and billing route. Report them when you close.
- `hydra-pod-dsh wf stages WF-<T>` shows each stage with the model that worked in it: its tokens (work = input + output + reasoning, with cache shown apart; never report cache as work), its cost or Z.ai credits, and the share of the 5 h and weekly windows it used, with the time each window resets. After each build and review, tell the user the stage's model, work tokens and window share in one line.
- Budgets are enforced when you advance to `EXECUTING` or `REVIEWING`. At 80% you get a warning on stderr: pass it on to the user. At 100% the move is refused with exit status 3, and the workflow is `BLOCKED` by policy. Stop and ask the user; do not retry.
- Your own usage: `hydra-pod-dsh wf manager WF-<T>` reads this session's usage from the DSH logs, checks its billing route and shows your cache hit ratio. Exit status 3 means the manager ran on a route outside policy. Report that to the user.
- `hydra-pod-dsh status` shows who is working, the workflow and the subscription windows. Run it before dispatching a build or a review and include its usage lines in your report. Warn the user when a window the next step draws on is at 80% or more. OpenCode Go figures are an estimate (the plan has no usage API); Z.ai figures are official.

## Findings and recovery

- Findings: after a review, `hydra-pod-dsh wf findings WF-<T>` lists the reviewer's findings, conclusions (`RC-n`) and suggestions (`RS-n`) with your verdicts. None may stay `unverified`: `wf decide APPROVE` is refused (exit 2) until every one is answered.
- A worker that died: `hydra-pod-dsh wf health WF-<T>` says `stalled` when a build or review has no process and no receipt/report. Then `hydra-pod-dsh wf recover WF-<T> --reason '…'` returns to `ASSIGNING` (or `TESTING` for a review) so the step can be dispatched again. It refuses when the step is not stalled.

## The user's controls

The user can override with `/hydra-pod-control WF-<T> cancel|pause|resume|approve|reject <reason>` (a DSH command that sends no model message) or with `hydra-pod-dsh wf human …`. Do not issue those yourself. `/hydra-pod-status` and `/hydra-pod-timeline` are the user's read-only views.
