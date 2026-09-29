# 1. Design

## Goal

Run the Hydra-Pod workflow with DeepSeek Harness (dsh) as the manager's host, instead of Claude Code. The workers and all of Hydra-Pod's mechanics (`hydra-pod-dispatch`, `hydra-pod-connect`, the ticket layout, receipts, the watchdog and the reviewer agent) stay as they are.

## Why this fits dsh without code changes

Findings from dsh `0.2.0-rc.1` (commit `4878cda`):

| Need | dsh mechanism | Source |
|---|---|---|
| A `/hydra-pod` command | a user-invocable skill: a `/name` token in a user message injects the skill body into that step | `packages/skill/tool-skill/README.md` |
| The `opus-manager` skill | the `SKILL.md` format is the same as Claude Code's; user roots `$DSH_HOME/skills` and `~/.agents/skills`, project roots `.dsh/skills` and `.agents/skills`; the roots are watched, so no restart is needed | `packages/skill/skill-filesystem/README.md` |
| Running `hydra-pod-dispatch` | the `bash` tool (a fresh `bash -c` per call) | `packages/shell/tool-bash/README.md` |
| Builds that take minutes | every command is a job: `run_in_background`, or a foreground call that outlives its timeout becomes a job; `job_output` waits, and completion notices arrive in the session | `packages/jobs/tool-jobs/README.md` |

So the integration is two skills and an installer, not a dsh plugin. A plugin would add a build step, a pnpm install into the profile, and the version gate we saw with third-party plugins.

## Constraints found, and how they are handled

1. **Frontmatter strictness.** dsh parses frontmatter with the `yaml` package and skips a skill whose frontmatter is invalid, logging only a host warning. Hydra-Pod's vendored `opus-manager` description contains an unquoted `: `. Handled by: `skills/opus-manager` (a loader with a quoted description), and `scripts/check-skill.mjs`, which uses dsh's own parser. A cleaner long-term fix is to quote that description in Hydra-Pod's vendored copy, but that is a change to a vendored MIT file and is left to the Hydra-Pod maintainer.
2. **Sandbox.** The default is `workspace-write` via bwrap: the host root is read-only, the workspace is writable, and `/tmp` is ephemeral. The dispatch tools write outside the workspace, so the skill tells the manager to escalate each denied call once, after a real denial, as the bash tool's rules require. The mode governs file effects only; network access is not restricted.
3. **60 s bash timeout.** Handled by background jobs (see above).
4. **No persistent shell.** Every command carries its own `cd` and environment variables.
5. **The receipt template path.** `hydra-pod-dispatch` hard-codes `~/.claude/skills/opus-manager/templates/receipt.md`. That works as long as Hydra-Pod's own `install.sh` has run. `install.sh` and `doctor.sh` here report it when it is missing.

## Workers as native dsh subagents (ACP): investigated, not adopted

dsh can run an external agent as a subagent over the Agent Client Protocol (`@deepseek-ai/dsh-subagent-acp`), and `opencode acp` exists. On 2026-09-28, with opencode 2.0.16, a probe (`initialize` + `session/new` only, no prompt, no quota) showed that this cannot carry Hydra-Pod's workers safely:

1. A new ACP session starts on an unrelated default model (`openrouter/perceptron/perceptron-mk1.5`). It ignores both `OPENCODE_CONFIG` and `OPENCODE_CONFIG_CONTENT`. dsh's ACP provider has no model setting, so the work would be billed to OpenRouter.
2. No `opencode-go/*` model is offered in the session (468 other models are), so the OpenCode Go builder is not reachable over ACP.
3. The only modes are `build` and `plan`. The project's read-only `reviewer` agent is not exposed, so the reviewer could not be kept read-only.
4. `opencode acp --standalone` (the variant that sees fresh logins) does not answer `initialize`.

ZCode has no ACP server at all (only its own `app-server` protocol). The workers therefore stay on `hydra-pod-dispatch`, and the plugin makes them visible instead. Revisit this if opencode's ACP server gains model and agent selection.

Using the subscriptions from dsh as model providers, without opencode, was not pursued. Both plans are sold for use through supported coding tools, and dsh is not on OpenCode Go's list of validated clients.

## Manager model

The manager is Claude Opus 5.5 through an Anthropic API key configured in dsh (decided 2026-09-28). The Claude Pro subscription stays with Anthropic's official clients. Rules carried over from Hydra-Pod:

- the manager is never the builder or the reviewer model;
- the manager does not write the implementation;
- the manager validates every review finding against the code before it becomes a fix ticket.


## Validation plan

The scripted half (real workers, scripted manager) ran as W1 and is recorded in `docs/04-validation-scripted-w1.md`. The remaining half — a real ticket with the model manager inside dsh — is the Phase 0.5 runbook in `docs/03-validation-w1.md`, and needs the operator: an Anthropic API key and the dsh UI approvals.
