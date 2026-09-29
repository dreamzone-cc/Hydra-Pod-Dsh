# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for hydra_pod_dsh (live stage, worker detection, usage, status rendering)."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_pod_dsh import cli, live, usage  # noqa: E402

NOW = 1_800_000_000.0
BUILD = ["opencode", "run", "--standalone", "--format", "json", "-m", "opencode-go/deepseek-v4.1-flash", "--auto"]
REVIEW = ["opencode", "run", "--standalone", "--format", "json", "-m", "zai-coding-plan/glm-5.3", "--agent", "reviewer"]


class TmpCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.tmp.name})
        p.start()
        self.addCleanup(p.stop)


class ClassifyTest(unittest.TestCase):
    def test_builder_and_reviewer(self):
        self.assertEqual(live.classify(BUILD)["role"], "builder")
        self.assertEqual(live.classify(BUILD)["model"], "opencode-go/deepseek-v4.1-flash")
        self.assertEqual(live.classify(REVIEW)["role"], "reviewer")

    def test_node_wrapped_opencode_and_zcode(self):
        self.assertEqual(live.classify(["node", "/x/bin/opencode", "run", "-m", "a/b"])["model"], "a/b")
        z = live.classify(["python3", "/x/bin/hydra-pod-connect", "review", "zcode-lite", "--prompt-file", "p"])
        self.assertEqual((z["role"], z["tool"]), ("reviewer", "zcode"))

    def test_unrelated_commands_ignored(self):
        self.assertIsNone(live.classify(["opencode", "acp"]))
        self.assertIsNone(live.classify(["vim", "run", "-m", "x"]))

    def test_dispatch_names_ticket_and_duplicates_collapse(self):
        procs = [(10, ["python3", "/b/hydra-pod-dispatch", "build", "T6"], "/p", 1.0),
                 (11, BUILD, "/p", 2.0), (12, BUILD, "/p", 2.1)]
        ws = live.running_workers(procs)
        self.assertEqual(len(ws), 1)
        self.assertEqual((ws[0]["role"], ws[0]["ticket"]), ("builder", "T6"))

    def test_dispatch_before_worker_starts(self):
        ws = live.running_workers([(10, ["hydra-pod-dispatch", "review", "T7"], "/p", 1.0)])
        self.assertEqual((ws[0]["role"], ws[0]["ticket"], cli.provider_of(ws[0])), ("reviewer", "T7", "zai"))


class StageTest(TmpCache):
    def test_set_read_clear(self):
        live.set_stage("T6", "validating", "/p", now=NOW)
        self.assertEqual(live.read_stage(NOW + 10)["stage"], "validating")
        live.clear_stage()
        self.assertIsNone(live.read_stage(NOW))

    def test_stale_stage_ignored(self):
        live.set_stage("T6", "planning", "/p", now=NOW)
        self.assertIsNone(live.read_stage(NOW + live.STAGE_TTL + 1))


