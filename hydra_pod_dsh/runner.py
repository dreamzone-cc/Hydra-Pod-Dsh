# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run a read-only agent that hydra-pod-dispatch cannot run (technical paper phase 5).

Hydra-Pod's dispatch knows two reviewers (GLM-5.3 through opencode, and ZCode).
The agents phase 5 adds (a second reviewer through the Claude Code CLI, and
advisors through the Claude Code CLI or OpenRouter via opencode) run here
instead, with the same outputs, so everything downstream already handles them:

- the report goes to `_receipts/<ticket>.review-<agent>.md` (findings.py reads
  every `<ticket>.review*.md`, so its findings, conclusions and suggestions
  join the verdict gate) or to `_receipts/<WF>.consult-<agent>.md`;
- one line per run goes to `_receipts/<ticket>.costs.jsonl`, the cost log
  resources.py mirrors into the ledger and budgets read.

Read-only is enforced by the runtime, never asked for: the Claude Code CLI gets
`--tools Read,Grep,Glob` (no other tool exists in the session), and opencode
runs the project's `reviewer` agent, whose permissions deny edits and shell.
The reviewer cannot run git, so the diff is put in its prompt.
"""

import json
import subprocess
import time
from pathlib import Path

from . import diffsum

TIMEOUT = 1200
MAX_DIFF_CHARS = 60_000
CLAUDE_TOOLS = "Read,Grep,Glob"
PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


class RunError(Exception):
    """The agent could not be started or produced no answer."""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def template(name: str) -> str:
    text = (PROMPTS / name).read_text()
    start = text.index("```text\n") + len("```text\n")
    return text[start:text.index("\n```", start)]


def review_prompt(project, task: str, agent_name: str, model: str) -> str:
    h = diffsum.ticket_header(project, task)
    base, head = h.get("base"), h.get("head")
    if not (base and head):
        raise RunError(f"{task} has no base/head: run hydra-pod-dispatch claim and accept first")
    diff = subprocess.run(["git", "-C", str(project), "diff", f"{base}..{head}"], capture_output=True,
                          text=True).stdout
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + f"\n[diff cut at {MAX_DIFF_CHARS} characters: Read the files for the rest]\n"
    ticket = diffsum.ticket_path(project, task)
    subs = {"<ticket>": ticket.stem, "<base>": base, "<head>": head, "<now>": now_iso(),
            "<reviewer>": f"{agent_name} / {model}", "<test command>": h.get("test_command") or "none",
            "<diff>": diff or "(empty diff)"}
    text = template("reviewer-readonly.md")
    for k, v in subs.items():
        text = text.replace(k, v)
    return text


def consult_prompt(question: str, context_files: list[str]) -> str:
    text = template("advisor.md")
    return (text.replace("<now>", now_iso()).replace("<question>", question.strip())
            .replace("<context files>", ", ".join(context_files) or "none"))


def _claude(project, prompt: str, model: str, raw: Path, max_budget: float | None) -> dict:
    cmd = ["claude", "-p", prompt, "--model", model, "--tools", CLAUDE_TOOLS, "--permission-mode", "dontAsk",
           "--output-format", "json", "--no-session-persistence", "--strict-mcp-config"]
    if max_budget:
        cmd += ["--max-budget-usd", str(max_budget)]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=project, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RunError(f"claude timed out after {TIMEOUT}s")
    except FileNotFoundError:
        raise RunError("claude (Claude Code CLI) is not on PATH")
    raw.write_text(r.stdout + (("\n" + r.stderr) if r.stderr else ""))
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise RunError(f"claude exit {r.returncode}: no JSON answer ({(r.stderr or r.stdout)[:200]!r})")
    u = out.get("usage") or {}
    return {"text": out.get("result") or "", "exit": r.returncode, "seconds": round(time.time() - t0),
            "tool_calls": None, "list_cost_usd": out.get("total_cost_usd"),
            "tokens": {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0),
                       "cache_read": u.get("cache_read_input_tokens", 0),
                       "cache_write": u.get("cache_creation_input_tokens", 0)},
            "error": out.get("is_error") and (out.get("result") or "error")}


def _opencode(project, prompt: str, model: str, raw: Path) -> dict:
    agent = Path(project) / ".opencode" / "agent" / "reviewer.md"
    conf = Path(project) / "opencode.json"
    if not agent.exists() and '"reviewer"' not in (conf.read_text() if conf.exists() else ""):
        raise RunError("this project has no opencode `reviewer` agent (run ~/Hydra-Pod/scripts/init-project.sh): "
                       "without it read-only cannot be enforced")
    cmd = ["opencode", "run", "--standalone", "--format", "json", "-m", model, "--agent", "reviewer", prompt]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=project, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RunError(f"opencode timed out after {TIMEOUT}s")
    except FileNotFoundError:
        raise RunError("opencode is not on PATH")
    raw.write_text(r.stdout)
    text, tools, cost = None, 0, 0.0
    tok = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0}
    for line in r.stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        part = e.get("part") or {}
        if e.get("type") == "text":
            text = part.get("text", text)
        elif e.get("type") == "tool_use":
            tools += 1
        elif e.get("type") == "step_finish":
            t = part.get("tokens") or {}
            for k in ("input", "output", "reasoning"):
                tok[k] += int(t.get(k) or 0)
            tok["cache_read"] += int((t.get("cache") or {}).get("read") or 0)
            cost += float(part.get("cost") or 0)
    return {"text": text or "", "exit": r.returncode, "seconds": round(time.time() - t0), "tool_calls": tools,
            "list_cost_usd": round(cost, 6), "tokens": tok, "error": None if text else "no text answer"}


PROVIDER = {"claude-cli": "claude-cli", "opencode": "opencode/openrouter"}


def run(project, agent_name: str, agent: dict, prompt: str, report: Path, cost_task: str, phase: str,
        max_budget: float | None = None) -> dict:
    """Run one read-only agent, write its report and one cost-log line. Returns the run's record."""
    rec = Path(project) / "_receipts"
    rec.mkdir(parents=True, exist_ok=True)
    raw = report.with_suffix(".raw.json" if agent["runtime"] == "claude-cli" else ".jsonl")
    if agent["runtime"] == "claude-cli":
        res = _claude(project, prompt, agent["model"], raw, max_budget)
    elif agent["runtime"] == "opencode":
        res = _opencode(project, prompt, agent["model"], raw)
    else:
        raise RunError(f"runtime {agent['runtime']} is not run by hydra-pod-dsh")
    entry = {"at": now_iso(), "phase": phase, "provider": PROVIDER[agent["runtime"]], "model": agent["model"],
             "agent": agent_name, "exit": res["exit"], "seconds": res["seconds"], "tool_calls": res["tool_calls"],
             "tokens": res["tokens"], "list_cost_usd": res["list_cost_usd"]}
    with open(rec / f"{cost_task}.costs.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")
    if res["error"] or not res["text"].strip():
        raise RunError(f"{agent_name} gave no answer ({res['error'] or 'empty'}); raw output in {raw.name}")
    report.write_text(res["text"].rstrip() + "\n")
    return {**entry, "report": str(report.relative_to(project))}
