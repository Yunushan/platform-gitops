#!/usr/bin/env python3
"""Render an opt-in HAProxy/Keepalived external entry pair for a reviewed Gateway."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import sys

import yaml

from bounded_file import read_bounded_text
from deploy_cilium_gateway import load_package as load_gateway
from gateway_edge_health import vrrp_config
from render_gerrit_router import IMAGE, LABELS as GERRIT_ROUTER_LABELS, RUNTIMES, dns_name, health_program, write_bundle
from render_cilium_gateway import listener_name, listener_ports, name
from start_gateway_edge import PROGRAMS, reviewed_version, NATIVE_TRUST
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
BASE = "/etc/platform/gateway-edge"
CA = "/run/platform/gateway-edge/ca.crt"
OWNER = "platform.gitops/gateway-edge"
KEEPALIVED_CAPABILITIES = ["NET_ADMIN", "NET_RAW", "SETGID"]
SSH_SERVICES = ("gitlab_ssh", "gerrit_ssh")


def digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def address(value):
    if not isinstance(value, str): return False
    try:
        ip = ipaddress.ip_address(value)
        return ip.version == 4 and not (ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_link_local)
    except (TypeError, ValueError):
        return False


def validate_ssh(ssh):
    if (not isinstance(ssh, dict) or set(ssh) != {"hostname", "port", "node_port"}
            or not dns_name(ssh["hostname"]) or "." not in ssh["hostname"]
            or type(ssh["port"]) is not int or not 1 <= ssh["port"] <= 65535
            or type(ssh["node_port"]) is not int or not 30000 <= ssh["node_port"] <= 32767
            or ssh["port"] == ssh["node_port"]):
        raise ValueError("Declare the SSH hostname, public TCP port, and distinct NodePort")
    return ssh


def validate_gitlab_ssh(ssh):
    return validate_ssh(ssh)


def gerrit_route(gateway, hostname):
    for route in gateway["routes"]:
        if (route["hostname"] == hostname and route["protocol"] == "tls"
                and route["service"] == "gerrit-router" and route["port"] == 443 and route["target_port"] == 8443
                and route["pod_selector"] == GERRIT_ROUTER_LABELS):
            return route
    raise ValueError("Bind Gerrit SSH to a matching TLSRoute through the Gerrit router")


def validate(plan, gateway, runtime):
    fields = {"schema_version", "name", "namespace", "gateway_contract_sha256", "vip", "vrid", "https_port", "health_port", "minimum_ready_routes",
              "entry_hosts", "backends", "routes", "images", "binaries", "ca_sha256", "ca_secret", "client_cidrs"}
    if (not isinstance(plan, dict) or set(plan) - set(SSH_SERVICES) != fields
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or runtime not in RUNTIMES or not name(plan["name"]) or len(plan["name"]) > 40 or not name(plan["namespace"]) or not name(plan["ca_secret"])
            or plan["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}
            or not digest(plan["gateway_contract_sha256"]) or not digest(plan["ca_sha256"])):
        raise ValueError("Invalid external entry plan")
    backend_ports = set(listener_ports(gateway).values())
    if (type(plan["vrid"]) is not int or not 1 <= plan["vrid"] <= 255
            or type(plan["https_port"]) is not int or not 1 <= plan["https_port"] <= 65535
            or type(plan["health_port"]) is not int or not 1024 <= plan["health_port"] <= 65535
            or plan["https_port"] == plan["health_port"] or {plan["https_port"], plan["health_port"]} & backend_ports):
        raise ValueError("Declare distinct public, backend and loopback-health ports")
    used_ports = {plan["https_port"], plan["health_port"]} | backend_ports
    for service in SSH_SERVICES:
        if service not in plan:
            continue
        ssh = validate_ssh(plan[service])
        ports = {ssh["port"], ssh["node_port"]}
        if ports & used_ports:
            raise ValueError("SSH ports conflict with the Gateway or edge ports")
        used_ports.update(ports)
    if "gerrit_ssh" in plan:
        gerrit_route(gateway, plan["gerrit_ssh"]["hostname"])
    if not isinstance(plan["client_cidrs"], list) or not 1 <= len(plan["client_cidrs"]) <= 32:
        raise ValueError("Declare approved external client networks")
    for cidr in plan["client_cidrs"]:
        if not isinstance(cidr, str) or ipaddress.ip_network(cidr, strict=True).version != 4:
            raise ValueError("Use explicit canonical IPv4 client CIDRs")
    vip = ipaddress.ip_interface(plan["vip"])
    if vip.version != 4 or not address(str(vip.ip)) or not 16 <= vip.network.prefixlen <= 30 or vip.ip in {vip.network.network_address, vip.network.broadcast_address}:
        raise ValueError("Declare the IPv4 VIP and common entry-node subnet")
    hosts = plan["entry_hosts"]
    if not isinstance(hosts, list) or len(hosts) != 2:
        raise ValueError("Exactly two distinct entry hosts are required")
    for host in hosts:
        if (not isinstance(host, dict) or set(host) != {"name", "address", "interface", "failure_domain", "node_name", "node_uid"}
                or not name(host["name"]) or len(host["name"]) > 12 or not name(host["failure_domain"])
                or not address(host["address"]) or ipaddress.ip_address(host["address"]) not in vip.network or host["address"] == str(vip.ip)
                or ipaddress.ip_address(host["address"]) in {vip.network.network_address, vip.network.broadcast_address}
                or not isinstance(host["interface"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", host["interface"])):
            raise ValueError("Declare entry interfaces, addresses and independent failure domains")
        bound = host["node_name"] is not None or host["node_uid"] is not None
        if (bound and (not dns_name(host["node_name"]) or not isinstance(host["node_uid"], str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", host["node_uid"]))
                or runtime in {"rke2", "k3s", "k8s"} and not bound):
            raise ValueError("Kubernetes entry hosts need exact Node names and UIDs")
    for field in ("name", "address", "failure_domain"):
        if len({host[field] for host in hosts}) != 2:
            raise ValueError("Entry hosts must be distinct")
    if hosts[0]["node_name"] is not None and hosts[0]["node_name"] == hosts[1]["node_name"]:
        raise ValueError("Never put both VIP peers on one Node")
    backends = plan["backends"]
    if not isinstance(backends, list) or not 2 <= len(backends) <= 16:
        raise ValueError("Select redundant Gateway serving nodes")
    for backend in backends:
        if (not isinstance(backend, dict) or set(backend) != {"node_name", "node_uid", "address"} or not dns_name(backend["node_name"])
                or not isinstance(backend["node_uid"], str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", backend["node_uid"])
                or not address(backend["address"]) or backend["address"] == str(vip.ip)):
            raise ValueError("Bind each serving address to its reviewed Node identity")
    if any(len({backend[key] for backend in backends}) != len(backends) for key in ("address", "node_name", "node_uid")):
        raise ValueError("Duplicate Gateway serving identity")
    routes = plan["routes"]
    known = {route["hostname"]: route for route in gateway["routes"]}
    if not isinstance(routes, list) or len(routes) != len(known) or {r.get("hostname") for r in routes if isinstance(r, dict)} != set(known):
        raise ValueError("Review every Gateway hostname in the external entry policy")
    for route in routes:
        if not isinstance(route, dict) or set(route) != {"hostname", "health_path", "health_status"}:
            raise ValueError("Invalid route health contract")
        if known[route["hostname"]]["protocol"] in {"http", "tls"}:
            if (not isinstance(route["health_path"], str) or not re.fullmatch(r"/[A-Za-z0-9_./~-]{0,199}", route["health_path"])
                    or type(route["health_status"]) is not int or not 200 <= route["health_status"] <= 299):
                raise ValueError("HTTP routes need an explicit unauthenticated read-only readiness endpoint")
        elif route["health_path"] is not None or route["health_status"] is not None:
            raise ValueError("This gRPC transport check establishes TLS reachability only")
    if type(plan["minimum_ready_routes"]) is not int or not 1 <= plan["minimum_ready_routes"] <= len(routes):
        raise ValueError("Declare the VIP route-health threshold")
    images_valid = runtime == "raw" and plan["images"] is None or (
        isinstance(plan["images"], dict) and set(plan["images"]) == {"haproxy", "keepalived"}
        and all(isinstance(image, str) and IMAGE.fullmatch(image) for image in plan["images"].values()))
    if (not images_valid
            or not isinstance(plan["binaries"], dict) or set(plan["binaries"]) != {"haproxy", "keepalived"}):
        raise ValueError("Pin the two daemon images and executable artifacts")
    for role, binary in plan["binaries"].items():
        expected_path = BASE + "/bin/" + role if runtime == "raw" else "/usr/sbin/" + role
        allowed = {"path", "sha256", "version"}
        if runtime == "raw" and role == "keepalived" and isinstance(binary, dict) and "config_opts_sha256" in binary: allowed.add("config_opts_sha256")
        if (not isinstance(binary, dict) or set(binary) != allowed or binary["path"] != expected_path
                or "config_opts_sha256" in binary and not digest(binary["config_opts_sha256"])
                or not digest(binary["sha256"]) or not reviewed_version(role, binary["version"])):
            raise ValueError("Declare exact daemon binary digests at their reviewed paths")
    return plan


def backend_name(hostname):
    return "route_" + hashlib.sha256(hostname.encode()).hexdigest()[:12]


def edge_identity(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def native_ca(plan):
    return str(NATIVE_TRUST / plan["ca_sha256"] / "ca.crt")


def haproxy_config(plan, gateway):
    ports = listener_ports(gateway)
    port_by_host = {route["hostname"]: ports[listener_name(route)] for route in gateway["routes"]}
    lines = ["global", "    log stdout format raw local0", "    maxconn 4096", "    ssl-default-server-options ssl-min-ver TLSv1.2", "",
        "defaults", "    mode tcp", "    log global", "    option tcplog", "    timeout connect 5s", "    timeout client 1h", "    timeout server 1h", "    timeout check 3s", "",
        "frontend external_https", "    bind :" + str(plan["https_port"]), "    tcp-request inspect-delay 5s",
        "    tcp-request connection reject unless { src -m ip " + " ".join(plan["client_cidrs"]) + " }",
        "    tcp-request content reject if { req.ssl_hello_type 1 } !{ req.ssl_sni -i -m str " + " ".join(r["hostname"] for r in plan["routes"]) + " }",
        "    tcp-request content accept if { req.ssl_hello_type 1 }"]
    for route in plan["routes"]:
        lines.append("    use_backend " + backend_name(route["hostname"]) + " if { req.ssl_sni -i " + route["hostname"] + " }")
    lines += ["    default_backend deny_unknown", "", "backend deny_unknown", "    tcp-request content reject", ""]
    for route in plan["routes"]:
        host = route["hostname"]
        lines += ["backend " + backend_name(host), "    balance roundrobin", "    default-server inter 2s fall 2 rise 2 init-state fully-down"]
        if route["health_path"] is not None:
            lines += ["    option httpchk", "    http-check send meth GET uri " + route["health_path"] + " ver HTTP/1.1 hdr Host " + host + " hdr Connection close",
                      "    http-check expect status " + str(route["health_status"])]
        for index, backend in enumerate(plan["backends"]):
            lines.append("    server node" + str(index) + " " + backend["address"] + ":" + str(port_by_host[host])
                + " check init-state fully-down check-ssl check-sni " + host + " verify required verifyhost " + host + " ca-file " + CA)
        lines.append("")
    for service in SSH_SERVICES:
        if service not in plan:
            continue
        ssh = plan[service]
        lines += ["frontend external_" + service, f"    bind :{ssh['port']}",
                  "    tcp-request connection reject unless { src -m ip " + " ".join(plan["client_cidrs"]) + " }",
                  "    default_backend " + service, "", "backend " + service, "    balance roundrobin",
                  "    option tcp-check", "    tcp-check expect rstring ^SSH-2[.]0-",
                  "    default-server inter 2s fall 2 rise 2 init-state fully-down"]
        for index, backend in enumerate(plan["backends"]):
            lines.append(f"    server node{index} {backend['address']}:{ssh['node_port']} check")
        lines.append("")
    lines += ["frontend local_health", "    mode http", "    no log", "    bind 127.0.0.1:" + str(plan["health_port"]),
              "    http-request return status 404 unless { path -m str /ready }", "    http-request set-var(txn.ready_routes) int(0)"]
    for route in plan["routes"]:
        lines.append("    http-request set-var(txn.ready_routes) var(txn.ready_routes),add(1) if { nbsrv(" + backend_name(route["hostname"]) + ") gt 0 }")
    ssh_ready = "".join(f" {{ nbsrv({service}) gt 0 }}" for service in SSH_SERVICES if service in plan)
    lines += ["    http-request return status 200 content-type text/plain string ready-" + edge_identity(plan) + " if { var(txn.ready_routes) -m int ge " + str(plan["minimum_ready_routes"]) + " }" + ssh_ready,
              "    http-request return status 503 content-type text/plain string unavailable", ""]
    return "\n".join(lines)


def keepalived_config(plan, host, index):
    return vrrp_config(plan, host, index, helper=BASE + "/gateway_edge_health.py", identity=edge_identity(plan))


def command(host, role, digest):
    return ["/usr/bin/python3", "-E", "-s", "-B", BASE + "/start_gateway_edge.py", "--contract-sha256", digest, "--host", host["name"], "--component", role, "--start"]


def kubernetes(plan, files, digest):
    namespace, app = plan["namespace"], plan["name"]
    config_name = app + "-" + digest[:12]
    labels = {OWNER: app}
    resources = [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": config_name, "namespace": namespace, "labels": labels},
                  "immutable": True, "data": {k: v for k, v in files.items() if k in PROGRAMS or k.endswith(".cfg") or k.endswith(".conf") or k == "edge-contract.json"}}]
    for host in plan["entry_hosts"]:
        selected = {**labels, "platform.gitops/edge-host": host["name"]}
        containers = []
        for role in ("haproxy", "keepalived"):
            container = {"name": role, "image": plan["images"][role], "command": command(host, role, digest),
                "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"cpu": "1", "memory": "256Mi"}},
                "securityContext": {"runAsUser": 1000 if role == "haproxy" else 0, "runAsGroup": 1000 if role == "haproxy" else 0,
                    "runAsNonRoot": role == "haproxy", "privileged": False, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"], "add": ["NET_BIND_SERVICE"] if role == "haproxy" else KEEPALIVED_CAPABILITIES}},
                "volumeMounts": [{"name": "program", "mountPath": BASE, "readOnly": True}, {"name": "trust", "mountPath": "/run/platform/gateway-edge", "readOnly": True},
                                 {"name": "tmp-" + role, "mountPath": "/tmp"}],
                "readinessProbe": {"exec": {"command": ["/usr/bin/python3", "-I", "-B", BASE + "/gateway_edge_health.py", "--port", str(plan["health_port"]), "--identity", edge_identity(plan)]}, "periodSeconds": 3, "timeoutSeconds": 2}}
            if role == "keepalived": container["volumeMounts"].append({"name": "run", "mountPath": "/run/platform-gateway-edge"})
            containers.append(container)
        pod = {"hostNetwork": True, "hostPID": False, "hostIPC": False, "dnsPolicy": "Default", "automountServiceAccountToken": False, "terminationGracePeriodSeconds": 15,
            "securityContext": {"fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
            "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchFields": [{"key": "metadata.name", "operator": "In", "values": [host["node_name"]]}]}]}}},
            "containers": containers, "volumes": [{"name": "program", "configMap": {"name": config_name, "defaultMode": 0o444}},
                {"name": "trust", "secret": {"secretName": plan["ca_secret"], "defaultMode": 0o440, "items": [{"key": "ca.crt", "path": "ca.crt"}]}},
                {"name": "run", "emptyDir": {"sizeLimit": "16Mi"}}, *[{"name": "tmp-" + role, "emptyDir": {"sizeLimit": "16Mi"}} for role in ("haproxy", "keepalived")]]}
        resources.append({"apiVersion": "apps/v1", "kind": "DaemonSet", "metadata": {"name": app + "-" + host["name"], "namespace": namespace, "labels": labels},
            "spec": {"selector": {"matchLabels": selected}, "updateStrategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}},
                     "template": {"metadata": {"labels": selected}, "spec": pod}}})
    resources.append({"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {"name": app, "namespace": namespace, "labels": labels},
                      "spec": {"minAvailable": 1, "selector": {"matchLabels": labels}}})
    return resources


def render(plan, gateway_contract, runtime):
    # A native/Compose external entry can front an existing Kubernetes Gateway.
    # Keep that original Gateway contract and target, rather than relabeling it.
    if (gateway_contract["runtime"] not in RUNTIMES
            or runtime in {"rke2", "k3s", "k8s"} and gateway_contract["runtime"] != runtime):
        raise ValueError("Gateway and external entry runtime labels differ")
    actual = hashlib.sha256((json.dumps(gateway_contract, sort_keys=True, indent=2) + "\n").encode()).hexdigest()
    if plan.get("gateway_contract_sha256") != actual:
        raise ValueError("External entry is not bound to this reviewed Gateway contract")
    gateway = gateway_contract["plan"]
    validate(plan, gateway, runtime)
    files = {name: read_bounded_text(ROOT / "scripts" / name, encoding="utf-8")
             for name in sorted(PROGRAMS - {"gateway_edge_health.py"})}
    files["gateway_edge_health.py"] = health_program()
    files["haproxy.cfg"] = haproxy_config(plan, gateway)
    if runtime == "raw": files["haproxy.cfg"] = files["haproxy.cfg"].replace(CA, native_ca(plan))
    for index, host in enumerate(plan["entry_hosts"]): files["keepalived-" + host["name"] + ".conf"] = keepalived_config(plan, host, index)
    contract = {"schema_version": 1, "runtime": runtime, "plan": plan, "gateway_plan": gateway,
                "files_sha256": {k: hashlib.sha256(v.encode()).hexdigest() for k, v in files.items()}}
    files["edge-contract.json"] = json.dumps(contract, sort_keys=True, indent=2) + "\n"
    digest = hashlib.sha256(files["edge-contract.json"].encode()).hexdigest()
    if runtime in {"rke2", "k3s", "k8s"}:
        files["edge-workloads.yaml"] = yaml.safe_dump_all(loads_strict_json(json.dumps(kubernetes(plan, files, digest))), sort_keys=False)
    elif runtime == "docker":
        for host in plan["entry_hosts"]:
            services = {}
            for role in ("haproxy", "keepalived"):
                services[role] = {"image": plan["images"][role], "entrypoint": [], "command": command(host, role, digest), "network_mode": "host", "read_only": True,
                    "labels": {OWNER: plan["name"], "platform.gitops/edge-host": host["name"], "platform.gitops/edge-contract": digest},
                    "user": "1000:1000" if role == "haproxy" else "0:0", "cap_drop": ["ALL"], "cap_add": ["NET_BIND_SERVICE"] if role == "haproxy" else KEEPALIVED_CAPABILITIES,
                    "security_opt": ["no-new-privileges:true"], "restart": "unless-stopped", "cpus": "1", "mem_limit": "256m", "pids_limit": 64,
                    "healthcheck": {"test": ["CMD", "/usr/bin/python3", "-I", "-B", BASE + "/gateway_edge_health.py", "--port", str(plan["health_port"]), "--identity", edge_identity(plan)], "interval": "3s", "timeout": "2s", "retries": 3},
                    "tmpfs": ["/tmp:rw,noexec,nosuid,size=16m"] + (["/run/platform-gateway-edge:rw,noexec,nosuid,size=16m,mode=0700"] if role == "keepalived" else []),
                    "volumes": [{"type": "bind", "source": "${PLATFORM_EDGE_BUNDLE:?Set the reviewed bundle directory}", "target": BASE, "read_only": True},
                                {"type": "bind", "source": "${PLATFORM_EDGE_INPUTS:?Set the private CA directory}", "target": "/run/platform/gateway-edge", "read_only": True}]}
            files["compose-" + host["name"] + ".yaml"] = yaml.safe_dump({"services": services}, sort_keys=False)
    else:
        for host in plan["entry_hosts"]:
            for role in ("haproxy", "keepalived"):
                capabilities = "CAP_NET_BIND_SERVICE" if role == "haproxy" else " ".join("CAP_" + c for c in KEEPALIVED_CAPABILITIES)
                files["edge-" + host["name"] + "-" + role + ".service"] = "\n".join(["[Unit]", "Description=Reviewed Gateway external entry " + role,
                    "After=network-online.target", "Wants=network-online.target", "", "[Service]", "Type=simple", "User=" + ("platform-edge" if role == "haproxy" else "root"),
                    "UMask=0077", "ExecStart=" + " ".join(command(host, role, digest)), "Restart=on-failure", "RestartSec=3",
                    "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "PrivateDevices=yes", "RestrictSUIDSGID=yes",
                    "CapabilityBoundingSet=" + capabilities, "AmbientCapabilities=" + capabilities,
                    "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK" + (" AF_PACKET" if role == "keepalived" else ""),
                    "RuntimeDirectory=platform-gateway-edge" if role == "keepalived" else "RuntimeDirectory=platform-edge-haproxy",
                    "RuntimeDirectoryMode=0700", "ReadWritePaths=/run/platform-gateway-edge" if role == "keepalived" else "ReadOnlyPaths=" + BASE,
                    "MemoryMax=256M", "TasksMax=64", "", "[Install]", "WantedBy=multi-user.target", ""])
    files["render-status.json"] = json.dumps({"component": "gateway-edge", "runtime": runtime, "contract_sha256": digest, "rendered": True,
        "deployed": False, "vip_failover_verified": False, "application_health_verified": False, "ha_verified": False, "full_stack_verified": False}, indent=2) + "\n"
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--gateway-bundle-dir", type=Path, required=True)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        gateway, _ = load_gateway(args.gateway_bundle_dir, plan["gateway_contract_sha256"])
        files = render(plan, gateway, args.runtime)
        write_bundle(files, args.output_dir, private_root=ROOT / "private")
        print(json.dumps({"rendered": True, "deployed": False, "ha_verified": False}))
        return 0
    except Exception:
        print("Gateway edge render stopped; inspect the private Gateway/entry plans and new output directory.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
