#!/usr/bin/env python3
"""Package initialized Gerrit primary sites with pinned, startup-checked artifacts.

No downloads, initialization, migrations, deployments or secret reads are made.
The two sites and shared repository filesystem must be provisioned separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

import yaml
import gerrit_storage as storage_guard
import gerrit_artifacts as artifact_guard

from bounded_file import read_bounded_text
from render_gerrit_router import IMAGE, RUNTIMES, dns_name, write_bundle
from standalone_python import standalone_script
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ("bin/gerrit.war", "plugins/high-availability.jar",
             "plugins/healthcheck.jar", "lib/global-refdb.jar")
SITE_LOCKS = ("etc/gerrit.config", "etc/high-availability.config")
SHA256 = re.compile(r"[0-9a-f]{64}")
LINUX_PATH = re.compile(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+")


class SiteError(ValueError):
    pass


def label(value: object) -> bool:
    return dns_name(value) and "." not in value and len(value) <= 63


def hashes(value: object, keys: tuple[str, ...]) -> bool:
    return (isinstance(value, dict) and set(value) == set(keys)
            and all(isinstance(digest, str) and SHA256.fullmatch(digest) for digest in value.values()))


def site_locks(plan: dict) -> tuple[str, ...]:
    return (*SITE_LOCKS, "etc/healthcheck.config") if plan["schema_version"] == 2 else SITE_LOCKS


def validate(plan: object) -> dict:
    fields = {"schema_version", "gerrit_version", "java_image", "namespace", "shared_claim",
              "shared_host_path", "ssh_public_key_sha256", "artifacts", "sites"}
    if not isinstance(plan, dict) or set(plan) - {"shared_storage"} != fields:
        raise SiteError("Site plan has missing or unsupported fields")
    if type(plan["schema_version"]) is not int or plan["schema_version"] not in (1, 2):
        raise SiteError("Unsupported site plan schema")
    if plan["schema_version"] == 2 and "shared_storage" not in plan:
        raise SiteError("Version 2 requires the reviewed shared CephFS scope")
    if "shared_storage" in plan:
        try: storage_guard.validate(plan["shared_storage"])
        except (ValueError, TypeError, AttributeError): raise SiteError("Pin the shared CephFS identity and subtree") from None
    if not isinstance(plan["gerrit_version"], str) or not re.fullmatch(r"[1-9][0-9]*\.[0-9]+\.[0-9]+", plan["gerrit_version"]):
        raise SiteError("An exact stable Gerrit release is required")
    if not isinstance(plan["java_image"], str) or not IMAGE.fullmatch(plan["java_image"]):
        raise SiteError("A digest-pinned Java runtime image is required")
    if not label(plan["namespace"]) or not label(plan["shared_claim"]):
        raise SiteError("Namespace and shared claim must be DNS labels")
    if not isinstance(plan["shared_host_path"], str) or not LINUX_PATH.fullmatch(plan["shared_host_path"]):
        raise SiteError("An explicit absolute Linux shared mount path is required")
    if not isinstance(plan["ssh_public_key_sha256"], str) or not SHA256.fullmatch(plan["ssh_public_key_sha256"]):
        raise SiteError("The shared SSH public-key file digest is required")
    if not hashes(plan["artifacts"], ARTIFACTS):
        raise SiteError("Pin the Gerrit WAR, HA, healthcheck and global-refdb artifacts")
    sites = plan["sites"]
    if not isinstance(sites, list) or len(sites) != 2:
        raise SiteError("Exactly two initialized primary sites are required")
    for site in sites:
        required = {"name", "claim", "host_path", "config_hashes"}
        if not isinstance(site, dict) or not required <= set(site) or set(site) - required - {"claim_subpath"}:
            raise SiteError("Invalid primary site fields")
        if "claim_subpath" in site and site["claim_subpath"] != "site":
            raise SiteError("The optional site claim subpath must be the literal site directory")
        if not label(site["name"]) or len(site["name"]) > 48 or not label(site["claim"]):
            raise SiteError("Site name and claim must be bounded DNS labels")
        if not isinstance(site["host_path"], str) or not LINUX_PATH.fullmatch(site["host_path"]):
            raise SiteError("Explicit absolute Linux primary site paths are required")
        if not hashes(site["config_hashes"], site_locks(plan)):
            raise SiteError("Pin each primary's separately reviewed Gerrit and HA configuration")
    for field in ("name", "claim", "host_path"):
        if sites[0][field] == sites[1][field]:
            raise SiteError("Primary names, site claims and host paths must be distinct")
    if plan["shared_claim"] in {site["claim"] for site in sites}:
        raise SiteError("Shared Git storage cannot be a primary's local site claim")
    paths = [Path(plan["shared_host_path"]), *(Path(site["host_path"]) for site in sites)]
    # Use lexical POSIX paths even when the renderer runs on Windows.
    strings = [p.as_posix().rstrip("/") for p in paths]
    for index, one in enumerate(strings):
        if any(one == two or one.startswith(two + "/") or two.startswith(one + "/")
               for two in strings[index + 1:]):
            raise SiteError("Local site directories and shared storage must not overlap")
    return plan


def lock_text(plan: dict, site: dict) -> str:
    locks = {**plan["artifacts"], **site["config_hashes"],
             "etc/ssh_host_ed25519_key.pub": plan["ssh_public_key_sha256"]}
    return "".join(f"{digest}  {name}\n" for name, digest in sorted(locks.items()))


def startup_script(guarded: bool = False, health_guard: bool = False, gerrit_version: str | None = None) -> str:
    script = """#!/bin/sh
