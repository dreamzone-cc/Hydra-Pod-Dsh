# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the ledger, the workflow state machine, resource attribution and `wf` CLI."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_pod_dsh import ledger, resources, workflow  # noqa: E402

TO_VERIFY = ["PLANNING", "PLAN_READY", "ASSIGNING", "EXECUTING", "TESTING", "REVIEWING", "VERIFYING"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.p / "cache")})
        env.start()
        self.addCleanup(env.stop)

    def to_verifying(self, wid="WF-T1", ticket="T1", **policy):
        workflow.create(self.p, ticket, "obj", policy or None)
        for s in TO_VERIFY:
            workflow.advance(self.p, wid, s)
        return workflow.get(self.p, wid)


class LedgerTest(Base):
    def test_append_assigns_increasing_seq_and_version(self):
        a = ledger.append(self.p, "hydra/workflow-created", workflow_id="W")
        b = ledger.append(self.p, "hydra/workflow-state", workflow_id="W")
        self.assertEqual((a["seq"], b["seq"], a["v"]), (1, 2, ledger.SCHEMA_VERSION))
        self.assertEqual([e["seq"] for e in ledger.read(self.p)], [1, 2])

    def test_concurrent_appends_never_share_a_seq(self):
        def worker():
            for _ in range(25):
                ledger.append(self.p, "hydra/workflow-state", workflow_id="W")
        threads = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        seqs = [e["seq"] for e in ledger.read(self.p)]
        self.assertEqual(sorted(seqs), list(range(1, 101)))

    def test_newer_schema_is_refused_not_reinterpreted(self):
        ledger.append(self.p, "hydra/workflow-created", workflow_id="W")
        with open(ledger.ledger_path(self.p), "a") as f:
            f.write(json.dumps({"v": ledger.SCHEMA_VERSION + 1, "seq": 2, "type": "x"}) + "\n")
        with self.assertRaises(ledger.LedgerError):
            ledger.read(self.p)

    def test_unknown_type_needs_ignorable(self):
        ledger.append(self.p, "hydra/other", workflow_id="W", ignorable=True)
        self.assertEqual(ledger.read(self.p, frozenset({"hydra/workflow-created"})), [])
        ledger.append(self.p, "hydra/required-new", workflow_id="W")
        with self.assertRaises(ledger.LedgerError):
            ledger.read(self.p, frozenset({"hydra/workflow-created"}))

    def test_torn_final_line_is_ignored_but_damage_elsewhere_is_not(self):
        ledger.append(self.p, "hydra/workflow-created", workflow_id="W")
        path = ledger.ledger_path(self.p)
        with open(path, "a") as f:
            f.write('{"v": 1, "seq": 2, "ty')  # crash mid-write
        self.assertEqual(len(ledger.read(self.p)), 1)
        path.write_text('{"broken\n' + path.read_text())
        with self.assertRaises(ledger.LedgerError):
            ledger.read(self.p)

    def test_valid_json_line_with_missing_keys_is_refused(self):
        ledger.append(self.p, "hydra/workflow-created", workflow_id="W")
        path = ledger.ledger_path(self.p)
        prev = ledger.line_hash(path.read_text().splitlines(keepends=True)[0])
        with open(path, "a") as f:  # structured JSON, chained correctly, but no seq/at/workflow_id
            f.write(json.dumps({"v": 1, "type": "hydra/workflow-state", "prev": prev}) + "\n")
        with self.assertRaises(ledger.LedgerError) as cm:
            ledger.read(self.p)
        self.assertIn("required key", str(cm.exception))

    def test_non_numeric_seq_is_refused(self):
        ledger.append(self.p, "hydra/workflow-created", workflow_id="W")
        path = ledger.ledger_path(self.p)
        prev = ledger.line_hash(path.read_text().splitlines(keepends=True)[0])
        with open(path, "a") as f:
            f.write(json.dumps({"v": 1, "seq": "2", "at": 1, "workflow_id": "W",
                                "type": "hydra/workflow-state", "prev": prev}) + "\n")
        with self.assertRaises(ledger.LedgerError) as cm:
            ledger.read(self.p)
        self.assertIn("'seq' must be an integer", str(cm.exception))


