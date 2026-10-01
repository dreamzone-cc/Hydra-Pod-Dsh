# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for phase 2 of the technical paper: repository map, lessons, context pack,
and the reviewer's conclusions/suggestions channel with the APPROVE gate."""
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

from hydra_pod_dsh import findings, ledger, lessons, pack, repomap, workflow  # noqa: E402

CLI = [sys.executable, "-B", str(ROOT / "bin/hydra-pod-dsh")]
TO_VERIFY = ["PLANNING", "PLAN_READY", "ASSIGNING", "EXECUTING", "TESTING", "REVIEWING", "VERIFYING"]
REVIEW = """**Verdict: PASS — no defects.**

**low | src/app/core.py:3 | Off by one on empty input | e | f**

## Conclusions
RC-1 | The ticket assumes ASCII input, but callers pass user text | src/app/api.py:4
RC-2 | Tests never cover the error branch | tests/test_core.py:1

## Suggestions
RS-1 | Extract the parsing into one helper | removes duplicated code | S
"""


def git(p, *args):
    return subprocess.run(["git", "-C", str(p), *args], capture_output=True, text=True, check=True).stdout


class Project(unittest.TestCase):
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
        files = {
            "src/app/core.py": "def parse_line(text):\n    return text.split()\n\n\nclass Store:\n    def put(self, k):\n        pass\n",
            "src/app/api.py": "from .core import parse_line, Store\n\n\ndef handle(req):\n    return parse_line(req)\n",
            "src/app/unrelated.py": "def lonely():\n    return 0\n",
            "tests/test_core.py": "from app.core import parse_line\n\n\ndef test_parse():\n    assert parse_line('a b')\n",
            ".gitignore": "_receipts/\n",
        }
        for rel, text in files.items():
            (self.p / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.p / rel).write_text(text)
        git(self.p, "add", "-A")
        git(self.p, "commit", "-qm", "base")

    def ticket(self, task="T1", extra="", body="Do it.\n"):
        d = self.p / "_tickets/open"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{task}-x.md").write_text(
            f"---\nticket: {task}-x\nstate: open\ntest_command: true\n{extra}allowed_files:\n"
            "  - src/app/core.py\n  - tests/test_core.py\nacceptance:\n  - true\n---\n" + body)
        workflow.create(self.p, task, "obj")


