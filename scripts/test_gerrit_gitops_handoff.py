#!/usr/bin/env python3
"""Check merged Gerrit handoff gates, exact CI receipts and FF-only CD reuse."""

import contextlib
import copy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError

import gerrit_gitops_handoff as adapter
import render_gerrit_gitops_handoff as renderer
import render_gerrit_promotion_access as access
import tekton_gerrit_reporter as reporter
from test_ci_gitops_handoff import CDClient, Git
from test_tekton_gerrit_reporter import fixture as review_fixture


class Reader:
    def __init__(self, source): self.source = source; self.head = "a" * 40; self.account_id = 7
    def request(self, path):
        if path == "/a/accounts/self": return {"_account_id": self.account_id, "username": "ci-read"}
        if "/branches/" in path: return {"ref": "refs/heads/main", "revision": self.head}
        return self.source.request(path)


def fixture(root, runtime="rke2"):
    contract, template, digest, report_plan, report_digest, ci, run, source, _ = review_fixture(root, runtime)
    reporter.report_run(ci, source, contract, template, digest, report_plan, report_digest, run)
    source.row["status"] = "MERGED"
    reader = Reader(source)
    obj = {"metadata": {"name": "application", "namespace": "argocd", "uid": "88888888-8888-4888-8888-888888888888", "generation": 1},
        "spec": {"source": {"repoURL": contract["plan"]["source"]["clone_url"], "targetRevision": "deploy/staging", "path": "deploy/staging"},
            "destination": {"server": "https://kubernetes.default.svc", "namespace": "application"}, "syncPolicy": {"automated": {"prune": False}}},
        "status": {"reconciledAt": (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(), "health": {"status": "Healthy"}, "sync": {"status": "Synced", "revision": "a" * 40},
            "operationState": {"phase": "Succeeded", "syncResult": {"revision": "a" * 40}}}}
    cd = CDClient(); cd.objects["application.argoproj.io/application"] = obj
    plan = {"schema_version": 1, "runtime": runtime, "bridge_contract_sha256": digest, "reporter_contract_sha256": report_digest,
        "source_read_username": "ci-read", "source_read_account_id": 7, "deployment_branch": "deploy/staging", "allow_create_branch": False, "allow_prune": False,
        "git_username": "cd-writer", "timeout_seconds": 30, "cd": {"kind": "argocd", "management_target": {"kubeconfig_env": "PLATFORM_CD_KUBECONFIG", "context": "cd", "cluster_uid": "77777777-7777-4777-8777-777777777777"},
            "namespace": "argocd", "name": "application", "uid": obj["metadata"]["uid"], "spec_sha256": adapter.handoff.sha256(obj["spec"]), "path": "deploy/staging"}}
    files = renderer.render(root / "reporter", plan); output = root / "handoff"; output.mkdir()
    for k, v in files.items(): (output / k).write_text(v, encoding="utf-8", newline="\n")
    handoff_digest = json.loads(files["handoff-status.json"])["contract_sha256"]
    _, selected, _, _ = adapter.load_bundle(output, handoff_digest)
    return plan, selected, contract, template, ci, cd, reader, run, handoff_digest


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.plan, self.report, self.contract, self.template, self.ci, self.cd, self.api, self.run, self.digest = fixture(self.root)
    def execute(self, *, apply=False):
        return adapter.execute(self.plan, self.report, self.contract, self.template, self.ci, self.cd, self.api, self.run["metadata"]["name"], apply=apply)

    def test_local_inspection_uses_no_credentials_servers_or_proof_directory(self):
        with mock.patch.object(adapter, "ReadAPI") as api, mock.patch.object(adapter, "Client") as kube:
            result = adapter.operate(self.root / "handoff", self.digest, self.run["metadata"]["name"])
        self.assertTrue(result["read_only"]); self.assertFalse(result["controller_verified"]); api.assert_not_called(); kube.assert_not_called()

    def test_only_exact_evidenced_reported_merged_source_head_can_preflight_or_push(self):
        git = Git()
        with mock.patch.object(adapter.handoff, "GitBranch", return_value=git):
            self.assertTrue(self.execute()["read_only"]); self.assertEqual(git.pushes, 0)
            result = self.execute(apply=True)
        self.assertTrue(result["controller_verified"]); self.assertFalse(result["full_stack_verified"]); self.assertEqual(git.pushes, 1)

    def test_unmerged_abandoned_new_head_and_rewritten_merge_commit_never_reach_git(self):
        original = copy.deepcopy(self.api.source.row)
        for mode in ("NEW", "ABANDONED", "head", "merge-commit"):
            self.api.source.row = copy.deepcopy(original); self.api.head = "a" * 40
            if mode in {"NEW", "ABANDONED"}: self.api.source.row["status"] = mode
            elif mode == "head": self.api.head = "b" * 40
            else: self.api.source.row["current_revision"] = "b" * 40
            with self.subTest(mode=mode), mock.patch.object(adapter.handoff, "GitBranch") as git, self.assertRaises(ValueError): self.execute(apply=True)
            git.assert_not_called()

    def test_no_promotion_from_missing_child_copied_or_altered_vote_or_receipt(self):
        objects, source = copy.deepcopy(self.ci.objects), copy.deepcopy(self.api.source.row)
        for mode in ("task", "pod", "receipt", "vote", "copied", "message", "reservation", "reader"):
            self.ci.objects = copy.deepcopy(objects); self.api.source.row = copy.deepcopy(source); self.api.account_id = 7
            if mode == "task": self.ci.objects.pop("taskrun.tekton.dev/application-task")
            if mode == "pod": self.ci.objects.pop("pod/application-pod")
            if mode == "receipt": self.ci.objects.pop(next(k for k in self.ci.objects if k.endswith("-vote")))
            if mode == "vote": self.api.source.row["labels"]["Verified"]["all"][0]["value"] = -1
            if mode == "copied": self.api.source.row["labels"]["Verified"]["all"][0]["copied"] = True
            if mode == "message": self.api.source.row["messages"][0]["message"] = "Another job"
            if mode == "reservation": self.ci.objects[next(k for k in self.ci.objects if k.endswith("-vote-lock"))]["metadata"]["uid"] = "99999999-9999-4999-8999-999999999999"
            if mode == "reader": self.api.account_id = 8
            with self.subTest(mode=mode), mock.patch.object(adapter.handoff, "GitBranch") as git, self.assertRaises(ValueError): self.execute(apply=True)
            git.assert_not_called()

    def test_before_push_rechecks_ci_and_source_and_never_force_rewinds(self):
        git = Git(); original = adapter.verified_ci; calls = []
        def moved(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2: self.api.head = "b" * 40
            return original(*args, **kwargs)
        with mock.patch.object(adapter, "verified_ci", side_effect=moved), mock.patch.object(adapter.handoff, "GitBranch", return_value=git), self.assertRaises(ValueError): self.execute(apply=True)
        self.assertEqual(git.pushes, 0)

    def test_reviewed_application_uid_source_and_pruning_remain_bound(self):
        original = copy.deepcopy(self.cd.objects)
        for mode in ("uid", "source", "prune", "manual"):
            self.cd.objects = copy.deepcopy(original); obj = self.cd.objects["application.argoproj.io/application"]
            if mode == "uid": obj["metadata"]["uid"] = "99999999-9999-4999-8999-999999999999"
            if mode == "source": obj["spec"]["source"]["targetRevision"] = "main"
            if mode == "prune": obj["spec"]["syncPolicy"]["automated"]["prune"] = True
            if mode == "manual": obj["spec"]["syncPolicy"].pop("automated")
            with self.subTest(mode=mode), mock.patch.object(adapter.handoff, "GitBranch") as git, self.assertRaises(ValueError): self.execute(apply=True)
            git.assert_not_called()

    def test_five_packages_preserve_submission_and_reporting_contracts(self):
        for runtime in adapter.bridge.RUNTIMES:
            with tempfile.TemporaryDirectory() as d:
                root = Path(d); plan, selected, contract, template, ci, cd, api, run, digest = fixture(root, runtime)
                status = json.loads((root / "handoff/handoff-status.json").read_text())
                self.assertEqual(status["runtime"], runtime); self.assertTrue(status["merged_exact_head_required"])
                self.assertFalse(status["controller_verified"]); self.assertFalse(status["full_stack_verified"])
                self.assertEqual(plan["bridge_contract_sha256"], selected["plan"]["source_contract_sha256"])
        (self.root / "handoff/gerrit_gitops_handoff.py").write_text("changed")
        with self.assertRaises(ValueError): adapter.load_bundle(self.root / "handoff", self.digest)

    def test_plan_cannot_use_reporter_writer_credentials_or_change_runtime(self):
        for field, value in (("git_username", self.report["plan"]["http_username"]), ("git_username", self.report["plan"]["http_username"].upper()), ("source_read_username", "CD-WRITER"), ("source_read_username", "cd-writer"), ("source_read_account_id", self.report["plan"]["account_id"]), ("source_read_account_id", True), ("runtime", "raw")):
            with self.subTest(field=field), self.assertRaises(ValueError): adapter.validate({**self.plan, field: value}, self.report, self.contract)

    def test_cli_error_does_not_echo_private_values(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            code = adapter.main(["--bundle-dir", str(self.root / "private-name"), "--contract-sha256", "bad", "--pipeline-run", "job"])
        self.assertEqual(code, 1); self.assertNotIn("private-name", output.getvalue()); self.assertNotIn("Traceback", output.getvalue())

    def test_failure_diagnostic_excludes_private_exception_messages_paths_and_locals(self):
        marker = "private-token-marker"
        try:
            adapter.verified_ci(None, None, None, None, None, None, marker)
        except Exception as error:
            diagnostic = adapter.failure_diagnostic(error)
        self.assertEqual(diagnostic["exception_type"], "other")
        self.assertTrue(any(row["file"] == "gerrit_gitops_handoff.py" and row["function"] == "verified_ci" for row in diagnostic["code_locations"]))
        self.assertNotIn(marker, json.dumps(diagnostic))
        self.assertNotIn(str(self.root), json.dumps(diagnostic))
        for error in (TimeoutError(marker), PermissionError(marker), ValueError(marker)):
            self.assertEqual(adapter.failure_diagnostic(error), {"exception_type": type(error).__name__, "code_locations": []})
        kubernetes = adapter.failure_diagnostic(adapter.KubernetesOperationError(1, "Forbidden: " + marker))
        self.assertTrue(kubernetes["kubernetes"]["signals"]["forbidden"])
        self.assertNotIn(marker, json.dumps(kubernetes))
        with HTTPError("https://private.invalid/" + marker, 401, marker,
                       {"Authorization": marker}, io.BytesIO(marker.encode())) as http:
            self.assertEqual(adapter.failure_diagnostic(http),
                             {"exception_type": "HTTPError", "code_locations": [], "http_status": 401})
        # Code filenames and function names outside the packaged program allowlist
        # are excluded even when exception tracebacks carry private identifiers.
        try:
            exec(compile("raise ValueError('" + marker + "')", "/private/" + marker + ".py", "exec"))
        except ValueError as error:
            self.assertEqual(adapter.failure_diagnostic(error), {"exception_type": "ValueError", "code_locations": []})

    @unittest.skipUnless(os.environ.get("GERRIT_HANDOFF_TEST_RUNTIME") == "1", "Explicit real Gerrit merge and deployment-branch HTTPS Git protocol")
    def test_real_gerrit_merged_commit_handoff(self):
        from test_gerrit_review_runtime_fixture import exercise
        exercise(self, handoff_check=True)

    @unittest.skipUnless(os.environ.get("GERRIT_ARGOCD_TEST_RUNTIME") == "1", "Explicit native Gerrit and actual Argo CD reconciliation")
    def test_real_gerrit_handoff_and_argocd_reconciliation(self):
        from test_gerrit_review_runtime_fixture import exercise
        exercise(self, handoff_check=True, argocd_check=True)


class PromotionAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.plan, self.report, self.contract, _, _, _, _, _, self.digest = fixture(self.root)
        self.policy = {"schema_version": 1,
            "ci": {"account": "promotion-ci-reader", "namespace": self.contract["plan"]["submitter_namespace"], "workers": ["worker-a", "worker-b.example.invalid"]},
            "cd": {"account": "promotion-cd-reader", "namespace": "argocd"}}

    def render(self, policy=None):
        return access.render(self.root / "handoff", self.digest, self.policy if policy is None else policy)

    def save(self, files):
        output = self.root / "access"; output.mkdir(exist_ok=True)
        for name, value in files.items(): (output / name).write_text(value, encoding="utf-8", newline="\n")
        return output

    def test_roles_only_read_exact_evidence_scope_and_named_cd_application(self):
        files = self.render()
        for side in ("ci", "cd"):
            objects = json.loads(files[side + "-reader-rbac.json"])["items"]
            self.assertEqual([o["kind"] for o in objects], ["ServiceAccount", "Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"])
            account, role, binding, cluster, cluster_binding = objects
            self.assertIs(account["automountServiceAccountToken"], False)
            self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": self.policy[side]["account"], "namespace": self.policy[side]["namespace"]}])
            self.assertEqual(cluster_binding["subjects"], binding["subjects"])
            for obj in (role, cluster):
                for rule in obj["rules"]:
                    self.assertEqual(rule["verbs"], ["get"])
                    self.assertFalse({"secrets", "*", "pipelineruns/status", "applications/status"} & set(rule["resources"]))
                self.assertFalse(obj.get("aggregationRule"))
            if side == "ci":
                self.assertEqual(role["metadata"]["namespace"], self.contract["plan"]["namespace"])
                self.assertNotEqual(binding["metadata"]["namespace"], binding["subjects"][0]["namespace"])
                self.assertEqual(role["rules"], [
                    {"apiGroups": ["tekton.dev"], "resources": ["pipelineruns", "taskruns"], "verbs": ["get"]},
                    {"apiGroups": [""], "resources": ["pods", "configmaps"], "verbs": ["get"]}])
                self.assertEqual(cluster["rules"][1]["resourceNames"], self.policy["ci"]["workers"])
                self.assertEqual(cluster["rules"][0]["resourceNames"], ["kube-system", self.contract["plan"]["namespace"]])
            else:
                self.assertEqual(role["rules"], [{"apiGroups": ["argoproj.io"], "resources": ["applications"], "resourceNames": ["application"], "verbs": ["get"]}])
                self.assertEqual(cluster["rules"], [{"apiGroups": [""], "resources": ["namespaces"], "resourceNames": ["kube-system"], "verbs": ["get"]}])
        status = json.loads(files["access-status.json"])
        for flag in ("applied", "credentials_created", "authorizations_verified", "controller_verified", "full_stack_verified"): self.assertIs(status[flag], False)

    def test_separate_cluster_targets_remain_pinned_for_all_five_hosting_formats(self):
        for runtime in adapter.bridge.RUNTIMES:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); _, _, contract, _, _, _, _, _, digest = fixture(root, runtime)
                policy = copy.deepcopy(self.policy); policy["ci"]["namespace"] = contract["plan"]["submitter_namespace"]
                files = access.render(root / "handoff", digest, policy)
                selected = json.loads(files["promotion-access-contract.json"])
                self.assertNotEqual(selected["targets"]["ci"]["cluster_uid"], selected["targets"]["cd"]["cluster_uid"])
                for side in ("ci", "cd"):
                    for obj in json.loads(files[side + "-reader-rbac.json"])["items"]:
                        self.assertEqual(obj["metadata"]["annotations"]["platform.gitops/access-target-cluster-uid"], selected["targets"][side]["cluster_uid"])
                self.assertEqual(json.loads(files["access-status.json"])["runtime"], runtime)

    def test_refuses_wildcards_system_accounts_unknown_fields_and_writer_reuse(self):
        policies = []
        for side, field, value in (("ci", "workers", []), ("ci", "workers", ["*"]), ("ci", "workers", ["worker-a", "worker-a"]),
                ("ci", "workers", ["worker..invalid"]), ("ci", "workers", [True]), ("ci", "namespace", "other-control"),
                ("cd", "namespace", "other-argocd"), ("cd", "account", "default"), ("cd", "token", "private-value"),
                ("ci", "account", self.report["plan"]["account"]), ("ci", "account", self.contract["plan"]["submitter_account"]),
                ("ci", "account", self.contract["plan"]["service_account"])):
            policy = copy.deepcopy(self.policy); policy[side][field] = value; policies.append(policy)
        policies.extend([{**self.policy, "schema_version": True}, {**self.policy, "apply": True}])
        for policy in policies:
            with self.subTest(policy=policy), self.assertRaises(ValueError): self.render(policy)

    def test_same_cluster_cannot_reuse_ci_identity_for_cd(self):
        plan = copy.deepcopy(self.plan); plan["cd"]["management_target"] = self.contract["plan"]["management_target"]
        plan["cd"]["namespace"] = self.contract["plan"]["submitter_namespace"]
        files = renderer.render(self.root / "reporter", plan)
        for name, value in files.items(): (self.root / "handoff" / name).write_text(value, encoding="utf-8", newline="\n")
        digest = json.loads(files["handoff-status.json"])["contract_sha256"]
        for account in (self.policy["ci"]["account"], self.report["plan"]["account"], self.contract["plan"]["submitter_account"]):
            policy = copy.deepcopy(self.policy); policy["cd"] = {"account": account, "namespace": plan["cd"]["namespace"]}
            with self.subTest(account=account), self.assertRaises(ValueError): access.render(self.root / "handoff", digest, policy)

    def test_package_rejects_rehashed_broader_permissions_or_changed_source_bundle(self):
        files = self.render(); output = self.save(files)
        digest = json.loads(files["access-status.json"])["contract_sha256"]
        access.load_bundle(output, digest, self.root / "handoff", self.digest)
        manifest = json.loads(files["ci-reader-rbac.json"]); manifest["items"][1]["rules"][0]["verbs"].append("patch")
        text = access.canonical(manifest); (output / "ci-reader-rbac.json").write_text(text, encoding="utf-8", newline="\n")
        selected = json.loads(files["promotion-access-contract.json"]); selected["files_sha256"]["ci-reader-rbac.json"] = access.digest(text)
        text = access.canonical(selected); (output / "promotion-access-contract.json").write_text(text, encoding="utf-8", newline="\n")
        with self.assertRaises(ValueError): access.load_bundle(output, access.digest(text), self.root / "handoff", self.digest)
        self.save(files)
        (self.root / "handoff/gerrit_gitops_handoff.py").write_text("changed", encoding="utf-8")
        with self.assertRaises(ValueError): access.load_bundle(output, digest, self.root / "handoff", self.digest)

    def test_cli_writes_only_new_private_package_without_credentials_or_servers(self):
        policy = self.root / "policy.json"; policy.write_text(json.dumps(self.policy), encoding="utf-8")
        output = self.root / "private/generated/access"; log = io.StringIO()
        arguments = ["--bundle-dir", str(self.root / "handoff"), "--contract-sha256", self.digest, "--plan", str(policy), "--output-dir", str(output)]
        with mock.patch.object(access, "ROOT", self.root), mock.patch.object(adapter, "Client") as client, mock.patch.object(adapter, "ReadAPI") as api, contextlib.redirect_stdout(log):
            self.assertEqual(access.main(arguments), 0); client.assert_not_called(); api.assert_not_called()
        original = {path.name: path.read_bytes() for path in output.iterdir()}
        with mock.patch.object(access, "ROOT", self.root), contextlib.redirect_stderr(log): self.assertEqual(access.main(arguments), 1)
        self.assertEqual(original, {path.name: path.read_bytes() for path in output.iterdir()})
        self.assertNotIn(str(self.root), log.getvalue()); self.assertNotIn(self.policy["ci"]["account"], log.getvalue())


if __name__ == "__main__": unittest.main()
