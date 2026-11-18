#!/usr/bin/env python3
"""Render private three-member ZooKeeper packages tied to a reviewed Zuul plan."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import sys
import uuid

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import IMAGE, RUNTIMES, dns_name, write_bundle
from render_gerrit_sites import label
import render_zuul_config as zuul
from start_zookeeper import TLS_FILES
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
MODULES = ("start_zookeeper.py", "bounded_file.py", "bounded_subprocess.py", "strict_json.py")
LOGBACK = """<configuration>
  <appender name="CONSOLE" class="ch.qos.logback.core.ConsoleAppender">
    <encoder><pattern>%date %-5level [%thread] %logger{40} - %msg%n</pattern></encoder>
  </appender>
  <root level="INFO"><appender-ref ref="CONSOLE"/></root>
</configuration>
"""


def validate(plan: object, zuul_plan: dict) -> dict:
    zuul.validate(zuul_plan)
    fields = {"schema_version", "ensemble_id", "zookeeper_version", "image", "namespace", "cluster_domain",
              "failure_domain_key", "client_port", "quorum_port", "election_port", "initialize_if_empty", "artifacts", "members"}
    if not isinstance(plan, dict) or set(plan) != fields or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("Missing or unsupported ZooKeeper plan fields")
    try:
        if not isinstance(plan["ensemble_id"], str) or str(uuid.UUID(plan["ensemble_id"])) != plan["ensemble_id"]:
            raise ValueError("Declare an independent canonical ensemble UUID")
    except (ValueError, AttributeError):
        raise ValueError("Declare an independent canonical ensemble UUID") from None
    version = plan["zookeeper_version"]
    if not isinstance(version, str) or not re.fullmatch(r"3[.]9[.][0-9]+", version) or int(version.split(".")[2]) < 4:
        raise ValueError("Select an exact reviewed ZooKeeper 3.9 release at least 3.9.4")
    if (not isinstance(plan["image"], str) or not IMAGE.fullmatch(plan["image"]) or not label(plan["namespace"])
            or not dns_name(plan["cluster_domain"]) or type(plan["initialize_if_empty"]) is not bool
            or not isinstance(plan["failure_domain_key"], str)
            or plan["failure_domain_key"] not in {"kubernetes.io/hostname", "topology.kubernetes.io/zone"}):
        raise ValueError("Declare reviewed image, namespace, domain and failure-domain policy")
    ports = [plan[field] for field in ("client_port", "quorum_port", "election_port")]
    if any(type(port) is not int or not 1024 <= port <= 65535 for port in ports) or len(set(ports)) != 3:
        raise ValueError("Declare three distinct unprivileged protocol ports")
    artifacts = plan["artifacts"]
    if (not isinstance(artifacts, dict) or not 1 <= len(artifacts) <= 256 or f"zookeeper-{version}.jar" not in artifacts
            or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,179}[.]jar", name)
                   or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                   for name, digest in artifacts.items())):
        raise ValueError("Lock every reviewed distribution jar by filename and SHA-256")
    members = plan["members"]
    if not isinstance(members, list) or len(members) != 3:
        raise ValueError("Declare exactly three voting members")
    for member in members:
        if (not isinstance(member, dict) or set(member) != {"id", "name", "host", "bind_address", "failure_domain", "claim", "state_host_path", "tls_secret"}
                or type(member["id"]) is not int or member["id"] not in {1, 2, 3}
                or not isinstance(member["name"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", member["name"])
                or not dns_name(member["host"]) or not label(member["failure_domain"])
                or not label(member["claim"]) or not label(member["tls_secret"])
                or not isinstance(member["state_host_path"], str)
                or not re.fullmatch(r"/(?:srv|var/lib)/[A-Za-z0-9_/-]+", member["state_host_path"])
                or "//" in member["state_host_path"] or member["state_host_path"].endswith("/")):
            raise ValueError("Invalid member identity, persistent storage, TLS reference or failure domain")
        try:
            if not isinstance(member["bind_address"], str):
                raise ValueError("Declare a specific private host publish address")
            address = ipaddress.IPv4Address(member["bind_address"])
            if not address.is_private or address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local:
                raise ValueError("Declare a specific private host publish address")
        except (ipaddress.AddressValueError, TypeError):
            raise ValueError("Declare a specific private host publish address") from None
    for field in ("id", "name", "host", "bind_address", "failure_domain", "claim", "state_host_path", "tls_secret"):
        if len({member[field] for member in members}) != 3:
            raise ValueError("Members require distinct identities, addresses, failure domains, storage and TLS references")
    paths = sorted(member["state_host_path"] for member in members)
    if any(right.startswith(left + "/") for left in paths for right in paths if left != right):
        raise ValueError("Member state paths must not overlap")
    if {(member["host"], plan["client_port"]) for member in members} != {(entry["host"], entry["port"]) for entry in zuul_plan["zookeeper"]}:
        raise ValueError("Zuul and ZooKeeper must declare the same three TLS client endpoints")
    return plan


def configuration(plan: dict, member: dict, runtime: str) -> str:
    state = member["state_host_path"] if runtime == "raw" else "/var/lib/zookeeper"
    settings = ["tickTime=2000", "initLimit=10", "syncLimit=5", "dataDir=" + state,
        "secureClientPort=" + str(plan["client_port"]), "secureClientPortAddress=" + (member["bind_address"] if runtime == "raw" else "0.0.0.0"),
        "serverCnxnFactory=org.apache.zookeeper.server.NettyServerCnxnFactory",
        "standaloneEnabled=false", "reconfigEnabled=false", "quorumListenOnAllIPs=" + ("false" if runtime == "raw" else "true"),
        "sslQuorum=true", "portUnification=false", "client.portUnification=false",
        "ssl.keyStore.location=/etc/zookeeper/private/client-server.pem", "ssl.keyStore.type=PEM",
        "ssl.trustStore.location=/etc/zookeeper/private/client-ca.pem", "ssl.trustStore.type=PEM",
        "ssl.clientAuth=need", "ssl.hostnameVerification=true", "ssl.enabledProtocols=TLSv1.3,TLSv1.2",
        "ssl.quorum.keyStore.location=/etc/zookeeper/private/quorum-server.pem", "ssl.quorum.keyStore.type=PEM",
        "ssl.quorum.trustStore.location=/etc/zookeeper/private/quorum-ca.pem", "ssl.quorum.trustStore.type=PEM",
        "ssl.quorum.clientAuth=need", "ssl.quorum.hostnameVerification=true", "ssl.quorum.clientHostnameVerification=true",
        "ssl.quorum.enabledProtocols=TLSv1.3,TLSv1.2", "leader.closeSocketAsync=true", "learner.closeSocketAsync=true",
        "authProvider.x509=org.apache.zookeeper.server.auth.X509AuthenticationProvider",
        "enforce.auth.enabled=true", "enforce.auth.schemes=x509", "admin.enableServer=false",
        "4lw.commands.whitelist=srvr", "autopurge.snapRetainCount=10", "autopurge.purgeInterval=24",
        "maxClientCnxns=60"]
    settings += [f"server.{peer['id']}={peer['host']}:{plan['quorum_port']}:{plan['election_port']}:participant"
                 for peer in sorted(plan["members"], key=lambda item: item["id"])]
    # Do not append ';clientport' to server lines: that enables a plain client listener.
    return "\n".join(settings) + "\n"


def command(member: dict, digest: str, *, initialize: bool, probe: bool = False) -> list[str]:
    args = ["python3", "/etc/zookeeper/start_zookeeper.py", "--member", member["name"], "--contract-sha256", digest,
            "--probe" if probe else "--start"]
    if initialize and not probe:
        args.append("--initialize-if-empty")
    return args


def render(plan: dict, zuul_plan: dict, runtime: str) -> dict[str, str]:
    validate(plan, zuul_plan)
    if runtime not in RUNTIMES:
        raise ValueError("Unknown ZooKeeper runtime")
    if runtime in {"rke2", "k3s", "k8s"} and any(
            member["host"] != f"{member['name']}.{plan['namespace']}.svc.{plan['cluster_domain']}" for member in plan["members"]):
        raise ValueError("Kubernetes member hosts must name their generated per-member Services")
    files = {name: read_bounded_text(ROOT / "scripts" / name, encoding="utf-8") for name in MODULES}
    files["logback.xml"] = LOGBACK
    digests = {}
    for member in plan["members"]:
        name = member["name"]
        files[name + ".cfg"] = configuration(plan, member, runtime)
        contract = {"schema_version": 1, "ensemble_id": plan["ensemble_id"], "member_id": member["id"],
            "members": [{"id": peer["id"], "host": peer["host"], "quorum_port": plan["quorum_port"], "election_port": plan["election_port"]}
                        for peer in sorted(plan["members"], key=lambda item: item["id"])],
            "host": member["host"], "client_port": plan["client_port"], "version": plan["zookeeper_version"],
            "probe_address": member["bind_address"] if runtime == "raw" else "127.0.0.1",
            "data_dir": member["state_host_path"] if runtime == "raw" else "/var/lib/zookeeper", "artifacts": plan["artifacts"],
            "files_sha256": {filename: hashlib.sha256(files[filename].encode()).hexdigest() for filename in (name + ".cfg", "logback.xml")}}
        files[name + "-contract.json"] = json.dumps(contract, sort_keys=True, indent=2) + "\n"
        digests[name] = hashlib.sha256(files[name + "-contract.json"].encode()).hexdigest()
    if sum(len(content.encode()) for content in files.values()) > 400 * 1024:
        raise ValueError("Member package exceeds the configuration payload limit")
    if runtime in {"rke2", "k3s", "k8s"}:
        files = {"zookeeper-members.yaml": kubernetes(plan, files, digests)}
    elif runtime == "docker":
        for member in plan["members"]:
            files[member["name"] + "-compose.yaml"] = docker(plan, member, digests[member["name"]])
    else:
        for member in plan["members"]:
            files[member["name"] + ".service"] = native_unit(plan, member, digests[member["name"]])
    files["render-status.json"] = json.dumps({"component": "zookeeper-three-member", "runtime": runtime,
        "rendered": True, "deployed": False, "quorum_verified": False, "ha_verified": False,
        "initialize_if_empty": plan["initialize_if_empty"], "required_tls_files_per_member": list(TLS_FILES),
        "contract_sha256": digests, "remaining": ["real-java-and-tls-configuration-acceptance", "private-pki-and-znode-acl-policy",
            "three-physical-failure-domains-and-network-paths", "real-session-write-election-and-partition-tests",
            "one-member-at-a-time-upgrade-and-key-rotation", "snapshot-log-off-platform-backup-and-restore-tests"]}, indent=2) + "\n"
    return files


def kubernetes(plan: dict, files: dict, digests: dict) -> str:
    namespace = plan["namespace"]
    config_name = "zookeeper-config-" + hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:12]
    labels = {"app.kubernetes.io/name": "zookeeper", "platform.gitops/ensemble": plan["ensemble_id"]}
    objects = [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": config_name, "namespace": namespace},
                "immutable": True, "data": files}]
    for member in plan["members"]:
        member_labels = {**labels, "platform.gitops/member": member["name"]}
        meta = {"name": member["name"], "namespace": namespace, "labels": member_labels}
        ports = [{"name": name, "containerPort": plan[field]} for name, field in
                 (("client-tls", "client_port"), ("quorum-tls", "quorum_port"), ("election-tls", "election_port"))]
        objects += [
            {"apiVersion": "v1", "kind": "Service", "metadata": meta, "spec": {"type": "ClusterIP",
                "publishNotReadyAddresses": True, "selector": member_labels,
                "ports": [{"name": port["name"], "port": port["containerPort"], "targetPort": port["name"]} for port in ports]}},
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta, "spec": {
                "replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": member_labels},
                "template": {"metadata": {"labels": member_labels}, "spec": {
                    "automountServiceAccountToken": False, "terminationGracePeriodSeconds": 60,
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
                        {"matchExpressions": [{"key": plan["failure_domain_key"], "operator": "In", "values": [member["failure_domain"]]}]}]}},
                        "podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [
                            {"labelSelector": {"matchLabels": labels}, "topologyKey": "kubernetes.io/hostname"}]}},
                    "containers": [{"name": "zookeeper", "image": plan["image"], "command": command(member, digests[member["name"]], initialize=plan["initialize_if_empty"]),
                        "env": [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}], "ports": ports,
                        "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                        "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"cpu": "2", "memory": "2Gi"}},
                        # Readiness is not liveness: a lost quorum must not cause
                        # a kubelet-driven restart cascade across all members.
                        "readinessProbe": {"exec": {"command": command(member, digests[member["name"]], initialize=False, probe=True)},
                                           "periodSeconds": 30, "timeoutSeconds": 8, "failureThreshold": 2},
                        "volumeMounts": [{"name": "config", "mountPath": "/etc/zookeeper", "readOnly": True},
                                         {"name": "private", "mountPath": "/etc/zookeeper/private", "readOnly": True},
                                         {"name": "state", "mountPath": "/var/lib/zookeeper"}, {"name": "tmp", "mountPath": "/tmp"}]}],
                    "volumes": [{"name": "config", "configMap": {"name": config_name}},
                        {"name": "private", "secret": {"secretName": member["tls_secret"], "defaultMode": 0o440,
                            "items": [{"key": name, "path": name} for name in TLS_FILES]}},
                        {"name": "state", "persistentVolumeClaim": {"claimName": member["claim"]}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}}],
                }}}},
        ]
    objects.append({"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {"name": "zookeeper-quorum", "namespace": namespace},
                    "spec": {"minAvailable": 2, "selector": {"matchLabels": labels}}})
    return yaml.safe_dump_all(objects, sort_keys=False)


def docker(plan: dict, member: dict, digest: str) -> str:
    service = {"image": plan["image"], "user": "1000:1000", "read_only": True,
        "entrypoint": command(member, digest, initialize=plan["initialize_if_empty"]), "command": [],
        "restart": "unless-stopped", "stop_grace_period": "60s", "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
        "mem_limit": "2g", "cpus": 2.0, "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "tmpfs": ["/tmp:rw,nosuid,nodev,size=256m,mode=1777"],
        "ports": [f"{member['bind_address']}:{plan[field]}:{plan[field]}" for field in ("client_port", "quorum_port", "election_port")],
        "volumes": [{"type": "bind", "source": source, "target": target, "read_only": readonly,
                     "bind": {"create_host_path": False}} for source, target, readonly in (
            (".", "/etc/zookeeper", True), ("./private/" + member["name"], "/etc/zookeeper/private", True),
            (member["state_host_path"], "/var/lib/zookeeper", False))],
        "healthcheck": {"test": ["CMD", *command(member, digest, initialize=False, probe=True)], "interval": "30s", "timeout": "8s", "retries": 2},
    }
    # A file per host, rather than three replicas on one Docker daemon.
    return yaml.safe_dump({"services": {member["name"]: service}}, sort_keys=False)


def native_unit(plan: dict, member: dict, digest: str) -> str:
    args = command(member, digest, initialize=plan["initialize_if_empty"])
    return f"""[Unit]
Description=Guarded ZooKeeper quorum member {member['name']}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=zookeeper
Group=zookeeper
Environment=PATH=/opt/java/bin:/usr/bin:/bin
Environment=PYTHONDONTWRITEBYTECODE=1
WorkingDirectory={member['state_host_path']}
ExecStart=/usr/bin/python3 {' '.join(args[1:])}
Restart=on-failure
RestartSec=10
TimeoutStopSec=60
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths={member['state_host_path']}
CapabilityBoundingSet=
UMask=0077
MemoryMax=2G
CPUQuota=200%

[Install]
WantedBy=multi-user.target
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--zuul-plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan, ci = [loads_strict_json(read_bounded_text(path, encoding="utf-8")) for path in (args.plan, args.zuul_plan)]
        write_bundle(render(plan, ci, args.runtime), args.output_dir, private_root=ROOT / "private")
    except (ValueError, OSError, RuntimeError):
        print("ZooKeeper render stopped; inspect private member, endpoint, artifact and output selections.", file=sys.stderr)
        return 1
    print(json.dumps({"rendered": True, "runtime": args.runtime, "deployed": False, "quorum_verified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
