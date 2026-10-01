# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for findings, manager usage, router/policy, budgets, health/recovery and checkpoints."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_pod_dsh import budget, findings, health, manager_usage, router, workflow  # noqa: E402

TO_VERIFY = ["PLANNING", "PLAN_READY", "ASSIGNING", "EXECUTING", "TESTING", "REVIEWING", "VERIFYING"]
REVIEW = """**Verdict: FAIL — one real problem.**

**high | src/x.py:20 | Tokenization regression: tabs no longer split | evidence | fix**

**medium | _receipts/T2.receipt.md:8 | Receipt evidence predates the head**

**low | tests/t.py:80 (UNSURE) | Test does not discriminate**

## Manager verdicts (Opus, 2026-09-25)

1. **high: tokenization regression, VALID.** Reproduced.
2. **medium: receipt evidence, INVALID.** The receipt is accurate.
"""


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.p / "cache"), "DSH_HOME": str(self.p / "dsh")})
        env.start()
        self.addCleanup(env.stop)


class FindingsTest(Tmp):
    def test_parse_findings_and_verdicts(self):
        r = findings.parse(REVIEW)
        self.assertEqual(r["verdict"], "FAIL")
        self.assertEqual([(f["severity"], f["status"]) for f in r["findings"]],
                         [("high", "valid"), ("medium", "rejected"), ("low", "unverified")])
        self.assertEqual(r["findings"][0]["location"], "src/x.py:20")
        self.assertTrue(r["findings"][2]["unsure"])

    def test_summary_over_review_files_of_all_tasks(self):
        rec = self.p / "_receipts"
        rec.mkdir(exist_ok=True)
        (rec / "T2-slug.review.md").write_text(REVIEW)
        (rec / "T2b.review-zcode.md").write_text("**Verdict: PASS**\n\nNo findings.\n")
        s = findings.summary(self.p, ["T2", "T2b"])
        self.assertEqual(s["by_severity"]["high"], 1)
        self.assertEqual(s["by_status"], {"valid": 1, "partial": 0, "rejected": 1, "unverified": 1})
        self.assertEqual(len(s["reports"]), 2)

    def test_real_hydra_pod_report_if_present(self):
        f = Path.home() / "om-sandbox/_receipts/T2-min-length.review.md"
        if not f.exists():
            self.skipTest("sandbox not present")
        r = findings.parse(f.read_text())
        self.assertEqual([x["severity"] for x in r["findings"]], ["high", "medium", "low"])
        self.assertTrue(all(x["status"] == "valid" for x in r["findings"]))


def write_session(home: Path, cwd: str, events: list, compress=True, name="s1"):
    d = home / "sessions" / "--key--" / f"session-{name}"
    d.mkdir(parents=True)
    lines = [json.dumps({"type": "session", "version": 4, "id": f"session-{name}", "cwd": cwd})]
    lines += [json.dumps(e) for e in events]
    data = ("\n".join(lines) + "\n").encode()
    if compress:
        from compression import zstd
        half = len(data) // 2
        # two frames, as appends produce
        (d / "session.v4.jsonl.zstd").write_bytes(zstd.compress(data[:half]) + zstd.compress(data[half:]))
    else:
        (d / "session.v4.jsonl").write_bytes(data)


def msg(t, provider, model, i, o):
    return {"type": "assistant/message", "seq": 1, "time": int(t * 1000),
            "data": {"message": {"source": {"provider": provider, "model": model}},
                     "usage": {"inputTokens": i, "outputTokens": o, "cacheReadTokens": 5}}}


SKILL = {"type": "user/message", "data": {"content": [{"type": "text", "text": '<skill_content name="hydra-pod">'}]}}

try:
    from compression import zstd as _zstd  # Python 3.14+
except ImportError:  # pragma: no cover - older Python
    _zstd = None


