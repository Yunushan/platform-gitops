#!/usr/bin/env python3
"""Package a prepared Keycloak cluster and a separate new-realm MFA definition.

Rendering never reads credentials, installs software, imports a realm, migrates
a database or changes a service. Live identity/failure acceptance is separate.
"""

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shlex
import sys

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import IMAGE, RUNTIMES, dns_name, write_bundle
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
LABELS = {"app.kubernetes.io/name": "platform-keycloak"}
PATH = re.compile(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+")
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,47}")


def label(value):
    return dns_name(value) and "." not in value and len(value) <= 63


def exact(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields.split()): raise ValueError("Missing or unsupported Keycloak fields")


def validate(plan):
    fields = "schema_version version image namespace hostname realm database tls_secret database_ca_secret private_dir native_home native_manifest_sha256 members network"
    exact(plan, fields + (" public_port" if isinstance(plan, dict) and "public_port" in plan else ""))
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1: raise ValueError("Unsupported Keycloak plan")
    if not isinstance(plan["version"], str) or not re.fullmatch(r"26[.]4[.][0-9]+", plan["version"]): raise ValueError("Review an exact Keycloak 26.4 patch release")
    if not isinstance(plan["image"], str) or not IMAGE.fullmatch(plan["image"]): raise ValueError("Pin the reviewed optimized image digest")
    for key in ("namespace", "realm", "tls_secret", "database_ca_secret"):
        if not label(plan[key]): raise ValueError("Use explicit DNS labels")
    if plan["realm"] == "master" or not dns_name(plan["hostname"]): raise ValueError("Use a dedicated realm and canonical DNS hostname")
    if type(plan.get("public_port", 443)) is not int or not 1 <= plan.get("public_port", 443) <= 65535: raise ValueError("Use an explicit canonical HTTPS port")
    for key in ("private_dir", "native_home"):
        if not isinstance(plan[key], str) or not PATH.fullmatch(plan[key]): raise ValueError("Use explicit canonical Linux paths")
    one, two = plan["private_dir"], plan["native_home"]
    if one == two or one.startswith(two + "/") or two.startswith(one + "/"): raise ValueError("Keep private credentials separate from the release")
    if not isinstance(plan["native_manifest_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", plan["native_manifest_sha256"]): raise ValueError("Pin the reviewed native distribution manifest")
    database = plan["database"]; exact(database, "host port name username postgres_major secret password_key")
    if not dns_name(database["host"]) or type(database["port"]) is not int or not 1024 <= database["port"] <= 65535: raise ValueError("Select an explicit PostgreSQL endpoint")
    if type(database["postgres_major"]) is not int or database["postgres_major"] != 17: raise ValueError("This reviewed Keycloak family uses PostgreSQL 17")
    for key in ("name", "username"):
        if not isinstance(database[key], str) or not IDENTIFIER.fullmatch(database[key]): raise ValueError("Use explicit dedicated database and role names")
    if not label(database["secret"]) or not label(database["password_key"]): raise ValueError("Reference the private database credential")
    members = plan["members"]
    if not isinstance(members, list) or not 2 <= len(members) <= 3: raise ValueError("Use two or three independently placed members")
    for member in members:
        exact(member, "name node address failure_domain")
        if not label(member["name"]) or not dns_name(member["node"]) or not label(member["failure_domain"]): raise ValueError("Review each host and failure domain")
        address = ipaddress.IPv4Address(member["address"])
        if address.is_loopback or address.is_multicast or address.is_unspecified or address.is_link_local or address.is_reserved or str(address) != member["address"]: raise ValueError("Use routable peer IPv4 addresses")
    for key in ("name", "node", "address", "failure_domain"):
        if len({m[key] for m in members}) != len(members): raise ValueError("Member identities, addresses and failure domains must be distinct")
    network = plan["network"]; exact(network, "ingress_namespaces monitoring_namespace database_cidrs")
    if not isinstance(network["ingress_namespaces"], list) or not 1 <= len(network["ingress_namespaces"]) <= 4 or not all(label(n) for n in network["ingress_namespaces"]): raise ValueError("Select the ingress namespaces")
    if len(set(network["ingress_namespaces"])) != len(network["ingress_namespaces"]) or not label(network["monitoring_namespace"]): raise ValueError("Use unique explicit network sources")
    if not isinstance(network["database_cidrs"], list) or not 1 <= len(network["database_cidrs"]) <= 8: raise ValueError("Review the PostgreSQL destination ranges")
    for value in network["database_cidrs"]:
        subnet = ipaddress.IPv4Network(value, strict=True)
        if subnet.prefixlen < 24 or subnet.is_loopback or subnet.is_multicast or subnet.is_link_local or subnet.network_address.is_unspecified or subnet.network_address.is_reserved: raise ValueError("Use narrow reviewed PostgreSQL ranges")
    if len(set(network["database_cidrs"])) != len(network["database_cidrs"]): raise ValueError("List each PostgreSQL range once")
    return plan


def build_arguments():
    return ["build", "--db=postgres", "--health-enabled=true", "--metrics-enabled=true", "--transaction-xa-enabled=false"]


def public_origin(plan):
    port = plan.get("public_port", 443)
    return "https://" + plan["hostname"] + (":" + str(port) if port != 443 else "")


def arguments(plan, *, native=False, member=None):
    private = plan["private_dir"] if native else "/run/platform-keycloak"
    db = plan["database"]
    options = {
        "db-url": f"jdbc:postgresql://{db['host']}:{db['port']}/{db['name']}?sslmode=verify-full&sslrootcert={private}/db-ca/ca.crt",
        "db-username": db["username"], "hostname": public_origin(plan), "hostname-strict": "true", "http-enabled": "false",
        "https-port": "8443", "https-certificate-file": private + "/tls/tls.crt", "https-certificate-key-file": private + "/tls/tls.key",
        "cache": "ispn", "cache-stack": "jdbc-ping", "cache-embedded-mtls-enabled": "true",
        "http-management-port": "9000", "http-management-scheme": "inherited",
        # Concurrent mTLS bootstrap can produce an ORM constraint error whose
        # SQL batch includes the private transport key. Keep parameter-bearing
        # ORM diagnostics out of ordinary application/centralized logs.
        "log-level": "info,org.hibernate.orm.jdbc.error:off,org.hibernate.orm.jdbc.batch:off",
        "spi-connections-jpa--quarkus--migration-strategy": "validate", "spi-connections-jpa--quarkus--initialize-empty": "false",
    }
    if member: options["cache-embedded-network-bind-address"] = member["address"]
    return ["start", "--optimized", *("--" + key + "=" + value for key, value in options.items())]


def new_realm(plan):
    # A new-realm import artifact only. Never put it on the daemon's import path
    # or overwrite a realm whose users, sessions and clients already exist.
    return {"realm": plan["realm"], "enabled": True, "sslRequired": "all", "registrationAllowed": False,
        "verifyEmail": True, "resetPasswordAllowed": True, "rememberMe": False, "duplicateEmailsAllowed": False,
        "bruteForceProtected": True, "failureFactor": 5, "passwordPolicy": "length(14)",
        "eventsEnabled": True, "eventsListeners": ["jboss-logging"], "adminEventsEnabled": True, "adminEventsDetailsEnabled": False,
        "browserFlow": "platform-browser-mfa", "requiredActions": [{"alias": "CONFIGURE_TOTP", "name": "Configure OTP",
            "providerId": "CONFIGURE_TOTP", "enabled": True, "defaultAction": True, "priority": 10, "config": {}}],
        "authenticationFlows": [
            {"alias": "platform-browser-mfa", "providerId": "basic-flow", "topLevel": True, "builtIn": False,
             "authenticationExecutions": [{"authenticator": "auth-cookie", "requirement": "ALTERNATIVE", "priority": 10, "authenticatorFlow": False},
                {"flowAlias": "platform-browser-mfa-forms", "requirement": "ALTERNATIVE", "priority": 20, "authenticatorFlow": True}]},
            {"alias": "platform-browser-mfa-forms", "providerId": "basic-flow", "topLevel": False, "builtIn": False,
             "authenticationExecutions": [{"authenticator": "auth-username-password-form", "requirement": "REQUIRED", "priority": 10, "authenticatorFlow": False},
                {"authenticator": "auth-otp-form", "requirement": "REQUIRED", "priority": 20, "authenticatorFlow": False}]}]}


def kubernetes(plan):
    namespace = plan["namespace"]; count = len(plan["members"])
    def meta(name="platform-keycloak"): return {"name": name, "namespace": namespace, "labels": dict(LABELS)}
    peer = {"podSelector": {"matchLabels": LABELS}}
    def ns(name): return {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": name}}}
    def ports(*numbers): return [{"protocol": "TCP", "port": n} for n in numbers]
    constraints = [{"matchExpressions": [
        {"key": "kubernetes.io/hostname", "operator": "In", "values": [member["node"]]},
        {"key": "topology.kubernetes.io/zone", "operator": "In", "values": [member["failure_domain"]]}]} for member in plan["members"]]
    pod = {"automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
        "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": constraints}},
            "podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{"labelSelector": {"matchLabels": LABELS}, "topologyKey": "topology.kubernetes.io/zone"}]}},
        "containers": [{"name": "keycloak", "image": plan["image"], "command": ["/opt/keycloak/bin/kc.sh"], "args": arguments(plan),
            "env": [{"name": "KC_DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": plan["database"]["secret"], "key": plan["database"]["password_key"]}}},
                {"name": "KC_CACHE_EMBEDDED_NETWORK_BIND_ADDRESS", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}}],
            "resources": {"requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"cpu": "2", "memory": "2Gi"}},
            "ports": [{"name": name, "containerPort": number} for name, number in (("https", 8443), ("management", 9000), ("cluster", 7800), ("failure", 57800))],
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
            "startupProbe": {"httpGet": {"path": "/health/started", "port": "management", "scheme": "HTTPS"}, "periodSeconds": 5, "failureThreshold": 120},
            "readinessProbe": {"httpGet": {"path": "/health/ready", "port": "management", "scheme": "HTTPS"}, "periodSeconds": 5},
            "volumeMounts": [{"name": "tls", "mountPath": "/run/platform-keycloak/tls", "readOnly": True},
                {"name": "db-ca", "mountPath": "/run/platform-keycloak/db-ca", "readOnly": True},
                {"name": "data", "mountPath": "/opt/keycloak/data"}, {"name": "tmp", "mountPath": "/tmp"}]}],
        "volumes": [{"name": "tls", "secret": {"secretName": plan["tls_secret"], "defaultMode": 288, "items": [{"key": k, "path": k} for k in ("tls.crt", "tls.key")]}},
            {"name": "db-ca", "secret": {"secretName": plan["database_ca_secret"], "defaultMode": 288, "items": [{"key": "ca.crt", "path": "ca.crt"}]}},
            {"name": "data", "emptyDir": {"sizeLimit": "1Gi"}}, {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}}]}
    return [{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta(), "spec": {"replicas": count,
                "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}},
                "selector": {"matchLabels": LABELS}, "template": {"metadata": {"labels": LABELS}, "spec": pod}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": meta(), "spec": {"selector": LABELS, "ports": [{"name": "https", "port": 8443, "targetPort": "https"}]}},
        {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": meta(), "spec": {"minAvailable": count - 1, "selector": {"matchLabels": LABELS}}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": meta(), "spec": {"podSelector": {"matchLabels": LABELS}, "policyTypes": ["Ingress", "Egress"],
            "ingress": [{"from": [ns(n) for n in plan["network"]["ingress_namespaces"]], "ports": ports(8443)},
                {"from": [ns(plan["network"]["monitoring_namespace"])], "ports": ports(9000)}, {"from": [peer], "ports": ports(7800, 57800)}],
            "egress": [{"to": [peer], "ports": ports(7800, 57800)},
                {"to": [ns("kube-system")], "ports": [{"protocol": protocol, "port": 53} for protocol in ("TCP", "UDP")]},
                {"to": [{"ipBlock": {"cidr": c}} for c in plan["network"]["database_cidrs"]], "ports": ports(plan["database"]["port"])}]}}]


def render(plan, runtime):
    validate(plan)
    if runtime not in RUNTIMES: raise ValueError("Unsupported Keycloak runtime")
    files = {"new-realm-mfa.json": json.dumps(new_realm(plan), sort_keys=True, indent=2) + "\n",
             "optimized-build.json": json.dumps({"schema_version": 1, "version": plan["version"], "arguments": build_arguments(), "built": False}, sort_keys=True, indent=2) + "\n"}
    if runtime in {"rke2", "k3s", "k8s"}: files["keycloak.yaml"] = yaml.safe_dump_all(kubernetes(plan), sort_keys=False)
    else:
        for member in plan["members"]:
            name = member["name"]; private = plan["private_dir"]
            if runtime == "docker":
                files[name + "-compose.yaml"] = yaml.safe_dump({"services": {name: {
                    "image": plan["image"], "entrypoint": ["/opt/keycloak/bin/kc.sh"], "command": arguments(plan, member=member),
                    "user": "1000:1000", "network_mode": "host", "read_only": True, "restart": "unless-stopped", "cap_drop": ["ALL"],
                    "security_opt": ["no-new-privileges:true"], "mem_limit": "2g", "cpus": 2.0, "pids_limit": 512,
                    "env_file": [{"path": private + "/database.env", "required": True, "format": "raw"}],
                    "volumes": [{"type": "bind", "source": private + "/" + directory, "target": "/run/platform-keycloak/" + directory,
                        "read_only": True, "bind": {"create_host_path": False}} for directory in ("tls", "db-ca")],
                    "tmpfs": ["/opt/keycloak/data:rw,nosuid,nodev,noexec,uid=1000,gid=1000,mode=0700,size=1g", "/tmp:rw,nosuid,nodev,noexec,mode=1777,size=256m"],
                }}}, sort_keys=False)
            else:
                command = " ".join(shlex.quote(a) for a in [plan["native_home"] + "/bin/kc.sh", *arguments(plan, native=True, member=member)])
                files[name + ".service"] = f"""[Unit]
Description=Keycloak cluster member {name} (prepared database, no schema updates)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=keycloak
Group=keycloak
WorkingDirectory={plan['native_home']}
EnvironmentFile={private}/database.env
ExecStartPre=/usr/bin/sha256sum --check --strict {private}/native-manifest.sha256
ExecStartPre=/usr/bin/sha256sum --check --strict {private}/native-files.sha256
ExecStart={command}
StateDirectory=platform-keycloak-{name}
StateDirectoryMode=0700
BindPaths=/var/lib/platform-keycloak-{name}:{plan['native_home']}/data
ReadWritePaths=/var/lib/platform-keycloak-{name}
Restart=on-failure
RestartSec=10
TimeoutStopSec=120
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
CapabilityBoundingSet=
UMask=0077
MemoryMax=2G
TasksMax=512

[Install]
WantedBy=multi-user.target
"""
        if runtime == "raw": files["native-manifest.sha256"] = plan["native_manifest_sha256"] + "  " + plan["private_dir"] + "/native-files.sha256\n"
    contract = {"schema_version": 1, "runtime": runtime, "plan": plan, "files_sha256": {name: hashlib.sha256(value.encode()).hexdigest() for name, value in sorted(files.items())}}
    files["keycloak-contract.json"] = json.dumps(contract, sort_keys=True, indent=2) + "\n"
    files["render-status.json"] = json.dumps({"component": "keycloak-ha", "runtime": runtime, "rendered": True, "deployed": False,
        "realm_imported": False, "mfa_verified": False, "database_verified": False, "cache_cluster_verified": False, "ha_verified": False, "full_stack_verified": False,
        "remaining": ["optimized-artifact-and-native-distribution-review", "prepared-ha-postgresql-schema-role-and-private-pki", "guarded-installation-and-new-realm-bootstrap",
            "mfa-enrollment-login-recovery-and-master-realm-controls", "application-oidc-clients-rbac-and-mail", "network-policy-datapath-and-cluster-membership",
            "session-survival-promotion-node-loss-upgrade-and-off-platform-restore"]}, indent=2) + "\n"
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True); args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, max_bytes=128 * 1024))
        write_bundle(render(plan, args.runtime), args.output_dir, private_root=ROOT / "private")
        print('{"rendered":true,"deployed":false,"mfa_verified":false,"ha_verified":false}'); return 0
    except Exception:
        print("Keycloak packaging refused; private values were not printed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
