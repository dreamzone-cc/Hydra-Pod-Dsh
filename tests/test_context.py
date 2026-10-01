# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the context-economy tools (technical paper phase 0 and 1): diff summary,
resume brief, cache hit ratio and the benchmark report."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_pod_dsh import bench, brief, diffsum, tokens, workflow  # noqa: E402

CLI = [sys.executable, "-B", str(ROOT / "bin/hydra-pod-dsh")]
TO_VERIFY = ["PLANNING", "PLAN_READY", "ASSIGNING", "EXECUTING", "TESTING", "REVIEWING", "VERIFYING"]


def git(p, *args):
    return subprocess.run(["git", "-C", str(p), *args], capture_output=True, text=True, check=True).stdout.strip()


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.p / ".cache"), "DSH_HOME": str(self.p / ".dsh"),
                                           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        env.start()
        self.addCleanup(env.stop)
        git(self.p, "init", "-q")
        (self.p / "src").mkdir()
        (self.p / "src/text.py").write_text("def old_helper():\n    return 1\n\n\ndef keep():\n    return 2\n")
        (self.p / "README.md").write_text("x\n")
        git(self.p, "add", "-A")
        git(self.p, "commit", "-qm", "base")
        self.base = git(self.p, "rev-parse", "HEAD")

    def ticket(self, task="T1", folder="doing", head=""):
        d = self.p / "_tickets" / folder
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{task}-thing.md").write_text(
            f"---\nticket: {task}-thing\nstate: doing\nbase: {self.base}\nhead: {head}   # set by accept\n"
            "test_command: true\nallowed_files:\n  - src/text.py\n  - tests/\nacceptance:\n  - true\n---\nbody\n")

    def change(self):
        (self.p / "src/text.py").write_text("def keep():\n    return 3\n\n\nclass Counter:\n    pass\n")
        (self.p / "tests").mkdir()
        (self.p / "tests/test_text.py").write_text("def test_counter():\n    assert True\n")
        (self.p / "README.md").write_text("changed\n")


