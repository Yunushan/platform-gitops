#!/usr/bin/env python3
"""Exercise Gerrit host activation policy and recovery; runtime daemons are modeled."""

import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import activate_gerrit_host as activate
from test_gerrit_activation import fixture, uid
from test_keycloak_host import Host as ModeledHost


def plan(runtime):
    site_plan = fixture()["site_plan"]
    return {"schema_version": 1, "runtime": runtime, "site_plan": site_plan, "member": "gerrit-a",
        "machine_id_sha256": "c" * 64, "boot_id": uid(20), "service_uid": 1000, "service_gid": 1000,
        "private_files_sha256": {k: "e" * 64 for k in activate.PRIVATE_FILES},
        "health": {"address": "192.0.2.31", "hostname": "review.example.com", "ca_file": "/etc/gerrit-health-ca.crt", "ca_sha256": "d" * 64},
        "docker": {"binary": "/usr/bin/docker", "socket": "/var/run/docker.sock", "binary_sha256": "e" * 64, "daemon_id": "fixture-engine",
            "server_version": "28.5.1", "image_id": "sha256:" + "f" * 64} if runtime == "docker" else None,
        "native": {"user": "gerrit", "group": "gerrit", "java_home": "/opt/gerrit-java", "java_manifest": "/etc/gerrit-java.sha256",
            "java_manifest_sha256": "e" * 64, "systemctl_sha256": "f" * 64} if runtime == "raw" else None}


class Host(ModeledHost):
    def __init__(self, runtime): super().__init__(); self.runtime = runtime

    def prerequisites(self, bundle, plan): return super().prerequisites(bundle, plan)

    def create(self, bundle, plan, state): return super().create({"runtime": self.runtime}, plan, state)

    def start(self, bundle, plan, state): return super().start({"runtime": self.runtime}, plan, state)


