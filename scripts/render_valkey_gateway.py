#!/usr/bin/env python3
"""Render two optional TLS application gateways without weakening the cache profile."""

import argparse
import ast
import copy
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import sys

import yaml

from bounded_file import read_bounded_bytes, read_bounded_text
import render_valkey_ha as cache
from render_gerrit_router import IMAGE, RUNTIMES, health_program, write_bundle
from render_gerrit_sites import label
import verify_valkey_runtime as observer
from render_jenkins_host import host_path
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
UNVERIFIED = {k: False for k in ("deployed", "application_compatibility_verified", "failover_verified", "ha_verified", "full_stack_verified")}


def validate(plan, contract):
    fields = "schema_version name namespace image native_binary native_binary_sha256 private_dir tls_secret frontend tls_names private_sha256 instances node_selector failure_domain_key client_namespaces observer"
    cache.exact(plan, fields + " vrrp" if isinstance(plan, dict) and "vrrp" in plan else fields)
    if (type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or not label(plan["name"]) or not label(plan["namespace"])
            or not IMAGE.fullmatch(str(plan["image"])) or not cache.PATH.fullmatch(str(plan["native_binary"])) or not observer.sha(plan["native_binary_sha256"])
            or not cache.PATH.fullmatch(str(plan["private_dir"])) or not label(plan["tls_secret"])): raise ValueError("Pin the dedicated gateway and runtime artifacts")
    # These are Linux runtime paths, even when packaging on Windows. Preserve
    # them in the package; adapt only the validator's runner-local path check.
    observation = copy.deepcopy(plan["observer"])
    for kind in ("ca", "client_cert", "client_key"):
        path = observation["trust"][kind + "_file"]
        if not isinstance(path, str) or not cache.PATH.fullmatch(path): raise ValueError("Pin canonical absolute Linux TLS paths")
        if not Path(path).is_absolute(): observation["trust"][kind + "_file"] = str(Path.cwd() / "gateway-path-validation" / kind)
    observer.validate(observation, contract)
    frontend = plan["frontend"]
    cache.exact(frontend, "hostname port require_client_certificate" if isinstance(frontend, dict)
                and "require_client_certificate" in frontend else "hostname port")
    if type(frontend.get("require_client_certificate", False)) is not bool:
        raise ValueError("Select an explicit frontend client-certificate policy")
    if not cache.dns_name(plan["frontend"]["hostname"]) or not re.search(r"[A-Za-z]", plan["frontend"]["hostname"]) or plan["frontend"]["port"] != 6381: raise ValueError("Use a DNS identity and reviewed TLS gateway port 6381")
    if not isinstance(plan["tls_names"], dict) or set(plan["tls_names"]) != set(plan["observer"]["members"]): raise ValueError("Pin a server certificate DNS identity per backend")
    if any(not cache.dns_name(value) or not re.search(r"[A-Za-z]", value) for value in plan["tls_names"].values()): raise ValueError("Backend certificate DNS names are required")
    if len(set(plan["tls_names"].values())) != 3: raise ValueError("Keep backend TLS identities distinct")
    if (plan["namespace"] not in contract["plan"]["network"]["client_namespaces"] or not isinstance(plan["client_namespaces"], list)
            or not 1 <= len(plan["client_namespaces"]) <= 8 or not all(label(n) for n in plan["client_namespaces"])
            or len(set(plan["client_namespaces"])) != len(plan["client_namespaces"])): raise ValueError("Authorize the gateway namespace upstream and explicitly inventory application namespaces")
    cache.exact(plan["private_sha256"], "frontend.pem upstream.pem")
    if not all(observer.sha(value) for value in plan["private_sha256"].values()): raise ValueError("Pin the private TLS PEM file hashes")
    instances = plan["instances"]
    if not isinstance(instances, list) or len(instances) != 2: raise ValueError("Prepare two distinct gateway hosts")
    for instance in instances:
        cache.exact(instance, "name address")
        if not label(instance["name"]) or not isinstance(instance["address"], str): raise ValueError("Declare each gateway name and address")
        ip = ipaddress.IPv4Address(instance["address"])
        if str(ip) != instance["address"] or ip.is_unspecified or ip.is_multicast or ip.is_loopback: raise ValueError("Declare direct gateway IPv4 addresses")
    if any(len({i[k] for i in instances}) != 2 for k in ("name", "address")): raise ValueError("Gateway host identities must be distinct")
    if "vrrp" in plan:
        vrrp = plan["vrrp"]
        cache.exact(vrrp, "vip vrid interfaces binary binary_sha256")
        if not host_path(plan["private_dir"]) or not host_path(vrrp["binary"]) or not observer.sha(vrrp["binary_sha256"]):
            raise ValueError("Pin canonical native Keepalived and gateway paths")
        vip = ipaddress.IPv4Interface(vrrp["vip"])
        addresses = {ipaddress.IPv4Address(instance["address"]) for instance in instances}
        cache_addresses = {ipaddress.IPv4Address(m["connect_address"]) for m in plan["observer"]["members"].values()}
        if (str(vip) != vrrp["vip"] or vip.ip in addresses | cache_addresses
                or vip.ip.is_loopback or vip.ip.is_link_local or vip.ip.is_multicast or vip.ip.is_reserved
                or not 16 <= vip.network.prefixlen <= 29 or vip.ip in {vip.network.network_address, vip.network.broadcast_address}
                or not all(addr in vip.network and addr not in {vip.network.network_address, vip.network.broadcast_address}
                           for addr in addresses)):
            raise ValueError("Select a distinct canonical VIP on the gateway peers' shared subnet")
        interfaces = vrrp["interfaces"]
        if (type(vrrp["vrid"]) is not int or not 1 <= vrrp["vrid"] <= 255
                or not isinstance(interfaces, dict) or set(interfaces) != {i["name"] for i in instances}
                or not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", value)
                           and value != "lo" for value in interfaces.values())):
            raise ValueError("Select a VRRP ID and one physical-facing interface per gateway peer")
    if (not isinstance(plan["node_selector"], dict) or plan["node_selector"].get("kubernetes.io/os") != "linux" or len(plan["node_selector"]) > 8
            or any(not isinstance(k, str) or not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9./_-]+", k) or not label(v) for k, v in plan["node_selector"].items())
            or not isinstance(plan["failure_domain_key"], str) or not re.fullmatch(r"[a-z0-9.-]+/[A-Za-z0-9_.-]+", plan["failure_domain_key"])): raise ValueError("Select Linux placement and a declared failure domain")
    return plan