class DiffSumTest(Repo):
    def test_scope_symbols_and_tests_from_the_ticket_header(self):
        self.ticket()
        self.change()
        s = diffsum.summarize(self.p, "T1")
        self.assertEqual(s["head"], "WORKTREE")  # empty head: header comment is not a value
        files = {f["path"]: f for f in s["files"]}
        # the builder's new test file is untracked before accept, and still counted; tickets are not
        self.assertEqual(set(files), {"src/text.py", "README.md", "tests/test_text.py"})
        new = files["tests/test_text.py"]
        self.assertEqual((new["added"], new["symbols_added"], new["in_scope"]), (2, ["test_counter"], True))
        self.assertEqual(s["out_of_scope"], ["README.md"])
        self.assertEqual(s["files"][0]["path"], "README.md")  # out-of-scope sorted first
        t = files["src/text.py"]
        self.assertEqual((t["symbols_added"], t["symbols_removed"]), (["Counter"], ["old_helper"]))
        self.assertIn("src/text.py", s["read_first"])  # a removed symbol needs judgment
        self.assertIn("OUT OF SCOPE: README.md", diffsum.render(s))

    def test_committed_range_and_test_detection(self):
        self.change()
        git(self.p, "add", "-A")
        git(self.p, "commit", "-qm", "work")
        head = git(self.p, "rev-parse", "HEAD")
        self.ticket(head=head)
        s = diffsum.summarize(self.p, "T1")
        files = {f["path"]: f for f in s["files"]}
        self.assertTrue(files["tests/test_text.py"]["test"])
        self.assertTrue(files["tests/test_text.py"]["in_scope"])  # a directory entry covers its files
        self.assertEqual(s["totals"]["test_files"], 1)

    def test_without_allowed_files_scope_is_not_judged(self):
        self.change()
        s = diffsum.summarize(self.p, None, base=self.base)
        self.assertEqual(s["out_of_scope"], [])
        self.assertTrue(all(f["in_scope"] is None for f in s["files"]))
        self.assertIn("not checked", diffsum.render(s))

    def test_no_base_is_a_clean_error(self):
        with self.assertRaises(ValueError):
            diffsum.summarize(self.p, "T9")

    def test_cli(self):
        self.ticket()
        self.change()
        workflow.create(self.p, "T1", "obj")
        r = subprocess.run(CLI + ["wf", "diffsum", "WF-T1", "--project", str(self.p), "--json"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["out_of_scope"], ["README.md"])
        r = subprocess.run(CLI + ["wf", "diffsum", "WF-T1", "--task", "../x", "--project", str(self.p)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)


class BriefTest(Repo):
    def test_brief_lists_state_open_findings_and_next_moves(self):
        workflow.create(self.p, "T1", "count vowels")
        for s in TO_VERIFY:
            workflow.advance(self.p, "WF-T1", s)
        (self.p / "_receipts").mkdir(exist_ok=True)
        (self.p / "_receipts/T1.review.md").write_text("**high | src/text.py:2 | wrong value | e | f**\n")
        b = brief.build(self.p, "WF-T1")
        self.assertEqual(b["next"], list(workflow.DECISIONS))
        self.assertEqual(len(b["findings"]["unverified"]), 1)
        text = brief.render(b)
        self.assertIn("WF-T1: VERIFYING — count vowels", text)
        self.assertIn("UNVERIFIED [high]", text)
        self.assertLessEqual(len(text.splitlines()), 30)

    def test_terminal_and_blocked(self):
        workflow.create(self.p, "T1", "o")
        workflow.human(self.p, "WF-T1", "cancel", "no longer needed")
        self.assertIn("none (terminal)", brief.render(brief.build(self.p, "WF-T1")))


class CacheRatioTest(unittest.TestCase):
    def test_ratio(self):
        self.assertEqual(tokens.cache_hit_ratio({"input": 100, "cache_read": 900}), 0.9)
        self.assertEqual(tokens.cache_hit_ratio({"input": 50, "cache_read": 0, "cache_write": 50}), 0.0)
        self.assertIsNone(tokens.cache_hit_ratio(tokens.zero()))


class BenchTest(Repo):
    def finish(self, ticket, rework=False):
        wid = f"WF-{ticket}"
        workflow.create(self.p, ticket, "o")
        for s in TO_VERIFY:
            workflow.advance(self.p, wid, s)
        if rework:
            workflow.decide(self.p, wid, "REWORK", "fix it", task_id=f"{ticket}b")
            for s in ("EXECUTING", "TESTING", "REVIEWING", "VERIFYING"):
                workflow.advance(self.p, wid, s)
        workflow.decide(self.p, wid, "APPROVE", "ok")
        workflow.advance(self.p, wid, "DONE")

    def test_report_and_compare(self):
        self.finish("T1")
        self.finish("T2", rework=True)
        r = bench.report([self.p])
        self.assertEqual(r["done"], 2)
        self.assertEqual(r["summary"]["first_try_rate"], 0.5)
        self.assertIsNone(r["summary"]["manager_work_tokens"])  # no DSH session: unknown, not zero
        self.assertIsNotNone(r["summary"]["plan_to_done_seconds"])
        base = {"summary": {"executor_work_tokens": 1000, "first_try_rate": 0.5, "manager_work_tokens": None}}
        new = {"summary": {"executor_work_tokens": 700, "first_try_rate": 0.4, "manager_work_tokens": 10}}
        rows = {x["metric"]: x for x in bench.compare(base, new)}
        self.assertEqual((rows["executor_work_tokens"]["change"], rows["executor_work_tokens"]["ok"]), (-0.3, True))
        self.assertFalse(rows["first_try_rate"]["ok"])
        self.assertIsNone(rows["manager_work_tokens"]["ok"])
        self.assertIn("WORSE", bench.render_compare(bench.compare(base, new)))
        self.assertIn("2 done", bench.render(r))

    def test_cli_save_and_compare(self):
        self.finish("T1")
        out = self.p / "base.json"
        r = subprocess.run(CLI + ["bench", "report", "--project", str(self.p), "--save", str(out)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(CLI + ["bench", "compare", str(out), str(out), "--json"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(all(m["ok"] in (True, None) for m in json.loads(r.stdout)["metrics"]))


if __name__ == "__main__":
    unittest.main()
