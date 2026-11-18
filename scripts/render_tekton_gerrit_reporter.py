#!/usr/bin/env python3
"""Prepare a separate Gerrit result reporter without changing submission scope."""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import write_bundle
import render_tekton_forge_bridge as common
import render_tekton_gerrit_bridge as source_renderer
from strict_json import loads_strict_json
from strict_yaml import loads_strict_yaml_all
import tekton_gerrit_bridge as bridge
import tekton_gerrit_reporter as reporter


ROOT = Path(__file__).resolve().parents[1]


def render(bundle, plan):
    contract, _ = bridge.load_bundle(bundle, plan.get("source_contract_sha256"))
    reporter.validate(plan, contract)
    original, runtime = contract["plan"], contract["runtime"]
    files = {k: read_bounded_text(bundle / k, encoding="utf-8") for k in (*bridge.programs(original), "run-template.json", "gerrit-event-contract.json")}
    files.update({k: read_bounded_text(ROOT / "scripts" / k, encoding="utf-8") for k in reporter.EXTRA_PROGRAMS})
    selected = {"schema_version": 1, "plan": plan, "files_sha256": {k: hashlib.sha256(v.encode()).hexdigest() for k, v in files.items()}}
    files["gerrit-report-contract.json"] = bridge.common.canonical(selected).decode() + "\n"
    digest = hashlib.sha256(files["gerrit-report-contract.json"].encode()).hexdigest()
    effective = {**original, "reporting": {"account": plan["account"], "secret": plan["secret"]}}
    files["reporter-rbac.yaml"] = yaml.safe_dump_all(common.reporter_rbac(effective), sort_keys=False)
    base = "/opt/platform/gerrit-tekton-reporter"
    command = ["python3", "-B", base + "/tekton_gerrit_reporter.py", "--bundle-dir", base, "--contract-sha256", digest, "--watch"]
    artifacts = source_renderer.render(original, runtime)
    if artifacts["gerrit-event-contract.json"] != files["gerrit-event-contract.json"]: raise ValueError("Original submission package no longer regenerates exactly")
    environment = {original["management_target"]["kubeconfig_env"]: str(reporter.INPUTS / "kubeconfig")}
    name = original["name"] + "-reporter"
    if runtime in {"rke2", "k3s", "k8s"}:
        objects = loads_strict_yaml_all(artifacts["bridge-workload.yaml"])
        configuration, deployment, budget = copy.deepcopy(objects)
        config_name = name + "-" + digest[:12]
        configuration["metadata"]["name"] = config_name
        configuration["data"] = {k: v for k, v in files.items() if k in {*selected["files_sha256"], "gerrit-report-contract.json"}}
        labels = {"app.kubernetes.io/name": name}
        deployment["metadata"]["name"] = name
        deployment["spec"]["selector"]["matchLabels"] = labels.copy()
        pod = deployment["spec"]["template"]
        pod["metadata"]["labels"] = labels.copy()
        spec = pod["spec"]; spec["serviceAccountName"] = plan["account"]
        spec["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]["labelSelector"]["matchLabels"] = labels
        container = spec["containers"][0]
        container.update(name="reporter", command=command, env=[{"name": k, "value": v} for k, v in environment.items()])
        next(v for v in container["volumeMounts"] if v["name"] == "program")["mountPath"] = base
        next(v for v in container["volumeMounts"] if v["name"] == "inputs")["mountPath"] = str(reporter.INPUTS)
        next(v for v in spec["volumes"] if v["name"] == "program")["configMap"]["name"] = config_name
        next(v for v in spec["volumes"] if v["name"] == "inputs")["secret"]["secretName"] = plan["secret"]
        budget["metadata"]["name"] = name; budget["spec"]["selector"]["matchLabels"] = labels
        files["reporter-workload.yaml"] = yaml.safe_dump_all([configuration, deployment, budget], sort_keys=False)
    elif runtime == "docker":
        compose, = loads_strict_yaml_all(artifacts["compose.yaml"])
        service = next(iter(compose["services"].values()))
        service.update(command=command, environment=environment)
        service["volumes"][0].update(source="${PLATFORM_GERRIT_REPORTER_BUNDLE:?Set the reviewed reporter package}", target=base)
        service["volumes"][1].update(source="${PLATFORM_GERRIT_REPORTER_INPUTS:?Set the separate reporter inputs}", target=str(reporter.INPUTS))
        files["compose.yaml"] = yaml.safe_dump({"services": {name: service}}, sort_keys=False)
    else:
        unit = artifacts["platform-gerrit-tekton.service"]
        unit = unit.replace("Current Gerrit patch sets to fixed Tekton jobs", "Verified Tekton result votes to original Gerrit patch sets").replace("platform-ci-bridge", "platform-ci-reporter")
        unit = unit.replace("/opt/platform/gerrit-tekton", base).replace(str(bridge.INPUTS), str(reporter.INPUTS)).replace("tekton_gerrit_bridge.py", "tekton_gerrit_reporter.py")
        unit = unit.replace(plan["source_contract_sha256"], digest)
        files["platform-gerrit-tekton-reporter.service"] = unit
    files["reporter-status.json"] = json.dumps({"prepared": True, "runtime": runtime, "contract_sha256": digest,
        "source_contract_sha256": plan["source_contract_sha256"], "separate_reporter_identity": True, "result_vote_path_implemented": True,
        **{k: False for k in bridge.FALSE_FLAGS}}, sort_keys=True, indent=2) + "\n"
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True); parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        write_bundle(render(args.bundle_dir, plan), args.output_dir, private_root=ROOT / "private")
        print(json.dumps({"prepared": True, **{k: False for k in bridge.FALSE_FLAGS}}, sort_keys=True)); return 0
    except Exception:
        print("Gerrit reporter preparation failed; private inputs and details suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