def programs():
    root = ROOT / "scripts"; found, pending = {}, ["valkey_gateway_health"]
    while pending:
        name = pending.pop()
        if name in found: continue
        raw = read_bounded_text(root / (name + ".py"), max_bytes=2 * 1024 ** 2); found[name] = raw
        for node in ast.walk(ast.parse(raw)):
            modules = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
            for module in modules:
                candidate = module.split(".")[0]
                if (root / (candidate + ".py")).is_file() and candidate not in found: pending.append(candidate)
    return {name + ".py": value for name, value in sorted(found.items())}


def proxy(plan, address, directory):
    frontend_tls = " ca-file " + directory + "/ca.pem verify required" if plan["frontend"].get("require_client_certificate") else ""
    rows = ["global", "  maxconn 1024", "  ssl-default-bind-options ssl-min-ver TLSv1.2", "  ssl-default-server-options ssl-min-ver TLSv1.2",
        "defaults", "  mode tcp", "  timeout connect 3s", "  timeout client 60s", "  timeout server 60s", "  timeout check 7s",
        "frontend application_cache", "  bind " + address + ":6381 ssl crt " + directory + "/frontend.pem" + frontend_tls, "  default_backend serving_primary",
        "backend serving_primary", "  option httpchk GET /ready", "  http-check expect status 200",
        "  default-server inter 1s fall 1 rise 1 init-state fully-down on-marked-down shutdown-sessions"]
    if "vrrp" in plan:
        vip = str(ipaddress.IPv4Interface(plan["vrrp"]["vip"]).ip)
        rows.insert(rows.index("  default_backend serving_primary"), "  bind " + vip + ":6381 ssl crt " + directory + "/frontend.pem" + frontend_tls)
    for index, (name, member) in enumerate(sorted(plan["observer"]["members"].items())):
        tls_name = plan["tls_names"][name]
        rows.append("  server " + name + " " + member["connect_address"] + ":6380 ssl verify required ca-file " + directory + "/ca.pem crt " + directory +
            "/upstream.pem sni str(" + tls_name + ") verifyhost " + tls_name + " check addr 127.0.0.1 port " + str(19091 + index) + " no-check-ssl")
    rows += ["listen local_readiness", "  bind 127.0.0.1:19090", "  mode http"]
    if "vrrp" in plan:
        rows += ["  http-request return status 200 content-type text/plain string ready-" + observer.fingerprint(plan)
                 + " if { path /ready } { nbsrv(serving_primary) eq 1 }", "  http-request return status 503"]
    else:
        rows += ["  monitor-uri /ready", "  monitor fail if { nbsrv(serving_primary) lt 1 }"]
    return "\n".join(rows) + "\n"


