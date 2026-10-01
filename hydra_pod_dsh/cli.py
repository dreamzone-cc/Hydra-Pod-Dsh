# SPDX-License-Identifier: AGPL-3.0-or-later
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
  hydra-pod-dsh wf brief <WF> [--json]        resume brief: state, counters, open findings, recent events, next moves
  hydra-pod-dsh wf diffsum <WF> [--task T] [--base REV] [--head REV] [--json]
                                              deterministic diff summary (files, symbols, scope) of a ticket's change
  hydra-pod-dsh wf pack <WF> [--task T] [--tokens N] [--json]
                                              write the ticket's context pack (_receipts/<ticket>.context.md)
  hydra-pod-dsh wf reviewers <WF> [--task T] [--json]   review stages this ticket needs, and who runs each
  hydra-pod-dsh wf consult <WF> ask --question-file Q.md [--advisor NAME ...] [--context FILE ...]
                                              independent opinions from the advisor panel (read-only)
  hydra-pod-dsh wf consult <WF> decide --text DECISION --reason TEXT   the manager's decision on the latest consult
  hydra-pod-dsh run review <ticket> --agent NAME [--wf WF] [--max-budget-usd N]
                                              a read-only review stage hydra-pod-dispatch cannot run (e.g. reviewer-claude)
  hydra-pod-dsh wf amend <WF> --field F --reason TEXT [--before X --after Y]   manager corrects its ticket
  hydra-pod-dsh wf check [<WF>] [--json]      ledger vs ticket folders (exit 4 on drift)
  hydra-pod-dsh wf report <WF> [--write]      closing report (Markdown); --write saves _receipts/<WF>.report.md
  hydra-pod-dsh bench report [--project DIR ...] [--save FILE] [--json]
                                              gate metrics per workflow and their medians (technical paper §7.1)
  hydra-pod-dsh bench compare <BASELINE.json> <NEW.json> [--json]   change of each gate metric
  hydra-pod-dsh map [--focus FILE ...] [--tokens N] [--project DIR]   ranked repository outline
  hydra-pod-dsh lesson add --text TEXT [--path P ...] [--tag T ...] [--wf WF] [--project DIR]
  hydra-pod-dsh lesson list [--path P ...] [--text TICKET_TEXT] [--project DIR] [--json]
  hydra-pod-dsh pick <role> [--complexity S|M|L] [--wf WF] [--project DIR] [--json]
                                              best available agent of the role's pool (quota-aware); --wf records it
  hydra-pod-dsh profile list|show|set [NAME] [--project DIR]   team profile (economy, balanced, max-quality)
  hydra-pod-dsh plan approve <NAME> --ticket T1 --ticket T2:T1 ... --reason TEXT [--project DIR]
                                              approve a multi-ticket plan (T2:T1 = T2 depends on T1)
  hydra-pod-dsh plan show <NAME> [--json] | plan list   waves, states, and what may start now
  hydra-pod-dsh skill add <NAME> --description D --file BODY.md [--paths G,..] [--keywords K,..] [--reason R]
  hydra-pod-dsh skill revise|approve|retire <NAME> [--file BODY.md] --reason R | skill list|show [NAME]
  hydra-pod-dsh steward init | steward set <SECTION> --text T --reason R | steward show   [--project DIR]
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
from pathlib import Path

