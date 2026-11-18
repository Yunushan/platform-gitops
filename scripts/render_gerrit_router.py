#!/usr/bin/env python3
"""Render opt-in Gerrit active/passive entry-point artifacts; never deploy them."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sys

import yaml

from bounded_file import read_bounded_text
from gateway_edge_health import vrrp_config
from standalone_python import standalone_script
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
RUNTIMES = ("rke2", "k3s", "k8s", "docker", "raw")
DNS_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")
IMAGE = re.compile(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}")
LABELS = {"app.kubernetes.io/name": "gerrit-router"}
CA_FILE = "/etc/haproxy/backend-ca/ca.crt"
READINESS_PORT = 8404
ROUTE_PATH = "/routez"
ROUTE_ID_MARKER = "@ROUTE_CONFIG_ID@"
VIP_PENDING = "/var/lib/gerrit-router/pending.json"
VIP_BOOT_GUARD = f"ConditionPathExists=!{VIP_PENDING}\n"
NATIVE_CONFIG = "/etc/haproxy"
HEALTH_HELPER = NATIVE_CONFIG + "/gateway_edge_health.py"


def health_program():
    return standalone_script(Path(__file__).with_name("gateway_edge_health.py"), "http_transport")


class RouterError(ValueError):
    pass


def dns_name(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 253 and bool(DNS_NAME.fullmatch(value))


def validate(plan: object) -> dict:
    if not isinstance(plan, dict):
        raise RouterError("The router plan must be an object")
    allowed = {"schema_version", "canonical_host", "primary_host", "backup_host",
               "https_port", "ssh_port", "haproxy_image", "namespace", "ca_secret"}
    if set(plan).difference({"hold_primary", "native_vip", "ssh_node_port"}) != allowed:
        raise RouterError("Router plan has missing or unsupported fields")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise RouterError("Unsupported router plan schema")
    if type(plan.get("hold_primary", False)) is not bool:
        raise RouterError("hold_primary must be a boolean")
    for key in ("canonical_host", "primary_host", "backup_host"):
        if not dns_name(plan[key]):
            raise RouterError(f"{key} must be a plain DNS hostname")
    if plan["primary_host"] == plan["backup_host"]:
        raise RouterError("Two distinct Gerrit primary hostnames are required")
    if plan["canonical_host"] in (plan["primary_host"], plan["backup_host"]):
        raise RouterError("The public entry point must not be a Gerrit primary address")
    for key in ("namespace", "ca_secret"):
        if not dns_name(plan[key]) or "." in plan[key] or len(plan[key]) > 63:
            raise RouterError(f"{key} must be a Kubernetes DNS label")
    for key in ("https_port", "ssh_port"):
        if type(plan[key]) is not int or not 1024 <= plan[key] <= 65535:
            raise RouterError(f"{key} must be an unprivileged TCP port")
    if plan["https_port"] == plan["ssh_port"]:
        raise RouterError("HTTPS and SSH ports must differ")
    image = plan["haproxy_image"]
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        raise RouterError("A digest-pinned HAProxy image is required")
    if "native_vip" in plan:
        validate_vip(plan["native_vip"], plan)
    if "ssh_node_port" in plan and (type(plan["ssh_node_port"]) is not int or not 30000 <= plan["ssh_node_port"] <= 32767):
        raise RouterError("Select a Kubernetes SSH NodePort from 30000 to 32767")
    return plan


def validate_vip(vip_plan, router_plan):
    if not isinstance(vip_plan, dict) or set(vip_plan) != {"vip", "vrid", "entry_hosts"}:
        raise RouterError("Declare an IPv4 VIP, VRID and two entry hosts")
    vrid = vip_plan["vrid"]
    if type(vrid) is not int or not 1 <= vrid <= 255 or not isinstance(vip_plan["vip"], str):
        raise RouterError("Invalid VRRP identity")
    vip = ipaddress.IPv4Interface(vip_plan["vip"])
    if (str(vip) != vip_plan["vip"] or not 16 <= vip.network.prefixlen <= 30
            or vip.ip.is_unspecified or vip.ip.is_multicast or vip.ip.is_loopback or vip.ip.is_link_local
            or vip.ip in {vip.network.network_address, vip.network.broadcast_address}):
        raise RouterError("Use a canonical routable IPv4 VIP on the entry subnet")
    hosts = vip_plan["entry_hosts"]
    if not isinstance(hosts, list) or len(hosts) != 2:
        raise RouterError("Exactly two VIP entry hosts are required")
    excluded = {str(vip.ip), str(vip.network.network_address), str(vip.network.broadcast_address),
                router_plan["primary_host"], router_plan["backup_host"], router_plan["canonical_host"]}
    for host in hosts:
        if not isinstance(host, dict) or set(host) != {"name", "address", "interface", "failure_domain"}:
            raise RouterError("Declare both host identities, addresses, interfaces and failure domains")
        for key in ("name", "failure_domain"):
            if not dns_name(host[key]) or "." in host[key] or len(host[key]) > 40:
                raise RouterError("Invalid native VIP host identity")
        interface = host["interface"]
        if not isinstance(interface, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", interface) or interface == "lo":
            raise RouterError("Invalid native VIP interface")
        address = host["address"]
        if not isinstance(address, str):
            raise RouterError("Use an explicit IPv4 address for each entry host")
        ip = ipaddress.IPv4Address(address)
        if str(ip) != address or ip not in vip.network or address in excluded:
            raise RouterError("Entry hosts need distinct addresses on the VIP subnet")
    if any(hosts[0][key] == hosts[1][key] for key in ("name", "address", "failure_domain")):
        raise RouterError("Use distinct native VIP host identities")
    if str(vip.ip) in (
        router_plan["primary_host"], router_plan["backup_host"], router_plan["canonical_host"],
    ):
        raise RouterError("The VIP must differ from Gerrit backend addresses")


def config_template(plan: dict) -> str:
    plan = validate(plan)
    host = plan["canonical_host"]
    https, ssh = plan["https_port"], plan["ssh_port"]
    hold = " disabled" if plan.get("hold_primary", False) else ""
    # A single health decision controls HTTPS and SSH. No independent SSH
    # balancing, HTTP-method heuristic, or double TLS encryption is introduced.
    # srv_is_up() does not reflect a dynamic DOWN state on an unchecked SSH
    # server. Use the checked HTTPS role and the usable SSH backend count.
    health_request = (
        f"GET /config/server/healthcheck~status HTTP/1.1\r\n"
        f"Host: {host}\r\nConnection: close\r\n\r\n"
    ).encode("ascii").hex()
    return f"""global
    log stdout format raw local0
    maxconn 4096
    ssl-default-server-options ssl-min-ver TLSv1.2

