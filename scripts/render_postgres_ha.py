#!/usr/bin/env python3
"""Render an opt-in PostgreSQL durability policy; never installs or promotes a database."""

import argparse
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import sys

import yaml

from bounded_file import read_bounded_bytes, read_bounded_text
from cnpg_release import cluster_schema
from materialize_patroni import APPLICATION_PROGRAM, PROGRAMS, STARTUP_PROGRAMS
import postgres_ha_policy as policy
from render_gerrit_router import write_bundle
from strict_json import loads_strict_json
from strict_yaml import loads_strict_yaml_all


ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=2)
def cnpg_schema_text(version="1.30.0", release=None):
    if version != "1.30.0" or release is not None:
        return cluster_schema(version, release)
    path = ROOT / "gitops/clusters/rke2-main/premium-3node/apps/cloudnativepg/charts/cloudnative-pg/templates/crds/crds.yaml"
    lines = read_bounded_text(path, encoding="utf-8", max_bytes=4 * 1024 ** 2).splitlines()
    if lines[0] != "{{- if .Values.crds.create }}" or lines[-1] != "{{- end }}": raise ValueError("Review the changed CNPG schema wrapper")
    definitions = loads_strict_yaml_all("\n".join(lines[1:-1]))
    cluster = next(obj for obj in definitions if obj.get("metadata", {}).get("name") == "clusters.postgresql.cnpg.io")
    schema = next(item["schema"]["openAPIV3Schema"] for item in cluster["spec"]["versions"] if item["name"] == "v1" and item["served"])
    return json.dumps(schema, sort_keys=True, separators=(",", ":")) + "\n"


def host_member(plan, runtime, member, digest):
    name, state = member["name"], member["state_path"]
    runtime_name = "platform-postgres-" + name
    runtime_dir = "/run/" + runtime_name
    bundle = "/opt/platform/postgres-bundle"
    inputs = "/etc/platform-postgres/inputs"
    uid, gid = plan["native"]["uid"], plan["native"]["gid"]
    limits = plan["resources"]["limits"]
    cpu = int(limits["cpu"].rstrip("m")) * (1 if limits["cpu"].endswith("m") else 1000)
    command = ["/opt/platform/patroni/bin/python3", "-E", "-s", "-B", bundle + "/start_patroni.py",
               "--contract-sha256", digest, "--member", name, "--execute"]
    if runtime == "raw":
        return """[Unit]
Description=Platform PostgreSQL member {name}
Wants=network-online.target
After=network-online.target
RequiresMountsFor={state}

[Service]
Type=exec
User={uid}
Group={gid}
RuntimeDirectory={runtime_name}
RuntimeDirectoryMode=0700
ExecStart={command}
NoNewPrivileges=yes
CapabilityBoundingSet=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths={state}
DevicePolicy=closed
DeviceAllow=/dev/watchdog rw
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
UMask=0077
KillMode=mixed
TimeoutStopSec=120
CPUQuota={quota}%
MemoryMax={memory}

[Install]
WantedBy=multi-user.target
""".format(name=name, state=state, uid=uid, gid=gid, runtime_name=runtime_name, command=" ".join(command),
           quota=str(cpu / 10).removesuffix(".0"), memory=limits["memory"].replace("i", ""))

    binds = [{"type": "bind", "source": path, "target": path, "read_only": readonly, "bind": {"create_host_path": False}}
             for path, readonly in ((bundle, True), (inputs, True), (state, False))]
    service = {"image": plan["images"]["patroni"], "pull_policy": "never", "user": f"{uid}:{gid}",
               "entrypoint": command, "network_mode": "host", "volumes": binds, "read_only": True,
               "tmpfs": [f"{runtime_dir}:rw,nosuid,nodev,noexec,mode=0700,uid={uid},gid={gid}"],
               "devices": ["/dev/watchdog:/dev/watchdog:rw"], "cap_drop": ["ALL"],
               "security_opt": ["no-new-privileges:true"], "stop_signal": "SIGTERM", "stop_grace_period": "120s",
               "cpus": str(cpu / 1000), "mem_limit": limits["memory"].replace("i", "")}
    return yaml.safe_dump({"services": {"patroni-" + name: service}}, sort_keys=False)