class WorkflowTest(Base):
    def test_happy_path_to_done(self):
        self.to_verifying()
        w = workflow.decide(self.p, "WF-T1", "APPROVE", "all checks pass")
        self.assertEqual(w.state, "APPROVED")
        w = workflow.advance(self.p, "WF-T1", "DONE")
        self.assertEqual((w.state, w.folder), ("DONE", "done"))

    def test_illegal_moves_refused(self):
        workflow.create(self.p, "T1", "obj")
        with self.assertRaises(workflow.TransitionError):
            workflow.advance(self.p, "WF-T1", "DONE")
        with self.assertRaises(workflow.TransitionError):
            workflow.decide(self.p, "WF-T1", "APPROVE", "too early")
        with self.assertRaises(workflow.TransitionError):
            workflow.create(self.p, "T1", "duplicate")

    def test_decision_needs_reason_and_known_value(self):
        self.to_verifying()
        for d, r in (("APPROVE", " "), ("MAYBE", "x")):
            with self.assertRaises(workflow.TransitionError):
                workflow.decide(self.p, "WF-T1", d, r)

    def test_rework_loops_back_and_adds_fix_task(self):
        self.to_verifying()
        w = workflow.decide(self.p, "WF-T1", "REWORK", "RF-1 valid", task_id="T1b")
        self.assertEqual((w.state, w.rework_attempts, w.task_id, w.tasks), ("REWORK", 1, "T1b", ["T1", "T1b"]))
        self.assertEqual(workflow.advance(self.p, "WF-T1", "EXECUTING").state, "EXECUTING")

    def test_re_review_and_replan_paths(self):
        self.to_verifying()
        w = workflow.decide(self.p, "WF-T1", "RE_REVIEW", "concurrency not checked",
                            directive={"scope": ["src/net/"], "findings_to_recheck": ["RF-14"]})
        self.assertEqual((w.state, w.review_cycles), ("RE_REVIEW", 1))
        workflow.advance(self.p, "WF-T1", "REVIEWING")
        workflow.advance(self.p, "WF-T1", "VERIFYING")
        w = workflow.decide(self.p, "WF-T1", "REPLAN", "architecture mismatch")
        self.assertEqual((w.state, w.replans), ("REPLAN", 1))
        self.assertEqual(workflow.advance(self.p, "WF-T1", "PLANNING").state, "PLANNING")
        ev = [e for e in workflow.timeline(self.p) if e["type"] == "hydra/verification-decision"][0]
        self.assertEqual(ev["payload"]["directive"]["findings_to_recheck"], ["RF-14"])

    def test_limit_turns_decision_into_escalate(self):
        self.to_verifying(max_rework_attempts=1)
        workflow.decide(self.p, "WF-T1", "REWORK", "first")
        for s in ("EXECUTING", "TESTING", "REVIEWING", "VERIFYING"):
            workflow.advance(self.p, "WF-T1", s)
        w = workflow.decide(self.p, "WF-T1", "REWORK", "second")
        self.assertEqual((w.state, w.rework_attempts), ("BLOCKED", 1))
        self.assertEqual(w.last_decision["requested"], "REWORK")
        self.assertIn("max_rework_attempts=1", w.last_decision["limit_note"])

    def test_failed_acceptance_is_rework_from_testing(self):
        workflow.create(self.p, "T1", "obj")
        for s in TO_VERIFY[:5]:
            workflow.advance(self.p, "WF-T1", s)
        self.assertEqual(workflow.decide(self.p, "WF-T1", "REWORK", "acceptance 2 failed").state, "REWORK")

    def test_escalate_abort_and_terminal_is_final(self):
        self.to_verifying()
        self.assertEqual(workflow.decide(self.p, "WF-T1", "ABORT", "wrong repo").state, "ABORTED")
        with self.assertRaises(workflow.TransitionError):
            workflow.advance(self.p, "WF-T1", "PLANNING")
        with self.assertRaises(workflow.TransitionError):
            workflow.human(self.p, "WF-T1", "resume", "no")

    def test_human_pause_resume_returns_to_previous_state(self):
        self.to_verifying()
        self.assertEqual(workflow.human(self.p, "WF-T1", "pause", "lunch").state, "BLOCKED")
        w = workflow.human(self.p, "WF-T1", "resume", "back")
        self.assertEqual(w.state, "VERIFYING")
        ev = workflow.timeline(self.p)[-1]
        self.assertEqual((ev["actor"]["kind"], ev["payload"]["action"]), ("user", "resume"))

    def test_state_is_rebuilt_from_the_ledger_alone(self):
        """Crash recovery (§21): a fresh fold of the file gives the same state."""
        self.to_verifying()
        workflow.decide(self.p, "WF-T1", "REWORK", "x", task_id="T1b")
        before = workflow.get(self.p, "WF-T1").to_dict()
        again = workflow.fold(ledger.read(self.p, workflow.EVENT_TYPES))["WF-T1"].to_dict()
        self.assertEqual(before, again)

    def test_unknown_policy_key_rejected(self):
        with self.assertRaises(workflow.TransitionError):
            workflow.create(self.p, "T1", "obj", {"max_whatever": 1})

    def test_policy_limits_must_be_positive_integers(self):
        for bad in ({"max_rework_attempts": 0}, {"max_rework_attempts": -1},
                    {"max_review_cycles": 1.5}, {"max_replans": True}):
            with self.assertRaises(workflow.TransitionError, msg=bad):
                workflow.create(self.p, "T1", "obj", bad)

    def test_deciding_the_same_fix_task_twice_does_not_duplicate_it(self):
        self.to_verifying()
        workflow.decide(self.p, "WF-T1", "REWORK", "first", task_id="T1b")
        for s in ("EXECUTING", "TESTING", "REVIEWING", "VERIFYING"):
            workflow.advance(self.p, "WF-T1", s)
        workflow.decide(self.p, "WF-T1", "REWORK", "again the same ticket", task_id="T1b")
        w = workflow.get(self.p, "WF-T1")
        self.assertEqual(w.tasks.count("T1b"), 1)
        self.assertEqual(w.tasks, ["T1", "T1b"])

    def test_unsafe_ticket_ids_rejected(self):
        for bad in ("x/../../evil", "a b", "../T1", "T1*", "", "T#1", "x" * 65, ".hidden"):
            with self.assertRaises(workflow.TransitionError, msg=bad):
                workflow.create(self.p, bad, "obj")
        self.to_verifying()
        with self.assertRaises(workflow.TransitionError):
            workflow.decide(self.p, "WF-T1", "REWORK", "x", task_id="T1/../../escape")
        with self.assertRaises(workflow.TransitionError):
            workflow.validate_workflow_id("WF-x/../../escape")
        workflow.validate_workflow_id("WF-T1")

    def test_concurrent_decides_cannot_both_land(self):
        """The transition (fold, check, append) runs under one lock, or two
        racing commands would both validate against the same state."""
        self.to_verifying()
        barrier = threading.Barrier(2)
        landed, refused = [], []

        def one(reason):
            barrier.wait()
            try:
                workflow.decide(self.p, "WF-T1", "REWORK", reason, task_id="T1b")
                landed.append(reason)
            except workflow.TransitionError:
                refused.append(reason)

        ts = [threading.Thread(target=one, args=(f"r{i}",)) for i in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(landed), 1)
        self.assertEqual(len(refused), 1)
        w = workflow.get(self.p, "WF-T1")
        self.assertEqual((w.state, w.rework_attempts), ("REWORK", 1))

    def test_concurrent_creates_cannot_duplicate(self):
        barrier = threading.Barrier(2)
        made = []

        def one():
            barrier.wait()
            try:
                workflow.create(self.p, "T9", "obj")
                made.append(1)
            except workflow.TransitionError:
                pass

        ts = [threading.Thread(target=one) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(made), 1)
        self.assertEqual(len([e for e in ledger.read(self.p)
                              if e["type"] == "hydra/workflow-created"]), 1)

    def test_timeline_limit_is_validated(self):
        for bad in (0, -1):
            with self.assertRaises(workflow.TransitionError):
                workflow.timeline(self.p, limit=bad)


