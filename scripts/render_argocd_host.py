#!/usr/bin/env python3
"""Render Docker Argo CD services for an external Kubernetes API."""

import argparse
import json
from pathlib import Path
import re

import yaml

import argocd_valkey
from bounded_file import read_bounded_text
from kubernetes_management import validate_target
from render_forge_host import port, private_path
from render_gerrit_router import IMAGE, dns_name, write_bundle
from render_gerrit_sites import label
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
REPO = "argocd-repo-server"
CONTROLLER = "argocd-application-controller"
SERVER = "argocd-server"
APPSET = "argocd-applicationset-controller"
NOTIFICATIONS = "argocd-notifications"
INPUT = "/run/platform/argocd"


def validate(plan):
    fields = {"schema_version", "image", "management_target", "namespace", "repository_inputs",
              "controller_inputs", "resync_seconds"}
    if (not isinstance(plan, dict)
            or set(plan) - {"api_server", "application_set", "notifications", "cache_host", "cache_port"} != fields
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1):
        raise ValueError("Invalid Argo CD host plan")
    validate_target(plan["management_target"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}", plan["management_target"]["context"]):
        raise ValueError("Use a plain kubeconfig context name")
    if not label(plan["namespace"]) or plan["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}:
        raise ValueError("Select a dedicated Argo CD namespace")
    if not isinstance(plan["image"], str) or not IMAGE.fullmatch(plan["image"]):
        raise ValueError("Pin the Argo CD image by digest")
    repo, controller = plan["repository_inputs"], plan["controller_inputs"]
    if (not private_path(repo) or not private_path(controller) or repo == controller
            or repo.startswith(controller + "/") or controller.startswith(repo + "/")):
        raise ValueError("Separate the repository and controller credentials")
    if type(plan["resync_seconds"]) is not int or not 2 <= plan["resync_seconds"] <= 3600:
        raise ValueError("Select an explicit resync period")
    if "api_server" in plan:
        api = plan["api_server"]
        if (not isinstance(api, dict) or set(api) != {"inputs", "port"}
                or not private_path(api["inputs"]) or not port(api["port"], 1024)
                or any(path == api["inputs"] or path.startswith(api["inputs"] + "/")
                       or api["inputs"].startswith(path + "/") for path in (repo, controller))):
            raise ValueError("Separate API credentials and select an unprivileged loopback port")
    other_inputs = [repo, controller]
    if "api_server" in plan:
        other_inputs.append(plan["api_server"]["inputs"])
    for section in ("application_set", "notifications"):
        if section not in plan:
            continue
        service = plan[section]
        if (not isinstance(service, dict) or set(service) != {"inputs"} or not private_path(service["inputs"])
                or any(path == service["inputs"] or path.startswith(service["inputs"] + "/")
                       or service["inputs"].startswith(path + "/") for path in other_inputs)):
            raise ValueError(f"Separate {section} credentials from other Argo CD inputs")
        other_inputs.append(service["inputs"])


def cache_configuration(plan, cache_bundle, cache_client_plan):
    direct_keys = {"cache_host", "cache_port"} & plan.keys()
    if cache_bundle is None and cache_client_plan is None:
        if len(direct_keys) != 2 or not dns_name(plan["cache_host"]) or not port(plan["cache_port"]):
            raise ValueError("Select an explicit TLS cache endpoint")
        return (
            [f"--redis={plan['cache_host']}:{plan['cache_port']}", "--redis-use-tls",
             f"--redis-ca-certificate={INPUT}/cache/ca.crt",
             f"--redis-client-certificate={INPUT}/cache/tls.crt",
             f"--redis-client-key={INPUT}/cache/tls.key"],
            {"REDIS_CREDS_DIR_PATH": INPUT + "/cache/credentials",
             "REPOSERVER_REDIS_CREDS_DIR_PATH": INPUT + "/cache/credentials"},
            ("cache/ca.crt", "cache/tls.crt", "cache/tls.key", "cache/credentials/auth"),
        )
    if direct_keys or cache_bundle is None or cache_client_plan is None:
        raise ValueError("Select only the pinned Sentinel cache package and its client plan together")
    contract = argocd_valkey.selected(cache_bundle, cache_client_plan)
    application = cache_client_plan["application"]
    if application["namespace"] != plan["namespace"] or application["private_dir"] != INPUT + "/cache":
        raise ValueError("Bind the Sentinel client plan to this namespace and container cache mount")
    return (
        argocd_valkey.arguments(contract, cache_client_plan),
        argocd_valkey.environment(cache_client_plan),
        ("cache/tls/ca.crt", "cache/tls/tls.crt", "cache/tls/tls.key",
         *("cache/credentials/" + name for name in argocd_valkey.CLIENT_FILES)),
    )


def render(plan, *, cache_bundle=None, cache_client_plan=None):
    validate(plan)
    cache_args, cache_environment, cache_files = cache_configuration(plan, cache_bundle, cache_client_plan)
    kube_args = [f"--kubeconfig={INPUT}/kubeconfig", f"--context={plan['management_target']['context']}",
                 f"--namespace={plan['namespace']}"]
    repo_args = [
        f"--repo-server={REPO}:8081", f"--repo-server-ca-cert-path={INPUT}/repo-ca.crt",
        f"--repo-server-client-cert-path={INPUT}/repo-client.crt",
        f"--repo-server-client-cert-key-path={INPUT}/repo-client.key",
    ]
    specifications = [
        (REPO, plan["repository_inputs"], [
            "--address=0.0.0.0", "--port=8081", "--metrics-address=127.0.0.1",
            f"--client-ca-path={INPUT}/repo-ca.crt",
            f"--revision-cache-expiration={plan['resync_seconds']}s", "--parallelismlimit=4",
        ]),
        (CONTROLLER, plan["controller_inputs"], [
            *kube_args, *repo_args,
            f"--app-resync={plan['resync_seconds']}", "--app-resync-jitter=0",
        ]),
    ]
    api = plan.get("api_server")
    if api:
        specifications.append((SERVER, api["inputs"], [
            *kube_args, *repo_args, "--address=0.0.0.0", "--port=8080",
            "--metrics-address=127.0.0.1", "--insecure=false", "--disable-auth=false",
        ]))
    appset = plan.get("application_set")
    if appset:
        specifications.append((APPSET, appset["inputs"], [
            *kube_args, f"--argocd-repo-server={REPO}:8081", *repo_args[1:],
            "--metrics-addr=127.0.0.1:8080", "--probe-addr=127.0.0.1:8081", "--webhook-addr=127.0.0.1:7000",
            "--enable-scm-providers=false", "--enable-policy-override=false",
            "--policy=create-update", "--concurrent-reconciliations=2",
        ]))
    if "notifications" in plan:
        specifications.append((NOTIFICATIONS, plan["notifications"]["inputs"], [
            *kube_args, f"--argocd-repo-server={REPO}:8081", *repo_args[1:],
            "--self-service-notification-enabled=false",
        ]))
    services = {}
    for name, inputs, args in specifications:
        required = ["repo-ca.crt"]
        uses_cache = name not in {APPSET, NOTIFICATIONS}
        if uses_cache:
            required.extend(cache_files)
        if name == NOTIFICATIONS:
            required.append("notification-ca-bundle.crt")
        required.extend(("config/reposerver/tls/tls.crt", "config/reposerver/tls/tls.key") if name == REPO
                        else ("kubeconfig", "repo-client.crt", "repo-client.key"))
        guard = " && ".join(f"test -s {INPUT}/{path}" for path in required)
        command = [*args, *cache_args] if uses_cache else args
        if name == SERVER:
            # The API has a second, independently configured repo-server cache client.
            command.extend("--repo-server-" + flag[2:] for flag in cache_args)
        services[name] = {
            "image": plan["image"], "user": "999:999", "init": True, "read_only": True,
            "entrypoint": ["/bin/sh", "-ec", guard + ' && exec "$$@"', "argocd", "/usr/local/bin/" + name],
            "command": command,
            "environment": {
                "HOME": "/tmp/home", "XDG_CACHE_HOME": "/tmp/cache",
                "GRPC_ENABLE_TXT_SERVICE_CONFIG": "false", "ARGOCD_GIT_MODULES_ENABLED": "false",
                **(cache_environment if uses_cache else {}),
                # The ApplicationSet manager loads its client config independently of the CLI flags.
                **({"KUBECONFIG": INPUT + "/kubeconfig"} if name == APPSET else {}),
                **({"SSL_CERT_FILE": INPUT + "/notification-ca-bundle.crt"} if name == NOTIFICATIONS else {}),
                **({"ARGOCD_APP_CONF_PATH": INPUT + "/config", "ARGOCD_TLS_DATA_PATH": INPUT + "/git-trust",
                    "ARGOCD_ASK_PASS_SOCK": "/tmp/argocd-askpass.sock"} if name == REPO else {}),
                **({
                    "ARGOCD_CONTROLLER_REPLICAS": "1", "ARGOCD_CONTROLLER_SHARD": "0",
                } if name == CONTROLLER else {}),
            },
            "volumes": [{"type": "bind", "source": inputs, "target": INPUT, "read_only": True,
                         "bind": {"create_host_path": False}}],
            "tmpfs": [
                "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
                *(["/app/config/gpg/keys:rw,nosuid,nodev,size=16m,mode=0700,uid=999,gid=999"] if name == REPO else []),
            ],
            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "restart": "unless-stopped", "pids_limit": 512, "mem_limit": "2g",
        }
    if api:
        services[SERVER]["ports"] = [f"127.0.0.1:{api['port']}:8080"]
    contract = {"schema_version": 1, "management_target": plan["management_target"], "namespace": plan["namespace"]}
    return {"compose.yaml": yaml.safe_dump({"services": services}, sort_keys=False),
            "management-target.json": json.dumps(contract, sort_keys=True, indent=2) + "\n"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cache-bundle-dir", type=Path)
    parser.add_argument("--cache-client-plan", type=Path)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, max_bytes=32768))
        client_plan = (loads_strict_json(read_bounded_text(args.cache_client_plan, max_bytes=32768))
                       if args.cache_client_plan is not None else None)
        if args.cache_client_plan is not None and not isinstance(client_plan, dict):
            raise ValueError("Supply a Sentinel client plan object")
        write_bundle(render(plan, cache_bundle=args.cache_bundle_dir, cache_client_plan=client_plan),
                     args.output_dir, private_root=ROOT / "private")
    except (ValueError, OSError):
        print("Argo CD host rendering failed. Review the private plan and select a new output directory.")
        return 1
    print(json.dumps({"rendered": True, "runtime": "docker", "deployed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