set -eu
site_dir=$1
shared_dir=$2
lock_file=$3
cd "$site_dir"
# Fail closed; never use --init or an image's demo/default entrypoint.
sha256sum --check --strict "$lock_file"
test -s etc/secure.config
test -s etc/keystore
test -s etc/ssh_host_ed25519_key
test -d "$shared_dir/git/All-Projects.git"
test -d "$shared_dir/git/All-Users.git"
exec java -Xms512m -Xmx2g -jar bin/gerrit.war daemon --console-log -d "$site_dir"
"""
    if guarded:
        script = script.replace('test -d "$shared_dir/git/All-Projects.git"', '''guard_program=$4
guard_scope=$5
guard_lock=$6
sha256sum --check --strict "$guard_lock"
python3 -I -B "$guard_program" --scope "$guard_scope" --shared-dir "$shared_dir" --site-dir "$site_dir" >/dev/null
test -d "$shared_dir/git/All-Projects.git"''')
    if health_guard:
        if not guarded: raise SiteError("Health configuration requires the guarded startup profile")
        artifact_guard.version(gerrit_version)
        script = script.replace('--site-dir "$site_dir" >/dev/null', '--site-dir "$site_dir" --require-healthcheck --gerrit-version ' + gerrit_version + ' >/dev/null')
    return script


def guard_files(plan: dict, script_dir: str) -> dict[str, str]:
    if "shared_storage" not in plan: return {}
    files = {"check_storage.py": standalone_script(Path(storage_guard.__file__), "strict_json"),
             "bounded_subprocess.py": storage_guard.read(Path(__file__).with_name("bounded_subprocess.py"), 128 * 1024).decode(),
             "shared-storage.json": json.dumps(plan["shared_storage"], sort_keys=True, separators=(",", ":")) + "\n"}
    if plan.get("schema_version") == 2:
        files["gerrit_artifacts.py"] = storage_guard.read(Path(artifact_guard.__file__), 128 * 1024).decode()
    files["shared-storage.sha256"] = "".join(hashlib.sha256(value.encode()).hexdigest() + "  " + script_dir + "/" + name + "\n" for name, value in sorted(files.items()))
    return files


def guard_arguments(plan: dict, script_dir: str) -> list[str]:
    return [script_dir + "/" + name for name in ("check_storage.py", "shared-storage.json", "shared-storage.sha256")] if "shared_storage" in plan else []


def deployment(plan: dict, site: dict) -> list[dict]:
    name = site["name"]
    labels = {"app.kubernetes.io/name": "gerrit-primary", "app.kubernetes.io/instance": name}
    meta = {"name": name, "namespace": plan["namespace"], "labels": labels}
    script_dir = "/opt/gerrit-startup"
    extra = guard_files(plan, script_dir); script = startup_script("shared_storage" in plan, plan["schema_version"] == 2, plan["gerrit_version"])
    startup_name = name + "-" + hashlib.sha256((script + lock_text(plan, site) + json.dumps(extra, sort_keys=True)).encode()).hexdigest()[:12]
    return [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {**meta, "name": startup_name},
         "immutable": True, "data": {"start.sh": script, "artifacts.sha256": lock_text(plan, site), **extra}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta, "spec": {
            "replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": labels},
            "template": {"metadata": {"labels": labels}, "spec": {
                "automountServiceAccountToken": False,
                "securityContext": {"runAsUser": 1000, "runAsGroup": 1000, "runAsNonRoot": True,
                                    "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
                "affinity": {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [
                    {"labelSelector": {"matchLabels": {"app.kubernetes.io/name": "gerrit-primary"}},
                     "topologyKey": "kubernetes.io/hostname"}]}},
                "containers": [{"name": "gerrit", "image": plan["java_image"],
                    "command": ["/bin/sh", f"{script_dir}/start.sh"],
                    "args": ["/var/gerrit", "/shared", f"{script_dir}/artifacts.sha256", *guard_arguments(plan, script_dir)],
                    "ports": [{"name": "https", "containerPort": 8443}, {"name": "ssh", "containerPort": 29418}],
                    "resources": {"requests": {"cpu": "1", "memory": "2Gi"}, "limits": {"cpu": "4", "memory": "3Gi"}},
                    "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                                        "capabilities": {"drop": ["ALL"]}},
                    "startupProbe": {"tcpSocket": {"port": "https"}, "periodSeconds": 10, "failureThreshold": 60},
                    "readinessProbe": {"tcpSocket": {"port": "https"}, "periodSeconds": 5},
                    "volumeMounts": [{"name": "site", "mountPath": "/var/gerrit",
                                      **({"subPath": site["claim_subpath"]} if "claim_subpath" in site else {})},
                                     {"name": "shared", "mountPath": "/shared"},
                                     {"name": "startup", "mountPath": script_dir, "readOnly": True},
                                     {"name": "tmp", "mountPath": "/tmp"}]}],
                "volumes": [{"name": "site", "persistentVolumeClaim": {"claimName": site["claim"]}},
                            {"name": "shared", "persistentVolumeClaim": {"claimName": plan["shared_claim"]}},
                            {"name": "startup", "configMap": {"name": startup_name}}, {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}],
            }}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": meta, "spec": {
            "type": "ClusterIP", "selector": labels, "ports": [
                {"name": "https", "port": 8443, "targetPort": "https"},
                {"name": "ssh", "port": 29418, "targetPort": "ssh"}]}},
    ]


def render(plan: dict, runtime: str) -> dict[str, str]:
    plan = validate(plan)
    if runtime not in RUNTIMES:
        raise SiteError("Unsupported Gerrit site runtime")
    files = {"start.sh": startup_script("shared_storage" in plan, plan["schema_version"] == 2, plan["gerrit_version"]), **guard_files(plan, "/etc/gerrit" if runtime == "raw" else "/opt/gerrit-startup")}
    for site in plan["sites"]:
        name = site["name"]
        files[f"{name}.sha256"] = lock_text(plan, site)
        if runtime in {"rke2", "k3s", "k8s"}:
            files[f"{name}.yaml"] = yaml.safe_dump_all(deployment(plan, site), sort_keys=False)
        elif runtime == "docker":
            extra_mounts = [("./" + filename, "/opt/gerrit-startup/" + filename, True) for filename in guard_files(plan, "/opt/gerrit-startup")]
            mounts = [{"type": "bind", "source": source, "target": target, "read_only": read_only,
                       "bind": {"create_host_path": False}} for source, target, read_only in (
                (site["host_path"], "/var/gerrit", False),
                (plan["shared_host_path"], "/shared", False),
                ("./start.sh", "/opt/gerrit-startup/start.sh", True),
                (f"./{name}.sha256", "/opt/gerrit-startup/artifacts.sha256", True), *extra_mounts)]
            files[f"{name}-compose.yaml"] = yaml.safe_dump({"services": {name: {
                "image": plan["java_image"], "user": "1000:1000", "restart": "unless-stopped",
                "entrypoint": ["/bin/sh", "/opt/gerrit-startup/start.sh"],
                "command": ["/var/gerrit", "/shared", "/opt/gerrit-startup/artifacts.sha256", *guard_arguments(plan, "/opt/gerrit-startup")],
                "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
                "ports": ["8443:8443", "29418:29418"], "volumes": mounts,
                "tmpfs": ["/tmp:rw,nosuid,nodev,size=1g,mode=1777"], "mem_limit": "3g", "cpus": 4.0,
            }}}, sort_keys=False)
        else:
            guard_args = "".join(" " + arg for arg in guard_arguments(plan, "/etc/gerrit"))
            mount_condition = "ConditionPathIsMountPoint=" + plan["shared_host_path"] + "\n" if "shared_storage" in plan else ""
            files[f"{name}.service"] = f"""[Unit]
