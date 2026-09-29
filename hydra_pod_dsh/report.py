# SPDX-License-Identifier: AGPL-3.0-or-later
"""Closing report for one workflow (architecture §27, §46: who, what, why, how much).

Built only from the ledger, the cost log, the review reports and the DSH session
logs: every number has a source, and unknown values are printed as unknown.
"""

import time

from . import budget, consistency, findings, manager_usage, resources, router, stages, tokens, workflow


def _t(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"


def _money(v):
    return "unknown" if v is None else f"${v:.4f}"


def build(project, wid: str) -> str:
    resources.sync(project, wid)
    w = workflow.get(project, wid)
    res = resources.summary(project, wid)
    fnd = findings.summary(project, w.tasks)
    mgr = manager_usage.usage_for(str(project), w.created_at, w.updated_at if w.state in workflow.TERMINAL else None)
    violations = router.check_manager_routes(mgr["by_model"])
    drift = consistency.check(project, wid)
    lines = [f"# {w.id}: {w.objective}", "",
             f"- State: **{w.state}** (ticket folder `{w.folder}/`), tasks: {', '.join(w.tasks)}",
             f"- Started {_t(w.created_at)}, last change {_t(w.updated_at)}; git HEAD at last move: `{w.git_head or '-'}`",
             f"- Rework {w.rework_attempts}/{w.policy['max_rework_attempts']}, review cycles "
             f"{w.review_cycles}/{w.policy['max_review_cycles']}, replans {w.replans}/{w.policy['max_replans']}, "
             f"recoveries {w.recoveries}"]
    if w.last_decision:
        d = w.last_decision
        lines.append(f"- Last decision: **{d['decision']}**" + (f" (requested {d['requested']})"
                     if d.get("requested") and d["requested"] != d["decision"] else "") + f": {d['reason']}")
    lines += ["", "## Timeline", "", "| time | event | move | actor | reason |", "|---|---|---|---|---|"]
    for e in workflow.timeline(project, wid):
        if e["type"] == "hydra/resource-usage":
            continue
        p = e.get("payload") or {}
        move = f"{p.get('from', '')} → {p['to']}" if "to" in p else ""
        what = e["type"].split("/", 1)[1] + (f" {p.get('decision') or p.get('action') or ''}".rstrip())
        actor = (e.get("actor") or {}).get("name") or (e.get("actor") or {}).get("kind", "")
        lines.append(f"| {_t(e['at'])[11:]} | {what} | {move} | {actor} | {(e.get('reason') or '').replace('|', '/')} |")
    st = stages.build(project, wid)
    lines += ["", "## Stages (model, tokens and subscription share per stage)", "",
              "Work tokens = input + output + reasoning; cache is shown apart and never added to them.", "",
              "| # | stage | duration | who | model | billing | work tokens | tokens | cost / credits | share of 5h / week |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for s in st["stages"]:
        dur = f"{int(s['seconds'] // 60)}m{int(s['seconds'] % 60):02d}s"
        if not s["actors"]:
            lines.append(f"| {s['n']} | {s['state']} | {dur} | - | | | | | | |")
        for a in s["actors"]:
            spend = (f"list {_money(a['list_cost_usd'])}" if a.get("list_cost_usd") is not None else "") + \
                    (f" {int(a['zai_credits'])} credits" if a.get("zai_credits") is not None else "") + \
                    (f"cost {_money(a.get('cost_usd'))}" if a["kind"] == "manager" else "")
            q = a.get("quota") or {}
            share = " / ".join(f"{q[n]['percent']}%" for n in ("5h", "week") if n in q) or "-"
            if q:
                share += f" ({q['source']})"
            lines.append(f"| {s['n']} | {s['state']} | {dur} | {a['role']} | {a['model']} | {a['billing']} | "
                         f"{a['work_tokens']} | {tokens.fmt(a['tokens'])} | {spend.strip() or '-'} | {share} |")
    lines += ["", "## Workers (cost log)", "", "| role | model | billing route | runs | work tokens | tokens | list cost | Z.ai credits | seconds |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in res["by_route"]:
        lines.append(f"| {r['role']} | {r['model']} | {r['billing']} | {r['runs']} | {r['work_tokens']} | {tokens.fmt(r['tokens'])} | "
                     f"{_money(r['list_cost_usd'])} | {r['zai_credits'] if r['zai_credits'] is not None else '-'} | "
                     f"{r['seconds'] if r['seconds'] is not None else '-'} |")
    t = res["total"]
    lines.append(f"| **total** | | | {t['runs']} | {t['work_tokens']} | {tokens.fmt(t['tokens'])} | {_money(t['list_cost_usd'])} | "
                 f"{t['zai_credits'] if t['zai_credits'] is not None else '-'} | {t['seconds'] if t['seconds'] is not None else '-'} |")
    lines += ["", "## Manager (DSH session logs)", ""]
    if not mgr["sessions"]:
        lines.append("No DSH session for this project in the workflow's time span (the manager ran elsewhere, "
                     "or the steps were run by hand).")
    for r in mgr["by_model"]:
        lines.append(f"- {r['provider']}/{r['model']} [{r.get('billing')}]: {r['messages']} messages, "
                     f"{tokens.fmt(r['tokens'])} (work {r['work_tokens']}), cost {_money(r['cost_usd'])}")
    lines += [f"- **POLICY:** {v}" for v in violations]
    if w.budget:
        b = budget.check(project, wid)
        lines += ["", f"## Budget: {b['level']}", ""]
        lines += [f"- {d['dimension']}: {d['used'] if d['used'] is not None else 'unknown'} / {d['limit']} "
                  f"({d['percent'] if d['percent'] is not None else '?'}%)" for d in b["dimensions"]]
    lines += ["", "## Findings", "", f"By severity {fnd['by_severity']}; by status {fnd['by_status']}."]
    for r in fnd["reports"]:
        lines.append(f"- `{r['file']}` ({r['task_id']}): verdict {r['verdict'] or '-'}, manager verified: {r['manager_verified']}")
        lines += [f"  - [{f['severity']}] {f['status']}: {f['location'].replace('|', '/')}: "
                  f"{f['problem'][:140].replace('|', '/')}" for f in r["findings"]]
    lines += ["", "## Consistency", ""]
    lines += [f"- {p['kind']}: {p['detail']}" for p in drift] or ["- ledger and ticket folders agree"]
    return "\n".join(lines) + "\n"
