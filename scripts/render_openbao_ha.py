#!/usr/bin/env python3
"""Render optional three-voter OpenBao Raft packages; never initialize or unseal."""

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
LABELS = {"app.kubernetes.io/name": "platform-openbao"}
PATH = re.compile(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+")


def label(value):
    return dns_name(value) and "." not in value and len(value) <= 63


def exact(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise ValueError("Missing or unsupported OpenBao fields")


def validate(plan):
    exact(plan, "schema_version version image namespace cluster_domain private_dir binary binary_sha256 audit_dir members network")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or plan["version"] != "2.7.2":
        raise ValueError("Use the reviewed OpenBao 2.7.2 package family")
    if not isinstance(plan["image"], str) or not IMAGE.fullmatch(plan["image"]):
        raise ValueError("Pin a reviewed image digest")
    if not label(plan["namespace"]) or not dns_name(plan["cluster_domain"]):
        raise ValueError("Declare an explicit namespace and cluster domain")
    if not isinstance(plan["binary_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", plan["binary_sha256"]):
        raise ValueError("Pin the native executable independently of its archive")
    paths = [plan[k] for k in ("private_dir", "binary", "audit_dir")]
    members = plan["members"]
    if not isinstance(members, list) or len(members) != 3:
        raise ValueError("Declare exactly three independent voting members")
    for member in members:
        exact(member, "name node address failure_domain data_dir data_claim audit_claim tls_secret")
        for key in ("name", "failure_domain", "data_claim", "audit_claim", "tls_secret"):
            if not label(member[key]): raise ValueError("Use explicit member identities and private input references")
        if not dns_name(member["node"]): raise ValueError("Declare each reviewed Linux host")
        address = ipaddress.IPv4Address(member["address"])
        if (str(address) != member["address"] or address.is_loopback or address.is_unspecified
                or address.is_multicast or address.is_link_local or address.is_reserved):
            raise ValueError("Declare specific routable peer IPv4 addresses")
        paths.append(member["data_dir"])
    for key in ("name", "node", "address", "failure_domain", "data_claim", "audit_claim", "tls_secret"):
        if len({m[key] for m in members}) != 3: raise ValueError("Members require distinct identities, hosts, storage, TLS and failure domains")
    if len({m[k] for m in members for k in ("data_claim", "audit_claim")}) != 6:
        raise ValueError("Keep each member's Raft and audit claims separate")
    if any(not isinstance(p, str) or not PATH.fullmatch(p) or any(part in {".", ".."} for part in p.split("/")) for p in paths):
        raise ValueError("Use explicit canonical Linux paths")
    if len(set(paths)) != len(paths) or any(b.startswith(a + "/") for a in paths for b in paths if a != b):
        raise ValueError("Keep executable, credentials, audit and member data paths disjoint")
    exact(plan["network"], "ingress_namespaces monitoring_namespace")
    ingress = plan["network"]["ingress_namespaces"]
    if not isinstance(ingress, list) or not 1 <= len(ingress) <= 8 or not all(label(n) for n in ingress) or len(set(ingress)) != len(ingress):
        raise ValueError("Select explicit client namespaces")
    if not label(plan["network"]["monitoring_namespace"]): raise ValueError("Select the authenticated monitoring namespace")
    return plan


def origin(plan, member, runtime, port):
    host = member["address"] if runtime in {"raw", "docker"} else member["name"] + "." + plan["namespace"] + ".svc." + plan["cluster_domain"]
    return "https://" + host + ":" + str(port)


def configuration(plan, member, runtime):
    native = runtime == "raw"
    private = plan["private_dir"] + "/" + member["name"] if native else "/run/openbao"
    data = member["data_dir"] if native else "/var/lib/openbao"
    audit = plan["audit_dir"] if native else "/var/log/openbao"
    bind = member["address"] if native or runtime == "docker" else "0.0.0.0"
    return {"ui": True, "disable_mlock": True, "log_level": "info", "log_format": "json",
        "api_addr": origin(plan, member, runtime, 8200), "cluster_addr": origin(plan, member, runtime, 8201),
        "listener": [{"tcp": {"address": bind + ":8200", "cluster_address": bind + ":8201",
            "tls_cert_file": private + "/tls/tls.crt", "tls_key_file": private + "/tls/tls.key",
            "tls_client_ca_file": private + "/tls/ca.crt", "tls_min_version": "tls12", "tls_max_version": "tls13"}}],
        "storage": {"raft": {"path": data, "node_id": member["name"],
            "retry_join": [{"leader_api_addr": origin(plan, peer, runtime, 8200), "leader_ca_cert_file": private + "/tls/ca.crt"}
                           for peer in sorted(plan["members"], key=lambda m: m["name"]) if peer["name"] != member["name"]]}},
        "audit": [{"file": {"platform-local": {"description": "Local HMAC audit; collect off-platform separately",
            "options": {"file_path": audit + "/audit.json", "mode": "0600", "log_raw": "false"}}}}],
        "telemetry": {"disable_hostname": True, "prometheus_retention_time": "30s"}}


def kubernetes(plan, configs):
    namespace = plan["namespace"]
    config_name = "openbao-config-" + hashlib.sha256(json.dumps(configs, sort_keys=True).encode()).hexdigest()[:12]
    objects = [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": config_name, "namespace": namespace}, "immutable": True, "data": configs}]
    for member in plan["members"]:
        labels = {**LABELS, "platform.gitops/member": member["name"]}
        meta = {"name": member["name"], "namespace": namespace, "labels": labels}
        objects.append({"apiVersion": "v1", "kind": "Service", "metadata": meta, "spec": {"type": "ClusterIP", "publishNotReadyAddresses": True,
            "selector": labels, "ports": [{"name": name, "port": port, "targetPort": port} for name, port in (("https", 8200), ("raft", 8201))]}})
        objects.append({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta, "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": labels}, "template": {"metadata": {"labels": labels}, "spec": {
                "automountServiceAccountToken": False, "terminationGracePeriodSeconds": 60,
                "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
                "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [
                    {"key": "kubernetes.io/hostname", "operator": "In", "values": [member["node"]]},
                    {"key": "topology.kubernetes.io/zone", "operator": "In", "values": [member["failure_domain"]]}]}]}},
                    "podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{"labelSelector": {"matchLabels": LABELS}, "topologyKey": "kubernetes.io/hostname"}]}},
                "containers": [{"name": "openbao", "image": plan["image"], "command": ["/usr/bin/bao", "server", "-config=/etc/openbao/" + member["name"] + ".json"],
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                    "ports": [{"name": "https", "containerPort": 8200}, {"name": "raft", "containerPort": 8201}],
                    "resources": {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"cpu": "2", "memory": "2Gi"}},
                    "startupProbe": {"tcpSocket": {"port": "https"}, "periodSeconds": 5, "failureThreshold": 60},
                    "livenessProbe": {"tcpSocket": {"port": "https"}, "periodSeconds": 10},
                    "readinessProbe": {"httpGet": {"scheme": "HTTPS", "path": "/v1/sys/health?standbyok=true", "port": "https"}, "periodSeconds": 5},
                    "volumeMounts": [{"name": name, "mountPath": path, **({"readOnly": True} if ro else {})} for name, path, ro in (
                        ("config", "/etc/openbao", True), ("tls", "/run/openbao/tls", True), ("data", "/var/lib/openbao", False), ("audit", "/var/log/openbao", False), ("tmp", "/tmp", False))]}],
                "volumes": [{"name": "config", "configMap": {"name": config_name}},
                    {"name": "tls", "secret": {"secretName": member["tls_secret"], "defaultMode": 0o440,
                        "items": [{"key": name, "path": name} for name in ("tls.crt", "tls.key", "ca.crt")]}},
                    {"name": "data", "persistentVolumeClaim": {"claimName": member["data_claim"]}},
                    {"name": "audit", "persistentVolumeClaim": {"claimName": member["audit_claim"]}},
                    {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "128Mi"}}]}}}})
    objects.append({"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {"name": "platform-openbao", "namespace": namespace},
        "spec": {"minAvailable": 2, "selector": {"matchLabels": LABELS}}})
    def ns(name): return {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": name}}}
    peer = {"podSelector": {"matchLabels": LABELS}}
    tcp = lambda *ports: [{"protocol": "TCP", "port": port} for port in ports]
    objects.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "platform-openbao", "namespace": namespace}, "spec": {
        "podSelector": {"matchLabels": LABELS}, "policyTypes": ["Ingress", "Egress"],
        "ingress": [{"from": [peer], "ports": tcp(8200, 8201)}, {"from": [ns(n) for n in plan["network"]["ingress_namespaces"]] + [ns(plan["network"]["monitoring_namespace"])], "ports": tcp(8200)}],
        "egress": [{"to": [peer], "ports": tcp(8200, 8201)}, {"to": [{**ns("kube-system"), "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
            "ports": [{"protocol": protocol, "port": 53} for protocol in ("TCP", "UDP")]}]}})
    return objects


