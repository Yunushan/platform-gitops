#!/usr/bin/env python3
"""Exercise exact-revision CI/CD handoff without company systems."""

import base64
import contextlib
import copy
import io
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

import ci_gitops_handoff as handoff
import tekton_forge_bridge as bridge
import tekton_forge_reporter as reporter
from bounded_subprocess import run_bounded
from test_tekton_forge_reporter import Forge
from test_tekton_forge_reporter import fixture as ci_fixture


class Source(Forge):
    def __init__(self):
        super().__init__()
        self.head = "a" * 40

    def request(self, plan, suffix, payload=None):
        if "/branches/" in suffix:
            return {"commit": {"id": self.head}}, {}
        return super().request(plan, suffix, payload)


class CDClient:
    def __init__(self):
        self.objects, self.calls = {}, []

    def get(self, resource, namespace=None):
        self.calls.append((resource, namespace))
        return copy.deepcopy(self.objects.get(resource))


def fixture(root, kind="argocd", runtime="rke2"):
    contract, template, digest, client, run = ci_fixture(root, runtime=runtime)
    source = Source()
    with mock.patch.object(reporter, "forge_request", side_effect=source.request):
        reporter.report_run(client, contract, template, digest, run)
    cd_client = CDClient()
    cd = {"kind": kind, "management_target": {"kubeconfig_env": "PLATFORM_CD_KUBECONFIG", "context": "cd", "cluster_uid": "77777777-7777-4777-8777-777777777777"},
          "namespace": "argocd" if kind == "argocd" else "platform-apps", "name": "application", "uid": "88888888-8888-4888-8888-888888888888",
          "spec_sha256": "", "path": "deploy/staging"}
    plan = {"schema_version": 1, "runtime": runtime, "bridge_contract_sha256": digest, "deployment_branch": "deploy/staging",
            "allow_create_branch": False, "allow_prune": False, "git_username": "cd-writer", "timeout_seconds": 30, "cd": cd}
    ready = {"observedGeneration": 1, "conditions": [{"type": "Ready", "status": "True", "observedGeneration": 1}]}
    metadata = {"name": cd["name"], "namespace": cd["namespace"], "uid": cd["uid"], "generation": 1}
    if kind == "argocd":
        obj = {"metadata": metadata, "spec": {"source": {"repoURL": contract["plan"]["source"]["clone_url"], "targetRevision": "deploy/staging", "path": cd["path"]},
                "destination": {"server": "https://kubernetes.default.svc", "namespace": "application"}, "syncPolicy": {"automated": {"prune": False}}},
               "status": {"reconciledAt": (datetime.now(UTC) + timedelta(seconds=5)).isoformat(), "health": {"status": "Healthy"},
                    "sync": {"status": "Synced", "revision": "a" * 40}, "operationState": {"phase": "Succeeded", "syncResult": {"revision": "a" * 40}}}}
        cd_client.objects["application.argoproj.io/application"] = obj
    else:
        cd.update(source_namespace="platform-apps", source_name="application", source_uid="99999999-9999-4999-8999-999999999999", source_spec_sha256="")
        revision = "deploy/staging@sha1:" + "a" * 40
        obj = {"metadata": metadata, "spec": {"path": "./deploy/staging", "sourceRef": {"kind": "GitRepository", "name": "application"},
               "wait": True, "force": False, "prune": False, "serviceAccountName": "application-reconciler"},
               "status": {**copy.deepcopy(ready), "lastAppliedRevision": revision}}
        source_obj = {"metadata": {"name": "application", "namespace": "platform-apps", "uid": cd["source_uid"], "generation": 1},
                      "spec": {"url": contract["plan"]["source"]["clone_url"], "ref": {"branch": "deploy/staging"}},
                      "status": {**copy.deepcopy(ready), "artifact": {"revision": revision}}}
        cd["source_spec_sha256"] = handoff.sha256(source_obj["spec"])
        cd_client.objects["kustomization.kustomize.toolkit.fluxcd.io/application"] = obj
        cd_client.objects["gitrepository.source.toolkit.fluxcd.io/application"] = source_obj
    cd["spec_sha256"] = handoff.sha256(obj["spec"])
    return plan, contract, template, client, run, source, cd_client


