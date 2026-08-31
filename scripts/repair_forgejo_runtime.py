#!/usr/bin/env python3
"""Repair Forgejo's runtime dependencies without changing its data volumes."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from bounded_file import read_bounded_bytes, read_bounded_text
from bounded_subprocess import run_bounded
from strict_json import loads_strict_json
from subprocess_timeout import bounded_timeout_seconds


class RepairError(RuntimeError):
    """A bounded, actionable runtime-repair failure."""


KUBECTL = os.environ.get("KUBECTL_BIN", "/var/lib/rancher/rke2/bin/kubectl")
KUBECONFIG = os.environ.get("KUBECONFIG_PATH", "/etc/rancher/rke2/rke2.yaml")
FORGEJO_NAMESPACE = os.environ.get("PLATFORM_FORGEJO_NAMESPACE", "forgejo")
FORGEJO_SELECTOR = "app.kubernetes.io/name=forgejo,app.kubernetes.io/instance=forgejo"
POSTGRES_NAMESPACE = os.environ.get("PLATFORM_POSTGRES_NAMESPACE", "platform-databases")
POSTGRES_HOST = os.environ.get(
    "PLATFORM_POSTGRES_HOST",
    "platform-postgres-rw.platform-databases.svc.cluster.local",
)
OBJECT_STORAGE_SECRET = os.environ.get("FORGEJO_S3_SECRET_NAME", "forgejo-object-storage")
REQUESTED_STORAGE_MODE = os.environ.get("FORGEJO_OBJECT_STORAGE_MODE", "").strip().lower()
MOUNT_PATHS = ("/data/gitea/git/.postgresql", "/etc/ssl/platform")
POSTGRES_CA_ITEM_PATHS = ("ca-certificates.crt", "root.crt")
COMMAND_TIMEOUT_SECONDS = 60


def command(args: list[str], *, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    timeout = bounded_timeout_seconds(
        COMMAND_TIMEOUT_SECONDS,
        "PLATFORM_FORGEJO_RUNTIME_COMMAND_TIMEOUT_SECONDS",
    )
    return run_bounded(
        args,
        check=check,
        text=True,
        input=input_text,
        timeout=timeout,
    )


def write_private_bytes(path: Path, content: bytes) -> None:
    """Write temporary certificate material without using an unguarded file API."""
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("temporary certificate write made no progress")
            remaining = remaining[written:]
    finally:
        os.close(descriptor)


def kube(*args: str, namespace: str | None = None, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    command_args = [KUBECTL, "--kubeconfig", KUBECONFIG]
    if namespace:
        command_args.extend(["-n", namespace])
    command_args.extend(args)
    return command(command_args, check=check, input_text=input_text)


def resource_json(
    resource: str,
    *,
    namespace: str | None = None,
    selector: str | None = None,
) -> dict[str, Any] | None:
    resource_args = ["get", resource]
    if selector:
        resource_args.extend(["-l", selector])
    resource_args.extend(["-o", "json"])
    result = kube(*resource_args, namespace=namespace, check=False)
    if result.returncode != 0:
        return None
    try:
        document = loads_strict_json(result.stdout)
    except json.JSONDecodeError:
        return None
    return document if isinstance(document, dict) else None


def resource_text(resource: str, *, namespace: str | None = None) -> str:
    result = kube("get", resource, "-o", "jsonpath={.data.ca-certificates\\.crt}", namespace=namespace, check=False)
    return result.stdout if result.returncode == 0 else ""


def fail(reason: str, message: str) -> None:
    print(message, file=sys.stderr)
    print(f"result=fail reason={reason}")
    raise RepairError(reason)


def secret_data(namespace: str, name: str) -> dict[str, bytes]:
    document = resource_json(f"secret/{name}", namespace=namespace)
    if not document:
        return {}
    decoded: dict[str, bytes] = {}
    for key, value in (document.get("data") or {}).items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        try:
            decoded[key] = base64.b64decode(value, validate=True)
        except (ValueError, TypeError):
            continue
    return decoded


def storage_backend(workload: dict[str, Any]) -> tuple[str, bool]:
    material = bytearray(json.dumps(workload, sort_keys=True).encode())
    for name in ("forgejo", "forgejo-inline-config"):
        for value in secret_data(FORGEJO_NAMESPACE, name).values():
            material.extend(b"\n")
            material.extend(value)
    lowered = bytes(material).lower()
    minio = any(
        marker in lowered
        for marker in (
            b"minio_endpoint",
            b"minio_access_key",
            b"minio_secret_key",
            b"storage_type: minio",
            b"storage_type = minio",
            b"storage_type=minio",
        )
    )
    filesystem = bool(
        re.search(rb"storage_type\s*[:=]\s*(local|filesystem|file)", lowered)
    )
    credentials = all(
        secret_data(FORGEJO_NAMESPACE, OBJECT_STORAGE_SECRET).get(key)
        for key in ("access-key-id", "secret-access-key")
    )
    if minio:
        return "minio", credentials
    if filesystem:
        return "filesystem", credentials
    return "filesystem", credentials


def database_backend(workload: dict[str, Any]) -> str:
    """Resolve the effective live Forgejo database type without logging secrets."""
    named_presence = {"forgejo": False, "gitea": False}
    named_values: dict[str, list[str]] = {"forgejo": [], "gitea": []}
    pod_spec = (workload.get("spec") or {}).get("template", {}).get("spec") or {}
    containers = (pod_spec.get("containers", []) or []) + (
        pod_spec.get("initContainers", []) or []
    )
    for container in containers:
        for env in container.get("env", []) or []:
            if not isinstance(env, dict):
                continue
            name = str(env.get("name") or "").strip().lower()
            if name not in {
                "forgejo__database__db_type",
                "gitea__database__db_type",
            }:
                continue
            source = "forgejo" if name.startswith("forgejo__") else "gitea"
            named_presence[source] = True
            value = env.get("value")
            if isinstance(value, str) and value.strip():
                named_values[source].append(value)

    material = bytearray(json.dumps(workload, sort_keys=True).encode())
    for name in ("forgejo", "forgejo-inline-config"):
        for value in secret_data(FORGEJO_NAMESPACE, name).values():
            material.extend(b"\n")
            material.extend(value)
    configured_values = re.findall(
        rb"(?im)(?:^|[\"'])"
        rb"(?:db_type|database_type)[\"']?\s*[:=]\s*[\"']?"
        rb"([a-z0-9_.+-]+)",
        bytes(material),
    )

    def normalize(value: str) -> str | None:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9_.+-]+", normalized):
            return None
        return {"postgresql": "postgres", "mariadb": "mysql"}.get(
            normalized, normalized
        )

    for source in ("forgejo", "gitea"):
        if not named_presence[source]:
            continue
        values = unique(
            normalized
            for normalized in (
                normalize(value) for value in named_values[source]
            )
            if normalized
        )
        if len(values) != 1:
            fail(
                "forgejo-database-type-unknown",
                "Forgejo live configuration does not expose one unambiguous "
                "database backend; refusing PostgreSQL-specific repair.",
            )
        return values[0]

    values = unique(
        normalized
        for normalized in (
            normalize(value.decode("ascii", errors="ignore"))
            for value in configured_values
        )
        if normalized
    )
    if len(values) > 1:
        fail(
            "forgejo-database-type-ambiguous",
            "Forgejo live configuration exposes conflicting database backends; "
            "refusing PostgreSQL-specific repair.",
        )
    if values:
        return values[0]
    fail(
        "forgejo-database-type-unknown",
        "Forgejo live configuration does not expose an explicit DB_TYPE; "
        "refusing to assume PostgreSQL.",
    )
    return "unknown"


def validate_storage_contract(workload: dict[str, Any]) -> str:
    if REQUESTED_STORAGE_MODE not in {
        "",
        "filesystem",
        "file",
        "local",
        "disk",
        "s3",
        "minio",
        "object",
        "object-storage",
        "object_storage",
    }:
        fail(
            "forgejo-object-storage-mode-invalid",
            "FORGEJO_OBJECT_STORAGE_MODE must be filesystem or s3.",
        )

    backend, credentials = storage_backend(workload)
    filesystem_requested = REQUESTED_STORAGE_MODE in {"filesystem", "file", "local", "disk"}
    object_requested = REQUESTED_STORAGE_MODE in {
        "s3",
        "minio",
        "object",
        "object-storage",
        "object_storage",
    }

    if backend == "minio" and filesystem_requested:
        fail(
            "forgejo-object-storage-mode-not-applied",
            "Forgejo still has MinIO settings in its live rendered configuration, "
            "but filesystem mode was requested. Render and reconcile the filesystem "
            "configuration before restarting Forgejo.",
        )
    if backend == "minio" and not credentials:
        fail(
            "forgejo-object-storage-secret-missing",
            "Forgejo live configuration uses MinIO, but the configured object-storage "
            "Secret does not contain both credentials. Configure S3/MinIO "
            "credentials, or explicitly render filesystem mode before retrying.",
        )
    if object_requested and backend != "minio":
        fail(
            "forgejo-object-storage-mode-not-applied",
            "S3 mode was requested, but Forgejo's live rendered configuration does "
            "not contain MinIO/S3 settings. Reconcile the Forgejo GitOps values first.",
        )
    if backend == "minio":
        print("forgejo_storage_backend=minio state=present")
        return backend

    if filesystem_requested:
        print("forgejo_storage_backend=filesystem state=present")
    else:
        print("forgejo_storage_backend=filesystem state=default-no-minio-config")
    return "filesystem"


def openssl(*args: str) -> subprocess.CompletedProcess[str]:
    return command(["openssl", *args], check=False)


def certificate_matches_host(path: Path) -> bool:
    result = openssl("x509", "-in", str(path), "-noout", "-checkhost", POSTGRES_HOST)
    return result.returncode == 0 and f"Hostname {POSTGRES_HOST} does match certificate" in result.stdout + result.stderr


def certificate_verifies(ca_path: Path, leaf_path: Path) -> bool:
    result = openssl(
        "verify",
        "-purpose",
        "sslserver",
        "-verify_hostname",
        POSTGRES_HOST,
        "-CAfile",
        str(ca_path),
        str(leaf_path),
    )
    return result.returncode == 0


def unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def postgres_cluster() -> dict[str, Any]:
    cluster = resource_json("cluster.postgresql.cnpg.io/platform-postgres", namespace=POSTGRES_NAMESPACE)
    if not cluster:
        fail(
            "forgejo-postgres-cluster-missing",
            f"CloudNativePG cluster platform-postgres is not available in {POSTGRES_NAMESPACE}.",
        )
    return cluster


def postgres_server_candidates(cluster: dict[str, Any]) -> list[str]:
    status = cluster.get("status") or {}
    spec = cluster.get("spec") or {}
    status_certs = status.get("certificates") or {}
    spec_certs = spec.get("certificates") or {}
    candidates = [
        status_certs.get("serverTLSSecret", ""),
        spec_certs.get("serverTLSSecret", ""),
        "platform-postgres-server",
        "platform-postgres-server-tls",
    ]
    secrets = kube(
        "get",
        "secrets",
        "-l",
        "cnpg.io/cluster=platform-postgres",
        "-o",
        "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}",
        namespace=POSTGRES_NAMESPACE,
        check=False,
    )
    candidates.extend(secrets.stdout.splitlines())
    return unique(candidates)


def postgres_ca_candidates(cluster: dict[str, Any], active_tls_secret: str) -> list[str]:
    status = cluster.get("status") or {}
    spec = cluster.get("spec") or {}
    status_certs = status.get("certificates") or {}
    spec_certs = spec.get("certificates") or {}
    candidates = [
        status_certs.get("serverCASecret", ""),
        spec_certs.get("serverCASecret", ""),
        "platform-postgres-ca",
        active_tls_secret,
        "platform-postgres-server",
        "platform-postgres-server-tls",
    ]
    secrets = kube(
        "get",
        "secrets",
        "-l",
        "cnpg.io/cluster=platform-postgres",
        "-o",
        "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}",
        namespace=POSTGRES_NAMESPACE,
        check=False,
    )
    candidates.extend(secrets.stdout.splitlines())
    return unique(candidates)


def active_postgres_certificate(
    cluster: dict[str, Any], temp_dir: Path
) -> tuple[str, Path]:
    for name in postgres_server_candidates(cluster):
        encoded = (secret_data(POSTGRES_NAMESPACE, name)).get("tls.crt")
        if not encoded:
            continue
        leaf = temp_dir / "postgres-server.crt"
        write_private_bytes(leaf, encoded)
        if leaf.stat().st_size and certificate_matches_host(leaf):
            print(f"forgejo_postgres_active_server_certificate={POSTGRES_NAMESPACE}/{name}:tls.crt")
            return name, leaf
    fail(
        "forgejo-postgres-active-server-certificate-missing",
        "No CloudNativePG server certificate matching "
        f"{POSTGRES_HOST} could be found.",
    )


def system_trust_path() -> Path | None:
    for candidate in (
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/ssl/ca-bundle.pem",
    ):
        path = Path(candidate)
        if path.is_file() and path.stat().st_size:
            return path
    return None


def refresh_forgejo_bundle(
    cluster: dict[str, Any],
    active_tls_secret: str,
    leaf_path: Path,
    temp_dir: Path,
) -> bool:
    existing = resource_text("configmap/platform-internal-roots", namespace=FORGEJO_NAMESPACE)
    existing_path = temp_dir / "existing-bundle.crt"
    write_private_bytes(existing_path, existing.encode())
    if certificate_verifies(existing_path, leaf_path):
        print(f"forgejo_postgres_ca_bundle=verified namespace={FORGEJO_NAMESPACE}")
        return False

    system_trust = system_trust_path()
    for name in postgres_ca_candidates(cluster, active_tls_secret):
        ca = secret_data(POSTGRES_NAMESPACE, name).get("ca.crt")
        if not ca:
            continue
        ca_path = temp_dir / "candidate-ca.crt"
        write_private_bytes(ca_path, ca)
        if not certificate_verifies(ca_path, leaf_path):
            continue

        bundle_path = temp_dir / "bundle.crt"
        if "BEGIN CERTIFICATE" in existing:
            base = existing.encode()
        elif system_trust:
            base = read_bounded_bytes(system_trust)
        else:
            base = b""
        write_private_bytes(bundle_path, base.rstrip() + b"\n" + ca.rstrip() + b"\n")
        if not certificate_verifies(bundle_path, leaf_path):
            continue

        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "platform-internal-roots",
                "labels": {
                    "app.kubernetes.io/part-of": "platform-pki",
                    "platform.gitops/trust-bundle": "internal-roots",
                },
            },
            "data": {"ca-certificates.crt": read_bounded_text(bundle_path)},
        }
        applied = kube(
            "apply",
            "-f",
            "-",
            namespace=FORGEJO_NAMESPACE,
            input_text=json.dumps(manifest),
            check=False,
        )
        if applied.returncode != 0:
            continue
        print(f"forgejo_postgres_ca_bundle=materialized source={POSTGRES_NAMESPACE}/{name}:ca.crt")
        for application in ("cert-manager", "trust-manager"):
            if resource_json(f"application/{application}", namespace="argocd"):
                kube(
                    "annotate",
                    f"application/{application}",
                    "argocd.argoproj.io/refresh=hard",
                    "--overwrite",
                    namespace="argocd",
                    check=False,
                )
        return True

    fail(
        "forgejo-postgres-ca-bundle-does-not-verify-active-server",
        "Forgejo's platform-internal-roots ConfigMap does not verify the "
        f"active PostgreSQL certificate for {POSTGRES_HOST}, and no matching "
        "CloudNativePG server CA was available.",
    )


def mount_contract_ready(workload: dict[str, Any]) -> bool:
    pod_spec = (workload.get("spec") or {}).get("template", {}).get("spec") or {}
    volume = next(
        (
            item
            for item in pod_spec.get("volumes", []) or []
            if item.get("name") == "platform-postgres-ca"
        ),
        None,
    )
    config_map = (volume or {}).get("configMap") or {}
    items = config_map.get("items") or []
    configured_items = {
        (item.get("key"), item.get("path"))
        for item in items
        if isinstance(item, dict)
    }
    volume_ready = (
        config_map.get("name") == "platform-internal-roots"
        and all(
            ("ca-certificates.crt", path) in configured_items
            for path in POSTGRES_CA_ITEM_PATHS
        )
    )
    if not volume_ready:
        return False
    containers = (pod_spec.get("containers", []) or []) + (pod_spec.get("initContainers", []) or [])
    if not containers:
        return False
    for container in containers:
        mounts = {
            item.get("mountPath")
            for item in container.get("volumeMounts", []) or []
            if item.get("name") == "platform-postgres-ca"
        }
        if not set(MOUNT_PATHS).issubset(mounts):
            return False
    return True


def patch_mount_contract(workload: str, document: dict[str, Any]) -> bool:
    if mount_contract_ready(document):
        print(f"forgejo_postgres_ca_mount=present workload={workload}")
        return False

    pod_spec = (document.get("spec") or {}).get("template", {}).get("spec") or {}
    containers = [
        {"name": container["name"], "volumeMounts": [
            {"name": "platform-postgres-ca", "mountPath": MOUNT_PATHS[0], "readOnly": True},
            {"name": "platform-postgres-ca", "mountPath": MOUNT_PATHS[1], "readOnly": True},
        ]}
        for key in ("containers", "initContainers")
        for container in pod_spec.get(key, []) or []
        if container.get("name")
    ]
    if not any(item["name"] for item in containers):
        fail("forgejo-runtime-container-missing", "Forgejo workload has no named containers to patch.")

    patch = {
        "spec": {
            "template": {
                "spec": {
                    "volumes": [{
                        "name": "platform-postgres-ca",
                        "configMap": {
                            "name": "platform-internal-roots",
                            "items": [
                                {"key": "ca-certificates.crt", "path": path}
                                for path in POSTGRES_CA_ITEM_PATHS
                            ],
                        },
                    }],
                    "containers": [
                        item for item in containers
                        if item["name"] in {
                            container["name"] for container in pod_spec.get("containers", []) or []
                        }
                    ],
                    "initContainers": [
                        item for item in containers
                        if item["name"] in {
                            container["name"] for container in pod_spec.get("initContainers", []) or []
                        }
                    ],
                }
            }
        }
    }
    patched = kube(
        "patch",
        workload,
        "--type=strategic",
        "-p",
        json.dumps(patch, separators=(",", ":")),
        namespace=FORGEJO_NAMESPACE,
        check=False,
    )
    if patched.returncode != 0:
        fail(
            "forgejo-runtime-mount-patch-failed",
            f"Could not add the PostgreSQL CA mount to {FORGEJO_NAMESPACE}/{workload}.",
        )
    print(f"forgejo_postgres_ca_mount=patched workload={workload}")
    return True


def ready_pods() -> int:
    pods = resource_json(
        "pods",
        namespace=FORGEJO_NAMESPACE,
        selector=FORGEJO_SELECTOR,
    )
    if not pods:
        return 0
    count = 0
    for pod in pods.get("items", []) or []:
        if not isinstance(pod, dict):
            continue
        if any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in (pod.get("status") or {}).get("conditions", []) or []
        ):
            count += 1
    return count


def ready_endpoints() -> int:
    slices = resource_json(
        "endpointslices.discovery.k8s.io",
        namespace=FORGEJO_NAMESPACE,
    )
    if not slices:
        return 0
    count = 0
    for slice_document in slices.get("items", []) or []:
        if not isinstance(slice_document, dict):
            continue
        if (
            (slice_document.get("metadata") or {})
            .get("labels", {})
            .get("kubernetes.io/service-name")
            != "forgejo-http"
        ):
            continue
        for endpoint in (slice_document.get("endpoints") or []):
            conditions = endpoint.get("conditions") or {}
            if conditions.get("ready") is not False and endpoint.get("addresses"):
                count += 1
    return count


def wait_for_runtime(workload: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rollout = kube(
            "rollout",
            "status",
            workload,
            "--timeout=10s",
            namespace=FORGEJO_NAMESPACE,
            check=False,
        )
        if rollout.returncode == 0 and ready_pods() > 0 and ready_endpoints() > 0:
            print(f"forgejo_runtime=ready pods={ready_pods()} endpoints={ready_endpoints()}")
            return
        time.sleep(5)
    diagnostics = kube(
        "get",
        "pods,service,endpointslice",
        "-o",
        "wide",
        namespace=FORGEJO_NAMESPACE,
        check=False,
    ).stdout.strip()
    fail(
        "forgejo-runtime-readiness-timeout",
        f"Forgejo workload {FORGEJO_NAMESPACE}/{workload} did not become ready "
        f"within {timeout}s.\n{diagnostics}",
    )


def main() -> int:
    try:
        timeout = int(sys.argv[1]) if len(sys.argv) == 2 else int(
            os.environ.get("FORGEJO_RUNTIME_REPAIR_TIMEOUT", "600")
        )
        if timeout < 1:
            raise ValueError
    except (IndexError, ValueError):
        print("usage: repair_forgejo_runtime.py [TIMEOUT_SECONDS]", file=sys.stderr)
        return 2

    if not Path(KUBECTL).is_file() or not os.access(KUBECTL, os.X_OK) or not Path(KUBECONFIG).is_file():
        print("result=fail reason=forgejo-runtime-kubectl-unavailable")
        return 1
    if not shutil.which("openssl"):
        print("result=fail reason=forgejo-runtime-openssl-unavailable")
        return 1

    try:
        workload = ""
        document: dict[str, Any] | None = None
        for kind in ("deployment", "statefulset"):
            candidate = resource_json(f"{kind}/forgejo", namespace=FORGEJO_NAMESPACE)
            if candidate:
                workload = f"{kind}/forgejo"
                document = candidate
                break
        if not workload or not document:
            fail(
                "forgejo-runtime-workload-missing",
                f"No Forgejo Deployment or StatefulSet exists in {FORGEJO_NAMESPACE}.",
            )

        validate_storage_contract(document)
        database_type = database_backend(document)
        print(f"forgejo_database_backend={database_type}")

        bundle_changed = False
        mount_changed = False
        postgres_ca = "skipped"
        if database_type == "postgres":
            with tempfile.TemporaryDirectory(prefix="platform-forgejo-runtime-") as temporary:
                temp_dir = Path(temporary)
                cluster = postgres_cluster()
                active_tls_secret, leaf_path = active_postgres_certificate(cluster, temp_dir)
                bundle_changed = refresh_forgejo_bundle(
                    cluster, active_tls_secret, leaf_path, temp_dir
                )
                mount_changed = patch_mount_contract(workload, document)
                postgres_ca = f"{POSTGRES_NAMESPACE}/{active_tls_secret}"
        else:
            print(
                "forgejo_postgres_runtime=skipped "
                f"database_type={database_type} reason=non-postgres-backend"
            )

        restart_needed = bundle_changed or mount_changed or ready_pods() == 0
        if restart_needed:
            restarted = kube(
                "rollout",
                "restart",
                workload,
                namespace=FORGEJO_NAMESPACE,
                check=False,
            )
            if restarted.returncode != 0:
                fail(
                    "forgejo-runtime-restart-failed",
                    f"Could not restart {FORGEJO_NAMESPACE}/{workload}.",
                )
            print(f"forgejo_runtime=restart-requested workload={workload}")
        wait_for_runtime(workload, timeout)

        print(f"result=ok workload={workload} postgres_ca={postgres_ca}")
        return 0
    except RepairError:
        return 1
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"result=fail reason=forgejo-runtime-unexpected-error detail={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
