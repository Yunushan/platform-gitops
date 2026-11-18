#!/usr/bin/env python3
"""Validate edge contracts; explicitly opt in to isolated real VIP runtime checks."""

import base64
import contextlib
import copy
import csv
import hashlib
import http.client
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
from socketserver import BaseRequestHandler, ThreadingTCPServer
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import yaml

from bounded_subprocess import run_bounded
import deploy_gateway_edge as deploy
import gateway_edge_health as health
import render_cilium_gateway as gateway_renderer
import render_gateway_edge as renderer
from render_gerrit_router import ssh_nodeport_service
import start_gateway_edge as starter
from test_cilium_gateway import fixture as gateway_plan, FakeClient as GatewayClient, conditions, gerrit_route, owner, pod


def fixture(root, runtime="rke2", *, gerrit_ssh=None):
    selected = gateway_plan()
    if gerrit_ssh is not None:
        selected["routes"].append(gerrit_route())
        selected["tls_passthrough_port"] = 9443
    files = gateway_renderer.render(selected, runtime)
    directory = root / "gateway"
    directory.mkdir()
    for name, value in files.items(): (directory / name).write_text(value, encoding="utf-8", newline="\n")
    contract = json.loads(files["deployment-contract.json"])
    digest = json.loads(files["render-status.json"])["contract_sha256"]
    plan = {"schema_version": 1, "name": "platform-entry", "namespace": "platform-entry", "gateway_contract_sha256": digest,
        "vip": "192.0.2.50/24", "vrid": 77, "https_port": 443, "health_port": 8404, "minimum_ready_routes": 2, "client_cidrs": ["198.51.100.0/24"],
        "entry_hosts": [{"name": "edge-" + suffix, "address": "192.0.2." + str(10 + index), "interface": "eth0", "failure_domain": "zone-" + suffix,
            "node_name": "edge-" + suffix, "node_uid": char * 8 + "-" + char * 4 + "-4" + char * 3 + "-8" + char * 3 + "-" + char * 12}
            for index, (suffix, char) in enumerate((("a", "5"), ("b", "6")))],
        "backends": [{"node_name": "worker-" + suffix, "address": "192.0.2." + str(30 + index), "node_uid": char * 8 + "-" + char * 4 + "-4" + char * 3 + "-8" + char * 3 + "-" + char * 12}
            for index, (suffix, char) in enumerate((("a", "3"), ("b", "4")))],
        "routes": [{"hostname": r["hostname"], "health_path": "/healthz" if r["protocol"] != "h2c" else None,
                    "health_status": 200 if r["protocol"] != "h2c" else None} for r in selected["routes"]],
        "images": {role: "registry.example.com/platform/" + role + "@sha256:" + char * 64 for role, char in (("haproxy", "a"), ("keepalived", "b"))},
        "binaries": {role: {"path": (renderer.BASE + "/bin/" if runtime == "raw" else "/usr/sbin/") + role, "sha256": char * 64, "version": "3.2.26" if role == "haproxy" else "2.3.4"} for role, char in (("haproxy", "a"), ("keepalived", "b"))},
        "ca_sha256": hashlib.sha256(b"fixture-ca\n").hexdigest(), "ca_secret": "entry-backend-ca"}
    if gerrit_ssh is not None:
        plan["gerrit_ssh"] = gerrit_ssh
    return plan, contract, directory


def package(root, runtime="rke2", *, gitlab_ssh=None, gerrit_ssh=None):
    plan, contract, gateway = fixture(root, runtime, gerrit_ssh=gerrit_ssh)
    if gitlab_ssh is not None:
        plan["gitlab_ssh"] = gitlab_ssh
    files = renderer.render(plan, contract, runtime)
    directory = root / "edge"
    directory.mkdir()
    for name, value in files.items(): (directory / name).write_text(value, encoding="utf-8", newline="\n")
    return plan, directory, json.loads(files["render-status.json"])["contract_sha256"], gateway


class Client(GatewayClient):
    def __init__(self, plan, selected):
        super().__init__(selected)
        self.applied, self.plan_edge, self.edges, self.fail_first_peer, self.bad_ca = True, plan, {}, False, False
        for backend in plan["backends"]:
            node = self.objects["node/" + backend["node_name"]]
            node["metadata"]["uid"] = backend["node_uid"]
            node["status"]["addresses"] = [{"type": "InternalIP", "address": backend["address"]}]
        for host in plan["entry_hosts"]:
            self.objects["node/" + host["node_name"]] = {"metadata": {"uid": host["node_uid"], "labels": {"topology.kubernetes.io/zone": host["failure_domain"]}},
                "status": {"conditions": conditions("Ready"), "addresses": [{"type": "InternalIP", "address": host["address"]}]}}
        self.objects["namespace/" + plan["namespace"]] = {"metadata": {"labels": {"pod-security.kubernetes.io/enforce": "privileged"}}}

    @contextlib.contextmanager
    def frozen(self): yield self

    def get(self, resource, namespace=None, *, optional=False):
        if resource in self.edges: return copy.deepcopy(self.edges[resource])
        if resource in self.objects: return super().get(resource, namespace, optional=optional)
        if "/" + self.plan_edge["name"] in resource:
            self.calls.append((["get", resource, namespace], None))
            return None
        return super().get(resource, namespace, optional=optional)

    def call(self, args, *, payload=None, seconds=30):
        if args[:2] == ["get", "secret/" + self.plan_edge["ca_secret"]]:
            self.calls.append((args, payload))
            return "true\n" + base64.b64encode(b"different" if self.bad_ca else b"fixture-ca\n").decode()
        if args[0] in {"apply", "rollout"}:
            self.calls.append((args, payload))
            if args[0] == "apply" and "--dry-run=server" not in args:
                for obj in yaml.safe_load_all(payload):
                    obj["metadata"].update(uid="uid-" + obj["metadata"]["name"], generation=1)
                    if obj["kind"] == "DaemonSet":
                        obj["status"] = {"observedGeneration": 1, **{k: 1 for k in ("desiredNumberScheduled", "currentNumberScheduled", "updatedNumberScheduled", "numberReady", "numberAvailable")}}
                        # Match the API's omitted zero-value Pod fields.
                        for field in ("hostPID", "hostIPC"): obj["spec"]["template"]["spec"].pop(field, None)
                    group = "." + obj["apiVersion"].split("/")[0] if "/" in obj["apiVersion"] else ""
                    self.edges[obj["kind"].lower() + group + "/" + obj["metadata"]["name"]] = obj
                    if self.fail_first_peer and obj["kind"] == "DaemonSet": raise TimeoutError("Fixture lost peer update acknowledgment")
            return ""
        return super().call(args, payload=payload, seconds=seconds)