class Git:
    def __init__(self, before="b" * 40):
        self.before, self.tip, self.pushes, self.source = before, before, 0, "a" * 40
        self.allow_create = None

    def ref(self, branch):
        return self.source if branch == "main" else self.tip

    def prepare(self, branch, revision, allow_create):
        self.allow_create = allow_create
        return self.before

    def check_path(self, *args):
        pass

    def promote(self, branch, revision, before):
        attempted = self.tip != revision
        self.pushes += int(attempted)
        self.tip = revision
        return attempted


class HandoffTests(unittest.TestCase):
    def test_new_argocd_operation_before_push_stops_promotion(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, contract, template, ci, run, source, cd = fixture(Path(directory))
            git = Git()
            original_get = cd.get
            reads = [0]
            def get(resource, namespace=None):
                obj = original_get(resource, namespace)
                reads[0] += 1
                if reads[0] == 2:
                    obj["status"]["operationState"]["phase"] = "Running"
                return obj
            with mock.patch.object(cd, "get", side_effect=get), mock.patch.object(handoff, "read_api", side_effect=source.request), \
                 mock.patch.object(handoff, "GitBranch", return_value=git), self.assertRaises(handoff.HandoffError):
                handoff.execute(plan, contract, template, ci, cd, run["metadata"]["name"], apply=True)
            self.assertEqual(git.pushes, 0)

    def test_deleted_branch_after_preflight_is_not_recreated(self):
        remote = object.__new__(handoff.GitBranch)
        remote.url = "https://forge.example/repository.git"
        remote.ref = mock.Mock(return_value=None)
        remote.call = mock.Mock()
        with self.assertRaises(handoff.HandoffError):
            remote.promote("deploy/staging", "a" * 40, "b" * 40)
        remote.call.assert_not_called()

    def test_push_uses_exact_preflight_lease(self):
        remote = object.__new__(handoff.GitBranch)
        remote.url = "https://forge.example/repository.git"
        remote.ref = mock.Mock(side_effect=["b" * 40, "a" * 40])
        remote.call = mock.Mock()
        self.assertTrue(remote.promote("deploy/staging", "a" * 40, "b" * 40))
        self.assertIn("--force-with-lease=refs/heads/deploy/staging:" + "b" * 40,
                      remote.call.call_args.args[0])

    def test_cli_failure_is_concise_and_does_not_expose_private_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_marker = "private-plan-value-not-for-output"
            plan = root / "plan.json"
            plan.write_text(json.dumps({"private_marker": private_marker}), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                code = handoff.main(["--plan", str(plan), "--bundle-dir", str(root), "--pipeline-run", "build"])
            self.assertEqual(code, 1)
            self.assertIn("handoff-validation-or-transport-failed", output.getvalue())
            self.assertNotIn(private_marker, output.getvalue())
            self.assertNotIn("Traceback", output.getvalue())

    def test_preflight_reads_evidence_but_never_pushes_or_writes_cluster_state(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, contract, template, ci, run, source, cd = fixture(Path(directory))
            git = Git()
            handoff.validate(plan, contract)
            with mock.patch.object(handoff, "read_api", side_effect=source.request), mock.patch.object(handoff, "GitBranch", return_value=git):
                result = handoff.execute(plan, contract, template, ci, cd, run["metadata"]["name"])
            self.assertTrue(result["read_only"])
            self.assertTrue(result["preflight_verified"])
            self.assertEqual(git.pushes, 0)
            self.assertTrue(all(resource.startswith("application.") for resource, _ in cd.calls))

    def test_successful_handoff_waits_for_the_same_commit_in_each_controller(self):
        for kind in ("argocd", "flux"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                plan, contract, template, ci, run, source, cd = fixture(Path(directory), kind)
                git, phases = Git(), []
                with mock.patch.object(handoff, "read_api", side_effect=source.request), mock.patch.object(handoff, "GitBranch", return_value=git):
                    result = handoff.execute(plan, contract, template, ci, cd, run["metadata"]["name"], apply=True, record=lambda **p: phases.append(p))
                self.assertTrue(result["controller_verified"])
                self.assertFalse(result["full_stack_verified"])
                self.assertEqual(git.pushes, 1)
                self.assertEqual(phases[-1]["phase"], "controller-verified")

    def test_post_push_tls_handshake_timeout_recovers_without_repeating_git_write(self):
        from kubernetes_management import KubernetesOperationError
        for cd_kind in ('argocd','flux'):
            with self.subTest(cd_kind=cd_kind),tempfile.TemporaryDirectory() as directory:
                plan,contract,template,ci,run,source,cd=fixture(Path(directory),cd_kind)
                git=Git();records=[]
                with mock.patch.object(handoff,'read_api',side_effect=source.request),mock.patch.object(handoff,'GitBranch',return_value=git),mock.patch.object(handoff,'cd_observed',side_effect=[KubernetesOperationError(1,'net/http: TLS handshake timeout'),True]),mock.patch.object(handoff.time,'sleep'):
                    result=handoff.execute(plan,contract,template,ci,cd,run['metadata']['name'],apply=True,record=lambda **v:records.append(v))
                self.assertTrue(result['controller_verified']);self.assertEqual(git.pushes,1)
                self.assertTrue(any('controller_observation_transport_retry' in v for v in records))

    def test_post_push_git_ref_read_recovers_without_another_push(self):
        for kind in ("argocd", "flux"):
            for failure in (handoff.HandoffError("git-operation-failed"), subprocess.TimeoutExpired("git", 1)):
                with self.subTest(kind=kind, failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                    plan, contract, template, ci, run, source, cd = fixture(Path(directory), kind)
                    git, records = Git(), []
                    original_ref = git.ref
                    deployment_reads = [0]

                    def ref(branch):
                        if git.pushes and branch == plan["deployment_branch"]:
                            deployment_reads[0] += 1
                            if deployment_reads[0] == 1:
                                raise failure
                        return original_ref(branch)

                    with mock.patch.object(git, "ref", side_effect=ref), \
                         mock.patch.object(handoff, "read_api", side_effect=source.request), \
                         mock.patch.object(handoff, "GitBranch", return_value=git), mock.patch.object(handoff.time, "sleep"):
                        result = handoff.execute(plan, contract, template, ci, cd, run["metadata"]["name"],
                                                 apply=True, record=lambda **row: records.append(row))
                    self.assertTrue(result["controller_verified"])
                    self.assertEqual(git.pushes, 1)
                    self.assertEqual(deployment_reads[0], 2)
                    self.assertTrue(any(row.get("deployment_ref_transport_retry") for row in records))

    def test_post_push_ref_recovery_still_refuses_a_superseding_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, contract, template, ci, run, source, cd = fixture(Path(directory))
            git = Git()
            original_ref = git.ref
            reads = iter([handoff.HandoffError("git-operation-failed"), "c" * 40])

            def ref(branch):
                if git.pushes and branch == plan["deployment_branch"]:
                    value = next(reads)
                    if isinstance(value, Exception):
                        raise value
                    return value
                return original_ref(branch)

            with mock.patch.object(git, "ref", side_effect=ref), \
                 mock.patch.object(handoff, "read_api", side_effect=source.request), \
                 mock.patch.object(handoff, "GitBranch", return_value=git), mock.patch.object(handoff.time, "sleep"), \
                 self.assertRaisesRegex(handoff.HandoffError, "^deployment-branch-was-superseded$"):
                handoff.execute(plan, contract, template, ci, cd, run["metadata"]["name"], apply=True)
            self.assertEqual(git.pushes, 1)

    def test_post_push_ref_failure_expires_without_success_or_another_push(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, contract, template, ci, run, source, cd = fixture(Path(directory))
            git = Git()
            original_ref = git.ref

            def ref(branch):
                if git.pushes and branch == plan["deployment_branch"]:
                    raise handoff.HandoffError("git-operation-failed")
                return original_ref(branch)

            with mock.patch.object(git, "ref", side_effect=ref), \
                 mock.patch.object(handoff, "read_api", side_effect=source.request), \
                 mock.patch.object(handoff, "GitBranch", return_value=git), \
                 mock.patch.object(handoff.time, "monotonic", side_effect=[0, plan["timeout_seconds"]]), \
                 self.assertRaisesRegex(handoff.HandoffError, "^controller-reconciliation-not-verified-before-timeout$"):
                handoff.execute(plan, contract, template, ci, cd, run["metadata"]["name"], apply=True)
            self.assertEqual(git.pushes, 1)

    def test_post_push_transient_read_recovers_without_repeating_git_write(self):
        for kind in ('argocd','flux'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                plan,contract,template,ci,run,source,cd=fixture(Path(directory),kind)
                git=Git();records=[]
                with mock.patch.object(handoff,'read_api',side_effect=source.request),mock.patch.object(handoff,'GitBranch',return_value=git),\
                        mock.patch.object(handoff,'cd_observed',side_effect=[subprocess.TimeoutExpired(['private-command'],1),True]),mock.patch.object(handoff.time,'sleep'):
                    result=handoff.execute(plan,contract,template,ci,cd,run['metadata']['name'],apply=True,record=lambda **v:records.append(v))
                self.assertTrue(result['controller_verified']);self.assertEqual(git.pushes,1)
                self.assertTrue(any('controller_observation_transport_retry' in v for v in records));self.assertNotIn('private-command',json.dumps(records))

    def test_tls_negotiation_timeout_retries_only_the_read_and_refuses_mixed_errors(self):
        from kubernetes_management import KubernetesOperationError
        timeout='net/http: TLS handshake timeout'
        records=[]
        self.assertFalse(handoff.read_observation(mock.Mock(side_effect=KubernetesOperationError(1,timeout)),lambda **v:records.append(v)))
        self.assertEqual(records[0]['controller_observation_transport_retry']['exception_type'],'KubernetesOperationError')
        for text in ('x509: certificate signed by unknown authority','certificate has expired','remote error: tls: bad certificate','TLS handshake failure','Unauthorized','Forbidden','Invalid value'):
            with self.subTest(text=text),self.assertRaises(KubernetesOperationError):
                handoff.read_observation(mock.Mock(side_effect=KubernetesOperationError(1,timeout+'; '+text)))

    def test_controller_observation_never_retries_identity_auth_tls_or_invalid_data(self):
        from kubernetes_management import KubernetesOperationError
        for message in ('i/o timeout','connection reset','ServiceUnavailable'):
            observe=mock.Mock(side_effect=KubernetesOperationError(1,message))
            self.assertFalse(handoff.read_observation(observe));observe.assert_called_once()
        for error in (ValueError('identity changed'),KubernetesOperationError(1,'Forbidden: connection reset'),
                KubernetesOperationError(1,'Unauthorized'),KubernetesOperationError(1,'x509: certificate signed by unknown authority'),
                KubernetesOperationError(1,'NotFound'),KubernetesOperationError(1,'invalid value: i/o timeout')):
            with self.subTest(error=type(error).__name__),self.assertRaises(type(error)):
                handoff.read_observation(mock.Mock(side_effect=error))

    def test_bad_ci_status_or_new_source_head_blocks_before_git(self):
        for fault in ("status", "head", "receipt", "pod"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                plan, contract, template, ci, run, source, cd = fixture(Path(directory))
                if fault == "status": source.status["status"] = "failure"
                if fault == "head": source.head = "c" * 40
                if fault == "receipt": del ci.objects["configmap/" + run["metadata"]["name"] + "-report"]
                if fault == "pod": del ci.objects["pod/application-pod"]
                with mock.patch.object(handoff, "read_api", side_effect=source.request), mock.patch.object(handoff, "GitBranch") as git, self.assertRaises(ValueError):
                    handoff.execute(plan, contract, template, ci, cd, run["metadata"]["name"], apply=True)
                git.assert_not_called()

    def test_reviewed_controller_identity_and_explicit_pruning_cannot_be_bypassed(self):
        for fault in ("uid", "source", "manual", "prune"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                plan, contract, template, ci, run, source, cd = fixture(Path(directory))
                obj = cd.objects["application.argoproj.io/application"]
                if fault == "uid": obj["metadata"]["uid"] = "different"
                if fault == "source": obj["spec"]["source"]["repoURL"] = "https://other.example.com/repository.git"
                if fault == "manual": obj["spec"]["syncPolicy"]["automated"]["enabled"] = False
                if fault == "prune": obj["spec"]["syncPolicy"]["automated"]["prune"] = True
                # Even a newly reviewed spec cannot authorize another source,
                # implicit pruning or a controller that does not auto reconcile.
                plan["cd"]["spec_sha256"] = handoff.sha256(obj["spec"])
                with mock.patch.object(handoff, "read_api", side_effect=source.request), mock.patch.object(handoff, "GitBranch") as git, self.assertRaises(ValueError):
                    handoff.execute(plan, contract, template, ci, cd, run["metadata"]["name"], apply=True)
                git.assert_not_called()

    def test_original_argo_recovery_requires_fresh_reconciliation_and_successful_original_revision(self):
        fence=datetime.now(UTC)
        original={'status':{'health':{'status':'Healthy'},'sync':{'status':'Synced','revision':'a'*40},
            'operationState':{'phase':'Succeeded','syncResult':{'revision':'a'*40}},'reconciledAt':(fence+timedelta(seconds=1)).isoformat()}}
        self.assertTrue(handoff.argocd_observed(original,'a'*40,since=fence))
        for mode in ('stale','future','wrong-operation','failed-operation','error-condition'):
            changed=copy.deepcopy(original)
            if mode=='stale':changed['status']['reconciledAt']=(fence-timedelta(seconds=1)).isoformat()
            elif mode=='future':changed['status']['reconciledAt']=(fence+timedelta(seconds=60)).isoformat()
            elif mode=='wrong-operation':changed['status']['operationState']['syncResult']['revision']='b'*40
            elif mode=='failed-operation':changed['status']['operationState']['phase']='Failed'
            else:changed['status']['conditions']=[{'type':'ComparisonError'}]
            with self.subTest(mode=mode):self.assertFalse(handoff.argocd_observed(changed,'a'*40,since=fence))

    def test_stale_or_wrong_revision_health_never_completes_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, contract, _, _, _, _, cd = fixture(Path(directory))
            obj = cd.objects["application.argoproj.io/application"]
            obj["status"]["sync"]["revision"] = "b" * 40
            self.assertFalse(handoff.cd_observed(cd, plan, contract, "a" * 40))
            obj["status"]["sync"]["revision"] = "a" * 40
            obj["status"]["reconciledAt"] = "2020-01-01T00:00:00Z"
            self.assertFalse(handoff.cd_observed(cd, plan, contract, "a" * 40, since=datetime.now(UTC)))
        with tempfile.TemporaryDirectory() as directory:
            plan, contract, _, _, _, _, cd = fixture(Path(directory), "flux")
            obj = cd.objects["kustomization.kustomize.toolkit.fluxcd.io/application"]
            obj["status"]["conditions"][0]["observedGeneration"] = 0
            self.assertFalse(handoff.cd_observed(cd, plan, contract, "a" * 40))
            obj["status"]["conditions"][0]["observedGeneration"] = 1
            obj["status"]["lastAppliedRevision"] = "deploy/staging@sha1:" + "b" * 40
            self.assertFalse(handoff.cd_observed(cd, plan, contract, "a" * 40))

    def test_timeout_retains_promoted_ref_and_never_rolls_back_automatically(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, contract, template, ci, run, source, cd = fixture(Path(directory))
            git, phases = Git(), []
            with mock.patch.object(handoff, "read_api", side_effect=source.request), mock.patch.object(handoff, "GitBranch", return_value=git), \
                 mock.patch.object(handoff, "cd_observed", return_value=False), mock.patch.object(handoff.time, "monotonic", side_effect=[0, 31]), self.assertRaises(handoff.HandoffError):
                handoff.execute(plan, contract, template, ci, cd, run["metadata"]["name"], apply=True, record=lambda **p: phases.append(p))
            self.assertEqual(git.pushes, 1)
            self.assertEqual(git.tip, "a" * 40)
            self.assertEqual(phases[-1]["phase"], "git-promoted")

    def test_existing_promoted_revision_is_idempotent_and_all_runtime_labels_remain_scoped(self):
        for runtime in ("rke2", "k3s", "k8s", "docker", "raw"):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as directory:
                plan, contract, template, ci, run, source, cd = fixture(Path(directory), runtime=runtime)
                handoff.validate(plan, contract)
                git = Git("a" * 40)
                with mock.patch.object(handoff, "read_api", side_effect=source.request), mock.patch.object(handoff, "GitBranch", return_value=git):
                    result = handoff.execute(plan, contract, template, ci, cd, run["metadata"]["name"], apply=True)
                self.assertEqual(git.pushes, 0)
                self.assertTrue(result["kubernetes_workload_scope"])
                self.assertFalse(result["full_stack_verified"])

    def test_local_mode_and_stale_program_fail_before_credentials_or_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, _, _, _, run, _, _ = fixture(root)
            path = root / "plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")
            with mock.patch.object(bridge, "Client") as client:
                self.assertTrue(handoff.operate(path, root, run["metadata"]["name"])["read_only"])
                client.assert_not_called()
            original = bridge.read_bounded_text
            def changed(path, **kwargs):
                return original(path, **kwargs) + ("\n# changed" if Path(path).name == "ci_gitops_handoff.py" else "")
            with mock.patch.object(bridge, "read_bounded_text", side_effect=changed), mock.patch.object(bridge, "Client") as client, self.assertRaises(ValueError):
                handoff.operate(path, root, run["metadata"]["name"], apply=True)
            client.assert_not_called()

    def test_private_journal_is_new_only_and_retains_partial_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(handoff, "ROOT", root):
                record, state = handoff.new_journal(root / "private" / "proof", {"controller_verified": False})
                record(phase="git-promoted", promoted_revision="a" * 40)
                actual = json.loads((root / "private" / "proof" / "handoff-proof.json").read_text())
                self.assertEqual(actual["phase"], "git-promoted")
                self.assertFalse(actual["controller_verified"])
                with self.assertRaises(ValueError):
                    handoff.new_journal(root / "private" / "proof", {})
                with self.assertRaises(ValueError):
                    handoff.new_journal(root / "public-proof", {})

    @unittest.skipUnless(os.name == "posix", "Real Git HTTPS branch fixture needs Linux Git/askpass paths")
    def test_real_https_git_fast_forward_preserves_other_refs_and_refuses_divergence(self):
        git, openssl = shutil.which("git"), shutil.which("openssl")
        if not git or not openssl:
            self.skipTest("Git and OpenSSL are required for the disposable local fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, repos, credentials = root / "source", root / "repos", root / "credentials"
            source.mkdir()
            repos.mkdir()
            credentials.mkdir()
            def run(args):
                return run_bounded(args, timeout=30, text=True, output_max_bytes=1024 * 1024, check=True)
            run([git, "init", "-q", "-b", "main", str(source)])
            for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.invalid")):
                run([git, "-C", str(source), "config", key, value])
            (source / "README").write_text("base")
            run([git, "-C", str(source), "add", "."])
            run([git, "-C", str(source), "commit", "-qm", "Base"])
            old = run([git, "-C", str(source), "rev-parse", "HEAD"]).stdout.strip()
            run([git, "-C", str(source), "branch", "deploy/staging"])
            run([git, "-C", str(source), "tag", "preserve"])
            manifests = source / "deploy" / "staging"
            manifests.mkdir(parents=True)
            (manifests / "kustomization.yaml").write_text("resources: []\n")
            run([git, "-C", str(source), "add", "."])
            run([git, "-C", str(source), "commit", "-qm", "Tested"])
            revision = run([git, "-C", str(source), "rev-parse", "HEAD"]).stdout.strip()
            run([git, "clone", "--bare", "--quiet", str(source), str(repos / "repository.git")])
            run([git, "--git-dir=" + str(repos / "repository.git"), "config", "http.receivepack", "true"])
            run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=local-test",
                 "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(root / "tls.key"), "-out", str(credentials / "ca.crt")])
            (credentials / "token").write_text("local-fixture-token")
            expected = "Basic " + base64.b64encode(b"cd-writer:local-fixture-token").decode()
            class GitHTTP(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def handle_git(self):
                    if self.headers.get("Authorization") != expected:
                        self.send_response(401)
                        self.send_header("WWW-Authenticate", 'Basic realm="fixture"')
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    url = urlsplit(self.path)
                    size = int(self.headers.get("Content-Length", "0"))
                    if size > 1024 * 1024:
                        raise ValueError("Fixture body too large")
                    env = {**os.environ, "GIT_PROJECT_ROOT": str(repos), "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": url.path,
                           "QUERY_STRING": url.query, "REQUEST_METHOD": self.command, "REMOTE_USER": "cd-writer",
                           "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(size)}
                    result = run_bounded([git, "http-backend"], input=self.rfile.read(size), env=env, timeout=30, output_max_bytes=4 * 1024 * 1024, check=True)
                    headers, body = result.stdout.split(b"\r\n\r\n", 1)
                    fields = [line.decode("ascii").split(":", 1) for line in headers.split(b"\r\n")]
                    self.send_response(next((int(value.strip().split()[0]) for key, value in fields if key.lower() == "status"), 200))
                    for key, value in fields:
                        if key.lower() != "status": self.send_header(key, value.strip())
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                do_GET = handle_git
                do_POST = handle_git
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(credentials / "ca.crt", root / "tls.key", password="")
            with ThreadingHTTPServer(("127.0.0.1", 0), GitHTTP) as server:
                server.socket = context.wrap_socket(server.socket, server_side=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    directory = root / "client"
                    directory.mkdir()
                    remote = handoff.GitBranch("https://127.0.0.1:" + str(server.server_port) + "/repository.git", "cd-writer", credentials, directory)
                    self.assertEqual(remote.prepare("deploy/staging", revision, False), old)
                    remote.check_path(revision, "deploy/staging")
                    self.assertTrue(remote.promote("deploy/staging", revision, old))
                    self.assertFalse(remote.promote("deploy/staging", revision, revision))
                    self.assertEqual(remote.ref("main"), revision)
                    tag = run([git, "--git-dir=" + str(repos / "repository.git"), "rev-parse", "refs/tags/preserve"]).stdout.strip()
                    self.assertEqual(tag, old)
                    with self.assertRaises(handoff.HandoffError):
                        remote.prepare("deploy/staging", old, False)
                    with self.assertRaises(handoff.HandoffError):
                        remote.prepare("deploy/new", revision, False)
                    self.assertIsNone(remote.prepare("deploy/new", revision, True))
                    self.assertTrue(remote.promote("deploy/new", revision, None))
                    self.assertEqual(remote.ref("deploy/new"), revision)
                    # Delete the branch after promote() reads it but before
                    # the server receives the push. The lease must reject
                    # recreating it, even though creation is valid Git syntax.
                    bare = "--git-dir=" + str(repos / "repository.git")
                    run([git, bare, "update-ref", "refs/heads/deploy/race", old])
                    self.assertEqual(remote.prepare("deploy/race", revision, False), old)
                    original_call = remote.call
                    def delete_during_push(args, **kwargs):
                        if args[0] == "push":
                            run([git, bare, "update-ref", "-d", "refs/heads/deploy/race"])
                        return original_call(args, **kwargs)
                    with mock.patch.object(remote, "call", side_effect=delete_during_push), self.assertRaises(handoff.HandoffError):
                        remote.promote("deploy/race", revision, old)
                    self.assertIsNone(remote.ref("deploy/race"))
                    # Exercise the Jenkins adapter against a separate real
                    # HTTPS repository. Jenkins evidence and Argo CD status
                    # are simulated; the Git protocol and branch are real.
                    import jenkins_gerrit_gitops_handoff as jenkins_handoff
                    target = repos / "a" / "team" / "project.git"
                    target.parent.mkdir(parents=True)
                    run([git, "clone", "--bare", "--quiet", str(source), str(target)])
                    run([git, "--git-dir=" + str(target), "config", "http.receivepack", "true"])
                    policy = json.loads((Path(__file__).resolve().parents[1] / "examples/jenkins-gerrit-argocd-handoff.json").read_text())
                    report = policy["report"]
                    report["trigger"]["gerrit_origin"] = "https://127.0.0.1:" + str(server.server_port)
                    report["trigger"]["revision"] = revision
                    plan, _, _, _, _, _, cd = fixture(root)
                    plan["bridge_contract_sha256"] = jenkins_handoff.reporter.reporter.fingerprint(report)
                    obj = cd.objects["application.argoproj.io/application"]
                    obj["spec"]["source"]["repoURL"] = report["trigger"]["gerrit_origin"] + "/a/team/project.git"
                    plan["cd"]["spec_sha256"] = handoff.sha256(obj["spec"])
                    policy["promotion"] = plan
                    evidence = {"revision": revision, "queue_id": 17, "review_vote_verified": True}
                    with mock.patch.object(jenkins_handoff, "verify_target"), \
                         mock.patch.object(jenkins_handoff.reporter, "verify_merged", return_value=evidence), \
                         mock.patch.object(handoff, "cd_observed", return_value=True):
                        result = jenkins_handoff.execute(policy, None, None, None, None, None, cd, git_inputs=credentials)
                        self.assertTrue(result["preflight_verified"])
                        self.assertEqual(run([git, "--git-dir=" + str(target), "rev-parse", "refs/heads/deploy/staging"]).stdout.strip(), old)
                        result = jenkins_handoff.execute(policy, None, None, None, None, None, cd, git_inputs=credentials, apply=True)
                        self.assertTrue(result["branch_push_attempted"])
                        self.assertEqual(run([git, "--git-dir=" + str(target), "rev-parse", "refs/heads/deploy/staging"]).stdout.strip(), revision)
                finally:
                    server.shutdown()
                    thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