defaults
    mode tcp
    log global
    timeout connect 5s
    timeout client 1h
    timeout server 1h
    timeout check 5s

frontend gerrit_https_entry
    bind :8443
    default_backend gerrit_https

frontend gerrit_ssh_entry
    bind :29418
    tcp-request connection reject if {{ nbsrv(gerrit_https) lt 1 }}
    default_backend gerrit_ssh

frontend gerrit_router_readiness
    mode http
    bind :{READINESS_PORT}
    timeout client 5s
    monitor-uri /readyz
    monitor fail if {{ nbsrv(gerrit_https) lt 1 }} || {{ nbsrv(gerrit_ssh) lt 1 }}
    acl route_probe method GET
    acl route_probe_path url {ROUTE_PATH}
    acl https_primary srv_is_up(gerrit_https/primary)
    acl https_backup srv_is_up(gerrit_https/backup)
    acl ssh_available nbsrv(gerrit_ssh) gt 0
    http-request return status 200 content-type text/plain string "{ROUTE_ID_MARKER} primary" if route_probe route_probe_path https_primary ssh_available
    http-request return status 200 content-type text/plain string "{ROUTE_ID_MARKER} backup" if route_probe route_probe_path !https_primary https_backup ssh_available
    http-request return status 503 content-type text/plain string "{ROUTE_ID_MARKER} unavailable" if route_probe route_probe_path
    http-request deny

backend gerrit_https
    option tcp-check
    tcp-check connect port {ssh}
    tcp-check expect rstring ^SSH-2.0-
    tcp-check connect port {https} ssl sni {host}
    tcp-check send-binary {health_request}
    tcp-check expect rstring ^HTTP/1[.][01][[:space:]]200[[:space:]]
    default-server init-state fully-down inter 3s fall 3 rise 2 on-marked-down shutdown-sessions
    server primary {plan['primary_host']}:{https} check verify required ca-file {CA_FILE} verifyhost {host} on-marked-up shutdown-backup-sessions{hold}
    server backup {plan['backup_host']}:{https} check verify required ca-file {CA_FILE} verifyhost {host} backup

backend gerrit_ssh
    use-server primary if {{ srv_is_up(gerrit_https/primary) }}
    use-server backup if {{ srv_is_up(gerrit_https/backup) }}
    default-server on-marked-down shutdown-sessions
    server primary {plan['primary_host']}:{ssh} track gerrit_https/primary on-marked-up shutdown-backup-sessions{hold}
    server backup {plan['backup_host']}:{ssh} track gerrit_https/backup backup
