# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every command the manager runs must work inside DeepSeek Harness's default sandbox.

DSH runs the manager's bash calls under bwrap with the workspace-write profile of
packages/sandbox/sandbox-local/src/profiles.ts: `/` read-only, a fresh tmpfs on /tmp,
and only the session workspace writable. So ~/.cache is read-only there. These tests
run the CLI under that exact profile (skipped where bwrap cannot create it) and check
that no command fails, and that a recorded move is never reported as a failure.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_pod_dsh import live, workflow  # noqa: E402

BIN = str(ROOT / "bin/hydra-pod-dsh")
TEST_TMP = ROOT / ".test-tmp"


def dsh_profile(workspace: str) -> list[str]:
    """bwrapProfileArgs({mode: 'workspace-write', workspaceRoot}) from DSH 0.2.0-rc.1 / rc.2."""
    return ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--unshare-pid", "--proc", "/proc",
            "--die-with-parent", "--tmpfs", "/tmp", "--bind", workspace, workspace]


def bwrap_works() -> bool:
    if not shutil.which("bwrap"):
        return False
    return subprocess.run(dsh_profile("/tmp") + ["--", "true"], capture_output=True).returncode == 0


@unittest.skipUnless(bwrap_works(), "bwrap cannot create DSH's sandbox profile here")
class DshSandboxTest(unittest.TestCase):
    def setUp(self):
        # Not under /tmp: DSH's profile mounts a fresh writable tmpfs there, which would make the
        # "read-only cache" writable (into a throwaway mount) and these tests vacuous. ~/.cache is
        # under the read-only root; so is this directory.
        TEST_TMP.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.p = base / "proj"
        self.cache = base / "cache"     # outside the workspace: read-only in the sandbox, like ~/.cache
        self.cache.mkdir()
        self.p.mkdir()
        (self.p / "src").mkdir()
        (self.p / "src/a.py").write_text("def f(x):\n    return x\n")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t"}
        for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
            subprocess.run(["git", "-C", str(self.p), *cmd], check=True, env=env)
        d = self.p / "_tickets/open"
        d.mkdir(parents=True)
        (d / "T1-x.md").write_text("---\nticket: T1-x\ncomplexity: S\nallowed_files:\n  - src/a.py\n---\nbody\n")
        self.env = {**os.environ, "XDG_CACHE_HOME": str(self.cache), "DSH_HOME": str(base / "dsh")}

    def box(self, *args):
        return subprocess.run(dsh_profile(str(self.p)) + ["--chdir", str(self.p), "--", sys.executable, "-B", BIN,
                                                         *args], capture_output=True, text=True, env=self.env,
                              timeout=120)

    def test_the_cache_really_is_read_only_in_the_profile(self):
        r = subprocess.run(dsh_profile(str(self.p)) + ["--", "touch", str(self.cache / "x")], capture_output=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(b"Read-only file system", r.stderr)
        r = subprocess.run(dsh_profile(str(self.p)) + ["--", "touch", str(self.p / "x")], capture_output=True)
        self.assertEqual(r.returncode, 0)  # the workspace is writable

    def test_manager_commands_succeed_and_moves_are_not_misreported(self):
        commands = [["wf", "start", "T1", "--objective", "o"], ["wf", "advance", "WF-T1", "PLANNING"],
                    ["wf", "advance", "WF-T1", "PLAN_READY"], ["wf", "pack", "WF-T1"], ["map", "--tokens", "200"],
                    ["shards", "list"], ["shards", "allocate", "WF-T1"], ["steward", "init"], ["bb", "list"],
                    ["skill", "mine"], ["lesson", "add", "--text", "keep f pure, no side effects", "--path", "src/a.py"],
                    ["wf", "brief", "WF-T1"], ["wf", "check"], ["plan", "list"], ["profile", "list"],
                    ["wf", "diffsum", "WF-T1", "--base", "HEAD"], ["bench", "report"]]
        for cmd in commands:
            r = self.box(*cmd)
            self.assertEqual(r.returncode, 0, f"{' '.join(cmd)}: {r.stderr.strip()[-300:]}")
        self.assertEqual(workflow.get(self.p, "WF-T1").state, "PLAN_READY")
        self.assertEqual(list(self.cache.iterdir()), [])   # nothing could be, and nothing was, written there

    def test_live_stage_falls_back_to_the_project_and_clears(self):
        self.assertEqual(self.box("wf", "start", "T1", "--objective", "o").returncode, 0)
        self.assertEqual(self.box("wf", "advance", "WF-T1", "PLANNING").returncode, 0)
        os.environ["XDG_CACHE_HOME"] = str(self.cache)
        try:
            st = live.read_stage(time.time(), projects=[str(self.p)])
            self.assertEqual((st["stage"], st["ticket"]), ("planning", "T1"))
            self.assertIn("live-stage.json", (self.p / ".hydra/.gitignore").read_text())
            # a stale stage in the read-only cache must not outlive a clear done inside the sandbox
            live._write(live.stage_file(), {"stage": "dispatching", "ticket": "T0", "project": "x",
                                            "at": time.time() - 5})
            self.assertEqual(self.box("stage", "--clear").returncode, 0)
            self.assertIsNone(live.read_stage(time.time(), projects=[str(self.p)]))
        finally:
            del os.environ["XDG_CACHE_HOME"]


if __name__ == "__main__":
    unittest.main()
