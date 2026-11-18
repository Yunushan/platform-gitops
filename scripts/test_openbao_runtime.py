#!/usr/bin/env python3
"""Verify operational observations cannot become broad HA or identity claims."""

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import verify_openbao_runtime as runtime


def fixture():
    plan = json.loads((Path(__file__).resolve().parents[1] / "examples/openbao-runtime.json").read_text())
    plan["cluster"]["ca_file"] = str(Path(tempfile.gettempdir()).resolve() / "fixture-ca.crt")
    plan["probe"]["value_sha256"] = hashlib.sha256(b"non-sensitive-canary").hexdigest()
    return plan


class Factory:
    def __init__(self, plan):
        self.plan = copy.deepcopy(plan); self.calls = []; self.health_calls = 0; self.bad = None
    def __call__(self, plan):
        owner = self
        class Client:
            def read(self, path, *, capabilities=None, denied=False):
                source = plan["source"]; restricted = source["token_env"] == owner.plan["probe"]["token_env"]
                member = next(m for m in owner.plan["cluster"]["members"] if m["api_url"] == source["url"])
                owner.calls.append((source["url"], path, restricted, capabilities is not None, denied))
                if path.startswith("sys/health"):
                    owner.health_calls += 1
                    return {"initialized": True, "sealed": owner.bad == "sealed", "cluster_id": "wrong" if owner.bad == "cluster" else source["cluster_id"],
                        "version": "old" if owner.bad == "version" else source["version"],
                        "standby": member["name"] != ("bao-b" if owner.bad == "leader-change" and owner.health_calls > 3 else "bao-a")}
                if capabilities is not None:
                    scope = ({owner.plan["probe"]["path"]: {"read"}, **{p: {"deny"} for p in (*runtime.DENIED, *owner.plan["probe"]["denied_paths"])}}
                        if restricted else {**runtime.OBSERVER_READS, **{p: {"deny"} for p in runtime.PRIVILEGED}})
                    if owner.bad == "root": return {p: ["root"] for p in capabilities}
                    if owner.bad == "probe-write" and restricted: scope[owner.plan["probe"]["path"]] = {"read", "update"}
                    return {p: list(scope[p]) for p in capabilities}
                if path == "sys/storage/raft/configuration":
                    rows = [{"node_id": m["name"], "address": m["raft_address"], "leader": i == 0, "voter": True} for i,m in enumerate(owner.plan["cluster"]["members"])]
                    if owner.bad == "voter": rows[2]["voter"] = False
                    if owner.bad == "address": rows[2]["address"] = "unexpected:8201"
                    return {"data": {"config": {"servers": rows}}}
                if path == "sys/audit":
                    devices = {n: {"type": d["type"], "options": {"file_path": d["file_path"], "mode": "0600", "log_raw": "false"}} for n,d in owner.plan["audit_devices"].items()}
                    options = next(iter(devices.values()))["options"]
                    if owner.bad == "raw-audit": options["log_raw"] = "true"
                    if owner.bad == "public-audit": options["mode"] = "0644"
                    if owner.bad == "extra-audit": devices["unexpected/"] = {}
                    return {"data": devices}
                if denied:
                    if owner.bad == "deny-read": raise ValueError("Expected 403 was instead 200")
                    return None
                if path == owner.plan["probe"]["path"]:
                    return {"data": {"data": {"value": "wrong" if owner.bad == "canary" else "non-sensitive-canary"}}}
                raise AssertionError("Unreviewed request")
        return Client()