from . import (adapters, bench, brief, budget, consistency, diffsum, findings, lessons, pack, plan, profiles, repomap,
               runner, skills, steward, health, ledger, live, manager_usage, report, resources, router, stages,
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
            lines.append(f"    {w['window']:<6} {pct:>6}  {amount:<18} resets in {usage.left(w['resets_at'], now)}")
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
        if a.state == "EXECUTING":
            first = workflow.get(project, a.workflow).tasks[0]
            waiting = plan.blocked_by(project, first)
            if waiting:
                raise workflow.TransitionError(
                    f"{a.workflow}: EXECUTING refused: the approved plan makes {first} wait for "
                    f"{', '.join(waiting)} (not DONE yet)")
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
        if a.decision == "APPROVE":
            # No finding, conclusion or suggestion may be left unanswered (technical paper §5-c).
            pending = findings.unverified(findings.summary(project, workflow.get(project, a.workflow).tasks))
            if pending:
                raise workflow.TransitionError(
                    f"{a.workflow}: APPROVE refused: {len(pending)} reviewer item(s) without a manager verdict: "
                    + "; ".join(pending[:8]) + ("; …" if len(pending) > 8 else ""))
        w = workflow.decide(project, a.workflow, a.decision, a.reason, actor=actor, task_id=a.task,
                            directive=directive)
        _score_skills(project, w)
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
    elif a.wf == "brief":
        out = brief.build(project, a.workflow)
        print(json.dumps({"schema_version": SCHEMA_VERSION, **out}) if a.json else brief.render(out))
        return 0
    elif a.wf == "diffsum":
        if a.task:
            workflow.validate_ticket_id(a.task)  # the id becomes a glob pattern
        task = a.task or workflow.get(project, a.workflow).task_id
        out = diffsum.summarize(project, task, a.base, a.head)
        print(json.dumps({"schema_version": SCHEMA_VERSION, **out}) if a.json else diffsum.render(out))
        return 0
    elif a.wf == "pack":
        if a.task:
            workflow.validate_ticket_id(a.task)
        out = pack.write(project, a.workflow, a.task, a.tokens)
        if a.json:
            print(json.dumps({"schema_version": SCHEMA_VERSION, **{k: v for k, v in out.items() if k != "text"}}))
        else:
            print(f"{out['path']}: {out['tokens']} tokens (budget {out['budget_tokens']}), "
                  f"{len(out['files'])} file(s), {len(out['read_hints'])} read hint(s), {out['lessons']} lesson(s), "
                  f"map of {out['map_files']} file(s)")
            if not out["ticket_points_to_pack"]:
                print(f"  add this line to the ticket body: {pack.render_pointer(out)}")
        return 0
    elif a.wf == "reviewers":
        if a.task:
            workflow.validate_ticket_id(a.task)
        w = workflow.get(project, a.workflow)
        task = a.task or w.task_id
        risk = (diffsum.ticket_header(project, task).get("risk") or "").lower() or None
        prof = profiles.active(project)
        cascade = router.review_plan(risk, findings.summary(project, w.tasks), second_review=prof["second_review"])
        reg = router.load()
        for st in cascade:
            if st["agent"]:
                st["dispatch"] = adapters.for_runtime(st["runtime"]).dispatch(
                    {**reg["agents"][st["agent"]], "name": st["agent"]}, "reviewer", task)
        if a.json:
            print(json.dumps({"schema_version": SCHEMA_VERSION, "task": task, "risk": risk, "profile": prof["name"],
                              "stages": cascade}))
        else:
            print(f"{task}: risk {risk or '-'}, profile {prof['name']}")
            for st in cascade:
                who = f"{st['agent']} ({st['model']})" if st["agent"] else "NO AVAILABLE AGENT"
                tried = "; ".join(f"{x['agent']}: {x['why']}" for x in st["tried"])
                print(f"  stage {st['stage']}: {'RUN' if st['needed'] else 'skip'} — {st['why']} — {who}"
                      + (f" — {st['dispatch']['command']}" if st["needed"] and st.get("dispatch") else "")
                      + (f" [skipped: {tried}]" if tried else ""))
        return 3 if any(st["needed"] and not st["agent"] for st in cascade) else 0
    elif a.wf == "consult":
        return _consult(project, a)
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
                      f"cost={cost:<9} zai_credits={cred:<5} seconds={r['seconds'] or '-'} "
                      f"tool_calls={r['tool_calls'] if r['tool_calls'] is not None else '-'}")
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


def _budget_gate(project: str, wid: str) -> int | None:
    """Exit 3 when the workflow's budget is exhausted; warnings go to stderr."""
    if not workflow.get(project, wid).budget:
        return None
    resources.sync(project, wid)
    b = budget.check(project, wid)
    for d in b["dimensions"]:
        if d["level"] in ("warn", "block"):
            print(f"hydra-pod-dsh: {'BLOCKED' if d['level'] == 'block' else 'warning'}: {d['dimension']} at "
                  f"{d['percent']}% of budget", file=sys.stderr)
    return 3 if b["level"] == "block" else None


def _cost_task(project: str, task: str) -> str:
    """The name hydra-pod-dispatch gives the ticket's cost log and reports: the ticket file's stem."""
    t = diffsum.ticket_path(project, task)
    return t.stem if t else task


