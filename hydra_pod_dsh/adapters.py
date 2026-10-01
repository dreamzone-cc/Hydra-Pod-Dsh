# SPDX-License-Identifier: AGPL-3.0-or-later
"""Worker adapters (technical paper §5.1, phase 3): one per runtime.

An adapter tells the Router three things about an agent on its runtime:
whether it can run here at all (`available`), how much of its subscription
window is used (`quota`), and how the manager dispatches it (`dispatch`).
It never runs a worker itself: builds and reviews still go through
`hydra-pod-dispatch`, which owns preflight, the watchdog, receipts and the
cost log. A new runtime is one subclass plus `register()`; the contract test
in tests/test_phase3.py checks every registered adapter the same way.
"""

import shutil

from . import usage

# A window at or above this share is treated as exhausted. The OpenCode Go figure
# is an estimate (no usage API), so it gets a wider safety margin.
EXHAUSTED = {"estimate": 85.0, "official": 95.0}


class Adapter:
    runtime = ""
    binary: str | None = None   # executable that must be on PATH, if any

    def available(self, agent: dict) -> tuple[bool, str]:
        if self.binary and not shutil.which(self.binary):
            return False, f"{self.binary} not on PATH"
        return True, ""

    def quota(self, agent: dict, now: float | None = None) -> dict:
        """{"percent": highest window share or None, "source": estimate|official|none, "exhausted": bool}."""
        return {"percent": None, "source": "none", "exhausted": False}

    def dispatch(self, agent: dict, role: str, task: str) -> dict:
        raise NotImplementedError


def window_quota(u: dict) -> dict:
    """Reduce a usage.py report to its fullest window."""
    pcts = [w["percent"] for w in u.get("windows", []) if w.get("percent") is not None]
    source = u.get("source") or "official"
    pct = max(pcts) if pcts else None
    return {"percent": pct, "source": source, "error": u.get("error"),
            "exhausted": pct is not None and pct >= EXHAUSTED.get(source, 95.0)}


class OpencodeAdapter(Adapter):
    runtime, binary = "opencode", "opencode"

    def quota(self, agent, now=None):
        if agent["billing"] == "subscription/opencode-go":
            return window_quota(usage.go_usage(agent["model"], now))
        if agent["billing"] == "subscription/zai-lite":
            return window_quota(usage.zai_usage(now))
        return super().quota(agent, now)

    def dispatch(self, agent, role, task):
        if role == "advisor":
            return {"kind": "shell", "background": True,
                    "command": f"hydra-pod-dsh wf consult WF-{task} ask --question-file <file> --advisor {agent.get('name', '<agent>')}"}
        if role == "reviewer" and not agent.get("model", "").startswith("zai-coding-plan/"):
            # hydra-pod-dispatch reviews only with GLM-5.3; other models run through hydra-pod-dsh
            return {"kind": "shell", "background": True,
                    "command": f"hydra-pod-dsh run review {task} --agent {agent.get('name', '<agent>')}"}
        verb = "build" if role in ("executor", "tester") else "review"
        return {"kind": "shell", "command": f"hydra-pod-dispatch {verb} {task}", "background": True}


class ZcodeAdapter(Adapter):
    runtime, binary = "zcode", "hydra-pod-connect"

    def quota(self, agent, now=None):
        return window_quota(usage.zai_usage(now))

    def dispatch(self, agent, role, task):
        if role != "reviewer":
            raise ValueError("zcode only reviews")
        return {"kind": "shell", "command": f"hydra-pod-dispatch review {task} --reviewer zcode", "background": True}


class ClaudeCliAdapter(Adapter):
    """The Claude Code CLI on the user's Claude subscription (its supported client), read-only.

    The subscription has no usage API, so its window is unknown (never guessed);
    `claude -p` itself refuses when the plan's limit is reached."""
    runtime, binary = "claude-cli", "claude"

    def dispatch(self, agent, role, task):
        name = agent.get("name", "<agent>")
        if role == "advisor":
            return {"kind": "shell", "background": True,
                    "command": f"hydra-pod-dsh wf consult WF-{task} ask --question-file <file> --advisor {name}"}
        if role not in ("reviewer", "security"):
            raise ValueError("the Claude Code CLI agents here are read-only: reviewer, security or advisor")
        return {"kind": "shell", "background": True, "command": f"hydra-pod-dsh run review {task} --agent {name}"}


class DshSubagentAdapter(Adapter):
    """An in-process DSH child: the manager starts it with DSH's own `subagent` tool."""
    runtime = "dsh-subagent"

    def dispatch(self, agent, role, task):
        return {"kind": "dsh-tool", "background": True,
                "command": f"DSH `subagent` tool, model {agent['model']}, prompt: the ticket "
                           f"_tickets/doing/{task}*.md and its context pack _receipts/{task}*.context.md"}


ADAPTERS: dict[str, Adapter] = {}


def register(adapter: Adapter) -> None:
    if not adapter.runtime:
        raise ValueError("an adapter needs a runtime name")
    ADAPTERS[adapter.runtime] = adapter


def for_runtime(runtime: str) -> Adapter | None:
    return ADAPTERS.get(runtime)


for _a in (OpencodeAdapter(), ZcodeAdapter(), ClaudeCliAdapter(), DshSubagentAdapter()):
    register(_a)
