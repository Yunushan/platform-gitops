#!/usr/bin/env python3
"""Test opt-in executor manifests without deploying or relaxing host policy."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

import render_zuul_config as config
import render_zuul_services as services
import zuul_runtime_preflight as preflight
from test_zuul_config import fixture, environment, provision


def executor_plan():
    plan = fixture()
    plan["executor"] = {"enabled": True,
        "image": "registry.example.com/platform/zuul-executor@sha256:" + "f" * 64,
        "replicas": 2, "kubernetes_seccomp": "runtime-default", "kubernetes_apparmor": "runtime-default",
        "docker_seccomp": "runtime-default", "docker_apparmor": "runtime-default"}
    return plan


class ZuulExecutorTests(unittest.TestCase):
    def test_opt_in_preserves_existing_core_only_behavior(self):
        for runtime in ("rke2", "k3s", "k8s", "docker"):
            files = services.render(fixture(), runtime)
            self.assertFalse(any("executors" in name for name in files))
            self.assertFalse(json.loads(files["render-status.json"])["container_executor_selected"])
        plan = fixture()
        plan["executor"] = {"enabled": False}
        self.assertNotIn("zuul-executors-compose.yaml", services.render(plan, "docker"))

    def test_kubernetes_requires_non_control_plane_dedicated_workers(self):
        for runtime in ("rke2", "k3s", "k8s"):
            objects = list(yaml.safe_load_all(services.render(executor_plan(), runtime)["zuul-executors.yaml"]))
            self.assertEqual([obj["kind"] for obj in objects], ["ConfigMap", "Deployment", "PodDisruptionBudget", "NetworkPolicy"])
            deployment = objects[1]["spec"]
            self.assertEqual(deployment["replicas"], 2)
            pod = deployment["template"]["spec"]
            self.assertEqual(pod["nodeSelector"], {"platform.gitops/ci-executor": "true"})
            expressions = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"]
            self.assertEqual({e["key"] for e in expressions}, {"node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master"})
            self.assertTrue(all(e["operator"] == "DoesNotExist" for e in expressions))
            self.assertIn("requiredDuringSchedulingIgnoredDuringExecution", pod["affinity"]["podAntiAffinity"])
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertNotIn("hostNetwork", pod)
            self.assertNotIn("hostPID", pod)
            ingress = objects[3]["spec"]
            self.assertEqual(ingress["policyTypes"], ["Ingress"])
            peer = ingress["ingress"][0]["from"][0]
            self.assertEqual(peer["podSelector"]["matchLabels"]["app.kubernetes.io/component"], "web")
            self.assertNotIn("namespaceSelector", peer)
            self.assertEqual(ingress["ingress"][0]["ports"], [{"protocol": "TCP", "port": 7900}])

    def test_executor_uses_guard_and_pod_address_for_log_streaming(self):
        objects = list(yaml.safe_load_all(services.render(executor_plan(), "k8s")["zuul-executors.yaml"]))
        pod = objects[1]["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertEqual(container["command"], services.command("executor", "11.0.0"))
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        advertised = next(e for e in container["env"] if e["name"] == "ZUUL_EXECUTOR_HOSTNAME")
        self.assertEqual(advertised["valueFrom"]["fieldRef"]["fieldPath"], "status.podIP")
        private = next(v for v in pod["volumes"] if v["name"] == "private")["secret"]
        self.assertIn("build-ssh.key", {item["key"] for item in private["items"]})
        self.assertFalse(any(v.get("hostPath") for v in pod["volumes"]))

    def test_approved_local_profiles_are_references_not_host_mutations(self):
        plan = executor_plan()
        revision = "a" * 64
        plan["executor"].update({"kubernetes_seccomp": "zuul/zuul-executor-" + revision + ".json",
                                 "kubernetes_apparmor": "zuul-executor-" + revision,
                                 "docker_seccomp": "/etc/docker/seccomp/zuul-executor-" + revision + ".json",
                                 "docker_apparmor": "zuul-executor-" + revision})
        objects = list(yaml.safe_load_all(services.render(plan, "k8s")["zuul-executors.yaml"]))
        security = objects[1]["spec"]["template"]["spec"]["containers"][0]["securityContext"]
        self.assertEqual(security["seccompProfile"], {"type": "Localhost", "localhostProfile": plan["executor"]["kubernetes_seccomp"]})
        self.assertEqual(security["appArmorProfile"]["type"], "Localhost")
        compose = yaml.safe_load(services.render(plan, "docker")["zuul-executors-compose.yaml"])
        self.assertIn("seccomp=" + plan["executor"]["docker_seccomp"], compose["services"]["zuul-executor-1"]["security_opt"])

    def test_unconfined_profiles_privilege_flags_and_invalid_counts_rejected(self):
        for key in ("kubernetes_seccomp", "kubernetes_apparmor", "docker_seccomp", "docker_apparmor"):
            for value in ("unconfined", "Unconfined", "../escape", ""):
                plan = executor_plan()
                plan["executor"][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    config.validate(plan)
        for count in (True, 0, 1, 17):
            plan = executor_plan()
            plan["executor"]["replicas"] = count
            with self.assertRaises(ValueError):
                config.validate(plan)
        plan = executor_plan()
        plan["executor"]["privileged"] = True
        with self.assertRaises(ValueError):
            config.validate(plan)

    def test_docker_executors_have_separate_state_and_no_public_ports(self):
        files = services.render(executor_plan(), "docker")
        executors = yaml.safe_load(files["zuul-executors-compose.yaml"])["services"]
        self.assertEqual(len(executors), 2)
        sources = set()
        for name, service in executors.items():
            self.assertNotIn("ports", service)
            self.assertEqual(service["expose"], ["7900"])
            self.assertEqual(service["user"], "1000:1000")
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertNotIn("privileged", service)
            self.assertNotIn("pid", service)
            self.assertNotIn("network_mode", service)
            state = next(v for v in service["volumes"] if v["target"] == "/var/lib/zuul")
            sources.add(state["source"])
            self.assertIn(name, service["environment"]["ZUUL_EXECUTOR_HOSTNAME"])
        self.assertEqual(len(sources), 2)

    def test_executor_log_endpoint_must_be_explicit_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            provision(root)
            env = environment(root)
            for address in ("executor.example.com", "127.0.0.1", "2001:db8::1"):
                env["ZUUL_EXECUTOR_HOSTNAME"] = address
                self.assertTrue(preflight.verify_inputs(env, "executor", config_root=root)["local_inputs_verified"])
            for address in ("", "https://executor.example.com/", "executor.example.com:7900", "x\nother", 123):
                env["ZUUL_EXECUTOR_HOSTNAME"] = address
                with self.assertRaises(ValueError):
                    preflight.verify_inputs(env, "executor", config_root=root)

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI unavailable")
    def test_real_compose_merge_parser_accepts_executor_opt_in(self):
        version = subprocess.run(["docker", "compose", "version", "--short"], capture_output=True, timeout=20, check=False)
        if version.returncode:
            self.skipTest("Compose plugin unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = services.render(executor_plan(), "docker")
            core, executor = root / "core.yaml", root / "executor.yaml"
            core.write_text(files["zuul-compose.yaml"], encoding="utf-8")
            executor.write_text(files["zuul-executors-compose.yaml"], encoding="utf-8")
            env = {**os.environ, **{key: "fixture-only" for key in services.SECRET_KEYS}}
            result = subprocess.run(["docker", "compose", "-f", str(core), "-f", str(executor), "config", "--quiet"],
                                    env=env, capture_output=True, timeout=30, check=False)
            self.assertEqual(result.returncode, 0, "Compose rejected the combined synthetic Zuul stack")


if __name__ == "__main__":
    unittest.main()
