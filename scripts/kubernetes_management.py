#!/usr/bin/env python3
"""Shared explicit-target Kubernetes operations; constructing a client is opt-in."""

from __future__ import annotations

from contextlib import contextmanager
import os
import json
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import urlsplit

from bounded_subprocess import run_bounded
from strict_json import loads_strict_json


class KubernetesOperationError(ValueError):
    """Retain fixed failure categories, never command arguments or server text."""

    def __init__(self, return_code: int, stderr: str):
        super().__init__("Kubernetes operation failed; inspect private server/controller diagnostics")
        text = stderr.casefold()
        patterns = {
            "timeout": ("timed out", "i/o timeout", "tls handshake timeout", "deadline exceeded", "requesttimeout", "(timeout)", "(servertimeout)", "request did not complete within requested timeout",
                        "client.timeout exceeded while awaiting headers"),
            "connection_failure": ("connection refused", "connection reset", "unexpected eof", "no route to host"),
            "tls_failure": ("x509:", "tls handshake", "certificate signed by unknown authority"),
            "certificate_time_invalid": ("certificate has expired", "not yet valid"),
            "certificate_untrusted": ("certificate signed by unknown authority", "unknown certificate authority"),
            "certificate_name_mismatch": ("certificate is valid for", "certificate is not valid for", "doesn't contain any ip sans"),
            "tls_handshake_timeout": ("tls handshake timeout",),
            "webhook_failure": ("failed calling webhook", "admission webhook", "conversion webhook"),
            "field_conflict": ("fieldmanagerconflict", "conflict with", "conflicts with"),
            "already_exists": ("alreadyexists", "already exists"),
            "not_found": ("notfound", "not found"),
            "forbidden": ("forbidden",),
            "unauthorized": ("unauthorized",),
            "invalid_resource": ("invalid value", "is invalid", "error validating data", "(invalid)"),
            "unavailable": ("serviceunavailable", "service unavailable", "no endpoints available"),
            "discovery_not_ready": ("no matches for kind", "ensure crds are installed first", "could not find the requested resource"),
        }
        self.diagnostic = {"return_code": return_code,
            "signals": {name: any(pattern in text for pattern in alternatives) for name, alternatives in patterns.items()}}
        # A negotiation timeout does not establish an invalid peer certificate.
        # Keep any other TLS/certificate evidence distinct, including mixed
        # retries in one stderr capture. Unknown TLS failures remain hard errors.
        remainder=text.replace('tls handshake timeout','')
        self.diagnostic['signals']['tls_non_timeout_failure']=any(marker in remainder for marker in ('x509','certificate','tls','ssl','handshake'))



