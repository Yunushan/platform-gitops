#!/usr/bin/env python3
"""Exercise scheduled handoff plan freezing and overlap protection."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import ci_gitops_handoff as handoff
import gitlab_gitops_schedule as scheduler
import test_gitlab_gitops_handoff as handoff_fixture


@unittest.skipUnless(os.name == "posix", "scheduler uses POSIX advisory locks")
class SchedulerTest(unittest.TestCase):
    def fixture(self, root):
        case = handoff_fixture.GitLabHandoffTest()
        fixed, _, _ = case.setup_case(root, "argocd")
        pipeline = {k: v for k, v in fixed["pipeline"].items() if k not in {"pipeline_id", "revision"}}
        plan = {"schema_version": 2, "runtime": "rke2", "pipeline_policy": pipeline,
                "handoff": dict(fixed["handoff"], bridge_contract_sha256=handoff.sha256(pipeline))}
        path = root / "plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        (root / "private").mkdir(exist_ok=True)
        return plan, path

    def test_pins_plan_and_creates_unique_proofs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, path = self.fixture(root)
            digest = handoff.sha256(plan)
            observed = []
            def operate(frozen, **kwargs):
                path.write_text("{}", encoding="utf-8")
                self.assertEqual(json.loads(frozen.read_text(encoding="utf-8")), plan)
                self.assertTrue(kwargs["check_server"])
                self.assertFalse(kwargs["apply"])
                observed.append(kwargs["output"])
                return {"read_only": True, "controller_verified": True}
            with mock.patch.object(scheduler.adapter, "operate", side_effect=operate):
                first = scheduler.run(path, digest, root=root)
                path.write_text(json.dumps(plan), encoding="utf-8")
                second = scheduler.run(path, digest, root=root)
            self.assertTrue(first["started"] and second["started"])
            self.assertNotEqual(observed[0], observed[1])
            self.assertFalse(list((root / "private").glob(".gitlab-gitops-plan-*")))
            with self.assertRaises(ValueError): scheduler.run(path, "0" * 64, root=root)

    def test_existing_lock_prevents_second_run(self):
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, path = self.fixture(root)
            digest = handoff.sha256(plan)
            lock = root / "private" / ("gitlab-gitops-" + digest + ".lock")
            with open(lock, "w") as holder:
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.object(scheduler.adapter, "operate") as operate:
                    result = scheduler.run(path, digest, root=root)
                self.assertEqual(result["reason"], "already-running")
                operate.assert_not_called()

    def test_plan_file_is_removed_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, path = self.fixture(root)
            with mock.patch.object(scheduler.adapter, "operate", side_effect=ValueError("fixture")):
                with self.assertRaises(ValueError): scheduler.run(path, handoff.sha256(plan), apply=True, root=root)
            self.assertFalse(list((root / "private").glob(".gitlab-gitops-plan-*")))


if __name__ == "__main__": unittest.main()