class RuntimeTests(unittest.TestCase):
    def test_local_mode_constructs_no_client_or_credentials(self):
        plan = fixture()
        result = runtime.operate(plan, factory=mock.Mock(side_effect=AssertionError("No connection")))
        self.assertTrue(result["local_inputs_verified"]); self.assertFalse(result["live_observations_verified"])
    def test_live_reads_all_three_members_and_returns_only_narrow_observations(self):
        plan = fixture(); factory = Factory(plan); result = runtime.operate(plan, live=True, factory=factory)
        self.assertTrue(result["live_observations_verified"])
        for flag in runtime.FALSE_FLAGS: self.assertIs(result[flag], False)
        self.assertNotIn("non-sensitive-canary", json.dumps(result))
        self.assertEqual(len({c[0] for c in factory.calls if c[2]}), 3)
        digest = result.pop("receipt_sha256"); self.assertEqual(digest, runtime.backup.fingerprint(result))
    def test_changed_cluster_sealed_member_leader_and_voter_fail_closed(self):
        for bad in ("cluster", "version", "sealed", "leader-change", "voter", "address"):
            plan = fixture(); factory = Factory(plan); factory.bad = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError): runtime.operate(plan, live=True, factory=factory)
    def test_root_or_writable_probe_and_wrong_canary_denial_refuse(self):
        for bad in ("root", "probe-write", "canary", "deny-read"):
            plan = fixture(); factory = Factory(plan); factory.bad = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError): runtime.operate(plan, live=True, factory=factory)
    def test_unreviewed_raw_or_public_audit_device_refuse(self):
        for bad in ("raw-audit", "public-audit", "extra-audit"):
            plan = fixture(); factory = Factory(plan); factory.bad = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError): runtime.operate(plan, live=True, factory=factory)
    def test_plan_rejects_shared_endpoints_secret_literals_and_mutating_paths(self):
        mutations = [lambda p: p["cluster"]["members"][1].update(api_url=p["cluster"]["members"][0]["api_url"]),
            lambda p: p["cluster"]["members"][1].update(name="bao-a"), lambda p: p["cluster"].update(version="latest"),
            lambda p: p["probe"].update(path="sys/init"), lambda p: p["probe"].update(denied_paths=[p["probe"]["path"]]),
            lambda p: p["observer"].update(token="<fixture-only>"), lambda p: p.update(request_seconds=True),
            lambda p: p["probe"].update(token_env=p["observer"]["token_env"]), lambda p: p["probe"].update(path="kv/data/*")]
        for i, change in enumerate(mutations):
            plan = fixture(); change(plan)
            with self.subTest(case=i), self.assertRaises(ValueError): runtime.validate(plan)
    def test_transport_refuses_redirects_and_does_not_send_writes(self):
        plan = fixture(); source = runtime.client_plan(plan, plan["cluster"]["members"][0], plan["observer"]["token_env"])
        source["source"]["ca_sha256"] = hashlib.sha256(b"fixture-ca").hexdigest()
        with mock.patch.object(runtime.backup, "read_bootstrap_bytes", return_value=b"fixture-ca"), mock.patch.object(runtime.backup.ssl, "create_default_context"), mock.patch.dict(os.environ, {plan["observer"]["token_env"]: "fixture-bearer-only"}):
            client = runtime.Client(source)
        connection = mock.Mock(); response = mock.Mock(status=307); connection.getresponse.return_value = response
        with mock.patch.object(client, "connect", return_value=connection):
            with self.assertRaises(ValueError): client.read("sys/audit")
            self.assertEqual(connection.request.call_count, 1); response.read.assert_not_called()
            response.status = 403; response.read.side_effect = [b'{"errors":["permission denied"]}', b""]
            self.assertIsNone(client.read("kv/data/denied", denied=True))
            response.status = 200; response.read.side_effect = [b"x" * (256 * 1024 + 1)]
            with self.assertRaises(ValueError): client.read("sys/audit")
        for call in connection.request.call_args_list: self.assertEqual(call.args[0], "GET")
    def test_cli_failure_does_not_emit_private_error_or_replace_previous_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); plan = root / "plan.json"; plan.write_text(json.dumps(fixture())); proof = root / "proof.json"; proof.write_text("retain")
            output = io.StringIO(); error = io.StringIO()
            with mock.patch.object(runtime, "operate", side_effect=ValueError("private-value")), contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                self.assertEqual(runtime.main(["--plan", str(plan), "--verify", "--proof", str(proof)]), 1)
            self.assertEqual(proof.read_text(), "retain"); self.assertNotIn("private-value", output.getvalue() + error.getvalue())


if __name__ == "__main__": unittest.main()
