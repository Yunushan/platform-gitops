#!/usr/bin/env python3
"""Exercise host activation ownership, policy and uncertain-write recovery."""

import copy
import hashlib
import itertools
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import activate_keycloak_host as activate
import render_keycloak_ha as renderer
from test_keycloak_ha import fixture
from test_keycloak_realm import uid


def host_plan(digest, runtime):
    return {"schema_version": 1, "contract_sha256": digest, "member": "keycloak-a", "machine_id_sha256": "c" * 64, "boot_id": uid(10), "service_uid": 1000, "service_gid": 1000,
        "bind_addresses": {"https": "192.0.2.31", "management": "127.0.0.1"},
        "files_sha256": {k: "d" * 64 for k in activate.FILES},
        "docker": {"binary_sha256": "e" * 64, "daemon_id": "fixture-engine", "server_version": "28.5.1", "image_id": "sha256:" + "f" * 64} if runtime == "docker" else None,
        "native": {"user": "keycloak", "group": "keycloak", "java_home": "/opt/keycloak-java", "java_manifest_sha256": "e" * 64, "systemctl_sha256": "f" * 64, "max_runtime_seconds": None} if runtime == "raw" else None}


class Host:
    def __init__(self):
        self.calls = []; self.original = None; self.lost = None; self.unknown = None; self.ready = True; self.facts = {"host": "fixture"}; self.changed = False

    def prerequisites(self, contract, plan, *, starting=False):
        self.calls.append("prerequisites"); return copy.deepcopy(self.facts)

    def existing(self, contract, plan): return self.original and self.original["handle"]

    def acknowledge(self, kind):
        if self.lost == kind: self.lost = None; raise TimeoutError("Lost acknowledgement")

    def create(self, contract, plan, state):
        self.calls.append("create")
        if self.unknown == "create": raise TimeoutError("Unknown create")
        self.original = {"handle": "a" * 64, "policy_sha256": "b" * 64, "active": False, "invocation": None, "loaded": contract["runtime"] == "docker"}
        self.acknowledge("create")

    def inspect(self, contract, plan, state, facts):
        self.calls.append("inspect")
        if self.original is None: raise ValueError("Original absent")
        return copy.deepcopy(self.original)

    def reload(self):
        self.calls.append("reload")
        if self.unknown == "reload": raise TimeoutError("Unknown reload")
        self.original["loaded"] = True; self.acknowledge("reload")

    def start(self, contract, plan, state):
        self.calls.append("start")
        if self.unknown == "start": raise TimeoutError("Unknown start")
        self.original.update(active=True, invocation="1" * 32 if contract["runtime"] == "raw" else "2026-11-07T01:02:03Z"); self.acknowledge("start")

    def healthy(self, contract, plan, original): return self.ready


def container(contract, plan, state, image, credential):
    member = contract["plan"]["members"][0]; private = contract["plan"]["private_dir"]
    return {"Id": "1" * 64, "Name": "/" + activate.name(plan), "Image": plan["docker"]["image_id"],
        "Config": {"Image": contract["plan"]["image"], "User": "1000:1000", "Entrypoint": ["/opt/keycloak/bin/kc.sh"], "Cmd": renderer.arguments(contract["plan"], member=member),
            "Env": [*(image.get("Config", {}).get("Env") or []), "KC_DB_PASSWORD=" + credential, *(key + "=" + value for key, value in activate.bind_environment(plan).items())], "Labels": {**image["Config"].get("Labels", {}), **activate.labels(state)}, "WorkingDir": "/opt/keycloak",
            "Hostname": plan["member"], "StopTimeout": 120, "Tty": False, "OpenStdin": False, "Healthcheck": {"Test": ["NONE"]}},
        "HostConfig": {"ReadonlyRootfs": True, "Privileged": False, "NetworkMode": "host", "IpcMode": "none", "CgroupnsMode": "private", "UsernsMode": "host", "Runtime": "runc", "PidsLimit": 512,
            "Memory": 2 * 1024 ** 3, "MemorySwap": 2 * 1024 ** 3, "NanoCpus": 2000000000, "AutoRemove": False, "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "LogConfig": {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}}, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"],
            "Tmpfs": {"/opt/keycloak/data": "rw,nosuid,nodev,noexec,uid=1000,gid=1000,mode=0700,size=1g", "/tmp": "rw,nosuid,nodev,noexec,mode=1777,size=256m"},
            "Mounts": [{"Type": "bind", "Source": private + "/" + p, "Target": "/run/platform-keycloak/" + p, "ReadOnly": True, "BindOptions": {"Propagation": "rprivate"}} for p in ("tls", "db-ca")]},
        "Mounts": [{"Type": "bind", "Source": private + "/" + p, "Destination": "/run/platform-keycloak/" + p, "RW": False, "Propagation": "rprivate"} for p in ("tls", "db-ca")]}