def _run_review(project: str, a) -> int:
    workflow.validate_ticket_id(a.ticket)
    reg = router.load()
    ag = reg["agents"].get(a.agent)
    if ag is None:
        raise ValueError(f"no agent {a.agent!r} in the registry")
    pools = [r for r, p in reg["pools"].items() if r in ("reviewer", "security")
             and any(a.agent in [m["agent"]] + list(m.get("fallback", [])) for m in router._members(p))]
    if not pools:
        raise ValueError(f"{a.agent} is in no reviewer or security pool")
    why = router._passes(a.agent, ag, pools[0], reg)
    if why:
        print(f"hydra-pod-dsh: {a.agent}: {why}", file=sys.stderr)
        return 3
    if ag["runtime"] not in runner.PROVIDER or (ag["runtime"] == "opencode" and ag["model"].startswith("zai-coding-plan/")):
        raise ValueError(f"{a.agent} runs through hydra-pod-dispatch review, not here")
    if a.wf and _budget_gate(project, a.wf):
        return 3
    stem = _cost_task(project, a.ticket)
    report = Path(project) / "_receipts" / f"{stem}.review-{a.agent}.md"
    try:
        prompt = runner.review_prompt(project, a.ticket, a.agent, ag["model"])
    except runner.RunError as e:
        raise ValueError(str(e))  # bad input: one clean line, exit 2
    try:
        r = runner.run(project, a.agent, ag, prompt, report, stem, "review", a.max_budget_usd)
    except runner.RunError as e:
        print(f"hydra-pod-dsh: {e}", file=sys.stderr)
        return 1
    if a.wf:
        resources.sync(project, a.wf)
    cost = "-" if r["list_cost_usd"] is None else f"${r['list_cost_usd']:.4f} list"
    print(f"{r['report']}: {a.agent} ({ag['model']}) in {r['seconds']}s, work {tokens.human(tokens.work(tokens.normalize(r['tokens'])))} "
          f"tokens, {cost} [{ag['billing']}]")
    return 0


def _consult(project: str, a) -> int:
    w = workflow.get(project, a.workflow)
    reg = router.load()
    previous = [e for e in workflow.timeline(project, w.id) if e["type"] == "hydra/consult"]
    if a.action == "decide":
        asks = [e for e in previous if e["payload"].get("action") == "ask"]
        if not asks:
            raise ValueError(f"{w.id} has no consult to decide on")
        if not a.text or not (a.reason or "").strip():
            raise ValueError("consult decide needs --text (the decision) and --reason")
        n = asks[-1]["payload"]["n"]
        workflow.record(project, w.id, "hydra/consult", ignorable=True, reason=a.reason,
                        payload={"action": "decide", "n": n, "decision": a.text})
        print(f"{w.id}: consult {n} decided — {a.text}")
        return 0
    if not a.question_file:
        raise ValueError("consult ask needs --question-file")
    with open(a.question_file, encoding="utf-8") as f:
        question = f.read()
    if len(question.strip()) < 20:
        raise ValueError("the question is too short to consult on")
    pool = reg["pools"].get("advisor")
    names = a.advisor or ([m["agent"] for m in router._members(pool)] if pool else [])
    if not names:
        raise ValueError("no advisors: the registry has no advisor pool, and none was named")
    for name in names:
        ag = reg["agents"].get(name)
        why = "not in the registry" if ag is None else router._passes(name, ag, "advisor", reg)
        if why:
            print(f"hydra-pod-dsh: advisor {name}: {why}", file=sys.stderr)
            return 3
    if _budget_gate(project, w.id):
        return 3
    n = 1 + sum(e["payload"].get("action") == "ask" for e in previous)
    prompt = runner.consult_prompt(question, a.context)
    stem = _cost_task(project, w.task_id)
    reports, failed = [], []
    for name in names:
        out = Path(project) / "_receipts" / f"{w.id}.consult-{n}-{name}.md"
        try:
            r = runner.run(project, name, reg["agents"][name], prompt, out, stem, "consult", a.max_budget_usd)
            reports.append(r["report"])
            print(f"  {name}: {r['report']} ({r['seconds']}s)")
        except runner.RunError as e:
            failed.append({"advisor": name, "error": str(e)[:300]})
            print(f"  {name}: FAILED — {e}", file=sys.stderr)
    resources.sync(project, w.id)
    workflow.record(project, w.id, "hydra/consult", ignorable=True,
                    payload={"action": "ask", "n": n, "advisors": names, "reports": reports, "failed": failed,
                             "question": question.strip()[:500]})
    print(f"{w.id}: consult {n}: {len(reports)} opinion(s)" + (f", {len(failed)} failed" if failed else "")
          + f". Weigh them, then: hydra-pod-dsh wf consult {w.id} decide --text '…' --reason '…'")
    return 0 if reports else 1


