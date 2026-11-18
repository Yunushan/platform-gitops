#!/usr/bin/env python3
"""Simulate CNPG status CAS, uncertain acknowledgments and planned switchover observation."""

import copy
import hashlib
import itertools
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from bounded_subprocess import run_bounded
import deploy_postgres_ha as deploy
import manage_postgres_switchover as switch
import test_postgres_installation as installation
import test_postgres_runtime as runtime_tests
import verify_postgres_runtime as runtime


def apply_patch(document, patch):
    result = copy.deepcopy(document)
    for item in patch:
        keys = [key.replace("~1", "/").replace("~0", "~") for key in item["path"].split("/")[1:]]
        parent = result
        for key in keys[:-1]: parent = parent[key]
        if item["op"] == "test":
            if parent.get(keys[-1]) != item["value"]: raise ValueError("Fixture resourceVersion or scope conflict")
        elif item["op"] == "add": parent[keys[-1]] = copy.deepcopy(item["value"])
        else: raise AssertionError("Unexpected JSON patch operation")
    return result


class SwitchClient(runtime_tests.SQLClient):
    def __init__(self, *args):
        super().__init__(*args)
        self.patch_attempts = 0; self.committed = 0; self.finish = True; self.lost_patch_ack = False; self.reject_patch = False
        self.after_dry = None; self.before_patch = None; self.after_commit = None; self.dry_mutator = None

    def make_ready(self):
        super().make_ready()
        self.cluster["metadata"]["resourceVersion"] = "100"
        self.cluster["status"]["phase"] = switch.HEALTHY

    def finish_switchover(self):
        status = self.cluster["status"]; target = status["targetPrimary"]
        source = status["currentPrimary"]
        status.update(currentPrimary=target, currentPrimaryTimestamp=status["targetPrimaryTimestamp"], phase=switch.HEALTHY,
                      phaseReason="", conditions=installation.conditions("Ready"))
        for name, state in status["instancesReportedState"].items(): state.update(isPrimary=name == target, timeLineID=2)
        pods = self.collections["pods", self.namespace]; new_primary = next(p for p in pods if p["metadata"]["name"] == target)
        endpoint = self.collections["endpointslices.discovery.k8s.io", self.namespace][0]["endpoints"][0]
        endpoint.update(addresses=[new_primary["status"]["podIP"]], targetRef={"kind": "Pod", "namespace": self.namespace, "name": target, "uid": new_primary["metadata"]["uid"]})
        old_status = self.objects["pod/" + source]["status"]["containerStatuses"][0]
        old_status.update(containerID="containerd://" + "e" * 64, restartCount=1, state={"running": {"startedAt": "2026-11-07T01:00:00Z"}})
        addresses = {p["metadata"]["name"]: p["status"]["podIP"] for p in pods}
        for name in list(self.samples):
            row = runtime_tests.sample(name == target); row["timeline_id"] = 2
            if name == source: row["postmaster_started_at"] = "1790000001.123456"
            if name == target:
                peers = sorted(set(addresses) - {target})
                row["settings"]["synchronous_standby_names"]["value"] = "ANY 1 (" + ",".join(json.dumps(peer) for peer in peers) + ")"
                for sender, peer in zip(row["senders"], peers): sender.update(application_name=peer, client_addr=addresses[peer])
            else: row["receivers"][0]["received_tli"] = 2
            self.samples[name] = row

    def call(self, args, payload=None, seconds=30):
        if args[0] != "patch": return super().call(args, payload, seconds)
        self.calls.append(tuple(args))
        path = Path(args[args.index("--patch-file") + 1])
        if payload is not None or not path.is_file() or os.name == "posix" and path.stat().st_mode & 0o077: raise AssertionError("Require a private temporary patch file")
        patch = json.loads(path.read_text())
        if "--dry-run=server" in args:
            result = apply_patch(self.cluster, patch)
            if self.dry_mutator: self.dry_mutator(result)
            if self.after_dry: self.after_dry(self)
            return json.dumps(result)
        self.patch_attempts += 1
        if self.before_patch: self.before_patch(self)
        if self.reject_patch: raise TimeoutError("Fixture request outcome unknown")
        self.cluster = apply_patch(self.cluster, patch); self.committed += 1
        self.cluster["metadata"]["resourceVersion"] = "101"
        result = copy.deepcopy(self.cluster)
        if self.finish: self.finish_switchover()
        if self.after_commit: self.after_commit(self)
        if self.lost_patch_ack:
            self.lost_patch_ack = False; raise TimeoutError("Fixture status acknowledgment lost")
        return json.dumps(result)


class SwitchoverTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("POSTGRES_PROMOTION_TEST_RUNTIME") == "1", "Explicit isolated actual PostgreSQL promotion fixture")
    def test_actual_clean_shutdown_checkpoint_promotion_and_strict_replica_rejoin(self):
        from test_postgres_runtime_fixture import exercise
        results = []
        exercise(self, hook=lambda state: results.append(state["promote"]()), planned_promotion=True)
        self.assertEqual(len(results), 1)
        for key in ("source_fenced", "shutdown_checkpoint_replayed", "target_original_process_preserved", "strict_three_member_quorum_restored", "timeline_advanced"):
            self.assertIs(results[0][key], True)

    def prepare(self, root, backend="rke2"):
        helper = installation.InstallationTests()
        bundle, deployment, original = helper.prepare(root, backend)
        client = SwitchClient(deployment, original.selected)
        helper.run_install(root, bundle, deployment, client)
        install_path = root / "private/operation/postgres-install-proof.json"
        install_sha = hashlib.sha256(install_path.read_bytes()).hexdigest()
        with mock.patch.object(runtime, "ROOT", root):
            runtime.operate(bundle, deployment, install_path, install_sha, observe=True, output=root / "private/runtime", client=client)
        runtime_path = root / "private/runtime/postgres-runtime-proof.json"
        proof = json.loads(runtime_path.read_text())
        plan = {"schema_version": 1, "contract_sha256": deployment["contract_sha256"], "deployment_plan_sha256": deploy.fingerprint(deployment),
            "installation_sha256": install_sha, "runtime_sha256": hashlib.sha256(runtime_path.read_bytes()).hexdigest(),
            "source_primary": {"name": "platform-postgres-1", "uid": proof["topology"]["pods"]["platform-postgres-1"]["uid"]},
            "target_primary": {"name": "platform-postgres-2", "uid": proof["topology"]["pods"]["platform-postgres-2"]["uid"]}}
        client.calls.clear()
        return bundle, deployment, plan, install_path, runtime_path, client

    def run_switch(self, root, prepared, mode="apply"):
        bundle, deployment, plan, install_path, runtime_path, client = prepared
        with mock.patch.object(switch, "ROOT", root), mock.patch.object(switch.time, "sleep"):
            return switch.operate(bundle, deployment, plan, install_path, runtime_path, mode=mode, output=root / "private/switchover", wait_seconds=5, client=client)

    def test_default_is_local_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); bundle, deployment, plan, install_path, runtime_path, client = self.prepare(root)
            with mock.patch.object(switch, "Client", side_effect=AssertionError("constructed")):
                result = switch.operate(bundle, deployment, plan, install_path, runtime_path)
            self.assertTrue(result["local_inputs_verified"]); self.assertTrue(result["read_only"])
            self.assertFalse(client.calls); self.assertFalse((root / "private/switchover").exists())

    def test_three_backends_request_once_then_resume_observation_only(self):
        for backend in ("rke2", "k3s", "k8s"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); prepared = self.prepare(root, backend); result = self.run_switch(root, prepared)
                self.assertTrue(result["planned_switchover_observed"]); self.assertFalse(result["read_only"])
                self.assertEqual(prepared[-1].committed, 1)
                for key in runtime.UNVERIFIED: self.assertIs(result[key], False)
                resumed = self.run_switch(root, prepared, "resume")
                self.assertTrue(resumed["planned_switchover_observed"]); self.assertTrue(resumed["read_only"])
                self.assertEqual(prepared[-1].patch_attempts, 1)
                calls = prepared[-1].calls
                self.assertTrue(all("--subresource=status" in call and "--type=json" in call and "--patch-file" in call for call in calls if call[0] == "patch"))
                self.assertFalse(any(call[0] in {"delete", "create", "apply", "replace"} for call in calls))

    def test_server_check_uses_only_dry_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); prepared = self.prepare(root); result = self.run_switch(root, prepared, "check-server")
            self.assertTrue(result["server_checked"]); self.assertTrue(result["read_only"])
            self.assertEqual(prepared[-1].patch_attempts, 0); self.assertFalse(result["planned_switchover_observed"])
            self.assertEqual(prepared[-1].cluster["status"]["currentPrimary"], prepared[2]["source_primary"]["name"])

    def test_lost_acknowledgment_observes_same_request_without_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); prepared = self.prepare(root); prepared[-1].lost_patch_ack = True
            with self.assertRaises(TimeoutError): self.run_switch(root, prepared)
            self.assertTrue(self.run_switch(root, prepared, "resume")["planned_switchover_observed"])
            self.assertEqual(prepared[-1].patch_attempts, 1)

    def test_uncertain_request_without_observed_effect_is_never_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); prepared = self.prepare(root); prepared[-1].reject_patch = True
            with self.assertRaises(TimeoutError): self.run_switch(root, prepared)
            with mock.patch.object(switch.time, "monotonic", side_effect=itertools.count()), self.assertRaises(TimeoutError): self.run_switch(root, prepared, "resume")
            self.assertEqual(prepared[-1].patch_attempts, 1); self.assertEqual(prepared[-1].committed, 0)

    def test_pending_request_uses_same_handle_until_operator_finishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); prepared = self.prepare(root); prepared[-1].finish = False
            with mock.patch.object(switch.time, "monotonic", side_effect=itertools.count()), self.assertRaises(TimeoutError): self.run_switch(root, prepared)
            prepared[-1].finish_switchover()
            self.assertTrue(self.run_switch(root, prepared, "resume")["planned_switchover_observed"])
            self.assertEqual(prepared[-1].patch_attempts, 1)

    def test_scope_or_evidence_changes_refuse_before_server_access(self):
        changes = [lambda p: p[2]["target_primary"].update(uid=installation.uid(999)), lambda p: p[2].update(runtime_sha256="a" * 64),
                   lambda p: p[2].update(deployment_plan_sha256="a" * 64), lambda p: p[2]["target_primary"].update(name=p[2]["source_primary"]["name"])]
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); prepared = self.prepare(root); change(prepared)
                with self.assertRaises(ValueError): self.run_switch(root, prepared)
                self.assertFalse(prepared[-1].calls)

    def test_changed_live_database_or_target_refuses_before_request(self):
        changes = [lambda c: c.objects["pod/platform-postgres-2"]["metadata"].update(uid=installation.uid(999)),
                   lambda c: c.samples["platform-postgres-2"].update(system_identifier="2222222222222222222"),
                   lambda c: c.samples["platform-postgres-1"]["settings"]["fsync"].update(value="off"),
                   lambda c: c.cluster["status"].update(phase="Failing over"), lambda c: c.cluster["metadata"].update(uid=installation.uid(999))]
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); prepared = self.prepare(root); change(prepared[-1])
                with self.assertRaises(ValueError): self.run_switch(root, prepared)
                self.assertEqual(prepared[-1].patch_attempts, 0)

    def test_dry_admission_or_race_after_review_cannot_weaken_scope(self):
        changes = [("dry_mutator", lambda obj: obj["spec"].update(instances=1)),
                   ("dry_mutator", lambda obj: obj["status"].update(targetPrimary="other")),
                   ("after_dry", lambda c: c.samples["platform-postgres-2"].update(postmaster_started_at="1790000002.123456")),
                   ("after_dry", lambda c: c.objects["persistentvolume/volume-1"]["metadata"].update(uid=installation.uid(999)))]
        for attribute, change in changes:
            with self.subTest(attribute=attribute), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); prepared = self.prepare(root); setattr(prepared[-1], attribute, change)
                with self.assertRaises(ValueError): self.run_switch(root, prepared)
                self.assertEqual(prepared[-1].patch_attempts, 0)

    def test_resource_version_cas_prevents_late_status_race(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); prepared = self.prepare(root)
            prepared[-1].before_patch = lambda c: c.cluster["metadata"].update(resourceVersion="999")
            with self.assertRaises(ValueError): self.run_switch(root, prepared)
            self.assertEqual(prepared[-1].committed, 0)
            with mock.patch.object(switch.time, "monotonic", side_effect=itertools.count()), self.assertRaises(TimeoutError): self.run_switch(root, prepared, "resume")
            self.assertEqual(prepared[-1].patch_attempts, 1)

    def test_replacement_storage_target_process_or_unadvanced_timeline_refuses_success(self):
        def unchanged_timeline(c):
            for row in c.samples.values():
                row["timeline_id"] = 1
                if row["receivers"]: row["receivers"][0]["received_tli"] = 1
        changes = [lambda c: c.objects["persistentvolume/volume-1"]["metadata"].update(uid=installation.uid(999)),
                   lambda c: c.objects["pod/platform-postgres-2"]["metadata"].update(uid=installation.uid(999)),
                   lambda c: c.samples["platform-postgres-2"].update(postmaster_started_at="1790000002.123456"), unchanged_timeline]
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); prepared = self.prepare(root); prepared[-1].after_commit = change
                with self.assertRaises(ValueError): self.run_switch(root, prepared)
                state = json.loads((root / "private/switchover/postgres-switchover-proof.json").read_text())
                self.assertFalse(state["planned_switchover_observed"]); self.assertEqual(prepared[-1].patch_attempts, 1)

    def test_superseded_request_and_changed_journal_never_issue_another_patch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); prepared = self.prepare(root); prepared[-1].lost_patch_ack = True
            with self.assertRaises(TimeoutError): self.run_switch(root, prepared)
            prepared[-1].cluster["status"]["targetPrimaryTimestamp"] = "2026-11-07T01:00:00Z"
            with self.assertRaises(ValueError): self.run_switch(root, prepared, "resume")
            self.assertEqual(prepared[-1].patch_attempts, 1)
            path = root / "private/switchover/status-request.json"; path.write_text("[]")
            prepared[-1].calls.clear()
            with self.assertRaises(ValueError): self.run_switch(root, prepared, "resume")
            self.assertFalse(prepared[-1].calls)

    def test_tooling_change_and_parallel_operation_lock_refuse_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); prepared = self.prepare(root); self.run_switch(root, prepared); prepared[-1].calls.clear()
            with mock.patch.object(switch, "toolset", return_value="a" * 64), self.assertRaises(ValueError): self.run_switch(root, prepared, "resume")
            self.assertFalse(prepared[-1].calls)
            with deploy.lock(root / "private/switchover"), self.assertRaises(OSError): self.run_switch(root, prepared, "resume")

    def test_completion_evidence_failure_retains_request_but_cannot_leave_success_true(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); prepared = self.prepare(root)
            writer = switch.atomic_write_text
            def fail_completion(path, value):
                if path.name.endswith("proof.json") and json.loads(value)["phase"] == "completed": raise OSError("Fixture completion write failed")
                return writer(path, value)
            with mock.patch.object(switch, "atomic_write_text", side_effect=fail_completion), self.assertRaises(OSError): self.run_switch(root, prepared)
            receipt = json.loads((root / "private/switchover/postgres-switchover-proof.json").read_text())
            self.assertEqual(receipt["phase"], "failed"); self.assertFalse(receipt["planned_switchover_observed"])
            self.assertTrue(receipt["request_intent"]); self.assertEqual(prepared[-1].patch_attempts, 1)
            self.assertTrue(self.run_switch(root, prepared, "resume")["planned_switchover_observed"])
            self.assertEqual(prepared[-1].patch_attempts, 1)

    def test_optional_status_request_matches_vendored_openapi(self):
        if os.environ.get("POSTGRES_POLICY_TEST_SCHEMA") != "1": self.skipTest("Opt in to jsonschema validation")
        import jsonschema
        with tempfile.TemporaryDirectory() as temporary:
            prepared = self.prepare(Path(temporary)); current = prepared[-1].cluster
            patch = switch.make_patch(current, prepared[2], "2026-11-07T01:00:00Z", "a" * 32)
            schema = json.loads(deploy.renderer.cnpg_schema_text())
            errors = list(jsonschema.Draft7Validator(schema).iter_errors(apply_patch(current, patch)))
            self.assertFalse(errors, "The planned status request does not match the vendored CNPG OpenAPI schema")

    def test_optional_real_kubectl_local_status_patch(self):
        binary = os.environ.get("POSTGRES_SWITCH_TEST_KUBECTL")
        if not binary: self.skipTest("Opt in to client-only real kubectl JSON patch verification")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); prepared = self.prepare(root); client = prepared[-1]
            source = root / "cluster.json"; source.write_text(json.dumps(client.cluster))
            config = root / "offline-config"; config.write_text(json.dumps({"apiVersion": "v1", "kind": "Config", "current-context": "offline", "contexts": [{"name": "offline", "context": {"cluster": "offline", "user": "offline"}}],
                "clusters": [{"name": "offline", "cluster": {"server": "https://127.0.0.1:1"}}], "users": [{"name": "offline", "user": {"token": "fixture-only-unused-token"}}]}))
            test = self
            class LocalClient:
                def call(self, args, seconds=30):
                    test.assertEqual(args[:2], ["patch", deploy.CLUSTER + client.name])
                    request = Path(args[args.index("--patch-file") + 1]); test.assertTrue(request.is_file())
                    if os.name == "posix": test.assertEqual(request.stat().st_mode & 0o077, 0)
                    command = [binary, "--kubeconfig", str(config), "--context", "offline", "patch", "--local=true", "-f", str(source), *args[2:]]
                    result = run_bounded(command, input="", text=True, check=False, timeout=20, output_max_bytes=256 * 1024)
                    if result.returncode: raise ValueError("Client-only JSON patch rejected")
                    return result.stdout
            patch = switch.make_patch(client.cluster, prepared[2], "2026-11-07T01:00:00Z", "a" * 32)
            result = switch.patch_request(LocalClient(), client.selected, patch, dry=False)
            self.assertEqual(result, apply_patch(client.cluster, patch))
            invalid = copy.deepcopy(patch); invalid[0]["value"] = installation.uid(999)
            with self.assertRaises(ValueError): switch.patch_request(LocalClient(), client.selected, invalid, dry=False)


if __name__ == "__main__": unittest.main()