Description=Gerrit primary {name} (initialized site, no automatic migration)
After=network-online.target
Wants=network-online.target
RequiresMountsFor={site['host_path']} {plan['shared_host_path']}
{mount_condition}

[Service]
Type=simple
User=gerrit
Group=gerrit
WorkingDirectory={site['host_path']}
ExecStart=/bin/sh /etc/gerrit/start.sh {site['host_path']} {plan['shared_host_path']} /etc/gerrit/{name}.sha256{guard_args}
Restart=on-failure
RestartSec=10
TimeoutStopSec=120
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ReadWritePaths={site['host_path']} {plan['shared_host_path']}
CapabilityBoundingSet=
UMask=0077
MemoryMax=3G

[Install]
WantedBy=multi-user.target
"""
    files["render-status.json"] = json.dumps({
        "schema_version": 1, "component": "gerrit-sites", "runtime": runtime,
        "gerrit_version": plan["gerrit_version"], "deployed": False,
        "ha_verified": False, "initialization_performed": False,
        "shared_storage_guarded": "shared_storage" in plan,
        "healthcheck_configuration_guarded": plan["schema_version"] == 2,
        "artifact_metadata_guarded": plan["schema_version"] == 2,
        "storage_acceptance_verified": False,
        "prerequisites": ["reviewed-initialized-sites-and-matching-ssh-host-keys",
                          "shared-storage-mounted-with-correct-locking-semantics",
                          "private-tls-sso-peer-credentials-and-plugin-compatibility",
                          "native-java-version-or-container-runtime-compatibility",
                          "router-network-isolation-and-live-failover-acceptance"],
    }, indent=2) + "\n"
    return files


def verify_site(plan: dict, site_name: str, directory: Path) -> dict:
    """Read-only file locks and optional metadata checks, not live compatibility."""
    plan = validate(plan)
    selected = [s for s in plan["sites"] if s["name"] == site_name]
    if len(selected) != 1:
        raise SiteError("Select a site declared in the plan")
    root = directory.resolve(strict=True)
    locks = {**plan["artifacts"], **selected[0]["config_hashes"],
             "etc/ssh_host_ed25519_key.pub": plan["ssh_public_key_sha256"]}
    matched = 0
    for relative, expected in locks.items():
        candidate = root / relative
        if candidate.is_symlink() or not candidate.resolve(strict=True).is_relative_to(root):
            raise SiteError("Artifact symlinks or paths outside the selected site are not accepted")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 ** 3:
                raise SiteError("Artifact is not a bounded regular file")
            digest = hashlib.sha256()
            remaining = 1024 ** 3 + 1
            while chunk := stream.read(min(1024 ** 2, remaining)):
                remaining -= len(chunk)
                if remaining <= 0:
                    raise SiteError("Artifact exceeded the verification limit")
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise SiteError("Site artifact or reviewed configuration does not match its lock")
        matched += 1
    if plan["schema_version"] == 2: artifact_guard.verify(root, plan["gerrit_version"])
    return {"schema_version": 1, "read_only": True, "integrity_verified": True,
            "artifact_metadata_verified": plan["schema_version"] == 2,
            "files_verified": matched, "ha_verified": False, "compatibility_verified": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("render", "verify-site"))
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=RUNTIMES)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--site-name")
    parser.add_argument("--site-root", type=Path)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        if args.command == "render":
            if not args.runtime or not args.output_dir or args.site_name or args.site_root:
                raise SiteError("Render requires runtime/output and no verification arguments")
            files = render(plan, args.runtime)
            write_bundle(files, args.output_dir, private_root=ROOT / "private")
            report = {"rendered": True, "runtime": args.runtime, "files": len(files), "deployed": False, "ha_verified": False}
        else:
            if not args.site_name or not args.site_root or args.runtime or args.output_dir:
                raise SiteError("Verification requires site name/root only")
            report = verify_site(plan, args.site_name, args.site_root)
    except (ValueError, OSError, RuntimeError):
        print("Gerrit site operation failed; review the private plan, locks, site files and output directory.", file=sys.stderr)
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
