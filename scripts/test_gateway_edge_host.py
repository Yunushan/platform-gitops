#!/usr/bin/env python3
"""Exercise host ownership, retained releases and interrupted activation without starting a VIP."""

from contextlib import nullcontext
import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import socket
import unittest
from unittest import mock

from atomic_file import atomic_write_text
import manage_gateway_edge_host as manager
import render_gateway_edge as renderer
from test_gateway_edge import fixture


def package(root, runtime="raw", suffix="initial", change=None):
    workspace = root / suffix; workspace.mkdir()
    plan, gateway, original = fixture(workspace, runtime)
    artifacts = workspace / "artifacts"; artifacts.mkdir()
    for role in manager.ROLES:
        value = (role + suffix).encode()
        (artifacts / role).write_bytes(value)
        plan["binaries"][role]["sha256"] = hashlib.sha256(value).hexdigest()
    if change: change(plan)
    files = renderer.render(plan, gateway, runtime)
    bundle = workspace / "bundle"; bundle.mkdir()
    for name, content in files.items(): (bundle / name).write_text(content, encoding="utf-8", newline="\n")
    ca = workspace / "ca.crt"; ca.write_bytes(b"fixture-ca\n")
    digest = json.loads(files["render-status.json"])["contract_sha256"]
    return bundle, digest, original, ca, artifacts


class FakeHost(manager.Host):
    def __init__(self, root):
        super().__init__(root / "state", root / "etc" / "gateway-edge", root / "units")
        self.calls, self.running, self.fail_role, self.containers, self.vip_assigned = [], {}, None, {}, False

    def prerequisites(self, contract, host): self.calls.append("prerequisites")
    def parse(self, directory, contract, host): self.calls.append("parse")
    def service_identity(self): self.calls.append("identity")
    def run(self, arguments, *, environment=None):
        self.calls.append(arguments)
        if arguments[:2] == ["/usr/bin/systemctl", "show"]:
            unit = arguments[2]
            return "FragmentPath=" + (str(self.units / unit) if (self.units / unit).exists() else "") + "\nDropInPaths=\n"
        if arguments[:2] == ["/usr/sbin/ip", "-j"]:
            return json.dumps([{"addr_info": [{"local": "192.0.2.50"}]}]) if self.vip_assigned else "[]"
        if arguments[:4] == ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", "ps"]: return "\n".join(self.containers)
        if arguments[:4] == ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", "inspect"]: return json.dumps([self.containers[arguments[4]]])
        raise AssertionError("Unexpected simulated host command")
    def stop_vip(self, directory, contract, host):
        self.calls.append("stop-vip"); self.running.pop("keepalived", None)
    def activate_role(self, directory, files, contract, host, role):
        self.calls.append("activate-" + role)
        if contract["runtime"] == "raw": atomic_write_text(self.units / self.unit(host, role), files[self.unit(host, role)].decode(), mode=0o644)
        self.running[role] = manager.sha(files["edge-contract.json"])
        if contract["runtime"] == "docker":
            self.containers[role] = {"Config": {"Labels": {renderer.OWNER: contract["plan"]["name"], "platform.gitops/edge-host": host,
                "platform.gitops/edge-contract": self.running[role], "com.docker.compose.service": role}}}
        if self.fail_role == role: raise TimeoutError("Lost acknowledgment after service restart")
    def verify_role(self, directory, files, contract, host, role):
        self.calls.append("verify-" + role)
        if self.running.get(role) != manager.sha(files["edge-contract.json"]): raise ValueError("Service still uses a different release")
    def wait_role(self, directory, files, contract, host, role): self.verify_role(directory, files, contract, host, role)


class HostTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("GERRIT_ROUTER_VM_RUNTIME") == "1", "Explicit native Gerrit router VM runtime")
    def test_real_gerrit_router_systemd_lifecycle(self):
        from test_gateway_edge_host_vm_fixture import exercise

        result = exercise(self, fixture, gerrit_router=True)
        self.assertTrue(result["gerrit_router_systemd_verified"])
        self.assertTrue(result["gerrit_router_vip_withdrawal_verified"])

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0, "Root Linux managed-directory traversal check")
    def test_private_job_umask_keeps_new_public_release_paths_readable(self):
        with tempfile.TemporaryDirectory(prefix="platform-edge-permissions-", dir="/run") as temporary:
            root = Path(temporary); root.chmod(0o755)
            old = os.umask(0o077)
            try: manager.secure_directory(root / "releases/example")
            finally: os.umask(old)
            config = root / "releases/example/config"; config.write_text("reviewed-public-config"); config.chmod(0o644)
            result = manager.run_bounded(["/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "--no-new-privs",
                "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all", "/usr/bin/python3", "-I", "-B", "-c",
                "import pathlib,sys; assert pathlib.Path(sys.argv[1]).read_text()=='reviewed-public-config'", str(config)],
                text=True, timeout=10, output_max_bytes=4096, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.environ.get("GATEWAY_EDGE_HOST_VM_RUNTIME") == "1", "Explicit disposable native host VM runtime")
    def test_real_native_host_vm_lifecycle(self):
        from test_gateway_edge_host_vm_fixture import exercise
        result = exercise(self, fixture)
        self.assertTrue(result["native_host_retained_rollback_verified"])

    def test_optional_root_lock_excludes_concurrent_operations_and_symlinks(self):
        if os.name != "posix" or os.geteuid() != 0: self.skipTest("Root POSIX lock-ownership fixture")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with manager.host_lock(root / "operation.lock"):
                with self.assertRaises(BlockingIOError):
                    with manager.host_lock(root / "operation.lock"): self.fail("Concurrent operation acquired the lock")
            (root / "alias.lock").symlink_to(root / "operation.lock")
            with self.assertRaises(OSError):
                with manager.host_lock(root / "alias.lock"): self.fail("Symlink lock was accepted")

    def test_optional_real_native_artifact_parser_without_installation(self):
        binaries = {role: os.environ.get("GATEWAY_EDGE_TEST_" + role.upper()) for role in manager.ROLES}
        if os.name != "posix" or os.geteuid() != 0 or not all(binaries.values()):
            self.skipTest("Set both reviewed local daemon fixtures and use a root parser identity; no unit is installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, gateway, original = fixture(root, "raw")
            interface = next(name for _, name in socket.if_nameindex() if name != "lo")
            current = manager.starter.interface_address(interface)
            network = ipaddress.ip_network(current + "/24", strict=False)
            candidates = [str(p) for p in network.hosts() if str(p) != current][:2]
            plan.update(vip=candidates[0] + "/24")
            plan["entry_hosts"][0].update(interface=interface, address=current)
            plan["entry_hosts"][1].update(address=candidates[1])
            artifacts = root / "artifacts"; artifacts.mkdir()
            for role, source in binaries.items():
                value = Path(source).read_bytes(); (artifacts / role).write_bytes(value)
                plan["binaries"][role]["sha256"] = hashlib.sha256(value).hexdigest()
            metadata = Path(binaries["keepalived"]).with_name("keepalived.config-opts")
            if metadata.is_file():
                value = metadata.read_bytes(); (artifacts / metadata.name).write_bytes(value)
                plan["binaries"]["keepalived"]["config_opts_sha256"] = hashlib.sha256(value).hexdigest()
            manager.run_bounded(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=fixture-ca",
                "-keyout", str(root / "key"), "-out", str(root / "ca.crt")], timeout=20, output_max_bytes=256 * 1024, check=True)
            plan["ca_sha256"] = hashlib.sha256((root / "ca.crt").read_bytes()).hexdigest()
            files = renderer.render(plan, gateway, "raw")
            contract = json.loads(files["edge-contract.json"])
            values = manager.payloads(contract, {k: v.encode() for k, v in files.items()}, root / "ca.crt", artifacts)
            directory = root / "release"; directory.mkdir()
            for name, value in values.items():
                target = directory / name; target.parent.mkdir(exist_ok=True, parents=True)
                target.write_bytes(value); target.chmod(manager.file_mode(name))
            run = manager.run_bounded
            def with_fixture_libraries(arguments, **kwargs):
                kwargs["env"] = {**kwargs.get("env", {}), **({"LD_LIBRARY_PATH": os.environ["GATEWAY_EDGE_TEST_LIBS"]} if os.environ.get("GATEWAY_EDGE_TEST_LIBS") else {})}
                return run(arguments, **kwargs)
            machine = manager.Host()
            with mock.patch.object(manager, "run_bounded", side_effect=with_fixture_libraries), mock.patch.object(manager.starter, "run_bounded", side_effect=with_fixture_libraries):
                result = manager.operate(directory, manager.sha(values["edge-contract.json"]), original, "edge-a", mode="check",
                    ca_file=root / "ca.crt", artifacts=artifacts, machine=machine)
                self.assertTrue(result["host_checked"])
                self.assertFalse(result["activated"])

    def test_corrupt_host_receipt_cannot_supply_paths_or_unrecorded_releases(self):
        owner = {"name": "platform-entry", "runtime": "raw", "host": "edge-a"}
        state = {"schema_version": 1, "owner": owner, "stable": None, "previous": None, "pending": None, "releases": {}}
        manager.validate_state(state, owner)
        for value in ("../../outside", "a" * 64, False):
            bad = copy.deepcopy(state); bad["stable"] = value
            with self.assertRaises(ValueError): manager.validate_state(bad, owner)

    def test_local_validation_cannot_construct_a_host_or_read_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle, digest, gateway, _, _ = package(Path(temporary))
            with mock.patch.object(manager, "Host") as host, mock.patch.object(manager, "payloads") as inputs:
                self.assertFalse(manager.operate(bundle, digest, gateway, "edge-a")["activated"])
            host.assert_not_called(); inputs.assert_not_called()

    def test_modified_compose_or_unit_is_rejected_even_when_programs_match(self):
        for runtime, name in (("docker", "compose-edge-a.yaml"), ("raw", "edge-edge-a-haproxy.service")):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as temporary:
                bundle, digest, gateway, _, _ = package(Path(temporary), runtime)
                with (bundle / name).open("a") as stream: stream.write("\n# changed\n")
                with self.assertRaises(ValueError): manager.operate(bundle, digest, gateway, "edge-a")

    def test_transition_keeps_vip_ownership_and_requires_explicit_downgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle, digest, gateway, _, _ = package(root)
            old, _ = manager.capture(bundle, digest, gateway)
            for update in (lambda c: c["plan"].update(vip="192.0.2.51/24"), lambda c: c["plan"].update(name="different"),
                           lambda c: c["plan"]["binaries"]["haproxy"].update(version="3.2.25")):
                new = copy.deepcopy(old); update(new)
                with self.assertRaises(ValueError): manager.transition(old, new)
            manager.transition(old, new, rollback=True)

    @unittest.skipUnless(os.name == "posix", "POSIX installation state and atomic symlink fixture")
    def test_native_install_upgrade_verify_and_explicit_rollback(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(manager.starter, "trusted_paths"), \
             mock.patch.object(manager, "host_lock", side_effect=lambda _: nullcontext()), mock.patch.object(manager.health, "healthy", return_value=True):
            root = Path(temporary); machine = FakeHost(root)
            first = package(root)
            newer = package(root, suffix="updated", change=lambda p: p["binaries"]["haproxy"].update(version="3.2.27"))
            def operate(values, mode, expected="absent"):
                bundle, digest, gateway, ca, artifacts = values
                return manager.operate(bundle, digest, gateway, "edge-a", mode=mode, ca_file=ca, artifacts=artifacts, expected_current=expected, machine=machine)
            self.assertTrue(operate(first, "apply")["host_ready"])
            self.assertEqual(machine.active.readlink().name, first[1])
            with self.assertRaises(ValueError): operate(newer, "apply")
            self.assertTrue(operate(newer, "apply", first[1])["activated"])
            self.assertTrue(operate(newer, "verify", newer[1])["host_ready"])
            trust = next((machine.state / "trust").glob("*/ca.crt"))
            trust.write_bytes(b"changed CA")
            with self.assertRaises(ValueError): operate(newer, "verify", newer[1])
            trust.write_bytes(b"fixture-ca\n")
            self.assertTrue(operate(first, "rollback", newer[1])["activated"])
            self.assertEqual(machine.active.readlink().name, first[1])
            state = json.loads((machine.state / "state.json").read_text())
            self.assertIsNone(state["pending"])
            self.assertEqual(state["stable"], first[1]); self.assertEqual(state["previous"], newer[1])
            self.assertEqual(len(list((machine.state / "attempts").glob("*/proof.json"))), 3)

    @unittest.skipUnless(os.name == "posix", "POSIX Docker activation selector fixture; engine operations are simulated")
    def test_docker_configuration_upgrade_and_unowned_container_rejection(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(manager.starter, "trusted_paths"), \
             mock.patch.object(manager, "host_lock", side_effect=lambda _: nullcontext()), mock.patch.object(manager.health, "healthy", return_value=True):
            root = Path(temporary); machine = FakeHost(root)
            first = package(root, "docker")
            newer = package(root, "docker", "updated")
            def apply(values, expected="absent"):
                bundle, digest, gateway, ca, _ = values
                return manager.operate(bundle, digest, gateway, "edge-a", mode="apply", ca_file=ca, expected_current=expected, machine=machine)
            self.assertTrue(apply(first)["activated"])
            machine.containers["haproxy"]["Config"]["Labels"]["platform.gitops/edge-contract"] = "f" * 64
            with self.assertRaises(ValueError): apply(newer, first[1])
            machine.containers["haproxy"]["Config"]["Labels"]["platform.gitops/edge-contract"] = first[1]
            self.assertTrue(apply(newer, first[1])["activated"])

    def test_docker_verifier_rejects_privilege_artifact_mount_and_health_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle, digest, gateway, _, _ = package(root, "docker")
            contract, files = manager.capture(bundle, digest, gateway)
            plan = contract["plan"]; binary = b"running-native-artifact"
            plan["binaries"]["haproxy"]["sha256"] = hashlib.sha256(binary).hexdigest()
            row = {"Image": "sha256:fixture", "Config": {"Image": plan["images"]["haproxy"], "User": "1000:1000", "Entrypoint": [],
                "Cmd": renderer.command({"name": "edge-a"}, "haproxy", digest), "Labels": {"platform.gitops/edge-contract": digest,
                    "com.docker.compose.project": "platform-entry-edge-a", "com.docker.compose.service": "haproxy"}},
                "HostConfig": {"NetworkMode": "host", "Privileged": False, "ReadonlyRootfs": True, "CapAdd": ["NET_BIND_SERVICE"], "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"], "IpcMode": "private", "RestartPolicy": {"Name": "unless-stopped"}},
                "State": {"Running": True, "Health": {"Status": "healthy"}, "Pid": 123, "Paused": False, "Restarting": False, "Dead": False},
                "Mounts": [{"Type": "bind", "Destination": target, "Source": str(bundle), "RW": False} for target in (renderer.BASE, "/run/platform/gateway-edge")]}
            machine = manager.Host()
            with mock.patch.object(machine, "compose", return_value="container-id"), mock.patch.object(machine, "image_id", return_value="sha256:fixture"), \
                 mock.patch.object(manager, "read_bounded_bytes", return_value=binary), mock.patch.object(machine, "run", side_effect=lambda _: json.dumps([row])):
                machine.verify_role(bundle, files, contract, "edge-a", "haproxy")
                for mutate in (lambda r: r["HostConfig"].update(Privileged=True), lambda r: r["Mounts"][0].update(RW=True),
                               lambda r: r["Config"].update(Image="unexpected"), lambda r: r["State"]["Health"].update(Status="starting"),
                               lambda r: r["State"].update(Paused=True), lambda r: r["HostConfig"].update(PidMode="host"),
                               lambda r: r["Mounts"].append({"Destination": "/outside", "Type": "bind", "Source": "/", "RW": True})):
                    original = copy.deepcopy(row); mutate(row)
                    with self.assertRaises(ValueError): machine.verify_role(bundle, files, contract, "edge-a", "haproxy")
                    row.clear(); row.update(original)

    def test_native_verifier_rejects_old_process_arguments_after_unit_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle, digest, gateway, _, artifacts = package(root)
            contract, files = manager.capture(bundle, digest, gateway)
            machine = manager.Host(units=root / "units")
            binary = (artifacts / "haproxy").read_bytes()
            arguments = manager.starter.daemon_arguments(bundle / "bin" / "haproxy", bundle / "haproxy.cfg", "haproxy", contract["plan"])
            current = [p.encode() for p in arguments]
            def read(path, **kwargs):
                path = Path(path)
                if path.suffix == ".service": return files[path.name]
                if path.name == "exe": return binary
                if path.name == "cmdline": return b"\0".join(current) + b"\0"
                raise AssertionError("Unexpected process read")
            properties = "ActiveState=active\nSubState=running\nMainPID=123\nDropInPaths=\nNeedDaemonReload=no\nFragmentPath=" + str(machine.units / machine.unit("edge-a", "haproxy"))
            with mock.patch.object(manager, "read_bounded_bytes", side_effect=read), mock.patch.object(machine, "run", return_value=properties):
                machine.verify_role(bundle, files, contract, "edge-a", "haproxy")
                current[-1] = b"/retained/older-release/haproxy.cfg"
                with self.assertRaises(ValueError): machine.verify_role(bundle, files, contract, "edge-a", "haproxy")

    @unittest.skipUnless(os.name == "posix", "POSIX partial installation fixture")
    def test_lost_acknowledgment_resumes_without_restarting_ready_services(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(manager.starter, "trusted_paths"), \
             mock.patch.object(manager, "host_lock", side_effect=lambda _: nullcontext()), mock.patch.object(manager.health, "healthy", return_value=True):
            root = Path(temporary); machine = FakeHost(root); machine.fail_role = "keepalived"
            bundle, digest, gateway, ca, artifacts = package(root)
            kwargs = dict(ca_file=ca, artifacts=artifacts, machine=machine)
            with self.assertRaises(TimeoutError): manager.operate(bundle, digest, gateway, "edge-a", mode="apply", **kwargs)
            state = json.loads((machine.state / "state.json").read_text())
            self.assertIsNone(state["stable"]); self.assertEqual(state["pending"]["target"], digest)
            with self.assertRaises(ValueError): manager.operate(bundle, digest, gateway, "edge-a", mode="apply", **kwargs)
            machine.fail_role = None
            before = [call for call in machine.calls if str(call).startswith("activate-")]
            self.assertTrue(manager.operate(bundle, digest, gateway, "edge-a", mode="resume", **kwargs)["activated"])
            self.assertEqual([call for call in machine.calls if str(call).startswith("activate-")], before)
            proof = [json.loads(p.read_text()) for p in (machine.state / "attempts").glob("*/proof.json")]
            self.assertTrue(any(p.get("failed_at") == "before-service-activation" for p in proof))
            self.assertFalse(any(p["ha_verified"] for p in proof))

    @unittest.skipUnless(os.name == "posix", "POSIX drift and symlink fixture")
    def test_failed_release_cannot_overwrite_an_unowned_changed_unit_on_resume(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(manager.starter, "trusted_paths"), \
             mock.patch.object(manager, "host_lock", side_effect=lambda _: nullcontext()), mock.patch.object(manager.health, "healthy", return_value=True):
            root = Path(temporary); machine = FakeHost(root); machine.fail_role = "haproxy"
            bundle, digest, gateway, ca, artifacts = package(root)
            kwargs = dict(ca_file=ca, artifacts=artifacts, machine=machine)
            with self.assertRaises(TimeoutError): manager.operate(bundle, digest, gateway, "edge-a", mode="apply", **kwargs)
            (machine.units / machine.unit("edge-a", "haproxy")).write_text("unrelated service")
            machine.fail_role = None
            with self.assertRaises(ValueError): manager.operate(bundle, digest, gateway, "edge-a", mode="resume", **kwargs)
            self.assertEqual((machine.units / machine.unit("edge-a", "haproxy")).read_text(), "unrelated service")

    @unittest.skipUnless(os.name == "posix", "POSIX partial ownership fixture")
    def test_pending_reservation_does_not_authorize_an_unowned_local_vip(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(manager.starter, "trusted_paths"), \
             mock.patch.object(manager, "host_lock", side_effect=lambda _: nullcontext()), mock.patch.object(manager.health, "healthy", return_value=True):
            root = Path(temporary); machine = FakeHost(root); machine.fail_role = "haproxy"
            bundle, digest, gateway, ca, artifacts = package(root)
            kwargs = dict(ca_file=ca, artifacts=artifacts, machine=machine)
            with self.assertRaises(TimeoutError): manager.operate(bundle, digest, gateway, "edge-a", mode="apply", **kwargs)
            machine.fail_role = None; machine.vip_assigned = True
            before = list(machine.calls)
            with self.assertRaises(ValueError): manager.operate(bundle, digest, gateway, "edge-a", mode="resume", **kwargs)
            self.assertFalse(any(str(c).startswith("activate-") for c in machine.calls[len(before):]))

    @unittest.skipUnless(os.name == "posix", "POSIX retained-release tamper fixture")
    def test_retained_manifest_tampering_is_detected_by_host_receipt(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(manager.starter, "trusted_paths"), \
             mock.patch.object(manager, "host_lock", side_effect=lambda _: nullcontext()), mock.patch.object(manager.health, "healthy", return_value=True):
            root = Path(temporary); machine = FakeHost(root)
            bundle, digest, gateway, ca, artifacts = package(root)
            kwargs = dict(ca_file=ca, artifacts=artifacts, machine=machine)
            manager.operate(bundle, digest, gateway, "edge-a", mode="apply", **kwargs)
            manifest = machine.active.resolve() / "host-files.json"
            manifest.write_text("{}")
            with self.assertRaises(ValueError): manager.operate(bundle, digest, gateway, "edge-a", mode="verify", expected_current=digest, **kwargs)


if __name__ == "__main__": unittest.main()
