#!/usr/bin/env python3
"""Test durability/placement policy and private Patroni materialization without a database."""

import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import socket
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import ExitStack
from types import SimpleNamespace
from uuid import uuid4

import yaml

from bounded_subprocess import run_bounded
from bounded_file import read_bounded_bytes
import cnpg_release
import materialize_patroni as materializer
import postgres_ha_policy as policy
import render_postgres_ha as renderer
import start_patroni as startup
from render_private_platform_values import configured_cnpg_postgres_cluster_manifest


def fixture():
    return {"schema_version": 1, "cluster_id": "11111111-2222-3333-4444-555555555555", "name": "platform-postgres", "namespace": "platform-databases",
        "postgres_version": "18.4", "cnpg_version": "1.30.0", "patroni_version": "4.1.5", "images": {"postgres": "registry.example.com/postgres@sha256:" + "a" * 64, "patroni": "registry.example.com/patroni@sha256:" + "b" * 64},
        "storage": {"size": "100Gi", "class": "ceph-rbd"}, "bootstrap": {"database": "platform", "owner": "platform_owner", "secret": "postgres-owner"},
        "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"cpu": "2", "memory": "4Gi"}},
        "failure_domain_key": "topology.kubernetes.io/zone", "replication": {"standbys": 1, "synchronous_commit": "remote_apply"},
        "ports": {"sql": 5432, "rest": 8008}, "client_cidrs": ["192.0.2.0/24"],
        "members": [{"name": "pg-" + str(i), "host": "pg-" + str(i) + ".example.com", "address": "192.0.2." + str(20 + i), "failure_domain": "rack-" + str(i),
                     "node_name": "db-" + str(i), "node_uid": "00000000-0000-0000-0000-" + str(i).zfill(12), "state_path": "/srv/postgres/pg-" + str(i), "tls_secret": "pg-" + str(i) + "-tls"} for i in (1, 2, 3)],
        "zookeeper": {"hosts": [{"host": "zk-" + str(i) + ".example.com", "port": 2281} for i in (1, 2, 3)], "principals": ["CN=patroni-pg-" + str(i) for i in (1, 2, 3)]},
        "tls": {"server_ca_secret": "postgres-ca", "server_secret": "postgres-tls", "ca_sha256": {plane: char * 64 for plane, char in (("postgres", "c"), ("rest", "d"), ("zk", "e"))}},
        "native": {"postgres_bin_dir": "/usr/lib/postgresql/18/bin", "uid": 26, "gid": 26}}


def write_endpoint_fixture():
    return {"host": "postgres-write.example.com", "port": 6432, "client_cidrs": ["192.0.2.1/32"],
            "haproxy_image": "registry.example.com/haproxy@sha256:" + "c" * 64,
            "routers": [{"name": f"edge-{index}", "address": f"192.0.2.{index + 9}",
                         "failure_domain": f"entry-zone-{index}"} for index in (1, 2)]}


def write_package(root, plan, runtime):
    files = renderer.render(plan, runtime); bundle = root / "bundle"; bundle.mkdir()
    for name, value in files.items(): (bundle / name).write_text(value, encoding="utf-8", newline="\n")
    digest = json.loads(files["render-status.json"])["contract_sha256"]
    return bundle, digest