def keepalived_config(plan, index):
    vrrp = plan["vrrp"]
    host, peer = plan["instances"][index], plan["instances"][1 - index]
    interface = vrrp["interfaces"][host["name"]]
    return "\n".join([
        "global_defs {", "  router_id " + host["name"], "  enable_script_security", "  script_user root", "}",
        "vrrp_script valkey_ready {",
        '  script "/usr/bin/python3 -I -B ' + plan["private_dir"] + '/gateway_edge_health.py --port 19090 --identity '
        + observer.fingerprint(plan) + '"',
        "  interval 2", "  timeout 2", "  fall 2", "  rise 2", "  weight 0", "  init_fail", "}",
        "vrrp_instance VALKEY {", "  state BACKUP", "  version 3", "  interface " + interface,
        "  virtual_router_id " + str(vrrp["vrid"]), "  priority " + str(150 if index == 0 else 100),
        "  advert_int 1", "  nopreempt", "  unicast_src_ip " + host["address"], "  check_unicast_src",
        "  unicast_peer {", "    " + peer["address"] + " min_ttl 255 max_ttl 255", "  }",
        "  virtual_ipaddress {", "    " + vrrp["vip"] + " dev " + interface, "  }",
        "  track_script {", "    valkey_ready", "  }", "}", "",
    ])


