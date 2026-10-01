# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for scripts/install.sh and scripts/check-skill.mjs (run: python3 -B -m unittest discover -s tests)."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "scripts/install.sh"
CHECK = ROOT / "scripts/check-skill.mjs"
HAS_YAML = (Path(os.environ.get("DSH_REPO", Path.home() / "deepseek-harness"))
            / "packages/skill/skill-filesystem/node_modules/yaml").exists()


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dsh_home = self.tmp / "dsh"
        self.hp = self.tmp / "hp"
        (self.hp / "skill/opus-manager").mkdir(parents=True)
        (self.hp / "skill/opus-manager/SKILL.md").write_text("---\nname: opus-manager\n---\n")

    def run_install(self, *args):
        env = dict(os.environ, DSH_HOME=str(self.dsh_home), HYDRA_POD_HOME=str(self.hp))
        return subprocess.run(["bash", str(INSTALL), *args], env=env, capture_output=True, text=True)

    def test_links_both_skills_to_this_checkout(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in ("hydra-pod", "opus-manager"):
            link = self.dsh_home / "skills" / name
            self.assertTrue(link.is_symlink(), name)
            self.assertEqual(link.resolve(), ROOT / "skills" / name)

    def test_second_run_reports_same(self):
        self.run_install()
        r = self.run_install()
        self.assertIn(f"same     {self.dsh_home}/skills/hydra-pod", r.stdout)
        self.assertIn(f"same     {self.dsh_home}/skills/opus-manager", r.stdout)

    def test_existing_entry_left_alone_without_force(self):
        other = self.dsh_home / "skills/hydra-pod"
        other.mkdir(parents=True)
        (other / "SKILL.md").write_text("mine")
        r = self.run_install()
        self.assertIn("differs", r.stdout)
        self.assertNotEqual(r.returncode, 0)  # declining to install is not a silent success
        self.assertEqual((other / "SKILL.md").read_text(), "mine")
        self.assertFalse(other.is_symlink())

    def test_unknown_option_is_rejected(self):
        r = self.run_install("--Force")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown option", r.stderr)

    def test_force_backs_up_outside_skills_dir(self):
        other = self.dsh_home / "skills/hydra-pod"
        other.mkdir(parents=True)
        (other / "SKILL.md").write_text("mine")
        r = self.run_install("--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(other.is_symlink())
        backups = list((self.dsh_home / "backups/hydra-pod-dsh").glob("*/hydra-pod/SKILL.md"))
        self.assertEqual([b.read_text() for b in backups], ["mine"])
        self.assertEqual(sorted(p.name for p in (self.dsh_home / "skills").iterdir()), ["hydra-pod", "opus-manager"])

    def test_missing_hydra_pod_fails_before_linking(self):
        shutil.rmtree(self.hp)
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("MISSING", r.stdout)
        self.assertFalse((self.dsh_home / "skills").exists())


@unittest.skipUnless(HAS_YAML and shutil.which("node"), "needs node and a built dsh checkout")
class CheckSkillTest(unittest.TestCase):
    def check(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(text)
        self.addCleanup(os.unlink, f.name)
        return subprocess.run(["node", str(CHECK), f.name], capture_output=True, text=True)

    def test_shipped_skills_are_valid(self):
        r = subprocess.run(["node", str(CHECK), *map(str, ROOT.glob("skills/*/SKILL.md"))],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_unquoted_colon_in_description_is_rejected(self):
        r = self.check("---\nname: x\ndescription: Managed mode: you act\n---\nbody\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("invalid YAML frontmatter", r.stderr)

    def test_bad_name_and_missing_description(self):
        self.assertIn("invalid skill name", self.check("---\nname: Bad_Name\ndescription: d\n---\n").stderr)
        self.assertIn("requires name and description", self.check("---\nname: ok\n---\n").stderr)

    def test_invocation_keys_must_be_booleans(self):
        r = self.check("---\nname: ok\ndescription: d\nuser-invocable: maybe\n---\n")
        self.assertIn("user-invocable must be true or false", r.stderr)
        self.assertIn('unsupported field "userInvocable"',
                      self.check("---\nname: ok\ndescription: d\nuserInvocable: true\n---\n").stderr)

    def test_hydra_pod_skill_is_slash_only(self):
        text = (ROOT / "skills/hydra-pod/SKILL.md").read_text()
        self.assertIn("disable-model-invocation: true", text)
        self.assertIn("user-invocable: true", text)


class SkillReferencesTest(unittest.TestCase):
    """Every ~/Hydra-Pod path the skills tell the manager to use must exist in Hydra-Pod."""

    def test_referenced_hydra_pod_paths_exist(self):
        import re
        hp = Path(os.environ.get("HYDRA_POD_HOME", Path.home() / "Hydra-Pod"))
        if not hp.is_dir():
            self.skipTest(f"{hp} not present")
        refs = set()
        for skill in [*ROOT.glob("skills/*/SKILL.md"), *ROOT.glob("skills/*/references/*.md")]:
            refs |= set(re.findall(r"~/Hydra-Pod/([\w./-]+[\w/])", skill.read_text()))
        self.assertTrue(refs)
        missing = [r for r in sorted(refs) if not (hp / r).exists()]
        self.assertEqual(missing, [])

    def test_hydra_pod_skill_references_exist(self):
        import re
        skill = ROOT / "skills/hydra-pod"
        refs = set(re.findall(r"`(references/[\w.-]+\.md)`", (skill / "SKILL.md").read_text()))
        self.assertEqual(refs, {"references/first-use.md", "references/ledger.md"})
        self.assertEqual([r for r in sorted(refs) if not (skill / r).is_file()], [])


if __name__ == "__main__":
    unittest.main()