class PolicyTests(unittest.TestCase):
    def test_patch_schema_cannot_silently_fall_back_to_the_previous_release(self):
        plan = fixture()
        plan["cnpg_version"] = "1.30.1"
        for runtime in ("rke2", "k3s", "k8s"):
            with self.assertRaises(ValueError):
                renderer.render(plan, runtime)
            with self.assertRaises(ValueError):
                renderer.render(plan, runtime, cnpg_release=b"unreviewed release")
        plan["cnpg_version"] = "1.30.99"
        with self.assertRaises(ValueError):
            renderer.render(plan, "rke2")

    def test_patch_package_carries_the_schema_selected_by_its_reviewed_manifest(self):
        schema = {"type": "object", "properties": {"spec": {"type": "object", "properties": {"reviewedPatchField": {"type": "string"}}}}}
        payload = yaml.safe_dump_all([{"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
            "metadata": {"name": "clusters.postgresql.cnpg.io"}, "spec": {"versions": [
                {"name": "v1", "served": True, "storage": True, "schema": {"openAPIV3Schema": schema}}]}}]).encode()
        reviewed = {"manifest_sha256": hashlib.sha256(payload).hexdigest()}
        plan = fixture()
        plan["cnpg_version"] = "1.30.1"
        with mock.patch.dict(cnpg_release.RELEASES, {"1.30.1": reviewed}):
            for runtime in ("rke2", "k3s", "k8s"):
                package = renderer.render(plan, runtime, cnpg_release=payload)
                self.assertEqual(json.loads(package["cnpg-cluster-schema.json"]), schema)
                self.assertEqual(json.loads(package["postgres-contract.json"])["plan"]["cnpg_version"], "1.30.1")
            with self.assertRaises(ValueError):
                cnpg_release.cluster_schema("1.30.1", payload + b"\n")
        self.assertNotEqual(json.loads(renderer.cnpg_schema_text()), schema)

    @unittest.skipUnless(os.environ.get("CNPG_TEST_RELEASE"), "Reviewed operator release fixture not selected")
    def test_official_patch_manifest_matches_its_selected_database_schema(self):
        payload = read_bounded_bytes(Path(os.environ["CNPG_TEST_RELEASE"]), max_bytes=2 * 1024 ** 2)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), cnpg_release.RELEASES["1.30.1"]["manifest_sha256"])
        upstream = cnpg_release.objects("1.30.1", payload)
        definition = next(value for value in upstream if value["kind"] == "CustomResourceDefinition" and value["metadata"]["name"] == "clusters.postgresql.cnpg.io")
        expected = next(value["schema"]["openAPIV3Schema"] for value in definition["spec"]["versions"] if value["name"] == "v1")
        plan = fixture()
        plan["cnpg_version"] = "1.30.1"
        package = renderer.render(plan, "rke2", cnpg_release=payload)
        self.assertEqual(json.loads(package["cnpg-cluster-schema.json"]), expected)
        self.assertNotEqual(expected, json.loads(renderer.cnpg_schema_text()))

    def test_host_router_checks_authenticated_primary_and_restricts_source_network(self):
        plan = fixture()
        plan["write_endpoint"] = write_endpoint_fixture()
        with tempfile.TemporaryDirectory() as path:
            bundle, digest = write_package(Path(path), plan, "raw")
            contract, template = materializer.load(bundle, digest, "pg-1")
            config_path = bundle / "postgres-write-edge-1.cfg"
            config = config_path.read_text()
            self.assertIn("bind 192.0.2.10:6432", config)
            self.assertIn("acl approved_client src 192.0.2.1/32", config)
            self.assertIn("nbsrv(patroni_primary) eq 1", config)
            self.assertIn("init-state fully-down on-marked-down shutdown-sessions", config)
            self.assertIn("http-check connect port 8008 ssl", config)
            self.assertIn("http-check send meth GET uri /primary", config)
            for member in plan["members"]:
                self.assertIn(f"check-sni {member['host']} verify required verifyhost {member['host']}", config)
            self.assertIn('hostssl "platform" "platform_owner" 192.0.2.10/32 scram-sha-256',
                          template["postgresql"]["pg_hba"])
            self.assertEqual(contract["plan"]["write_endpoint"], plan["write_endpoint"])
            config_path.write_text(config.replace("verify required", "verify none"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "configuration differs"):
                materializer.load(bundle, digest, "pg-1")
        files = renderer.render(plan, "docker")
        compose = yaml.safe_load(files["compose.postgres-write-edge-2.yaml"])
        router = compose["services"]["postgres-write-edge-2"]
        self.assertEqual(router["image"], plan["write_endpoint"]["haproxy_image"])
        self.assertEqual(router["network_mode"], "host")
        self.assertFalse(router["volumes"][0]["bind"]["create_host_path"])
        with self.assertRaisesRegex(ValueError, "optional Patroni write endpoint"):
            renderer.render(plan, "k3s")
        plan["write_endpoint"]["routers"][1]["address"] = plan["write_endpoint"]["routers"][0]["address"]
        with self.assertRaisesRegex(ValueError, "distinct identities"):
            renderer.render(plan, "raw")

    def test_host_application_rules_precede_legacy_client_access(self):
        plan = fixture()
        plan["applications"] = [{"database": "sameuser", "owner": "all", "secret": "second-owner"}]
        for runtime in ("raw", "docker"):
            with self.subTest(runtime=runtime):
                files = renderer.render(plan, runtime)
                contract = json.loads(files["postgres-contract.json"])
                self.assertIn("provision_patroni_application.py", contract["files_sha256"])
                cfg = yaml.safe_load(files["patroni-pg-1.template.yaml"])
                rules = cfg["postgresql"]["pg_hba"]
                self.assertEqual(rules, cfg["bootstrap"]["pg_hba"])
                allow = rules.index('hostssl "sameuser" "all" 192.0.2.0/24 scram-sha-256')
                refuse = rules.index('host all "platform_owner","all" 0.0.0.0/0 reject')
                legacy = rules.index("hostssl all all 192.0.2.0/24 scram-sha-256")
                self.assertLess(allow, refuse)
                self.assertLess(refuse, legacy)
        with self.assertRaisesRegex(ValueError, "additional Patroni"):
            renderer.render(plan, "rke2")
        plan["applications"][0]["owner"] = plan["bootstrap"]["owner"]
        with self.assertRaisesRegex(ValueError, "its own database, login and credential"):
            renderer.render(plan, "docker")

    def test_render_refuses_unreviewed_patroni_release_for_host_runtime(self):
        plan = fixture()
        plan["patroni_version"] = "4.1.6"
        for runtime in ("raw", "docker"):
            with self.subTest(runtime=runtime):
                with self.assertRaisesRegex(ValueError, "reviewed hostname-verifying ZooKeeper adapter"):
                    renderer.render(plan, runtime)

    def test_launcher_accepts_only_the_reviewed_hostname_verifying_runtime(self):
        plan = fixture()
        release = plan["patroni_version"]
        reviewed_hash = policy.STRICT_PATRONI_DCS[release]
        configurations = (
            (reviewed_hash, policy.STRICT_PATRONI_KAZOO, True),
            ("3dc418155d17671055f6943ccb1c34c1f3be7c1bfcdf5e2233b1343e68df1e8b", policy.STRICT_PATRONI_KAZOO, False),
            (reviewed_hash, "2.10.0", False),
        )
        for source_hash, kazoo_version, accepted in configurations:
            with self.subTest(source_hash=source_hash, kazoo_version=kazoo_version):
                postgres = SimpleNamespace(returncode=0, stdout="postgres (PostgreSQL) 18.4\n")
                identity = SimpleNamespace(returncode=0, stdout=f"{release}\n{kazoo_version}\n{source_hash}\n")
                with mock.patch.object(startup, "run_bounded", side_effect=(postgres, identity)) as run:
                    config = Path("patroni.yaml")
                    if accepted:
                        self.assertEqual(startup.check_executables(plan, config, {}, validate_configuration=False),
                                         [sys.executable, "-I", "-B", "-m", "patroni", str(config)])
                    else:
                        with self.assertRaisesRegex(ValueError, "failed validation"):
                            startup.check_executables(plan, config, {}, validate_configuration=False)
                    self.assertEqual(run.call_count, 2)

    def test_each_host_package_runs_one_member_with_a_private_data_and_watchdog_scope(self):
        plan = fixture()
        for runtime in ("raw", "docker"):
            files = renderer.render(plan, runtime)
            digest = json.loads(files["render-status.json"])["contract_sha256"]
            self.assertIn("start_patroni.py", json.loads(files["postgres-contract.json"])["files_sha256"])
            for member in plan["members"]:
                name, state = member["name"], member["state_path"]
                if runtime == "raw":
                    unit = files[f"patroni-{name}.service"]
                    self.assertIn(f"--contract-sha256 {digest} --member {name} --execute", unit)
                    self.assertIn(f"ReadWritePaths={state}\n", unit)
                    self.assertIn("DeviceAllow=/dev/watchdog rw\n", unit)
                    self.assertIn("User=26\nGroup=26\n", unit)
                    continue
                services = yaml.safe_load(files[f"compose.patroni-{name}.yaml"])["services"]
                self.assertEqual(set(services), {f"patroni-{name}"})
                service = services[f"patroni-{name}"]
                self.assertEqual(service["image"], plan["images"]["patroni"])
                self.assertEqual(service["entrypoint"][-5:], ["--contract-sha256", digest, "--member", name, "--execute"])
                self.assertEqual(service["devices"], ["/dev/watchdog:/dev/watchdog:rw"])
                self.assertEqual([v["source"] for v in service["volumes"] if not v["read_only"]], [state])
                self.assertTrue(all(v["bind"]["create_host_path"] is False for v in service["volumes"]))

    @unittest.skipUnless(os.name == "posix", "State locks require POSIX directory descriptors")
    def test_managed_state_rejects_adoption_and_locks_the_original_member(self):
        plan = fixture()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(materializer, "directory"):
            state = Path(directory)
            member = {**plan["members"][0], "state_path": str(state)}
            old = state / "PG_VERSION"
            old.write_text("18\n")
            with self.assertRaisesRegex(ValueError, "empty dedicated state"):
                startup.claim_state(plan, member)
            self.assertEqual(old.read_text(), "18\n")
            old.unlink()
            original = startup.claim_state(plan, member)
            try:
                with self.assertRaises(BlockingIOError):
                    startup.claim_state(plan, member)
            finally:
                os.close(original)
            os.close(startup.claim_state(plan, member))
            with self.assertRaisesRegex(ValueError, "state identity differs"):
                startup.claim_state({**plan, "cluster_id": str(uuid4())}, member)

    def test_private_cnpg_renderer_requires_explicit_valid_durability_configuration(self):
        env = {"PLATFORM_PRODUCTION_STRICT": "false", "CNPG_BACKUP_ENABLED": "false"}
        for value in (None, "0", "1", "2"):
            settings = dict(env, CNPG_INSTANCES="3")
            if value is not None:
                settings["CNPG_SYNCHRONOUS_STANDBYS"] = value
            with self.subTest(standbys=value), mock.patch.dict(os.environ, settings, clear=True):
                cluster = next(obj for obj in yaml.safe_load_all(configured_cnpg_postgres_cluster_manifest())
                               if obj["kind"] == "Cluster")
                if value in {"1", "2"}:
                    self.assertEqual(cluster["spec"]["postgresql"]["synchronous"], {
                        "method": "any", "number": int(value), "dataDurability": "required", "failoverQuorum": True,
                    })
                else:
                    self.assertNotIn("postgresql", cluster["spec"])
        for value, instances in (("1", "2"), ("2", "1"), ("three", "3"), ("-1", "3")):
            with (self.subTest(standbys=value, instances=instances),
                  mock.patch.dict(os.environ, dict(env, CNPG_SYNCHRONOUS_STANDBYS=value,
                                                   CNPG_INSTANCES=instances), clear=True),
                  self.assertRaises(SystemExit)):
                configured_cnpg_postgres_cluster_manifest()

    def test_configuration_bytes_survive_contract_round_trip_and_quantity_normalization(self):
        plan = fixture(); plan["resources"]["requests"].update(cpu="1000m", memory="1024Mi")
        plan["storage"]["size"] = "1024Gi"
        for runtime in policy.RUNTIMES:
            self.assertEqual(renderer.render(plan, runtime), renderer.render(json.loads(json.dumps(plan, sort_keys=True)), runtime))
        spec = policy.cnpg(plan)["spec"]
        self.assertEqual(spec["resources"]["requests"], {"cpu": "1", "memory": "1Gi"})
        self.assertEqual(spec["storage"]["size"], "1Ti")

    def test_all_five_packages_preserve_unverified_installation_promotion_and_restore(self):
        for runtime in policy.RUNTIMES:
            files = renderer.render(fixture(), runtime)
            status = json.loads(files["render-status.json"])
            for key in ("deployed", "synchronous_replication_verified", "promotion_verified", "restore_verified", "ha_verified", "full_stack_verified"):
                self.assertIs(status[key], False)
            self.assertIn("fenced-promotion-and-client-reconnect-tests", status["remaining"])
            self.assertEqual("postgres-resources.yaml" in files, runtime in {"rke2", "k3s", "k8s"})

    def test_cnpg_requires_durability_quorum_placement_and_explicit_primary_upgrade(self):
        plan = fixture(); resource = yaml.safe_load(renderer.render(plan, "rke2")["postgres-resources.yaml"])
        spec = resource["spec"]
        self.assertEqual(spec["instances"], 3); self.assertEqual(spec["primaryUpdateStrategy"], "supervised")
        self.assertFalse(spec["enableSuperuserAccess"])
        self.assertEqual(spec["resources"], plan["resources"])
        self.assertEqual(spec["postgresql"]["synchronous"], {"method": "any", "number": 1, "dataDurability": "required", "failoverQuorum": True})
        self.assertEqual(spec["affinity"]["podAntiAffinityType"], "required")
        self.assertEqual(spec["affinity"]["topologyKey"], "topology.kubernetes.io/zone")
        self.assertEqual(spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"], ["db-1", "db-2", "db-3"])
        self.assertTrue(spec["postgresql"]["pg_hba"][-1].endswith("reject"))

    def test_patroni_requires_strict_sync_tls_acl_watchdog_and_keeps_failed_data(self):
        for count in (1, 2):
            plan = fixture(); plan["replication"]["standbys"] = count
            config = yaml.safe_load(renderer.render(plan, "raw")["patroni-pg-1.template.yaml"])
            self.assertEqual(config["bootstrap"]["dcs"]["synchronous_node_count"], count)
            self.assertTrue(config["bootstrap"]["dcs"]["synchronous_mode_strict"])
            self.assertFalse(any(key.endswith("_file") for key in config["bootstrap"]["dcs"]["postgresql"]["parameters"]))
            self.assertEqual(config["bootstrap"]["dcs"]["synchronous_mode"], "quorum")
            self.assertEqual(config["restapi"]["verify_client"], "required"); self.assertFalse(config["ctl"]["insecure"])
            self.assertIn("authentication", config["restapi"])
            self.assertTrue(config["zookeeper"]["use_ssl"]); self.assertTrue(config["zookeeper"]["verify"])
            self.assertEqual(set(config["zookeeper"]["set_acls"]), set(plan["zookeeper"]["principals"]))
            self.assertEqual(config["watchdog"]["mode"], "required")
            self.assertFalse(config["postgresql"]["remove_data_directory_on_rewind_failure"])
            self.assertFalse(config["postgresql"]["remove_data_directory_on_diverged_timelines"])
            self.assertTrue(config["bootstrap"]["pg_hba"][0].endswith("scram-sha-256"))
            self.assertTrue(all(auth["sslmode"] == "verify-full" for auth in config["postgresql"]["authentication"].values()))

    def test_policy_rejects_relaxed_durability_shared_domains_root_identity_and_unpinned_images(self):
        changes = [lambda p: p["replication"].update(standbys=0), lambda p: p["replication"].update(standbys=True),
                   lambda p: p["replication"].update(synchronous_commit="off"), lambda p: p["replication"].update(synchronous_mode_strict=False),
                   lambda p: p["members"][1].update(failure_domain=p["members"][0]["failure_domain"]), lambda p: p["members"][1].update(address=p["members"][0]["address"]),
                   lambda p: p["members"][0].update(state_path="/srv/postgres/../foreign"), lambda p: p["native"].update(uid=0),
                   lambda p: p["images"].update(postgres="postgres:latest"), lambda p: p.update(client_cidrs=["0.0.0.0/0"]),
                   lambda p: p["zookeeper"].update(principals=["world:anyone"] * 3), lambda p: p["members"][0].update(node_uid=None)]
        changes.append(lambda p: p["bootstrap"].update(owner="postgres"))
        changes.append(lambda p: p["resources"]["limits"].update(memory="128Mi"))
        for change in changes:
            plan = fixture(); change(plan)
            with self.assertRaises((ValueError, TypeError)): renderer.render(plan, "rke2")

    def test_native_does_not_require_kubernetes_node_uids(self):
        plan = fixture()
        for member in plan["members"]: member["node_uid"] = None
        self.assertIn("patroni-pg-1.template.yaml", renderer.render(plan, "raw"))
        with self.assertRaises(ValueError): renderer.render(plan, "k8s")

    def test_default_materializer_cannot_read_private_credentials_or_change_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); bundle, digest = write_package(root, fixture(), "docker")
            with mock.patch.object(materializer, "regular", side_effect=AssertionError("Private input read")):
                result = materializer.operate(bundle, digest, "pg-1", inputs=root / "absent-inputs", output=root / "absent-output")
            self.assertTrue(result["local_policy_verified"]); self.assertFalse(result["credentials_materialized"])
            self.assertFalse((root / "absent-output").exists())

    def test_changed_program_template_contract_and_member_are_refused(self):
        for name in ("materialize_patroni.py", "patroni-pg-1.template.yaml", "postgres-contract.json"):
            with tempfile.TemporaryDirectory() as temporary:
                bundle, digest = write_package(Path(temporary), fixture(), "raw")
                (bundle / name).write_text("unreviewed")
                with self.assertRaises(ValueError): materializer.load(bundle, digest, "pg-1")
        with tempfile.TemporaryDirectory() as temporary:
            bundle, digest = write_package(Path(temporary), fixture(), "raw")
            with self.assertRaises(ValueError): materializer.load(bundle, digest, "unreviewed-member")

    def test_optional_actual_cnpg_crd_schema(self):
        if os.environ.get("POSTGRES_POLICY_TEST_SCHEMA") != "1": self.skipTest("Opt in with jsonschema to validate against the vendored CNPG Cluster schema")
        from jsonschema import Draft7Validator
        crds = renderer.ROOT / "gitops/clusters/rke2-main/premium-3node/apps/cloudnativepg/charts/cloudnative-pg/templates/crds/crds.yaml"
        from strict_yaml import loads_strict_yaml_all
        # Helm's CRD file has a small template preamble, not part of the CRD.
        content = crds.read_text(encoding="utf-8")
        start = content.index("apiVersion: apiextensions.k8s.io/v1")
        clean = "\n".join(line for line in content[start:].splitlines() if not line.strip().startswith("{{"))
        objects = loads_strict_yaml_all(clean)
        cluster = next(obj for obj in objects if obj.get("metadata", {}).get("name") == "clusters.postgresql.cnpg.io")
        schema = next(item["schema"]["openAPIV3Schema"] for item in cluster["spec"]["versions"] if item["name"] == "v1")
        for runtime in ("rke2", "k3s", "k8s"):
            Draft7Validator(schema).validate(yaml.safe_load(renderer.render(fixture(), runtime)["postgres-resources.yaml"]))


@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0 and os.environ.get("POSTGRES_POLICY_TEST_NONROOT") == "1", "Opt in to /run and unprivileged TLS materialization fixtures")
class NonRootMaterializationTests(unittest.TestCase):
    def check_patroni_runtime(self, root, plan, inputs, passwords):
        python = os.environ.get("POSTGRES_POLICY_TEST_PATRONI_PYTHON")
        if not python:
            return
        self.assertNotEqual(os.readlink("/proc/self/ns/net"), os.readlink("/proc/1/ns/net"))
        bin_dir = Path(os.environ["POSTGRES_POLICY_TEST_PG_BIN"]).resolve(strict=True)
        libraries = Path(os.environ["POSTGRES_POLICY_TEST_PG_LIBS"]).resolve(strict=True)
        binaries = root / "pg-bin"
        binaries.mkdir(mode=0o755)
        for binary in bin_dir.iterdir():
            if binary.is_file():
                (binaries / binary.name).symlink_to(binary)
        postgres = binaries / "postgres"
        postgres.unlink()
        # Supply the extracted package's shared libraries without altering
        # the host linker or inheriting LD_LIBRARY_PATH in the startup helper.
        postgres.write_text("#!/bin/sh\nexport LD_LIBRARY_PATH=" + shlex.quote(str(libraries))
                            + "\nexec " + shlex.quote(str(bin_dir / "postgres")) + ' "$@"\n')
        postgres.chmod(0o755)
        plan = copy.deepcopy(plan)
        name = "fixture-" + uuid4().hex
        plan["members"][0]["name"] = name
        plan["postgres_version"] = "18.6"
        plan["native"]["postgres_bin_dir"] = str(binaries)
        with ExitStack() as stack:
            for index, endpoint in enumerate(plan["zookeeper"]["hosts"], start=101):
                listener = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
                listener.bind((f"127.0.0.{index}", 0))
                listener.listen()
                # The upstream validator probes TCP availability of all three
                # endpoints. This fixture exercises validation, not DCS protocol.
                endpoint.update(host=listener.getsockname()[0], port=listener.getsockname()[1])
            with tempfile.TemporaryDirectory(prefix="managed-", dir=root) as directory:
                managed = Path(directory)
                managed.chmod(0o755)
                runtime = Path("/run/platform-postgres-" + name)
                runtime.mkdir(mode=0o700)
                stack.callback(lambda: runtime.rmdir())
                stack.callback((runtime / "patroni.yaml").unlink, missing_ok=True)
                os.chown(runtime, 65534, 65534)
                bundle, digest = write_package(managed, plan, "raw")
                bundle.chmod(0o755)
                for path in bundle.iterdir():
                    path.chmod(0o444)
                command = ["/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", python,
                           "-E", "-s", "-B", str(bundle / "start_patroni.py"), "--contract-sha256", digest,
                           "--member", name, "--inputs-dir", str(inputs), "--check-runtime"]
                result = run_bounded(command, input="", text=True, timeout=70, output_max_bytes=65536, check=False)
                self.assertEqual(result.returncode, 0, "Patroni runtime validation failed; private diagnostics suppressed")
                self.assertTrue(json.loads(result.stdout)["runtime_configuration_verified"])
                self.assertFalse(any(value in result.stdout + result.stderr for value in passwords))
                configuration = yaml.safe_load((runtime / "patroni.yaml").read_text())
                self.assertEqual(configuration["postgresql"]["data_dir"], plan["members"][0]["state_path"] + "/pgdata")
                self.assertEqual(configuration["watchdog"]["mode"], "required")

    def test_real_tls_key_checks_private_output_idempotence_and_conflicting_rotation(self):
        with tempfile.TemporaryDirectory(prefix="platform-postgres-test-", dir="/run") as temporary:
            root = Path(temporary); root.chmod(0o755)
            plan = fixture(); plan["native"].update(uid=65534, gid=65534)
            inputs, output = root / "inputs", root / "output"
            for directory in (inputs, output): directory.mkdir(mode=0o700); os.chown(directory, 65534, 65534)
            for plane in ("postgres", "rest", "zk"):
                key, cert = inputs / (plane + ".key"), inputs / (plane + ".crt")
                subject = "/CN=patroni-pg-1" if plane == "zk" else "/CN=pg-1.example.com"
                ca, ca_key, csr = inputs / (plane + "-ca.crt"), root / (plane + "-ca.key"), root / (plane + ".csr")
                run_bounded(["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=fixture-" + plane + "-ca",
                    "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                    "-keyout", str(ca_key), "-out", str(ca)], input="", text=True, timeout=20, output_max_bytes=65536, check=True)
                run_bounded(["/usr/bin/openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", subject, "-keyout", str(key), "-out", str(csr)],
                    input="", text=True, timeout=20, output_max_bytes=65536, check=True)
                extensions = root / (plane + ".ext")
                purpose = "serverAuth" if plane == "postgres" else "clientAuth" if plane == "zk" else "serverAuth,clientAuth"
                extensions.write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=" + purpose
                    + "\nsubjectAltName=DNS:pg-1.example.com\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n")
                run_bounded(["/usr/bin/openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca), "-CAkey", str(ca_key), "-set_serial", "2", "-days", "1",
                    "-extfile", str(extensions), "-out", str(cert)], input="", text=True, timeout=20, output_max_bytes=65536, check=True)
                for path in (key, cert, ca): os.chown(path, 65534, 65534); path.chmod(0o600)
                plan["tls"]["ca_sha256"][plane] = hashlib.sha256(ca.read_bytes()).hexdigest()
            passwords = []
            for name in (*policy.PASSWORDS.values(), policy.REST_PASSWORD):
                value = secrets.token_urlsafe(32); passwords.append(value)
                path = inputs / name; path.write_text(value + "\n"); path.chmod(0o600); os.chown(path, 65534, 65534)
            bundle, digest = write_package(root, plan, "raw")
            bundle.chmod(0o755)
            for path in bundle.iterdir(): path.chmod(0o444)
            command = ["/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "/usr/bin/python3", "-E", "-s", "-B", str(bundle / "materialize_patroni.py"),
                "--bundle-dir", str(bundle), "--contract-sha256", digest, "--member", "pg-1", "--materialize", "--inputs-dir", str(inputs), "--output-file", str(output / "patroni.yaml")]
            for _ in range(2):
                result = run_bounded(command, input="", text=True, timeout=20, output_max_bytes=65536, check=False)
                self.assertEqual(result.returncode, 0, "Private materialization failed; raw diagnostics suppressed")
                self.assertTrue(json.loads(result.stdout)["credentials_materialized"])
                self.assertTrue(json.loads(result.stdout)["runtime_tls_material_verified"])
                self.assertFalse(any(value in result.stdout + result.stderr for value in passwords))
            path = output / "patroni.yaml"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600); self.assertEqual(path.stat().st_uid, 65534)
            self.check_patroni_runtime(root, plan, inputs, passwords)
            before = path.read_bytes()
            alias_command = [str(output / ".." / "output" / "patroni.yaml") if arg == str(path) else arg for arg in command]
            result = run_bounded(alias_command, input="", text=True, timeout=20, output_max_bytes=65536, check=False)
            self.assertNotEqual(result.returncode, 0); self.assertEqual(path.read_bytes(), before)
            for mismatch in ("hostname", "principal"):
                wrong = copy.deepcopy(plan)
                if mismatch == "hostname": wrong["members"][0]["host"] = "wrong.example.com"
                else: wrong["zookeeper"]["principals"][0] = "CN=wrong-principal"
                wrong_root = root / mismatch; wrong_root.mkdir(mode=0o755)
                wrong_bundle, wrong_digest = write_package(wrong_root, wrong, "raw")
                wrong_bundle.chmod(0o755)
                for item in wrong_bundle.iterdir(): item.chmod(0o444)
                wrong_command = [str(wrong_bundle / "materialize_patroni.py") if arg == str(bundle / "materialize_patroni.py") else str(wrong_bundle) if arg == str(bundle) else wrong_digest if arg == digest else arg for arg in command]
                result = run_bounded(wrong_command, input="", text=True, timeout=20, output_max_bytes=65536, check=False)
                self.assertNotEqual(result.returncode, 0); self.assertEqual(path.read_bytes(), before)
            changed = inputs / "superuser-password"; changed.write_text(secrets.token_urlsafe(32)); os.chown(changed, 65534, 65534); changed.chmod(0o600)
            result = run_bounded(command, input="", text=True, timeout=20, output_max_bytes=65536, check=False)
            self.assertNotEqual(result.returncode, 0); self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__": unittest.main()
