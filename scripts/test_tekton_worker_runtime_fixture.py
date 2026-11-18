#!/usr/bin/env python3
"""Actual pinned Tekton installation/admission/canary on disposable K3s VMs."""

import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from bounded_file import read_bounded_bytes
import deploy_tekton as deploy
import render_tekton as renderer
from kubernetes_management import KubernetesOperationError
from verify_oci_archive import verify_archive
from test_k3s_vm_fixture import PROBE_IMAGE
from test_tekton import fixture as plan_fixture


RELEASE_SHA = "cc7bf83d3e25ac4c6d8069c03a1e3447b435c2ab548aba783528a7d896181b3f"
PYTHON_IMAGE = "docker.io/library/python@sha256:cc55e4ded9c95f6ce2133b3d096db43f214d280c2a8af65d83e246c8fa7d168c"
SHELL_IMAGE = "cgr.dev/chainguard/busybox@sha256:19f02276bf8dbdd62f069b922f10c65262cc34b710eea26ff928129a736be791"


def certificate_clock_diagnostic(context, client):
    """Observe public admission CA validity and guest clocks without keys."""
    try:
        registration = client.get("validatingwebhookconfiguration/validation.webhook.pipeline.tekton.dev")
        bundles = {entry.get("clientConfig", {}).get("caBundle", "") for entry in registration.get("webhooks", [])}
        if len(bundles) != 1 or not next(iter(bundles)): return {"status": "ca_not_available"}
        encoded = next(iter(bundles))
        if len(encoded) > 128 * 1024: raise ValueError("Public CA observation bound")
        content = base64.b64decode(encoded, validate=True)
        path = context["root"] / "public-webhook-observation-ca.pem"
        path.write_bytes(content); path.chmod(0o600)
        output = context["run"](["openssl", "x509", "-in", path, "-noout", "-startdate", "-enddate"], seconds=10).stdout
        dates = dict(line.split("=", 1) for line in output.splitlines())
        parsed = {key: datetime.strptime(value, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc).timestamp() for key, value in dates.items()}
        rows = []
        from test_k3s_vm_fixture import application_workers
        controls = context.get("control_nodes", context["nodes"][:1])
        for node in [*controls, *application_workers(context)]:
            before = time.time()
            value = context["ssh"](node, "python3 -c 'import time; print(time.time())'", seconds=10)
            after = time.time(); clock = float(value.stdout.strip())
            if not math.isfinite(clock): raise ValueError("Guest returned an invalid clock observation")
            rows.append({"node": node["name"], "offset_seconds": round(clock - (before + after) / 2, 3),
                "round_trip_seconds": round(after - before, 3), "before_ca_validity": clock < parsed["notBefore"], "after_ca_validity": clock > parsed["notAfter"]})
        return {"status": "observed", "public_ca_sha256": hashlib.sha256(content).hexdigest(), "guest_clocks": rows}
    except Exception as error:
        return {"status": "observation_failed", "exception_type": type(error).__name__}


def stage_rke2_image(context, node, path, image, digest):
    name, manifest = image.rsplit("@", 1)
    if ":" in name.rsplit("/", 1)[-1]:
        name = name.rsplit(":", 1)[0]
    reference = name + "@" + manifest
    destination = context["upload"](node, path, digest)
    binaries = "sudo /var/lib/rancher/rke2/bin/"
    socket = "/run/k3s/containerd/containerd.sock"
    ssh = context["ssh"]
    # The bundled containerd's --digests importer adds synthetic aliases.
    # CRI may then prefer an alias over the pinned name during CreateContainer.
    ssh(node, binaries + "ctr --address " + socket + " --namespace k8s.io images import " + destination, seconds=120)
    ssh(node, "rm -- " + destination)
    inspect = binaries + "crictl --runtime-endpoint unix://" + socket + " --image-endpoint unix://" + socket + " inspecti "

    def registered():
        observed = ssh(node, inspect + reference, check=False, seconds=10)
        if observed.returncode != 0:
            return False
        status = json.loads(observed.stdout)["status"]
        if status.get("repoDigests") != [reference] or not re.fullmatch(r"sha256:[0-9a-f]{64}", status.get("id", "")):
            return False
        by_id = ssh(node, inspect + status["id"], check=False, seconds=10)
        return by_id.returncode == 0 and json.loads(by_id.stdout)["status"].get("repoDigests") == [reference]

    context["wait"](registered, seconds=30)