class EdgeTests(unittest.TestCase):
    def test_gerrit_ssh_adds_a_health_gate_and_requires_the_reviewed_tls_route(self):
        ssh = {"hostname": gerrit_route()["hostname"], "port": 29418, "node_port": 30294}
        for runtime in renderer.RUNTIMES:
            with tempfile.TemporaryDirectory() as directory:
                plan, bundle, digest, gateway = package(Path(directory), runtime, gerrit_ssh=ssh)
                reviewed, _, _ = deploy.load_package(bundle, digest, gateway)
                config = (bundle / "haproxy.cfg").read_text()
                self.assertIn("frontend external_gerrit_ssh\n    bind :29418", config)
                self.assertIn("server node0 192.0.2.30:30294 check", config)
                self.assertIn("{ nbsrv(gerrit_ssh) gt 0 }", config)
                selected = reviewed["gateway_plan"]
                selected["routes"][-1]["service"] = "another-service"
                with self.assertRaisesRegex(ValueError, "matching TLSRoute"):
                    renderer.validate(plan, selected, runtime)
                plan["gitlab_ssh"] = {**ssh, "hostname": "ssh.example.invalid"}
                with self.assertRaisesRegex(ValueError, "ports conflict"):
                    renderer.validate(plan, reviewed["gateway_plan"], runtime)

    def test_gerrit_ssh_requires_local_healthy_router_endpoints_on_both_serving_nodes(self):
        ssh = {"hostname": gerrit_route()["hostname"], "port": 29418, "node_port": 30294}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, bundle, digest, gateway = package(root, gerrit_ssh=ssh)
            _, _, selected = deploy.load_package(bundle, digest, gateway)
            route = gerrit_route()
            client = Client(plan, selected["plan"])
            service = ssh_nodeport_service(route["namespace"], ssh["node_port"])
            service["metadata"]["uid"] = "gerrit-ssh-service-uid"
            client.objects["service/gerrit-router-ssh"] = service
            endpoints = []
            for node in ("worker-a", "worker-b"):
                backend = pod("router", node, "fixture-image", "ReplicaSet", "rs")
                name = backend["metadata"]["name"]
                backend["metadata"].update(uid=name + "-uid", labels=route["pod_selector"])
                backend["spec"]["containers"][0]["ports"] = [{"name": "ssh", "containerPort": 29418}]
                client.objects["pod/" + name] = backend
                endpoints.append({"conditions": {"ready": True}, "targetRef": {"kind": "Pod", "name": name,
                                   "namespace": route["namespace"], "uid": backend["metadata"]["uid"]}})
            normal_call = client.call

            def endpoint_call(args, **kwargs):
                if "kubernetes.io/service-name=gerrit-router-ssh" in args:
                    return json.dumps({"items": [{"metadata": {"ownerReferences": owner("Service", service["metadata"]["uid"])},
                                       "ports": [{"name": "ssh", "port": 29418, "protocol": "TCP"}], "endpoints": endpoints}]})
                return normal_call(args, **kwargs)

            with mock.patch.object(deploy, "ROOT", root), mock.patch.object(deploy, "Client", return_value=client), mock.patch.object(client, "call", side_effect=endpoint_call):
                spec = service["spec"]
                spec["externalTrafficPolicy"] = "Cluster"
                with self.assertRaisesRegex(ValueError, "reviewed Local NodePort"):
                    deploy.operate(bundle, digest, gateway, apply=True, output=root / "private" / "wrong-policy")
                spec["externalTrafficPolicy"] = "Local"
                endpoints[-1]["conditions"]["ready"] = False
                with self.assertRaisesRegex(ValueError, "two selected serving failure domains"):
                    deploy.operate(bundle, digest, gateway, apply=True, output=root / "private" / "one-node")
                self.assertFalse(any(args[0] == "apply" for args, _ in client.calls))
                endpoints[-1]["conditions"]["ready"] = True
                self.assertTrue(deploy.operate(bundle, digest, gateway, apply=True,
                                               output=root / "private" / "verified-proof")["pair_ready"])
                native = root / "native"
                native.mkdir()
                _, bundle, digest, gateway = package(native, "raw", gerrit_ssh=ssh)
                client.calls.clear()
                client.bad_ca = True
                self.assertTrue(deploy.operate(bundle, digest, gateway, check_server=True,
                                               output=root / "private" / "native-check")["prerequisites_verified"])
                self.assertFalse(any(args[0] == "apply" for args, _ in client.calls))

    def test_tls_passthrough_uses_its_own_gateway_port_and_verified_https_health(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, contract, _ = fixture(Path(directory))
            selected = contract["plan"]
            gerrit = gerrit_route()
            host = gerrit["hostname"]
            selected["routes"].append(gerrit)
            selected["tls_passthrough_port"] = 9443
            plan["routes"].append({"hostname": host, "health_path": "/config/server/version", "health_status": 200})
            renderer.validate(plan, selected, "rke2")
            config = renderer.haproxy_config(plan, selected)
            self.assertIn("server node0 192.0.2.30:9443 check init-state fully-down check-ssl check-sni " + host, config)
            self.assertIn("server node0 192.0.2.30:8443 check init-state fully-down check-ssl check-sni forge.example.com", config)
            self.assertIn("GET uri /config/server/version ver HTTP/1.1 hdr Host " + host, config)
            plan["gitlab_ssh"] = {"hostname": "ssh.example.invalid", "port": 9443, "node_port": 30222}
            with self.assertRaisesRegex(ValueError, "ports conflict"):
                renderer.validate(plan, selected, "rke2")
            del plan["gitlab_ssh"], selected["https_port"], selected["tls_secret"]
            selected["routes"] = [gerrit]
            plan["routes"] = [plan["routes"][-1]]
            plan["minimum_ready_routes"] = 1
            renderer.validate(plan, selected, "rke2")
            self.assertIn("server node0 192.0.2.30:9443", renderer.haproxy_config(plan, selected))

    def test_native_entry_references_original_kubernetes_gateway_without_relabeling(self):
        from manage_gateway_edge_host import capture
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, original, gateway = fixture(root, "k3s")
            original_bytes = (gateway / "deployment-contract.json").read_bytes()
            plan["images"] = None  # Native daemons use the reviewed executable pins.
            for role in plan["binaries"]: plan["binaries"][role]["path"] = renderer.BASE + "/bin/" + role
            files = renderer.render(plan, original, "raw")
            bundle = root / "native-entry"; bundle.mkdir()
            for name, value in files.items(): (bundle / name).write_text(value, encoding="utf-8", newline="\n")
            digest = json.loads(files["render-status.json"])["contract_sha256"]
            contract, _ = capture(bundle, digest, gateway)
            self.assertEqual(contract["runtime"], "raw")
            self.assertEqual(contract["plan"]["gateway_contract_sha256"], hashlib.sha256(original_bytes).hexdigest())
            self.assertEqual((gateway / "deployment-contract.json").read_bytes(), original_bytes)
            self.assertEqual(json.loads(original_bytes)["runtime"], "k3s")

    @unittest.skipUnless(os.environ.get("GATEWAY_EDGE_VIP_TEST_RUNTIME") == "1", "Explicit isolated real HAProxy/Keepalived VIP runtime")
    def test_real_private_vip_health_failover(self):
        from test_gateway_edge_vip_fixture import exercise
        exercise(self, fixture)

    def test_all_runtime_packages_share_reviewed_tls_and_vrrp_contracts(self):
        for runtime in renderer.RUNTIMES:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plan, bundle, digest, _ = package(root, runtime)
                self.assertEqual(starter.load_bundle(bundle, digest, "edge-a")["runtime"], runtime)
                cfg = (bundle / "haproxy.cfg").read_text()
                self.assertIn("check-ssl check-sni forge.example.com verify required verifyhost forge.example.com", cfg)
                self.assertIn("init-state fully-down", cfg)
                self.assertTrue(all("check init-state fully-down" in line for line in cfg.splitlines() if line.strip().startswith("server ")))
                self.assertFalse(any(" ssl " in line for line in cfg.splitlines() if line.strip().startswith("server ")))
                self.assertIn("default_backend deny_unknown", cfg)
                for host in plan["entry_hosts"]:
                    vrrp = (bundle / ("keepalived-" + host["name"] + ".conf")).read_text()
                    self.assertIn("state BACKUP", vrrp)
                    self.assertIn("init_fail", vrrp)
                    self.assertIn("weight 0", vrrp)
                    self.assertNotIn("auth_pass", vrrp)
                self.assertFalse(json.loads((bundle / "render-status.json").read_text())["ha_verified"])

    def test_gitlab_ssh_listener_and_health_are_opt_in_for_all_runtimes(self):
        for runtime in renderer.RUNTIMES:
            with tempfile.TemporaryDirectory() as directory:
                plan, gateway, _ = fixture(Path(directory), runtime)
                plan["gitlab_ssh"] = {"hostname": "ssh.example.com", "port": 2222, "node_port": 30222}
                rendered = renderer.render(plan, gateway, runtime)
                config = rendered["haproxy.cfg"]
                self.assertIn("frontend external_gitlab_ssh\n    bind :2222", config)
                self.assertIn("server node0 192.0.2.30:30222 check", config)
                self.assertIn("{ nbsrv(gitlab_ssh) gt 0 }", config)
                self.assertIn("src -m ip 198.51.100.0/24", config.split("frontend external_gitlab_ssh")[1])
                plan["gitlab_ssh"]["port"] = plan["https_port"]
                with self.assertRaisesRegex(ValueError, "ports conflict"):
                    renderer.render(plan, gateway, runtime)

    def test_privileged_vip_boundary_is_separate_from_nonroot_haproxy_and_api_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            _, bundle, _, _ = package(Path(directory))
            resources = list(yaml.safe_load_all((bundle / "edge-workloads.yaml").read_text()))
            for ds in (r for r in resources if r["kind"] == "DaemonSet"):
                pod = ds["spec"]["template"]["spec"]
                self.assertTrue(pod["hostNetwork"])
                self.assertFalse(pod["automountServiceAccountToken"])
                by_name = {c["name"]: c for c in pod["containers"]}
                self.assertEqual(by_name["haproxy"]["securityContext"]["runAsUser"], 1000)
                self.assertEqual(by_name["keepalived"]["securityContext"]["capabilities"]["add"], ["NET_ADMIN", "NET_RAW", "SETGID"])
                self.assertFalse(by_name["keepalived"]["securityContext"]["privileged"])

    def test_local_mode_cannot_read_host_inputs_or_construct_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, bundle, digest, gateway = package(Path(directory))
            with mock.patch.object(starter, "host_inputs") as host, mock.patch.object(deploy, "Client") as client:
                self.assertFalse(starter.operate(bundle, digest, "edge-a", "keepalived")["started"])
                self.assertFalse(deploy.operate(bundle, digest, gateway)["pair_ready"])
            host.assert_not_called()
            client.assert_not_called()

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0,
                         "Root-owned Linux native artifact boundary")
    def test_native_host_inputs_bind_original_package_and_reject_artifact_drift(self):
        with tempfile.TemporaryDirectory(prefix="platform-edge-input-", dir="/run") as directory:
            root = Path(directory); (root / "bin").mkdir()
            ca = b"isolated CA fingerprint input"; (root / "ca.crt").write_bytes(ca)
            metadata = b"original build options"; (root / "bin/keepalived.config-opts").write_bytes(metadata)
            binaries = {}
            for role in ("haproxy", "keepalived"):
                path = root / "bin" / role; value = ("original " + role).encode()
                path.write_bytes(value); path.chmod(0o555)
                binaries[role] = {"sha256": hashlib.sha256(value).hexdigest()}
            binaries["keepalived"]["config_opts_sha256"] = hashlib.sha256(metadata).hexdigest()
            contract = {"runtime": "raw", "plan": {"entry_hosts": [{"name": "edge-a", "interface": "eth0", "address": "192.0.2.10"}],
                "ca_sha256": hashlib.sha256(ca).hexdigest(), "binaries": binaries}}
            with mock.patch.object(starter, "interface_address", return_value="192.0.2.10"):
                for role in binaries:
                    self.assertEqual(starter.host_inputs(root, contract, "edge-a", role, inputs=root), root / "bin" / role)
                (root / "bin/keepalived.config-opts").write_bytes(b"changed metadata")
                with self.assertRaisesRegex(ValueError, "metadata changed"):
                    starter.host_inputs(root, contract, "edge-a", "keepalived", inputs=root)
                (root / "bin/haproxy").chmod(0o755); (root / "bin/haproxy").write_bytes(b"changed binary")
                with self.assertRaisesRegex(ValueError, "reviewed root-owned artifact"):
                    starter.host_inputs(root, contract, "edge-a", "haproxy", inputs=root)

    def test_bad_topology_unpinned_images_and_unreviewed_gateway_are_rejected(self):
        faults = [lambda p: p.update(vip="192.0.2.255/24"), lambda p: p["entry_hosts"][1].update(address="192.0.2.10"),
                  lambda p: p["entry_hosts"][1].update(failure_domain="zone-a"), lambda p: p["images"].update(haproxy="haproxy:latest"),
                  lambda p: p["routes"][0].update(health_path="/health\nmalicious"), lambda p: p.update(gateway_contract_sha256="0" * 64)]
        for fault in faults:
            with tempfile.TemporaryDirectory() as directory:
                plan, contract, _ = fixture(Path(directory))
                fault(plan)
                with self.assertRaises(ValueError): renderer.render(plan, contract, "rke2")

    def test_ordered_rollout_verification_and_partial_failure_journal(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plan, bundle, digest, gateway = package(root)
                selected = gateway_plan()
                client = Client(plan, selected)
                client.fail_first_peer = fail
                with mock.patch.object(deploy, "ROOT", root), mock.patch.object(deploy, "Client", return_value=client):
                    if fail:
                        with self.assertRaises(TimeoutError): deploy.operate(bundle, digest, gateway, apply=True, output=root / "private" / "proof")
                    else:
                        self.assertTrue(deploy.operate(bundle, digest, gateway, apply=True, output=root / "private" / "proof")["pair_ready"])
                peers = [obj["metadata"]["name"] for args, payload in client.calls if args[0] == "apply" and "--dry-run=server" not in args
                         for obj in yaml.safe_load_all(payload) if obj["kind"] == "DaemonSet"]
                self.assertEqual(peers, ["platform-entry-edge-b"] if fail else ["platform-entry-edge-b", "platform-entry-edge-a"])
                proof = json.loads((root / "private" / "proof" / "edge-proof.json").read_text())
                self.assertFalse(proof["vip_failover_verified"])
                if fail:
                    self.assertEqual(proof["failed_at"], "before-peer-apply")
                    self.assertEqual(proof["write_outcome"], "unverified")

    def test_ca_or_node_identity_drift_blocks_before_writes(self):
        for fault in ("ca", "node"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plan, bundle, digest, gateway = package(root)
                client = Client(plan, gateway_plan())
                if fault == "ca": client.bad_ca = True
                else: client.objects["node/edge-a"]["metadata"]["uid"] = "changed"
                with mock.patch.object(deploy, "ROOT", root), mock.patch.object(deploy, "Client", return_value=client), self.assertRaises(ValueError):
                    deploy.operate(bundle, digest, gateway, apply=True, output=root / "private" / "proof")
                self.assertFalse(any(args[0] == "apply" for args, _ in client.calls))

    def test_gitlab_ssh_requires_matching_live_service_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ssh = {"hostname": "ssh.example.com", "port": 2222, "node_port": 30222}
            plan, bundle, digest, gateway = package(root, gitlab_ssh=ssh)
            client = Client(plan, gateway_plan())
            service = {"metadata": {}, "spec": {
                "type": "NodePort", "externalTrafficPolicy": "Cluster",
                "selector": {"app": "gitlab-shell", "release": "gitlab"},
                "ports": [{"name": "ssh", "protocol": "TCP", "port": 2222,
                           "nodePort": 30223, "targetPort": 2222}],
            }}
            client.objects["service/gitlab-gitlab-shell"] = service
            with mock.patch.object(deploy, "ROOT", root), mock.patch.object(deploy, "Client", return_value=client):
                with self.assertRaisesRegex(ValueError, "same edge plan"):
                    deploy.operate(bundle, digest, gateway, apply=True, output=root / "private" / "proof")
                self.assertFalse(any(args[0] == "apply" for args, _ in client.calls))
                service["spec"]["ports"][0]["nodePort"] = 30222
                self.assertTrue(deploy.operate(bundle, digest, gateway, apply=True,
                                               output=root / "private" / "verified-proof")["pair_ready"])

    def test_host_start_guard_rejects_incorrect_interface_and_identity_before_exec(self):
        with tempfile.TemporaryDirectory() as directory:
            _, bundle, digest, _ = package(Path(directory))
            with mock.patch.object(starter.sys, "platform", "linux"), mock.patch.object(starter, "interface_address", return_value="192.0.2.99"), \
                 mock.patch.object(starter.os, "execve") as execute, self.assertRaises(ValueError):
                starter.operate(bundle, digest, "edge-a", "keepalived", start=True)
            execute.assert_not_called()

    def test_host_check_is_nonmutating_and_daemon_identities_cannot_be_swapped(self):
        with tempfile.TemporaryDirectory() as directory:
            _, bundle, digest, _ = package(Path(directory))
            with mock.patch.object(starter, "host_inputs", return_value=Path("/usr/sbin/keepalived")), mock.patch.object(starter, "trusted_paths"), \
                 mock.patch.object(starter, "verify_daemon_version"), mock.patch.object(starter, "run_bounded", return_value=SimpleNamespace(returncode=0)) as check, mock.patch.object(starter.os, "execve") as execute:
                self.assertTrue(starter.operate(bundle, digest, "edge-a", "keepalived", check_host=True)["host_checked"])
                self.assertIn("--config-test", check.call_args.args[0])
                execute.assert_not_called()
                with mock.patch.object(starter.os, "geteuid", return_value=1000, create=True), self.assertRaises(ValueError):
                    starter.operate(bundle, digest, "edge-a", "keepalived", start=True)
                execute.assert_not_called()

    def test_real_keepalived_parser_does_not_change_interface_addresses(self):
        binary = os.environ.get("GATEWAY_EDGE_TEST_KEEPALIVED")
        if os.name != "posix" or not binary: self.skipTest("Set the local Keepalived validation binary for parser-only checks")
        if os.geteuid() != 0: self.skipTest("The root-script security parser fixture requires a root test identity; it never starts VRRP")
        env = {**os.environ}
        if os.environ.get("GATEWAY_EDGE_TEST_LIBS"): env["LD_LIBRARY_PATH"] = os.environ["GATEWAY_EDGE_TEST_LIBS"]
        starter.verify_daemon_version(Path(binary), "keepalived", "2.3.4", env)
        interfaces = [(index, name) for index, name in socket.if_nameindex() if name != "lo"]
        if not interfaces: self.skipTest("Parser fixture needs a Linux network interface")
        interface = interfaces[0][1]
        current = starter.interface_address(interface)
        network = ipaddress.ip_network(current + "/24", strict=False)
        candidates = [str(address) for address in network.hosts() if str(address) != current][:2]
        def addresses():
            result = run_bounded(["/usr/sbin/ip", "-j", "address", "show"], timeout=5, output_max_bytes=256 * 1024, text=True, check=True)
            return sorted((row["ifname"], item["family"], item["local"], item["prefixlen"]) for row in json.loads(result.stdout) for item in row.get("addr_info", []))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, _, _ = fixture(root)
            plan["vip"] = candidates[0] + "/24"
            plan["entry_hosts"][0].update(interface=interface, address=current)
            plan["entry_hosts"][1]["address"] = candidates[1]
            config = root / "keepalived.conf"
            config.write_text(renderer.keepalived_config(plan, plan["entry_hosts"][0], 0))
            before = addresses()
            result = run_bounded([binary, "--config-test", "--use-file", str(config)], env=env, timeout=15, output_max_bytes=256 * 1024, text=True, check=False)
            self.assertEqual(result.returncode, 0, "Keepalived parser rejected the fixture; diagnostic SHA256=" + hashlib.sha256(result.stderr.encode()).hexdigest())
            self.assertEqual(addresses(), before)

    def test_keepalived_refuses_missing_group_reset_capability(self):
        required = (1 << 6) | (1 << 12) | (1 << 13)
        for value in (0, required & ~(1 << 6), required & ~(1 << 12), required & ~(1 << 13)):
            with self.subTest(mask=value), mock.patch.object(starter, "read_bounded_text", return_value="CapEff:\t" + format(value, "x") + "\n"), self.assertRaises(ValueError):
                starter.require_keepalived_capabilities()
        with mock.patch.object(starter, "read_bounded_text", return_value="CapEff:\t" + format(required, "x") + "\n"):
            starter.require_keepalived_capabilities()

    def test_old_startup_behavior_and_reported_version_mismatch_are_rejected(self):
        for value in ("3.2.9", "3.2.19", "3.1.20", "3.3.0", "3.2.20-dev1"):
            self.assertFalse(starter.reviewed_version("haproxy", value))
        for reported in ("HAProxy version 3.2.9", "HAProxy version 3.2.26-dev1", "other binary"):
            with mock.patch.object(starter, "run_bounded", return_value=SimpleNamespace(returncode=0, stdout=reported, stderr="")), self.assertRaises(ValueError):
                starter.verify_daemon_version(Path("/usr/sbin/haproxy"), "haproxy", "3.2.26", {})
        for component, version, output in (("haproxy", "3.2.26", "HAProxy version 3.2.26-local"), ("keepalived", "2.3.4", "Keepalived v2.3.4 (fixture)")):
            with mock.patch.object(starter, "run_bounded", return_value=SimpleNamespace(returncode=0, stdout="", stderr=output)):
                starter.verify_daemon_version(Path("/usr/sbin/" + component), component, version, {})

    def test_optional_docker_compose_parser(self):
        binary = os.environ.get("GATEWAY_EDGE_TEST_DOCKER")
        if not binary: self.skipTest("Set the Docker CLI for Compose configuration validation; no engine is used")
        with tempfile.TemporaryDirectory() as directory:
            _, bundle, _, _ = package(Path(directory), "docker")
            env = {**os.environ, "PLATFORM_EDGE_BUNDLE": str(bundle), "PLATFORM_EDGE_INPUTS": str(bundle)}
            for host in ("edge-a", "edge-b"):
                result = run_bounded([binary, "compose", "-f", str(bundle / ("compose-" + host + ".yaml")), "config", "--quiet"],
                    env=env, text=True, timeout=20, output_max_bytes=256 * 1024, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_optional_native_unit_parser(self):
        binary = os.environ.get("GATEWAY_EDGE_TEST_SYSTEMD_ANALYZE")
        if not binary or os.name != "posix": self.skipTest("Set systemd-analyze for native unit validation; units are not installed or started")
        with tempfile.TemporaryDirectory() as directory:
            _, bundle, _, _ = package(Path(directory), "raw")
            result = run_bounded([binary, "verify", *[str(path) for path in sorted(bundle.glob("*.service"))]],
                text=True, timeout=20, output_max_bytes=256 * 1024, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_package_drift_is_rejected_before_any_cluster_contact(self):
        for filename in ("haproxy.cfg", "edge-workloads.yaml", "gateway_edge_health.py"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                _, bundle, digest, gateway = package(Path(directory))
                with (bundle / filename).open("a", encoding="utf-8") as stream: stream.write("\n# changed\n")
                with mock.patch.object(deploy, "Client") as client, self.assertRaises(ValueError):
                    deploy.operate(bundle, digest, gateway)
                client.assert_not_called()

    def test_local_health_gate_fails_closed_for_nonready_status_and_wrong_path(self):
        connection = mock.MagicMock()
        response = connection.getresponse.return_value
        response.status = 503
        with mock.patch.object(health.http.client, "HTTPConnection", return_value=connection) as factory:
            self.assertFalse(health.healthy(8404, "a" * 64))
            response.status = 200
            response.read.side_effect = [b"unavailable", b""]
            self.assertFalse(health.healthy(8404, "a" * 64))
            response.read.side_effect = [b"ready-" + b"a" * 64, b""]
            self.assertTrue(health.healthy(8404, "a" * 64))
            response.read.side_effect = [b"ready-" + b"a" * 64, b""]
            self.assertFalse(health.healthy(8404, "b" * 64))
            response.read.side_effect = [b"x" * 8193]
            self.assertFalse(health.healthy(8404, "a" * 64))
            factory.assert_called_with("127.0.0.1", 8404, timeout=1)
        connection.request.assert_called_with("GET", "/ready", headers={"Connection": "close"})

    def test_real_daemon_configuration_and_tls_failover_fixture(self):
        binary = os.environ.get("GATEWAY_EDGE_TEST_HAPROXY")
        if os.name != "posix" or not binary: self.skipTest("Set the local Linux HAProxy validation binary for the loopback TLS fixture")
        env = {**os.environ}
        if os.environ.get("GATEWAY_EDGE_TEST_LIBS"): env["LD_LIBRARY_PATH"] = os.environ["GATEWAY_EDGE_TEST_LIBS"]
        starter.verify_daemon_version(Path(binary), "haproxy", "3.2.26", env)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, selected, _ = fixture(root)
            route = gerrit_route()
            passthrough_host = route["hostname"]
            selected["plan"]["routes"].append(route)
            plan["routes"].append({"hostname": passthrough_host, "health_path": "/healthz", "health_status": 200})
            plan["minimum_ready_routes"] = len(plan["routes"])
            run_bounded(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=forge.example.com", "-addext",
                "subjectAltName=DNS:forge.example.com,DNS:rpc.example.com,DNS:" + passthrough_host, "-addext",
                "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign,cRLSign",
                "-keyout", str(root / "key"), "-out", str(root / "ca.crt")], timeout=20, output_max_bytes=256 * 1024, check=True)
            run_bounded(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=wrong.example.com", "-keyout", str(root / "wrong.key"),
                "-out", str(root / "wrong.csr")], timeout=20, output_max_bytes=256 * 1024, check=True)
            (root / "wrong.ext").write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
                "subjectAltName=DNS:wrong.example.com\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n")
            run_bounded(["openssl", "x509", "-req", "-in", str(root / "wrong.csr"), "-CA", str(root / "ca.crt"), "-CAkey", str(root / "key"),
                "-set_serial", "2", "-days", "1", "-extfile", str(root / "wrong.ext"), "-out", str(root / "wrong.crt")], timeout=20, output_max_bytes=256 * 1024, check=True)
            run_bounded(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=forge.example.com", "-addext",
                "subjectAltName=DNS:forge.example.com,DNS:rpc.example.com", "-keyout", str(root / "untrusted.key"), "-out", str(root / "untrusted.crt")], timeout=20, output_max_bytes=256 * 1024, check=True)
            active = {"a": True, "b": True, "c": True, "d": True}
            class Backend(BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def handle(self):
                    try: super().handle()
                    except (BrokenPipeError, ConnectionResetError, ssl.SSLError): pass
                def do_GET(self):
                    body = self.server.identity.encode()
                    self.send_response(200 if self.path != "/healthz" or active[self.server.identity] else 503)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            class TLSBackend(ThreadingHTTPServer):
                def get_request(self):
                    client, address_ = self.socket.accept()
                    client.settimeout(3)
                    try:
                        return self.tls_context.wrap_socket(client, server_side=True), address_
                    except BaseException:
                        client.close()
                        raise
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(root / "ca.crt", root / "key")
            wrong_host = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            wrong_host.load_cert_chain(root / "wrong.crt", root / "wrong.key")
            untrusted = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            untrusted.load_cert_chain(root / "untrusted.crt", root / "untrusted.key")
            servers, threads = [], []
            ssh_servers = []
            try:
                port = 0
                for identity_, ip, tls_context in (("a", "127.0.0.2", context), ("b", "127.0.0.3", context), ("c", "127.0.0.4", wrong_host), ("d", "127.0.0.5", untrusted)):
                    server = TLSBackend((ip, port), Backend)
                    port = server.server_port
                    server.identity = identity_
                    server.tls_context = tls_context
                    servers.append(server)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    threads.append(thread); thread.start()
                # This certificate is valid under the selected CA for its own
                # hostname. Excluding it from the forge backend therefore
                # exercises hostname checking rather than a broken fixture CA.
                validation = ssl.create_default_context(cafile=str(root / "ca.crt"))
                with socket.create_connection(("127.0.0.4", port), timeout=3) as raw:
                    with validation.wrap_socket(raw, server_hostname="wrong.example.com"): pass
                def free_port():
                    with socket.socket() as sock: sock.bind(("127.0.0.1", 0)); return sock.getsockname()[1]
                selected["plan"]["https_port"] = port
                passthrough_port = 0
                for identity_, ip in (("tls-a", "127.0.0.2"), ("tls-b", "127.0.0.3")):
                    server = TLSBackend((ip, passthrough_port), Backend)
                    passthrough_port = server.server_port
                    server.identity = identity_
                    server.tls_context = context
                    active[identity_] = True
                    servers.append(server)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    threads.append(thread); thread.start()
                selected["plan"]["tls_passthrough_port"] = passthrough_port
                plan.update(https_port=free_port(), health_port=free_port(), client_cidrs=["127.0.0.0/8"])
                class SSHBanner(BaseRequestHandler):
                    def handle(self):
                        self.request.sendall(self.server.banner)

                ssh_node_port = 0
                for ip, banner in (("127.0.0.2", b"SSH-2.0-fixture-a\r\n"),
                                   ("127.0.0.3", b"SSH-2.0-fixture-b\r\n"),
                                   ("127.0.0.4", b"HTTP/1.1 200 OK\r\n")):
                    server = ThreadingTCPServer((ip, ssh_node_port), SSHBanner)
                    ssh_node_port = server.server_address[1]
                    server.banner = banner
                    ssh_servers.append(server)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    threads.append(thread)
                    thread.start()
                plan["gitlab_ssh"] = {"hostname": "ssh.example.com", "port": free_port(), "node_port": ssh_node_port}
                for row, ip in zip(plan["backends"], ("127.0.0.2", "127.0.0.3")): row["address"] = ip
                plan["backends"].extend({"node_name": "negative-" + label, "node_uid": "fixture-only", "address": ip}
                    for label, ip in (("hostname", "127.0.0.4"), ("trust", "127.0.0.5")))
                config = renderer.haproxy_config(plan, selected["plan"]).replace(renderer.CA, str(root / "ca.crt")).replace("bind :", "bind 127.0.0.1:")
                config = config.replace("    balance roundrobin", "    balance roundrobin\n    option log-health-checks")
                config = config.replace("global\n", "global\n    stats socket " + str(root / "stats.sock") + " level user\n", 1)
                path = root / "haproxy.cfg"; path.write_text(config)
                checked = run_bounded([binary, "-c", "-f", str(path)], env=env, text=True, timeout=10, output_max_bytes=256 * 1024, check=False)
                self.assertEqual(checked.returncode, 0, checked.stderr)
                with tempfile.TemporaryFile() as diagnostics:
                    daemon_args = [binary, "-W", "-db", "-f", str(path)]
                    process = subprocess.Popen(daemon_args, env=env, stdout=diagnostics, stderr=diagnostics)
                    try:
                        deadline = time.monotonic() + 20
                        identity = renderer.edge_identity(plan)
                        while not health.healthy(plan["health_port"], identity) and time.monotonic() < deadline: time.sleep(0.2)
                        self.assertTrue(health.healthy(plan["health_port"], identity))
                        hook = root / "gateway_edge_health.py"
                        hook.write_text(renderer.health_program(), encoding="utf-8")
                        check_hook = [sys.executable, "-I", "-B", str(hook), "--port", str(plan["health_port"]), "--identity"]
                        self.assertEqual(run_bounded([*check_hook, identity], timeout=5, output_max_bytes=16384).returncode, 0)
                        self.assertEqual(run_bounded([*check_hook, "0" * 64], timeout=5, output_max_bytes=16384).returncode, 1)
                        def ssh_banner():
                            with socket.create_connection(("127.0.0.1", plan["gitlab_ssh"]["port"]), timeout=3) as raw:
                                with raw.makefile("rb") as stream:
                                    return stream.readline(256)
                        expected_banners = {b"SSH-2.0-fixture-a\r\n", b"SSH-2.0-fixture-b\r\n"}
                        seen = set()
                        deadline = time.monotonic() + 10
                        while seen != expected_banners and time.monotonic() < deadline:
                            seen.add(ssh_banner())
                            self.assertLessEqual(seen, expected_banners)
                            time.sleep(0.2)
                        self.assertEqual(seen, expected_banners)
                        ssh_servers[0].shutdown()
                        ssh_servers[0].server_close()
                        time.sleep(5)
                        self.assertEqual({ssh_banner() for _ in range(4)}, {b"SSH-2.0-fixture-b\r\n"})
                        self.assertTrue(health.healthy(plan["health_port"], identity))
                        # Native systemd observes the master PID. Verify that
                        # production master/worker mode preserves its identity.
                        self.assertEqual((Path("/proc") / str(process.pid) / "cmdline").read_bytes().rstrip(b"\0").split(b"\0"), [part.encode() for part in daemon_args])
                        with socket.socket(socket.AF_UNIX) as stats:
                            stats.settimeout(3)
                            stats.connect(str(root / "stats.sock")); stats.sendall(b"show stat\n")
                            chunks = []
                            while chunk := stats.recv(8192): chunks.append(chunk)
                            rows = list(csv.DictReader(b"".join(chunks).decode().lstrip("# ").splitlines()))
                            snapshot = [{k: r.get(k) for k in ("pxname", "svname", "status", "check_status", "check_code", "check_health")} for r in rows if r.get("svname", "").startswith("node")]
                        client_context = ssl.create_default_context(cafile=str(root / "ca.crt"))
                        def request(host="forge.example.com"):
                            with socket.create_connection(("127.0.0.1", plan["https_port"]), timeout=3) as raw:
                                with client_context.wrap_socket(raw, server_hostname=host) as tls:
                                    tls.sendall(f"GET /which HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode("ascii"))
                                    response = http.client.HTTPResponse(tls); response.begin(); return response.read(1024).decode()
                        try:
                            self.assertEqual({request() for _ in range(6)}, {"a", "b"})
                            self.assertEqual({request(passthrough_host) for _ in range(6)}, {"tls-a", "tls-b"})
                        except (ssl.SSLError, AssertionError) as error:
                            diagnostics.seek(0)
                            raise AssertionError("Loopback-only TLS fixture states: " + json.dumps(snapshot) + "\n" + diagnostics.read(256 * 1024).decode(errors="replace")) from error
                        client_context.check_hostname = False
                        with self.assertRaises((OSError, ssl.SSLError)): request("unknown.example.com")
                        client_context.check_hostname = True
                        active["a"] = False
                        time.sleep(5)
                        self.assertEqual({request() for _ in range(4)}, {"b"})
                        ssh_servers[1].shutdown()
                        ssh_servers[1].server_close()
                        deadline = time.monotonic() + 12
                        while health.healthy(plan["health_port"], identity) and time.monotonic() < deadline: time.sleep(0.2)
                        self.assertFalse(health.healthy(plan["health_port"], identity))
                        self.assertEqual(request(), "b")
                        active["b"] = False
                        deadline = time.monotonic() + 12
                        while health.healthy(plan["health_port"], identity) and time.monotonic() < deadline: time.sleep(0.2)
                        self.assertFalse(health.healthy(plan["health_port"], identity))
                    finally:
                        process.terminate()
                        try: process.wait(timeout=5)
                        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
            finally:
                for server in ssh_servers: server.shutdown(); server.server_close()
                for server in servers: server.shutdown(); server.server_close()
                for thread in threads: thread.join(timeout=3)


if __name__ == "__main__": unittest.main()