class ResourcesTest(Base):
    RUNS = [{"at": "a", "phase": "build", "provider": "opencode-go", "model": "opencode-go/deepseek-v4.1-flash",
             "exit": 0, "seconds": 36, "tool_calls": 12, "tokens": {"input": 100, "output": 20}, "list_cost_usd": 0.5},
            {"at": "b", "phase": "review", "provider": "zcode-lite", "model": "GLM-5.3", "exit": 0,
             "seconds": 77, "zai_credits": 11}]

    def write_costs(self, name, runs):
        (self.p / "_receipts").mkdir(exist_ok=True)
        with open(self.p / "_receipts" / name, "a") as f:
            for r in runs:
                f.write(json.dumps(r) + "\n")

    def test_sync_is_idempotent_and_attributes_billing(self):
        workflow.create(self.p, "T5", "obj")
        self.write_costs("T5-slug.costs.jsonl", self.RUNS)
        self.assertEqual(resources.sync(self.p, "WF-T5"), 2)
        self.assertEqual(resources.sync(self.p, "WF-T5"), 0)
        sm = resources.summary(self.p, "WF-T5")
        routes = {(r["role"], r["billing"]) for r in sm["by_route"]}
        self.assertEqual(routes, {("executor", "subscription/opencode-go"), ("reviewer", "subscription/zai-lite")})
        self.assertEqual((sm["total"]["list_cost_usd"], sm["total"]["zai_credits"], sm["total"]["work_tokens"]), (0.5, 11, 120))
        self.assertEqual(sm["total"]["tokens"]["input"], 100)

    def test_run_time_accepts_the_dispatch_format_and_degrades_cleanly(self):
        """hydra-pod-dispatch writes aware local ISO (`now().astimezone().isoformat()`)."""
        import datetime as dt
        aware = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        self.assertAlmostEqual(resources.run_time({"at": aware}), dt.datetime.now().timestamp(), delta=5)
        self.assertAlmostEqual(resources.run_time({"at": "2026-09-29T10:00:00+00:00"}), 1790676000.0, delta=1)
        self.assertEqual(resources.run_time({"at": "not a date"}), None)
        self.assertEqual(resources.run_time({}), None)

    def test_unknown_cost_stays_unknown(self):
        workflow.create(self.p, "T5", "obj")
        self.write_costs("T5.costs.jsonl", [self.RUNS[1]])
        resources.sync(self.p, "WF-T5")
        self.assertIsNone(resources.summary(self.p, "WF-T5")["total"]["list_cost_usd"])

    def test_fix_ticket_runs_count_toward_the_workflow(self):
        self.to_verifying("WF-T5", "T5")
        workflow.decide(self.p, "WF-T5", "REWORK", "x", task_id="T5b")
        self.write_costs("T5b.costs.jsonl", [self.RUNS[0]])
        self.assertEqual(resources.sync(self.p, "WF-T5"), 1)