def validate_target(target: object) -> dict:
    if (not isinstance(target, dict) or set(target) != {"kubeconfig_env", "context", "cluster_uid"}
            or not isinstance(target["kubeconfig_env"], str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", target["kubeconfig_env"])
            or not isinstance(target["context"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", target["context"])
            or not isinstance(target["cluster_uid"], str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", target["cluster_uid"])):
        raise ValueError("Declare an explicit Kubernetes context and kube-system UID")
    return target


class Client:
    def __init__(self, target: dict):
        validate_target(target)
        config = os.environ.get(target["kubeconfig_env"], "")
        if not config or not Path(config).is_file():
            raise ValueError("Provision the explicit management kubeconfig path")
        binary = shutil.which("kubectl")
        if not binary:
            raise ValueError("kubectl is required for explicit server actions")
        self.prefix = [binary, "--kubeconfig", config, "--context", target["context"]]

    @contextmanager
    def frozen(self):
        # Resolve local certificate references before moving the configuration.
        # config view does not execute credential plugins; reject those plugins
        # before any server command can invoke them.
        config = loads_strict_json(self.call(["config", "view", "--minify", "--flatten", "--raw", "-o", "json"]))
        users = config.get("users", []) if isinstance(config, dict) else []
        credentials = users[0].get("user") if len(users) == 1 else None
        clusters = config.get("clusters", []) if isinstance(config, dict) else []
        if (not isinstance(credentials, dict) or set(credentials) not in ({"token"}, {"client-certificate-data", "client-key-data"})
                or any(not isinstance(value, str) or not value for value in credentials.values()) or len(clusters) != 1
                or "certificate-authority" in clusters[0].get("cluster", {})):
            raise ValueError("Use self-contained static credentials for a frozen management operation")
        original = self.prefix
        with tempfile.TemporaryDirectory(prefix="platform-management-target-") as directory:
            path = Path(directory) / "kubeconfig"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                json.dump(config, output, sort_keys=True)
            self.prefix = [original[0], "--kubeconfig", str(path), "--context", original[-1]]
            try:
                yield self
            finally:
                self.prefix = original

    def call(self, args: list[str], *, payload: bytes | None = None, seconds: int = 30) -> str:
        result = run_bounded([*self.prefix, f"--request-timeout={seconds}s", *args], input=(payload.decode("utf-8") if payload is not None else ""),
                             timeout=seconds + 15, text=True, output_max_bytes=4 * 1024 ** 2, check=False)
        if result.returncode:
            raise KubernetesOperationError(result.returncode, result.stderr)
        return result.stdout

    def get(self, resource: str, namespace: str | None = None, *, optional: bool = False) -> dict | None:
        args = ["get", resource, "-o", "json"]
        if namespace:
            args += ["--namespace", namespace]
        if optional:
            args.append("--ignore-not-found=true")
        data = self.call(args).strip()
        return loads_strict_json(data) if data else None


def verify_target(client: Client, target: dict) -> None:
    validate_target(target)
    config = loads_strict_json(client.call(["config", "view", "--minify", "-o", "json"]))
    clusters, contexts = config.get("clusters", []), config.get("contexts", [])
    name = target["context"]
    if (len(clusters) != 1 or len(contexts) != 1 or config.get("current-context") != name
            or contexts[0].get("name") != name or contexts[0].get("context", {}).get("cluster") != clusters[0].get("name")):
        raise ValueError("Management context must select one cluster")
    cluster = clusters[0]["cluster"]
    server = urlsplit(cluster.get("server", ""))
    if cluster.get("insecure-skip-tls-verify") or server.scheme != "https" or not server.hostname or server.username or server.password or server.query or server.fragment:
        raise ValueError("Management API requires verified HTTPS")
    identity = client.get("namespace/kube-system")
    if not identity or identity.get("metadata", {}).get("uid") != target["cluster_uid"]:
        raise ValueError("Management cluster UID differs from the reviewed target")


def inventory(objects: list[dict]) -> list[dict]:
    return [{"apiVersion": obj["apiVersion"], "kind": obj["kind"], "name": obj["metadata"]["name"], "namespace": obj["metadata"].get("namespace")}
            for obj in objects]


def resource_name(item: dict) -> str:
    group = item["apiVersion"].split("/")[0] if "/" in item["apiVersion"] else ""
    return item["kind"].lower() + ("." + group if group else "") + "/" + item["name"]


def verify_owned(client: Client, item: dict, label: str, owner: str) -> None:
    if item["kind"] == "Namespace":
        return
    if item["kind"] == "Secret":
        # kubectl formats only metadata: never capture generated private key or
        # credential contents in this process while inspecting ownership.
        template = '{{.metadata.name}}{{"\\n"}}{{with .metadata.labels}}{{index . ' + json.dumps(label) + '}}{{end}}'
        output = client.call(["get", resource_name(item), "--namespace", item["namespace"],
                              "--ignore-not-found=true", "-o", "go-template=" + template]).strip()
        if output and ("\n" not in output or output.split("\n", 1)[1].strip() != owner):
            raise ValueError("Existing component Secret is not owned by this package")
        return
    current = client.get(resource_name(item), item["namespace"], optional=True)
    if current and ((current.get("metadata") or {}).get("labels") or {}).get(label) != owner:
        raise ValueError("Existing component resource is not owned by this package")
