import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml
import render_gitlab_praefect as renderer
import render_private_gitlab_values as private_values
from render_deployable_gitops_apps import selected_application_documents
from check_gitops_profile import check_kustomization_structure, profile_source_paths


def plan():
    return {"schema_version": 1, "fresh_install": True, "virtual_storage": "default",
            "storage_class": "replicated-block", "storage_size_gi": 100,
            "postgres_host": "postgres.internal.example", "postgres_port": 5432,
            "postgres_database": "praefect", "postgres_user": "praefect",
            "postgres_secret": "praefect-db", "postgres_secret_key": "password",
            "postgres_ca_secret": "postgres-ca", "gitaly_auth_secret": "gitaly-auth",
            "praefect_auth_secret": "praefect-auth"}


def tls_plan():
    return dict(plan(), schema_version=2, gitaly_tls_secret="gitaly-tls",
                praefect_tls_secret="praefect-tls", repository_ca_secret="repository-ca")


class PraefectOverlayTest(unittest.TestCase):
    def test_gitlab_profile_registers_inherited_postgres_and_valkey(self):
        documents = selected_application_documents(renderer.ROOT, private_values.PROFILE,
                                                   "https://example.invalid/platform.git")
        apps = {doc["metadata"]["name"]: doc["spec"] for doc in map(yaml.safe_load, documents)}
        self.assertIn("platform-postgres", apps)
        self.assertIn("platform-valkey", apps)
        self.assertEqual(apps["gitlab-ce"]["project"], "platform-gitlab")
        self.assertEqual(apps["gitlab-runner"]["project"], "platform-gitlab")
        self.assertTrue({"forgejo", "woodpecker"}.isdisjoint(apps))
        paths = profile_source_paths(renderer.ROOT, private_values.PROFILE)
        self.assertIn(renderer.ROOT / private_values.APPS / "platform-postgres", paths)

    def test_private_profile_wires_tls_and_network_policy_and_preserves_existing_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gitlab = root / private_values.APPS / "gitlab-ce"
            runner = root / private_values.APPS / "gitlab-runner"
            for directory in (gitlab, runner):
                directory.mkdir(parents=True)
                original = renderer.ROOT / directory.relative_to(root)
                for filename in ("values.yaml", "kustomization.yaml"):
                    (directory / filename).write_bytes((original / filename).read_bytes())
            app = gitlab / "kustomization.yaml"
            config = yaml.safe_load(app.read_text())
            config["helmCharts"][0]["additionalValuesFiles"] = ["site.yaml"]
            app.write_text(yaml.safe_dump(config))
            (gitlab / "site.yaml").write_text(
                "global:\n  certificates:\n    customCAs:\n      - secret: site-ca\n",
            )
            env = {name: "ref-" + name.removesuffix("_SECRET_NAME").lower().replace("_", "-")
                   for name in private_values.SECRET_FIELDS}
            base = private_values.prepare(root, "platform.example.invalid", env)
            self.assertEqual(set(base), {gitlab / "values.yaml", runner / "values.yaml"})
            self.assertEqual(yaml.safe_load(base[runner / "values.yaml"])["gitlabUrl"],
                             "https://gitlab.platform.example.invalid")

            plan_path = root / "private" / "plan.json"
            plan_path.parent.mkdir()
            env["GITLAB_PRAEFECT_PLAN_FILE"] = "private/plan.json"
            edge = {
                "schema_version": 1,
                "gitlab_ssh": {"hostname": "ssh.example.invalid", "port": 2222, "node_port": 30222},
                "entry_hosts": [{"address": "192.0.2.10"}, {"address": "192.0.2.11"}],
                "backends": [{"address": "192.0.2.20"}, {"address": "192.0.2.21"}],
            }
            (root / "private/edge.json").write_text(json.dumps(edge))
            env["GITLAB_SSH_EDGE_PLAN_FILE"] = "private/edge.json"
            postgres = root / private_values.CNPG_APP
            shutil.copytree(renderer.ROOT / private_values.CNPG_APP, postgres)
            env["GITLAB_CNPG_MANAGED_DATABASES"] = "true"
            plan_path.write_text(json.dumps(dict(tls_plan(), schema_version=3)))
            external = private_values.prepare(root, "platform.example.invalid", env)
            self.assertEqual([obj["kind"] for obj in yaml.safe_load_all(
                external[postgres / private_values.CNPG_RESOURCES])], ["DatabaseRole", "Database"])
            plan_path.write_text(json.dumps(dict(tls_plan(), schema_version=3, postgres_user="gitlab")))
            with self.assertRaisesRegex(ValueError, "dedicated database"):
                private_values.prepare(root, "platform.example.invalid", env)
            plan_path.write_text(json.dumps(dict(tls_plan(), schema_version=3,
                                                 postgres_host=private_values.CNPG_HOST)))
            changed = private_values.prepare(root, "platform.example.invalid", env)
            declarations = list(yaml.safe_load_all(changed[postgres / private_values.CNPG_RESOURCES]))
            roles = [obj["spec"] for obj in declarations if obj["kind"] == "DatabaseRole"]
            databases = [obj["spec"] for obj in declarations if obj["kind"] == "Database"]
            self.assertEqual([role["name"] for role in roles], ["gitlab", "praefect"])
            self.assertEqual([role["passwordSecret"]["name"] for role in roles],
                             [env["GITLAB_POSTGRES_SECRET_NAME"], "praefect-db"])
            self.assertTrue(all(role["databaseRoleReclaimPolicy"] == "retain" and
                                not role["superuser"] and not role["createdb"] for role in roles))
            self.assertEqual([db["owner"] for db in databases], ["gitlab", "praefect"])
            self.assertEqual({ext["name"] for ext in databases[0]["extensions"]}, {"amcheck", "pg_trgm", "btree_gist"})
            self.assertTrue(all(db["databaseReclaimPolicy"] == "retain" for db in databases))
            cluster = (postgres / "postgres-cluster.yaml").read_text(encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "dedicated"):
                private_values.prepare(root, "platform.example.invalid", env,
                                       cnpg_cluster_manifest=cluster.replace("owner: forgejo", "owner: gitlab"))
            config = yaml.safe_load(changed[app])
            self.assertEqual(config["helmCharts"][0]["additionalValuesFiles"], ["site.yaml", "praefect-values.yaml"])
            self.assertEqual(config["components"][-1], "repository-network")
            self.assertIn(private_values.SSH_POLICY, config["resources"])
            values = yaml.safe_load(changed[gitlab / "values.yaml"])
            self.assertEqual(values["global"]["hosts"]["ssh"], "ssh.example.invalid")
            self.assertEqual(values["global"]["shell"]["port"], 2222)
            self.assertEqual(values["gitlab"]["gitlab-shell"]["service"]["nodePort"], 30222)
            policy = yaml.safe_load(changed[gitlab / private_values.SSH_POLICY])
            self.assertEqual(policy["spec"]["ingress"][0]["from"],
                             [{"ipBlock": {"cidr": ip + "/32"}} for ip in ("192.0.2.10", "192.0.2.11", "192.0.2.20", "192.0.2.21")])
            overlay = yaml.safe_load(changed[gitlab / "praefect-values.yaml"])
            trust_sources = overlay["global"]["certificates"]["customCAs"]
            self.assertEqual(trust_sources[0], {"configMap": "platform-internal-roots", "keys": ["ca-certificates.crt"]})
            self.assertEqual([ref["secret"] for ref in trust_sources[1:]], ["site-ca", "postgres-ca", "repository-ca"])
            for path, content in changed.items():
                path.parent.mkdir(exist_ok=True)
                path.write_text(content, encoding="utf-8")
            findings = check_kustomization_structure(app, root)
            self.assertTrue(any("missing local Helm chart" in item or "missing Helm chart home" in item
                                for item in findings))
            self.assertFalse(any("without Kustomization: repository-network" in item for item in findings))
            self.assertEqual(private_values.prepare(root, "", env), changed)
            del env["GITLAB_PRAEFECT_PLAN_FILE"]
            del env["GITLAB_CNPG_MANAGED_DATABASES"]
            resumed = private_values.prepare(root, "", env)
            self.assertEqual(resumed[postgres / private_values.CNPG_RESOURCES],
                             changed[postgres / private_values.CNPG_RESOURCES])
            (gitlab / "praefect-values.yaml").unlink()
            self.assertTrue(any("missing Helm valuesFile: praefect-values.yaml" in item
                                for item in check_kustomization_structure(app, root)))

    @unittest.skipUnless(os.environ.get("GITLAB_BASE_RENDER"), "no rendered base chart supplied")
    def test_premium_base_renders_external_backup_without_bundled_gateways(self):
        path = Path(os.environ["GITLAB_BASE_RENDER"])
        docs = [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(doc, dict)]
        resources = {(doc.get("kind"), doc["metadata"]["name"]): doc for doc in docs}
        self.assertEqual(resources[("StatefulSet", "gitlab-gitaly")]["spec"]["replicas"], 1)
        self.assertNotIn(("StatefulSet", "gitlab-praefect"), resources)
        self.assertFalse(any("Gateway" in kind for kind, _ in resources))
        self.assertFalse(any(kind == "Deployment" and "certmanager" in name for kind, name in resources))
        backup = resources[("CronJob", "gitlab-toolbox-backup")]
        self.assertIn("<GITLAB_BACKUP_STORAGE_SECRET_NAME>", str(backup))
        migrations = next(doc for (kind, name), doc in resources.items()
                          if kind == "Job" and name.startswith("gitlab-migrations-"))
        workloads = (
            (resources[("Deployment", "gitlab-webservice-default")]["spec"], "webservice"),
            (resources[("Deployment", "gitlab-sidekiq-all-in-1-v2")]["spec"], "sidekiq"),
            (migrations["spec"], "migrations"),
            (backup["spec"]["jobTemplate"]["spec"], "toolbox-backup"),
        )
        for workload, container_name in workloads:
            pod = workload["template"]["spec"]
            container = next(item for item in pod["containers"] if item["name"] == container_name)
            env = {item["name"]: item.get("value") for item in container["env"]}
            self.assertEqual(env["PGSSLMODE"], "verify-full")
            self.assertEqual(env["PGSSLROOTCERT"], "/etc/ssl/certs/ca-certificates.crt")
            projected = next(item for item in pod["volumes"] if item["name"] == "custom-ca-certificates")
            self.assertTrue(any(source.get("configMap", {}).get("name") == "platform-internal-roots"
                                for source in projected["projected"]["sources"]))
        for doc in docs:
            if doc.get("kind") == "Ingress":
                self.assertEqual(doc["spec"]["ingressClassName"], "traefik")
        for name in ("gitlab-webservice-default", "gitlab-registry", "gitlab-kas"):
            ingress = resources[("Ingress", name)]["spec"]
            host = ingress["rules"][0]["host"]
            self.assertIn({"hosts": [host], "secretName": "gitlab-tls"}, ingress["tls"])
        for kind, api in (("PodDisruptionBudget", "policy/v1"), ("HorizontalPodAutoscaler", "autoscaling/v2"),
                          ("Ingress", "networking.k8s.io/v1"), ("CronJob", "batch/v1")):
            self.assertEqual({doc["apiVersion"] for doc in docs if doc.get("kind") == kind}, {api})

    def test_three_replicas_with_scoped_secrets_and_database_tls(self):
        values = yaml.safe_load(renderer.render(plan())["praefect-values.yaml"])
        praefect = values["global"]["praefect"]
        self.assertEqual(praefect["virtualStorages"][0]["gitalyReplicas"], 3)
        self.assertEqual(praefect["virtualStorages"][0]["defaultReplicationFactor"], 3)
        self.assertEqual(praefect["psql"]["sslMode"], "verify-full")
        self.assertEqual(values["gitlab"]["praefect"]["replicas"], 3)
        self.assertEqual(values["gitlab"]["gitaly"]["antiAffinity"], "hard")
        self.assertEqual(values["gitlab"]["praefect"]["antiAffinity"], "hard")

    def test_repository_tls_secrets_are_explicit_and_distinct(self):
        values = yaml.safe_load(renderer.render(tls_plan())["praefect-values.yaml"])["global"]
        self.assertTrue(values["gitaly"]["tls"]["enabled"])
        self.assertEqual(values["praefect"]["tls"], {"enabled": True, "secretName": "praefect-tls"})
        self.assertEqual(values["praefect"]["virtualStorages"][0]["tlsSecretName"], "gitaly-tls")
        self.assertEqual(values["certificates"]["customCAs"], [
            {"secret": "postgres-ca", "keys": ["postgres-ca.crt"]},
            {"secret": "repository-ca", "keys": ["repository-ca.crt"]},
        ])
        for invalid in (dict(tls_plan(), praefect_tls_secret="gitaly-tls"),
                        dict(tls_plan(), gitaly_tls_secret=["invalid"]), dict(plan(), schema_version=2)):
            with self.assertRaises(ValueError):
                renderer.render(invalid)

    def test_rejects_ambiguous_or_nonfresh_plans(self):
        for key, value in (("fresh_install", False), ("virtual_storage", "other"), ("postgres_host", "https://bad"),
                           ("postgres_port", True), ("storage_size_gi", 0),
                           ("postgres_database", "x' injected"), ("praefect_auth_secret", "gitaly-auth")):
            candidate = copy.deepcopy(plan())
            candidate[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                renderer.render(candidate)
        candidate = plan()
        candidate["extra"] = "ignored"
        with self.assertRaises(ValueError):
            renderer.render(candidate)

    def test_repository_isolation_is_opt_in_and_restricts_the_shared_allow_policy(self):
        legacy = renderer.render(tls_plan())
        self.assertNotIn("repository-network-policies.yaml", legacy)
        files = renderer.render(dict(tls_plan(), schema_version=3))
        values = yaml.safe_load(files["praefect-values.yaml"])["gitlab"]
        for role in ("gitaly", "praefect"):
            self.assertEqual(values[role]["podLabels"][renderer.REPOSITORY_TIER_LABEL], "true")
        component = yaml.safe_load(files["kustomization.yaml"])
        patch = component["patches"][0]
        self.assertEqual(patch["target"]["name"], "platform-allow-same-namespace")
        operations = yaml.safe_load(patch["patch"])
        self.assertIn({"op": "remove", "path": "/spec/ingress"}, operations)
        policies = list(yaml.safe_load_all(files["repository-network-policies.yaml"]))
        ingress = {doc["metadata"]["name"]: doc["spec"] for doc in policies}
        ordinary = ingress["gitlab-allow-same-namespace-ingress"]
        self.assertEqual(ordinary["podSelector"]["matchExpressions"], [
            {"key": renderer.REPOSITORY_TIER_LABEL, "operator": "NotIn", "values": ["true"]},
        ])
        for role, apps in (("gitaly", ["gitaly", "praefect"]),
                           ("praefect", ["gitaly", "gitlab-shell", "sidekiq", "toolbox", "webservice"])):
            tls, metrics = ingress[f"gitlab-allow-{role}-ingress"]["ingress"]
            self.assertEqual(tls["from"], [{"podSelector": {
                "matchLabels": {"release": "gitlab"},
                "matchExpressions": [{"key": "app", "operator": "In", "values": apps}],
            }}])
            self.assertEqual(tls["ports"], [{"protocol": "TCP", "port": 8076}])
            self.assertEqual(metrics["ports"], [{"protocol": "TCP", "port": 9236}])

    @unittest.skipUnless(os.environ.get("GITLAB_ISOLATION_RENDER"), "no rendered Kustomize application supplied")
    def test_premium_application_renders_repository_ingress_and_pod_labels(self):
        path = Path(os.environ["GITLAB_ISOLATION_RENDER"])
        docs = [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(doc, dict)]
        resources = {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}
        shared = resources["NetworkPolicy", "platform-allow-same-namespace"]["spec"]
        self.assertEqual(shared, {"podSelector": {}, "policyTypes": ["Egress"],
                                  "egress": [{"to": [{"podSelector": {}}]}]})
        baseline = resources["NetworkPolicy", "platform-allow-ingress"]["spec"]["ingress"]
        self.assertTrue(all("ports" in rule for rule in baseline))
        self.assertFalse({8075, 8076} & {port["port"] for rule in baseline for port in rule["ports"]})
        for role, name in (("gitaly", "gitlab-gitaly-default"), ("praefect", "gitlab-praefect")):
            policy = resources["NetworkPolicy", f"gitlab-allow-{role}-ingress"]
            workload = resources["StatefulSet", name]
            self.assertEqual(policy["metadata"]["namespace"], workload["metadata"]["namespace"])
            pod = workload["spec"]["template"]
            for key, value in policy["spec"]["podSelector"]["matchLabels"].items():
                self.assertEqual(pod["metadata"]["labels"][key], value)

    @unittest.skipUnless(os.environ.get("GITLAB_ISOLATION_RENDER"), "no rendered Kustomize application supplied")
    def test_gitlab_sync_waves_and_ingress_follow_rendered_dependencies(self):
        path = Path(os.environ["GITLAB_ISOLATION_RENDER"])
        docs = [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(doc, dict)]
        resources = {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}

        def order(obj):
            annotations = obj["metadata"].get("annotations", {})
            self.assertIn(annotations.get("argocd.argoproj.io/hook"), (None, "Sync"))
            return int(annotations.get("argocd.argoproj.io/sync-wave", 0))

        namespace = resources["Namespace", "gitlab"]
        guard = resources["Job", "gitlab-gitlab-upgrade-check"]
        guard_script = resources["ConfigMap", "gitlab-gitlab-upgrade-check"]
        secrets_job = next(doc for (kind, name), doc in resources.items()
                           if kind == "Job" and name.startswith("gitlab-shared-secrets-"))
        for kind in ("ServiceAccount", "Role", "RoleBinding", "ConfigMap"):
            self.assertLess(order(resources[kind, "gitlab-shared-secrets"]), order(secrets_job))
        self.assertLess(order(namespace), order(guard_script))
        self.assertLess(order(guard_script), order(guard))
        self.assertLess(order(guard), order(secrets_job))
        self.assertLess(order(secrets_job), order(resources["ConfigMap", "gitlab-gitlab-chart-info"]))
        api_policy = resources["NetworkPolicy", "gitlab-allow-bootstrap-api"]
        default_deny = resources["NetworkPolicy", "platform-default-deny"]
        self.assertLess(order(namespace), order(default_deny))
        self.assertLess(order(default_deny), order(api_policy))
        self.assertLess(order(api_policy), order(guard))
        for job in (guard, secrets_job):
            pod = job["spec"]["template"]
            self.assertEqual(api_policy["spec"]["podSelector"]["matchLabels"],
                             {"platform.gitops/gitlab-bootstrap": "true"})
            self.assertEqual(pod["metadata"]["labels"]["platform.gitops/gitlab-bootstrap"], "true")
        guard_pod = guard["spec"]["template"]["spec"]
        self.assertEqual(guard_pod["serviceAccountName"], "gitlab-upgrade-check")
        self.assertEqual(guard_pod["initContainers"][0]["image"],
                         secrets_job["spec"]["template"]["spec"]["containers"][0]["image"])
        role = resources["Role", "gitlab-upgrade-check"]
        self.assertTrue(all(rule["verbs"] == ["get"] and rule["resourceNames"] for rule in role["rules"]))
        lookup_names = set(re.findall(r"(?:configmap|deployment|statefulset|pvc)/([a-z0-9-]+)",
                                      guard_pod["initContainers"][0]["command"][2]))
        self.assertEqual(lookup_names, {name for rule in role["rules"] for name in rule["resourceNames"]})

        ingress_rule = resources["NetworkPolicy", "gitlab-allow-traefik-backends"]["spec"]["ingress"][0]
        service_ports = set()
        for doc in docs:
            if doc["kind"] != "Ingress":
                continue
            for rule in doc["spec"]["rules"]:
                for path in rule["http"]["paths"]:
                    backend = path["backend"]["service"]
                    service = resources["Service", backend["name"]]
                    service_ports.add(next(port["port"] for port in service["spec"]["ports"]
                                           if port["port"] == backend["port"]["number"]))
        self.assertEqual(service_ports, {entry["port"] for entry in ingress_rule["ports"]})

    @unittest.skipUnless(os.environ.get("GITLAB_ISOLATION_RENDER"), "no rendered Kustomize application supplied")
    def test_praefect_waits_for_the_one_migration_job_it_can_read(self):
        path = Path(os.environ["GITLAB_ISOLATION_RENDER"])
        resources = {(doc["kind"], doc["metadata"]["name"]): doc
                     for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(doc, dict)}
        job, = (doc for (kind, name), doc in resources.items()
                if kind == "Job" and name.startswith("gitlab-praefect-migrate-"))
        job_name = job["metadata"]["name"]
        job_spec = job["spec"]
        job_template = job_spec["template"]
        migrator, = job_template["spec"]["containers"]
        workload = resources["StatefulSet", "gitlab-praefect"]
        router_template = workload["spec"]["template"]
        router_pod = router_template["spec"]
        router, = (container for container in router_pod["containers"] if container["name"] == "praefect")
        gate = router_pod["initContainers"][0]
        account = router_pod["serviceAccountName"]

        self.assertEqual(job_spec["parallelism"], 1)
        self.assertEqual(job_spec["completions"], 1)
        self.assertEqual(job_spec["backoffLimit"], 0)
        self.assertEqual(job_template["spec"]["restartPolicy"], "Never")
        self.assertIs(job_template["spec"]["automountServiceAccountToken"], False)
        self.assertEqual(migrator["image"], router["image"])
        self.assertEqual(migrator["volumeMounts"], router["volumeMounts"])
        self.assertEqual(migrator["args"], ["/usr/local/bin/praefect", "-config", "/etc/gitaly/config.toml", "sql-migrate"])
        self.assertEqual({env["name"]: env.get("value") for env in router["env"]}["PRAEFECT_AUTO_MIGRATE"], "0")
        self.assertNotEqual(job_template["metadata"]["labels"]["app"],
                            resources["Service", "gitlab-praefect"]["spec"]["selector"]["app"])
        self.assertIs(router_pod["automountServiceAccountToken"], False)
        self.assertEqual(gate["name"], "wait-for-praefect-migration")
        self.assertEqual({env["name"]: env["value"] for env in gate["env"]}["MIGRATION_JOB"], job_name)
        self.assertEqual(resources["Role", account]["rules"], [
            {"apiGroups": ["batch"], "resources": ["jobs"], "resourceNames": [job_name], "verbs": ["get"]},
        ])
        self.assertEqual(resources["RoleBinding", account]["subjects"], [
            {"kind": "ServiceAccount", "name": account, "namespace": "gitlab"},
        ])
        self.assertTrue(all(mount["name"] != "praefect-schema-api" for mount in router["volumeMounts"]))
        self.assertTrue(all(router_template["metadata"]["labels"][key] == value for key, value in
                            resources["NetworkPolicy", "gitlab-allow-bootstrap-api"]["spec"]["podSelector"]["matchLabels"].items()))
        def wave(document):
            return int(document["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-wave", 0))
        self.assertLess(wave(resources["ConfigMap", "gitlab-praefect"]), wave(job))
        self.assertLess(wave(resources["Role", account]), wave(job))
        self.assertLess(wave(job), wave(workload))

    @unittest.skipUnless(os.environ.get("GITLAB_ISOLATION_RENDER") and shutil.which("sh") and os.name == "posix",
                         "no rendered Kustomize application and POSIX shell supplied")
    def test_gitlab_upgrade_guard_uses_upstream_script_for_existing_release(self):
        path = Path(os.environ["GITLAB_ISOLATION_RENDER"])
        docs = [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(doc, dict)]
        name = "gitlab-gitlab-upgrade-check"
        job = next(doc for doc in docs if doc["kind"] == "Job" and doc["metadata"]["name"] == name)
        config = next(doc for doc in docs if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == name)
        pod = job["spec"]["template"]["spec"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bootstrap = root / "bootstrap"
            bootstrap.mkdir()
            chart_info = root / "chart-info"
            chart_info.mkdir()
            kubectl = root / "kubectl"
            kubectl.write_text(
                '#!/bin/sh\ncase "$FIXTURE" in\n'
                '  api-error) exit 1 ;;\n'
                '  existing) [ "$2" != configmap/gitlab-gitlab-chart-info ] || echo "$2" ;;\n'
                '  missing-metadata) [ "$2" != pvc/repo-data-gitlab-gitaly-default-2 ] || echo "$2" ;;\n'
                'esac\n', encoding="utf-8",
            )
            kubectl.chmod(0o700)
            upstream = root / "runcheck"
            upstream.write_text(config["data"]["runcheck"].replace("/chart-info/", str(chart_info) + "/")
                                .replace("/dev/termination-log", str(root / "termination-log")), encoding="utf-8")
            detect = pod["initContainers"][0]["command"][2].replace("/bootstrap/", str(bootstrap) + "/")
            run_check = pod["containers"][0]["command"][2].replace("/bootstrap/", str(bootstrap) + "/")
            run_check = run_check.replace("/scripts/runcheck", str(upstream))
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"])
            env.update({item["name"]: item["value"] for item in pod["containers"][0]["env"]})
            for scenario, expected in (("fresh", 0), ("missing-metadata", 1), ("api-error", 1)):
                (bootstrap / "fresh").unlink(missing_ok=True)
                result = subprocess.run(["sh", "-ec", detect], env=dict(env, FIXTURE=scenario), capture_output=True, timeout=5)
                self.assertEqual(result.returncode, expected)
                self.assertEqual((bootstrap / "fresh").exists(), scenario == "fresh")
                if scenario == "fresh":
                    self.assertEqual(subprocess.run(["sh", "-ec", run_check], env=env, capture_output=True, timeout=5).returncode, 0)
            for version, chart, expected in (("18.11.1", "9.11.1", 0), ("18.10.1", "9.10.1", 1)):
                (chart_info / "gitlabVersion").write_text(version, encoding="utf-8")
                (chart_info / "gitlabChartVersion").write_text(chart, encoding="utf-8")
                subprocess.run(["sh", "-ec", detect], env=dict(env, FIXTURE="existing"), capture_output=True, check=True, timeout=5)
                self.assertFalse((bootstrap / "fresh").exists())
                result = subprocess.run(["sh", "-ec", run_check], env=env, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, expected, result.stdout)

    @unittest.skipUnless(os.environ.get("GITLAB_CHART_RENDER"), "no rendered chart supplied")
    def test_vendored_chart_render_contains_expected_praefect_resources(self):
        path = Path(os.environ["GITLAB_CHART_RENDER"])
        docs = [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(doc, dict)]
        workloads = {doc["metadata"]["name"]: doc for doc in docs if doc.get("kind") == "StatefulSet"}
        for name in ("gitlab-gitaly-default", "gitlab-praefect"):
            spec = workloads[name]["spec"]
            self.assertEqual(spec["replicas"], 3)
            affinity = spec["template"]["spec"]["affinity"]["podAntiAffinity"]
            self.assertEqual(affinity["requiredDuringSchedulingIgnoredDuringExecution"][0]["topologyKey"],
                             "kubernetes.io/hostname")
        cm = next(doc for doc in docs if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "gitlab-praefect")
        config = cm["data"]["config.toml.tpl"]
        self.assertIn("default_replication_factor = 3", config)
        self.assertIn("sslmode = 'verify-full'", config)
        pod = workloads["gitlab-praefect"]["spec"]["template"]["spec"]
        container = next(item for item in pod["containers"] if item["name"] == "praefect")
        self.assertIn({"name": "PGSSLROOTCERT", "value": "/etc/ssl/certs/ca-certificates.crt"}, container["env"])
        self.assertTrue(any(mount["mountPath"] == "/etc/ssl/certs/" for mount in container["volumeMounts"]))

    @unittest.skipUnless(os.environ.get("GITLAB_TLS_RENDER"), "no rendered TLS chart supplied")
    def test_rendered_repository_clients_and_server_mounts_use_tls(self):
        path = Path(os.environ["GITLAB_TLS_RENDER"])
        docs = [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(doc, dict)]
        resources = {(doc.get("kind"), doc["metadata"]["name"]): doc for doc in docs}
        config = resources["ConfigMap", "gitlab-praefect"]["data"]["config.toml.tpl"]
        self.assertIn("tls_listen_addr = ':8076'", config)
        self.assertIn("sslmode = 'verify-full'", config)
        for i in range(3):
            self.assertIn(f"tls://gitlab-gitaly-default-{i}.gitlab-gitaly-default.gitlab.svc:8076", config)
        configs = [str(doc.get("data", {})) for doc in docs if doc.get("kind") == "ConfigMap"]
        self.assertTrue(any("tls://gitlab-praefect.gitlab.svc:8076" in value for value in configs))
        self.assertFalse(any("tcp://gitlab-praefect.gitlab.svc:" in value for value in configs))
        for role, workload in (("gitaly", "gitlab-gitaly-default"), ("praefect", "gitlab-praefect")):
            spec = resources["StatefulSet", workload]["spec"]
            sources = [source["secret"] for volume in spec["template"]["spec"]["volumes"]
                       for source in volume.get("projected", {}).get("sources", []) if "secret" in source]
            self.assertIn({"name": role + "-tls", "items": [
                {"key": "tls.crt", "path": role + ".crt"}, {"key": "tls.key", "path": role + ".key"},
            ]}, sources)
            self.assertIn({"name": "repository-ca", "items": [
                {"key": "repository-ca.crt", "path": "repository-ca.crt"},
            ]}, sources)
        self.assertEqual(resources["Service", "gitlab-praefect"]["spec"]["clusterIP"], "None")


if __name__ == "__main__":
    unittest.main()
