#!/usr/bin/env python3
"""Disposable GitLab API and real Git HTTPS handoff integration fixture."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import urlsplit

from bounded_subprocess import run_bounded
import ci_gitops_handoff as handoff
import gerrit_artifact_promotion as promotion
import gitlab_artifact_promotion as artifact
import gitlab_gitops_handoff as adapter
import gitlab_static_image as publisher
import verify_gitlab_pipeline as verifier
from test_ci_gitops_handoff import fixture
from test_gerrit_artifact_promotion import DEPLOYMENT_UID, deployment
from test_gitlab_gitops_handoff import publication_case


@unittest.skipUnless(os.name == "posix" and os.environ.get("GITLAB_GITOPS_HTTPS_FIXTURE") == "1"
                     and shutil.which("git") and shutil.which("openssl"), "isolated Git HTTPS fixture not requested")
class RealGitLabHandoff(unittest.TestCase):
    def test_real_git_push_and_retry_for_both_controllers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, repos, credentials = root / "source", root / "repos", root / "credentials"
            for path in (source, repos / "group", credentials): path.mkdir(parents=True)
            def run(*args):
                return run_bounded(list(args), timeout=30, text=True, output_max_bytes=1024 * 1024, check=True).stdout.strip()
            run("git", "init", "-q", "-b", "main", str(source))
            run("git", "-C", str(source), "config", "user.name", "Fixture")
            run("git", "-C", str(source), "config", "user.email", "fixture@example.invalid")
            (source / "README").write_text("base", encoding="utf-8")
            run("git", "-C", str(source), "add", ".")
            run("git", "-C", str(source), "commit", "-qm", "Base")
            old = run("git", "-C", str(source), "rev-parse", "HEAD")
            run("git", "-C", str(source), "branch", "deploy/staging")
            (source / "deploy" / "staging").mkdir(parents=True)
            (source / "deploy" / "staging" / "workload.json").write_text(json.dumps(deployment()), encoding="utf-8")
            flux = source / "deploy" / "flux-staging"
            flux.mkdir()
            (flux / "workload.json").write_text(json.dumps(deployment()), encoding="utf-8")
            (flux / "kustomization.yaml").write_text(
                "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n- workload.json\n", encoding="utf-8")
            (source / "public").mkdir()
            (source / "public/index.html").write_bytes(b"reviewed-static-bytes\n")
            run("git", "-C", str(source), "add", ".")
            run("git", "-C", str(source), "commit", "-qm", "Tested")
            revision = run("git", "-C", str(source), "rev-parse", "HEAD")
            repository = repos / "group" / "project.git"
            run("git", "clone", "--bare", "--quiet", str(source), str(repository))
            run("git", "--git-dir=" + str(repository), "config", "http.receivepack", "true")
            run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                "-addext", "subjectAltName=DNS:localhost", "-keyout", str(root / "tls.key"), "-out", str(credentials / "ca.crt"))
            (credentials / "token").write_text("synthetic-git-token", encoding="utf-8")
            basic = "Basic " + base64.b64encode(b"cd-writer:synthetic-git-token").decode()
            api_token = "synthetic-api-token"
            class Server(BaseHTTPRequestHandler):
                failing = False
                statuses = []
                receipt = None
                artifact_reads = 0
                def log_message(self, *args): pass
                def handle_request(self):
                    url = urlsplit(self.path)
                    if url.path.startswith("/api/v4/"):
                        if self.headers.get("PRIVATE-TOKEN") != api_token:
                            self.send_error(403); return
                        prefix = "/api/v4/projects/7"
                        job = {"id": 31, "name": "build", "status": "failed" if self.failing else "success", "allow_failure": False}
                        status = Server.statuses.pop(0) if url.path == prefix + "/pipelines/23" and Server.statuses else "success"
                        values = {prefix: {"id": 7, "path_with_namespace": "group/project"},
                                  prefix + "/repository/branches/main": {"name": "main", "protected": True, "commit": {"id": revision}},
                                  prefix + "/pipelines/23": {"id": 23, "project_id": 7, "sha": revision, "ref": "main", "status": status, "source": "push", "updated_at": "fixture"},
                                  prefix + "/pipelines/latest": {"id": 23, "project_id": 7, "sha": revision, "ref": "main", "status": "success", "source": "push", "updated_at": "fixture"},
                                  prefix + "/pipelines/23/jobs": [job],
                                  prefix + "/jobs/31": dict(job, pipeline={"id": 23}, commit={"id": revision})}
                        if url.path == prefix + "/jobs/31/artifacts/" + publisher.ARTIFACT:
                            Server.artifact_reads += 1
                            if Server.receipt is not None:
                                values[url.path] = Server.receipt
                        if url.path not in values: self.send_error(404); return
                        body = json.dumps(values[url.path]).encode()
                        self.send_response(200); self.send_header("X-Next-Page", "")
                        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                        return
                    if self.headers.get("Authorization") != basic:
                        self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="fixture"')
                        self.send_header("Content-Length", "0"); self.end_headers(); return
                    size = int(self.headers.get("Content-Length", "0"))
                    if size > 1024 * 1024: self.send_error(413); return
                    env = {**os.environ, "GIT_PROJECT_ROOT": str(repos), "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": url.path,
                           "QUERY_STRING": url.query, "REQUEST_METHOD": self.command, "REMOTE_USER": "cd-writer",
                           "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(size)}
                    result = run_bounded(["git", "http-backend"], input=self.rfile.read(size), env=env,
                                         timeout=30, output_max_bytes=4 * 1024 * 1024, check=True)
                    headers, body = result.stdout.split(b"\r\n\r\n", 1)
                    fields = [line.decode("ascii").split(":", 1) for line in headers.split(b"\r\n")]
                    self.send_response(next((int(v.strip().split()[0]) for k, v in fields if k.lower() == "status"), 200))
                    for k, v in fields:
                        if k.lower() != "status": self.send_header(k, v.strip())
                    self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                do_GET = handle_request
                do_POST = handle_request
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(credentials / "ca.crt", root / "tls.key")
            with ThreadingHTTPServer(("127.0.0.1", 0), Server) as server:
                server.socket = context.wrap_socket(server.socket, server_side=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                try:
                    origin = f"https://localhost:{server.server_port}"
                    pipeline = {"schema_version": 1, "api_url": origin + "/api/v4", "project_id": 7, "project_path": "group/project",
                                "branch": "main", "pipeline_id": 23, "revision": revision, "required_jobs": ["build"]}
                    with patch.dict(os.environ, {"SSL_CERT_FILE": str(credentials / "ca.crt")}):
                        os.environ.pop("SSLKEYLOGFILE", None)
                        for kind in ("argocd", "flux"):
                            with self.subTest(kind=kind):
                                run("git", "--git-dir=" + str(repository), "update-ref", "refs/heads/deploy/staging", old)
                                policy, _, _, _, _, _, cd = fixture(root, kind=kind)
                                policy["bridge_contract_sha256"] = handoff.sha256(pipeline)
                                clone = origin + "/group/project.git"
                                if kind == "argocd":
                                    obj = cd.objects["application.argoproj.io/application"]
                                    obj["spec"]["source"]["repoURL"] = clone
                                    obj["status"]["sync"]["revision"] = revision
                                    obj["status"]["operationState"]["syncResult"]["revision"] = revision
                                    obj["status"]["reconciledAt"] = datetime.now(timezone.utc).isoformat()
                                else:
                                    obj = cd.objects["kustomization.kustomize.toolkit.fluxcd.io/application"]
                                    repo = cd.objects["gitrepository.source.toolkit.fluxcd.io/application"]
                                    repo["spec"]["url"] = clone
                                    repo["status"]["artifact"]["revision"] = "deploy/staging@sha1:" + revision
                                    obj["status"]["lastAppliedRevision"] = "deploy/staging@sha1:" + revision
                                    policy["cd"]["source_spec_sha256"] = handoff.sha256(repo["spec"])
                                policy["cd"]["spec_sha256"] = handoff.sha256(obj["spec"])
                                pipeline_policy = {k: v for k, v in pipeline.items() if k not in {"pipeline_id", "revision"}}
                                policy["bridge_contract_sha256"] = handoff.sha256(pipeline_policy)
                                reusable = {"schema_version": 2, "runtime": "rke2", "pipeline_policy": pipeline_policy, "handoff": policy}
                                plan = adapter.materialize(reusable, 23, revision)
                                get = verifier.requester(pipeline, api_token)
                                self.assertEqual(adapter.discover_latest(pipeline_policy, get), (23, revision))
                                Server.statuses = ["pending", "running", "success"]
                                adapter.wait_pipeline(pipeline, get, 30, sleep=lambda _: None)
                                Server.failing = True
                                with self.assertRaises(ValueError): adapter.execute(plan, cd, get, apply=True, git_inputs=credentials)
                                self.assertEqual(run("git", "--git-dir=" + str(repository), "rev-parse", "refs/heads/deploy/staging"), old)
                                Server.failing = False
                                # Controller fixture reports its reconciliation immediately after the push.
                                original_observe = handoff.cd_observed
                                with patch.object(handoff, "cd_observed", wraps=original_observe) as observed:
                                    if kind == "argocd":
                                        def observe(*args, **kwargs):
                                            obj["status"]["reconciledAt"] = datetime.now(timezone.utc).isoformat()
                                            return original_observe(*args, **kwargs)
                                        # Avoid relying on wall-clock ordering of the preloaded fixture.
                                        observed.side_effect = observe
                                    result = adapter.execute(plan, cd, get, apply=True, git_inputs=credentials, require_latest=True)
                                self.assertTrue(result["controller_verified"])
                                self.assertTrue(result["branch_push_attempted"])
                                self.assertEqual(run("git", "--git-dir=" + str(repository), "rev-parse", "refs/heads/deploy/staging"), revision)
                                result = adapter.execute(plan, cd, get, apply=True, git_inputs=credentials, require_latest=True)
                                self.assertFalse(result["branch_push_attempted"])
                                run("git", "--git-dir=" + str(repository), "update-ref", "refs/heads/deploy/staging", old)
                                self.artifact_round_trip(plan, root, credentials, old, repository, run, cd, obj, Server, api_token)
                finally:
                    server.shutdown(); thread.join(timeout=5)

    def artifact_round_trip(self, fixed, root, credentials, old, repository, run, cd, obj, server, api_token):
        pipeline = fixed["pipeline"]
        kind = fixed["handoff"]["cd"]["kind"]
        ca = (credentials / "ca.crt").read_bytes()
        plan, contract, receipt = publication_case(fixed, root, ca_sha256=hashlib.sha256(ca).hexdigest())
        plan["artifact_policy"]["expected_deployment_revision"] = old
        if kind == "flux":
            obj["spec"]["path"] = "./deploy/flux-staging"
            plan["handoff"]["cd"].update(path="deploy/flux-staging", spec_sha256=handoff.sha256(obj["spec"]))
            plan["artifact_policy"]["manifest"] = "deploy/flux-staging/workload.json"
        manifest_path = plan["artifact_policy"]["manifest"]
        publish_inputs = root / (kind + "-publish-inputs")
        (publish_inputs / "source").mkdir(parents=True)
        for name in ("ca.crt", "token"):
            shutil.copyfile(credentials / name, publish_inputs / "source" / name)
        env = {"CI_SERVER_URL": contract["api_url"].removesuffix("/api/v4"), "CI_PROJECT_ID": "7", "CI_PROJECT_PATH": "group/project",
               "CI_COMMIT_BRANCH": "main", "CI_JOB_NAME": "build", "CI_COMMIT_REF_PROTECTED": "true", "CI_PIPELINE_SOURCE": "push",
               "CI_COMMIT_SHA": pipeline["revision"], "CI_PIPELINE_ID": "23", "CI_JOB_ID": "31"}

        def publish(registry, output):
            self.assertEqual(registry["revision"], pipeline["revision"])
            self.assertEqual((output / "index.html").read_bytes(), b"reviewed-static-bytes\n")
            return receipt["artifact"]

        # Keep the real HTTPS checkout and exact revision checks in this test.
        with (patch.object(publisher.harbor, "private_inputs"), patch.object(publisher.harbor, "publish", side_effect=publish) as upload,
              patch.object(publisher.os, "geteuid", return_value=65534)):
            server.receipt = publisher.publish(contract, publish_inputs, env)
            self.assertEqual(server.receipt, receipt)
            with self.assertRaises((ValueError, subprocess.SubprocessError)):
                publisher.publish(contract, publish_inputs, {**env, "CI_COMMIT_SHA": old})
            upload.assert_called_once()

        live = deployment()
        live["metadata"].update(uid=DEPLOYMENT_UID, generation=1)
        live["status"] = {"observedGeneration": 1, "replicas": 2, "updatedReplicas": 2, "readyReplicas": 2, "availableReplicas": 2}
        cd.objects["deployment.apps/application"] = live
        (credentials / "api-token").write_text(api_token, encoding="ascii")
        (credentials / "api-token").chmod(0o600)
        (credentials / "ca.crt").chmod(0o600)
        get, get_artifact = artifact.readers(plan, contract, credentials)
        artifact_request = artifact.release_request(plan, contract, get, get_artifact)
        self.assertEqual(artifact_request["image"], receipt["artifact"]["image"])
        self.assertEqual(artifact_request["annotations"]["platform.gitops/gitlab-job-id"], "31")

        def observed(*args, **kwargs):
            revision = run("git", "--git-dir=" + str(repository), "rev-parse", "refs/heads/deploy/staging")
            if kind == "argocd":
                obj["status"]["sync"]["revision"] = revision
                obj["status"]["operationState"]["syncResult"]["revision"] = revision
                obj["status"]["reconciledAt"] = datetime.now(timezone.utc).isoformat()
            else:
                ref = "deploy/staging@sha1:" + revision
                obj["status"]["lastAppliedRevision"] = ref
                cd.objects["gitrepository.source.toolkit.fluxcd.io/application"]["status"]["artifact"]["revision"] = ref
            manifest = run("git", "--git-dir=" + str(repository), "show", revision + ":" + manifest_path)
            live["spec"] = json.loads(manifest)["spec"]
            return cd_observed(*args, **kwargs)

        cd_observed = handoff.cd_observed
        with (patch.object(promotion, "verify_destination"), patch.object(handoff, "cd_observed", side_effect=observed),
              patch.object(promotion, "verify_release_signature", return_value={"signed_annotations_sha256": handoff.sha256(artifact_request["annotations"])}) as sign):
            preview = artifact.execute(plan, contract, cd, get, get_artifact, inputs=credentials)
            self.assertEqual(run("git", "--git-dir=" + str(repository), "rev-parse", "refs/heads/deploy/staging"), old)
            result = artifact.execute(plan, contract, cd, get, get_artifact, inputs=credentials, apply=True)
            self.assertEqual(result["promoted_revision"], preview["prepared_revision"])
            self.assertTrue(result["branch_push_attempted"] and result["deployment_rollout_verified"])
            repeated = artifact.execute(plan, contract, cd, get, get_artifact, inputs=credentials, apply=True)
            self.assertFalse(repeated["branch_push_attempted"])
            self.assertEqual(sign.call_count, 5)
            for args in sign.call_args_list:
                self.assertEqual(args.args[0]["annotations"], artifact_request["annotations"])
        changed = run("git", "--git-dir=" + str(repository), "diff", "--name-only", pipeline["revision"], result["promoted_revision"])
        self.assertEqual(changed, manifest_path)


if __name__ == "__main__": unittest.main()