def make_db(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE session_message (id TEXT, time_created INTEGER, data TEXT)")
    for t, provider, model, cost in rows:
        con.execute("INSERT INTO session_message VALUES ('x', ?, ?)",
                    (int(t * 1000), json.dumps({"model": {"providerID": provider, "id": model}, "cost": cost})))
    con.commit()
    con.close()


class GoUsageTest(unittest.TestCase):
    def test_windows_limits_and_rolling_reset(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "o.db"
            make_db(db, [(NOW - 3600, "opencode-go", "deepseek-v4.1-flash", 3.0),      # in 5h
                         (NOW - 2 * 86400, "opencode-go", "deepseek-v4.1-flash", 6.0),  # in week
                         (NOW - 3600, "zai-coding-plan", "glm-5.3", 99.0),              # other provider
                         (NOW - 40 * 86400, "opencode-go", "deepseek-v4.1-flash", 50.0)])  # too old
            u = usage.go_usage("opencode-go/deepseek-v4.1-flash", NOW, db)
        w = {x["window"]: x for x in u["windows"]}
        self.assertEqual((w["5h"]["used_usd"], w["5h"]["limit_usd"], w["5h"]["percent"]), (3.0, 12.0, 25.0))
        self.assertEqual((w["week"]["used_usd"], w["week"]["limit_usd"]), (9.0, 30.0))
        self.assertEqual(w["5h"]["resets_at"], NOW - 3600 + 5 * 3600)
        self.assertEqual(u["source"], "estimate")

    def test_unknown_model_and_missing_db(self):
        self.assertIn("no monthly limit", usage.go_usage("opencode-go/nope", NOW)["error"])
        u = usage.go_usage("opencode-go/deepseek-v4.1-flash", NOW, Path("/nonexistent/o.db"))
        self.assertIn("unreadable", u["error"])

    def test_unreadable_limits_json_degrades(self):
        with mock.patch.object(usage, "LIMITS_FILE", Path("/nonexistent/limits.json")):
            u = usage.go_usage("opencode-go/deepseek-v4.1-flash", NOW)
        self.assertIn("unreadable", u["error"])
        self.assertEqual(u["windows"], [])


class ZaiUsageTest(TmpCache):
    Q = {"level": "lite", "windows": [
        {"window": "5h", "used": 500, "total": 2000, "remaining": 1500, "resets_at": NOW + 60},
        {"window": "1w", "used": 9000, "total": 10000, "remaining": 1000, "resets_at": NOW + 600}]}

    def test_maps_windows_and_caches(self):
        calls = []
        fetch = lambda: calls.append(1) or self.Q  # noqa: E731
        u = usage.zai_usage(NOW, fetch)
        self.assertEqual([(w["window"], w["percent"]) for w in u["windows"]], [("5h", 25.0), ("week", 90.0)])
        usage.zai_usage(NOW + 30, fetch)
        self.assertEqual(len(calls), 1)
        usage.zai_usage(NOW + usage.ZAI_CACHE_TTL + 1, fetch)
        self.assertEqual(len(calls), 2)

    def test_cache_never_holds_the_key(self):
        usage.zai_usage(NOW, lambda: self.Q)
        text = (usage.cache_dir() / "zai-quota.json").read_text()
        self.assertNotIn("key", text.lower())

    def test_unavailable(self):
        self.assertIn("unavailable", usage.zai_usage(NOW, lambda: None)["error"])

    def test_missing_hydra_pod_checkout_degrades(self):
        def no_module():
            raise ImportError("No module named 'hydra_pod'")
        self.assertIn("not importable", usage.zai_usage(NOW, no_module)["error"])

    def test_quota_backend_failure_degrades(self):
        def down():
            raise OSError("network down")
        self.assertIn("unavailable", usage.zai_usage(NOW, down)["error"])

    def test_malformed_quota_shape_degrades(self):
        u = usage.zai_usage(NOW, lambda: {"level": "lite", "windows": [{"window": "5h"}]})
        self.assertIn("malformed", u["error"])
        self.assertEqual(u["windows"], [])


class StatusTest(unittest.TestCase):
    GO = {"provider": "OpenCode Go", "model": "m", "source": "estimate",
          "windows": [{"window": "5h", "used_usd": 1.0, "limit_usd": 12.0, "percent": 8.3, "resets_at": NOW + 3600}]}
    ZAI = {"provider": "Z.ai GLM Coding Plan", "model": "z", "source": "official",
           "windows": [{"window": "week", "used": 1, "limit": 10, "remaining": 9, "percent": 10.0, "resets_at": None}]}

    def st(self, workers, stage):
        return cli.status(NOW, workers, stage, self.GO, self.ZAI)

    def test_worker_beats_manager_stage(self):
        w = dict(live.classify(BUILD), pid=1, cwd="/p", since=NOW, ticket="T6")
        s = self.st([w], {"stage": "validating", "by": "Opus", "ticket": "T6", "at": NOW})
        self.assertEqual((s["active"]["role"], s["active"]["subscription"]), ("builder", "opencode-go"))
        text = cli.render(s)
        self.assertIn("now: builder", text)
        self.assertIn("◀ in use", text.split("Z.ai")[0])

    def test_manager_then_idle(self):
        s = self.st([], {"stage": "planning", "by": "Opus", "ticket": None, "at": NOW, "project": "/p"})
        self.assertIn("now: manager (planning) — Opus", cli.render(s))
        self.assertIn("now: idle", cli.render(self.st([], None)))

    def test_cli_json_runs(self):
        env = dict(os.environ, OPENCODE_DB="/nonexistent/o.db", ZAI_KEY_FILE="/nonexistent/key")
        with tempfile.TemporaryDirectory() as d:
            env["XDG_CACHE_HOME"] = d
            r = subprocess.run([str(ROOT / "bin/hydra-pod-dsh"), "status", "--json"],
                               capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("usage", json.loads(r.stdout))

    def test_unreadable_project_records_degrade_to_ledger_error(self):
        stage = {"stage": "planning", "by": "Opus", "ticket": None, "at": NOW, "project": "/p"}
        with mock.patch.object(cli.workflow, "load", side_effect=UnicodeDecodeError("utf-8", b"", 0, 1, "bad")):
            st = cli.status(NOW, [], stage, self.GO, self.ZAI)
        self.assertIn("UnicodeDecodeError", st["ledger_error"])
        self.assertEqual(st["workflows"], [])


if __name__ == "__main__":
    unittest.main()
