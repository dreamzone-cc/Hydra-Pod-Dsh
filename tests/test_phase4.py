# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for phase 4 of the technical paper: approved plans, dependency waves and the EXECUTING gate."""
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

from hydra_pod_dsh import ledger, plan, workflow  # noqa: E402

CLI = [sys.executable, "-B", str(ROOT / "bin/hydra-pod-dsh")]
TO_ASSIGNING = ["PLANNING", "PLAN_READY", "ASSIGNING"]
REST = ["EXECUTING", "TESTING", "REVIEWING", "VERIFYING"]


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.p / ".cache")})
        env.start()
        self.addCleanup(env.stop)
        # T1 and T2 touch different files; T3 shares T1's file; T4 needs T1 and T2.
        for t, files in {"T1": ["src/a.py"], "T2": ["src/b.py"], "T3": ["src/a.py"], "T4": ["src/c/"]}.items():
            d = self.p / "_tickets/open"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{t}-x.md").write_text("---\nticket: x\nallowed_files:\n" + "".join(f"  - {f}\n" for f in files)
                                         + "---\n")

    def cli(self, *args):
        return subprocess.run(CLI + list(args) + ["--project", str(self.p)], capture_output=True, text=True)

    def test_waves_keep_dependencies_and_separate_shared_files(self):
        out = plan.approve(self.p, "feature", ["T1", "T2", "T3", "T4:T1,T2"], "split in four")
        self.assertEqual(out["waves"], [["T1", "T2"], ["T3", "T4"]])  # T3 waits for T1's file, T4 for its deps
        ev = [e for e in ledger.read(self.p) if e["type"] == plan.EVENT]
        self.assertTrue(ev[0]["ignorable"])
        self.assertEqual(workflow.load(self.p), {})  # a plan is not a workflow; readers skip it

    def test_refusals_write_nothing(self):
        for specs, msg in ((["T1:T2", "T2:T1"], "cycle"), (["T1:T9"], "not in the plan"),
                           (["T1", "T1"], "twice"), (["T7"], "no ticket T7"), (["../x"], "ticket id")):
            with self.assertRaises((ValueError, workflow.TransitionError)) as e:
                plan.approve(self.p, "p", specs, "r")
            self.assertIn(msg, str(e.exception))
        with self.assertRaises(ValueError):
            plan.approve(self.p, "p", ["T1"], " ")
        self.assertEqual(ledger.read(self.p), [])

    def test_status_and_executing_gate(self):
        plan.approve(self.p, "feature", ["T1", "T2", "T4:T1,T2"], "r")
        for t in ("T1", "T2", "T4"):
            workflow.create(self.p, t, "o")
            for s in TO_ASSIGNING:
                workflow.advance(self.p, f"WF-{t}", s)
        st = plan.status(self.p, "feature")
        self.assertEqual((st["ready"], st["start_now"]), (["T1", "T2"], ["T1", "T2"]))
        r = self.cli("wf", "advance", "WF-T4", "EXECUTING")
        self.assertEqual(r.returncode, 2)
        self.assertIn("wait for T1, T2", r.stderr)
        for t in ("T1", "T2"):
            self.assertEqual(self.cli("wf", "advance", f"WF-{t}", "EXECUTING").returncode, 0)
            for s in REST[1:]:
                workflow.advance(self.p, f"WF-{t}", s)
            workflow.decide(self.p, f"WF-{t}", "APPROVE", "ok")
            workflow.advance(self.p, f"WF-{t}", "DONE")
        self.assertEqual(plan.status(self.p, "feature")["start_now"], ["T4"])
        self.assertEqual(self.cli("wf", "advance", "WF-T4", "EXECUTING").returncode, 0)

    def test_fix_ticket_follows_its_workflow(self):
        plan.approve(self.p, "f", ["T1", "T4:T1"], "r")
        workflow.create(self.p, "T4", "o")
        for s in TO_ASSIGNING:
            workflow.advance(self.p, "WF-T4", s)
        self.assertEqual(plan.blocked_by(self.p, "T4"), ["T1"])
        self.assertEqual(plan.blocked_by(self.p, "T2"), [])  # not in any plan: never gated

    def test_running_ticket_keeps_overlapping_one_out_of_the_batch(self):
        plan.approve(self.p, "f", ["T1", "T3"], "r")   # no dependency, same file
        workflow.create(self.p, "T1", "o")
        for s in TO_ASSIGNING + ["EXECUTING"]:
            workflow.advance(self.p, "WF-T1", s)
        st = plan.status(self.p, "f")
        self.assertEqual((st["ready"], st["start_now"]), (["T3"], []))

    def test_cli(self):
        r = self.cli("plan", "approve", "feature", "--ticket", "T1", "--ticket", "T2:T1", "--reason", "two steps")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[T1] → [T2]", r.stdout)
        out = json.loads(self.cli("plan", "show", "feature", "--json").stdout)
        self.assertEqual(out["start_now"], ["T1"])
        self.assertIn("feature: 2 ticket(s)", self.cli("plan", "list").stdout)
        self.assertEqual(self.cli("plan", "show", "ghost").returncode, 2)


if __name__ == "__main__":
    unittest.main()
