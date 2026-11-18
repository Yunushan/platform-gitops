"""Run the Zuul image promoter against native controllers and K3s workers."""

import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
from contextlib import ExitStack
from urllib.parse import quote

from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
from ci_gitops_handoff import sha256
from strict_json import loads_strict_json
from test_gerrit_artifact_promotion_worker_fixture import NAME, NAMESPACE, deployment, retain
from test_zuul_gitops_live_fixture import DEPLOYMENT_BRANCHES, PROJECT_API, ROOT, branch_ref, https_buildset
from test_zuul_static_image_job_fixture import JobPublicationChecks
from test_zuul_web_fixture import web_request


DESTINATIONS = {"argocd": NAMESPACE, "flux": "synthetic-flux-application"}
STATIC_FILE = b"exact-source-v1\n"


def deployment_manifest(kind, base, *, replicas=2):
    document = deployment(base, replicas=replicas)
    document["metadata"]["namespace"] = DESTINATIONS[kind]
    return document


def configure_writer(gerrit, revision):
    api = gerrit["api"]
    writer = api.bootstrap("cd-writer")
    group = api.call("fixture-admin", "/a/groups/zuul-artifact-writers", {}, method="PUT")["id"]
    api.call("fixture-admin", "/a/groups/" + quote(group, safe="") + "/members/" + str(writer["_account_id"]), {}, method="PUT")
    allow = {"rules": {group: {"action": "ALLOW", "force": False}}}
    sources = {"refs/heads/main": {"permissions": {"read": allow}}}
    for branch in DEPLOYMENT_BRANCHES.values():
        sources["refs/heads/" + branch] = {"permissions": {"read": allow, "push": allow}}
        sources["refs/for/refs/heads/" + branch] = {"permissions": {"pushMerge": {"exclusive": True, **allow}}}
    api.call("fixture-admin", PROJECT_API + "/access", {"add": sources})
    baseline = api.call("ci-read", PROJECT_API + "/commits/" + revision)["parents"][0]["commit"]
    for branch in DEPLOYMENT_BRANCHES.values():
        api.call("fixture-admin", branch_ref(branch), {"revision": baseline}, method="PUT")
    return writer, baseline


def install_registry_ca(context, origin, ca):
    if origin != "https://192.0.2.91:8443":
        raise ValueError("Use the original disposable registry")
    path = "/var/lib/fixture-k3s/agent/etc/containerd/certs.d/192.0.2.91:8443"
    hosts = f'server = "{origin}"\n[host."{origin}"]\n  capabilities = ["pull", "resolve"]\n  ca = "{path}/ca.crt"\n'
    for node in context["nodes"][1:]:
        context["ssh"](node, "sudo mkdir -p " + path)
        context["ssh"](node, "sudo tee " + path + "/ca.crt >/dev/null", payload=ca.decode())
        context["ssh"](node, "sudo tee " + path + "/hosts.toml >/dev/null", payload=hosts)


