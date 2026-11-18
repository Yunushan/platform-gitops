"""Exercise read-only host-loss observations and rejection of incomplete evidence."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import verify_ceph_host_recovery as recovery
from test_ceph_storage import FakeClient, fixture, uid


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.plan = fixture()
        self.plan["admitted_sha256"] = "a" * 64
        self.client = FakeClient(self.plan)
        self.objects = recovery.policy.objects(self.plan, "rke2")
        self.receipt = {
            "schema_version": 1, "phase": "completed", "action": "create", "runtime": "rke2",
            "operation_id": "1" * 32, "local_inputs_verified": True, "server_checked": True,
            "storage_resources_provisioned": True, "plan_sha256": recovery.policy.fingerprint(self.plan),
            "toolset_sha256": recovery.storage.toolset(), "observed_admitted_sha256": self.plan["admitted_sha256"],
            "objects": {}, **recovery.policy.UNVERIFIED,
        }
        for obj in self.objects:
            obj["metadata"]["annotations"] = {
                recovery.storage.OPERATION: self.receipt["operation_id"],
                recovery.storage.CONTRACT: self.receipt["plan_sha256"],
            }
            live = json.loads(self.client.call(["create"], payload=json.dumps(obj).encode()))
            self.receipt["objects"][recovery.storage.key(obj)] = {
                "uid": live["metadata"]["uid"], "admitted_sha256": recovery.storage.admitted(obj, live),
            }
        self.views = self.client.views
        self.osd = self.views[("osd", "dump")]
        self.osd.update(fsid=self.plan["provider"]["fsid"], epoch=20)
        for index, row in enumerate(self.osd["osds"]):
            row["uuid"] = uid(200 + index)
        for row in self.osd["pools"]:
            row["pg_num"] = 1
        self.views[("fs", "dump")]["filesystems"][0]["id"] = 7
        self.pgs = [{"pgid": f"{index}.0", "state": "active+clean", "acting": [0, 1, 2], "up": [0, 1, 2],
                     "acting_primary": 0, "up_primary": 0} for index in (1, 2, 3)]
        self.views[("pg", "dump", "pgs_brief")] = {"pg_stats": self.pgs}
        self.client.calls.clear()

    def rebuild(self):
        self.osd["epoch"] += 1
        self.osd["osds"][0].update(up=0, **{"in": 0})
        first = self.client.data[(None, "node/storage-1")]
        first["status"]["conditions"] = [{"type": "Ready", "status": "Unknown"}]
        self.views[("status",)]["quorum_names"] = ["b", "c"]
        self.views[("status",)]["health"]["status"] = "HEALTH_WARN"
        for pg in self.pgs:
            pg.update(acting=[1, 2, 3], up=[1, 2, 3], acting_primary=1, up_primary=1)

    def operation(self, phase, output, directory):
        path = directory / "storage.json"
        if not path.exists():
            path.write_text(json.dumps(self.receipt), encoding="utf-8")
            path.chmod(0o600)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return recovery.operate(self.plan, "rke2", path, digest, "storage-1", phase, output, client=self.client)

    def test_phase_sequence_keeps_receipt_and_requires_clean_recovery(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(recovery.storage, "ROOT", Path(temporary)):
            directory = Path(temporary)
            output = directory / "private" / "drill"
            with self.assertRaises(ValueError):
                self.operation("rebuilt", output, directory)
            self.operation("baseline", output, directory)
            before = (output / "ceph-host-recovery.json").read_bytes()
            self.rebuild()
            self.pgs[0]["state"] = "active+degraded"
            with self.assertRaisesRegex(ValueError, "three clean"):
                self.operation("rebuilt", output, directory)
            self.assertEqual((output / "ceph-host-recovery.json").read_bytes(), before)
            self.pgs[0]["state"] = "active+clean+scrubbing+deep"
            self.assertTrue(self.operation("rebuilt", output, directory)["replica_rebuild_observed"])
            self.osd["epoch"] += 1
            self.osd["osds"][0].update(up=1, **{"in": 1})
            self.client.data[(None, "node/storage-1")]["status"]["conditions"][0]["status"] = "True"
            self.views[("status",)]["quorum_names"] = ["a", "b", "c"]
            self.views[("status",)]["health"]["status"] = "HEALTH_OK"
            self.assertTrue(self.operation("returned", output, directory)["original_host_return_observed"])
            self.assertFalse(json.loads((output / "ceph-host-recovery.json").read_bytes())["application_io_verified"])

        self.assertTrue(all(call[0] in {"get", "exec", "config"} for call in self.client.calls))
        self.assertEqual({tuple(call[call.index("ceph") + 2:-1]) for call in self.client.calls if call[0] == "exec"},
                         set(recovery.READS))

    def test_rebuild_refuses_missing_groups_reused_osd_and_additional_host_loss(self):
        sample = recovery.collect(self.client, self.plan, self.objects, self.receipt)
        baseline = recovery.assess(self.plan, self.objects, sample, "storage-1", "baseline")
        self.rebuild()
        original = recovery.collect(self.client, self.plan, self.objects, self.receipt)
        for fault in ("missing_pg", "changed_uuid", "another_host_down", "pool_replaced"):
            failed = copy.deepcopy(original)
            if fault == "missing_pg":
                failed[5].pop()
            elif fault == "changed_uuid":
                failed[0]["osds"][0]["uuid"] = uid(804)
            elif fault == "another_host_down":
                failed[6]["storage-2"] = False
            else:
                failed[0]["pools"][0]["pool"] = 4
                failed[5][0]["pgid"] = "4.0"
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                recovery.assess(self.plan, self.objects, failed, "storage-1", "rebuilt", baseline)


if __name__ == "__main__":
    unittest.main()