def render(plan, runtime):
    validate(plan)
    if runtime not in RUNTIMES: raise ValueError("Unknown OpenBao runtime")
    configs = {m["name"] + ".json": json.dumps(configuration(plan, m, runtime), sort_keys=True, indent=2) + "\n" for m in plan["members"]}
    files = dict(configs)
    if runtime in {"rke2", "k3s", "k8s"}:
        files = {"openbao.yaml": yaml.safe_dump_all(kubernetes(plan, configs), sort_keys=False)}
    else:
        for member in plan["members"]:
            name = member["name"]
            if runtime == "docker":
                files[name + "-compose.yaml"] = yaml.safe_dump({"services": {name: {"image": plan["image"], "entrypoint": ["/usr/bin/bao"],
                    "command": ["server", "-config=/etc/openbao/config.json"], "user": "1000:1000", "network_mode": "host", "read_only": True,
                    "restart": "unless-stopped", "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "pids_limit": 512,
                    "mem_limit": "2g", "memswap_limit": "2g", "cpus": 2.0, "tmpfs": ["/tmp:rw,nosuid,nodev,noexec,mode=1777,size=128m"],
                    "volumes": [{"type": "bind", "source": source, "target": target, "read_only": ro, "bind": {"create_host_path": False}} for source, target, ro in (
                        (plan["private_dir"] + "/" + name + "/config.json", "/etc/openbao/config.json", True),
                        (plan["private_dir"] + "/" + name + "/tls", "/run/openbao/tls", True),
                        (member["data_dir"], "/var/lib/openbao", False), (plan["audit_dir"], "/var/log/openbao", False))]}}}, sort_keys=False)
            else:
                cfg = plan["private_dir"] + "/" + name + "/config.json"
                command = " ".join(shlex.quote(v) for v in (plan["binary"], "server", "-config=" + cfg))
                files[name + ".service"] = f"""[Unit]
Description=OpenBao Raft voter {name}; initialization and unseal are separate
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=openbao
Group=openbao
ExecStartPre=/usr/bin/sha256sum --check --strict {plan['private_dir']}/{name}/binary.sha256
ExecStart={command}
Restart=on-failure
RestartSec=5
TimeoutStopSec=60
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
CapabilityBoundingSet=
UMask=0077
MemoryMax=2G
MemorySwapMax=0
TasksMax=512
ReadWritePaths={member['data_dir']} {plan['audit_dir']}

[Install]
WantedBy=multi-user.target
"""
                files[name + "-binary.sha256"] = plan["binary_sha256"] + "  " + plan["binary"] + "\n"
    files["openbao-contract.json"] = json.dumps({"schema_version": 1, "runtime": runtime, "plan": plan,
        "files_sha256": {k: hashlib.sha256(v.encode()).hexdigest() for k, v in sorted(files.items())}}, sort_keys=True, indent=2) + "\n"
    files["render-status.json"] = json.dumps({"component": "openbao-raft-three-voter", "runtime": runtime, "rendered": True,
        "deployed": False, "initialized": False, "unsealed": False, "audit_verified": False, "restricted_identities_verified": False,
        "quorum_recovery_verified": False, "off_platform_restore_verified": False, "ha_verified": False, "full_stack_verified": False,
        "remaining": ["reviewed-image-and-native-binary-provenance", "independent-encrypted-member-storage-and-tls", "guarded-workload-installation",
            "off-platform-key-custody-initialization-and-unseal-ceremony", "restricted-authentication-policy-and-root-revocation",
            "audit-forwarding-alerts-and-rotation", "node-loss-upgrade-and-independent-snapshot-restore"]}, indent=2) + "\n"
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True); args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, max_bytes=128 * 1024))
        write_bundle(render(plan, args.runtime), args.output_dir, private_root=ROOT / "private")
        print('{"rendered":true,"deployed":false,"initialized":false,"unsealed":false,"ha_verified":false}')
        return 0
    except Exception:
        print("OpenBao packaging refused; private values were not printed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