class RolloutChecks(JobPublicationChecks):
    admission = False

    def release_credential(self, context):
        import provision_harbor_robot as robot

        plan = {**self.robot_plan, "robot": {**self.robot_plan["robot"],
                "name": "release-signer", "username": "robot$fixture+release-signer"}}
        directory = context["root"] / "release-robot-private"
        client = robot.Client(plan, password=self.admin_password)
        result = robot.operate(plan, directory, mode="apply", client=client)
        context["test"].assertTrue(result["permissions_verified"])
        credential = robot.load(directory / "credential.json")
        context["test"].assertNotEqual(credential["robot_id"], self.robot_credential["robot_id"])
        context["private_values"].append(credential["secret"])
        return credential

    def application_files(self, registry):
        files = super().application_files(registry)
        base = registry["origin"].removeprefix("https://") + "/" + registry["repository"] + "@" + registry["base_digest"]
        for kind, directory in DEPLOYMENT_BRANCHES.items():
            files[directory + "/workload.json"] = json.dumps(deployment_manifest(kind, base, replicas=3 if self.admission else 2))
        files[DEPLOYMENT_BRANCHES["flux"] + "/kustomization.yaml"] = (
            "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n- workload.json\n")
        return files

    def rollout(self, test, native, gerrit, selection, contract, receipt, ca):
        from test_k3s_vm_fixture import exercise
        registry = contract["plan"]["registry"]
        writer, before = configure_writer(gerrit, selection["revision"])
        base = registry["origin"].removeprefix("https://") + "/" + registry["repository"] + "@" + registry["base_digest"]
        print(json.dumps({"zuul_artifact_rollout_stage": "boot-k3s-workers"}), flush=True)
        credential = (self.release_credential(native)
                      if os.environ.get("ZUUL_SIGNED_ARTIFACT_ROLLOUT_TEST_RUNTIME") == "1" else None)

        def run(context):
            install_registry_ca(context, registry["origin"], ca)
            results = {}
            signers = {}
            with ExitStack() as lifetime:
                if self.admission:
                    from test_kyverno_admission_authority_fixture import AuthorityChecks
                    from test_zuul_admitted_rollout_fixture import ReleaseAdmission

                    authority = AuthorityChecks().install_authority(native, context, credential_name=None)
                for kind in ("argocd", "flux"):
                    print(json.dumps({"zuul_artifact_rollout_stage": kind}), flush=True)
                    signer = None
                    if credential is not None:
                        from test_sigstore_image_runtime_fixture import SignatureChecks

                        signer = (ReleaseAdmission(context, kind, DESTINATIONS[kind], authority, lifetime)
                                  if self.admission else SignatureChecks())
                        signer.robot_credential = credential
                        signer.reader_password = self.reader_password
                        signer.service_clients = self.service_clients
                        signers[kind] = signer
                    results.update(self.controller(test, context, gerrit, selection, contract, receipt, base, writer, before,
                                                   kind, native=native, signer=signer))
                if self.admission:
                    # Verify both bindings with both OCI signatures present and both mirrors live.
                    for signer in signers.values():
                        signer.verify_mirror(test)
                        signer.signed_dryrun(test, "signed-after-both-promotions")
            if self.admission:
                for kind, signer in signers.items():
                    test.assertEqual(len(signer.tuf_processes), 3)
                    test.assertTrue(all(row["original_process_removed"] for row in signer.tuf_processes))
                    results[f"zuul_artifact_{kind}_tuf_sha256"] = retain(f"zuul-artifact-{kind}-tuf.json", signer.private_tuf_proof)
            return results

        report = exercise(test, workload=run, zuul_rollout=True, admission_workers=self.admission,
                          guest_runtime_seconds=1700 if self.admission else 1100)
        test.assertIs(report["temporary_guests_and_scope_removed"], True)
        results = {key: report[key] for key in ("zuul_artifact_argocd_rollout_verified", "zuul_artifact_flux_rollout_verified")}
        for name in ("argocd", "flux"):
            test.assertIs(results[f"zuul_artifact_{name}_rollout_verified"], True)
            if credential is not None:
                field = f"zuul_artifact_{name}_release_signature_verified"
                test.assertIs(report[field], True)
                results[field] = True
            if self.admission:
                field = f"zuul_artifact_{name}_admission_verified"
                test.assertIs(report[field], True)
                results[field] = True
        report_path = Path(os.environ["K3S_VM_TEST_REPORT"])
        results.update(zuul_artifact_rollout_k3s_cleanup_verified=True,
            zuul_artifact_rollout_reports_sha256={"k3s": hashlib.sha256(report_path.read_bytes()).hexdigest()})
        return results

    def controller(self, test, context, gerrit, selection, contract, receipt, base, writer, before, kind, *, native, signer):
        namespace = DESTINATIONS[kind]
        directory = DEPLOYMENT_BRANCHES[kind]
        clone_url = contract["plan"]["source"]["clone_url"]
        state = {}
        api = gerrit["api"]

        def prepare(manager, _target):
            def create(obj):
                return json.loads(manager.call(["create", "-f", "-", "-o", "json"], payload=json.dumps(obj).encode()))

            host = contract["plan"]["registry"]["origin"].removeprefix("https://")
            auth = base64.b64encode(("fixture-reader:" + self.reader_password).encode()).decode()
            config = base64.b64encode(json.dumps({"auths": {host: {"auth": auth}}}).encode()).decode()
            create({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "application-pull", "namespace": namespace},
                    "immutable": True, "type": "kubernetes.io/dockerconfigjson", "data": {".dockerconfigjson": config}})
            original = create(deployment_manifest(kind, base, replicas=len(context["nodes"]) - 1))
            service = create({"apiVersion": "v1", "kind": "Service", "metadata": {"name": NAME, "namespace": namespace},
                              "spec": {"type": "NodePort", "selector": {"app": NAME},
                                       "ports": [{"port": 8080, "targetPort": "http"}]}})
            server = json.loads(manager.call(["config", "view", "--minify", "-o", "json"]))["clusters"][0]["cluster"]["server"]
            state.update(service=service, policy={
                "namespace": namespace, "deployment": NAME, "container": "web", "manifest": directory + "/workload.json",
                "deployment_uid": original["metadata"]["uid"], "expected_image": base, "expected_deployment_revision": before,
                "destination_server": server, "commit_name": "Zuul Artifact Writer", "commit_email": writer["email"]})
            return state

        def promote(reader, application, target, source=None):
            ca = (gerrit["tls"] / "ca.crt").read_bytes()
            token = secrets.token_urlsafe(36)

            def upstream(path):
                return web_request("127.0.0.101", path)

            with (tempfile.TemporaryDirectory(prefix="zuul-artifact-inputs-", dir=gerrit["root"]) as inputs_dir,
                  tempfile.TemporaryDirectory(prefix="zuul-artifact-proof-", dir=ROOT / "private/validation") as proof_dir,
                  https_buildset(gerrit, selection, token, upstream) as origin):
                inputs, proof = Path(inputs_dir), Path(proof_dir)
                config = read_bounded_bytes(Path(os.environ[target["kubeconfig_env"]]), max_bytes=256 * 1024)
                for name, contents in (("token", gerrit["users"]["cd-writer"].encode()), ("ca.crt", ca),
                        ("zuul-token", token.encode()), ("zuul-ca.crt", ca), ("cd-kubeconfig", config)):
                    path = inputs / name
                    path.write_bytes(contents)
                    path.chmod(0o600)
                buildset = {**selection, "zuul_origin": origin, "gerrit_origin": gerrit["origin"],
                            "zuul_ca_sha256": hashlib.sha256(ca).hexdigest()}
                meta = application["metadata"]
                cd = {"kind": kind, "management_target": target, "namespace": meta["namespace"], "name": meta["name"],
                      "uid": meta["uid"], "spec_sha256": sha256(application["spec"]), "path": directory}
                if source is not None:
                    meta = source["metadata"]
                    cd.update(source_namespace=meta["namespace"], source_name=meta["name"],
                              source_uid=meta["uid"], source_spec_sha256=sha256(source["spec"]))
                plan = {"schema_version": 1, "runtime": contract["runtime"], "buildset": buildset,
                        "publisher_contract_sha256": sha256(contract), "artifact_policy": state["policy"],
                        "handoff": {"schema_version": 1, "runtime": contract["runtime"],
                            "bridge_contract_sha256": sha256(buildset), "deployment_branch": directory,
                            "allow_create_branch": False, "allow_prune": False, "git_username": "cd-writer",
                            "timeout_seconds": 150, "cd": cd}}
                script = ROOT / "scripts/zuul_artifact_promotion.py"
                prefix = []
                if signer is not None:
                    import zuul_artifact_promotion as artifact

                    plan["schema_version"] = 2
                    data, evidence = artifact.verify(plan, contract, upstream)
                    annotations = artifact.signature_annotations(plan, contract, data, evidence)
                    signer.signature_setup(native, image=receipt["image"], annotations=annotations,
                                           directory_name="zuul-signature-" + kind)
                    test.assertTrue(signer.signature_original["original_process_removed"])
                    test.assertTrue(signer.signature_binding_negatives)
                    plan["signature_policy"] = {name: signer.signature_plan[name] for name in
                        ("cosign_binary", "registry_ca", "public_key", "trusted_root", "credential")}
                    # The fixture's kubectl sits below a directory owned by
                    # this UID. Give it the verifier inputs and a private journal.
                    uid = 65534
                    test.assertNotIn(uid, signer.signature_uids.values())
                    for name, ref in plan["signature_policy"].items():
                        if name != "cosign_binary":
                            path = inputs / ("signature-" + name)
                            shutil.copyfile(ref["path"], path)
                            path.chmod(0o400)
                            ref = {**ref, "path": str(path)}
                            plan["signature_policy"][name] = ref
                    shutil.copytree(ROOT / "scripts", inputs / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
                    (inputs / "private").mkdir(mode=0o700)
                    os.chown(inputs / "private", uid, uid)
                    script = inputs / "scripts/zuul_artifact_promotion.py"
                    proof = inputs / "private"
                    prefix = ["/usr/bin/setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups", "--no-new-privs", "--"]
                plan_path, contract_path = inputs / "plan.json", inputs / "contract.json"
                for path, obj in ((plan_path, plan), (contract_path, contract)):
                    path.write_text(json.dumps(obj), encoding="utf-8")
                    path.chmod(0o600)
                if signer is not None:
                    for path in (inputs, *(p for p in inputs.iterdir() if p.is_file())):
                        os.chown(path, uid, uid)
                command = [*prefix, sys.executable, "-B", str(script), "--plan", str(plan_path),
                           "--publisher-contract", str(contract_path), "--input-dir", str(inputs)]
                env = {"PATH": str(Path(reader.prefix[0]).parent) + ":/usr/bin:/bin", "HOME": str(inputs), "LC_ALL": "C"}
                if signer is not None:
                    request = run_bounded([*command, "--prepare-signature"], timeout=30, text=True,
                                          env=env, output_max_bytes=128 * 1024, check=False)
                    test.assertEqual(request.returncode, 0)
                    test.assertEqual(loads_strict_json(request.stdout),
                                     {"schema_version": 1, "image": receipt["image"], "annotations": annotations})

                def invoke(mode, name):
                    output = proof / name
                    result = run_bounded([*command, mode, "--output", str(output)], timeout=360 if signer else 245, text=True,
                                         env=env, output_max_bytes=128 * 1024, check=False)
                    journal = loads_strict_json(read_bounded_bytes(output / "handoff-proof.json", max_bytes=65536))
                    test.assertEqual(result.returncode, 0, f"{kind} {name}: {journal.get('failed_at')} {journal.get('error_code')}")
                    test.assertEqual(journal["source_revision"], selection["revision"])
                    return loads_strict_json(result.stdout), journal

                preview, preflight = invoke("--check-server", "preflight")
                test.assertTrue(preview["read_only"])
                test.assertEqual(api.call("ci-read", branch_ref(directory))["revision"], before)
                result, applied = invoke("--apply", "apply")
                test.assertTrue(result["branch_push_attempted"])
                test.assertTrue(result["controller_verified"] and result["deployment_rollout_verified"])
                test.assertEqual(result["artifact_image"], receipt["image"])
                test.assertEqual(result["promoted_revision"], preview["prepared_revision"])
                test.assertNotEqual(result["promoted_revision"], selection["revision"])
                replay, repeated = invoke("--apply", "replay")
                test.assertFalse(replay["branch_push_attempted"])
                test.assertTrue(replay["deployment_rollout_verified"])
                test.assertEqual(api.call("ci-read", branch_ref(directory))["revision"], result["promoted_revision"])
                if signer is not None:
                    for journal in (preflight, applied, repeated):
                        test.assertIs(journal["merged_ci_signature_verified"], True)
                        test.assertEqual(journal["signature_proof"]["signed_annotations_sha256"], sha256(annotations))
                        test.assertEqual(journal["signature_proof"]["public_key_sha256"], plan["signature_policy"]["public_key"]["sha256"])
                    test.assertIs(result["signed_image_admission_verified"], False)
                state.update(promoted=result["promoted_revision"], application_uid=cd["uid"])
                proof_hash = retain(f"zuul-artifact-{kind}-promotion.json",
                    {"preflight": preflight, "apply": applied, "replay": repeated})
                return {f"zuul_artifact_{kind}_cli_and_replay_verified": True, "promotion_sha256": proof_hash,
                        f"zuul_artifact_{kind}_release_signature_verified": signer is not None}

        def verify(manager, actual, outcome):
            live = manager.get("deployment/" + NAME, namespace)
            test.assertEqual(live["metadata"]["uid"], actual["policy"]["deployment_uid"])
            pods = json.loads(manager.call(["get", "pods", "-n", namespace, "-l", "app=" + NAME, "-o", "json"]))["items"]
            test.assertEqual(len(pods), len(context["nodes"]) - 1)
            test.assertEqual({pod["spec"]["nodeName"] for pod in pods}, {node["name"] for node in context["nodes"][1:]})
            for pod in pods:
                status = pod["status"]["containerStatuses"][0]
                test.assertTrue(status["ready"])
                test.assertTrue(status["imageID"].endswith("@" + receipt["manifest_digest"]))
                test.assertEqual(pod["spec"]["containers"][0]["image"], receipt["image"])
            port = actual["service"]["spec"]["ports"][0]["nodePort"]

            def served(node):
                connection = http.client.HTTPConnection(node["address"], port, timeout=5)
                try:
                    connection.request("GET", "/")
                    response = connection.getresponse()
                    return response.status == 200 and response.read(1024) == STATIC_FILE
                finally:
                    connection.close()

            for node in context["nodes"][1:]:
                context["wait"](lambda node=node: served(node), seconds=30)
            if kind == "argocd":
                test.assertIn("argocd.argoproj.io/tracking-id", live["metadata"].get("annotations", {}))
            else:
                test.assertEqual(live["metadata"].get("labels", {}).get("kustomize.toolkit.fluxcd.io/name"), "application")
                app = manager.get("kustomization.kustomize.toolkit.fluxcd.io/application", "flux-system")
                test.assertEqual(app["metadata"]["uid"], state["application_uid"])
                test.assertEqual(app["status"]["lastAppliedRevision"], directory + "@sha1:" + state["promoted"])
                test.assertIn(namespace + "_" + NAME + "_apps_Deployment",
                              {item["id"] for item in app["status"]["inventory"]["entries"]})
            readback = retain(f"zuul-artifact-{kind}-rollout.json", {"deployment": live, "pods": pods,
                               "promoted_revision": state["promoted"], "promotion_sha256": outcome["promotion_sha256"]})
            admission = signer.verify_rollout(native, live, pods, state["promoted"]) if self.admission else {}
            return {f"zuul_artifact_{kind}_rollout_verified": True, f"zuul_artifact_{kind}_rollout_sha256": readback, **admission}

        callbacks = {"prepare": prepare, "verify": verify}
        if kind == "argocd":
            from test_argocd_reconciliation_fixture import exercise
            return exercise(test, gerrit, {"source": {"clone_url": clone_url}}, promote, worker_context=context, artifact_context=callbacks)
        from test_flux_reconciliation_fixture import exercise
        return exercise(test, gerrit, clone_url, lambda reader, app, src, target: promote(reader, app, target, src),
                        worker_context=context, deployment_branch=directory, artifact_context={**callbacks, "path": directory})


class AdmittedRolloutChecks(RolloutChecks):
    admission = True
