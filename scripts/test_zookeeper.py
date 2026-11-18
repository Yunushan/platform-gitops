#!/usr/bin/env python3
"""Test quorum packages, persistent member guards and bounded TLS status reads."""

import contextlib
import hashlib
import io
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import render_zookeeper as renderer
import start_zookeeper as starter
import yaml
from bounded_subprocess import run_bounded
from test_zuul_config import fixture


def plans(kubernetes=False):
    ci = fixture()
    members = [{"id": index, "name": f"zk-{index}",
                "host": f"zk-{index}.coordination.svc.cluster.local" if kubernetes else f"zk-{index}.example.com",
                "bind_address": f"192.0.2.{index + 10}", "failure_domain": f"rack-{index}",
                "claim": f"zk-{index}-data", "state_host_path": f"/srv/zookeeper/zk-{index}", "tls_secret": f"zk-{index}-tls"}
               for index in range(1, 4)]
    ci["zookeeper"] = [{"host": member["host"], "port": 2281} for member in members]
    jar = b"non-executable-distribution-fixture"
    plan = {"schema_version": 1, "ensemble_id": "11111111-1111-4111-8111-111111111111", "zookeeper_version": "3.9.4",
        "image": "registry.example.com/platform/zookeeper@sha256:" + "a" * 64, "namespace": "coordination",
        "cluster_domain": "cluster.local", "failure_domain_key": "topology.kubernetes.io/zone",
        "client_port": 2281, "quorum_port": 2888, "election_port": 3888, "initialize_if_empty": False,
        "artifacts": {"zookeeper-3.9.4.jar": hashlib.sha256(jar).hexdigest()}, "members": members}
    return plan, ci, jar


def provision(directory: Path):
    plan, ci, jar = plans()
    files = renderer.render(plan, ci, "docker")
    root, home, state = (directory / name for name in ("config", "distribution", "state"))
    root.mkdir()
    (root / "private").mkdir()
    (home / "lib").mkdir(parents=True)
    state.mkdir()
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8", newline="\n")
    for name in starter.TLS_FILES:
        (root / "private" / name).write_bytes(b"non-secret-metadata-fixture")
        (root / "private" / name).chmod(0o640)
    (home / "lib" / "zookeeper-3.9.4.jar").write_bytes(jar)
    digest = json.loads(files["render-status.json"])["contract_sha256"]["zk-1"]
    return root, home, state, digest


class ZooKeeperTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("ZOOKEEPER_TEST_RUNTIME")=="1","explicit isolated ZooKeeper runtime required")
    def test_real_guarded_tls_quorum_acl_and_leader_loss(self):
        from test_zookeeper_runtime_fixture import exercise
        report=exercise(self)
        self.assertTrue(report['real_three_member_tls_quorum_verified'])
        self.assertTrue(report['same_client_session_after_leader_loss_verified'])
        self.assertTrue(report['original_member_state_rejoin_verified'])
        self.assertTrue(report['minority_write_refused_and_original_quorum_recovered'])
        self.assertTrue(report['network_partition_reconnect_and_session_fencing_verified'])
        self.assertTrue(report['original_member_processes_removed'])
        self.assertFalse(report['physical_ha_verified'])

    def test_no_plaintext_listener_or_single_server_fallback(self):
        plan, ci, _ = plans()
        files = renderer.render(plan, ci, "docker")
        for member in plan["members"]:
            settings = dict(line.split("=", 1) for line in files[member["name"] + ".cfg"].splitlines())
            self.assertNotIn("clientPort", settings)
            self.assertEqual(settings["secureClientPort"], "2281")
            self.assertEqual(settings["standaloneEnabled"], "false")
            self.assertEqual(settings["portUnification"], "false")
            self.assertEqual(settings["client.portUnification"], "false")
            self.assertEqual(settings["sslQuorum"], "true")
            for prefix in ("ssl.", "ssl.quorum."):
                self.assertEqual(settings[prefix + "clientAuth"], "need")
                self.assertEqual(settings[prefix + "hostnameVerification"], "true")
                self.assertEqual(settings[prefix + "keyStore.type"], "PEM")
            self.assertEqual(settings["enforce.auth.schemes"], "x509")
            self.assertEqual(settings["4lw.commands.whitelist"], "srvr")
            self.assertEqual(settings["admin.enableServer"], "false")
            peers = [value for key, value in settings.items() if key.startswith("server.")]
            self.assertEqual(len(peers), 3)
            self.assertTrue(all(value.endswith(":participant") and ";" not in value for value in peers))

    def test_kubernetes_never_duplicates_a_member_or_shares_storage_or_private_keys(self):
        plan, ci, _ = plans(kubernetes=True)
        reference = None
        for runtime in ("rke2", "k3s", "k8s"):
            files = renderer.render(plan, ci, runtime)
            objects = list(yaml.safe_load_all(files["zookeeper-members.yaml"]))
            if reference is None:
                reference = objects
            self.assertEqual(objects, reference)
            config = next(item for item in objects if item["kind"] == "ConfigMap")
            self.assertTrue(config["immutable"])
            deployments = [item for item in objects if item["kind"] == "Deployment"]
            self.assertEqual(len(deployments), 3)
            claims, secrets, domains = set(), set(), set()
            for deployment in deployments:
                self.assertEqual(deployment["spec"]["replicas"], 1)
                self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})
                pod = deployment["spec"]["template"]["spec"]
                self.assertFalse(pod["automountServiceAccountToken"])
                self.assertTrue(pod["securityContext"]["runAsNonRoot"])
                affinity = pod["affinity"]
                self.assertIn("requiredDuringSchedulingIgnoredDuringExecution", affinity["podAntiAffinity"])
                terms = affinity["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
                domains.add(terms[0]["matchExpressions"][0]["values"][0])
                volumes = {volume["name"]: volume for volume in pod["volumes"]}
                claims.add(volumes["state"]["persistentVolumeClaim"]["claimName"])
                secrets.add(volumes["private"]["secret"]["secretName"])
                self.assertEqual({item["key"] for item in volumes["private"]["secret"]["items"]}, set(starter.TLS_FILES))
                container = pod["containers"][0]
                self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
                self.assertIn("--probe", container["readinessProbe"]["exec"]["command"])
                self.assertNotIn("livenessProbe", container)
                self.assertNotIn("--initialize-if-empty", container["command"])
            self.assertEqual((len(claims), len(secrets), len(domains)), (3, 3, 3))
            pdb = next(item for item in objects if item["kind"] == "PodDisruptionBudget")
            self.assertEqual(pdb["spec"]["minAvailable"], 2)
            for service in (item for item in objects if item["kind"] == "Service"):
                self.assertTrue(service["spec"]["publishNotReadyAddresses"])
                matches = [deployment for deployment in deployments
                           if deployment["spec"]["template"]["metadata"]["labels"] == service["spec"]["selector"]]
                self.assertEqual(len(matches), 1)

    def test_docker_is_one_member_per_host_with_explicit_private_publish_addresses(self):
        plan, ci, _ = plans()
        files = renderer.render(plan, ci, "docker")
        for member in plan["members"]:
            services = yaml.safe_load(files[member["name"] + "-compose.yaml"])["services"]
            self.assertEqual(list(services), [member["name"]])
            service = services[member["name"]]
            self.assertEqual(service["command"], [])
            self.assertNotIn("network_mode", service)
            self.assertTrue(all(port.startswith(member["bind_address"] + ":") for port in service["ports"]))
            self.assertTrue(all(not volume["bind"]["create_host_path"] for volume in service["volumes"]))
            self.assertIn("--probe", service["healthcheck"]["test"])
        native = renderer.render(plan, ci, "raw")
        self.assertIn("dataDir=/srv/zookeeper/zk-1", native["zk-1.cfg"])
        self.assertIn("User=zookeeper", native["zk-1.service"])
        self.assertIn("ReadWritePaths=/srv/zookeeper/zk-1", native["zk-1.service"])

    def test_configurations_parse_with_real_compose_without_starting_any_container(self):
        docker = shutil.which("docker")
        if not docker:
            self.skipTest("Docker Compose CLI unavailable")
        plan, ci, _ = plans()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in renderer.render(plan, ci, "docker").items():
                (root / name).write_text(text, encoding="utf-8")
            for member in plan["members"]:
                result = run_bounded([docker, "compose", "-f", str(root / (member["name"] + "-compose.yaml")), "config", "--quiet"],
                                     timeout=30, text=True, input="", output_max_bytes=65536, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_endpoint_and_failure_domain_mismatches_are_rejected(self):
        for field in ("id", "host", "bind_address", "failure_domain", "claim", "state_host_path", "tls_secret"):
            plan, ci, _ = plans()
            plan["members"][1][field] = plan["members"][0][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                renderer.render(plan, ci, "docker")
        plan, ci, _ = plans()
        ci["zookeeper"][0]["port"] = 2282
        with self.assertRaises(ValueError):
            renderer.render(plan, ci, "raw")
        plan, ci, _ = plans()
        with self.assertRaises(ValueError):
            renderer.render(plan, ci, "k8s")
        for field, value in (("client_port", True), ("quorum_port", 2281), ("initialize_if_empty", "yes"),
                             ("failure_domain_key", []), ("zookeeper_version", "3.9.3"), ("extra", "ignored")):
            plan, ci, _ = plans()
            plan[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                renderer.validate(plan, ci)

    def test_local_check_locks_complete_classpath_and_configuration_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root, home, state, digest = provision(Path(directory))
            before = {str(path): path.read_bytes() for path in Path(directory).rglob("*") if path.is_file()}
            contract = starter.local_inputs("zk-1", digest, root=root, home=home)
            self.assertTrue(starter.state_inputs(contract, state=state))
            self.assertEqual(before, {str(path): path.read_bytes() for path in Path(directory).rglob("*") if path.is_file()})
            extra = home / "lib" / "unreviewed.jar"
            extra.write_bytes(b"unexpected")
            with self.assertRaises(ValueError):
                starter.local_inputs("zk-1", digest, root=root, home=home)
            extra.unlink()
            (root / "zk-1.cfg").write_bytes(b"clientPort=2181\n")
            with self.assertRaises(ValueError):
                starter.local_inputs("zk-1", digest, root=root, home=home)

    def test_member_initialization_never_adopts_or_overwrites_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root, home, state, digest = provision(Path(directory))
            contract = starter.local_inputs("zk-1", digest, root=root, home=home)
            self.assertFalse(starter.state_inputs(contract, state=state, initialize=True))
            self.assertEqual((state / "myid").read_bytes(), b"1\n")
            (state / "version-2").mkdir()
            (state / "version-2" / "log.1").write_bytes(b"preserve-existing-data")
            before = {str(path): path.read_bytes() for path in state.rglob("*") if path.is_file()}
            self.assertFalse(starter.state_inputs(contract, state=state, initialize=True))
            for field, value in (("member_id", 2), ("ensemble_id", "22222222-2222-4222-8222-222222222222"), ("members", [])):
                changed = {**contract, field: value}
                with self.subTest(field=field), self.assertRaises(ValueError):
                    starter.state_inputs(changed, state=state, initialize=True)
            self.assertEqual(before, {str(path): path.read_bytes() for path in state.rglob("*") if path.is_file()})

    def test_unmanaged_and_interrupted_initialization_stop_for_inspection(self):
        for name, content in (("myid", b"1\n"), (starter.MARKER, b"{}"), ("snapshot.1", b"existing")):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root, home, state, digest = provision(Path(directory))
                contract = starter.local_inputs("zk-1", digest, root=root, home=home)
                path = state / name
                path.write_bytes(content)
                with self.assertRaises((ValueError, OSError)):
                    starter.state_inputs(contract, state=state, initialize=True)
                self.assertEqual(path.read_bytes(), content)
                self.assertEqual(len(list(state.iterdir())), 1)

    def test_foreground_exec_requires_initialization_authorization_and_strips_jvm_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root, home, state, digest = provision(Path(directory))
            with mock.patch.object(starter.sys, "platform", "linux"), mock.patch.object(starter.os, "geteuid", return_value=1000, create=True), \
                    mock.patch.object(starter.shutil, "which", return_value="/reviewed/java"), \
                    mock.patch.object(starter, "run_bounded", return_value=subprocess.CompletedProcess([], 0, "Apache ZooKeeper, version 3.9.4-abcdef\n", "")), \
                    mock.patch.object(starter.os, "execve") as execute, mock.patch.dict(os.environ, {"JAVA_TOOL_OPTIONS": "-Dunsafe=true", "LD_PRELOAD": "unreviewed"}):
                with self.assertRaises(ValueError):
                    starter.launch("zk-1", digest, root=root, home=home, state=state)
                execute.assert_not_called()
                self.assertEqual(list(state.iterdir()), [])
                starter.launch("zk-1", digest, initialize=True, root=root, home=home, state=state)
                binary, argv, env = execute.call_args.args
                self.assertEqual(binary, "/reviewed/java")
                self.assertEqual(argv[-2:], ["org.apache.zookeeper.server.quorum.QuorumPeerMain", str(root / "zk-1.cfg")])
                self.assertNotIn("JAVA_TOOL_OPTIONS", env)
                self.assertNotIn("LD_PRELOAD", env)
                self.assertEqual((state / "myid").read_bytes(), b"1\n")

    def test_changed_jar_missing_tls_and_world_readable_keys_stop_the_member(self):
        with tempfile.TemporaryDirectory() as directory:
            root, home, _state, digest = provision(Path(directory))
            jar = home / "lib" / "zookeeper-3.9.4.jar"
            original = jar.read_bytes()
            jar.write_bytes(b"different-runtime")
            with self.assertRaises(ValueError):
                starter.local_inputs("zk-1", digest, root=root, home=home)
            jar.write_bytes(original)
            key = root / "private" / "health-client.key"
            if os.name == "posix":
                key.chmod(0o644)
                with self.assertRaises(ValueError):
                    starter.local_inputs("zk-1", digest, root=root, home=home)
                key.chmod(0o640)
            key.unlink()
            with self.assertRaises(OSError):
                starter.local_inputs("zk-1", digest, root=root, home=home)

    def test_root_start_and_default_cli_never_initialize_or_connect(self):
        with mock.patch.object(starter.sys, "platform", "linux"), mock.patch.object(starter.os, "geteuid", return_value=0, create=True), \
                mock.patch.object(starter, "local_inputs") as inputs, mock.patch.object(starter.os, "execve") as execute:
            with self.assertRaises(ValueError):
                starter.launch("zk-1", "a" * 64, initialize=True)
            inputs.assert_not_called()
            execute.assert_not_called()
        with mock.patch.object(starter, "local_inputs", return_value={"data_dir": "/var/lib/zookeeper"}), \
                mock.patch.object(starter, "state_inputs", return_value=True) as state, \
                mock.patch.object(starter, "launch") as launch, mock.patch.object(starter, "probe") as probe, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(starter.main(["--member", "zk-1", "--contract-sha256", "a" * 64]), 0)
            self.assertTrue(json.loads(stdout.getvalue())["read_only"])
            self.assertTrue(json.loads(stdout.getvalue())["state_initialization_pending"])
            self.assertNotIn("initialize", state.call_args.kwargs)
            launch.assert_not_called()
            probe.assert_not_called()

    def test_wrong_version_or_validator_mutation_cannot_start_or_initialize(self):
        for change in ("wrong-version", "config-mutation"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root, home, state, digest = provision(Path(directory))
                def validation(*args, change=change, root=root, **kwargs):
                    if change == "config-mutation":
                        (root / "logback.xml").write_text("changed", encoding="utf-8")
                    return subprocess.CompletedProcess([], 0, "Apache ZooKeeper, version " + ("3.9.5" if change == "wrong-version" else "3.9.4"), "")
                with mock.patch.object(starter.sys, "platform", "linux"), mock.patch.object(starter.os, "geteuid", return_value=1000, create=True), \
                        mock.patch.object(starter.shutil, "which", return_value="/reviewed/java"), mock.patch.object(starter, "run_bounded", side_effect=validation), \
                        mock.patch.object(starter.os, "execve") as execute, self.assertRaises(ValueError):
                    starter.launch("zk-1", digest, initialize=True, root=root, home=home, state=state)
                execute.assert_not_called()
                self.assertEqual(list(state.iterdir()), [])

    def test_default_cli_and_render_never_claim_deployment_or_contact_members(self):
        plan, ci, _ = plans()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / name for name in ("zk.json", "ci.json")]
            for path, content in zip(paths, (plan, ci)):
                path.write_text(json.dumps(content), encoding="utf-8")
            output = root / "private" / "new"
            args = ["--plan", str(paths[0]), "--zuul-plan", str(paths[1]), "--runtime", "docker", "--output-dir", str(output)]
            with mock.patch.object(renderer, "ROOT", root), mock.patch.object(renderer, "MODULES", ()), contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(renderer.main(args), 0)
            self.assertNotIn("example.com", stdout.getvalue())
            self.assertFalse(json.loads(stdout.getvalue())["deployed"])
            before = (output / "zk-1.cfg").read_bytes()
            with mock.patch.object(renderer, "ROOT", root), mock.patch.object(renderer, "MODULES", ()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(renderer.main(args), 1)
            self.assertEqual((output / "zk-1.cfg").read_bytes(), before)
            with mock.patch.object(starter, "local_inputs") as local, mock.patch.object(starter, "launch") as launch, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(starter.main(["--member", "zk-1", "--contract-sha256", "a" * 64, "--initialize-if-empty"]), 1)
                local.assert_not_called()
                launch.assert_not_called()

    def test_tls_probe_rejects_standalone_observer_and_oversized_responses(self):
        contract = {"host": "zk-1.example.com", "client_port": 2281, "probe_address": "127.0.0.1"}
        for payload, accepted in ((b"ZooKeeper version: 3.9.4\nMode: leader\n", True), (b"Mode: follower\n", True),
                                  (b"Mode: standalone\n", False), (b"Mode: observer\n", False),
                                  (b"This instance is not currently serving requests\n", False), (b"x" * 16385, False)):
            with self.subTest(payload=payload[:40]):
                context = mock.MagicMock()
                context.wrap_socket.return_value.__enter__.return_value.recv.side_effect = [payload, b""]
                with mock.patch.object(starter.ssl, "SSLContext", return_value=context) as create, \
                        mock.patch.object(starter.socket, "create_connection"):
                    if accepted:
                        report = starter.probe(contract)
                        self.assertTrue(report["member_serving"])
                        self.assertFalse(report["ha_verified"])
                    else:
                        with self.assertRaises(ValueError):
                            starter.probe(contract)
                    create.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
                    context.load_verify_locations.assert_called_once_with(cafile=str(Path("/etc/zookeeper/private/client-ca.pem")))
                    self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
                    self.assertEqual(context.wrap_socket.call_args.kwargs["server_hostname"], contract["host"])


class ZooKeeperTLSProbeTests(unittest.TestCase):
    """Real TLS protocol over memory BIOs, not a live socket or ZooKeeper server."""

    @classmethod
    def setUpClass(cls):
        cls.openssl = shutil.which("openssl")
        if not cls.openssl:
            raise unittest.SkipTest("OpenSSL CLI unavailable for local TLS fixtures")
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        private = cls.root / "private"
        private.mkdir()

        def openssl(*args):
            result = run_bounded([cls.openssl, *args], timeout=20, text=True, input="", output_max_bytes=65536, check=False)
            if result.returncode:
                raise AssertionError("OpenSSL fixture generation failed; no private material printed")

        ca, key = private / "client-ca.pem", cls.root / "ca.key"
        openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                "-subj", "/CN=local-test-ca", "-days", "1", "-keyout", str(key), "-out", str(ca),
                "-addext", "basicConstraints=critical,CA:TRUE")
        for name, purpose, serial in (("server", "serverAuth", 2), ("health-client", "clientAuth", 3)):
            csr, cert, leaf_key, extensions = (cls.root / (name + suffix) for suffix in (".csr", ".pem", ".key", ".ext"))
            extension_text = "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=" + purpose + "\n"
            if name == "server":
                extension_text += "subjectAltName=DNS:localhost\n"
            extensions.write_text(extension_text, encoding="utf-8")
            openssl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-subj", "/CN=" + name, "-keyout", str(leaf_key), "-out", str(csr))
            openssl("x509", "-req", "-in", str(csr), "-CA", str(ca), "-CAkey", str(key), "-set_serial", str(serial),
                    "-days", "1", "-extfile", str(extensions), "-out", str(cert))
            openssl("verify", "-CAfile", str(ca), "-purpose", "sslserver" if name == "server" else "sslclient", str(cert))
            if name == "health-client":
                shutil.copyfile(cert, private / "health-client.pem")
                shutil.copyfile(leaf_key, private / "health-client.key")

    def status_request(self, *, host="localhost", send_client=True):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(self.root / "server.pem"), str(self.root / "server.key"))
        context.load_verify_locations(cafile=str(self.root / "private" / "client-ca.pem"))
        context.verify_mode = ssl.CERT_REQUIRED
        test = self
        def wrap(client_context, raw, *, server_hostname):
            client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
            client = client_context.wrap_bio(client_in, client_out, server_side=False, server_hostname=server_hostname)
            server = context.wrap_bio(server_in, server_out, server_side=True)
            def pump():
                for source, destination in ((client_out, server_in), (server_out, client_in)):
                    while source.pending:
                        destination.write(source.read())
            completed = [False, False]
            for _ in range(32):
                for index, peer in enumerate((client, server)):
                    if not completed[index]:
                        try:
                            peer.do_handshake()
                            completed[index] = True
                        except ssl.SSLWantReadError:
                            pass
                    pump()
                if all(completed):
                    break
            test.assertEqual(completed, [True, True], "In-memory TLS handshake did not complete")
            test.assertTrue(client_context.check_hostname)
            test.assertEqual(client_context.verify_mode, ssl.CERT_REQUIRED)
            test.assertIsNone(client_context.keylog_filename)
            class Channel:
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
                def settimeout(self, seconds):
                    test.assertGreater(seconds, 0)
                def sendall(self, data):
                    client.write(data)
                    pump()
                    test.assertEqual(server.read(4), b"srvr")
                    server.write(b"ZooKeeper version: fixture\nMode: follower\n")
                    try:
                        server.unwrap()
                    except ssl.SSLWantReadError:
                        pass
                    pump()
                def recv(self, size):
                    return client.read(size)
            return Channel()
        # Use the probe's real client SSLContext, CA and key loading; replace
        # only socket transport. Host interception cannot change fixture PKI.
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(starter.socket, "create_connection"))
            stack.enter_context(mock.patch.object(ssl.SSLContext, "wrap_socket", wrap))
            if not send_client:
                stack.enter_context(mock.patch.object(ssl.SSLContext, "load_cert_chain", return_value=None))
            return starter.probe({"host": host, "client_port": 2281, "probe_address": "127.0.0.1"}, root=self.root)

    def test_mutual_tls_protocol_accepts_pinned_server_name_and_client(self):
        report = self.status_request()
        self.assertTrue(report["tls_status_probe"])
        self.assertEqual(report["mode"], "follower")
        self.assertFalse(report["ha_verified"])

    def test_tls_protocol_rejects_wrong_server_name_and_missing_client_certificate(self):
        with self.assertRaises(ssl.SSLError):
            self.status_request(host="wrong.example.com")
        with self.assertRaises(ssl.SSLError):
            self.status_request(send_client=False)


if __name__ == "__main__":
    unittest.main()
