# 3. Phase 0.5: validation run W1 (runbook)

Exit criterion (architecture §31): one real ticket is closed with the manager (Claude Opus 5.5) inside DSH, on the current integration, with every transition in the ledger and costs recorded. **Status: not yet run.** It needs the operator: an Anthropic API key, and a few approvals in the DSH UI.

## Before you start

1. **Pick the manager billing route.** Default: an Anthropic **API key** in DSH (Settings → Models → Add model provider → `anthropic`), then choose Claude Opus 5.5 in the model menu. Since the operator amendment of 2026-09-30, the Claude Pro OAuth bridge (`pi-anthropic` models, `oauth/claude-pro`) is also an allowed manager route, so the bridge plugin may stay installed if you prefer that path.
   *Note (2026-10-01):* in this machine's `web` profile, the provider named `anthropic` is **"Anthropic (Claude OAuth)"** (`apiKeyEnv: ANTHROPIC_OAUTH_TOKEN`), i.e. the Claude subscription, not an API key. `hydra-pod-dsh` now classifies a provider by its definition in the DSH profile, so `wf manager` reports this route as `oauth/claude-pro`, and its cost as part of the subscription rather than API spend.
2. **Check readiness** (no quota spent): `~/Hydra-Pod/Hydra-Pod-Dsh/scripts/doctor.sh`. Every line must be `ok`.
3. **Check the providers:** `hydra-pod-connect status` shows opencode-go and Z.ai as Connected. Z.ai's weekly window must have room: `hydra-pod-dsh status`.
4. **Start DSH from a clean test project**, e.g. a copy of the sandbox:
   `cp -r ~/om-sandbox ~/w1-sandbox && cd ~/w1-sandbox && git status` (must be clean), then `dsh web`.

## The run

In a new DSH session (workspace `~/w1-sandbox`, model Claude Opus 5.5):

```
/hydra-pod add a function count_vowels(s) with tests, as ticket T6
```

Watch for these, and note each one:

| Check | Expected |
|---|---|
| Skill injection | an **Instructions** card for `hydra-pod`; the manager then loads `opus-manager` and reads `~/Hydra-Pod/skill/opus-manager/SKILL.md` |
| Ledger | `hydra-pod-dsh wf start T6 …`, then one `wf advance` per step; the pill shows the state |
| Build | `hydra-pod-dispatch build T6` runs as a background job; the pill shows `Builder: … (OpenCode Go)` with the 5 h window |
| Review | the pill shows `Reviewer: … glm-5.3 (Z.ai Lite)`; the Z.ai window moves |
| Approvals | one approval per dispatch call under `workspace-write` |
| Decision | `wf decide WF-T6 APPROVE` (or `REWORK` with a fix ticket) with a reason |
| Costs | `wf resources WF-T6` lists executor and reviewer runs by billing route |

## Record the result

Append to this file, filling in each item:

- Date, DSH commit, and the manager model id.
- `hydra-pod-dsh wf timeline WF-T6` (paste it).
- `hydra-pod-dsh wf resources WF-T6` (paste it), plus the Anthropic API cost of the manager session, from the Anthropic console.
- The number of approvals, and every point where the manager left the flow.
- The verdict: pass or fail against the exit criterion.