def container(bundle, selected, state, image):
    mounts = [{"Type": "bind", "Source": source, "Destination": target, "RW": not readonly, "Propagation": "rprivate"}
        for source, target, readonly in activate.mounts(bundle, selected)]
    return {"Id": "1" * 64, "Name": "/" + activate.name(selected), "Image": selected["docker"]["image_id"],
        "Config": {"Image": selected["site_plan"]["java_image"], "User": "1000:1000", "Entrypoint": ["/bin/sh"],
            "Cmd": ["/opt/gerrit-startup/start.sh", *activate.command(selected)], "Env": image["Config"]["Env"],
            "Labels": {**image["Config"]["Labels"], **activate.labels(state)}, "WorkingDir": "/var/gerrit", "Hostname": selected["member"],
            "StopTimeout": 120, "Tty": False, "OpenStdin": False, "Healthcheck": {"Test": ["NONE"]}},
        "HostConfig": {"ReadonlyRootfs": True, "Privileged": False, "NetworkMode": "host", "IpcMode": "none", "CgroupnsMode": "private", "UsernsMode": "host", "Runtime": "runc",
            "PidsLimit": 512, "Memory": 3 * 1024 ** 3, "MemorySwap": 3 * 1024 ** 3, "NanoCpus": 4000000000, "AutoRemove": False,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}, "LogConfig": {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}},
            "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"], "Tmpfs": {"/tmp": "rw,nosuid,nodev,noexec,mode=1777,size=1g"},
            "Mounts": [{"Type": "bind"} for _ in mounts]}, "Mounts": mounts}


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.private = self.root / "private"; self.private.mkdir()
        self.patch = mock.patch.object(activate, "ROOT", self.root); self.patch.start(); self.addCleanup(self.patch.stop)
        self.source = mock.patch.object(activate, "tools", return_value="f" * 64); self.source.start(); self.addCleanup(self.source.stop)

    def bundle(self, selected, suffix=""):
        directory = self.private / ("bundle-" + selected["runtime"] + suffix); directory.mkdir()
        for name, content in activate.sites.render(selected["site_plan"], selected["runtime"]).items(): (directory / name).write_bytes(content.encode())
        return directory

    def test_local_and_host_checks_do_not_create_members_or_journals(self):
        for runtime in ("raw", "docker"):
            selected = plan(runtime); bundle = self.bundle(selected); host = Host(runtime)
            with mock.patch.object(activate, "Host", side_effect=AssertionError("host access")):
                result = activate.operate(bundle, selected)
            self.assertTrue(result["read_only"]); self.assertEqual(host.calls, [])
            result = activate.operate(bundle, selected, mode="check-host", host=host)
            self.assertTrue(result["host_checked"]); self.assertNotIn("create", host.calls)
            self.assertFalse(any(p.name.startswith("operation") for p in self.private.iterdir()))

    def test_both_host_lifecycles_activate_and_resume_original_without_restart(self):
        for runtime in ("raw", "docker"):
            selected = plan(runtime); bundle = self.bundle(selected); host = Host(runtime); output = self.private / ("operation-" + runtime)
            result = activate.operate(bundle, selected, mode="activate", host=host, output=output)
            self.assertTrue(result["original_member_active"]); self.assertTrue(result["tls_health_observed"]); self.assertTrue(result["ssh_host_key_observed"])
            self.assertFalse(result["read_only"])
            for key in activate.UNVERIFIED: self.assertFalse(result[key])
            result = activate.operate(bundle, selected, mode="resume", host=host, output=output)
            self.assertTrue(result["read_only"]); self.assertEqual(host.calls.count("create"), 1); self.assertEqual(host.calls.count("start"), 1)
            self.assertEqual(host.calls.count("reload"), int(runtime == "raw"))

    def test_lost_create_reload_and_start_ack_are_observed_without_repetition(self):
        for runtime, action in (("raw", "create"), ("raw", "reload"), ("raw", "start"), ("docker", "create"), ("docker", "start")):
            with self.subTest(runtime=runtime, action=action):
                selected = plan(runtime); bundle = self.bundle(selected, action); output = self.private / ("lost-" + runtime + action); host = Host(runtime); host.lost = action
                with self.assertRaises(TimeoutError): activate.operate(bundle, selected, mode="activate", host=host, output=output)
                result = activate.operate(bundle, selected, mode="resume", host=host, output=output)
                self.assertTrue(result["original_member_active"]); self.assertEqual(host.calls.count(action), 1)

    def test_missing_uncertain_create_and_source_or_host_drift_do_not_authorize_a_new_member(self):
        for runtime in ("raw", "docker"):
            selected = plan(runtime); bundle = self.bundle(selected); output = self.private / ("missing-" + runtime); host = Host(runtime); host.unknown = "create"
            with self.assertRaises(TimeoutError): activate.operate(bundle, selected, mode="activate", host=host, output=output)
            with self.assertRaises(ValueError): activate.operate(bundle, selected, mode="resume", host=host, output=output)
            self.assertEqual(host.calls.count("create"), 1)
            host.facts["host"] = "another-host"
            with self.assertRaises(ValueError): activate.operate(bundle, selected, mode="resume", host=host, output=output)
            host.facts["host"] = "fixture"
            with mock.patch.object(activate, "tools", return_value="a" * 64), self.assertRaises(ValueError):
                activate.operate(bundle, selected, mode="resume", host=host, output=output)

    def test_timeout_retains_original_and_restarted_member_refuses_resume(self):
        selected = plan("raw"); bundle = self.bundle(selected); output = self.private / "timeout"; host = Host("raw"); host.ready = False
        with mock.patch.object(activate.time, "monotonic", side_effect=[0, 10]), self.assertRaises(TimeoutError):
            activate.operate(bundle, selected, mode="activate", host=host, output=output, wait_seconds=5)
        host.ready = True
        activate.operate(bundle, selected, mode="resume", host=host, output=output)
        host.original["invocation"] = "2" * 32
        with self.assertRaises(ValueError): activate.operate(bundle, selected, mode="resume", host=host, output=output)
        self.assertEqual(host.calls.count("start"), 1)
        receipt = json.loads((output / "gerrit-host-proof.json").read_text())
        self.assertFalse(receipt["original_member_active"])

    def test_existing_member_and_modified_package_refuse_before_create(self):
        selected = plan("docker"); bundle = self.bundle(selected); host = Host("docker")
        host.original = {"handle": "1" * 64}
        with self.assertRaises(ValueError): activate.operate(bundle, selected, mode="activate", host=host, output=self.private / "existing")
        self.assertNotIn("create", host.calls)
        (bundle / "start.sh").write_text("exec java --init\n")
        with self.assertRaises(ValueError): activate.operate(bundle, selected)

    def test_package_and_plan_cannot_hide_legacy_storage_or_runtime_substitution(self):
        selected = plan("raw"); bundle = self.bundle(selected)
        invalid = [lambda p: p.update(runtime="k3s"), lambda p: p["site_plan"].update(schema_version=1),
            lambda p: p["native"].update(user="root"), lambda p: p["native"].update(java_home=p["site_plan"]["shared_host_path"]),
            lambda p: p["health"].update(address="0.0.0.0"), lambda p: p["health"].update(address="::1")]
        for mutate in invalid:
            bad = copy.deepcopy(selected); mutate(bad)
            with self.assertRaises(ValueError): activate.operate(bundle, bad)
        (bundle / "extra.py").write_text("print('unreviewed')")
        with self.assertRaises(ValueError): activate.operate(bundle, selected)

    def test_native_unit_retains_direct_java_and_guarded_mount_with_no_auto_restart(self):
        selected = plan("raw"); state = {"operation_id": uid(50), "plan_sha256": "c" * 64}
        body = activate.unit_text(selected, state)
        self.assertIn("Restart=no\n", body); self.assertNotIn("Restart=on-failure", body)
        self.assertIn("/etc/gerrit/start.sh", body); self.assertIn("ConditionPathIsMountPoint=/srv/gerrit-shared", body)
        self.assertIn("Environment=JAVA_HOME=/opt/gerrit-java", body); self.assertNotIn("docker", body)

    @unittest.skipUnless(os.environ.get("GERRIT_HOST_TEST_SYSTEMD_SYNTAX") == "1", "Explicit Linux systemd unit syntax check")
    def test_native_unit_passes_real_systemd_parser_without_starting_it(self):
        from bounded_subprocess import run_bounded
        binary = shutil.which("systemd-analyze")
        self.assertIsNotNone(binary)
        selected = plan("raw"); state = {"operation_id": uid(50), "plan_sha256": "c" * 64}
        with tempfile.TemporaryDirectory(prefix="gerrit-unit-syntax-") as temporary:
            path = Path(temporary) / "platform-gerrit-a.service"; path.write_text(activate.unit_text(selected, state))
            result = run_bounded([binary, "verify", "--man=no", str(path)], text=True, timeout=30, output_max_bytes=65536, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_docker_command_and_inspection_reject_extra_privilege_or_redirected_mounts(self):
        selected = plan("docker"); bundle = self.bundle(selected); state = {"operation_id": uid(50), "plan_sha256": "c" * 64, "toolset_sha256": "d" * 64}
        image = {"Config": {"Env": ["PATH=/usr/bin:/bin"], "Labels": {"vendor": "fixture"}}}
        row = container(bundle, selected, state, image)
        self.assertRegex(activate.container_policy(row, bundle, selected, state, image), r"^[0-9a-f]{64}$")
        args = activate.create_arguments(bundle, selected, state)
        self.assertIn("--pull=never", args); self.assertIn("--restart=no", args); self.assertNotIn("--init", args)
        self.assertIn("--network=host", args)
        mutations = [lambda r: r["HostConfig"].update(Privileged=True), lambda r: r["Config"].update(Env=["JAVA_TOOL_OPTIONS=unreviewed"]),
            lambda r: r["Mounts"][0].update(Source="/another-site"), lambda r: r["HostConfig"].update(RestartPolicy={"Name": "always", "MaximumRetryCount": 0}),
            lambda r: r["HostConfig"].update(NetworkMode="bridge"), lambda r: r["HostConfig"].update(CapAdd=["SYS_ADMIN"]),
            lambda r: r["Config"].update(Cmd=["java", "--init"])]
        for mutate in mutations:
            bad = copy.deepcopy(row); mutate(bad)
            with self.assertRaises(ValueError): activate.container_policy(bad, bundle, selected, state, image)
        empty_image = {"Config": {"Env": None, "Labels": {}}}
        nullable = container(bundle, selected, state, empty_image)
        self.assertRegex(activate.container_policy(nullable, bundle, selected, state, empty_image), r"^[0-9a-f]{64}$")
        nullable["Config"]["Env"] = False
        with self.assertRaises(ValueError): activate.container_policy(nullable, bundle, selected, state, empty_image)

    def test_health_requires_every_configured_probe_passed_not_just_http200(self):
        checks = {key: {"result": "passed", "elapsed": 1, "ts": 100} for key in activate.storage.REQUIRED_HEALTHCHECKS}
        checks.update(ts=100, elapsed=5)
        self.assertTrue(activate.healthy_body(json.dumps(checks).encode()))
        self.assertTrue(activate.healthy_body(b")]}'\n" + json.dumps(checks).encode()))
        self.assertFalse(activate.healthy_body(b"{}"))
        for result in ("failed", "timeout", "disabled", "not_run", True):
            bad = copy.deepcopy(checks); bad["auth"]["result"] = result
            self.assertFalse(activate.healthy_body(json.dumps(bad).encode()))
        del checks["auth"]; self.assertFalse(activate.healthy_body(json.dumps(checks).encode()))

    @unittest.skipUnless(os.environ.get("GERRIT_HOST_TEST_DOCKER_POLICY") == "1", "Explicit isolated Docker CLI/inspection fixture")
    def test_actual_docker_cli_create_and_original_inspection_without_starting_gerrit(self):
        from test_gerrit_docker_policy_fixture import exercise
        result = exercise(self)
        self.assertTrue(result["docker_engine_cleanup_verified"])
        self.assertTrue(result["workload"]["docker_cli_create_inspect_verified"])
        self.assertFalse(result["workload"]["gerrit_runtime_verified"])


if __name__ == "__main__": unittest.main()