def _score_skills(project: str, w: "workflow.Workflow") -> None:
    """Skills attached to the workflow's latest context pack earn the decision's outcome."""
    decision = (w.last_decision or {}).get("decision")
    if decision not in ("APPROVE", "REWORK"):
        return
    packs = [e for e in workflow.timeline(project, w.id) if e["type"] == "hydra/context-pack"]
    if not packs:
        return
    last = packs[-1]
    for name in last["payload"].get("skills") or []:
        try:
            s = skills.outcome(project, name, decision == "APPROVE", w.id, last["seq"])
        except ValueError:
            continue  # a skill removed since the pack was written
        print(f"  skill {name}: {decision == 'APPROVE' and 'success' or 'failure'} recorded ({s['state']})")


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
        rows = [f"by severity: {out['by_severity']}", f"by status: {out['by_status']}",
                f"conclusions and suggestions: {out['items'] or 'none'}"]
        for r in out["reports"]:
            rows.append(f"  {r['task_id']} {r['file']}: verdict {r['verdict'] or '-'}, "
                        f"{len(r['findings'])} finding(s), manager verified: {r['manager_verified']}")
            rows += [f"    [{f['severity']}] {f['status']:<10} {f['location']} — {f['problem'][:90]}" for f in r["findings"]]
            rows += [f"    {i['id']} {i['status']:<14} {i['text'][:90]}" + (f" — {i['reason'][:60]}" if i["reason"] else "")
                     for i in r["items"]]
        return "\n".join(rows)
    rows = [f"sessions: {', '.join(out['sessions']) or 'none'}"]
    for r in out["by_model"]:
        cost = "unknown" if r["cost_usd"] is None else f"${r['cost_usd']:.4f}"
        rows.append(f"  {r['provider']}/{r['model']} [{r.get('billing', '?')}] messages={r['messages']} "
                    f"{tokens.fmt(r['tokens'])} (work {tokens.human(r['work_tokens'])}) cost={cost}"
                    + ("" if r.get("cache_hit_ratio") is None else f" cache_hit={100 * r['cache_hit_ratio']:.0f}%"))
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
    x = common(wsub.add_parser("brief"))
    x.add_argument("workflow"); x.add_argument("--json", action="store_true")
    x = common(wsub.add_parser("diffsum"))
    x.add_argument("workflow"); x.add_argument("--task", help="ticket whose header gives base/head/allowed_files")
    x.add_argument("--base"); x.add_argument("--head", help="default: the ticket's head, else the working tree")
    x.add_argument("--json", action="store_true")
    x = common(wsub.add_parser("pack"))
    x.add_argument("workflow"); x.add_argument("--task"); x.add_argument("--json", action="store_true")
    x.add_argument("--tokens", type=int, default=pack.DEFAULT_TOKENS, help="budget of the pack")
    x = common(wsub.add_parser("reviewers"))
    x.add_argument("workflow"); x.add_argument("--task"); x.add_argument("--json", action="store_true")
    x = common(wsub.add_parser("consult"))
    x.add_argument("workflow"); x.add_argument("action", choices=("ask", "decide"))
    x.add_argument("--question-file"); x.add_argument("--advisor", action="append")
    x.add_argument("--context", action="append", default=[]); x.add_argument("--max-budget-usd", type=float)
    x.add_argument("--text"); x.add_argument("--reason")
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
    bn = sub.add_parser("bench", help="benchmark report over the ledgers of one or more projects")
    bsub = bn.add_subparsers(dest="bench", required=True)
    x = bsub.add_parser("report")
    x.add_argument("--project", action="append", help="repeatable; default: the current directory")
    x.add_argument("--save", help="also write the JSON report to this file (the baseline of a phase gate)")
    x.add_argument("--json", action="store_true")
    x = bsub.add_parser("compare")
    x.add_argument("baseline"); x.add_argument("new"); x.add_argument("--json", action="store_true")
    x = sub.add_parser("map", help="ranked repository outline within a token budget")
    x.add_argument("--project", default=os.getcwd()); x.add_argument("--focus", action="append")
    x.add_argument("--tokens", type=int, default=1500); x.add_argument("--json", action="store_true")
    ls = sub.add_parser("lesson", help="the project's lessons memory (_receipts/lessons.md)")
    lsub = ls.add_subparsers(dest="lesson", required=True)
    x = lsub.add_parser("add")
    x.add_argument("--project", default=os.getcwd()); x.add_argument("--text", required=True)
    x.add_argument("--path", action="append"); x.add_argument("--tag", action="append"); x.add_argument("--wf")
    x = lsub.add_parser("list")
    x.add_argument("--project", default=os.getcwd()); x.add_argument("--path", action="append")
    x.add_argument("--text", default=""); x.add_argument("--json", action="store_true")
    x = sub.add_parser("pick", help="best available agent of a role's pool")
    x.add_argument("role"); x.add_argument("--complexity", choices=("S", "M", "L"))
    x.add_argument("--wf"); x.add_argument("--project", default=os.getcwd()); x.add_argument("--json", action="store_true")
    x = sub.add_parser("profile", help="the project's team profile")
    x.add_argument("action", choices=("list", "show", "set")); x.add_argument("name", nargs="?")
    x.add_argument("--project", default=os.getcwd())
    x = sub.add_parser("plan", help="approved multi-ticket plans and their dependency waves")
    x.add_argument("action", choices=("approve", "show", "list")); x.add_argument("name", nargs="?")
    x.add_argument("--ticket", action="append", default=[], help="T or T:DEP1,DEP2 (repeatable)")
    x.add_argument("--reason"); x.add_argument("--by"); x.add_argument("--project", default=os.getcwd())
    x.add_argument("--json", action="store_true")
    x = sub.add_parser("skill", help="the project's skills library (.hydra/skills)")
    x.add_argument("action", choices=("add", "revise", "approve", "retire", "list", "show"))
    x.add_argument("name", nargs="?"); x.add_argument("--description"); x.add_argument("--file")
    x.add_argument("--paths"); x.add_argument("--keywords"); x.add_argument("--reason", default="")
    x.add_argument("--project", default=os.getcwd()); x.add_argument("--json", action="store_true")
    x = sub.add_parser("steward", help="the project steward's core memory (.hydra/steward/core.md)")
    x.add_argument("action", choices=("init", "set", "show")); x.add_argument("section", nargs="?")
    x.add_argument("--text"); x.add_argument("--reason"); x.add_argument("--project", default=os.getcwd())
    x = sub.add_parser("run", help="run a read-only review stage that hydra-pod-dispatch cannot run")
    x.add_argument("what", choices=("review",)); x.add_argument("ticket"); x.add_argument("--agent", required=True)
    x.add_argument("--wf"); x.add_argument("--max-budget-usd", type=float)
    x.add_argument("--project", default=os.getcwd())
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


