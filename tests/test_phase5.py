# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for phase 5 of the technical paper: the second reviewer and the advisors run by
hydra-pod-dsh. Fake `claude` and `opencode` executables stand in for the real CLIs (no quota
is spent) and record their arguments, so the read-only flags are checked, not assumed."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_pod_dsh import findings, ledger, resources, router, workflow  # noqa: E402

CLI = [sys.executable, "-B", str(ROOT / "bin/hydra-pod-dsh")]
REPORT = ("Reviewer: reviewer-claude at now\\n**Verdict: FAIL** one bug.\\n"
          "**high | src/a.py:1 | off by one | evidence | fix**\\n## Conclusions\\n"
          "RC-1 | the helper assumes ASCII | src/a.py:1\\n## Suggestions\\nnone")
FAKE_CLAUDE = f"""#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_LOG/claude.args"
cat <<'EOF'
{{"type":"result","is_error":false,"result":"{REPORT}","total_cost_usd":0.0123,
 "usage":{{"input_tokens":1200,"output_tokens":300,"cache_read_input_tokens":5000,"cache_creation_input_tokens":100}}}}
EOF
"""
FAKE_OPENCODE = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_LOG/opencode.args"
echo '{"type":"step_finish","part":{"tokens":{"input":800,"output":200,"reasoning":50,"cache":{"read":10}},"cost":0.004}}'
echo '{"type":"text","part":{"text":"## Recommendation\\nKeep one parser."}}'
"""


def git(p, *args):
    return subprocess.run(["git", "-C", str(p), *args], capture_output=True, text=True, check=True).stdout.strip()


class Phase5(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name) / "proj"
        self.p.mkdir()
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir()
        self.log = Path(self.tmp.name) / "log"
        self.log.mkdir()
        for name, text in (("claude", FAKE_CLAUDE), ("opencode", FAKE_OPENCODE)):
            (bindir / name).write_text(text)
            (bindir / name).chmod((bindir / name).stat().st_mode | stat.S_IEXEC)
        self.env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_LOG": str(self.log),
                    "XDG_CACHE_HOME": str(Path(self.tmp.name) / "cache"), "HOME": self.tmp.name,
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@t"}
        os.environ["XDG_CACHE_HOME"] = self.env["XDG_CACHE_HOME"]
        git(self.p, "init", "-q")
        (self.p / "src").mkdir()
        (self.p / "src/a.py").write_text("def f(x):\n    return x\n")
        git(self.p, "add", "-A")
        git(self.p, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
        base = git(self.p, "rev-parse", "HEAD")
        (self.p / "src/a.py").write_text("def f(x):\n    return x + 1\n")
        git(self.p, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "work")
        head = git(self.p, "rev-parse", "HEAD")
        d = self.p / "_tickets/doing"
        d.mkdir(parents=True)
        (d / "T1-fix.md").write_text(f"---\nticket: T1-fix\nbase: {base}\nhead: {head}\ntest_command: true\n"
                                     "risk: high\nallowed_files:\n  - src/a.py\n---\nFix f.\n")
        workflow.create(self.p, "T1", "fix f")

    def cli(self, *args):
        return subprocess.run(CLI + list(args) + ["--project", str(self.p)], capture_output=True, text=True,
                              env=self.env, timeout=60)

    def costs(self):
        f = self.p / "_receipts/T1-fix.costs.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


class RunReviewTest(Phase5):
    def test_second_review_is_read_only_and_joins_the_verdict_gate(self):
        r = self.cli("run", "review", "T1", "--agent", "reviewer-claude", "--wf", "WF-T1")
        self.assertEqual(r.returncode, 0, r.stderr)
        args = (self.log / "claude.args").read_text().splitlines()
        self.assertEqual(args[args.index("--tools") + 1], "Read,Grep,Glob")       # nothing else exists
        self.assertEqual(args[args.index("--model") + 1], "claude-sonnet-5-5")  # not the manager's Opus
        self.assertIn("--no-session-persistence", args)
        prompt = "\n".join(args[args.index("-p") + 1:args.index("--model")])  # one argument, many lines
        self.assertIn("return x + 1", prompt)  # the diff is in the prompt: the reviewer cannot run git
        report = (self.p / "_receipts/T1-fix.review-reviewer-claude.md").read_text()
        self.assertIn("**Verdict: FAIL**", report)
        fs = findings.summary(self.p, ["T1"])
        self.assertEqual(fs["by_status"]["unverified"], 1)
        self.assertEqual(fs["items"], {"conclusion:unverified": 1})
        cost = self.costs()[-1]
        self.assertEqual((cost["provider"], cost["phase"], cost["tokens"]["cache_read"]), ("claude-cli", "review", 5000))
        rows = resources.summary(self.p, "WF-T1")["by_route"]
        self.assertEqual(rows[0]["billing"], "subscription/claude-code")

    def test_refusals(self):
        self.assertEqual(self.cli("run", "review", "T1", "--agent", "advisor-openrouter").returncode, 2)  # not a reviewer
        self.assertEqual(self.cli("run", "review", "T1", "--agent", "reviewer").returncode, 2)  # hydra-pod-dispatch's
        self.assertEqual(self.cli("run", "review", "../T1", "--agent", "reviewer-claude").returncode, 2)
        (self.p / "_tickets/doing/T1-fix.md").write_text("---\nticket: T1-fix\nallowed_files:\n  - src/a.py\n---\n")
        r = self.cli("run", "review", "T1", "--agent", "reviewer-claude")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no base/head", r.stderr)

    def test_budget_blocks_before_spending(self):
        workflow.create(self.p, "T2", "o", budget={"max_cost_usd": 0.01})
        (self.p / "_receipts").mkdir(exist_ok=True)
        (self.p / "_receipts/T2.costs.jsonl").write_text(json.dumps(
            {"at": "2026-10-01T00:00:00+00:00", "phase": "build", "provider": "opencode-go", "model": "m",
             "list_cost_usd": 0.02, "tokens": {}}) + "\n")
        r = self.cli("run", "review", "T1", "--agent", "reviewer-claude", "--wf", "WF-T2")
        self.assertEqual(r.returncode, 3)
        self.assertFalse((self.log / "claude.args").exists())


class ConsultTest(Phase5):
    def question(self):
        q = self.p / "q.md"
        q.write_text("Should parsing move into one shared helper, or stay per function?")
        return str(q)

    def test_panel_answers_then_manager_decides(self):
        (self.p / ".opencode/agent").mkdir(parents=True)
        (self.p / ".opencode/agent/reviewer.md").write_text("read-only reviewer\n")
        self.assertEqual(self.cli("wf", "consult", "WF-T1", "decide", "--text", "x", "--reason", "y").returncode, 2)
        r = self.cli("wf", "consult", "WF-T1", "ask", "--question-file", self.question(), "--context", "src/a.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in ("advisor-claude", "advisor-openrouter"):
            self.assertTrue((self.p / f"_receipts/WF-T1.consult-1-{name}.md").exists(), name)
        oc = (self.log / "opencode.args").read_text().splitlines()
        self.assertEqual((oc[oc.index("--agent") + 1], oc[oc.index("-m") + 1]),
                         ("reviewer", "openrouter/google/gemini-2.5-pro"))
        self.assertIn("src/a.py", (self.log / "claude.args").read_text())
        self.assertEqual({c["provider"] for c in self.costs()}, {"claude-cli", "opencode/openrouter"})
        billing = {r["billing"] for r in resources.summary(self.p, "WF-T1")["by_route"]}
        self.assertEqual(billing, {"subscription/claude-code", "api/openrouter"})
        r = self.cli("wf", "consult", "WF-T1", "decide", "--text", "one shared helper", "--reason", "both agree")
        self.assertEqual(r.returncode, 0, r.stderr)
        ev = [e["payload"] for e in ledger.read(self.p) if e["type"] == "hydra/consult"]
        self.assertEqual([(e["action"], e["n"]) for e in ev], [("ask", 1), ("decide", 1)])

    def test_advisor_without_enforced_read_only_fails_alone(self):
        r = self.cli("wf", "consult", "WF-T1", "ask", "--question-file", self.question())  # no opencode reviewer agent
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("advisor-openrouter: FAILED", r.stderr)
        self.assertFalse((self.log / "opencode.args").exists())  # never started without its read-only agent
        ev = [e["payload"] for e in ledger.read(self.p) if e["type"] == "hydra/consult"][0]
        self.assertEqual((ev["reports"][0].endswith("advisor-claude.md"), len(ev["failed"])), (True, 1))

    def test_only_advisors_may_advise(self):
        r = self.cli("wf", "consult", "WF-T1", "ask", "--question-file", self.question(), "--advisor", "builder")
        self.assertEqual(r.returncode, 3)
        self.assertIn("cannot serve as advisor", r.stderr)


class SandboxDenialTest(Phase5):
    def test_opencode_read_only_home_is_reported_as_a_sandbox_denial(self):
        (self.p / ".opencode/agent").mkdir(parents=True)
        (self.p / ".opencode/agent/reviewer.md").write_text("read-only reviewer\n")
        bindir = Path(self.env["PATH"].split(":")[0])
        (bindir / "opencode").write_text("#!/bin/sh\necho \"Error: EROFS: read-only file system, open '$HOME/.local/share/opencode/log/opencode.log'\" >&2\nexit 1\n")
        q = self.p / "q.md"
        q.write_text("Should parsing move into one shared helper, or stay per function?")
        r = self.cli("wf", "consult", "WF-T1", "ask", "--question-file", str(q), "--advisor", "advisor-openrouter")
        self.assertEqual(r.returncode, 1)
        self.assertIn("read-only file system", r.stderr)
        self.assertIn("sandbox_permissions: danger-full-access", r.stderr)


class PolicyTest(unittest.TestCase):
    def test_new_roles_and_pools(self):
        reg = router.load()
        self.assertEqual(router.violations(reg), [])
        reg["agents"]["advisor-claude"].pop("read_only")
        v = router.violations(reg)
        self.assertTrue(any("advisor-claude: advisor without enforced read-only" in x for x in v), v)
        reg = router.load()
        reg["agents"]["advisor-openrouter"]["role"] = "reviewer"   # OpenRouter is approved for advisors only
        self.assertTrue(any("api/openrouter not allowed for role reviewer" in x for x in router.violations(reg)))
        up = lambda a: (True, "")  # noqa: E731
        free = lambda a: {"percent": None, "source": "none", "exhausted": False}  # noqa: E731
        self.assertEqual(router.choose("tester", None, router.load(), free, up)["agent"], "builder")
        self.assertEqual(router.choose("security", None, router.load(), free, up)["agent"], "reviewer-claude")


if __name__ == "__main__":
    unittest.main()