class CliTest(Base):
    def run_cli(self, *args):
        env = dict(os.environ, XDG_CACHE_HOME=str(self.p / "cache"))
        return subprocess.run([str(ROOT / "bin/hydra-pod-dsh"), *args, "--project", str(self.p)],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_wf_round_trip_and_stage_sync(self):
        self.assertEqual(self.run_cli("wf", "start", "T3", "--objective", "o").returncode, 0)
        for s in TO_VERIFY:
            self.assertEqual(self.run_cli("wf", "advance", "WF-T3", s).returncode, 0)
        stage = json.loads((self.p / "cache/hydra-pod-dsh/stage.json").read_text())
        self.assertEqual(stage["stage"], "validating")
        r = self.run_cli("wf", "decide", "WF-T3", "APPROVE", "--reason", "ok")
        self.assertIn("APPROVED", r.stdout)
        out = json.loads(self.run_cli("wf", "show", "WF-T3", "--json").stdout)
        self.assertEqual((out["schema_version"], out["workflows"][0]["state"]), (1, "APPROVED"))
        self.run_cli("wf", "advance", "WF-T3", "DONE")
        self.assertFalse((self.p / "cache/hydra-pod-dsh/stage.json").exists())

    def test_refusal_exits_2_with_message(self):
        self.run_cli("wf", "start", "T3", "--objective", "o")
        r = self.run_cli("wf", "advance", "WF-T3", "DONE")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not allowed", r.stderr)

    def test_unknown_workflow_is_a_clean_refusal(self):
        self.run_cli("wf", "start", "T3", "--objective", "o")
        for args in (("wf", "show", "WF-TYPO"), ("wf", "check", "WF-TYPO")):
            r = self.run_cli(*args)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn("no workflow", r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_environment_error_is_clean_not_traceback(self):
        ro = self.p / "ro"
        ro.mkdir()
        ro.chmod(0o500)
        try:
            env = dict(os.environ, XDG_CACHE_HOME=str(self.p / "cache"))
            r = subprocess.run([str(ROOT / "bin/hydra-pod-dsh"), "wf", "start", "T3", "--objective", "o",
                                "--project", str(ro / "proj")], capture_output=True, text=True,
                               env=env, timeout=60)
        finally:
            ro.chmod(0o700)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("hydra-pod-dsh:", r.stderr)


class LedgerIntegrityTest(Base):
    def lines(self):
        return ledger.ledger_path(self.p).read_text().splitlines(keepends=True)

    def test_chain_links_every_event(self):
        for i in range(3):
            ledger.append(self.p, "hydra/workflow-state", workflow_id="W")
        evs = ledger.read(self.p)
        self.assertEqual(evs[0]["prev"], ledger.GENESIS)
        self.assertEqual(evs[1]["prev"], ledger.line_hash(self.lines()[0]))

    def test_edited_removed_or_reordered_line_is_detected(self):
        for i in range(3):
            ledger.append(self.p, "hydra/workflow-state", workflow_id="W", reason=f"r{i}")
        original = self.lines()
        path = ledger.ledger_path(self.p)
        for tampered in ([original[0].replace("r0", "rX")] + original[1:],   # edited
                         [original[0]] + original[2:],                       # removed
                         [original[1], original[0], original[2]]):           # reordered
            path.write_text("".join(tampered))
            with self.assertRaises(ledger.LedgerError):
                ledger.read(self.p)
        path.write_text("".join(original))
        self.assertEqual(len(ledger.read(self.p)), 3)

    def test_appending_after_a_torn_line_keeps_the_chain(self):
        ledger.append(self.p, "hydra/workflow-state", workflow_id="W")
        with open(ledger.ledger_path(self.p), "a") as f:
            f.write('{"v": 1, "seq": 2, "ty')
        self.assertEqual(len(ledger.read(self.p)), 1)

    def test_legacy_path_is_moved_once(self):
        legacy = self.p / "_tickets" / "ledger.jsonl"
        legacy.parent.mkdir(parents=True)
        ledger.append(self.p, "hydra/workflow-created", workflow_id="W")   # creates the new path
        new = ledger.ledger_path(self.p)
        self.assertEqual(new, self.p / "_receipts" / "ledger.jsonl")
        text = new.read_text()
        new.unlink()
        legacy.write_text(text)
        self.assertEqual(len(ledger.read(self.p)), 1)
        self.assertFalse(legacy.exists())
        self.assertTrue(new.exists())


class AmendTest(Base):
    def test_amend_records_without_changing_state(self):
        workflow.create(self.p, "T1", "o")
        for s in TO_VERIFY[:5]:
            workflow.advance(self.p, "WF-T1", s)
        w = workflow.amend(self.p, "WF-T1", "expected value was miscounted", field="acceptance",
                           before="==> 3", after="==> 4")
        self.assertEqual(w.state, "TESTING")
        e = workflow.timeline(self.p)[-1]
        self.assertEqual((e["type"], e["payload"]["before"], e["payload"]["after"]),
                         ("hydra/task-amended", "==> 3", "==> 4"))
        with self.assertRaises(workflow.TransitionError):
            workflow.amend(self.p, "WF-T1", " ", field="x")


if __name__ == "__main__":
    unittest.main()