def _bench(a) -> int:
    if a.bench == "compare":
        with open(a.baseline, encoding="utf-8") as f:
            base = json.load(f)
        with open(a.new, encoding="utf-8") as f:
            new = json.load(f)
        rows = bench.compare(base, new)
        print(json.dumps({"schema_version": SCHEMA_VERSION, "metrics": rows}) if a.json else bench.render_compare(rows))
        return 0
    out = {"schema_version": SCHEMA_VERSION, **bench.report([os.path.abspath(p) for p in a.project or [os.getcwd()]])}
    if a.save:
        with open(a.save, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
    print(json.dumps(out) if a.json else bench.render(out))
    return 0


def _dispatch(ap, a) -> int:
    if a.cmd == "wf":
        return _wf(a)
    if a.cmd == "bench":
        return _bench(a)
    if a.cmd == "pick":
        try:
            r = router.choose(a.role, a.complexity)
        except router.PolicyError as e:
            print(f"hydra-pod-dsh: {e}", file=sys.stderr)
            return 3
        if a.wf:
            workflow.record(os.path.abspath(a.project), a.wf, "hydra/route-decision", ignorable=True,
                            payload={"role": a.role, "complexity": a.complexity, "agent": r["agent"],
                                     "model": r["model"], "billing": r["billing"], "considered": r["considered"]})
        print(json.dumps({"schema_version": SCHEMA_VERSION, **r}) if a.json else
              f"{a.role} -> {r['agent']}: {r['runtime']} / {r['model']} [{r['billing']}]"
              + "".join(f"\n  passed over {c['agent']}: {c['why']}" for c in r["considered"] if not c["ok"]))
        return 0
    if a.cmd == "profile":
        project = os.path.abspath(a.project)
        if a.action == "list":
            act = profiles.active(project)["name"]
            for name, p in profiles.available().items():
                print(f"{'*' if name == act else ' '} {name:<12} {p['description']}")
        elif a.action == "show":
            p = profiles.active(project)
            print(f"{p['name']}: {p['description']}")
        else:
            if not a.name:
                raise ValueError("profile set needs a NAME")
            p = profiles.set_active(project, a.name)
            print(f"profile: {p['name']} ({profiles.active_file(project)})")
        return 0
    if a.cmd == "plan":
        project = os.path.abspath(a.project)
        if a.action == "list":
            for name, p in plan.plans(project).items():
                print(f"{name}: {len(p['deps'])} ticket(s) in {len(p['waves'])} wave(s) — {p.get('reason') or ''}")
            return 0
        if not a.name:
            raise ValueError(f"plan {a.action} needs a NAME")
        if a.action == "approve":
            if not a.ticket:
                raise ValueError("plan approve needs at least one --ticket")
            out = plan.approve(project, a.name, a.ticket, a.reason or "",
                               {"kind": "manager", "name": a.by} if a.by else None)
        else:
            out = plan.status(project, a.name)
        if a.json:
            print(json.dumps({"schema_version": SCHEMA_VERSION, **out}))
            return 0
        print(f"plan {out['name']}: " + " → ".join("[" + ", ".join(w) + "]" for w in out["waves"]))
        if "states" in out:
            for t, st in out["states"].items():
                after = f" (after {', '.join(out['deps'][t])})" if out["deps"][t] else ""
                print(f"  {t:<20} {st}{after}")
            print(f"  start now: {', '.join(out['start_now']) or 'nothing'}"
                  + ("  — plan complete" if out["done"] else ""))
        return 0
    if a.cmd == "skill":
        project = os.path.abspath(a.project)
        if a.action == "list":
            idx = skills.load(project)["skills"]
            if a.json:
                print(json.dumps({"schema_version": SCHEMA_VERSION, "skills": idx}))
            for name, s in sorted(idx.items()) if not a.json else ():
                ok = sum(o["ok"] for o in s["outcomes"])
                print(f"{name:<24} {s['state']:<9} v{s['version']} {ok}/{len(s['outcomes'])} ok — {s['description']}")
            if not idx and not a.json:
                print("no skills")
            return 0
        if not a.name:
            raise ValueError(f"skill {a.action} needs a NAME")
        if a.action == "show":
            print(skills.body(project, a.name))
            return 0
        if a.action in ("add", "revise"):
            if not a.file:
                raise ValueError(f"skill {a.action} needs --file with the skill's instructions")
            with open(a.file, encoding="utf-8") as f:
                text = f.read()
            s = (skills.add(project, a.name, a.description or "", text, a.paths, a.keywords, a.reason)
                 if a.action == "add" else skills.revise(project, a.name, text, a.reason))
        elif a.action == "approve":
            s = skills.approve(project, a.name, a.reason)
        else:
            s = skills.retire(project, a.name, a.reason)
        print(f"skill {a.name}: {s['state']} (v{s['version']})")
        return 0
    if a.cmd == "steward":
        project = os.path.abspath(a.project)
        if a.action == "init":
            print(f"steward memory: {steward.init(project)}")
        elif a.action == "show":
            print(steward.core(project) or "no steward memory yet (hydra-pod-dsh steward init)")
        else:
            if not a.section or a.text is None:
                raise ValueError("steward set needs a SECTION and --text")
            print(f"steward memory: {steward.set_section(project, a.section, a.text, a.reason or '')}")
        return 0
    if a.cmd == "run":
        return _run_review(os.path.abspath(a.project), a)
    if a.cmd == "map":
        if a.tokens < 100:
            raise ValueError("--tokens must be at least 100")
        out = repomap.build(os.path.abspath(a.project), a.focus, a.tokens)
        print(json.dumps({"schema_version": SCHEMA_VERSION, **out}) if a.json else out["text"])
        return 0
    if a.cmd == "lesson":
        project = os.path.abspath(a.project)
        if a.lesson == "add":
            if a.wf:
                workflow.validate_workflow_id(a.wf)
            lessons.add(project, a.text, a.path, a.tag, a.wf)
            print(f"lesson recorded in {lessons.path(project)}")
            return 0
        found = lessons.relevant(project, a.path or [], a.text, limit=50) if (a.path or a.text) else lessons.load(project)
        if a.json:
            print(json.dumps({"schema_version": SCHEMA_VERSION, "lessons": found}))
        else:
            print("\n".join(f"{x['date']} {x['workflow_id']}: {x['text']}" for x in found) or "no lessons")
        return 0
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
