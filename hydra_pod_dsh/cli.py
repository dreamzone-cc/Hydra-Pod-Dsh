"""hydra-pod-dsh: live stage and subscription usage for Hydra-Pod in DeepSeek Harness.

  hydra-pod-dsh status [--json]              who is working now, and that subscription's usage
  hydra-pod-dsh stage <ticket|-> <stage> [--project DIR] [--by NAME]
                                              record the manager's own stage
  hydra-pod-dsh stage --clear                 the manager is idle
  hydra-pod-dsh wf start <ticket> --objective TEXT [--max-rework N ...]
  hydra-pod-dsh wf advance <WF> <STATE> [--reason TEXT]
  hydra-pod-dsh wf decide <WF> <DECISION> --reason TEXT [--task T6b] [--directive JSON]
  hydra-pod-dsh wf human <WF> cancel|pause|resume|approve|reject --reason TEXT [--to STATE]
  hydra-pod-dsh wf show [<WF>] [--json]       folded state (rebuilt from the ledger)
  hydra-pod-dsh wf timeline [<WF>] [--limit N] [--json]
  hydra-pod-dsh wf resources <WF> [--json]    sync the cost log into the ledger, then totals
  hydra-pod-dsh wf budget|health|findings|manager <WF> [--json]
  hydra-pod-dsh wf recover <WF> --reason TEXT [--force]   retry a stalled worker step
  hydra-pod-dsh wf stages <WF> [--json]       per stage: model, tokens (work vs cache), subscription share and resets
  hydra-pod-dsh wf amend <WF> --field F --reason TEXT [--before X --after Y]   manager corrects its ticket
  hydra-pod-dsh wf check [<WF>] [--json]      ledger vs ticket folders (exit 4 on drift)
  hydra-pod-dsh wf report <WF> [--write]      closing report (Markdown); --write saves _receipts/<WF>.report.md
  hydra-pod-dsh route <role> [--capability C] [--model M] [--json]
  hydra-pod-dsh policy [--json]               registry violations (billing, read-only, role separation)
  exit status: 0 ok, 1 environment error (clean message), 2 refused move or bad input,
  3 blocked by budget/policy, 4 ledger/ticket drift
  (every wf command takes --project DIR, default: the current directory)

Spends no quota: worker detection reads /proc, OpenCode Go usage is estimated
from opencode's local database, and Z.ai usage comes from the official quota
endpoint (cached for a minute).
"""

import argparse
import json
import os
import sys
import time

from . import (budget, consistency, findings, health, ledger, live, manager_usage, report, resources, router, stages,
               tokens, usage, workflow)

SCHEMA_VERSION = 1  # of the JSON printed by `status --json` and `wf ... --json` (§13.3)
DEFAULT_BUILDER = "opencode-go/deepseek-v4.1-flash"
# The manager's live stage for each workflow state it owns; worker states are
# shown from their processes instead (live.py).
STAGE_FOR_STATE = {"PLANNING": "planning", "PLAN_READY": "planning", "ASSIGNING": "dispatching",
                   "TESTING": "accepting", "VERIFYING": "validating", "REWORK": "fixing",
                   "RE_REVIEW": "dispatching", "REPLAN": "planning", "APPROVED": "closing"}
MANAGER_STAGES = ("planning", "dispatching", "accepting", "validating", "fixing", "verifying", "closing")


def provider_of(worker: dict) -> str | None:
    """Which subscription a worker draws on: "opencode-go", "zai", or None."""
    model = worker.get("model") or ""
    if worker.get("tool") == "zcode" or model.startswith("zai-coding-plan/"):
        return "zai"
    if model.startswith("opencode-go/"):
        return "opencode-go"
    if worker.get("tool") == "hydra-pod-dispatch":
        # Dispatch started, the worker process not yet: the baseline routes.
        return "opencode-go" if worker["role"] == "builder" else "zai"
    return None