def image_archives(release, assets):
    if hashlib.sha256(release).hexdigest() != RELEASE_SHA: raise ValueError("Require the reviewed original Tekton v1.4.0 release")
    images = sorted(set(re.findall(r"ghcr[.]io/tektoncd/pipeline/[a-z0-9-]+:v1[.]4[.]0@sha256:[0-9a-f]{64}", release.decode())))
    if len(images) != 8: raise ValueError("Unexpected original Tekton Linux image inventory")
    archives = [(image, assets / "images" / (image.rsplit("@sha256:", 1)[1] + ".tar")) for image in images]
    archives.append((PYTHON_IMAGE, assets / "python-image/python.tar"))
    return images, archives


def exercise(test, context, *, workload=None):
    from test_k3s_vm_fixture import application_workers
    client, root = context["client"], context["root"]
    runtime = context.get("runtime", "k3s")
    if runtime not in {"k3s", "rke2"}:
        raise ValueError("Use the explicit K3s or RKE2 worker fixture")
    release = read_bounded_bytes(Path(os.environ["TEKTON_VM_TEST_RELEASE"]), max_bytes=16 * 1024 ** 2)
    assets = Path(os.environ["TEKTON_VM_TEST_IMAGES"]).resolve(strict=True)
    images, archives = image_archives(release, assets)
    verified = {image: verify_archive(path, image) for image, path in archives}
    stage = "worker-image-import"
    try:
        for node in application_workers(context):
            for image, path in archives:
                print(json.dumps({"tekton_vm_stage": stage, "worker": node["name"], "image_digest": image.rsplit("@", 1)[1]}), flush=True)
                if runtime == "rke2":
                    stage_rke2_image(context, node, path, image, verified[image])
                else:
                    destination = "/tmp/fixture-image-" + image.rsplit("@sha256:", 1)[1] + ".tar"
                    context["upload"](node, path, destination)
                    context["ssh"](node, "sudo /mnt/fixtures/k3s ctr --address /run/k3s/containerd/containerd.sock --namespace k8s.io images import --digests " + destination, seconds=120)
                    context["ssh"](node, "rm -- " + destination)
        stage = "reviewed-package"
        plan, _ = plan_fixture(); plan.update(bundle_sha256=RELEASE_SHA, management_target=context["target"], webhook_max_replicas=2,
            node_selector={"platform.gitops/ci-worker": "true", "kubernetes.io/os": "linux"}, failure_domain_key="topology.kubernetes.io/zone", image_overrides={SHELL_IMAGE: PROBE_IMAGE})
        plan["canary"].update(image=PYTHON_IMAGE)
        files = renderer.render(plan, release, runtime); bundle = root / "tekton-package"; bundle.mkdir(mode=0o700)
        for name, content in files.items(): (bundle / name).write_text(content, encoding="utf-8", newline="\n")
        digest = json.loads(files["render-status.json"])["contract_sha256"]
        client.call(["create", "-f", "-"], payload=json.dumps({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": plan["canary"]["namespace"], "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}}).encode())
        stage = "controller-admission-installation"
        print(json.dumps({"tekton_vm_stage": stage}), flush=True)
        preview = deploy.operate(bundle, digest, check_server=True)
        test.assertFalse(preview["server_checked"])
        test.assertTrue(preview["namespace_bootstrap_checked"])
        test.assertEqual(preview["namespace_bootstrap_required"], ["tekton-pipelines", "tekton-pipelines-resolvers"])
        for namespace in preview["namespace_bootstrap_required"]:
            test.assertIsNone(client.get("namespace/" + namespace, optional=True))
        installed = deploy.operate(bundle, digest, apply=True)
        test.assertTrue(installed["deployed"]); test.assertFalse(installed["canary_completed"])
        test.assertEqual(json.loads(client.call(["get", "pipelineruns.tekton.dev", "-n", plan["canary"]["namespace"], "-o", "json"]))["items"], [])
        stage = "explicit-pipeline-canary"
        print(json.dumps({"tekton_vm_stage": stage}), flush=True)
        result = deploy.operate(bundle, digest, apply=True, run_canary=True)
        test.assertTrue(result["canary_completed"]); test.assertFalse(result["event_integration_verified"]); test.assertFalse(result["application_ci_verified"])
        runs = json.loads(client.call(["get", "pipelineruns.tekton.dev", "-n", plan["canary"]["namespace"], "-o", "json"]))["items"]
        test.assertEqual(len(runs), 1); deploy.verify_canary(client, plan, runs[0])
        report = {"real_tekton_infrastructure_canary_verified": True, "actual_tekton_controller_admission_verified": True,
            "tekton_worker_runtime": runtime,
            "installation_without_implicit_job_verified": True, "tekton_release_sha256": RELEASE_SHA, "tekton_oci_archives_sha256": verified,
            "tekton_pipeline_run_uid": runs[0]["metadata"]["uid"], "publisher_image_signatures_verified": False,
            "gerrit_event_execution_verified": False, "gerrit_review_feedback_verified": False, "ci_cd_application_handoff_verified": False}
        if workload is not None:
            if not callable(workload): raise ValueError("Require an explicit actual-worker workload callback")
            stage = "actual-application-workload"
            report.update(workload({**context, "helper_images": images + [PROBE_IMAGE]}))
        return report
    except Exception as failure:
        print(json.dumps({"tekton_vm_failure_stage": stage}), flush=True)
        if isinstance(failure, KubernetesOperationError):
            print(json.dumps({"tekton_original_operation_failure": failure.diagnostic}), flush=True)
        print(json.dumps({"tekton_certificate_clock_diagnostic": certificate_clock_diagnostic(context, client)}), flush=True)
        if stage == "controller-admission-installation" and "files" in locals():
            result = context["run"]([*client.prefix, "--request-timeout=30s", "apply", "--server-side", "--field-manager=platform-tekton", "--dry-run=server", "-f", "-"], payload=files["tekton-components.yaml"], check=False, seconds=45)
            error = result.stderr.casefold()
            print(json.dumps({"component_admission_error_signals": {"missing_namespace": bool(re.search(r'namespaces? "[a-z0-9-]+" not found', error)),
                "field_conflict": "conflict" in error, "invalid_resource": "invalid" in error,
                "forbidden": "forbidden" in error, "webhook_failure": "failed calling webhook" in error}}), flush=True)
            result = context["run"]([*client.prefix, "--request-timeout=30s", "apply", "--server-side", "--field-manager=platform-tekton", "--dry-run=server", "-f", "-"],
                payload=files["tekton-canary.yaml"], check=False, seconds=45)
            print(json.dumps({"canary_admission_diagnostic": KubernetesOperationError(result.returncode, result.stderr).diagnostic}), flush=True)
        rows = []
        for namespace in ("tekton-pipelines", "tekton-pipelines-resolvers", "platform-ci-canary"):
            try: pods = json.loads(client.call(["get", "pods", "-n", namespace, "-o", "json"]))["items"]
            except ValueError: continue
            for pod in pods:
                statuses = pod.get("status", {}).get("initContainerStatuses", []) + pod.get("status", {}).get("containerStatuses", [])
                rows.append({"namespace": namespace, "phase": pod.get("status", {}).get("phase"), "waiting_reasons": [x["state"]["waiting"].get("reason") for x in statuses if x.get("state", {}).get("waiting")],
                    "terminated_reasons": [x["state"]["terminated"].get("reason") for x in statuses if x.get("state", {}).get("terminated")]})
        print(json.dumps({"actual_tekton_pod_states": rows}, sort_keys=True), flush=True)
        raise