class ActivationTests(unittest.TestCase):
    def test_explicit_local_cli_and_socket_ignore_inherited_docker_transport(self):
        spec = {"binary": "/opt/reviewed/docker", "socket": "/run/reviewed-engine/docker.sock"}
        for settings, expected in ((None, ["/usr/bin/docker", "--host=unix:///var/run/docker.sock", "info"]),
                                   (spec, ["/opt/reviewed/docker", "--host=unix:///run/reviewed-engine/docker.sock", "info"])):
            backend = activate.Host(settings)
            with mock.patch.object(backend, "run", return_value="selected-engine") as run, mock.patch.dict(os.environ, {"DOCKER_HOST": "tcp://foreign.example.invalid:2375", "DOCKER_CONTEXT": "foreign"}):
                self.assertEqual(backend.docker("info"), "selected-engine")
                run.assert_called_once_with(expected)

    def test_explicit_transport_paths_are_validated_without_host_access(self):
        self.setup("docker")
        original = copy.deepcopy(self.plan["docker"])
        for changes in ({"binary": "/opt/reviewed/docker"}, {"socket": "/run/reviewed/docker.sock"},
                        {"binary": "docker", "socket": "/run/docker.sock"}, {"binary": "/usr/bin/docker", "socket": "tcp://engine:2375"},
                        {"binary": "/usr/bin/docker", "socket": "/run/../tmp/docker.sock"}, {"binary": "/usr/bin/docker", "socket": "/run//docker.sock"},
                        {"binary": "/usr/bin/docker", "socket": "/usr/bin/docker"}, {"binary": "/usr/bin/docker\nflag", "socket": "/run/docker.sock"}):
            self.plan["docker"] = {**original, **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.action("local")
        self.plan["docker"] = {**original, "binary": "/opt/reviewed/docker", "socket": "/run/reviewed-engine/docker.sock"}
        self.assertTrue(self.action("local")["local_inputs_verified"]); self.assertFalse(self.host.calls)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup); self.root = Path(temporary.name)
        patch = mock.patch.object(activate, "ROOT", self.root); patch.start(); self.addCleanup(patch.stop)
        self.counter = 0

    def setup(self, runtime):
        self.counter += 1; self.bundle = self.root / ("bundle-" + str(self.counter)); self.bundle.mkdir()
        files = renderer.render(fixture(), runtime)
        for name, value in files.items(): (self.bundle / name).write_text(value, encoding="utf-8", newline="\n")
        self.contract = json.loads(files["keycloak-contract.json"])
        self.plan = host_plan(hashlib.sha256(files["keycloak-contract.json"].encode()).hexdigest(), runtime); self.host = Host()
        self.operation = self.root / "private" / ("operation-" + str(self.counter))

    def action(self, mode, output=None): return activate.operate(self.bundle, self.plan, mode=mode, output=output or self.operation, host=self.host, wait_seconds=5)

    def test_local_validation_never_constructs_a_host_or_reads_inputs(self):
        for runtime in ("raw", "docker"):
            self.setup(runtime)
            with mock.patch.object(activate, "Host", side_effect=AssertionError("Host access")):
                self.assertTrue(activate.operate(self.bundle, self.plan)["local_inputs_verified"])
            self.assertFalse(self.host.calls)

    def test_check_is_read_only_and_all_broader_flags_remain_false(self):
        for runtime in ("raw", "docker"):
            self.setup(runtime); result = self.action("check-host")
            self.assertTrue(result["host_checked"]); self.assertTrue(result["read_only"])
            self.assertNotIn("create", self.host.calls); self.assertFalse(self.operation.exists())
            for key in activate.UNVERIFIED: self.assertIs(result[key], False)

    def test_activation_and_resume_preserve_original_handles_without_new_writes(self):
        for runtime in ("raw", "docker"):
            self.setup(runtime); result = self.action("activate")
            self.assertTrue(result["original_member_active"]); self.assertTrue(result["tls_readiness_observed"]); self.assertFalse(result["read_only"])
            before = [x for x in self.host.calls if x in {"create", "reload", "start"}]
            self.assertEqual(before, ["create", "reload", "start"] if runtime == "raw" else ["create", "start"])
            self.assertTrue(self.action("resume")["read_only"])
            self.assertEqual([x for x in self.host.calls if x in {"create", "reload", "start"}], before)
            for key in activate.UNVERIFIED: self.assertIs(result[key], False)

    def test_lost_acknowledgements_are_observed_not_reissued(self):
        for runtime in ("raw", "docker"):
            for stage in (("create", "reload", "start") if runtime == "raw" else ("create", "start")):
                self.setup(runtime); self.host.lost = stage
                with self.assertRaises(TimeoutError): self.action("activate")
                self.assertTrue(self.action("resume")["original_member_active"])
                self.assertEqual(self.host.calls.count(stage), 1)

    def test_unknown_create_reload_and_start_never_repeat(self):
        for runtime in ("raw", "docker"):
            for stage in (("create", "reload", "start") if runtime == "raw" else ("create", "start")):
                self.setup(runtime); self.host.unknown = stage
                with self.assertRaises(TimeoutError): self.action("activate")
                before = self.host.calls.count(stage)
                with mock.patch.object(activate.time, "monotonic", side_effect=itertools.count(0, 10)), mock.patch.object(activate.time, "sleep"):
                    with self.assertRaises((ValueError, TimeoutError)): self.action("resume")
                self.assertEqual(self.host.calls.count(stage), before)

    def test_preexisting_and_replaced_members_are_refused(self):
        self.setup("docker"); self.host.original = {"handle": "9" * 64}
        with self.assertRaises(ValueError): self.action("activate")
        self.assertNotIn("create", self.host.calls)
        self.setup("raw"); self.action("activate"); self.host.original["handle"] = "8" * 64
        with self.assertRaises(ValueError): self.action("resume")
        self.assertEqual(self.host.calls.count("create"), 1)

    def test_deleted_changed_or_restarted_original_is_never_recreated(self):
        for mutation in (lambda h: setattr(h, "original", None), lambda h: h.original.update(policy_sha256="7" * 64), lambda h: h.original.update(invocation="8" * 32)):
            self.setup("raw"); self.action("activate"); mutation(self.host)
            with self.assertRaises(ValueError): self.action("resume")
            self.assertEqual(self.host.calls.count("create"), 1); self.assertEqual(self.host.calls.count("start"), 1)

    def test_readiness_can_complete_later_without_repeating_start(self):
        self.setup("docker"); self.host.ready = False
        with mock.patch.object(activate.time, "monotonic", side_effect=itertools.count(0, 10)), mock.patch.object(activate.time, "sleep"):
            with self.assertRaises(TimeoutError): self.action("activate")
        self.host.ready = True; self.assertTrue(self.action("resume")["tls_readiness_observed"])
        self.assertEqual(self.host.calls.count("start"), 1)

    def test_changed_sources_or_host_refuse_before_creating_again(self):
        self.setup("raw"); self.action("activate")
        with mock.patch.object(activate, "tools", return_value="0" * 64):
            with self.assertRaises(ValueError): self.action("resume")
        self.host.facts["boot"] = "different"
        with self.assertRaises(ValueError): self.action("resume")
        self.assertEqual(self.host.calls.count("start"), 1)

    def test_rendered_package_tampering_and_runtime_mismatch_refuse_locally(self):
        self.setup("raw"); self.plan["native"]["java_home"] = fixture()["native_home"]
        with self.assertRaises(ValueError): self.action("local")
        self.setup("docker"); (self.bundle / "keycloak-a-compose.yaml").write_text("changed")
        with self.assertRaises(ValueError): self.action("local")
        self.assertFalse(self.host.calls)

    def test_private_credential_file_has_one_assignment_and_no_interpolation(self):
        for runtime, raw, value in (("docker", b'KC_DB_PASSWORD=a$"b\\c\n', 'a$"b\\c'), ("raw", b"KC_DB_PASSWORD='a$\"b\\c'\n", 'a$"b\\c'),
                                    ("raw", b'KC_DB_PASSWORD="a\\$b\\\\c\\\"d"\n', 'a$b\\c"d')):
            self.assertEqual(activate.password(raw, runtime), value)
        for raw in (b"KC_DB_PASSWORD=${other}", b"KC_DB_PASSWORD=x\nKC_HTTP_ENABLED=true", b"OTHER=x", b"KC_DB_PASSWORD=", b"KC_DB_PASSWORD=x\x00y"):
            with self.assertRaises(ValueError): activate.password(raw, "docker")

    def test_readability_checks_only_the_declared_mount_but_native_checks_host_ancestors(self):
        private = self.root / "root-private"
        mount = private / "tls"; key = mount / "server.key"
        identity = {"service_uid": 1000, "service_gid": 1000}
        modes = {private: stat.S_IFDIR | 0o700, mount: stat.S_IFDIR | 0o755,
                 key: stat.S_IFREG | 0o640}

        def metadata(path, **kwargs):
            return SimpleNamespace(st_mode=modes.get(path, stat.S_IFDIR | 0o755),
                                   st_uid=0, st_gid=1000 if path == key else 0)

        with mock.patch.object(Path, "stat", autospec=True, side_effect=metadata):
            # Docker's root daemon traverses host ancestors when binding the mount;
            # the workload identity must still traverse and read everything inside it.
            activate.readable(key, identity, boundary=mount)
            with self.assertRaises(ValueError): activate.readable(key, identity)
            for path, mode in ((mount, stat.S_IFDIR | 0o700), (key, stat.S_IFREG | 0o600)):
                original = modes[path]; modes[path] = mode
                with self.subTest(path=path.name), self.assertRaises(ValueError):
                    activate.readable(key, identity, boundary=mount)
                modes[path] = original
            with self.assertRaises(ValueError):
                activate.readable(private / "database.env", identity, boundary=mount)

    def test_native_unit_preserves_hardening_and_binds_java_and_operation(self):
        self.setup("raw"); state = {"operation_id": uid(11)}; body = activate.unit_text(self.contract, self.plan, state)
        for value in ("NoNewPrivileges=yes", "ProtectSystem=strict", "User=keycloak", "--optimized", "migration-strategy=validate", "initialize-empty=false", "Environment=JAVA_HOME=/opt/keycloak-java", uid(11), activate.fingerprint(self.plan)):
            self.assertIn(value, body)
        self.assertNotIn("bootstrap-admin", body); self.assertNotIn("--import-realm", body)
        self.assertNotIn("RuntimeMaxSec=", body)
        self.plan["native"]["max_runtime_seconds"] = 180
        body = activate.unit_text(self.contract, self.plan, state)
        self.assertIn("RuntimeMaxSec=180", body); self.assertTrue(body.endswith("Restart=no\nRuntimeMaxSec=180\n"))

    def test_listener_scope_and_named_account_are_explicit_and_pinned(self):
        for runtime in ("raw", "docker"):
            self.setup(runtime)
            for value in ("0.0.0.0", "192.0.2.99", "hostname.invalid"):
                self.plan["bind_addresses"]["management"] = value
                with self.assertRaises(ValueError): self.action("local")
            self.plan["bind_addresses"]["management"] = "127.0.0.1"
            self.assertTrue(self.action("local")["local_inputs_verified"])
        self.setup("raw"); self.plan["native"]["user"] = "root"
        with self.assertRaises(ValueError): self.action("local")

    def test_kernel_listener_requires_declared_address_and_original_socket(self):
        original = {"pid": 123, "cgroup": "/fixture.scope"}
        header = b"sl local_address rem_address st tx tr retr uid timeout inode\n"
        def row(address, inode=456): return ("0: " + address + ":2328 00000000:0000 0A 0 0 0 0 0 " + str(inode) + "\n").encode()
        for ipv4, ipv6, expected in ((row("0100007F"), b"", True), (b"", row("0000000000000000FFFF00000100007F"), True),
                                     (row("00000000"), b"", False), (b"", row("0" * 32), False), (row("0100007F", inode=457), b"", False)):
            def read(path, **kwargs):
                if path == Path("/sys/fs/cgroup/fixture.scope/cgroup.procs"): return b"123\n"
                if path == Path("/proc/net/tcp"): return header + ipv4
                if path == Path("/proc/net/tcp6"): return header + ipv6
                raise AssertionError("Unexpected kernel read")
            with mock.patch.object(activate, "process_cgroup", return_value="/fixture.scope"), mock.patch.object(activate, "read_bounded_bytes", side_effect=read), \
                    mock.patch.object(Path, "iterdir", return_value=iter([Path("/proc/123/fd/4")])), mock.patch.object(activate.os, "readlink", return_value="socket:[456]"):
                self.assertIs(activate.owns_listener(original, "127.0.0.1", 9000), expected)

    def test_owned_management_endpoint_cannot_mask_an_unowned_https_listener(self):
        self.setup("raw")
        with mock.patch.object(activate, "owns_listener", side_effect=lambda original, address, port: port == 9000) as observed, \
                mock.patch.object(activate, "read_bounded_bytes", side_effect=AssertionError("No TLS request before both listeners are owned")):
            self.assertFalse(activate.Host().healthy(self.contract, self.plan, {}))
        self.assertEqual([call.args[1:] for call in observed.call_args_list], [("127.0.0.1", 9000), ("192.0.2.31", 8443)])

    def test_docker_create_and_inspect_reject_privilege_or_identity_drift(self):
        self.setup("docker"); state = {"operation_id": uid(12), "plan_sha256": activate.fingerprint(self.plan), "toolset_sha256": "c" * 64, "handle": None}
        image = {"Config": {"Env": ["LANG=C"], "Labels": {"reviewed": "fixture"}}}; credential = "synthetic-private-credential"
        row = container(self.contract, self.plan, state, image, credential)
        digest = activate.container_policy(row, self.contract, self.plan, state, image, credential); self.assertTrue(activate.sha(digest))
        row["Config"]["Env"].reverse()
        self.assertEqual(activate.container_policy(row, self.contract, self.plan, state, image, credential), digest)
        row["HostConfig"]["OomKillDisable"] = False; row["Mounts"].reverse()
        self.assertEqual(activate.container_policy(row, self.contract, self.plan, state, image, credential), digest)
        row["HostConfig"]["OomKillDisable"] = None
        self.assertEqual(activate.container_policy(row, self.contract, self.plan, state, image, credential), digest)
        for value in (True, 0, "false"):
            changed = copy.deepcopy(row); changed["HostConfig"]["OomKillDisable"] = value
            with self.subTest(oom_policy=value), self.assertRaises(ValueError): activate.container_policy(changed, self.contract, self.plan, state, image, credential)
        arguments = activate.create_arguments(self.contract, self.plan, state)
        self.assertNotIn(credential, " ".join(arguments)); self.assertIn("--network=host", arguments)
        self.assertIn("--pull=never", arguments)
        for mutate in (lambda o: o["HostConfig"].update(Privileged=True), lambda o: o["Config"]["Env"].append("KC_HTTP_ENABLED=true"),
                       lambda o: o["Mounts"][0].update(RW=True), lambda o: o["Config"].update(StopTimeout=1), lambda o: o.update(Id="bad"), lambda o: o["Config"]["Env"].append("KC_DB_PASSWORD=" + credential)):
            changed = copy.deepcopy(row); mutate(changed)
            with self.assertRaises(ValueError): activate.container_policy(changed, self.contract, self.plan, state, image, credential)

    @unittest.skipUnless(os.environ.get("KEYCLOAK_HOST_TEST_SYSTEMD") == "1", "Explicit disposable Linux systemd/TLS mechanics fixture")
    def test_real_systemd_original_invocation_and_strict_tls(self):
        from test_keycloak_host_fixture import exercise
        result = exercise(self)
        self.assertTrue(result["systemd_create_start_resume_verified"]); self.assertTrue(result["kernel_listener_ownership_verified"])
        self.assertFalse(result["keycloak_runtime_verified"]); self.assertFalse(result["docker_runtime_verified"]); self.assertFalse(result["ha_verified"])


if __name__ == "__main__": unittest.main()
