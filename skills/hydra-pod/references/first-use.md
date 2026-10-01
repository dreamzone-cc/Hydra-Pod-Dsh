# First use in a project, and accounts

## First use

If this project has no `_tickets/workers.md` yet, run the `opus-manager` skill's first-use setup. If `~/Hydra-Pod/scripts/init-project.sh` exists, offer to run it first (it installs the verified reviewer agent, the dispatch watchdog and a workers.md draft). Propose this adopted baseline, but still confirm each choice with the user as the skill requires:

- Builder: `opencode run --standalone --format json -m opencode-go/deepseek-v4.1-flash --auto`
- Reviewer: `opencode run --standalone --format json -m zai-coding-plan/glm-5.3 --agent reviewer` (Z.ai Lite via the Z.AI Coding Plan provider; the project-level `reviewer` agent enforces read-only)
- Wrap every opencode dispatch in `_tickets/run-watchdog.sh`.
- Build every review prompt from `~/Hydra-Pod/prompts/reviewer.md` (or `reviewer-zcode.md`) with its fixed context block filled in: current time, exact test command, earlier specs. Never drop that block.
- Optional alternative reviewer (only if the user chooses it): ZCode, via `~/Hydra-Pod/scripts/zcode-review-prep.sh <ticket> <base> <head>` then `hydra-pod-connect review zcode-lite --prompt-file _receipts/<ticket>.zcode-prompt.txt --out _receipts/<ticket>.review.md`. It is read-only (Read/Glob/Grep only, MCP off) and cannot run git or tests.
- Never use `opencode-go/glm-5.3` (bills OpenCode Go) or `zai/glm-5.3` (pay-as-you-go) for review, and never use `pi` for Z.ai.

## Accounts

Before dispatching, check accounts without spending quota: `hydra-pod-connect status` (hydra-pod-dispatch also stops a build or review if the provider is not connected). If a provider needed for this task is not Connected, ask the user to run `hydra-pod-connect connect <provider>` in their own terminal (browser login; zcode-lite must be approved within about 5 minutes) instead of trying to authenticate yourself. Never read or copy credentials.

Full framework docs: `~/Hydra-Pod/README.md`. Harness-specific notes: `~/Hydra-Pod/Hydra-Pod-Dsh/README.md`.