def worker_label(worker: dict) -> str:
    tool, model = worker.get("tool"), worker.get("model")
    if tool == "zcode":
        return "ZCode (GLM-5.3, Z.ai Lite)"
    if model and model.startswith("opencode-go/"):
        return f"opencode · {model.split('/', 1)[1]} (OpenCode Go)"
    if model and model.startswith("zai-coding-plan/"):
        return f"opencode · {model.split('/', 1)[1]} (Z.ai Lite)"
    return f"{tool} · {model}" if model else str(tool)


def status(now: float | None = None, workers=None, stage=None, go=None, zai=None) -> dict:
    now = time.time() if now is None else now
    workers = live.running_workers() if workers is None else workers
    stage = live.read_stage(now) if stage is None else stage
    zai = zai if zai is not None else usage.zai_usage(now)  # one fetch, shared with the stage table
    active = None
    if workers:
        w = sorted(workers, key=lambda x: x.get("since") or 0)[-1]
        active = {"source": "process", "role": w["role"], "who": worker_label(w), "model": w.get("model"),
                  "ticket": w.get("ticket") or (stage or {}).get("ticket"), "since": w.get("since"),
                  "project": w.get("cwd"), "subscription": provider_of(w)}
    elif stage:
        active = {"source": "manager", "role": "manager", "who": stage.get("by"), "stage": stage["stage"],
                  "ticket": stage.get("ticket"), "since": stage.get("at"), "project": stage.get("project"),
                  "subscription": None}
    project = (active or {}).get("project") or (stage or {}).get("project")
    workflows, events, ledger_error = [], [], None
    if project:
        try:
            workflows = [w.to_dict() for w in workflow.load(project).values() if w.state not in workflow.TERMINAL]
            for wd in workflows:
                wd["health"] = health.classify(project, wd["id"], workers)["health"]
                wd["findings"] = {k: v for k, v in findings.summary(project, wd["tasks"]).items() if k != "reports"}
                wd["resources"] = resources.summary(project, wd["id"])["total"]
                wd["drift"] = consistency.check(project, wd["id"])
                wd["stages"] = stages.build(project, wd["id"], now, zai=zai, sync=False)["stages"]
                if wd["budget"]:
                    wd["budget_check"] = budget.check(project, wd["id"])
            events = [e for e in workflow.timeline(project) if e["type"] != "hydra/resource-usage"][-8:]
        except (ledger.LedgerError, OSError, ValueError, KeyError, UnicodeDecodeError) as e:
            # one unreadable record (cost log, review file, DSH session…) must not kill the dashboard
            ledger_error = f"{e.__class__.__name__}: {e}"
    agents = []
    try:
        for name, ag in router.load()["agents"].items():
            busy = [x for x in workers if (x.get("tool") == "zcode" and ag["runtime"] == "zcode") or
                    (x.get("model") == ag["model"] and x.get("tool") == ag["runtime"])]
            agents.append({"name": name, "role": ag["role"], "runtime": ag["runtime"], "model": ag["model"],
                           "billing": ag["billing"],
                           "activity": "running" if busy else
                           ("running" if ag["role"] == "manager" and active and active["source"] == "manager" else "idle")})
    except (OSError, ValueError, KeyError):
        pass
    builder_model = next((w["model"] for w in workers if provider_of(w) == "opencode-go" and w.get("model")),
                         DEFAULT_BUILDER)
    return {
        "schema_version": SCHEMA_VERSION,
        "now": now,
        "project": project,
        "workflows": workflows,
        "timeline": events,
        "agents": agents,
        "ledger_error": ledger_error,
        "active": active,
        "workers": workers,
        "manager_stage": stage,
        "usage": {
            "opencode-go": go if go is not None else usage.go_usage(builder_model, now),
            "zai": zai,
        },
    }


