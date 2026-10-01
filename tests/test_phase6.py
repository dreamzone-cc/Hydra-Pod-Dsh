# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for phase 6 of the technical paper: the self-evolving skills library and the steward's memory."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_pod_dsh import ledger, pack, skills, steward, workflow  # noqa: E402

CLI = [sys.executable, "-B", str(ROOT / "bin/hydra-pod-dsh")]
BODY = "Add a test for empty input, whitespace-only input, tabs, and mixed case for every new function."
TO_VERIFY = ["PLANNING", "PLAN_READY", "ASSIGNING", "EXECUTING", "TESTING", "REVIEWING", "VERIFYING"]


def git(p, *args):
    subprocess.run(["git", "-C", str(p), *args], capture_output=True, check=True)


class Base(unittest.TestCase):
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
        (self.p / "src/text.py").write_text("def words(t):\n    return t.split()\n")
        (self.p / "tests").mkdir()
        (self.p / "tests/test_text.py").write_text("from text import words\n\n\ndef test_w():\n    assert words('a')\n")
        git(self.p, "add", "-A")
        git(self.p, "commit", "-qm", "base")

    def skill(self, name="edge-tests", **kw):
        return skills.add(self.p, name, "Edge-case tests for text functions", BODY,
                          kw.get("paths", "src/*.py"), kw.get("keywords", "tokenize"), "seen in 3 tickets")


class LifecycleTest(Base):
    def test_candidate_trial_active(self):
        self.assertEqual(self.skill()["state"], "candidate")
        self.assertEqual(skills.match(self.p, ["src/text.py"]), [])  # a candidate is never attached
        skills.approve(self.p, "edge-tests", "matches T1-T3")
        self.assertEqual(skills.match(self.p, ["src/text.py"]), ["edge-tests"])
        self.assertEqual(skills.match(self.p, ["docs/x.md"], "please Tokenize this"), ["edge-tests"])
        for i in range(3):
            s = skills.outcome(self.p, "edge-tests", True, f"WF-T{i}", i)
        self.assertEqual(s["state"], "active")
        changes = [e["payload"]["change"] for e in ledger.read(self.p) if e["type"] == skills.EVENT]
        self.assertEqual(changes, ["added", "approved", "outcome", "outcome", "outcome"])

    def test_trial_failure_goes_to_review_and_revision_restarts(self):
        self.skill()
        skills.approve(self.p, "edge-tests", "ok")
        self.assertEqual(skills.outcome(self.p, "edge-tests", False, "WF-T1", 1)["state"], "review")
        self.assertEqual(skills.match(self.p, ["src/text.py"]), [])
        s = skills.revise(self.p, "edge-tests", BODY + " Also cover unicode.", "failed on T1: unicode")
        self.assertEqual((s["state"], s["version"], s["outcomes"]), ("candidate", 2, []))

    def test_active_demoted_when_its_success_rate_drops(self):
        self.skill()
        skills.approve(self.p, "edge-tests", "ok")
        for i in range(3):
            skills.outcome(self.p, "edge-tests", True, "WF-A", i)
        for i, ok in enumerate((False, False, False)):
            s = skills.outcome(self.p, "edge-tests", ok, "WF-B", 10 + i)
        self.assertEqual(s["state"], "review")  # 2 of the last 5 succeeded: below 60%

    def test_one_outcome_per_pack_and_refusals(self):
        self.skill()
        skills.approve(self.p, "edge-tests", "ok")
        skills.outcome(self.p, "edge-tests", True, "WF-T1", 7)
        skills.outcome(self.p, "edge-tests", True, "WF-T1", 7)
        self.assertEqual(len(skills.load(self.p)["skills"]["edge-tests"]["outcomes"]), 1)
        for bad in (dict(name="Bad Name"), dict(paths="", keywords="")):
            with self.assertRaises(ValueError):
                self.skill(**bad)
        with self.assertRaises(ValueError):
            self.skill()  # exists
        with self.assertRaises(ValueError):
            skills.approve(self.p, "edge-tests", "again")  # not a candidate


class PackAndDecideTest(Base):
    def test_pack_attaches_skills_and_decide_scores_them(self):
        self.skill()
        skills.approve(self.p, "edge-tests", "ok")
        steward.init(self.p)
        steward.set_section(self.p, "Invariants", "- words() splits on any whitespace", "from T0")
        d = self.p / "_tickets/open"
        d.mkdir(parents=True)
        (d / "T1-x.md").write_text("---\nticket: T1-x\nallowed_files:\n  - src/text.py\n---\nbody\n")
        workflow.create(self.p, "T1", "o")
        out = pack.write(self.p, "WF-T1")
        self.assertEqual(out["skills"], ["edge-tests"])
        text = (self.p / out["path"]).read_text()
        self.assertIn("### Invariants\n\n- words() splits on any whitespace", text)
        self.assertNotIn("not written yet", text)
        self.assertLess(text.index("Project memory"), text.index("Files this ticket changes"))  # stable prefix first
        for s in TO_VERIFY:
            workflow.advance(self.p, "WF-T1", s)
        r = subprocess.run(CLI + ["wf", "decide", "WF-T1", "APPROVE", "--reason", "ok", "--project", str(self.p)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("skill edge-tests: success recorded (trial)", r.stdout)
        self.assertEqual([o["ok"] for o in skills.load(self.p)["skills"]["edge-tests"]["outcomes"]], [True])


class StewardTest(Base):
    def test_init_keeps_manager_sections_and_limits_size(self):
        steward.init(self.p)
        self.assertIn("python3 -B -m unittest discover -s tests", steward.core(self.p))
        steward.set_section(self.p, "Conventions", "- snake_case everywhere", "observed")
        steward.init(self.p)  # regenerating facts keeps what the manager wrote
        self.assertEqual(steward.read_sections(self.p)["Conventions"], "- snake_case everywhere")
        with self.assertRaises(ValueError):
            steward.set_section(self.p, "Architecture", "x" * 9000, "too much")
        with self.assertRaises(ValueError):
            steward.set_section(self.p, "Secrets", "x", "not a section")
        with self.assertRaises(ValueError):
            steward.set_section(self.p, "Conventions", "x", " ")

    def test_cli(self):
        body = self.p / "b.md"
        body.write_text(BODY)
        run = lambda *a: subprocess.run(CLI + list(a) + ["--project", str(self.p)], capture_output=True, text=True)  # noqa
        self.assertEqual(run("skill", "add", "edge-tests", "--description", "d", "--file", str(body),
                             "--paths", "src/*.py").returncode, 0)
        self.assertIn("edge-tests", run("skill", "list").stdout)
        self.assertEqual(run("skill", "approve", "edge-tests").returncode, 2)  # a reason is required
        self.assertEqual(run("steward", "init").returncode, 0)
        self.assertIn("Facts (generated)", run("steward", "show").stdout)


if __name__ == "__main__":
    unittest.main()