def host_router(plan, runtime, router):
    name = f"postgres-write-{router['name']}"
    config = f"{policy.WRITE_ROUTER_DIR}/haproxy.cfg"
    if runtime == "raw":
        return name + ".service", f"""[Unit]
Description=PostgreSQL write endpoint ({router['name']})
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=haproxy
Group=haproxy
ExecStartPre=/usr/sbin/haproxy -c -f {config}
ExecStart=/usr/sbin/haproxy -W -db -f {config}
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
UMask=0077

[Install]
WantedBy=multi-user.target
"""
    service = {
        "image": plan["write_endpoint"]["haproxy_image"],
        "user": "65532:65532",
        "entrypoint": ["haproxy", "-W", "-db", "-f", config],
        "network_mode": "host",
        "pull_policy": "never",
        "restart": "unless-stopped",
        "read_only": True,
        "volumes": [{"type": "bind", "source": policy.WRITE_ROUTER_DIR, "target": policy.WRITE_ROUTER_DIR,
                     "read_only": True, "bind": {"create_host_path": False}}],
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "mem_limit": "256m",
    }
    return f"compose.{name}.yaml", yaml.safe_dump({"services": {name: service}}, sort_keys=False)


def render(plan, runtime, *, cnpg_release=None):
    policy.validate(plan, runtime)
    # Regeneration after reading the canonical contract must preserve YAML
    # bytes even when caller dictionaries used a different insertion order.
    plan = loads_strict_json(json.dumps(plan, sort_keys=True))
    if yaml.__version__ != "6.0.3": raise ValueError("Use the reviewed PyYAML 6.0.3 renderer")
    host_runtime = runtime in {"raw", "docker"}
    if host_runtime and cnpg_release is not None:
        raise ValueError("A CNPG release manifest applies only to Kubernetes database packages")
    programs = PROGRAMS + (STARTUP_PROGRAMS + (APPLICATION_PROGRAM,) if host_runtime else ())
    files = {name: read_bounded_text(ROOT / "scripts" / name, encoding="utf-8", max_bytes=2 * 1024 ** 2) for name in programs}
    if runtime in {"rke2", "k3s", "k8s"}:
        files["postgres-resources.yaml"] = yaml.safe_dump_all([policy.cnpg(plan)], sort_keys=False)
        files["cnpg-cluster-schema.json"] = cnpg_schema_text(plan["cnpg_version"], cnpg_release)
    else:
        for member in plan["members"]:
            files["patroni-" + member["name"] + ".template.yaml"] = yaml.safe_dump(policy.patroni(plan, member), sort_keys=False)
    routers = plan.get("write_endpoint", {}).get("routers", [])
    for router in routers:
        files[f"postgres-write-{router['name']}.cfg"] = policy.write_router_config(plan, router)
    contract = {"schema_version": 1, "runtime": runtime, "plan": plan,
                "files_sha256": {name: hashlib.sha256(value.encode()).hexdigest() for name, value in files.items()}}
    files["postgres-contract.json"] = json.dumps(contract, sort_keys=True, indent=2) + "\n"
    digest = hashlib.sha256(files["postgres-contract.json"].encode()).hexdigest()
    if host_runtime:
        for member in plan["members"]:
            name = "patroni-" + member["name"]
            filename = name + ".service" if runtime == "raw" else "compose." + name + ".yaml"
            files[filename] = host_member(plan, runtime, member, digest)
        for router in routers:
            filename, content = host_router(plan, runtime, router)
            files[filename] = content
    files["render-status.json"] = json.dumps({"component": "postgresql-ha", "runtime": runtime,
        "contract_sha256": digest, "rendered": True,
        "deployed": False, "synchronous_replication_verified": False, "promotion_verified": False, "restore_verified": False,
        "ha_verified": False, "full_stack_verified": False,
        "remaining": ["artifact-and-operator-provenance", "guarded-runtime-installation", "live-node-tls-and-volume-identity", "application-role-integration",
                      "synchronous-write-and-quorum-loss-tests", "fenced-promotion-and-client-reconnect-tests", "off-platform-backup-and-restore", "upgrade-and-recovery"]}, indent=2) + "\n"
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path); parser.add_argument("--runtime", choices=policy.RUNTIMES, required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--cnpg-release", type=Path, help="Exact reviewed release manifest for an optional CNPG patch lane")
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, max_bytes=128 * 1024))
        release = read_bounded_bytes(args.cnpg_release, max_bytes=2 * 1024 ** 2) if args.cnpg_release else None
        write_bundle(render(plan, args.runtime, cnpg_release=release), args.output_dir, private_root=ROOT / "private")
        print(json.dumps({"rendered": True, "deployed": False, "ha_verified": False, "full_stack_verified": False})); return 0
    except Exception:
        print("PostgreSQL policy rendering refused; private inputs were not printed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
