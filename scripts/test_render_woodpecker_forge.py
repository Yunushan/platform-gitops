import copy
import contextlib
import io
import hashlib
import os
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import render_woodpecker_forge as subject
import render_private_platform_values as private_renderer
import render_deployable_gitops_apps as applications
import check_gitops_profile as profiles
import validate_platform_contract as contract
import argparse


class ForgeValuesTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux" and os.environ.get("WOODPECKER_HELM_API_FIXTURE") == "1", "isolated chart admission fixture not requested")
    def test_gitea_woodpecker_pods_admitted_under_restricted_policy(self):
        import test_rook_api_fixture as api_fixture
        helm = Path(os.environ["WOODPECKER_HELM_BINARY"])
        self.assertEqual(hashlib.sha256(helm.read_bytes()).hexdigest(), "c16af303055cfe1c1e64652b547cfdbed924e89fd2a20d8632d797978c14def3")
        chart = Path(__file__).resolve().parents[1] / "gitops/clusters/rke2-main/apps/woodpecker/charts/woodpecker-3.6.5/woodpecker"
        with tempfile.TemporaryDirectory(prefix="woodpecker-chart-", dir="/tmp") as directory:
            root = Path(directory); values = root / "values.yaml"
            with mock.patch.dict(os.environ, {"PLATFORM_WOODPECKER_HOST": "ci.example.invalid", "WOODPECKER_FORGE_PROVIDER": "gitea",
                                             "WOODPECKER_GITEA_URL": "https://gitea.example.invalid", "WOODPECKER_DATABASE_MODE": "sqlite"}, clear=True):
                private_renderer.render_woodpecker(values, {})
            run_env = {"PATH": "/usr/bin:/bin", "HOME": directory, "HELM_CONFIG_HOME": str(root / "config"),
                       "HELM_CACHE_HOME": str(root / "cache"), "HELM_DATA_HOME": str(root / "data")}
            result = subprocess.run([str(helm), "template", "woodpecker", str(chart), "--namespace", "woodpecker", "--values", str(values)],
                                    env=run_env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            objects = [obj for obj in subject.yaml.safe_load_all(result.stdout) if isinstance(obj, dict)]
            workloads = [obj for obj in objects if obj.get("kind") == "StatefulSet" and obj["metadata"]["name"] in {"woodpecker-server", "woodpecker-agent"}]
            self.assertEqual(len(workloads), 2)
            def check(client, _root, kubectl):
                kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "v1", "kind": "Namespace", "metadata": {
                    "name": "woodpecker", "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
                accounts = {obj["spec"]["template"]["spec"].get("serviceAccountName") or "default" for obj in workloads}
                for name in accounts:
                    kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": name, "namespace": "woodpecker"}})
                for workload in workloads:
                    template = copy.deepcopy(workload["spec"]["template"])
                    workload_name = workload["metadata"]["name"]
                    # The StatefulSet controller supplies these volumes when it creates a Pod.
                    volumes = {volume["name"]: volume for volume in template["spec"].get("volumes", [])}
                    for claim in workload["spec"].get("volumeClaimTemplates", []):
                        name = claim["metadata"]["name"]
                        volumes[name] = {"name": name, "persistentVolumeClaim": {"claimName": f"{name}-{workload_name}-0"}}
                    template["spec"]["volumes"] = list(volumes.values())
                    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {**template["metadata"], "name": workload_name + "-0", "namespace": "woodpecker"}, "spec": template["spec"]}
                    result = subprocess.run([*client.prefix, "create", "--dry-run=server", "-f", "-", "-o", "json"],
                                            input=json.dumps(pod), capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    admitted = json.loads(result.stdout)
                    self.assertEqual(admitted["metadata"]["namespace"], "woodpecker")
            api_fixture.exercise(self, management_action=check)

    @unittest.skipUnless(sys.platform == "linux" and os.environ.get("WOODPECKER_HELM_FIXTURE") == "1", "offline Helm fixture not requested")
    def test_vendored_chart_renders_gitea_server_and_secret(self):
        helm = Path(os.environ["WOODPECKER_HELM_BINARY"])
        self.assertEqual(hashlib.sha256(helm.read_bytes()).hexdigest(), "c16af303055cfe1c1e64652b547cfdbed924e89fd2a20d8632d797978c14def3")
        chart = Path(__file__).resolve().parents[1] / "gitops/clusters/rke2-main/apps/woodpecker/charts/woodpecker-3.6.5/woodpecker"
        with tempfile.TemporaryDirectory(prefix="woodpecker-render-", dir="/tmp") as directory:
            root = Path(directory); path = root / "values.yaml"
            env = {"PLATFORM_WOODPECKER_HOST": "ci.example.invalid", "WOODPECKER_FORGE_PROVIDER": "gitea",
                   "WOODPECKER_GITEA_URL": "https://gitea.example.invalid", "WOODPECKER_DATABASE_MODE": "sqlite"}
            with mock.patch.dict(os.environ, env, clear=True): private_renderer.render_woodpecker(path, {})
            run_env = {"PATH": "/usr/bin:/bin", "HOME": directory, "HELM_CONFIG_HOME": str(root / "config"),
                       "HELM_CACHE_HOME": str(root / "cache"), "HELM_DATA_HOME": str(root / "data")}
            result = subprocess.run([str(helm), "template", "woodpecker", str(chart), "--namespace", "woodpecker", "--values", str(path)],
                                    env=run_env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            objects = [obj for obj in subject.yaml.safe_load_all(result.stdout) if isinstance(obj, dict)]
            server = next(obj for obj in objects if obj.get("kind") == "StatefulSet" and obj["metadata"]["name"] == "woodpecker-server")
            container = server["spec"]["template"]["spec"]["containers"][0]
            env_values = {entry["name"]: entry.get("value") for entry in container["env"]}
            self.assertEqual(env_values["WOODPECKER_GITEA"], "true")
            self.assertEqual(env_values["WOODPECKER_GITEA_URL"], "https://gitea.example.invalid")
            self.assertNotIn("WOODPECKER_FORGEJO", env_values)
            self.assertIn("woodpecker-gitea-oauth", [entry["secretRef"]["name"] for entry in container["envFrom"] if "secretRef" in entry])

    def plan(self):
        return {"schema_version": 1, "provider": "gitea", "url": "https://gitea.example.invalid",
                "oauth_secret": "woodpecker-gitea-oauth", "previous_oauth_secret": "woodpecker-forgejo-oauth"}

    def test_switch_preserves_state_and_other_secret_references(self):
        values = {"server": {"statefulSet": {"replicaCount": 1}, "env": {"WOODPECKER_FORGEJO": "true",
                  "WOODPECKER_FORGEJO_URL": "https://old.example.invalid", "WOODPECKER_HOST": "https://ci.example.invalid"},
                  "extraSecretNamesForEnvFrom": ["agent-token", "woodpecker-forgejo-oauth", "database"]},
                  "agent": {"replicaCount": 2}}
        original = copy.deepcopy(values)
        result = subject.render(values, self.plan())
        self.assertEqual(values, original)
        self.assertEqual(result["agent"], original["agent"])
        self.assertEqual(result["server"]["statefulSet"], original["server"]["statefulSet"])
        self.assertEqual(result["server"]["env"], {"WOODPECKER_HOST": "https://ci.example.invalid", "WOODPECKER_GITEA": "true", "WOODPECKER_GITEA_URL": "https://gitea.example.invalid"})
        self.assertEqual(result["server"]["extraSecretNamesForEnvFrom"], ["agent-token", "database", "woodpecker-gitea-oauth"])
        self.assertEqual(subject.render(result, self.plan()), result)

    def test_rejects_credentials_and_invalid_origin(self):
        for url in ("http://gitea.example.invalid", "https://user:password@example.invalid", "https://example.invalid/path", "https://example.invalid?x=1", "https://example.invalid:99999", "https://example.invalid\\evil"):
            plan = self.plan(); plan["url"] = url
            with self.subTest(url=url), self.assertRaises(ValueError): subject.render({"server": {"env": {}}}, plan)

    def test_refuses_inline_oauth_and_multiple_forge_drivers(self):
        for env in ({"WOODPECKER_FORGEJO_SECRET": "private-value"}, {"WOODPECKER_GITEA_CLIENT": "private-value"},
                    {"WOODPECKER_GITLAB": "true"}, {"WOODPECKER_GITHUB": True}):
            with self.subTest(key=next(iter(env))), self.assertRaises(ValueError):
                subject.render({"server": {"env": env}}, self.plan())

    def test_private_renderer_explicitly_selects_gitea_and_is_idempotent(self):
        env = {"PLATFORM_WOODPECKER_HOST": "ci.example.invalid", "WOODPECKER_FORGE_PROVIDER": "gitea",
               "WOODPECKER_GITEA_URL": "https://gitea.example.invalid", "WOODPECKER_DATABASE_MODE": "sqlite"}
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, env, clear=True):
            path = Path(directory) / "values.yaml"
            self.assertTrue(private_renderer.render_woodpecker(path, {}))
            values = subject.yaml.safe_load(path.read_text())
            self.assertEqual(values["server"]["env"]["WOODPECKER_GITEA_URL"], env["WOODPECKER_GITEA_URL"])
            self.assertNotIn("WOODPECKER_FORGEJO", values["server"]["env"])
            self.assertIn("woodpecker-gitea-oauth", values["server"]["extraSecretNamesForEnvFrom"])
            self.assertTrue(subject.gitea_values_ready(path))
            self.assertFalse(private_renderer.render_woodpecker(path, {}))

    def test_profile_check_rejects_forgejo_or_missing_gitea_oauth_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "values.yaml"
            path.write_text("server:\n  env:\n    WOODPECKER_FORGEJO: 'true'\n")
            self.assertFalse(subject.gitea_values_ready(path))
            path.write_text("server:\n  env:\n    WOODPECKER_GITEA: 'true'\n    WOODPECKER_GITEA_URL: https://gitea.example.invalid\n  extraSecretNamesForEnvFrom: [agent-token]\n")
            self.assertFalse(subject.gitea_values_ready(path))

    def test_gitea_profile_admits_only_gitea_configured_woodpecker(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            (root / "profiles").mkdir(); (root / "gitops/clusters/rke2-main/projects").mkdir(parents=True)
            app = root / "gitops/clusters/rke2-main/apps/woodpecker"; app.mkdir(parents=True)
            other = root / "gitops/clusters/rke2-main/apps/monitoring"; other.mkdir(parents=True)
            for path, namespace in ((app, "woodpecker"), (other, "monitoring")):
                (path / "kustomization.yaml").write_text("apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nnamespace: " + namespace + "\nhelmCharts: []\n")
            (root / "profiles/gitea-woodpecker-argocd.yaml").write_text(
                "profile: gitea-woodpecker-argocd\nincludes:\n  - gitops/clusters/rke2-main/apps/woodpecker\n  - gitops/clusters/rke2-main/apps/monitoring\n")
            values = app / "values.yaml"
            values.write_text("server:\n  env:\n    WOODPECKER_FORGEJO: 'true'\n")
            args = argparse.Namespace(repo_root=root, applications_file=None, required_path=[], profile="gitea-woodpecker-argocd",
                                      repo_url="https://example.invalid/repo.git", output=root / "output.yaml")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(profiles.check_profile(root, args.profile), 1)
                self.assertEqual(applications.render(args), 0)
            self.assertNotIn("name: woodpecker\n", args.output.read_text())
            values.write_text(subject.yaml.safe_dump(subject.render({"server": {"env": {}}}, self.plan())))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(profiles.check_profile(root, args.profile), 0)
                self.assertEqual(applications.render(args), 0)
            self.assertIn("name: woodpecker\n", args.output.read_text())

    def test_private_renderer_targets_gitea_profile_values_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cluster = root / "gitops/clusters/rke2-main"
            base = cluster / "apps/woodpecker/values.yaml"
            premium = cluster / "premium-3node/apps/woodpecker/values.yaml"
            for path in (base, premium):
                path.parent.mkdir(parents=True)
                path.write_text("server: {}\n", encoding="utf-8")
            gitea = cluster / "alternatives/gitea/values.yaml"
            gitea.parent.mkdir(parents=True)
            template = Path(__file__).resolve().parents[1] / gitea.relative_to(root)
            gitea.write_text(
                template.read_text(encoding="utf-8").replace(
                    "  config:\n",
                    "  config:\n    security:\n      ALLOWED_HOST_LIST: existing.example.invalid:443\n",
                ),
                encoding="utf-8",
            )
            for path in (gitea, base):
                source = Path(__file__).resolve().parents[1] / path.parent.relative_to(root) / "kustomization.yaml"
                (path.parent / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            profile_dir = root / "profiles"
            profile_dir.mkdir()
            profile = "gitea-woodpecker-argocd"
            (profile_dir / f"{profile}.yaml").write_text(
                f"profile: {profile}\nincludes:\n"
                f"  - {gitea.parent.relative_to(root).as_posix()}\n"
                f"  - {base.parent.relative_to(root).as_posix()}\n",
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "PLATFORM_PROFILE": "gitea-woodpecker-argocd",
                "PLATFORM_WOODPECKER_HOST": "ci.example.invalid",
                "WOODPECKER_FORGE_PROVIDER": "gitea",
                "WOODPECKER_GITEA_URL": "https://gitea.example.invalid",
                "WOODPECKER_GITEA_OAUTH_SECRET_NAME": "woodpecker-gitea-oauth",
                "WOODPECKER_SERVER_REPLICAS": "1",
                "PLATFORM_GITEA_HOST": "gitea.example.invalid",
                "GITEA_DATA_SIZE": "20Gi",
                "GITEA_STORAGE_CLASS": "local-test",
            }
            command = [sys.executable, "-B", str(Path(private_renderer.__file__)), "--skip-forgejo",
                       "--skip-step-ca", "--skip-platform-image-integrity"]
            mismatch = subprocess.run(command, cwd=root, env={**env, "WOODPECKER_FORGE_PROVIDER": "forgejo"},
                                      capture_output=True, text=True, timeout=30)
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertIn("WOODPECKER_FORGE_PROVIDER=gitea", mismatch.stderr)
            self.assertEqual(base.read_text(encoding="utf-8"), "server: {}\n")
            result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            settings = subject.yaml.safe_load(base.read_text(encoding="utf-8"))["server"]
            self.assertEqual(settings["env"]["WOODPECKER_GITEA_URL"], env["WOODPECKER_GITEA_URL"])
            self.assertNotIn("WOODPECKER_DATABASE_DRIVER", settings["env"])
            with mock.patch.dict(os.environ, env, clear=True):
                contract.assert_rendered_gitea_woodpecker(base, base.read_text(encoding="utf-8"))
            self.assertEqual(premium.read_text(encoding="utf-8"), "server: {}\n")
            rendered = gitea.read_text(encoding="utf-8")
            self.assertNotRegex(rendered, r"<[A-Z_]+>")
            values = subject.yaml.safe_load(rendered)
            self.assertEqual(values["gitea"]["config"]["server"]["ROOT_URL"], "https://gitea.example.invalid/")
            self.assertEqual(
                values["gitea"]["config"]["security"]["ALLOWED_HOST_LIST"],
                "existing.example.invalid:443, ci.example.invalid:443",
            )
            self.assertEqual(values["persistence"], {"enabled": True, "size": "20Gi", "storageClass": "local-test"})
            self.assertEqual(values["replicaCount"], 1)
            application_file = root / "applications.yaml"
            args = argparse.Namespace(repo_root=root, applications_file=None, required_path=[], profile=profile,
                                      repo_url="https://example.invalid/repo.git", output=application_file)
            with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(applications.render(args), 0)
            names = {app["metadata"]["name"] for app in subject.yaml.safe_load_all(application_file.read_text(encoding="utf-8"))}
            self.assertEqual(names, {"gitea", "woodpecker"})
            rerun = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(rerun.returncode, 0, rerun.stderr)
            self.assertEqual(gitea.read_text(encoding="utf-8"), rendered)

    def test_private_renderer_refuses_insecure_gitea(self):
        env = {"PLATFORM_WOODPECKER_HOST": "ci.example.invalid", "WOODPECKER_FORGE_PROVIDER": "gitea",
               "WOODPECKER_GITEA_URL": "http://gitea.example.invalid", "WOODPECKER_DATABASE_MODE": "sqlite"}
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit): private_renderer.render_woodpecker(Path(directory) / "values.yaml", {})

    def test_cli_rejects_duplicate_yaml_without_printing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); values = root / "values.yaml"; plan = root / "plan.json"
            values.write_text("server:\n  env:\n    WOODPECKER_GITEA: true\n    WOODPECKER_GITEA: false\n")
            plan.write_text(json.dumps(self.plan()))
            result = subprocess.run([sys.executable, "-B", str(Path(subject.__file__)), "--values", str(values), "--plan", str(plan)],
                                    capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")


if __name__ == "__main__": unittest.main()