class RepoMapTest(Project):
    def test_focus_ranks_its_users_above_unrelated_code(self):
        idx = repomap.index(self.p)
        self.assertIn("parse_line", [d["name"] for d in idx["src/app/core.py"]["defs"]])
        self.assertIn("put", [d["name"] for d in idx["src/app/core.py"]["defs"]])  # methods too
        scores = repomap.rank(idx, ["src/app/api.py"])
        self.assertGreater(scores["src/app/core.py"], scores["src/app/unrelated.py"])

    def test_budget_and_focus_first(self):
        out = repomap.build(self.p, ["src/app/api.py"], 100)
        self.assertTrue(out["text"].startswith("src/app/api.py:"))
        self.assertLessEqual(out["tokens"], 110)
        self.assertEqual(out["focus"], ["src/app/api.py"])

    def test_cache_follows_the_working_tree(self):
        repomap.index(self.p)
        self.assertEqual(len(list((self.p / ".cache/hydra-pod-dsh/repomap").glob("*.json"))), 1)
        (self.p / "src/app/new.py").write_text("def fresh():\n    pass\n")
        self.assertIn("src/app/new.py", repomap.index(self.p))  # untracked file seen, cache refreshed
        self.assertEqual(len(list((self.p / ".cache/hydra-pod-dsh/repomap").glob("*.json"))), 1)

    def test_cli(self):
        r = subprocess.run(CLI + ["map", "--project", str(self.p), "--focus", "src/app/core.py", "--json"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreaterEqual(json.loads(r.stdout)["files_indexed"], 4)


class LessonsTest(Project):
    def test_add_load_and_relevance(self):
        lessons.add(self.p, "parse_line must keep tabs as separators", ["src/app/core.py"], ["tokenizing"], "WF-T1")
        lessons.add(self.p, "the api layer never raises on bad input", ["src/app/api.py"], [])
        lessons.add(self.p, "unicode names need casefold, not lower", [], ["unicode"])
        self.assertEqual(len(lessons.load(self.p)), 3)
        got = lessons.relevant(self.p, ["src/app/"], "handle unicode text")
        self.assertEqual([x["text"][:9] for x in got], ["the api l", "parse_lin", "unicode n"])
        self.assertEqual(lessons.relevant(self.p, ["docs/x.md"], "nothing"), [])

    def test_validation(self):
        with self.assertRaises(ValueError):
            lessons.add(self.p, "short")
        with self.assertRaises(ValueError):
            lessons.add(self.p, "a long enough lesson text", tags=["Bad Tag"])

    def test_cli(self):
        r = subprocess.run(CLI + ["lesson", "add", "--project", str(self.p), "--text", "always run the api tests",
                                  "--path", "src/app/api.py", "--wf", "../x"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)  # a bad workflow id never reaches the file
        r = subprocess.run(CLI + ["lesson", "add", "--project", str(self.p), "--text", "always run the api tests",
                                  "--path", "src/app/api.py"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(CLI + ["lesson", "list", "--project", str(self.p), "--path", "src/app/api.py", "--json"],
                           capture_output=True, text=True)
        self.assertEqual(len(json.loads(r.stdout)["lessons"]), 1)


class PackTest(Project):
    def test_pack_contents_and_ledger_event(self):
        lessons.add(self.p, "parse_line must keep tabs as separators", ["src/app/core.py"], [])
        self.ticket(extra="complexity: s\nrisk: high\nread_hints: src/app/api.py, src/app/core.py\n")
        out = pack.write(self.p, "WF-T1")
        text = (self.p / "_receipts/T1-x.context.md").read_text()
        self.assertIn("### src/app/core.py (7 lines, whole)", text)
        self.assertIn("### src/app/api.py", text)             # read hint, minus the files already in scope
        self.assertEqual(out["read_hints"], ["src/app/api.py"])
        self.assertIn("parse_line must keep tabs", text)
        self.assertIn("RC-1 |", text)
        self.assertIn("complexity S, risk high", text)
        self.assertFalse(out["ticket_points_to_pack"])
        events = [e for e in ledger.read(self.p) if e["type"] == "hydra/context-pack"]
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["ignorable"])
        self.assertEqual(events[0]["payload"]["tokens"], out["tokens"])

    def test_large_file_becomes_an_outline_and_budget_holds(self):
        big = "".join(f"def f{i}(x):\n    return x + {i}\n\n" for i in range(400))
        (self.p / "src/app/core.py").write_text(big)
        self.ticket()
        out = pack.build(self.p, "T1", max_tokens=3000)
        self.assertIn("outline: read the parts you change", out["text"])
        self.assertLess(out["tokens"], 3600)

    def test_pointer_detected_and_bad_header_refused(self):
        self.ticket(body="Context pack: _receipts/T1-x.context.md\n")
        self.assertTrue(pack.build(self.p, "T1")["ticket_points_to_pack"])
        self.ticket("T2", extra="risk: extreme\n")
        with self.assertRaises(ValueError):
            pack.build(self.p, "T2")

    def test_cli_prints_pointer(self):
        self.ticket()
        r = subprocess.run(CLI + ["wf", "pack", "WF-T1", "--project", str(self.p)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("add this line to the ticket body: Context pack: _receipts/T1-x.context.md", r.stdout)


class ReviewItemsTest(Project):
    def test_parse_items_and_manager_answers(self):
        r = findings.parse(REVIEW + "\n## Manager verdicts\n\n1. **low: off by one, INVALID.** Empty input returns [].\n"
                           "RC-1: ACCEPTED — callers do pass user text; new ticket T2\n"
                           "RS-1: backlog — worth it after T2\n")
        items = {i["id"]: i for i in r["items"]}
        self.assertEqual(set(items), {"RC-1", "RC-2", "RS-1"})
        self.assertEqual((items["RC-1"]["status"], items["RS-1"]["status"], items["RC-2"]["status"]),
                         ("accepted", "backlog", "unverified"))
        self.assertEqual(items["RS-1"]["detail"], ["removes duplicated code", "S"])
        self.assertTrue(items["RC-1"]["reason"].startswith("callers do pass"))
        # a suggestion word on a conclusion is not a valid answer
        r = findings.parse(REVIEW + "\n## Manager verdicts\nRC-2: BACKLOG — later\n")
        self.assertEqual({i["id"]: i["status"] for i in r["items"]}["RC-2"], "unverified")

    def approve(self):
        return subprocess.run(CLI + ["wf", "decide", "WF-T1", "APPROVE", "--reason", "ok", "--project", str(self.p)],
                              capture_output=True, text=True)

    def test_approve_waits_for_every_verdict(self):
        self.ticket()
        for s in TO_VERIFY:
            workflow.advance(self.p, "WF-T1", s)
        rec = self.p / "_receipts"
        rec.mkdir(exist_ok=True)
        (rec / "T1.review.md").write_text(REVIEW)
        r = self.approve()
        self.assertEqual(r.returncode, 2)
        self.assertIn("4 reviewer item(s) without a manager verdict", r.stderr)
        self.assertEqual(workflow.get(self.p, "WF-T1").state, "VERIFYING")
        (rec / "T1.review.md").write_text(REVIEW + "\n## Manager verdicts\n1. **low: INVALID.** no\n"
                                          "RC-1: REJECTED — input is validated upstream\n"
                                          "RC-2: ACCEPTED — test added in T1b\nRS-1: ADOPT-NOW — small\n")
        r = self.approve()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(workflow.get(self.p, "WF-T1").state, "APPROVED")
        r = subprocess.run(CLI + ["wf", "findings", "WF-T1", "--project", str(self.p)], capture_output=True, text=True)
        self.assertIn("RC-1 rejected", r.stdout)


if __name__ == "__main__":
    unittest.main()
