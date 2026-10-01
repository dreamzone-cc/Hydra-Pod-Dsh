# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured findings from Hydra-Pod review reports (architecture §5.4, rev 2).

Hydra-Pod reviewers write one finding per line as
`**<severity> | <file:line> | <problem> ...` into `_receipts/<task>.review*.md`,
and the Manager appends a `## Manager verdicts` section whose numbered items
say `VALID` / `INVALID` for each finding in order. This module reads both; it
never edits a report. A finding without a verdict is `unverified`.

Besides defects, the reviewer may report conclusions (`RC-<n> | observation |
evidence`) and suggestions (`RS-<n> | improvement | benefit | cost`), the
channel of technical paper §5-c. The manager answers each by id in its verdicts
section: `RC-1: ACCEPTED|REJECTED|NEEDS-EVIDENCE — why` and
`RS-1: ADOPT-NOW|BACKLOG|REJECTED — why`. An item without an answer is
`unverified`, like a finding.
"""

import re
from pathlib import Path

SEVERITIES = ("critical", "high", "medium", "low", "info")
FINDING = re.compile(r"^\W*\**\s*(critical|high|medium|low|info)\s*\|\s*([^|]*?)\s*\|\s*(.+)$", re.I)
VERDICT_ITEM = re.compile(r"^\s*(\d+)\.\s*\**\s*(critical|high|medium|low|info)\b(.*)$", re.I)
VERDICT_WORD = re.compile(r"\b(PARTIALLY VALID|INVALID|NOT VALID|REJECTED|VALID)\b")
ITEM = re.compile(r"^\W*(R[CS])-(\d+)\**\s*\|\s*(.+)$")
ITEM_VERDICT = re.compile(r"^\W*(R[CS]-\d+)\b[\s:*.)-]*(ACCEPTED|REJECTED|NEEDS[- ]EVIDENCE|ADOPT[- ]NOW|BACKLOG)\b"
                          r"\W*(.*)$", re.I)
ITEM_STATUSES = {"RC": ("accepted", "rejected", "needs-evidence"), "RS": ("adopt-now", "backlog", "rejected")}
OVERALL = re.compile(r"\*\*Verdict:\s*([A-Z_]+)", re.I)


def review_files(project, task: str) -> list[Path]:
    rec = Path(project) / "_receipts"
    return sorted(set(rec.glob(f"{task}.review*.md")) | set(rec.glob(f"{task}-*.review*.md")))


def parse(text: str) -> dict:
    head, _, verdicts = text.partition("## Manager verdicts")
    found = []
    for line in head.splitlines():
        m = FINDING.match(line.strip())
        if m:
            problem = re.sub(r"\*+", "", m.group(3)).strip()
            found.append({"severity": m.group(1).lower(), "location": m.group(2).strip(" *"),
                          "problem": problem.split(" | ")[0][:300], "unsure": "UNSURE" in line,
                          "status": "unverified"})
    items = [VERDICT_ITEM.match(line) for line in verdicts.splitlines()]
    items = [m for m in items if m]
    for i, m in enumerate(items):
        if i >= len(found):
            break
        w = VERDICT_WORD.search(m.group(3).upper())
        if w:
            word = w.group(1)
            found[i]["status"] = ("rejected" if word in ("INVALID", "NOT VALID", "REJECTED")
                                  else "partial" if word.startswith("PARTIALLY") else "valid")
    items = {}
    for line in head.splitlines():
        m = ITEM.match(line.strip())
        if m:
            iid = f"{m.group(1)}-{int(m.group(2))}"
            parts = [x.strip(" *") for x in m.group(3).split("|")]
            items[iid] = {"id": iid, "kind": "conclusion" if m.group(1) == "RC" else "suggestion",
                          "text": parts[0][:300], "detail": [x[:200] for x in parts[1:]],
                          "status": "unverified", "reason": None}
    for line in verdicts.splitlines():
        m = ITEM_VERDICT.match(line.strip())
        if not m or m.group(1).upper() not in items:
            continue
        item = items[m.group(1).upper()]
        word = m.group(2).lower().replace(" ", "-")
        if word in ITEM_STATUSES[item["id"][:2]]:
            item["status"], item["reason"] = word, m.group(3).strip()[:200] or None
    overall = OVERALL.search(head)
    return {"verdict": overall.group(1).upper() if overall else None, "findings": found,
            "items": list(items.values()), "manager_verified": bool(verdicts.strip())}


def for_task(project, task: str) -> list[dict]:
    out = []
    for f in review_files(project, task):
        r = parse(f.read_text(errors="replace"))
        r["file"] = f.name
        out.append(r)
    return out


def summary(project, tasks: list[str]) -> dict:
    counts = {s: 0 for s in SEVERITIES}
    status = {"valid": 0, "partial": 0, "rejected": 0, "unverified": 0}
    reports, items = [], {}
    for t in tasks:
        for r in for_task(project, t):
            r["task_id"] = t
            reports.append(r)
            for f in r["findings"]:
                counts[f["severity"]] += 1
                status[f["status"]] += 1
            for i in r["items"]:
                key = f"{i['kind']}:{i['status']}"
                items[key] = items.get(key, 0) + 1
    return {"reports": reports, "by_severity": counts, "by_status": status, "items": items}


def unverified(summary_: dict) -> list[str]:
    """Every finding and reviewer item still waiting for the manager's verdict, as one line each."""
    out = []
    for r in summary_["reports"]:
        out += [f"{r['task_id']} [{f['severity']}] {f['location']}" for f in r["findings"] if f["status"] == "unverified"]
        out += [f"{r['task_id']} {i['id']} ({i['kind']})" for i in r["items"] if i["status"] == "unverified"]
    return out