def _left(ts: float | None, now: float) -> str:
    if not ts:
        return "-"
    s = max(0, int(ts - now))
    d, rem = divmod(s, 86400)
    h, m = divmod(rem // 60, 60)
    return f"{d}d {h}h" if d else f"{h}h {m:02d}m"


def render(st: dict) -> str:
    now, a = st["now"], st["active"]
    lines = ["Hydra-Pod"]
    if a is None:
        lines.append("  now: idle")
    elif a["source"] == "process":
        lines.append(f"  now: {a['role']} — {a['who']}" + (f" — {a['ticket']}" if a.get("ticket") else ""))
    else:
        lines.append(f"  now: manager ({a['stage']}) — {a['who']}" + (f" — {a['ticket']}" if a.get("ticket") else ""))
    for w in st.get("workflows") or []:
        lines.append(f"  workflow {w['id']}: {w['state']} (task {w['task_id']}, rework {w['rework_attempts']}"
                     f"/{w['policy']['max_rework_attempts']}, review {w['review_cycles']}"
                     f"/{w['policy']['max_review_cycles']})")
    if st.get("ledger_error"):
        lines.append(f"  ledger: {st['ledger_error']}")
    for key in ("opencode-go", "zai"):
        u = st["usage"][key]
        mark = " ◀ in use" if a and a.get("subscription") == key else ""
        tag = "estimate" if u.get("source") == "estimate" else "official"
        lines.append(f"  {u['provider']} ({u['model']}, {tag}){mark}")
        if u.get("error"):
            lines.append(f"    {u['error']}")
        for w in u["windows"]:
            amount = (f"${w['used_usd']:.2f} / ${w['limit_usd']:.2f}" if "used_usd" in w
                      else f"{w['used']} / {w['limit']}")
            pct = "-" if w["percent"] is None else f"{w['percent']}%"
            lines.append(f"    {w['window']:<6} {pct:>6}  {amount:<18} resets in {_left(w['resets_at'], now)}")
    return "\n".join(lines)


def _sync_stage(project: str, w: "workflow.Workflow") -> None:
    """Keep the live stage in step with the workflow the manager just moved."""
    if w.state in workflow.TERMINAL or w.state == "BLOCKED":
        live.clear_stage()
    elif w.state in STAGE_FOR_STATE:
        live.set_stage(w.task_id, STAGE_FOR_STATE[w.state], project)


def _wf(a) -> int:
    project = os.path.abspath(a.project)
    actor = {"kind": "manager", "name": a.by} if getattr(a, "by", None) else None
    if a.wf == "start":
        policy = {k: v for k, v in (("max_rework_attempts", a.max_rework), ("max_review_cycles", a.max_review),
                                    ("max_replans", a.max_replans)) if v is not None}
        bud = {k: v for k, v in (("max_cost_usd", a.budget_cost), ("max_zai_credits", a.budget_credits),
                                  ("max_runtime_minutes", a.budget_minutes),
                                  ("max_manager_tokens", a.budget_manager_tokens)) if v is not None}
        w = workflow.create(project, a.ticket, a.objective, policy, actor, bud)
    elif a.wf == "advance":
        if a.state in workflow.DISPATCH_STATES and workflow.get(project, a.workflow).budget:
            resources.sync(project, a.workflow)
            b = budget.check(project, a.workflow)
            over = [d for d in b["dimensions"] if d["level"] in ("block", "warn")]
            if b["level"] == "block":
                w = workflow.policy_block(project, a.workflow, "budget exhausted before dispatch",
                                          {"dimensions": over})
                _sync_stage(project, w)
                print(f"hydra-pod-dsh: {a.workflow} BLOCKED: budget exhausted: "
                      + ", ".join(f"{d['dimension']} {d['percent']}%" for d in over if d["level"] == "block"),
                      file=sys.stderr)
                return 3
            for d in over:
                print(f"hydra-pod-dsh: warning: {d['dimension']} at {d['percent']}% of budget", file=sys.stderr)
        w = workflow.advance(project, a.workflow, a.state, actor=actor, reason=a.reason)
    elif a.wf == "decide":
        directive = json.loads(a.directive) if a.directive else None
        w = workflow.decide(project, a.workflow, a.decision, a.reason, actor=actor, task_id=a.task,
                            directive=directive)
    elif a.wf == "human":
        w = workflow.human(project, a.workflow, a.action, a.reason, to=a.to)
    elif a.wf == "show":
        wfs = workflow.load(project)
        if a.workflow and a.workflow not in wfs:
            raise workflow.TransitionError(f"no workflow {a.workflow!r}")
        items = [wfs[a.workflow]] if a.workflow else list(wfs.values())
        if a.json:
            print(json.dumps({"schema_version": SCHEMA_VERSION, "workflows": [w.to_dict() for w in items]}))
        else:
            for w in items:
                print(f"{w.id}: {w.state} [{w.folder}] task={w.task_id} tasks={','.join(w.tasks)} "
                      f"rework={w.rework_attempts}/{w.policy['max_rework_attempts']} "
                      f"review={w.review_cycles}/{w.policy['max_review_cycles']} "
                      f"replan={w.replans}/{w.policy['max_replans']} — {w.objective}")
        return 0
    elif a.wf == "stages":
        out = stages.build(project, a.workflow)
        print(json.dumps({"schema_version": SCHEMA_VERSION, **out}) if a.json else stages.render(out))
        return 0
    elif a.wf == "amend":
        w = workflow.amend(project, a.workflow, a.reason, field=a.field, before=a.before, after=a.after, actor=actor)
        print(f"{w.id}: {w.state} — amended {a.field}")
        return 0
    elif a.wf == "check":
        probs = consistency.check(project, a.workflow)
        if a.json:
            print(json.dumps({"schema_version": SCHEMA_VERSION, "problems": probs}))
        else:
            print("\n".join(f"{p['workflow_id'] or '-'} {p['task_id']}: {p['kind']}: {p['detail']}" for p in probs)
                  or "consistent")
        return 4 if probs else 0
    elif a.wf == "report":
        if a.write:
            workflow.validate_workflow_id(a.workflow)  # the id becomes a file name
        text = report.build(project, a.workflow)
        if a.write:
            out = os.path.join(project, "_receipts", f"{a.workflow}.report.md")
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                f.write(text)
            print(out)
        else:
            print(text, end="")
        return 0
    elif a.wf == "recover":
        h = health.classify(project, a.workflow)
        if h["health"] != "stalled" and not a.force:
            raise workflow.TransitionError(f"{a.workflow} is {h['health']}, not stalled (use --force to override)")
        w = workflow.recover(project, a.workflow, a.reason, actor=actor)
    elif a.wf in ("budget", "health", "findings", "manager"):
        w = workflow.get(project, a.workflow)
        if a.wf == "budget":
            resources.sync(project, a.workflow)
            out = budget.check(project, a.workflow)
        elif a.wf == "health":
            out = health.classify(project, a.workflow)
        elif a.wf == "findings":
            out = findings.summary(project, w.tasks)
        else:
            out = manager_usage.usage_for(project, w.created_at,
                                          w.updated_at if w.state in workflow.TERMINAL else None)
            out["policy_violations"] = router.check_manager_routes(out["by_model"])
        if a.json:
            print(json.dumps({"schema_version": SCHEMA_VERSION, **out}))
        else:
            print(_human(a.wf, out))
        return 3 if (a.wf == "manager" and out["policy_violations"]) else 0
    elif a.wf == "resources":
        added = resources.sync(project, a.workflow)
        sm = resources.summary(project, a.workflow)
        if a.json:
            print(json.dumps({"schema_version": SCHEMA_VERSION, "synced": added, **sm}))
        else:
            print(f"{a.workflow}: {added} new run(s) synced")
            for r in sm["by_route"] + [dict(sm["total"], role="TOTAL", model="", billing="")]:
                cost = "-" if r["list_cost_usd"] is None else f"${r['list_cost_usd']:.4f}"
                cred = "-" if r["zai_credits"] is None else str(int(r["zai_credits"]))
                print(f"  {r['role']:<9} {r['model'] or '':<32} {r['billing'] or '':<26} runs={r['runs']:<3} "
                      f"cost={cost:<9} zai_credits={cred:<5} seconds={r['seconds'] or '-'}")
                print(f"            tokens {tokens.fmt(r['tokens'])} (work {tokens.human(r['work_tokens'])})")
        return 0
    else:  # timeline
        events = workflow.timeline(project, a.workflow, a.limit)
        if a.json:
            print(json.dumps({"schema_version": SCHEMA_VERSION, "events": events}))
        else:
            for e in events:
                p = e.get("payload") or {}
                move = f"{p.get('from')} -> {p.get('to')}" if "to" in p else ""
                what = p.get("decision") or p.get("action") or ""
                print(f"{time.strftime('%H:%M:%S', time.localtime(e['at']))}  {e['workflow_id']}  "
                      f"{e['type'].split('/', 1)[1]:<22} {what:<9} {move:<24} {e.get('reason') or ''}")
        return 0
    _sync_stage(project, w)
    note = (w.last_decision or {}) if a.wf == "decide" else {}
    print(f"{w.id}: {w.state} [{w.folder}]" + (f" — {note}" if note else ""))
    return 0


def _human(kind: str, out: dict) -> str:
    if kind == "budget":
        rows = [f"{out['workflow_id']}: budget {out['level']}"] + [
            f"  {d['dimension']:<22} {d['used']!s:>10} / {d['limit']:<10} {d['percent'] if d['percent'] is not None else '-'}% {d['level']}"
            for d in out["dimensions"]]
        return "\n".join(rows) if out["dimensions"] else f"{out['workflow_id']}: no budget set"
    if kind == "health":
        extra = f" (recover to {out['recover_to']})" if out.get("recover_to") else ""
        return f"{out['workflow_id']}: {out['state']} — {out['health']}{extra}"
    if kind == "findings":
        rows = [f"by severity: {out['by_severity']}", f"by status: {out['by_status']}"]
        for r in out["reports"]:
            rows.append(f"  {r['task_id']} {r['file']}: verdict {r['verdict'] or '-'}, "
                        f"{len(r['findings'])} finding(s), manager verified: {r['manager_verified']}")
            rows += [f"    [{f['severity']}] {f['status']:<10} {f['location']} — {f['problem'][:90]}" for f in r["findings"]]
        return "\n".join(rows)
    rows = [f"sessions: {', '.join(out['sessions']) or 'none'}"]
    for r in out["by_model"]:
        cost = "unknown" if r["cost_usd"] is None else f"${r['cost_usd']:.4f}"
        rows.append(f"  {r['provider']}/{r['model']} [{r.get('billing', '?')}] messages={r['messages']} "
                    f"{tokens.fmt(r['tokens'])} (work {tokens.human(r['work_tokens'])}) cost={cost}")
    rows += [f"  POLICY: {v}" for v in out.get("policy_violations", [])]
    return "\n".join(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hydra-pod-dsh", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    g = sub.add_parser("stage")
    g.add_argument("ticket", nargs="?")
    g.add_argument("stage", nargs="?", choices=MANAGER_STAGES)
    g.add_argument("--project", default=os.getcwd())
    g.add_argument("--by", default=live.MANAGER)
    g.add_argument("--clear", action="store_true")
    wf = sub.add_parser("wf", help="workflow state machine over the project ledger")
    wsub = wf.add_subparsers(dest="wf", required=True)
    def common(x):
        x.add_argument("--project", default=os.getcwd())
        x.add_argument("--by", help="actor name recorded in the ledger")
        return x
    x = common(wsub.add_parser("start"))
    x.add_argument("ticket"); x.add_argument("--objective", required=True)
    x.add_argument("--max-rework", type=int); x.add_argument("--max-review", type=int)
    x.add_argument("--max-replans", type=int)
    x.add_argument("--budget-cost", type=float); x.add_argument("--budget-credits", type=float)
    x.add_argument("--budget-minutes", type=float); x.add_argument("--budget-manager-tokens", type=float)
    x = common(wsub.add_parser("advance"))
    x.add_argument("workflow"); x.add_argument("state", choices=workflow.STATES); x.add_argument("--reason")
    x = common(wsub.add_parser("decide"))
    x.add_argument("workflow"); x.add_argument("decision", choices=workflow.DECISIONS)
    x.add_argument("--reason", required=True); x.add_argument("--task"); x.add_argument("--directive")
    x = common(wsub.add_parser("human"))
    x.add_argument("workflow"); x.add_argument("action", choices=("cancel", "pause", "resume", "approve", "reject"))
    x.add_argument("--reason", required=True); x.add_argument("--to", choices=workflow.STATES)
    x = common(wsub.add_parser("show"))
    x.add_argument("workflow", nargs="?"); x.add_argument("--json", action="store_true")
    x = common(wsub.add_parser("stages"))
    x.add_argument("workflow"); x.add_argument("--json", action="store_true")
    x = common(wsub.add_parser("amend"))
    x.add_argument("workflow"); x.add_argument("--field", required=True); x.add_argument("--reason", required=True)
    x.add_argument("--before"); x.add_argument("--after")
    x = common(wsub.add_parser("check"))
    x.add_argument("workflow", nargs="?"); x.add_argument("--json", action="store_true")
    x = common(wsub.add_parser("report"))
    x.add_argument("workflow"); x.add_argument("--write", action="store_true")
    x = common(wsub.add_parser("recover"))
    x.add_argument("workflow"); x.add_argument("--reason", required=True); x.add_argument("--force", action="store_true")
    for kind in ("budget", "health", "findings", "manager"):
        x = common(wsub.add_parser(kind))
        x.add_argument("workflow"); x.add_argument("--json", action="store_true")
    r = sub.add_parser("route")
    r.add_argument("role"); r.add_argument("--capability"); r.add_argument("--model"); r.add_argument("--json", action="store_true")
    pc = sub.add_parser("policy")
    pc.add_argument("--json", action="store_true")
    x = common(wsub.add_parser("resources"))
    x.add_argument("workflow"); x.add_argument("--json", action="store_true")
    x = common(wsub.add_parser("timeline"))
    x.add_argument("workflow", nargs="?"); x.add_argument("--limit", type=int); x.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        return _dispatch(ap, a)
    except (workflow.TransitionError, ledger.LedgerError, ValueError, KeyError) as e:
        # a refused move or bad input/config data: one clean line, never a traceback
        print(f"hydra-pod-dsh: {e if str(e) else e.__class__.__name__}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"hydra-pod-dsh: {e}", file=sys.stderr)
        return 1


def _dispatch(ap, a) -> int:
    if a.cmd == "wf":
        return _wf(a)
    if a.cmd == "route":
        try:
            r = router.route(a.role, a.capability, a.model)
        except router.PolicyError as e:
            print(f"hydra-pod-dsh: {e}", file=sys.stderr)
            return 3
        print(json.dumps({"schema_version": SCHEMA_VERSION, **r}) if a.json else
              f"{a.role} -> {r['agent']}: {r['runtime']} / {r['model']} [{r['billing']}]")
        return 0
    if a.cmd == "policy":
        v = router.violations()
        print(json.dumps({"schema_version": SCHEMA_VERSION, "violations": v}) if a.json else
              ("\n".join(v) if v else "policy: ok"))
        return 3 if v else 0
    if a.cmd == "status":
        st = status()
        print(json.dumps(st) if a.json else render(st))
        return 0
    if a.clear:
        live.clear_stage()
        print("stage cleared")
        return 0
    if not a.stage:
        ap.error("stage needs <ticket|-> <stage>, or --clear")
    entry = live.set_stage(None if a.ticket in (None, "-") else a.ticket, a.stage, os.path.abspath(a.project), a.by)
    print(f"stage: {entry['stage']}" + (f" ({entry['ticket']})" if entry["ticket"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