"""


def haproxy_config(plan: dict) -> str:
    template = config_template(plan)
    return template.replace(ROUTE_ID_MARKER, hashlib.sha256(template.encode("ascii")).hexdigest())


def route_config_id(plan: dict) -> str:
    return hashlib.sha256(config_template(plan).encode("ascii")).hexdigest()


def ssh_nodeport_service(namespace: str, node_port: int) -> dict:
    return {"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": "gerrit-router-ssh", "namespace": namespace, "labels": dict(LABELS)},
            "spec": {"type": "NodePort", "externalTrafficPolicy": "Local", "selector": dict(LABELS),
                     "ports": [{"name": "ssh", "protocol": "TCP", "port": 29418, "targetPort": "ssh", "nodePort": node_port}]}}


def kubernetes_resources(plan: dict, config: str) -> list[dict]:
    namespace = plan["namespace"]
    config_name = "gerrit-router-" + hashlib.sha256(config.encode()).hexdigest()[:20]
    def meta(name: str = "gerrit-router") -> dict:
        return {"name": name, "namespace": namespace, "labels": dict(LABELS)}
    resources = [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta(config_name), "immutable": True,
         "data": {"haproxy.cfg": config}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta(),
         "spec": {"replicas": 2, "selector": {"matchLabels": LABELS},
                  "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 0}},
                  "template": {"metadata": {"labels": LABELS}, "spec": {
                      "automountServiceAccountToken": False,
                      "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532,
                                          "fsGroup": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
                      "affinity": {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [
                          {"labelSelector": {"matchLabels": LABELS}, "topologyKey": "kubernetes.io/hostname"}]}},
                      "containers": [{"name": "haproxy", "image": plan["haproxy_image"],
                          "command": ["haproxy"], "args": ["-W", "-db", "-f", "/etc/haproxy/haproxy.cfg"],
                          "ports": [{"name": "https", "containerPort": 8443}, {"name": "ssh", "containerPort": 29418},
                                    {"name": "readiness", "containerPort": READINESS_PORT}],
                          "resources": {"requests": {"cpu": "100m", "memory": "64Mi"},
                                        "limits": {"cpu": "1", "memory": "256Mi"}},
                          "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                                              "capabilities": {"drop": ["ALL"]}},
                          "readinessProbe": {"httpGet": {"path": "/readyz", "port": "readiness"}, "periodSeconds": 5},
                          "livenessProbe": {"tcpSocket": {"port": "https"}, "periodSeconds": 10},
                          "volumeMounts": [{"name": "config", "mountPath": "/etc/haproxy", "readOnly": True},
                                           {"name": "ca", "mountPath": "/etc/haproxy/backend-ca", "readOnly": True}]}],
                      "volumes": [{"name": "config", "configMap": {"name": config_name}},
                                  {"name": "ca", "secret": {"secretName": plan["ca_secret"],
                                                             "items": [{"key": "ca.crt", "path": "ca.crt"}]}}],
                  }}}},
        {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": meta(),
         "spec": {"minAvailable": 1, "selector": {"matchLabels": LABELS}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": meta(),
         "spec": {"type": "ClusterIP", "selector": LABELS,
                  "ports": [{"name": "https", "port": 443, "targetPort": "https"},
                            {"name": "ssh", "port": 29418, "targetPort": "ssh"}]}},
    ]
    if "ssh_node_port" in plan:
        resources.append(ssh_nodeport_service(namespace, plan["ssh_node_port"]))
    return resources


def native_vip_files(plan):
    vip = plan["native_vip"]
    settings = {**vip, "name": "gerrit", "health_port": READINESS_PORT}
    identity = route_config_id(plan)
    return {
        "gateway_edge_health.py": health_program(),
        "gerrit-router-vrrp.service": f"""[Unit]
Description=Gerrit router virtual IP
Wants=network-online.target
After=network-online.target gerrit-router.service
{VIP_BOOT_GUARD}
[Service]
Type=simple
ExecStartPre=/usr/sbin/keepalived --config-test --vrrp --use-file=/etc/haproxy/gerrit-router-vrrp.conf
ExecStart=/usr/sbin/keepalived --dont-fork --log-console --vrrp --no_bfd --use-file=/etc/haproxy/gerrit-router-vrrp.conf --pid=/run/gerrit-router-vrrp/main.pid --vrrp_pid=/run/gerrit-router-vrrp/child.pid
Restart=on-failure
RuntimeDirectory=gerrit-router-vrrp
RuntimeDirectoryMode=0700
ReadWritePaths=/run/gerrit-router-vrrp
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW CAP_SETGID
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
UMask=0077

