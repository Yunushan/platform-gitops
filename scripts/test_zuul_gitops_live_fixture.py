"""Exercise the Zuul promoter against real isolated Gerrit and GitOps controllers."""

from contextlib import contextmanager
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import ssl
import sys
import tempfile
import threading
from urllib.parse import quote

from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
from ci_gitops_handoff import sha256
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
PROJECT = "applications/example"
MANIFEST_PATH = "deploy/staging"
DEPLOYMENT_BRANCHES = {"argocd": "deploy/staging", "flux": "deploy/flux-staging"}
PROJECT_API = "/a/projects/" + quote(PROJECT, safe="")
MANIFEST = b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: synthetic-application\ndata:\n  result: tested\n"


def branch_ref(branch):
    return PROJECT_API + "/branches/" + quote(branch, safe="")


def prepare(gerrit, controllers):
    api = gerrit["api"]
    writer = api.bootstrap("cd-writer")
    group = api.call("fixture-admin", "/a/groups/zuul-deployment-writers", {}, method="PUT")["id"]
    api.call("fixture-admin", "/a/groups/" + quote(group, safe="") + "/members/" + str(writer["_account_id"]), {}, method="PUT")
    access = {"rules": {group: {"action": "ALLOW"}}}
    branches = [DEPLOYMENT_BRANCHES[kind] for kind in controllers]
    api.call("fixture-admin", PROJECT_API + "/access", {"add": {
        "refs/heads/main": {"permissions": {"read": access}},
        **{"refs/heads/" + branch: {"permissions": {"read": access, "push": access}} for branch in branches},
    }})
    baseline = api.call("ci-read", PROJECT_API + "/branches/main")["revision"]
    for branch in branches:
        api.call("fixture-admin", branch_ref(branch), {"revision": baseline}, method="PUT")


def add_manifest(gerrit, change):
    if gerrit.get("gitops_worker_deployment") is None:
        gerrit["api"].call("fixture-admin", "/a/changes/" + change + "/edit/" + quote(MANIFEST_PATH + "/configmap.yaml", safe=""),
                           MANIFEST, method="PUT", content_type="text/plain")
    else:
        from test_argocd_worker_workload import KUSTOMIZATION, application_test, manifest
        for name, body in ((MANIFEST_PATH + "/web.json", json.dumps(manifest(runtime="rke2")).encode()),
                           (MANIFEST_PATH + "/kustomization.yaml", json.dumps(KUSTOMIZATION).encode()),
                           ("test_deployment.py", application_test(runtime="rke2"))):
            gerrit["api"].call("fixture-admin", "/a/changes/" + change + "/edit/" + quote(name, safe=""),
                               body, method="PUT", content_type="text/plain")


@contextmanager
def https_buildset(gerrit, policy, token, upstream):
    endpoint = f"/api/tenant/{policy['tenant']}/buildset/{policy['uuid']}"
    authorization = "Bearer " + token

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            values = self.headers.get_all("Authorization", [])
            if self.path != endpoint or len(values) != 1 or not hmac.compare_digest(values[0], authorization):
                self.send_error(403)
                return
            try:
                payload = json.dumps(upstream(self.path)).encode()
            except (OSError, ValueError):
                self.send_error(502)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    trust = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    trust.load_cert_chain(gerrit["tls"] / "ca.crt", gerrit["tls"] / "tls.key")
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        server.socket = trust.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"https://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=5)