def render(plan, contract, runtime):
    validate(plan, contract)
    if runtime not in RUNTIMES: raise ValueError("Unknown gateway hosting runtime")
    if "vrrp" in plan and runtime not in {"raw", "docker"}:
        raise ValueError("Use a native VRRP service with the raw or host-network Docker gateway")
    directory = plan["private_dir"] if runtime == "raw" else "/etc/platform-valkey-gateway"
    trust = plan["observer"]["trust"]
    expected_trust = {"ca": "ca.pem", "client_cert": "client_cert.pem", "client_key": "client_key.pem"}
    if any(trust[k + "_file"] != directory + "/" + filename for k, filename in expected_trust.items()): raise ValueError("Review runtime-local observer TLS paths before rendering")
    files = programs()
    if "vrrp" in plan:
        files["gateway_edge_health.py"] = health_program()
        vrrp = plan["vrrp"]
        files["keepalived-binary.sha256"] = vrrp["binary_sha256"] + "  " + vrrp["binary"] + "\n"
        for index, host in enumerate(plan["instances"]):
            name = host["name"]
            config = plan["private_dir"] + "/" + name + "-keepalived.conf"
            files[name + "-keepalived.conf"] = keepalived_config(plan, index)
            files[name + "-keepalived.service"] = "\n".join([
                "[Unit]", "Description=Valkey gateway VIP for " + name, "After=network-online.target", "Wants=network-online.target", "",
                "[Service]", "Type=simple", "RuntimeDirectory=platform-valkey-vrrp-" + name, "RuntimeDirectoryMode=0700",
                "ExecStartPre=/usr/bin/sha256sum --check --strict " + plan["private_dir"] + "/keepalived-binary.sha256",
                "ExecStart=" + vrrp["binary"] + " --dont-fork --log-console --vrrp --use-file " + config
                + " --pid=%t/platform-valkey-vrrp-" + name + "/main.pid --vrrp_pid=%t/platform-valkey-vrrp-" + name + "/vrrp.pid",
                "Restart=on-failure", "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes",
                "CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW CAP_SETGID CAP_SETUID",
                "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK AF_PACKET", "UMask=0077", "",
            ])
    specification = {"contract": contract, "observer": plan["observer"], "health_ports": {name: 19091 + i for i, name in enumerate(sorted(plan["observer"]["members"]))}}
    files["gateway-checks.json"] = observer.canonical(specification) + "\n"
    files["private.sha256"] = "".join(digest + "  " + directory + "/" + filename + "\n" for filename, digest in sorted({
        **plan["private_sha256"], **{filename: trust[k + "_sha256"] for k, filename in expected_trust.items()}}.items()))
    private_names = ("frontend.pem", "upstream.pem", *expected_trust.values())
    if runtime in {"rke2", "k3s", "k8s"}:
        files["haproxy.cfg"] = proxy(plan, "0.0.0.0", directory)
        assets = {**files}; asset_digest = hashlib.sha256(observer.canonical(assets).encode()).hexdigest()[:12]
        cm = plan["name"] + "-" + asset_digest; path = "/opt/platform-valkey-gateway"
        labels = {"app.kubernetes.io/name": "valkey-application-gateway", "app.kubernetes.io/instance": plan["name"]}
        containers = []
        for role, args in (("health", ["python3", "-E", "-s", "-B", path + "/valkey_gateway_health.py", "--specification", path + "/gateway-checks.json"]),
                ("proxy", ["haproxy", "-W", "-db", "-f", path + "/haproxy.cfg"])):
            containers.append({"name": role, "image": plan["image"], "command": ["/bin/sh", "-ec"],
                "args": ["sha256sum --check --strict " + path + "/private.sha256 >/dev/null; exec \"$@\"", "gateway", *args],
                "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "2", "memory": "256Mi"}},
                "volumeMounts": [{"name": "code", "mountPath": path, "readOnly": True},
                    *[{"name": "private", "mountPath": directory + "/" + filename, "subPath": filename, "readOnly": True} for filename in private_names],
                    {"name": "tmp", "mountPath": "/tmp"}],
                **({"env": [{"name": plan["observer"]["credentials"][role + "_env"], "valueFrom": {"secretKeyRef": {"name": plan["tls_secret"], "key": role + "-password"}}} for role in ("probe", "discovery")]} if role == "health" else {})})
        containers[1].update(ports=[{"name": "rediss", "containerPort": 6381}], readinessProbe={"exec": {"command": ["python3", "-E", "-s", "-B", "-c",
            "import http.client,sys; c=http.client.HTTPConnection('127.0.0.1',19090,timeout=1); c.request('GET','/ready'); sys.exit(0 if c.getresponse().status==200 else 1)"]}, "periodSeconds": 5, "timeoutSeconds": 2})
        objects = [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": cm, "namespace": plan["namespace"]}, "immutable": True, "data": assets},
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": plan["name"], "namespace": plan["namespace"]}, "spec": {"replicas": 2,
                "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}},
                "selector": {"matchLabels": labels}, "template": {"metadata": {"labels": labels}, "spec": {"automountServiceAccountToken": False,
                    "securityContext": {"runAsUser": 1000, "runAsGroup": 1000, "runAsNonRoot": True, "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
                    "nodeSelector": plan["node_selector"], "affinity": {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [
                        {"labelSelector": {"matchLabels": labels}, "topologyKey": plan["failure_domain_key"]}]}}, "containers": containers,
                    "volumes": [{"name": "code", "configMap": {"name": cm}}, {"name": "private", "secret": {"secretName": plan["tls_secret"], "defaultMode": 0o440}}, {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}]}}}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": plan["name"], "namespace": plan["namespace"]}, "spec": {"selector": labels, "ports": [{"name": "rediss", "port": 6381, "targetPort": "rediss"}]}},
            {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {"name": plan["name"], "namespace": plan["namespace"]}, "spec": {"minAvailable": 1, "selector": {"matchLabels": labels}}}]
        objects.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": plan["name"], "namespace": plan["namespace"]},
            "spec": {"podSelector": {"matchLabels": labels}, "policyTypes": ["Ingress", "Egress"],
                "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": n}}} for n in plan["client_namespaces"]], "ports": [{"protocol": "TCP", "port": 6381}]}],
                "egress": [{"to": [{"ipBlock": {"cidr": m["connect_address"] + "/32"}} for m in plan["observer"]["members"].values()], "ports": [{"protocol": "TCP", "port": p} for p in (6380, 26380)]}]}})
        if sum(len(v.encode()) for v in assets.values()) > 700 * 1024: raise ValueError("Bound the immutable gateway code/config payload")
        files["gateway.yaml"] = yaml.safe_dump_all(objects, sort_keys=False)
    else:
        if runtime == "raw": files["native-binary.sha256"] = plan["native_binary_sha256"] + "  " + plan["native_binary"] + "\n"
        for instance in plan["instances"]:
            name = instance["name"]; files[name + ".cfg"] = proxy(plan, instance["address"], directory)
            if runtime == "docker":
                files[name + "-compose.yaml"] = yaml.safe_dump({"services": {role: {"image": plan["image"], "user": "1000:1000", "network_mode": "host", "read_only": True,
                    "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "restart": "unless-stopped", "mem_limit": "256m", "cpus": 2,
                    "entrypoint": ["/bin/sh", "-ec"], "command": ["sha256sum --check --strict /code/private.sha256 >/dev/null; exec \"$@\"", "gateway", *args],
                    "volumes": [{"type": "bind", "source": source, "target": target, "read_only": True, "bind": {"create_host_path": False}} for source, target in ((".", "/code"), (plan["private_dir"], directory))],
                    **({"env_file": [{"path": plan["private_dir"] + "/observer.env", "required": True}]} if role == "health" else {}), "tmpfs": ["/tmp:rw,nosuid,nodev,noexec,size=64m"]}
                    for role, args in (("health", ["python3", "-E", "-s", "-B", "/code/valkey_gateway_health.py", "--specification", "/code/gateway-checks.json"]),
                        ("proxy", ["haproxy", "-W", "-db", "-f", "/code/" + name + ".cfg"]))}}, sort_keys=False)
            else:
                for role, args in (("health", "/usr/bin/python3 -E -s -B " + directory + "/valkey_gateway_health.py --specification " + directory + "/gateway-checks.json"),
                        ("proxy", plan["native_binary"] + " -W -db -f " + directory + "/" + name + ".cfg")):
                    files[name + "-" + role + ".service"] = "[Unit]\nDescription=TLS Valkey application gateway " + role + "\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nUser=valkey-gateway\nGroup=valkey-gateway\nWorkingDirectory=" + directory + "\nExecStartPre=/usr/bin/sha256sum --check --strict " + directory + "/private.sha256\n" + ("ExecStartPre=/usr/bin/sha256sum --check --strict " + directory + "/native-binary.sha256\n" if role == "proxy" else "") + ("EnvironmentFile=" + directory + "/observer.env\n" if role == "health" else "") + "ExecStart=" + args + "\nRestart=on-failure\nNoNewPrivileges=yes\nProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\nPrivateDevices=yes\nCapabilityBoundingSet=\nMemoryMax=256M\nTasksMax=64\nUMask=0077\n"
    files["render-status.json"] = json.dumps({"component": "valkey-tls-application-gateway", "runtime": runtime,
        "upstream_mtls_preserved": True, "sentinel_authentication_preserved": True, **UNVERIFIED}, indent=2) + "\n"
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--cache-bundle-dir", type=Path, required=True); parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True); parser.add_argument("--output-dir", type=Path, required=True); args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=256 * 1024)); contract = observer.package(args.cache_bundle_dir, plan["observer"]["contract_sha256"])
        files = render(plan, contract, args.runtime); write_bundle(files, args.output_dir, private_root=ROOT / "private")
        print(observer.canonical({"rendered": True, **UNVERIFIED})); return 0
    except Exception:
        print("Valkey gateway preparation stopped; private inputs suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
