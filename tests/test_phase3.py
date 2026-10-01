# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for phase 3 of the technical paper: registry v2 pools, adapters, quota-aware
routing with fallbacks, the review cascade and team profiles."""
import copy
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

from hydra_pod_dsh import adapters, ledger, profiles, router, workflow  # noqa: E402

CLI = [sys.executable, "-B", str(ROOT / "bin/hydra-pod-dsh")]
UP = lambda a: (True, "")  # noqa: E731
FREE = lambda a: {"percent": 10.0, "source": "official", "exhausted": False}  # noqa: E731


def registry() -> dict:
    """The shipped registry plus a second executor and a second-vendor read-only reviewer stage."""
    reg = router.load()
    reg["agents"]["builder-strong"] = {"role": "executor", "runtime": "opencode", "model": "opencode-go/big-model",
                                       "billing": "subscription/opencode-go", "vendor": "deepseek",
                                       "capabilities": ["coding"]}
    reg["agents"]["reviewer-second"] = {"role": "specialist", "runtime": "dsh-subagent", "model": "other/model",
                                        "billing": "api/anthropic", "vendor": "anthropic", "read_only": "test",
                                        "capabilities": ["code-review"]}
    reg["pools"]["executor"]["members"] = [
        {"agent": "builder", "tier": "fast", "for": ["S", "M"]},
        {"agent": "builder-strong", "tier": "strong", "for": ["L"]},
    ]
    reg["pools"]["reviewer"]["stages"].append({"agent": "reviewer-second", "when": ["risk:high", "disagreement"]})
    return reg


class AdapterContractTest(unittest.TestCase):
    """Every registered adapter, and every runtime an agent uses, answers the same way."""

    def test_every_agent_runtime_with_a_worker_role_has_an_adapter(self):
        reg = router.load()
        for name, a in reg["agents"].items():
            if a["role"] in ("executor", "reviewer", "specialist"):
                self.assertIsNotNone(adapters.for_runtime(a["runtime"]), name)

    def test_contract(self):
        agent = {"model": "m/x", "billing": "api/anthropic", "role": "reviewer"}
        for runtime, ad in adapters.ADAPTERS.items():
            ok, why = ad.available(agent)
            self.assertIsInstance(ok, bool)
            self.assertIsInstance(why, str)
            d = ad.dispatch(agent, "reviewer", "T1")
            self.assertIn(d["kind"], ("shell", "dsh-tool"), runtime)
            self.assertIn("T1", d["command"])

    def test_window_quota(self):
        q = adapters.window_quota({"source": "estimate", "windows": [{"percent": 50}, {"percent": 86}]})
        self.assertEqual((q["percent"], q["exhausted"]), (86, True))  # estimate: exhausted from 85%
        q = adapters.window_quota({"source": "official", "windows": [{"percent": 90}]})
        self.assertFalse(q["exhausted"])
        self.assertIsNone(adapters.window_quota({"windows": []})["percent"])

    def test_dispatch_commands(self):
        self.assertEqual(adapters.for_runtime("opencode").dispatch({}, "executor", "T1")["command"],
                         "hydra-pod-dispatch build T1")
        self.assertEqual(adapters.for_runtime("zcode").dispatch({}, "reviewer", "T1")["command"],
                         "hydra-pod-dispatch review T1 --reviewer zcode")
        with self.assertRaises(ValueError):
            adapters.for_runtime("zcode").dispatch({}, "executor", "T1")


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "agents.json").write_text((ROOT / "agents.json").read_text())
        (self.dir / "agents.d").mkdir()

    def test_shipped_registry_is_clean(self):
        self.assertEqual(router.violations(), [])

    def test_extension_adds_agents_but_not_policy(self):
        (self.dir / "agents.d/10-extra.json").write_text(json.dumps({
            "agents": {"builder-alt": {"role": "executor", "runtime": "opencode", "model": "opencode-go/alt",
                                       "billing": "subscription/opencode-go", "vendor": "deepseek"},
                       "builder": {"role": "executor", "runtime": "opencode", "model": "evil/x", "billing": "api/x"}},
            "policy": {"allowed_billing": {"executor": ["api/x"]}}}))
        reg = router.load(self.dir / "agents.json")
        self.assertIn("builder-alt", reg["agents"])
        self.assertEqual(reg["agents"]["builder"]["model"], "opencode-go/deepseek-v4.1-flash")  # not overridden
        self.assertEqual(reg["policy"]["allowed_billing"]["executor"], ["subscription/opencode-go"])
        v = router.violations(reg)
        self.assertTrue(any("only agents.json may set the policy" in x for x in v))
        self.assertTrue(any("agent builder already exists" in x for x in v))

    def test_pool_violations(self):
        reg = registry()
        self.assertEqual(router.pool_violations(reg), [])
        bad = copy.deepcopy(reg)
        bad["pools"]["reviewer"]["stages"][1]["agent"] = "reviewer-zcode"   # same vendor as... not executor, but
        bad["agents"]["reviewer-zcode"]["vendor"] = "deepseek"              # now it is the executor's vendor
        bad["pools"]["executor"]["members"].append({"agent": "nobody"})
        bad["agents"]["reviewer-second"].pop("read_only")
        bad["pools"]["reviewer"]["stages"].append({"agent": "reviewer-second", "when": ["sometimes"]})
        v = router.pool_violations(bad)
        self.assertTrue(any("vendor other than the executor's" in x for x in v), v)
        self.assertTrue(any("unknown agent 'nobody'" in x for x in v), v)
        self.assertTrue(any("reviewer-second has no enforced read-only" in x for x in v), v)
        self.assertTrue(any("unknown when" in x for x in v), v)


class ChooseTest(unittest.TestCase):
    def test_complexity_selects_the_tier(self):
        reg = registry()
        self.assertEqual(router.choose("executor", "S", reg, FREE, UP)["agent"], "builder")
        self.assertEqual(router.choose("executor", "L", reg, FREE, UP)["agent"], "builder-strong")

    def test_exhausted_window_falls_back(self):
        reg = registry()
        reg["pools"]["executor"]["members"] = [{"agent": "builder"}, {"agent": "builder-strong"}]
        quota = lambda a: {"percent": 99.0, "source": "estimate", "exhausted": a["model"].endswith("flash")}  # noqa
        r = router.choose("executor", "S", reg, quota, UP)
        self.assertEqual(r["agent"], "builder-strong")
        self.assertIn("subscription window at 99.0%", r["considered"][0]["why"])

    def test_nothing_available_is_a_policy_error(self):
        reg = registry()
        with self.assertRaises(router.PolicyError) as e:
            router.choose("executor", "M", reg, FREE, lambda a: (False, "opencode not on PATH"))
        self.assertIn("opencode not on PATH", str(e.exception))
        with self.assertRaises(router.PolicyError):
            router.choose("executor", "XL", reg, FREE, UP)

    def test_role_and_billing_still_hard(self):
        reg = registry()
        reg["pools"]["executor"]["members"] = [{"agent": "reviewer"}, {"agent": "builder"}]
        r = router.choose("executor", None, reg, FREE, UP)
        self.assertEqual(r["agent"], "builder")
        self.assertIn("cannot serve as executor", r["considered"][0]["why"])

    def test_reviewer_choice_uses_stage_one_and_its_fallback(self):
        reg = registry()
        down = lambda a: (a["runtime"] != "opencode", "opencode not on PATH")  # noqa: E731
        self.assertEqual(router.choose("reviewer", None, reg, FREE, down)["agent"], "reviewer-zcode")


class ReviewPlanTest(unittest.TestCase):
    REJECTED_HIGH = {"reports": [{"findings": [{"status": "rejected", "severity": "high"}], "items": []}]}

    def stages(self, risk=None, fs=None, second=None):
        return [(s["stage"], s["needed"], s["agent"]) for s in
                router.review_plan(risk, fs or {"reports": []}, registry(), second, FREE, UP)]

    def test_second_stage_only_when_needed(self):
        self.assertEqual(self.stages(), [(1, True, "reviewer"), (2, False, "reviewer-second")])
        self.assertEqual(self.stages(risk="high")[1][1], True)
        self.assertEqual(self.stages(fs=self.REJECTED_HIGH)[1][1], True)
        rejected_rc = {"reports": [{"findings": [], "items": [{"status": "rejected", "kind": "conclusion"}]}]}
        self.assertEqual(self.stages(fs=rejected_rc)[1][1], True)

    def test_profile_overrides_later_stages_only(self):
        self.assertEqual(self.stages(risk="high", second="never"), [(1, True, "reviewer"), (2, False, "reviewer-second")])
        self.assertEqual(self.stages(second="always")[1][1], True)

    def test_shipped_registry_has_one_stage(self):
        plan = router.review_plan("high", {"reports": []}, None, None, FREE, UP)
        self.assertEqual([(s["stage"], s["agent"]) for s in plan], [(1, "reviewer")])


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.p / ".cache")})
        env.start()
        self.addCleanup(env.stop)

    def test_default_set_and_refuse(self):
        self.assertEqual(set(profiles.available()), {"economy", "balanced", "max-quality"})
        self.assertEqual(profiles.active(self.p)["name"], "balanced")
        profiles.set_active(self.p, "economy")
        self.assertEqual(profiles.active(self.p)["second_review"], "never")
        for bad in ("../x", "nope"):
            with self.assertRaises(ValueError):
                profiles.set_active(self.p, bad)
        (self.p / ".hydra/profile").write_text("ghost\n")
        with self.assertRaises(ValueError):
            profiles.active(self.p)

    def test_cli_pick_records_and_reviewers(self):
        workflow.create(self.p, "T1", "o")
        d = self.p / "_tickets/open"
        d.mkdir(parents=True)
        (d / "T1-x.md").write_text("---\nticket: T1-x\nrisk: high\nallowed_files:\n  - a.py\n---\n")
        # Agents' availability depends on this machine; fake a PATH with both tools.
        bindir = self.p / "bin"
        bindir.mkdir()
        for tool in ("opencode", "hydra-pod-connect"):
            (bindir / tool).write_text("#!/bin/sh\nexit 0\n")
            (bindir / tool).chmod(0o755)
        env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(self.p)}
        r = subprocess.run(CLI + ["pick", "executor", "--complexity", "S", "--wf", "WF-T1", "--project", str(self.p)],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        ev = [e for e in ledger.read(self.p) if e["type"] == "hydra/route-decision"]
        self.assertEqual((len(ev), ev[0]["payload"]["agent"], ev[0].get("ignorable")), (1, "builder", True))
        r = subprocess.run(CLI + ["wf", "reviewers", "WF-T1", "--project", str(self.p), "--json"],
                           capture_output=True, text=True, env=env)
        self.assertIn(r.returncode, (0, 3), r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["risk"], out["profile"], out["stages"][0]["stage"]), ("high", "balanced", 1))


if __name__ == "__main__":
    unittest.main()