def exercise(test, gerrit, policy, upstream, controller, *, worker_context=None):
    if worker_context is not None and controller not in {"argocd", "flux"}:
        raise ValueError("Select Argo CD or Flux for the worker-backed Zuul case")
    api = gerrit["api"]
    revision = policy["revision"]
    branch = DEPLOYMENT_BRANCHES[controller]
    ref = branch_ref(branch)
    baseline = api.call("ci-read", ref)["revision"]
    test.assertNotEqual(baseline, revision)
    clone_url = gerrit["origin"] + "/a/" + PROJECT + ".git"
    ca = read_bounded_bytes(gerrit["tls"] / "ca.crt", max_bytes=256 * 1024)
    token = secrets.token_urlsafe(36)

    def promote(reader, application, target, source=None):
        with (tempfile.TemporaryDirectory(prefix="zuul-promoter-", dir=gerrit["root"]) as directory,
              tempfile.TemporaryDirectory(prefix="zuul-gitops-", dir=ROOT / "private/validation") as journals):
            root = Path(directory)
            inputs = root / "inputs"
            inputs.mkdir(mode=0o700)
            for name, value in (
                ("token", gerrit["users"]["cd-writer"].encode()), ("ca.crt", ca),
                ("zuul-token", token.encode()), ("zuul-ca.crt", ca),
                ("cd-kubeconfig", read_bounded_bytes(Path(os.environ[target["kubeconfig_env"]]), max_bytes=256 * 1024)),
            ):
                path = inputs / name
                path.write_bytes(value)
                path.chmod(0o600)
            with https_buildset(gerrit, policy, token, upstream) as origin:
                ci = {**policy, "zuul_origin": origin, "gerrit_origin": gerrit["origin"], "zuul_ca_sha256": hashlib.sha256(ca).hexdigest()}
                observed = application["metadata"]
                cd = {"kind": controller, "management_target": target, "namespace": observed["namespace"], "name": observed["name"],
                      "uid": observed["uid"], "spec_sha256": sha256(application["spec"]), "path": MANIFEST_PATH}
                if source is not None:
                    observed = source["metadata"]
                    cd.update(source_namespace=observed["namespace"], source_name=observed["name"], source_uid=observed["uid"],
                              source_spec_sha256=sha256(source["spec"]))
                plan = {"schema_version": 1, "runtime": "raw", "buildset": ci, "handoff": {
                    "schema_version": 1, "runtime": "raw", "bridge_contract_sha256": sha256(ci),
                    "deployment_branch": branch, "allow_create_branch": False, "allow_prune": False,
                    "git_username": "cd-writer", "timeout_seconds": 90, "cd": cd,
                }}
                plan_path = root / "reviewed.json"
                plan_path.write_text(json.dumps(plan), encoding="utf-8")
                plan_path.chmod(0o600)
                command = [sys.executable, "-B", str(ROOT / "scripts/zuul_gitops_handoff.py"),
                           "--plan", str(plan_path), "--input-dir", str(inputs)]
                environment = {"PATH": str(Path(reader.prefix[0]).parent) + ":/usr/bin:/bin",
                               "HOME": str(root), "LC_ALL": "C"}

                def run(mode, name):
                    output = Path(journals) / name
                    result = run_bounded([*command, mode, "--output", str(output)], text=True, check=False,
                                         env=environment, timeout=145, output_max_bytes=256 * 1024)
                    proof = loads_strict_json(read_bounded_bytes(output / "handoff-proof.json", max_bytes=65536))
                    test.assertEqual(result.returncode, 0, f"Promoter phase {proof.get('failed_at')}: {proof.get('error_code')}")
                    test.assertEqual(proof["source_revision"], revision)
                    test.assertEqual(proof["pipeline_run_uid"], policy["uuid"])
                    return loads_strict_json(result.stdout)

                test.assertTrue(run("--check-server", "preflight")["read_only"])
                test.assertEqual(api.call("ci-read", ref)["revision"], baseline)
                print(f"Zuul {controller} fixture verified read-only preflight against the original deployment ref.", flush=True)
                promoted = run("--apply", "promote")
                test.assertTrue(promoted["branch_push_attempted"])
                test.assertTrue(promoted["controller_verified"])
                test.assertEqual(api.call("ci-read", ref)["revision"], revision)
                repeated = run("--apply", "repeat")
                test.assertFalse(repeated["branch_push_attempted"])
                test.assertTrue(repeated["controller_verified"])
                if worker_context is not None:
                    from test_argocd_worker_workload import verify_workload
                    before = verify_workload(test, worker_context, controller=controller)
                    manager = worker_context["client"]
                    pods = json.loads(manager.call(["get", "pods", "-n", "synthetic-application", "-l", "app=synthetic-web", "-o", "json"]))["items"]
                    victim = pods[0]
                    manager.call(["delete", "pod/" + victim["metadata"]["name"], "-n", "synthetic-application", "--wait=false"])
                    def ready():
                        rows = json.loads(manager.call(["get", "pods", "-n", "synthetic-application", "-l", "app=synthetic-web", "-o", "json"]))["items"]
                        return len(rows) == 2 and all(p["metadata"]["uid"] != victim["metadata"]["uid"] and any(
                            c["type"] == "Ready" and c["status"] == "True" for c in p.get("status", {}).get("conditions", [])) for p in rows)
                    worker_context["wait"](ready, seconds=120)
                    after = verify_workload(test, worker_context, controller=controller)
                    for key in ("application_deployment_uid", "application_service_uid", "application_node_uids"):
                        test.assertEqual(before[key], after[key])
                    test.assertNotEqual(before["application_pod_uids"], after["application_pod_uids"])
                    recovered = run("--apply", "recovered")
                    test.assertFalse(recovered["branch_push_attempted"])
                    test.assertTrue(recovered["controller_verified"])
                    print("The Zuul-tested workload served on both dedicated RKE2 workers after Pod replacement, with no second push.", flush=True)
        return {f"zuul_{controller}_live_promotion_verified": True, f"zuul_{controller}_cli_and_replay_verified": True,
                "zuul_buildset_https_fixture_verified": True,
                **({f"zuul_{controller}_worker_pod_replacement_verified": True} if worker_context is not None else {})}

    if controller == "argocd":
        from test_argocd_reconciliation_fixture import exercise as reconcile
        return reconcile(test, gerrit, {"source": {"clone_url": clone_url}}, promote, worker_context=worker_context)
    if controller == "flux":
        from test_flux_reconciliation_fixture import exercise as reconcile
        return reconcile(test, gerrit, clone_url, lambda reader, app, source, target: promote(reader, app, target, source),
                         deployment_branch=branch, worker_context=worker_context)
    raise ValueError("Select one supported disposable GitOps controller")
