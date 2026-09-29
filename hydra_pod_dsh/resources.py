"""Resource attribution (architecture §16, rev 2): Hydra-Pod's per-run cost log
(`_receipts/<ticket>.costs.jsonl`, written by hydra-pod-dispatch) is the source of
truth for external workers; this module mirrors each run into the ledger once,
attributed to workflow / task / role / model / billing route, and aggregates it.
"""

import datetime as dt
import hashlib
import json
from pathlib import Path

from . import tokens, workflow

ROLE = {"build": "executor", "review": "reviewer"}
BILLING = {"opencode-go": "subscription/opencode-go",
           "opencode/zai-coding-plan": "subscription/zai-lite",
           "zcode-lite": "subscription/zai-lite"}
KEYS = ("seconds", "tool_calls", "list_cost_usd", "zai_credits")


def cost_files(project: Path, task: str) -> list[Path]:
    """`<task>.costs.jsonl`, or `<task>-<slug>.costs.jsonl` when the ticket id has a slug."""
    rec = Path(project) / "_receipts"
    exact = rec / f"{task}.costs.jsonl"
    return [exact] if exact.exists() else sorted(rec.glob(f"{task}-*.costs.jsonl"))


def run_time(run: dict) -> float | None:
    """Epoch seconds of a cost-log entry's `at` (ISO 8601, written when the run ended)."""
    try:
        return dt.datetime.fromisoformat(str(run.get("at"))).timestamp()
    except ValueError:
        return None


def run_key(line: str) -> str:
    return hashlib.sha256(line.strip().encode()).hexdigest()[:16]


def sync(project, wid: str) -> int:
    """Mirror new cost-log runs of every task of the workflow; returns how many were added."""
    w = workflow.get(project, wid)
    seen = {e["payload"].get("run_key") for e in workflow.timeline(project, wid)
            if e["type"] == "hydra/resource-usage"}
    added = 0
    for task in w.tasks:
        for f in cost_files(project, task):
            for line in f.read_text().splitlines():
                if not line.strip():
                    continue
                key = run_key(line)
                if key in seen:
                    continue
                try:
                    run = json.loads(line)
                except ValueError:
                    continue
                provider = run.get("provider")
                workflow.record(project, wid, "hydra/resource-usage",
                                actor={"kind": "worker", "name": f"{provider}:{run.get('model')}"},
                                payload={"run_key": key, "task_id": task, "source": f.name,
                                         "phase": run.get("phase"), "role": ROLE.get(run.get("phase"), run.get("phase")),
                                         "provider": provider, "model": run.get("model"),
                                         "billing": BILLING.get(provider, "unknown"), "exit": run.get("exit"),
                                         "tokens": tokens.normalize(run.get("tokens")),
                                         "run_at": run_time(run),
                                         **{k: run.get(k) for k in KEYS}})
                seen.add(key)
                added += 1
    return added


def summary(project, wid: str) -> dict:
    """Totals per role/model/billing route, plus the workflow total. Unknown stays None."""
    rows: dict[tuple, dict] = {}
    for e in workflow.timeline(project, wid):
        if e["type"] != "hydra/resource-usage":
            continue
        p = e["payload"]
        key = (p.get("role"), p.get("model"), p.get("billing"))
        r = rows.setdefault(key, {"role": key[0], "model": key[1], "billing": key[2], "runs": 0,
                                  "tokens": tokens.zero(), **{k: None for k in KEYS}})
        r["runs"] += 1
        r["tokens"] = tokens.add(r["tokens"], tokens.normalize(p.get("tokens")))
        for k in KEYS:
            if isinstance(p.get(k), (int, float)):
                r[k] = round((r[k] or 0) + p[k], 6)
    total = {"runs": sum(r["runs"] for r in rows.values()), "tokens": tokens.zero()}
    for r in rows.values():
        r["work_tokens"] = tokens.work(r["tokens"])
        total["tokens"] = tokens.add(total["tokens"], r["tokens"])
    total["work_tokens"] = tokens.work(total["tokens"])
    for k in KEYS:
        vals = [r[k] for r in rows.values() if r[k] is not None]
        total[k] = round(sum(vals), 6) if vals else None
    return {"workflow_id": wid, "by_route": list(rows.values()), "total": total}
