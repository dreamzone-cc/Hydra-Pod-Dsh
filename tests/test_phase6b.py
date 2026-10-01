# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the rest of phase 6: context shards and their allocation, the shared blackboard
with `ask`, and the skill smith. A fake `claude` returns a configurable answer and counts its
calls, so caching and the question cap are checked without spending quota."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_pod_dsh import blackboard, lessons, pack, router, shards, skills, skillsmith, workflow  # noqa: E402

CLI = [sys.executable, "-B", str(ROOT / "bin/hydra-pod-dsh")]
FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, pathlib, sys
log = pathlib.Path(os.environ["FAKE_LOG"])
n = int((log / "calls").read_text()) + 1 if (log / "calls").exists() else 1
(log / "calls").write_text(str(n))
(log / "args.json").write_text(json.dumps(sys.argv[1:]))
print(json.dumps({"is_error": False, "result": (log / "answer.txt").read_text(), "total_cost_usd": 0.001,
                  "usage": {"input_tokens": 100, "output_tokens": 20}}))
"""


class Project(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name) / "proj"
        self.p.mkdir()
        self.log = Path(self.tmp.name) / "log"
        self.log.mkdir()
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir()
        (bindir / "claude").write_text(FAKE_CLAUDE)
        (bindir / "claude").chmod((bindir / "claude").stat().st_mode | stat.S_IEXEC)
        env = {"PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_LOG": str(self.log),
               "XDG_CACHE_HOME": str(Path(self.tmp.name) / "cache"), "DSH_HOME": str(Path(self.tmp.name) / "dsh"),
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        files = {
            "src/core/parse.py": "def parse_line(text):\n    return text.split()\n" + "# pad\n" * 40,
            "src/core/store.py": "from .parse import parse_line\n\n\nclass Store:\n    def put(self, k):\n        return parse_line(k)\n",
            "src/api/handlers.py": "from core.parse import parse_line\n\n\ndef handle(req):\n    return parse_line(req)\n",
            "tests/test_parse.py": "from core.parse import parse_line\n\n\ndef test_p():\n    assert parse_line('a b')\n",
            "docs/guide.md": "# Guide\n\nparse_line splits on whitespace.\n",
        }
        for rel, text in files.items():
            (self.p / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.p / rel).write_text(text)
        subprocess.run(["git", "-C", str(self.p), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.p), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.p), "commit", "-qm", "base"], check=True)

    def ticket(self, task="T1", allowed=("src/api/handlers.py",), hints="src/core/parse.py"):
        d = self.p / "_tickets/open"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{task}-x.md").write_text(f"---\nticket: {task}-x\ncomplexity: S\nread_hints: {hints}\nallowed_files:\n"
                                        + "".join(f"  - {f}\n" for f in allowed) + "---\nbody\n")
        workflow.create(self.p, task, "o")

    def answer(self, text):
        (self.log / "answer.txt").write_text(text)

    def calls(self):
        f = self.log / "calls"
        return int(f.read_text()) if f.exists() else 0


UP = lambda a: (True, "")  # noqa: E731
FREE = lambda a: {"percent": None, "source": "none", "exhausted": False}  # noqa: E731


class ShardTest(Project):
    def test_partition_is_deterministic_and_covers_every_file(self):
        a, b = shards.partition(self.p), shards.partition(self.p)
        self.assertEqual(a, b)
        files = sorted(f for s in a for f in s["files"])
        self.assertIn("docs/guide.md", files)
        self.assertEqual(len(files), len(set(files)))  # each file in exactly one shard

    def test_split_large_and_merge_small(self):
        out = shards.partition(self.p, max_tokens=60)
        self.assertTrue(all(s["tokens"] <= 60 or len(s["files"]) == 1 for s in out))
        big = shards.partition(self.p, max_tokens=10_000)
        ids = [s["id"] for s in big]
        self.assertLess(len(big), len(out))  # small shards merge into the ones they reference
        self.assertEqual(ids, sorted(ids))

    def test_fingerprint_follows_the_working_tree(self):
        s = shards.shard_of(shards.partition(self.p), "src/core/parse.py")
        before = shards.fingerprint(self.p, s["files"])
        (self.p / "src/core/parse.py").write_text("def parse_line(t):\n    return t.split(',')\n")
        self.assertNotEqual(shards.fingerprint(self.p, s["files"]), before)

    def reg(self, executor_window=None, keeper_window=None):
        reg = router.load()
        if executor_window:
            reg["agents"]["builder"]["effective_window"] = executor_window
        if keeper_window:
            for k in ("reviewer", "advisor-claude"):
                reg["agents"][k]["effective_window"] = keeper_window
        return reg

    def test_allocation_reads_itself_when_it_fits_else_uses_keepers(self):
        self.ticket()
        out = shards.allocate(self.p, "WF-T1", reg=self.reg(), quota=FREE, available=UP, record=False, max_shard_tokens=60)
        self.assertEqual((out["status"], out["keepers"]), ("ok", {}))
        self.assertTrue(out["reads"])
        self.assertIn("builder", out["assumed_windows"])  # no registered window: said, not hidden
        touch = sum(out["shard_tokens"][s] for s in out["touch"])
        out = shards.allocate(self.p, "WF-T1", reg=self.reg(executor_window=touch + 1, keeper_window=10_000),
                              quota=FREE, available=UP, record=False, max_shard_tokens=60)
        self.assertEqual(out["reads"], [])
        self.assertEqual(set(out["keepers"].values()), {"reviewer"})  # cheapest read-only keeper first
        self.assertGreater(out["capacity_tokens"], out["executor_window"])

    def test_split_needed_and_lock(self):
        self.ticket()
        out = shards.allocate(self.p, "WF-T1", reg=self.reg(executor_window=1), quota=FREE, available=UP, record=False)
        self.assertEqual(out["status"], "split-needed")
        shards.allocate(self.p, "WF-T1", reg=self.reg(), quota=FREE, available=UP)
        for s in ("PLANNING", "PLAN_READY", "ASSIGNING", "EXECUTING"):
            workflow.advance(self.p, "WF-T1", s)
        self.ticket("T2")
        out = shards.allocate(self.p, "WF-T2", reg=self.reg(), quota=FREE, available=UP, record=False)
        self.assertEqual(out["status"], "locked")
        self.assertIn("WF-T1 (EXECUTING)", out["notes"][-1])

    def test_policy_keeps_paid_advisor_out_of_automatic_pools(self):
        reg = router.load()
        self.assertEqual(router.violations(reg), [])
        for pool in ("keeper", "skillsmith"):
            self.assertNotIn("advisor-openrouter", [m["agent"] for m in reg["pools"][pool]["members"]])


class BlackboardTest(Project):
    def setUp(self):
        super().setUp()
        self.ticket()
        self.answer("ANSWER: parse_line splits on any whitespace.\nREFS: src/core/parse.py:2\nCONFIDENCE: high")

    def ask(self, q="How does parse_line split its input?", **kw):
        return blackboard.ask(self.p, "WF-T1", q, agent="advisor-claude", **kw)

    def test_ask_stores_then_serves_from_cache_until_the_file_changes(self):
        f = self.ask()
        self.assertEqual((f["stored"], f["refs"], f["confidence"]), (True, [{"path": "src/core/parse.py", "line": 2}],
                                                                     "high"))
        self.assertEqual(f["shard"], shards.shard_of(shards.partition(self.p), "src/core/parse.py")["id"])  # guessed
        args = json.loads((self.log / "args.json").read_text())
        self.assertEqual(args[args.index("--tools") + 1], "Read,Grep,Glob")
        again = self.ask("how does PARSE_LINE split its input")   # same question, other spelling
        self.assertTrue(again["cached"])
        self.assertEqual(self.calls(), 1)                          # no second model call
        (self.p / "src/core/parse.py").write_text("def parse_line(text):\n    return text.split(',')\n")
        self.assertEqual(blackboard.facts(self.p), [])            # the fact went stale
        self.assertFalse(self.ask()["cached"])
        self.assertEqual(self.calls(), 2)

    def test_cap_unknown_and_bad_refs(self):
        self.answer("ANSWER: unknown\nREFS:\nCONFIDENCE: low")
        self.assertFalse(self.ask()["stored"])
        self.answer("ANSWER: it is in store.py\nREFS: src/core/store.py:999, nope.py:1\nCONFIDENCE: high")
        f = self.ask("Where is Store.put defined?")
        self.assertEqual((f["refs"], sorted(f["bad_refs"]), f["confidence"]),
                         ([], ["nope.py:1", "src/core/store.py:999"], "low"))  # unbacked claim: low confidence
        with self.assertRaises(ValueError):
            self.ask("Something about parse_line again?", max_questions=2)

    def test_manager_fact_and_validation(self):
        sid = shards.shard_of(shards.partition(self.p), "src/core/parse.py")["id"]
        f = blackboard.add(self.p, "WF-T1", sid, "split rule", "parse_line splits on whitespace", "src/core/parse.py:2")
        self.assertEqual(f["source"], "manager")
        with self.assertRaises(ValueError):
            blackboard.add(self.p, "WF-T1", sid, "q", "an answer", "src/core/parse.py:500")
        with self.assertRaises(ValueError):
            blackboard.add(self.p, "WF-T1", "S-nope", "q", "an answer")

    def test_pack_carries_facts_and_ask_instructions(self):
        reg = router.load()
        reg["agents"]["builder"]["effective_window"] = 30
        reg["agents"]["advisor-claude"]["effective_window"] = 10_000
        reg["agents"]["reviewer"]["effective_window"] = 1   # too small: the next keeper is chosen
        out = shards.allocate(self.p, "WF-T1", reg=reg, quota=FREE, available=UP, max_shard_tokens=60)
        self.assertTrue(out["keepers"], out)
        self.ask()
        text = pack.build(self.p, "T1")["text"]
        self.assertIn("parse_line splits on any whitespace. (src/core/parse.py:2)", text)
        self.assertIn("hydra-pod-dsh ask", text)
        self.assertIn("--wf WF-T1 --shard S-", text)

    def test_cli(self):
        r = subprocess.run(CLI + ["ask", "How does parse_line split its input?", "--wf", "WF-T1", "--agent",
                                  "advisor-claude", "--project", str(self.p)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(CLI + ["bb", "list", "--project", str(self.p)], capture_output=True, text=True)
        self.assertIn("ok", r.stdout)
        r = subprocess.run(CLI + ["shards", "list", "--project", str(self.p), "--json"], capture_output=True, text=True)
        self.assertTrue(json.loads(r.stdout)["shards"])


class SkillSmithTest(Project):
    def test_mine_groups_repeats_and_skips_covered(self):
        lessons.add(self.p, "parse_line must keep tabs as separators", ["src/core/parse.py"], ["tokenizing"], "WF-T1")
        lessons.add(self.p, "the store must normalise keys before parsing", ["src/core/store.py"], [], "WF-T2")
        lessons.add(self.p, "docs need a changelog entry for each api change", ["docs/guide.md"], [], "WF-T3")
        for t, reason in (("T1", "tabs are not treated as separators in parse_line"),
                          ("T2", "parse_line still ignores tabs as separators")):
            workflow.create(self.p, t, "o")
            for s in ("PLANNING", "PLAN_READY", "ASSIGNING", "EXECUTING", "TESTING", "REVIEWING", "VERIFYING"):
                workflow.advance(self.p, f"WF-{t}", s)
            workflow.decide(self.p, f"WF-{t}", "REWORK", reason, task_id=f"{t}b")
        props = skillsmith.mine(self.p)
        kinds = sorted(p["kind"] for p in props)
        self.assertEqual(kinds, ["lessons", "rework"])             # the docs lesson stands alone
        lesson = next(p for p in props if p["kind"] == "lessons")
        self.assertEqual(lesson["paths"], ["src/core/parse.py", "src/core/store.py"])
        skills.add(self.p, "core-parsing", "Parsing rules in core", "x" * 50, "src/core/*.py", "")
        self.assertEqual([p["kind"] for p in skillsmith.mine(self.p)], ["rework"])  # now covered

    def test_draft_becomes_a_candidate_only(self):
        lessons.add(self.p, "parse_line must keep tabs as separators", ["src/core/parse.py"], ["tokenizing"], "WF-T1")
        lessons.add(self.p, "the store must normalise keys before parsing", ["src/core/parse.py"], [], "WF-T2")
        pid = skillsmith.mine(self.p)[0]["id"]
        self.answer("NAME: core-parsing-rules\nDESCRIPTION: Rules for parse_line and its callers\n"
                    "PATHS: src/core/*.py\nKEYWORDS: parse, tokenize\nBODY:\n1. Split on any whitespace.\n"
                    "2. Add a test with tabs and newlines for every change to parse_line.\n")
        s = skillsmith.draft(self.p, pid)
        self.assertEqual((s["state"], s["paths"], s["keywords"]), ("candidate", ["src/core/*.py"], ["parse", "tokenize"]))
        self.assertEqual(skills.match(self.p, ["src/core/parse.py"]), [])   # not used until the manager approves
        self.answer("I think you should write tests.")
        lessons.add(self.p, "api handlers must reject empty requests", ["src/api/handlers.py"], ["validation"], "WF-T4")
        lessons.add(self.p, "api handlers must log the request id", ["src/api/handlers.py"], ["validation"], "WF-T5")
        other = next(p["id"] for p in skillsmith.mine(self.p) if "src/api/handlers.py" in p["paths"])
        with self.assertRaises(ValueError):
            skillsmith.draft(self.p, other)                      # a free-form answer is not a skill
        with self.assertRaises(ValueError):
            skillsmith.draft(self.p, "P-nope")


if __name__ == "__main__":
    unittest.main()
