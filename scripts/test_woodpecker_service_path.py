#!/usr/bin/env python3
"""Exercise the actual policy-aware Woodpecker probe scripts and health verdict."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

from strict_yaml import loads_strict_yaml_all
from test_bash_support import bash_executable, bash_path, run_bash

ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = ROOT / "ansible/playbooks/repair-platform-service-path-consumers.yml"


def tasks(path: Path) -> list[dict]:
    return loads_strict_yaml_all(path.read_text(encoding="utf-8"))[0][0]["tasks"]


def check_targets(shell: str) -> None:
    code = shell.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    service = {"spec": {"clusterIP": "192.0.2.1", "ports": [{"port": 9000}]}}
    endpoints = {"items": [{"ports": [{"name": "grpc", "port": 9000}], "endpoints": [
        {"addresses": ["198.51.100.1"], "conditions": {"ready": True}},
        {"addresses": ["203.0.113.1"]},
        {"addresses": ["203.0.113.2"], "conditions": {"ready": False}},
        {"addresses": ["203.0.113.3"], "conditions": {"terminating": True}},
        {"addresses": ["2001:db8::1"]},
    ]}]}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        def run(document: dict, service_doc: dict = service):
            (root / "service.json").write_text(json.dumps(service_doc), encoding="utf-8")
            (root / "endpoints.json").write_text(json.dumps(document), encoding="utf-8")
            return subprocess.run([sys.executable, "-c", code, directory], capture_output=True, text=True, timeout=10)

        result = run(endpoints)
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == [
            "http://woodpecker-server.woodpecker.svc.cluster.local:9000/",
            "http://192.0.2.1:9000/", "http://198.51.100.1:9000/",
            "http://203.0.113.1:9000/", "http://[2001:db8::1]:9000/",
        ], result.stdout
        assert run({"items": []}).returncode != 0
        invalid = copy.deepcopy(endpoints)
        invalid["items"][0]["endpoints"][0]["addresses"] = ["not-an-ip; echo unsafe"]
        assert run(invalid).returncode != 0
        assert run(endpoints, {"spec": {"clusterIP": "None", "ports": []}}).returncode != 0


def check_pod_script(shell: str) -> None:
    manifest = shell.split("apiVersion: batch/v1\n", 1)[1].split("\nYAML\n", 1)[0]
    job = loads_strict_yaml_all("apiVersion: batch/v1\n" + manifest)[0]
    spec = job["spec"]["template"]["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert not spec.get("hostNetwork", False)
    script = spec["containers"][0]["args"][0].replace("\\$", "$")
    assert "NAMESPACE=woodpecker" in shell
    assert "--noproxy '*'" in script
    _, flavor = bash_executable()
    with tempfile.TemporaryDirectory(prefix=".woodpecker-probe-", dir=ROOT) as directory:
        root = Path(directory)
        path = root / "probe.sh"
        path.write_text(script, encoding="utf-8", newline="\n")
        # Exported function mocks curl in the unchanged generated pod script.
        fake = """curl() {
            target="${!#}"
            if [ "${FAIL_TARGET:-}" = "$target" ]; then printf 0; return 28; fi
            printf 1
            return 52
        }
        export -f curl
        export CONNECT_TIMEOUT=1
        export PROBE_URLS='http://service:9000/ http://198.51.100.1:9000/ http://203.0.113.1:9000/'
        """
        command = "bash " + shlex.quote(bash_path(path, flavor))
        result = run_bash(fake + "\nunset FAIL_TARGET\n" + command)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.count("result=ok ") == 9, result.stdout
        for target in ("http://service:9000/", "http://203.0.113.1:9000/"):
            result = run_bash(fake + f"\nexport FAIL_TARGET={shlex.quote(target)}\n" + command)
            assert result.returncode != 0, result.stdout
            assert result.stdout.count("result=fail ") == 3, result.stdout
        fallback = """
        command() { if [ "$2" = curl ]; then return 1; fi; builtin command "$@"; }
        nc() { [ "${FAIL_TARGET:-}" != "http://${4}:9000/" ]; }
        export -f command nc
        """
        result = run_bash(fake + fallback + "\nunset FAIL_TARGET\n" + command)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.count("tool=nc") == 9, result.stdout
        result = run_bash(fake + fallback + "\nexport FAIL_TARGET=http://203.0.113.1:9000/\n" + command)
        assert result.returncode != 0, result.stdout
        assert result.stdout.count("result=fail ") == 3, result.stdout


def check_verdicts(repair_tasks: list[dict]) -> None:
    host = next(t for t in repair_tasks if t["name"] == "Report Woodpecker node-host probe limitations")
    assert "ansible.builtin.debug" in host and "ansible.builtin.fail" not in host
    final = next(t for t in repair_tasks if t["name"] == "Stop after Woodpecker consumer refresh failure")
    assert "platform_woodpecker_grpc_pod_probe" in final["when"][-1]
    # Require all three failures to remain unconditional repair gates. Keep the
    # CI test runnable with the repository's pinned PyYAML-only dependencies.
    gates = (
        "platform_woodpecker_agent_rollout", "platform_woodpecker_grpc_pod_probe",
        "platform_woodpecker_argocd_reconcile_after_consumer_refresh",
    )
    assert " ".join(final["when"][-1].split()) == " or ".join(
        f"(({name} | default({{}})).rc | default(0) | int != 0)" for name in gates
    )

    health = tasks(ROOT / "ansible/playbooks/verify-platform-app-health.yml")
    expression = next(t for t in health if t["name"] == "Record per-node app health verdict")["ansible.builtin.set_fact"]["platform_app_health_failed"]
    host_gate = next(line.strip() for line in expression.splitlines()
                     if "platform_app_health_service_probe" in line)
    assert host_gate == (
        "or ((platform_app_health_node_service_strict_effective | bool) and "
        "(((platform_app_health_service_probe | default({'rc': 1})).rc | int) != 0))"
    )
    assert (
        "or (((hostvars[rke2_first_server].platform_app_health_pod_service_probe "
        "| default({'rc': 1})).rc | int) != 0)"
    ) in expression


def main() -> int:
    repair_tasks = tasks(PLAYBOOK)
    shell = next(t["ansible.builtin.shell"] for t in repair_tasks
                 if t.get("register") == "platform_woodpecker_grpc_pod_probe")
    check_targets(shell)
    check_pod_script(shell)
    check_verdicts(repair_tasks)
    print("Policy-aware Woodpecker service-path regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