[Install]
WantedBy=multi-user.target
""",
        **{f"keepalived-{host['name']}.conf": vrrp_config(
            settings, host, index, helper=HEALTH_HELPER, identity=identity, gerrit=True)
           for index, host in enumerate(vip["entry_hosts"])},
    }


def render(plan: dict, runtime: str) -> dict[str, str]:
    plan = validate(plan)
    if runtime not in RUNTIMES:
        raise RouterError("Unsupported router runtime")
    if "native_vip" in plan and runtime != "raw":
        raise RouterError("The native VIP pair uses independent Linux hosts")
    if "ssh_node_port" in plan and runtime not in {"rke2", "k3s", "k8s"}:
        raise RouterError("The SSH NodePort requires a Kubernetes router deployment")
    config = haproxy_config(plan)
    files = {"haproxy.cfg": config}
    if runtime in {"rke2", "k3s", "k8s"}:
        files["router.yaml"] = yaml.safe_dump_all(kubernetes_resources(plan, config), sort_keys=False)
    elif runtime == "docker":
        compose = {"services": {"gerrit-router": {
            "image": plan["haproxy_image"], "user": "65532:65532",
            "entrypoint": ["haproxy"], "command": ["-W", "-db", "-f", "/etc/haproxy/haproxy.cfg"],
            "restart": "unless-stopped", "read_only": True, "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "ports": ["8443:8443", "29418:29418"],
            "volumes": [{"type": "bind", "source": "./haproxy.cfg", "target": "/etc/haproxy/haproxy.cfg", "read_only": True,
                         "bind": {"create_host_path": False}},
                        {"type": "bind", "source": "./backend-ca.crt", "target": CA_FILE, "read_only": True,
                         "bind": {"create_host_path": False}}],
            "mem_limit": "256m", "cpus": 1.0,
        }}}
        files["compose.yaml"] = yaml.safe_dump(compose, sort_keys=False)
    else:
        files["gerrit-router.service"] = """[Unit]
Description=Gerrit active/passive HTTPS and SSH router
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=haproxy
Group=haproxy
ExecStartPre=/usr/sbin/haproxy -c -f /etc/haproxy/haproxy.cfg
ExecStart=/usr/sbin/haproxy -W -db -f /etc/haproxy/haproxy.cfg
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
RestrictSUIDSGID=yes
CapabilityBoundingSet=
UMask=0077

[Install]
WantedBy=multi-user.target
"""
        if "native_vip" in plan:
            files.update(native_vip_files(plan))
            files["gerrit-router.service"] = files["gerrit-router.service"].replace(
                "Wants=network-online.target\n", "Wants=network-online.target\n" + VIP_BOOT_GUARD, 1)
    files["render-status.json"] = json.dumps({
        "schema_version": 1, "runtime": runtime, "component": "gerrit-router",
        "topology": "two-primary-active-passive", "deployed": False, "ha_verified": False,
        "hold_primary": plan.get("hold_primary", False),
        "route_config_id": route_config_id(plan),
        "native_vip_configured": "native_vip" in plan,
        "prerequisites": ["initialized-gerrit-ha-primaries-with-shared-repositories",
                          "version-compatible-ha-global-refdb-and-healthcheck-artifacts",
                          "backend-certificates-for-canonical-host-and-trusted-ca",
                          "stable-per-primary-dns-and-identical-ssh-host-keys",
                          "redundant-external-entry-and-network-isolation",
                          "private-authentication-and-peer-forwarding-config",
                          "protocol-and-node-loss-live-acceptance"],
    }, indent=2) + "\n"
    return files


def write_bundle(files: dict[str, str], output: Path, *, private_root: Path) -> None:
    # Refuse public output, replacement, and symlink escapes. Create an entirely
    # new private directory: no existing configuration can be overwritten.
    if not files or any(not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", name) or name in {".", ".."}
                        for name in files):
        raise RouterError("Invalid bundle file names")
    resolved = output.resolve()
    try:
        relative = resolved.relative_to(private_root.resolve())
    except ValueError:
        raise RouterError("Output must be a new directory under private/") from None
    if not relative.parts or output.exists() or output.is_symlink():
        raise RouterError("Output directory must not already exist")
    resolved.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, content in files.items():
        descriptor = os.open(resolved / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    # Errors deliberately leave the new partial directory for inspection; never
    # delete private material as part of an automatic cleanup.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        files = render(plan, args.runtime)
        write_bundle(files, args.output_dir, private_root=ROOT / "private")
    except (ValueError, OSError):
        # Input and OS errors may contain private paths/addresses: do not echo.
        print("Router render failed: check the private plan, required fields and new private output directory.", file=sys.stderr)
        return 1
    print(json.dumps({"rendered": True, "runtime": args.runtime, "files": len(files),
                      "deployed": False, "ha_verified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