@unittest.skipUnless(_zstd, "DSH session logs (.zstd) need Python 3.14+")
class ManagerUsageTest(Tmp):
    def test_counts_only_hydra_sessions_for_the_project_in_the_span(self):
        home = self.p / "dsh"
        proj = str(self.p / "proj")
        write_session(home, proj, [SKILL, msg(100, "anthropic", "claude-opus-5-5", 10, 20),
                                   msg(300, "anthropic", "claude-opus-5-5", 1, 2)])
        write_session(home, proj, [msg(150, "anthropic", "claude-opus-5-5", 999, 999)], name="s2")  # no skill
        write_session(home, "/elsewhere", [SKILL, msg(150, "anthropic", "x", 999, 999)], name="s3")
        u = manager_usage.usage_for(proj, since=50, until=200, home=home)
        self.assertEqual(u["sessions"], ["session-s1"])
        self.assertEqual([(r["input"], r["output"], r["cache_read"]) for r in u["by_model"]], [(10, 20, 5)])
        self.assertIsNone(u["by_model"][0]["cost_usd"])  # no price configured: unknown, not 0

    def test_parent_workspace_counts_when_log_names_the_project(self):
        home = self.p / "dsh"
        proj = str(self.p / "ws" / "proj")
        write_session(home, str(self.p / "ws"), [SKILL, {"type": "tool/call", "data": {"cmd": f"cd {proj}"}},
                                                 msg(100, "pi-anthropic", "claude-opus-5", 1, 1)], compress=False)
        u = manager_usage.usage_for(proj, 0, home=home)
        self.assertEqual(len(u["by_model"]), 1)
        # Operator amendment 2026-09-30: the Claude Pro OAuth bridge is an allowed manager route.
        self.assertEqual(router.check_manager_routes(u["by_model"]), [])  # annotates billing in place
        self.assertEqual(u["by_model"][0]["billing"], "oauth/claude-pro")

    def test_non_whitelisted_manager_routes_are_still_flagged(self):
        def row(provider):
            return {"provider": provider, "model": "m", "messages": 1,
                    "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}}
        for provider in ("zai-payg", "opencode"):  # pay-as-you-go and unmapped runtimes stay out of policy
            self.assertTrue(router.check_manager_routes([row(provider)]), provider)

    def test_truncated_final_frame_is_tolerated(self):
        home = self.p / "dsh"
        write_session(home, "/p", [SKILL, msg(1, "anthropic", "m", 1, 1)])
        f = next((home / "sessions").rglob("*.zstd"))
        f.write_bytes(f.read_bytes() + b"\x28\xb5\x2f\xfd\x00")  # start of a frame being written
        self.assertEqual(len(manager_usage.usage_for("/p", 0, home=home)["by_model"]), 1)

    def test_scan_is_memoized_per_file_version(self):
        """`status` reads every workflow's manager rows: one project, so each
        session file must be decompressed once, not once per workflow."""
        home = self.p / "dsh"
        write_session(home, "/p", [SKILL, msg(1, "anthropic", "m", 1, 1)])
        f = next((home / "sessions").rglob("*.zstd"))
        calls = []
        real = manager_usage._scan

        def counting(path, project):
            calls.append(path)
            return real(path, project)

        with mock.patch.object(manager_usage, "_scan", counting):
            manager_usage.scan(f, "/p")
            manager_usage.scan(f, "/p")
        self.assertEqual(len(calls), 1)
        os.utime(f, ns=(0, 0))  # a new file version must invalidate the memo
        with mock.patch.object(manager_usage, "_scan", counting):
            manager_usage.scan(f, "/p")
        self.assertEqual(len(calls), 2)


class RouterTest(unittest.TestCase):
    def test_shipped_registry_passes_policy(self):
        self.assertEqual(router.violations(), [])

    def test_hard_constraints(self):
        reg = router.load()
        self.assertEqual(router.route("executor", reg=reg)["billing"], "subscription/opencode-go")
        with self.assertRaises(router.PolicyError):   # zcode cannot select a model; opencode reviewer can
            router.route("reviewer", model="other/m", reg=reg, exclude=("reviewer", "reviewer-claude"))
        self.assertEqual(router.route("reviewer", capability="security", reg=reg)["agent"], "reviewer-claude")
        with self.assertRaises(router.PolicyError):
            router.route("reviewer", capability="security", reg=reg, exclude=("reviewer-claude",))

    def test_registry_violations_detected(self):
        reg = router.load()
        reg["agents"]["builder"]["billing"] = "api/deepseek"
        reg["agents"]["reviewer"].pop("read_only")
        reg["agents"]["builder"]["model"] = reg["agents"]["manager"]["model"]
        v = router.violations(reg)
        self.assertTrue(any("not allowed" in x for x in v))
        self.assertTrue(any("read-only" in x for x in v))
        self.assertTrue(any("also a executor" in x for x in v))

    def test_classify_provider(self):
        none = {}   # no DSH profile definitions: the name decides
        self.assertEqual(router.classify_provider("pi-anthropic", none), "oauth/claude-pro")
        self.assertEqual(router.classify_provider("anthropic", none), "api/anthropic")
        self.assertEqual(router.classify_provider("deepseek-official", none), "api/deepseek")

    def test_classify_provider_from_the_dsh_profile(self):
        """In a DSH profile `anthropic` can be an OAuth bridge to a Claude subscription: the definition decides."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.p = Path(tmp.name)
        prof = self.p / "dsh/profiles"
        (prof / "web").mkdir(parents=True)
        (prof / "web/cordis.patch.yml").write_text(
            "- id: llm\n  config:\n    providers:\n      anthropic:\n        displayName: Anthropic (Claude OAuth)\n"
            "        apiKeyEnv: ANTHROPIC_OAUTH_TOKEN\n        api: anthropic-messages\n"
            "        baseURL: https://api.anthropic.com\n      oauth-glm:\n        displayName: OAuth · GLM\n"
            "        apiKeyEnv: DSH_OAUTH_SUBS_API_KEY\n        compat:\n          forceAdaptiveThinking: true\n"
            "        baseURL: http://127.0.0.1:8318/glm\n      anthropic-api:\n        displayName: Anthropic\n"
            "        apiKeyEnv: ANTHROPIC_API_KEY\n        baseURL: https://api.anthropic.com\n")
        (prof / "tui").mkdir()
        (prof / "tui/cordis.patch.yml").write_text(
            "providers:\n  anthropic:\n    displayName: Anthropic\n    apiKeyEnv: ANTHROPIC_API_KEY\n")
        defs = {k.lower(): v for k, v in router.dsh_provider_defs(self.p / "dsh").items()}
        self.assertEqual(defs["oauth-glm"]["baseURL"], "http://127.0.0.1:8318/glm")  # after a nested block
        self.assertEqual(router.classify_provider("anthropic", defs), "oauth/claude-pro")  # web wins over tui
        self.assertEqual(router.classify_provider("anthropic-api", defs), "api/anthropic")
        self.assertEqual(router.classify_provider("oauth-glm", defs), "oauth/glm")
        allowed = router.load()["policy"]["allowed_billing"]["manager"]
        self.assertIn(router.classify_provider("anthropic", defs), allowed)
        self.assertNotIn(router.classify_provider("oauth-glm", defs), allowed)  # GLM can never be the manager


class BudgetHealthTest(Tmp):
    def cli(self, *args):
        env = dict(os.environ)
        return subprocess.run([str(ROOT / "bin/hydra-pod-dsh"), *args, "--project", str(self.p)],
                              capture_output=True, text=True, env=env, timeout=60)

    def cost(self, task, usd):
        (self.p / "_receipts").mkdir(exist_ok=True)
        with open(self.p / "_receipts" / f"{task}.costs.jsonl", "a") as f:
            f.write(json.dumps({"at": str(usd), "phase": "build", "provider": "opencode-go",
                                "model": "opencode-go/deepseek-v4.1-flash", "list_cost_usd": usd, "seconds": 60}) + "\n")

    def test_budget_levels(self):
        workflow.create(self.p, "T1", "o", budget={"max_cost_usd": 1.0})
        self.assertEqual(budget.check(self.p, "WF-T1")["level"], "ok")  # no run yet: 0 spent
        self.cost("T1", 0.85)
        from hydra_pod_dsh import resources
        resources.sync(self.p, "WF-T1")
        self.assertEqual(budget.check(self.p, "WF-T1")["level"], "warn")
        self.cost("T1", 0.2)
        resources.sync(self.p, "WF-T1")
        self.assertEqual(budget.check(self.p, "WF-T1")["level"], "block")

    def test_unreported_metric_is_unknown_not_ok(self):
        workflow.create(self.p, "T1", "o", budget={"max_cost_usd": 1.0})
        (self.p / "_receipts").mkdir(exist_ok=True)
        (self.p / "_receipts" / "T1.costs.jsonl").write_text(json.dumps(
            {"phase": "review", "provider": "zcode-lite", "model": "GLM-5.3", "zai_credits": 5}) + "\n")
        from hydra_pod_dsh import resources
        resources.sync(self.p, "WF-T1")
        b = budget.check(self.p, "WF-T1")
        self.assertEqual((b["level"], b["dimensions"][0]["used"]), ("unknown", None))

    def test_exhausted_budget_blocks_dispatch_exit_3(self):
        self.assertEqual(self.cli("wf", "start", "T1", "--objective", "o", "--budget-cost", "0.5").returncode, 0)
        for s in ("PLANNING", "PLAN_READY", "ASSIGNING"):
            self.cli("wf", "advance", "WF-T1", s)
        self.cost("T1", 0.6)
        r = self.cli("wf", "advance", "WF-T1", "EXECUTING")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("budget exhausted", r.stderr)
        w = workflow.get(self.p, "WF-T1")
        self.assertEqual((w.state, w.blocked_from), ("BLOCKED", "ASSIGNING"))
        self.assertEqual(workflow.timeline(self.p)[-1]["type"], "hydra/policy-decision")

    def test_unknown_budget_key_rejected(self):
        with self.assertRaises(workflow.TransitionError):
            workflow.create(self.p, "T1", "o", budget={"max_love": 1})
        with self.assertRaises(workflow.TransitionError):
            workflow.create(self.p, "T1", "o", budget={"max_cost_usd": -1})

    def test_health_and_recovery(self):
        workflow.create(self.p, "T1", "o")
        for s in TO_VERIFY[:4]:  # ... EXECUTING
            workflow.advance(self.p, "WF-T1", s)
        now = time.time()
        self.assertEqual(health.classify(self.p, "WF-T1", [], now)["health"], "starting")
        stalled = health.classify(self.p, "WF-T1", [], now + health.GRACE_SECONDS + 1)
        self.assertEqual((stalled["health"], stalled["recover_to"]), ("stalled", "ASSIGNING"))
        running = health.classify(self.p, "WF-T1", [{"cwd": str(self.p)}], now + 999)
        self.assertEqual(running["health"], "running")
        rec = self.p / "_receipts"
        rec.mkdir(exist_ok=True)
        (rec / "T1-x.receipt.md").write_text("done")
        self.assertEqual(health.classify(self.p, "WF-T1", [], now + 999)["health"], "ready")
        w = workflow.recover(self.p, "WF-T1", "worker died")
        self.assertEqual((w.state, w.recoveries), ("ASSIGNING", 1))
        with self.assertRaises(workflow.TransitionError):
            workflow.recover(self.p, "WF-T1", "again")

    def test_recover_refuses_unless_stalled(self):
        self.cli("wf", "start", "T1", "--objective", "o")
        for s in TO_VERIFY[:4]:
            self.cli("wf", "advance", "WF-T1", s)
        r = self.cli("wf", "recover", "WF-T1", "--reason", "x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not stalled", r.stderr)
        self.assertEqual(self.cli("wf", "recover", "WF-T1", "--reason", "x", "--force").returncode, 0)

    def test_blocked_is_waiting_for_user(self):
        workflow.create(self.p, "T1", "o")
        workflow.human(self.p, "WF-T1", "pause", "x")
        self.assertEqual(health.classify(self.p, "WF-T1", [])["health"], "waiting_for_user")


class CheckpointTest(Tmp):
    def test_git_head_recorded_on_moves(self):
        subprocess.run(["git", "init", "-q", str(self.p)], check=True)
        subprocess.run(["git", "-C", str(self.p), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                        "--allow-empty", "-m", "c"], check=True)
        head = subprocess.run(["git", "-C", str(self.p), "rev-parse", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
        workflow.create(self.p, "T1", "o")
        w = workflow.advance(self.p, "WF-T1", "PLANNING")
        self.assertEqual(w.git_head, head)
        self.assertEqual(workflow.timeline(self.p)[-1]["payload"]["git_head"], head)

    def test_no_git_is_fine(self):
        workflow.create(self.p, "T1", "o")
        self.assertIsNone(workflow.advance(self.p, "WF-T1", "PLANNING").git_head)


class ConsistencyReportTest(Tmp):
    def ticket(self, folder, name):
        d = self.p / "_tickets" / folder
        d.mkdir(parents=True, exist_ok=True)
        for other in ("open", "doing", "done", "blocked", "dropped"):
            f = self.p / "_tickets" / other / name
            if f.exists():
                f.unlink()
        (d / name).write_text("ticket")

    def test_consistent_and_drift_cases(self):
        from hydra_pod_dsh import consistency
        workflow.create(self.p, "T1", "o")
        self.assertEqual(consistency.check(self.p), [])              # NEW: no ticket file yet is fine
        for s in ("PLANNING", "PLAN_READY"):
            workflow.advance(self.p, "WF-T1", s)
        self.assertEqual(consistency.check(self.p)[0]["kind"], "missing-ticket")
        self.ticket("open", "T1-x.md")
        self.assertEqual(consistency.check(self.p), [])
        workflow.advance(self.p, "WF-T1", "ASSIGNING")
        self.assertEqual(consistency.check(self.p)[0]["kind"], "folder-mismatch")  # claim not run
        self.ticket("doing", "T1-x.md")
        self.assertEqual(consistency.check(self.p), [])
        self.ticket("doing", "T9-stray.md")
        self.assertEqual([p["kind"] for p in consistency.check(self.p)], ["untracked-ticket"])

    def test_approved_with_ticket_already_done_is_not_drift(self):
        from hydra_pod_dsh import consistency
        workflow.create(self.p, "T1", "o")
        for s in TO_VERIFY:
            workflow.advance(self.p, "WF-T1", s)
        workflow.decide(self.p, "WF-T1", "APPROVE", "ok")
        self.ticket("done", "T1.md")
        self.assertEqual(consistency.check(self.p), [])

    def test_report_has_every_section_and_marks_unknowns(self):
        from hydra_pod_dsh import report
        workflow.create(self.p, "T1", "count things", budget={"max_cost_usd": 1})
        for s in TO_VERIFY:
            workflow.advance(self.p, "WF-T1", s)
        workflow.decide(self.p, "WF-T1", "APPROVE", "all acceptance checks pass")
        (self.p / "_receipts").mkdir(exist_ok=True)
        (self.p / "_receipts" / "T1.review.md").write_text(REVIEW)
        text = report.build(self.p, "WF-T1")
        for part in ("# WF-T1: count things", "## Timeline", "## Workers (cost log)", "## Manager (DSH session logs)",
                     "## Budget: ok", "## Findings", "## Consistency", "APPROVE", "all acceptance checks pass"):
            self.assertIn(part, text)
        self.assertIn("unknown", text)            # no cost log: unknown, never $0
        self.assertIn("missing-ticket", text)      # no ticket file in this fixture

    def test_cli_check_exit_4_and_report_write(self):
        env = dict(os.environ)
        run = lambda *a: subprocess.run([str(ROOT / "bin/hydra-pod-dsh"), *a, "--project", str(self.p)],
                                        capture_output=True, text=True, env=env, timeout=60)
        run("wf", "start", "T1", "--objective", "o")
        for s in ("PLANNING", "PLAN_READY"):
            run("wf", "advance", "WF-T1", s)
        self.assertEqual(run("wf", "check").returncode, 4)
        r = run("wf", "report", "WF-T1", "--write")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.p / "_receipts" / "WF-T1.report.md").exists())


class TokensTest(unittest.TestCase):
    def test_normalize_aliases_and_work_excludes_cache(self):
        from hydra_pod_dsh import tokens
        a = tokens.normalize({"input": 10, "output": 5, "reasoning": 2, "cache_read": 1000})
        b = tokens.normalize({"inputTokens": 1, "outputTokens": 1, "cacheReadTokens": 50, "cacheWriteTokens": 7})
        s = tokens.add(a, b)
        self.assertEqual(tokens.work(s), 19)
        self.assertEqual((s["cache_read"], s["cache_write"]), (1050, 7))
        self.assertIn("cache 1.1kr/7w", tokens.fmt(s))


class StagesTest(Tmp):
    GO = {"windows": [{"window": "5h", "limit_usd": 12.0, "percent": 3.0, "resets_at": 5000.0},
                      {"window": "week", "limit_usd": 30.0, "percent": 10.0, "resets_at": 9000.0}]}
    ZAI = {"windows": [{"window": "5h", "limit": 2000, "percent": 2.0, "resets_at": 4000.0},
                       {"window": "week", "limit": 10000, "percent": 70.0, "resets_at": 8000.0}]}

    def cost(self, task, **run):
        (self.p / "_receipts").mkdir(exist_ok=True)
        with open(self.p / "_receipts" / f"{task}.costs.jsonl", "a") as f:
            f.write(json.dumps(run) + "\n")

    def iso(self, t):
        import datetime
        return datetime.datetime.fromtimestamp(t).astimezone().isoformat()

    def test_runs_and_manager_land_in_their_stage_with_quota_share(self):
        from hydra_pod_dsh import stages
        workflow.create(self.p, "T1", "o")
        for s in TO_VERIFY[:4]:
            workflow.advance(self.p, "WF-T1", s)                  # ... EXECUTING
        t_exec = time.time()
        workflow.advance(self.p, "WF-T1", "TESTING")
        workflow.advance(self.p, "WF-T1", "REVIEWING")
        t_rev = time.time()
        workflow.advance(self.p, "WF-T1", "VERIFYING")
        self.cost("T1", at=self.iso(t_exec), phase="build", provider="opencode-go",
                  model="opencode-go/deepseek-v4.1-flash", list_cost_usd=0.6,
                  tokens={"input": 100, "output": 10, "cache_read": 5000})
        self.cost("T1", at=self.iso(t_rev), phase="review", provider="opencode/zai-coding-plan",
                  model="glm-5.3", zai_credits=40, tokens={"input": 50, "output": 5})
        mgr = {"sessions": ["s"], "records": [{"time": t_rev + 0.0001, "provider": "anthropic",
                                               "model": "claude-opus-5-5", "tokens": {"input": 7, "output": 3,
                                               "reasoning": 0, "cache_read": 0, "cache_write": 0}}]}
        st = stages.build(self.p, "WF-T1", go={"opencode-go/deepseek-v4.1-flash": self.GO}, zai=self.ZAI, manager=mgr)
        by_state = {s["state"]: s for s in st["stages"]}
        ex = by_state["EXECUTING"]["actors"][0]
        self.assertEqual((ex["role"], ex["work_tokens"], ex["tokens"]["cache_read"]), ("executor", 110, 5000))
        self.assertEqual((ex["quota"]["5h"]["percent"], ex["quota"]["source"]), (5.0, "estimate"))
        roles = {a["role"]: a for a in by_state["REVIEWING"]["actors"]}
        self.assertEqual(roles["reviewer"]["quota"]["5h"]["percent"], 2.0)          # 40 / 2000
        self.assertEqual(roles["reviewer"]["quota"]["week"]["resets_at"], 8000.0)
        self.assertEqual((roles["manager"]["billing"], roles["manager"]["work_tokens"]), ("api/anthropic", 10))
        self.assertIsNone(roles["manager"]["quota"])                               # pay per use: no windows

    def test_rework_gives_a_second_executing_row_and_old_runs_fall_back_by_phase(self):
        from hydra_pod_dsh import stages
        workflow.create(self.p, "T1", "o")
        for s in TO_VERIFY:
            workflow.advance(self.p, "WF-T1", s)
        workflow.decide(self.p, "WF-T1", "REWORK", "x")
        workflow.advance(self.p, "WF-T1", "EXECUTING")
        self.cost("T1", phase="build", provider="opencode-go", model="opencode-go/deepseek-v4.1-flash",
                  list_cost_usd=0.1)   # no `at`: falls back to the last EXECUTING
        st = stages.build(self.p, "WF-T1", go={}, zai={"windows": []}, manager={"sessions": [], "records": []})
        execs = [s for s in st["stages"] if s["state"] == "EXECUTING"]
        self.assertEqual(len(execs), 2)
        self.assertEqual((len(execs[0]["actors"]), len(execs[1]["actors"])), (0, 1))

    def test_status_is_read_only_for_the_ledger(self):
        workflow.create(self.p, "T1", "o")
        workflow.advance(self.p, "WF-T1", "PLANNING")
        self.cost("T1", phase="build", provider="opencode-go", model="m", list_cost_usd=0.1)
        from hydra_pod_dsh import cli, ledger as lg
        before = lg.ledger_path(self.p).read_text()
        st = cli.status(time.time(), workers=[], stage={"stage": "planning", "project": str(self.p), "at": time.time(),
                                                        "by": "x", "ticket": "T1"}, go={"windows": []},
                        zai={"windows": []})
        self.assertIn("stages", st["workflows"][0])
        self.assertEqual(lg.ledger_path(self.p).read_text(), before)


if __name__ == "__main__":
    unittest.main()
