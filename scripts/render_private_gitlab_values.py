"""Prepare the premium GitLab CE and Runner app files for private GitOps."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Mapping

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import dns_name
from render_gerrit_sites import label
from render_gateway_edge import address, validate_gitlab_ssh
from render_gitlab_praefect import render as render_praefect
from strict_json import loads_strict_json
from strict_yaml import loads_strict_yaml_all


PROFILE = "premium-3node-gitlab-ce"
APPS = Path("gitops/clusters/rke2-main/premium-3node/apps")
SECRET_FIELDS = (
    "GITLAB_POSTGRES_SECRET_NAME",
    "GITLAB_REDIS_SECRET_NAME",
    "GITLAB_REDIS_CA_SECRET_NAME",
    "GITLAB_OBJECT_STORAGE_SECRET_NAME",
    "GITLAB_REGISTRY_STORAGE_SECRET_NAME",
    "GITLAB_BACKUP_STORAGE_SECRET_NAME",
    "GITLAB_RUNNER_TOKEN_SECRET_NAME",
)
PLACEHOLDER = re.compile(r"<([A-Z0-9_]+)>")
REPOSITORY_COMPONENT = "repository-network"
REPOSITORY_VALUES = "praefect-values.yaml"
SSH_POLICY = "ssh-edge-network-policy.yaml"
CNPG_APP = APPS / "platform-postgres"
CNPG_RESOURCES = "gitlab-databases.yaml"
CNPG_HOST = "platform-postgres-rw.platform-databases.svc.cluster.local"
MANAGED_POSTGRES_CA = {"configMap": "platform-internal-roots", "keys": ["ca-certificates.crt"]}


class IndentedDumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def yaml_mapping(text: str) -> dict:
    documents = loads_strict_yaml_all(text, yaml_12=True)
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise ValueError("Expected a single YAML mapping in a GitLab application file")
    return documents[0]


def private_plan(repo_root: Path, filename: str) -> dict:
    path = (repo_root / filename).resolve(strict=True)
    if not path.is_relative_to((repo_root / "private").resolve()):
        raise ValueError("Store GitLab deployment plans under private/")
    plan = loads_strict_json(read_bounded_text(path))
    if not isinstance(plan, dict) or type(plan.get("schema_version")) is not int:
        raise ValueError("Expected a versioned GitLab deployment plan")
    return plan


def configure_ssh(base: dict, edge: dict) -> dict:
    if edge["schema_version"] != 1:
        raise ValueError("GitLab SSH requires a version 1 Gateway edge plan")
    ssh = validate_gitlab_ssh(edge.get("gitlab_ssh"))
    sources = set()
    # Cluster externalTrafficPolicy can SNAT on the receiving NodePort node.
    for key, minimum, maximum in (("entry_hosts", 2, 2), ("backends", 2, 16)):
        hosts = edge.get(key)
        if (not isinstance(hosts, list) or not minimum <= len(hosts) <= maximum
                or any(not isinstance(host, dict) or not address(host.get("address")) for host in hosts)
                or len({host["address"] for host in hosts}) != len(hosts)):
            raise ValueError("GitLab SSH requires the edge and backend IPv4 addresses")
        sources.update(host["address"] for host in hosts)
    base["global"]["hosts"]["ssh"] = ssh["hostname"]
    base["global"]["shell"]["port"] = ssh["port"]
    shell = base["gitlab"]["gitlab-shell"]
    shell.setdefault("service", {}).update(
        type="NodePort", nodePort=ssh["node_port"], internalPort=2222, externalTrafficPolicy="Cluster",
    )
    labels = {"app": "gitlab-shell", "release": "gitlab"}
    shell["topologySpreadConstraints"] = [{
        "maxSkew": 1, "topologyKey": "kubernetes.io/hostname",
        "whenUnsatisfiable": "DoNotSchedule", "labelSelector": {"matchLabels": labels},
    }]
    return {
        "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
        "metadata": {"name": "gitlab-allow-edge-ssh"},
        "spec": {"podSelector": {"matchLabels": labels}, "policyTypes": ["Ingress"],
                 "ingress": [{"from": [{"ipBlock": {"cidr": host + "/32"}} for host in sorted(sources)],
                              "ports": [{"protocol": "TCP", "port": 2222}]}]},
    }


def cnpg_resources(repo_root: Path, databases: list[dict], cluster_manifest: str | None = None) -> dict[Path, str]:
    """Wire scoped DatabaseRole and Database resources into the CNPG app."""
    app = repo_root / CNPG_APP
    kustomization_path = app / "kustomization.yaml"
    config = yaml_mapping(read_bounded_text(kustomization_path))
    namespace = "platform-databases"
    cluster_name = "platform-postgres"
    if config["namespace"] != namespace:
        raise ValueError("GitLab databases require the premium shared PostgreSQL namespace")
    output_path = app / CNPG_RESOURCES
    resources = config["resources"]
    existing = []
    for filename in resources:
        if filename == CNPG_RESOURCES:
            continue
        if filename == "postgres-cluster.yaml" and cluster_manifest is not None:
            manifest = cluster_manifest
        else:
            manifest = read_bounded_text(app / filename)
        existing.extend(doc for doc in loads_strict_yaml_all(manifest) if isinstance(doc, dict))
    clusters = [doc for doc in existing if doc.get("kind") == "Cluster"]
    if (len(clusters) != 1 or clusters[0]["metadata"]["name"] != cluster_name
            or clusters[0]["metadata"].get("namespace", namespace) != namespace):
        raise ValueError("GitLab databases require the premium platform-postgres Cluster")
    spec = clusters[0]["spec"]
    bootstrap = spec.get("bootstrap", {}).get("initdb", {})
    owners = {bootstrap.get("owner"), *(role["name"] for role in spec.get("managed", {}).get("roles", []))}
    owners.update(doc["spec"]["name"] for doc in existing if doc.get("kind") == "DatabaseRole")
    names = {"postgres", "template0", "template1", bootstrap.get("database")}
    names.update(doc["spec"]["name"] for doc in existing if doc.get("kind") == "Database")
    secrets = {bootstrap.get("secret", {}).get("name")}
    secrets.update(role.get("passwordSecret", {}).get("name") for role in spec.get("managed", {}).get("roles", []))
    secrets.update(doc["spec"].get("passwordSecret", {}).get("name")
                   for doc in existing if doc.get("kind") == "DatabaseRole")
    documents = []
    for database in databases:
        owner, name, secret_name = database["owner"], database["name"], database["secret"]
        if (any(not isinstance(value, str) or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value)
                for value in (owner, name)) or not label(secret_name)):
            raise ValueError("Invalid GitLab managed database, role or Secret name")
        if (owner in owners or owner in {"postgres", "streaming_replica", "cnpg_pooler_pgbouncer"}
                or owner.startswith("pg_") or name in names or secret_name in secrets):
            raise ValueError("GitLab databases, roles and credentials must be dedicated")
        owners.add(owner)
        names.add(name)
        secrets.add(secret_name)
        metadata = {"name": f"{cluster_name}-{database['component']}", "namespace": namespace}
        common = {"apiVersion": "postgresql.cnpg.io/v1", "metadata": metadata}
        documents.append({**common, "kind": "DatabaseRole", "spec": {
            "name": owner, "cluster": {"name": cluster_name}, "login": True, "superuser": False,
            "createdb": False, "createrole": False, "replication": False, "bypassrls": False,
            "passwordSecret": {"name": secret_name}, "databaseRoleReclaimPolicy": "retain",
        }})
        db_spec = {"name": name, "owner": owner, "cluster": {"name": cluster_name},
                   "databaseReclaimPolicy": "retain"}
        if database["component"] == "gitlab":
            db_spec["extensions"] = [{"name": extension, "ensure": "present"}
                                     for extension in ("amcheck", "pg_trgm", "btree_gist")]
        documents.append({**common, "kind": "Database", "spec": db_spec})
    # An existing generated bundle can grow to include Praefect. Preserve the
    # role/database identities and Secret references already in use.
    if output_path.exists():
        prior = loads_strict_yaml_all(read_bounded_text(output_path))
        if any(doc not in documents for doc in prior):
            raise ValueError("Existing GitLab database declarations differ from the requested identities")
    if CNPG_RESOURCES not in resources:
        resources.append(CNPG_RESOURCES)
    for doc in documents:
        if any(item.get("kind") == doc["kind"] and item.get("metadata", {}).get("name") == doc["metadata"]["name"]
               for item in existing):
            raise ValueError("GitLab database resource name is already in use")
    return {
        output_path: yaml.dump_all(documents, Dumper=IndentedDumper, sort_keys=False),
        kustomization_path: yaml.dump(config, Dumper=IndentedDumper, sort_keys=False),
    }


def prepare(repo_root: Path, domain: str, environment: Mapping[str, str], *,
            cnpg_cluster_manifest: str | None = None) -> dict[Path, str]:
    """Validate and prepare all changes before writing any private values."""
    gitlab_dir = repo_root / APPS / "gitlab-ce"
    runner_dir = repo_root / APPS / "gitlab-runner"
    values_path = gitlab_dir / "values.yaml"
    runner_path = runner_dir / "values.yaml"
    substitutions = {"PLATFORM_DOMAIN": domain}
    substitutions.update({name: environment.get(name, "").strip() for name in SECRET_FIELDS})
    originals = {path: read_bounded_text(path) for path in (values_path, runner_path)}
    required_inputs = {match for content in originals.values() for match in PLACEHOLDER.findall(content)}
    for name in sorted(required_inputs):
        value = substitutions.get(name, "")
        valid = dns_name(value) and dns_name("registry-gitlab." + value) if name == "PLATFORM_DOMAIN" else label(value)
        if not valid:
            raise ValueError(f"Set a valid GitLab deployment input for {name}")

    rendered = {path: PLACEHOLDER.sub(lambda match: substitutions[match[1]], content)
                for path, content in originals.items()}
    base = yaml_mapping(rendered[values_path])
    yaml_mapping(rendered[runner_path])
    plan_file = environment.get("GITLAB_PRAEFECT_PLAN_FILE", "").strip()
    ssh_file = environment.get("GITLAB_SSH_EDGE_PLAN_FILE", "").strip()
    cnpg_mode = environment.get("GITLAB_CNPG_MANAGED_DATABASES", "").strip().lower()
    if cnpg_mode not in {"", "true", "false"}:
        raise ValueError("Set GITLAB_CNPG_MANAGED_DATABASES to true or false")
    cnpg_enabled = cnpg_mode == "true" or (repo_root / CNPG_APP / CNPG_RESOURCES).is_file()
    if not plan_file and not ssh_file and not cnpg_enabled:
        return rendered

    kustomization_path = gitlab_dir / "kustomization.yaml"
    kustomization = yaml_mapping(read_bounded_text(kustomization_path))
    charts = [chart for chart in kustomization["helmCharts"] if chart.get("name") == "gitlab"]
    components = kustomization.get("components")
    if (len(charts) != 1 or charts[0].get("releaseName") != "gitlab"
            or charts[0].get("namespace") != "gitlab" or charts[0].get("valuesFile") != "values.yaml"
            or kustomization.get("helmGlobals", {}).get("chartHome") != "charts/gitlab-10.1.1"
            or not isinstance(components, list) or "../../components/network-isolation" not in components):
        raise ValueError("GitLab repository and SSH settings require the pinned premium application")
    if ssh_file:
        edge = private_plan(repo_root, ssh_file)
        policy = configure_ssh(base, edge)
        resources = kustomization["resources"]
        if SSH_POLICY not in resources:
            resources.append(SSH_POLICY)
        rendered[gitlab_dir / SSH_POLICY] = yaml.safe_dump(policy, sort_keys=False)
        rendered[values_path] = yaml.dump(base, Dumper=IndentedDumper, sort_keys=False)
        rendered[kustomization_path] = yaml.dump(kustomization, Dumper=IndentedDumper, sort_keys=False)
    # Helm replaces lists across values files. Keep the site's existing trust
    # roots alongside the repository and Praefect database trust roots.
    chart = charts[0]
    extra_values = chart.get("additionalValuesFiles", [])
    if not isinstance(extra_values, list) or any(not isinstance(path, str) for path in extra_values):
        raise ValueError("Invalid GitLab additionalValuesFiles")
    values_sources = [base]
    for filename in extra_values:
        if filename == REPOSITORY_VALUES:
            continue
        path = (gitlab_dir / filename).resolve()
        if not path.is_relative_to(gitlab_dir.resolve()):
            raise ValueError("Additional GitLab values must be inside the application directory")
        values_sources.append(yaml_mapping(read_bounded_text(path)))
    existing_cas: list = []
    application_database: dict = {}
    application_password: dict = {}
    for values in values_sources:
        global_values = values.get("global", {})
        postgres = global_values.get("psql", {})
        application_database.update(postgres)
        application_password.update(postgres.get("password", {}))
        certificates = global_values.get("certificates", {})
        if "customCAs" in certificates:
            existing_cas = certificates["customCAs"]

    if cnpg_enabled:
        cluster_name = environment.get("CNPG_CLUSTER_NAME", "").strip() or "platform-postgres"
        namespace = environment.get("CNPG_CLUSTER_NAMESPACE", "").strip() or "platform-databases"
        if (cluster_name, namespace) != ("platform-postgres", "platform-databases"):
            raise ValueError("GitLab managed databases require the platform-postgres Cluster")
        if application_database["host"] != CNPG_HOST or application_database["port"] != 5432:
            raise ValueError("GitLab managed database must use the shared CloudNativePG write Service")
        owner, name, secret = (application_database["username"], application_database["database"],
                               application_password["secret"])
        databases = [{"component": "gitlab", "owner": owner, "name": name, "secret": secret}]
    if not plan_file:
        if cnpg_enabled and REPOSITORY_VALUES in extra_values:
            retained = yaml_mapping(read_bounded_text(gitlab_dir / REPOSITORY_VALUES))["global"]["praefect"]
            psql = retained["psql"]
            if psql["host"] == CNPG_HOST and str(psql["port"]) == "5432":
                databases.append({"component": "praefect", "owner": psql["user"], "name": psql["dbName"],
                                  "secret": retained["dbSecret"]["secret"]})
        if cnpg_enabled:
            rendered.update(cnpg_resources(repo_root, databases, cnpg_cluster_manifest))
        return rendered

    plan = private_plan(repo_root, plan_file)
    if plan["schema_version"] != 3:
        raise ValueError("Private premium GitLab requires a version 3 Praefect plan")
    bundle = render_praefect(plan)
    overlay = yaml_mapping(bundle[REPOSITORY_VALUES])
    if (plan["postgres_database"] == application_database["database"]
            or plan["postgres_user"] == application_database["username"]
            or plan["postgres_secret"] == application_password["secret"]):
        raise ValueError("Praefect needs a dedicated database, role and password Secret")
    if cnpg_enabled:
        if plan["postgres_host"] == CNPG_HOST and plan["postgres_port"] == 5432:
            databases.append({"component": "praefect", "owner": plan["postgres_user"], "name": plan["postgres_database"],
                              "secret": plan["postgres_secret"]})
        rendered.update(cnpg_resources(repo_root, databases, cnpg_cluster_manifest))
    if not isinstance(existing_cas, list):
        raise ValueError("GitLab customCAs must be a list")
    if MANAGED_POSTGRES_CA not in existing_cas:
        existing_cas = [MANAGED_POSTGRES_CA, *existing_cas]
    repository_cas = overlay["global"]["certificates"]["customCAs"]
    overlay["global"]["certificates"]["customCAs"] = [
        *existing_cas, *(item for item in repository_cas if item not in existing_cas),
    ]

    chart["additionalValuesFiles"] = [path for path in extra_values if path != REPOSITORY_VALUES] + [REPOSITORY_VALUES]
    kustomization["components"] = [item for item in components if item != REPOSITORY_COMPONENT] + [REPOSITORY_COMPONENT]
    rendered[gitlab_dir / REPOSITORY_VALUES] = yaml.safe_dump(overlay, sort_keys=False)
    for name in ("kustomization.yaml", "repository-network-policies.yaml"):
        rendered[gitlab_dir / REPOSITORY_COMPONENT / name] = bundle[name]
    rendered[kustomization_path] = yaml.dump(kustomization, Dumper=IndentedDumper, sort_keys=False)
    return rendered
