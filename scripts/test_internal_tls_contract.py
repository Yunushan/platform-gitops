#!/usr/bin/env python3
"""Validate managed internal trust and encrypted OpenBao/PostgreSQL/Valkey paths."""

from __future__ import annotations

import contextlib
import functools
import http.server
import io
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import repair_forgejo_runtime as forgejo_runtime

from forgejo_database_contract import (
    FORGEJO_NON_POSTGRES_DATABASE_TYPES,
    effective_forgejo_database_type,
)
from repair_forgejo_runtime import (
    MOUNT_PATHS,
    POSTGRES_CA_BUNDLE_PATH,
    POSTGRES_SERVER_CERTIFICATE_SECRET,
    mount_contract_ready,
    stale_init_application_mount_patch,
    tls_env_contract_ready,
)
from reconcile_forgejo_tls_routes import build_patch


ROOT = Path(__file__).resolve().parents[1]
PREMIUM = ROOT / "gitops/clusters/rke2-main/premium-3node/apps"
MAKEFILE = ROOT / "Makefile"
PRODUCTION_CHECK = ROOT / "scripts/bootstrap/run-platform-production-check.sh"
VERIFY_PLAYBOOK = ROOT / "ansible/playbooks/verify-platform-internal-tls.yml"
SECRET_PLAYBOOK = ROOT / "ansible/playbooks/configure-platform-app-secrets.yml"
PUBLIC_TLS_PLAYBOOK = ROOT / "ansible/playbooks/manage-platform-tls.yml"
PUBLIC_TLS_VERIFY_PLAYBOOK = ROOT / "ansible/playbooks/verify-platform-tls.yml"
WOODPECKER_REPAIR_PLAYBOOK = ROOT / "ansible/playbooks/repair-woodpecker.yml"
TLS_CHAIN_HELPER = ROOT / "scripts/complete_tls_chain.sh"
WOODPECKER_TLS_REPAIR_HELPER = ROOT / "scripts/repair_woodpecker_oauth_tls.sh"
FORGEJO_RUNTIME_REPAIR_PLAYBOOK = ROOT / "ansible/playbooks/repair-forgejo-runtime.yml"
FORGEJO_RUNTIME_REPAIR_HELPER = ROOT / "scripts/repair_forgejo_runtime.py"
PKI_DOC = ROOT / "docs/INTERNAL_PKI.md"
PRODUCTION_READINESS = ROOT / "docs/PRODUCTION_READINESS.md"


def read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"required internal TLS file is missing: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} is missing {needle!r}")


def forbid(text: str, needle: str, label: str) -> None:
    if needle in text:
        raise AssertionError(f"{label} must not contain {needle!r}")


def forgejo_postgres_tls_required(text: str) -> bool:
    """Keep the public contract strict while accepting rendered non-PostgreSQL profiles."""
    if "Forgejo" not in text or "rendered by scripts/render_private_platform_values.py" not in text:
        return True
    return effective_forgejo_database_type(text) not in FORGEJO_NON_POSTGRES_DATABASE_TYPES


def test_forgejo_runtime_storage_preflight() -> None:
    import copy
    from forgejo_storage_contract import valid_minio_endpoint

    for endpoint in ("objects.example.test", "objects.example.test:9000", "127.0.0.1:9000", "[::1]:9000"):
        assert valid_minio_endpoint(endpoint)
    for endpoint in ("", "https://objects.example.test", "user:password@host", "host/path", "<ENDPOINT>", "host:0", "host:99999", "white space", "bad_host"):
        assert not valid_minio_endpoint(endpoint)
    inline = {
        "attachment": b"STORAGE_TYPE=minio\n",
        "storage": b"MINIO_ENDPOINT=objects.example.test\nMINIO_BUCKET=forgejo\n",
    }
    credential_env = [{"name": "FORGEJO__STORAGE_0X2E_MINIO__" + key, "valueFrom": {"secretKeyRef": {"name": "custom-s3", "key": secret_key}}}
                      for key, secret_key in (("MINIO_ACCESS_KEY_ID", "access-key-id"), ("MINIO_SECRET_ACCESS_KEY", "secret-access-key"))]
    workload = {"spec": {"template": {"spec": {"initContainers": [{"name": "init-app-ini", "env": credential_env}]}}}}
    def data(namespace, name):
        return inline if name == "forgejo-inline-config" else {"access-key-id": b"test-key", "secret-access-key": b"never-log-test-secret"}
    output = io.StringIO()
    with mock.patch.object(forgejo_runtime, "secret_data", side_effect=data), mock.patch.object(forgejo_runtime, "REQUESTED_STORAGE_MODE", ""), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        # Reproduces the user's crash: global endpoint + explicit attachment
        # minio is not sufficient without a named storage.minio section.
        try:
            forgejo_runtime.validate_storage_contract(workload)
        except forgejo_runtime.RepairError as exc:
            assert str(exc) == "forgejo-object-storage-config-invalid"
        else:
            raise AssertionError("broken attachment inheritance passed runtime preflight")
        inline["storage.minio"] = b"STORAGE_TYPE=minio\nMINIO_ENDPOINT=objects.example.test\nMINIO_BUCKET=forgejo\n"
        assert forgejo_runtime.validate_storage_contract(workload) == "minio"
        with mock.patch.object(forgejo_runtime, "REQUESTED_STORAGE_MODE", "filesystem"):
            try:
                forgejo_runtime.validate_storage_contract(workload)
            except forgejo_runtime.RepairError as exc:
                assert str(exc) == "forgejo-object-storage-mode-not-applied"
            else:
                raise AssertionError("explicit filesystem request ignored")
        local_workload = copy.deepcopy(workload)
        local_workload["spec"]["template"]["spec"]["initContainers"][0]["env"] = []
        inline["attachment"] = b"STORAGE_TYPE=local\n"
        # Leftover MinIO keys/credentials alone do not select that backend.
        assert forgejo_runtime.validate_storage_contract(local_workload) == "filesystem"
        inline["attachment"] = b"STORAGE_TYPE=minio\n"
        try:
            forgejo_runtime.validate_storage_contract(local_workload)
        except forgejo_runtime.RepairError as exc:
            assert str(exc) == "forgejo-object-storage-secret-missing"
        else:
            raise AssertionError("unbound S3 credentials accepted")
        inline["attachment"] = b"STORAGE_TYPE=minio\nSTORAGE_TYPE=local\n"
        try:
            forgejo_runtime.validate_storage_contract(workload)
        except forgejo_runtime.RepairError as exc:
            assert str(exc) == "forgejo-storage-config-unknown"
        else:
            raise AssertionError("ambiguous INI accepted")
    assert "never-log-test-secret" not in output.getvalue()
    playbook = read(FORGEJO_RUNTIME_REPAIR_PLAYBOOK)
    require(playbook, "- forgejo_storage_contract.py", "runtime storage bundle")
    require(playbook, "lookup('ansible.builtin.env', 'FORGEJO_OBJECT_STORAGE_MODE')", "remote storage mode")


def test_forgejo_runtime_mount_contract_scope() -> None:
    def workload(
        init_paths: tuple[str, ...],
        *,
        ca_path: str = POSTGRES_CA_BUNDLE_PATH,
    ) -> dict[str, object]:
        return {
            "metadata": {"resourceVersion": "17"},
            "spec": {
                "template": {
                    "spec": {
                        "volumes": [{
                            "name": "platform-postgres-ca",
                            "configMap": {
                                "name": "platform-internal-roots",
                                "items": [
                                    {
                                        "key": "ca-certificates.crt",
                                        "path": path,
                                    }
                                    for path in ("ca-certificates.crt", "root.crt")
                                ],
                            },
                        }],
                        "containers": [{
                            "name": "forgejo",
                            "env": [{
                                "name": "SSL_CERT_FILE",
                                "value": ca_path,
                            }],
                            "volumeMounts": [
                                {
                                    "name": "platform-postgres-ca",
                                    "mountPath": path,
                                }
                                for path in MOUNT_PATHS
                            ],
                        }],
                        "initContainers": [{
                            "name": "configure-gitea",
                            "env": [{
                                "name": "SSL_CERT_FILE",
                                "value": ca_path,
                            }],
                            "volumeMounts": [
                                {
                                    "name": "platform-postgres-ca",
                                    "mountPath": path,
                                }
                                for path in init_paths
                            ],
                        }],
                    }
                }
            }
        }

    if not mount_contract_ready(workload((MOUNT_PATHS[0],))):
        raise AssertionError(
            "a PostgreSQL root.crt mount on the init container must satisfy the contract"
        )
    if mount_contract_ready(workload(MOUNT_PATHS)):
        raise AssertionError(
            "the application trust-directory mount must not be accepted on init containers"
        )
    if mount_contract_ready(workload(())):
        raise AssertionError("an init container without PostgreSQL trust must fail closed")
    if not tls_env_contract_ready(workload((MOUNT_PATHS[0],))):
        raise AssertionError(
            "all Forgejo containers must use the shared PostgreSQL CA bundle via SSL_CERT_FILE"
        )
    if tls_env_contract_ready(
        workload(
            (MOUNT_PATHS[0],),
            ca_path="/etc/ssl/platform/ca-certificates.crt",
        )
    ):
        raise AssertionError("a stale SSL_CERT_FILE path must fail closed")

    guarded_patch = stale_init_application_mount_patch(workload(MOUNT_PATHS))
    if len(guarded_patch) != 2:
        raise AssertionError("stale init mount cleanup must emit one guard and one remove")
    if guarded_patch[0] != {
        "op": "test",
        "path": "/metadata/resourceVersion",
        "value": "17",
    }:
        raise AssertionError("stale init mount cleanup must guard the resourceVersion")
    if guarded_patch[1].get("op") != "remove" or not guarded_patch[1].get(
        "path", ""
    ).endswith("/volumeMounts/1"):
        raise AssertionError("stale init mount cleanup targeted the wrong mount index")


def run_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed ({' '.join(command)}):\n{result.stdout}\n{result.stderr}"
        )
    return result


class QuietRequestHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args


def test_forgejo_postgres_tls_probe() -> None:
    openssl = shutil.which("openssl")
    if not openssl:
        raise AssertionError("OpenSSL is required for PostgreSQL TLS probe tests")
    with tempfile.TemporaryDirectory(prefix="platform-postgres-probe-test-") as temporary:
        directory = Path(temporary)
        for name in ("trusted", "unrelated"):
            run_command(
                [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-days", "2", "-subj", f"/CN={name}",
                 "-addext", "basicConstraints=critical,CA:TRUE",
                 "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                 "-addext", "subjectKeyIdentifier=hash",
                 "-keyout", str(directory / f"{name}.key"),
                 "-out", str(directory / f"{name}.crt")],
                cwd=directory,
            )
        run_command(
            [openssl, "req", "-new", "-newkey", "rsa:2048", "-nodes",
             "-subj", f"/CN={forgejo_runtime.POSTGRES_HOST}",
             "-keyout", str(directory / "server.key"),
             "-out", str(directory / "server.csr")],
            cwd=directory,
        )
        (directory / "server.ext").write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectKeyIdentifier=hash\n"
            "authorityKeyIdentifier=keyid,issuer\n"
            f"subjectAltName=DNS:{forgejo_runtime.POSTGRES_HOST}\n",
            encoding="utf-8",
        )
        run_command(
            [openssl, "x509", "-req", "-in", str(directory / "server.csr"),
             "-CA", str(directory / "trusted.crt"),
             "-CAkey", str(directory / "trusted.key"), "-CAcreateserial",
             "-days", "2", "-extfile", str(directory / "server.ext"),
             "-out", str(directory / "server.crt")],
            cwd=directory,
        )
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(directory / "server.crt", directory / "server.key")
        real_runner = forgejo_runtime.run_bounded

        @contextlib.contextmanager
        def postgres_server(mode: str = "tls"):
            stop = threading.Event()
            failures: list[BaseException] = []
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                listener.settimeout(3)
                port = listener.getsockname()[1]

                def serve() -> None:
                    try:
                        with listener.accept()[0] as connection:
                            connection.settimeout(3)
                            request = b""
                            while len(request) < 8:
                                chunk = connection.recv(8 - len(request))
                                if not chunk:
                                    raise AssertionError("probe closed before SSLRequest")
                                request += chunk
                            if request != struct.pack("!II", 8, 80877103):
                                raise AssertionError("probe did not use PostgreSQL SSLRequest")
                            if mode == "request-stall":
                                stop.wait(3)
                                return
                            if mode in {"refuse", "error"}:
                                connection.sendall(
                                    b"N" if mode == "refuse" else b"Euntrusted-server-text"
                                )
                                return
                            connection.sendall(b"S")
                            if mode == "handshake-stall":
                                stop.wait(3)
                                return
                            try:
                                with server_context.wrap_socket(connection, server_side=True) as channel:
                                    # Keep the server open until the probe closes. No
                                    # application reply or server shutdown is needed.
                                    if channel.recv(1) != b"":
                                        raise AssertionError("TLS probe sent application data")
                            except (ssl.SSLError, ConnectionError):
                                # Certificate rejection or a completed client-side
                                # probe may close before the server sends TLS tickets.
                                pass
                    except BaseException as exc:
                        failures.append(exc)

                worker = threading.Thread(target=serve, daemon=True)
                worker.start()

                def forward(args, **kwargs):
                    expected = [
                        forgejo_runtime.KUBECTL, "--kubeconfig", forgejo_runtime.KUBECONFIG,
                        "-n", forgejo_runtime.POSTGRES_NAMESPACE, "port-forward",
                        "--address=127.0.0.1", "service/platform-postgres-rw", ":5432",
                    ]
                    if args != expected:
                        raise AssertionError("probe did not select a loopback-only primary Service tunnel")
                    # A real long-lived child exercises readiness output, early
                    # termination, and cleanup, while the local fixture supplies TLS.
                    return real_runner(
                        [sys.executable, "-c", "import time; "
                         f"print('Forwarding from 127.0.0.1:{port} -> 5432', flush=True); "
                         "time.sleep(30)"],
                        **kwargs,
                    )

                try:
                    with mock.patch.object(
                        forgejo_runtime, "run_bounded", side_effect=forward
                    ):
                        yield
                finally:
                    stop.set()
                    worker.join(timeout=4)
                    if worker.is_alive():
                        raise AssertionError("PostgreSQL test server failed to stop")
                    if failures:
                        raise failures[0]

        trusted = directory / "trusted.crt"
        output = io.StringIO()
        with postgres_server(), contextlib.redirect_stdout(output):
            started = time.monotonic()
            if not forgejo_runtime.postgres_server_handshake_verifies(trusted):
                raise AssertionError(
                    "valid PostgreSQL certificate failed verification: " + output.getvalue()
                )
            if time.monotonic() - started >= 2:
                raise AssertionError("probe waited for PostgreSQL to close a verified connection")
        for ca_path, hostname in (
            (directory / "unrelated.crt", forgejo_runtime.POSTGRES_HOST),
            (trusted, "wrong-postgres.example.test"),
        ):
            output = io.StringIO()
            with postgres_server(), mock.patch.object(
                forgejo_runtime, "POSTGRES_HOST", hostname
            ), contextlib.redirect_stdout(output):
                if forgejo_runtime.postgres_server_handshake_verifies(ca_path):
                    raise AssertionError("untrusted or wrong-host certificate was accepted")
            if "reason=certificate-verification-failed" not in output.getvalue():
                raise AssertionError("certificate failure lost its classification")
        for mode, expected_phase in (
            ("refuse", "ssl-request"), ("error", "ssl-request"),
            ("request-stall", "ssl-request"), ("handshake-stall", "tls-handshake"),
        ):
            output = io.StringIO()
            with postgres_server(mode), mock.patch.object(
                forgejo_runtime, "POSTGRES_TLS_PROBE_TIMEOUT_SECONDS", 0.5
            ), contextlib.redirect_stdout(output):
                started = time.monotonic()
                if forgejo_runtime.postgres_server_handshake_verifies(trusted):
                    raise AssertionError("stalled or unencrypted PostgreSQL was accepted")
                if time.monotonic() - started >= 2:
                    raise AssertionError("probe exceeded its bounded attempt timeout")
            if f"phase={expected_phase}" not in output.getvalue():
                raise AssertionError("probe failure did not identify the stalled phase")
            if "untrusted-server-text" in output.getvalue():
                raise AssertionError("probe exposed unauthenticated server text")

        def announced_forward(args, **kwargs):
            del args
            kwargs["stdout_callback"](b"Forwarding from 127.0.0.1:34567 -> 5432\n")
            return subprocess.CompletedProcess([], -9, b"", b"")

        for phase in ("tcp-connect", "port-forward"):
            output = io.StringIO()
            forward = (
                subprocess.TimeoutExpired(["kubectl"], 0.5)
                if phase == "port-forward" else announced_forward
            )
            with mock.patch.object(
                forgejo_runtime, "run_bounded", side_effect=forward,
            ), mock.patch.object(socket, "create_connection", side_effect=TimeoutError), \
                    contextlib.redirect_stdout(output):
                if forgejo_runtime.postgres_server_handshake_verifies(trusted):
                    raise AssertionError("a timed-out probe was accepted")
            if f"phase={phase} reason=timeout" not in output.getvalue():
                raise AssertionError("transient timeout escaped the retry path")

        with mock.patch.object(
            forgejo_runtime, "run_bounded", side_effect=announced_forward
        ), mock.patch.object(socket, "create_connection", side_effect=ConnectionRefusedError), \
                contextlib.redirect_stdout(io.StringIO()):
            if forgejo_runtime.postgres_server_handshake_verifies(trusted):
                raise AssertionError("connection refusal was treated as a verified handshake")


def test_forgejo_postgres_probe_retry_deadline() -> None:
    expected = forgejo_runtime.POSTGRES_SERVER_CERTIFICATE_SECRET
    cluster = {"spec": {"certificates": {
        "serverCASecret": expected, "serverTLSSecret": expected,
    }}}
    ca_path = Path("probe-ca.crt")
    for recover in (True, False):
        clock = [0.0]
        attempts: list[float] = []

        def sleep(seconds: float) -> None:
            clock[0] += seconds

        def probe(path: Path, *, deadline: float) -> bool:
            if path != ca_path:
                raise AssertionError("probe changed the configured trust root")
            attempts.append(clock[0])
            clock[0] += min(10, deadline - clock[0])
            return recover and len(attempts) == 2

        output = io.StringIO()
        with mock.patch.object(forgejo_runtime, "validate_postgres_server_certificate_secret",
                               return_value=(ca_path, Path("leaf.crt"))), \
                mock.patch.object(forgejo_runtime, "postgres_cluster", return_value=cluster), \
                mock.patch.object(forgejo_runtime, "postgres_server_handshake_verifies", side_effect=probe), \
                mock.patch.object(forgejo_runtime, "postgres_runtime_diagnostics", return_value="probe-diagnostics"), \
                mock.patch.object(forgejo_runtime, "POSTGRES_CERTIFICATE_REPAIR_TIMEOUT_SECONDS", 23), \
                mock.patch.object(forgejo_runtime.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(forgejo_runtime.time, "sleep", side_effect=sleep), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                result = forgejo_runtime.reconcile_postgres_certificate_contract(cluster, Path("."))
            except forgejo_runtime.RepairError as exc:
                if recover or str(exc) != "forgejo-postgres-certificate-contract-timeout":
                    raise
            else:
                if not recover or result != (cluster, expected):
                    raise AssertionError("repair falsely succeeded or failed to recover")
        if attempts != [0.0, 15.0] or clock[0] != 23:
            raise AssertionError("retry loop exceeded its remaining deadline")
        if not recover and "probe-diagnostics" not in output.getvalue():
            raise AssertionError("exhausted retry did not include PostgreSQL diagnostics")

    with mock.patch.object(forgejo_runtime, "run_bounded") as runner:
        runner.return_value = subprocess.CompletedProcess([], 0, '{"spec":{}}', "")
        forgejo_runtime.postgres_cluster(timeout_seconds=0.25)
        if runner.call_args.kwargs["timeout"] > 0.25:
            raise AssertionError("CNPG lookup ignored the retry loop's remaining budget")

    with mock.patch.object(forgejo_runtime, "validate_postgres_server_certificate_secret",
                           return_value=(ca_path, Path("leaf.crt"))), \
            mock.patch.object(forgejo_runtime, "postgres_cluster", side_effect=[
                subprocess.TimeoutExpired(["kubectl"], 10), cluster,
            ]), \
            mock.patch.object(forgejo_runtime, "postgres_server_handshake_verifies", return_value=True), \
            mock.patch.object(forgejo_runtime.time, "sleep"), \
            contextlib.redirect_stdout(io.StringIO()):
        if forgejo_runtime.reconcile_postgres_certificate_contract(cluster, Path(".")) != (cluster, expected):
            raise AssertionError("transient CNPG discovery timeout prevented recovery")


def test_forgejo_postgres_tunnel_failures() -> None:
    for chunks, expected in (
        ([], "port-forward-failed"),
        ([b"Forwarding from 0.0.0.0:5432 -> 5432\n"], "port-forward-failed"),
        ([b"Forwarding from 127.0.0.1:99999 -> 5432\n"], "port-forward-output-invalid"),
        ([b"x" * 4097], "port-forward-output-invalid"),
    ):
        def run(args, **kwargs):
            del args
            for chunk in chunks:
                if kwargs["stdout_callback"](chunk):
                    break
            return subprocess.CompletedProcess([], 1, b"", b"unauthenticated-error-text")

        output = io.StringIO()
        with mock.patch.object(forgejo_runtime, "run_bounded", side_effect=run), \
                mock.patch.object(socket, "create_connection") as connect, \
                contextlib.redirect_stdout(output):
            if forgejo_runtime.postgres_server_handshake_verifies(Path("unused")):
                raise AssertionError("missing or invalid tunnel readiness was accepted")
        if connect.called or expected not in output.getvalue():
            raise AssertionError("tunnel failure was not isolated from the TLS probe")
        if "unauthenticated-error-text" in output.getvalue():
            raise AssertionError("raw tunnel stderr leaked into diagnostics")

    def fragmented(args, **kwargs):
        del args
        if kwargs["stdout_callback"](b"Forwarding from 127.0."):
            raise AssertionError("partial tunnel announcement was accepted")
        if not kwargs["stdout_callback"](b"0.1:34567 -> 5432\n"):
            raise AssertionError("complete fragmented announcement was ignored")
        return subprocess.CompletedProcess([], -9, b"", b"")

    output = io.StringIO()
    with mock.patch.object(forgejo_runtime, "run_bounded", side_effect=fragmented), \
            mock.patch.object(ssl, "create_default_context", side_effect=OSError), \
            contextlib.redirect_stdout(output):
        if forgejo_runtime.postgres_server_handshake_verifies(Path("unused")):
            raise AssertionError("CA-load failure was accepted")
    if "address=127.0.0.1:34567 phase=ca-load" not in output.getvalue():
        raise AssertionError("split readiness output did not select the announced port")


def test_forgejo_runtime_still_requires_application_readiness() -> None:
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(sys, "argv", ["repair_forgejo_runtime.py", "1"]))
        stack.enter_context(mock.patch.object(Path, "is_file", return_value=True))
        stack.enter_context(mock.patch.object(os, "access", return_value=True))
        stack.enter_context(mock.patch.object(shutil, "which", return_value="openssl"))
        for name, value in (
            ("resource_json", {"kind": "Deployment"}),
            ("repair_config_environment", {"kind": "Deployment"}),
            ("repair_shared_valkey", None),
            ("validate_storage_contract", "filesystem"), ("database_backend", "postgres"),
            ("postgres_cluster", {}), ("reconcile_postgres_certificate_contract", ({}, "tls")),
            ("active_postgres_certificate", ("tls", Path("leaf"))),
            ("refresh_forgejo_bundle", False), ("patch_mount_contract", False),
            ("ready_pods", 1),
        ):
            stack.enter_context(mock.patch.object(forgejo_runtime, name, return_value=value))
        readiness = stack.enter_context(mock.patch.object(
            forgejo_runtime, "wait_for_runtime",
            side_effect=forgejo_runtime.RepairError("application-network-not-ready"),
        ))
        output = io.StringIO()
        stack.enter_context(contextlib.redirect_stdout(output))
        if forgejo_runtime.main() != 1:
            raise AssertionError("verified server certificate bypassed application readiness")
        readiness.assert_called_once_with("deployment/forgejo", 1)
        if "result=ok" in output.getvalue():
            raise AssertionError("failed application network was reported as repaired")


def test_forgejo_config_environment_runtime() -> None:
    import copy
    import json
    from forgejo_config_env import normalize_config_env

    env = [{"name": "GITEA__cache__HOST", "valueFrom": {"secretKeyRef": {"name": "private-redis", "key": "uri"}}},
           {"name": "GITEA_APP_INI", "value": "/data/gitea/conf/app.ini"}]
    original = {"metadata": {"resourceVersion": "17"}, "spec": {"template": {"spec": {
        "initContainers": [{"name": "init-app-ini", "env": env}],
        "containers": [{"name": "forgejo", "env": env}],
        "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "keep-data"}}],
    }}}}
    unchanged = copy.deepcopy(original)
    patched = copy.deepcopy(original)
    for group in ("initContainers", "containers"):
        patched["spec"]["template"]["spec"][group][0]["env"] = normalize_config_env(env)
    output = io.StringIO()
    with mock.patch.object(forgejo_runtime, "kube", return_value=subprocess.CompletedProcess([], 0, json.dumps(patched), "")) as kube, contextlib.redirect_stdout(output):
        assert forgejo_runtime.repair_config_environment("deployment/forgejo", original) == patched
        operations = json.loads(kube.call_args.kwargs["input_text"])
        assert operations[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "17"}
        assert len(operations) == 3
        assert all(item["path"].endswith("/0/env") for item in operations[1:])
        assert "--patch-file=/dev/stdin" in kube.call_args.args
        assert "private-redis" not in str(kube.call_args.args)
        kube.reset_mock()
        assert forgejo_runtime.repair_config_environment("deployment/forgejo", patched) == patched
        kube.assert_not_called()
    assert original == unchanged
    for document, reason in (
        (original, "forgejo-config-env-patch-failed"),
        ({**original, "metadata": {}}, "forgejo-config-env-version-missing"),
    ):
        with mock.patch.object(forgejo_runtime, "kube", return_value=subprocess.CompletedProcess([], 1, "", "never-log-private-data")), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                forgejo_runtime.repair_config_environment("deployment/forgejo", document)
            except forgejo_runtime.RepairError as exc:
                assert str(exc) == reason
            else:
                raise AssertionError("unguarded or rejected patch was accepted")
    assert "never-log-private-data" not in output.getvalue()
    require(read(FORGEJO_RUNTIME_REPAIR_PLAYBOOK), "- forgejo_config_env.py", "runtime config env bundle")


def test_valkey_runtime_contract() -> None:
    import copy
    import json
    from valkey_runtime_contract import ready_primary_endpoints, storage_policy_patch

    app = {"metadata": {"resourceVersion": "12"}, "spec": {
        "source": {"path": "gitops/clusters/rke2-main/premium-3node/apps/platform-valkey", "targetRevision": "main"},
        "destination": {"namespace": "platform-cache"},
        "ignoreDifferences": [{"group": "", "kind": "Namespace", "jsonPointers": ["/status"]}],
        "syncPolicy": {"automated": {"prune": False, "selfHeal": True}, "syncOptions": ["CreateNamespace=true"]},
    }}
    original = copy.deepcopy(app)
    patch = storage_policy_patch(app)
    assert app == original
    assert patch["metadata"] == {"resourceVersion": "12"}
    assert set(patch["spec"]) == {"ignoreDifferences", "syncPolicy"}
    assert set(patch["spec"]["syncPolicy"]) == {"syncOptions"}
    assert patch["spec"]["ignoreDifferences"][0] == app["spec"]["ignoreDifferences"][0]
    assert patch["spec"]["ignoreDifferences"][1]["jqPathExpressions"] == [".spec.volumeClaimTemplates[]?.spec.storageClassName"]
    assert patch["spec"]["ignoreDifferences"][2]["jsonPointers"] == ["/spec/storageClassName"]
    preserved = copy.deepcopy(app)
    preserved["metadata"]["resourceVersion"] = "13"
    preserved["spec"]["ignoreDifferences"] = patch["spec"]["ignoreDifferences"]
    preserved["spec"]["syncPolicy"].update(patch["spec"]["syncPolicy"])
    assert storage_policy_patch(preserved) == {}
    for alteration in ("namespace", "path", "version", "disabled", "replace", "force"):
        invalid = copy.deepcopy(app)
        if alteration == "namespace":
            invalid["spec"]["destination"]["namespace"] = "custom"
        elif alteration == "path":
            invalid["spec"]["source"]["path"] = "custom"
        elif alteration == "version":
            invalid["metadata"] = {}
        elif alteration == "disabled":
            invalid["spec"]["syncPolicy"]["syncOptions"].append("RespectIgnoreDifferences=false")
        else:
            invalid["spec"]["syncPolicy"]["syncOptions"].append(alteration.title() + "=true")
        try:
            storage_policy_patch(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("custom or unguarded Valkey Application was accepted")

    ready = {"items": [{"metadata": {"labels": {"kubernetes.io/service-name": "platform-valkey-primary"}},
                        "ports": [{"port": 6380, "protocol": "TCP"}],
                        "endpoints": [{"addresses": ["192.0.2.10"], "conditions": {"ready": True}}]}]}
    assert ready_primary_endpoints(ready) == 1
    for ports in (None, [], [{"port": None}], [{"port": 6379}], [{"port": 6380, "protocol": "UDP"}]):
        assert ready_primary_endpoints({"items": [{**ready["items"][0], "ports": ports}]}) == 0
    for conditions in ({}, {"ready": False}, {"ready": True, "terminating": True}):
        invalid = copy.deepcopy(ready)
        invalid["items"][0]["endpoints"][0]["conditions"] = conditions
        assert ready_primary_endpoints(invalid) == 0

    uri = "rediss://:private-test-value@platform-valkey-primary.platform-cache.svc.cluster.local:6379/0"
    workload = {"spec": {"template": {"spec": {"initContainers": [{"name": "init-app-ini", "env": [
        {"name": "FORGEJO__cache__HOST", "valueFrom": {"secretKeyRef": {"name": "forgejo-redis", "key": "uri"}}},
    ]}]}}}}
    with mock.patch.object(forgejo_runtime, "secret_data", return_value={"uri": uri.encode()}):
        assert forgejo_runtime.uses_shared_valkey(workload)
    with mock.patch.object(forgejo_runtime, "secret_data", return_value={"uri": b"rediss://external.test:6379/0"}):
        assert not forgejo_runtime.uses_shared_valkey(workload)
    output = io.StringIO()
    for secret, reason in (({}, "forgejo-redis-secret-unavailable"), ({"uri": b"rediss://[invalid"}, "forgejo-redis-uri-invalid")):
        with mock.patch.object(forgejo_runtime, "secret_data", return_value=secret), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                forgejo_runtime.uses_shared_valkey(workload)
            except forgejo_runtime.RepairError as exc:
                assert str(exc) == reason
            else:
                raise AssertionError("missing or malformed Redis binding was ignored")

    service = {"spec": {"ports": [{"port": 6379, "targetPort": "primary-proxy"}], "selector": {
        "app.kubernetes.io/instance": "platform-valkey", "app.kubernetes.io/name": "valkey",
    }}}
    for mode in ("repair", "conflict", "active", "active-failed", "active-error", "storage-blocked", "api-failure", "custom-service", "sync-failed", "timeout", "conflicts-exhausted", "already-ready", "external"):
        reads, patches = [], []
        clock = [0]
        applications = [copy.deepcopy(app)]
        if mode.startswith("active"):
            applications[0] = copy.deepcopy(preserved)
            applications[0]["operation"] = {"sync": {"prune": True}}
            applications[0]["status"] = {"operationState": {"startedAt": "ongoing", "phase": "Running"}}

        def resource(resource, **kwargs):
            reads.append(resource)
            if resource == "endpointslices.discovery.k8s.io":
                if mode == "api-failure":
                    return None
                if mode == "already-ready" or (clock[0] >= 5 and mode in {"repair", "conflict", "active"}):
                    return ready
                return {"items": []}
            if resource == "service/platform-valkey-primary":
                return {} if mode == "custom-service" else service
            if resource == "application/platform-valkey":
                if mode in {"active-failed", "active-error"} and clock[0] >= 5:
                    completed = copy.deepcopy(applications[0])
                    completed.pop("operation")
                    completed["status"]["operationState"].update({"phase": "Failed" if mode == "active-failed" else "Error", "message": "retained sync failed"})
                    return completed
                if mode == "sync-failed" and clock[0] >= 5:
                    return {**applications[0], "status": {"operationState": {
                        "phase": "Failed", "startedAt": "new", "message": "sync failed"}}}
                return applications[0]
            raise AssertionError(resource)

        def kube(*args, **kwargs):
            if args[0] == "get":
                return subprocess.CompletedProcess([], 0, "Valkey diagnostics", "")
            assert args[:2] == ("patch", "application/platform-valkey")
            assert kwargs["namespace"] == "argocd"
            assert "--patch-file=/dev/stdin" in args
            patch = json.loads(kwargs["input_text"])
            patches.append(patch)
            assert patch["metadata"]["resourceVersion"] == applications[0]["metadata"]["resourceVersion"]
            if mode == "conflicts-exhausted" or (mode == "conflict" and len(patches) == 1):
                applications[0]["metadata"]["resourceVersion"] = "18"
                return subprocess.CompletedProcess([], 1, "", "never-log-private-data")
            if "spec" in patch:
                applications[0] = copy.deepcopy(preserved)
            else:
                assert patch["operation"]["sync"]["prune"] is False
                assert "RespectIgnoreDifferences=true" in patch["operation"]["sync"]["syncOptions"]
            return subprocess.CompletedProcess([], 0, "patched", "")

        def sleep(delay):
            clock[0] += delay

        expected = {
            "storage-blocked": "storage-blocked", "api-failure": "forgejo-valkey-endpoints-unavailable",
            "custom-service": "forgejo-valkey-service-contract", "sync-failed": "forgejo-valkey-sync-failed",
            "timeout": "forgejo-valkey-endpoint-timeout", "conflicts-exhausted": "forgejo-valkey-sync-request-failed",
            "active-failed": "forgejo-valkey-sync-failed", "active-error": "forgejo-valkey-sync-failed",
        }.get(mode)
        with mock.patch.object(forgejo_runtime, "uses_shared_valkey", return_value=mode != "external"), \
                mock.patch.object(forgejo_runtime, "resource_json", side_effect=resource), \
                mock.patch.object(forgejo_runtime, "require_valkey_storage_ready", side_effect=forgejo_runtime.RepairError("storage-blocked") if mode == "storage-blocked" else None), \
                mock.patch.object(forgejo_runtime, "kube", side_effect=kube), \
                mock.patch.object(forgejo_runtime.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(forgejo_runtime.time, "sleep", side_effect=sleep), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                forgejo_runtime.repair_shared_valkey(workload, 12)
            except forgejo_runtime.RepairError as exc:
                assert str(exc) == expected, (mode, str(exc))
            else:
                assert expected is None, mode
        if mode in {"active", "active-failed", "active-error", "storage-blocked", "api-failure", "custom-service", "already-ready", "external"}:
            assert not patches, mode
        if mode in {"active-failed", "active-error"}:
            assert clock[0] == 5, "retained sync failure waited for the readiness timeout"
        if mode == "external":
            assert not reads
        if mode == "conflicts-exhausted":
            assert len(patches) == 5
    assert "private-test-value" not in output.getvalue()
    assert "never-log-private-data" not in output.getvalue()
    require(read(FORGEJO_RUNTIME_REPAIR_PLAYBOOK), "- valkey_runtime_contract.py", "runtime Valkey helper bundle")


def test_valkey_longhorn_storage_preflight() -> None:
    import copy

    documents = {
        "statefulset/platform-valkey": {"spec": {"replicas": 1, "volumeClaimTemplates": [{"metadata": {"name": "valkey-data"}}]}},
        "persistentvolumeclaims": {"items": [{"metadata": {"name": "valkey-data-platform-valkey-0"}, "spec": {"volumeName": "retained-pv"}}]},
        "persistentvolume/retained-pv": {"spec": {"csi": {"driver": "driver.longhorn.io", "volumeHandle": "retained-volume"}}},
        "volumes.longhorn.io/retained-volume": {"status": {"robustness": "healthy"}},
        "settings.longhorn.io/default-instance-manager-image": {"value": "longhorn-instance-manager:new"},
        "instancemanagers.longhorn.io": {"items": [
            {"metadata": {"name": "new-manager"}, "spec": {"image": "longhorn-instance-manager:new", "nodeID": "node-1", "dataEngine": "v1"}, "status": {"currentState": "running", "ip": "192.0.2.11"}},
            {"metadata": {"name": "old-manager"}, "spec": {"image": "longhorn-instance-manager:old"}, "status": {"currentState": "error"}},
        ]},
        "events": {"items": [{"involvedObject": {"name": "new-manager"}, "type": "Warning", "lastTimestamp": "later",
                              "message": "OutOfcpu requested: 960, used: 7820, capacity: 8000"}]},
    }
    for mode in ("healthy", "faulted", "manager-failed", "missing-manager", "api-error", "non-longhorn", "stateless"):
        data = copy.deepcopy(documents)
        if mode == "faulted":
            data["volumes.longhorn.io/retained-volume"]["status"]["robustness"] = "faulted"
        elif mode == "manager-failed":
            data["instancemanagers.longhorn.io"]["items"][0]["status"] = {"currentState": "error"}
        elif mode == "missing-manager":
            data["instancemanagers.longhorn.io"]["items"] = []
        elif mode == "api-error":
            data["persistentvolume/retained-pv"] = None
        elif mode == "non-longhorn":
            data["persistentvolume/retained-pv"]["spec"]["csi"]["driver"] = "custom.csi"
        elif mode == "stateless":
            data["statefulset/platform-valkey"]["spec"]["volumeClaimTemplates"] = []
        output = io.StringIO()
        with mock.patch.object(forgejo_runtime, "resource_json", side_effect=lambda name, **kwargs: data[name]), \
                mock.patch.object(forgejo_runtime, "kube") as kube, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                forgejo_runtime.require_valkey_storage_ready()
            except forgejo_runtime.RepairError as exc:
                assert mode not in {"healthy", "non-longhorn", "stateless"}
                assert str(exc) == ("forgejo-valkey-storage-unavailable" if mode == "api-error" else "forgejo-valkey-storage-blocked")
            else:
                assert mode in {"healthy", "non-longhorn", "stateless"}, mode
            kube.assert_not_called()
        if mode == "manager-failed":
            assert "OutOfcpu requested: 960" in output.getvalue()


def test_forgejo_chart_dependency_env_precedence() -> None:
    """Execute the pinned chart loader against its empty inline Redis defaults."""
    if os.name == "nt":
        print("Forgejo chart shell behavior test runs on Linux CI.")
        return
    from forgejo_config_env import normalize_config_env

    chart = PREMIUM / "forgejo/charts/forgejo-17.1.4/forgejo"
    script = read(chart / "scripts/config_environment.sh")
    with tempfile.TemporaryDirectory(prefix="forgejo-chart-env-") as temporary:
        root = Path(temporary)
        inline = root / "inlines"
        inline.mkdir()
        for section, line in (("cache", "HOST ="), ("queue", "CONN_STR ="), ("database", "PASSWD =")):
            source = root / section
            source.write_text(line + "\n", encoding="utf-8")
            (inline / section).symlink_to(source)
        script = script.replace("/tmp/existing-envs", str(root / "existing-envs"))
        script = script.replace("/env-to-ini-mounts/inlines/", str(inline))
        script = script.replace("/env-to-ini-mounts/additionals/", str(root / "absent"))
        path = root / "config_environment.sh"
        path.write_text(script, encoding="utf-8")
        harness = r'''
gitea() { printf 'test-generated-secret'; }
environment-to-ini() {
  printf 'effective-cache=%s\neffective-queue=%s\neffective-password=%s\n' "$FORGEJO__CACHE__HOST" "$FORGEJO__QUEUE__CONN_STR" "$FORGEJO__DATABASE__PASSWD"
}
source "$1"
'''
        legacy = [{"name": name, "value": value} for name, value in (
            ("GITEA__cache__HOST", "rediss://:test-password@valkey.example.test:6379/0"),
            ("GITEA__queue__CONN_STR", "rediss://:test-password@valkey.example.test:6379/0"),
            ("GITEA__database__PASSWD", "test-db-password"),
        )]
        base_env = {key: value for key, value in os.environ.items() if not key.startswith(("GITEA__", "FORGEJO__"))}
        for entries, fixed in ((legacy, False), (normalize_config_env(legacy), True)):
            env = dict(base_env, GITEA_APP_INI=str(root / "app.ini"))
            env.update({entry["name"]: entry["value"] for entry in entries})
            result = subprocess.run(["bash", "-c", harness, "bash", str(path)], env=env, text=True, capture_output=True, timeout=30, check=True)
            for label, binding in zip(("cache", "queue", "password"), legacy):
                expected = binding["value"] if fixed else ""
                assert f"effective-{label}={expected}\n" in result.stdout


def test_forgejo_route_reconciliation() -> None:
    target_host = "gitops.example.test"
    canonical_secret = "forgejo-tls"

    stale_ingress = {
        "spec": {
            "rules": [{"host": "forgejo.example.test", "http": {"paths": []}}],
            "tls": [{"hosts": ["forgejo.example.test"], "secretName": "custom-tls"}],
        }
    }
    ingress_patch = build_patch(stale_ingress, "Ingress", "forgejo", target_host, canonical_secret)
    if ingress_patch:
        raise AssertionError("stale Woodpecker host was allowed to rewrite the Forgejo Ingress")

    empty_fallback = {
        "spec": {
            "rules": [{"host": target_host}],
            "tls": [{"hosts": [target_host]}],
        }
    }
    fallback_patch = build_patch(
        empty_fallback,
        "Ingress",
        "platform-forgejo",
        target_host,
        canonical_secret,
    )
    fallback_tls = next(item for item in fallback_patch if item["path"] == "/spec/tls")
    if fallback_tls["value"][0]["secretName"] != canonical_secret:
        raise AssertionError("empty platform Ingress TLS binding did not select forgejo-tls")

    unrelated_ingress = {
        "spec": {
            "rules": [{"host": "unrelated.example.test"}],
            "tls": [],
        }
    }
    if build_patch(unrelated_ingress, "Ingress", "unrelated", target_host, canonical_secret):
        raise AssertionError("unrelated Ingress was modified by Forgejo route reconciliation")

    stale_route = {
        "spec": {
            "routes": [
                {
                    "match": "Host(`forgejo.example.test`) && PathPrefix(`/`)",
                    "kind": "Rule",
                }
            ],
            "tls": {},
        }
    }
    route_patch = build_patch(
        stale_route,
        "IngressRoute",
        "forgejo-http",
        target_host,
        canonical_secret,
    )
    if route_patch:
        raise AssertionError("stale Woodpecker host was allowed to rewrite the Forgejo IngressRoute")

    matching_route = {
        "spec": {
            "routes": [
                {
                    "match": f"Host(`{target_host}`) && PathPrefix(`/`)",
                    "kind": "Rule",
                }
            ],
            "tls": {},
        }
    }
    route_patch = build_patch(
        matching_route,
        "IngressRoute",
        "forgejo-http",
        target_host,
        canonical_secret,
    )
    route_tls = next(item for item in route_patch if item["path"] == "/spec/tls")
    if route_tls["value"].get("secretName") != canonical_secret:
        raise AssertionError("empty platform IngressRoute TLS binding did not select forgejo-tls")

    custom_route = {
        "spec": {
            "routes": [{"match": f"Host(`{target_host}`)", "kind": "Rule"}],
            "tls": {"secretName": "custom-tls"},
        }
    }
    if build_patch(custom_route, "IngressRoute", "custom", target_host, canonical_secret):
        raise AssertionError("explicit custom IngressRoute TLS binding was modified")


def test_woodpecker_route_reconciler_bundle() -> None:
    playbook = read(WOODPECKER_REPAIR_PLAYBOOK)
    for dependency in ("bounded_file.py", "strict_json.py"):
        require(
            playbook,
            f"source: {dependency}",
            "Woodpecker TLS route reconciler bundle",
        )
        require(
            playbook,
            f"- {dependency}",
            "Woodpecker TLS route reconciler cleanup",
        )


def test_public_tls_chain_completion() -> None:
    if os.name == "nt":
        print("Public TLS chain behavior test skipped on Windows; static contract still enforced.")
        return
    bash = shutil.which("bash")
    openssl = shutil.which("openssl")
    curl = shutil.which("curl")
    if not bash or not openssl or not curl:
        print("Public TLS chain behavior test skipped because bash/openssl/curl are unavailable.")
        return

    with tempfile.TemporaryDirectory(prefix="platform-public-tls-") as raw_directory:
        directory = Path(raw_directory)
        root_key = directory / "root.key"
        root_cert = directory / "root.pem"
        intermediate_key = directory / "intermediate.key"
        intermediate_csr = directory / "intermediate.csr"
        intermediate_cert = directory / "intermediate.pem"
        intermediate_der = directory / "intermediate.der"
        leaf_key = directory / "leaf.key"
        leaf_csr = directory / "leaf.csr"
        leaf_cert = directory / "leaf.pem"

        run_command(
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-sha256",
                "-nodes",
                "-days",
                "2",
                "-subj",
                "/CN=Platform TLS Test Root",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
                "-keyout",
                str(root_key),
                "-out",
                str(root_cert),
            ],
            cwd=directory,
        )
        run_command(
            [
                openssl,
                "req",
                "-new",
                "-newkey",
                "rsa:2048",
                "-sha256",
                "-nodes",
                "-subj",
                "/CN=Platform TLS Test Intermediate",
                "-keyout",
                str(intermediate_key),
                "-out",
                str(intermediate_csr),
            ],
            cwd=directory,
        )
        intermediate_extensions = directory / "intermediate.ext"
        intermediate_extensions.write_text(
            "\n".join(
                (
                    "basicConstraints=critical,CA:TRUE,pathlen:0",
                    "keyUsage=critical,keyCertSign,cRLSign",
                    "subjectKeyIdentifier=hash",
                    "authorityKeyIdentifier=keyid,issuer",
                    "",
                )
            ),
            encoding="utf-8",
        )
        run_command(
            [
                openssl,
                "x509",
                "-req",
                "-in",
                str(intermediate_csr),
                "-CA",
                str(root_cert),
                "-CAkey",
                str(root_key),
                "-CAcreateserial",
                "-days",
                "2",
                "-sha256",
                "-extfile",
                str(intermediate_extensions),
                "-out",
                str(intermediate_cert),
            ],
            cwd=directory,
        )
        run_command(
            [
                openssl,
                "x509",
                "-in",
                str(intermediate_cert),
                "-outform",
                "DER",
                "-out",
                str(intermediate_der),
            ],
            cwd=directory,
        )

        handler = functools.partial(QuietRequestHandler, directory=str(directory))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            leaf_extensions = directory / "leaf.ext"
            leaf_extensions.write_text(
                "\n".join(
                    (
                        "basicConstraints=critical,CA:FALSE",
                        "keyUsage=critical,digitalSignature,keyEncipherment",
                        "extendedKeyUsage=serverAuth",
                        "subjectAltName=DNS:forgejo.example.test",
                        (
                            "authorityInfoAccess=caIssuers;URI:"
                            f"http://127.0.0.1:{server.server_port}/{intermediate_der.name}"
                        ),
                        "",
                    )
                ),
                encoding="utf-8",
            )
            run_command(
                [
                    openssl,
                    "req",
                    "-new",
                    "-newkey",
                    "rsa:2048",
                    "-sha256",
                    "-nodes",
                    "-subj",
                    "/CN=forgejo.example.test",
                    "-keyout",
                    str(leaf_key),
                    "-out",
                    str(leaf_csr),
                ],
                cwd=directory,
            )
            run_command(
                [
                    openssl,
                    "x509",
                    "-req",
                    "-in",
                    str(leaf_csr),
                    "-CA",
                    str(intermediate_cert),
                    "-CAkey",
                    str(intermediate_key),
                    "-CAcreateserial",
                    "-days",
                    "2",
                    "-sha256",
                    "-extfile",
                    str(leaf_extensions),
                    "-out",
                    str(leaf_cert),
                ],
                cwd=directory,
            )

            completed_chain = directory / "completed-fullchain.pem"
            result = run_command(
                [bash, str(TLS_CHAIN_HELPER), str(leaf_cert), str(completed_chain), str(root_cert)],
                cwd=directory,
            )
            if "tls_chain=completed" not in result.stdout:
                raise AssertionError("leaf-only certificate did not use verified AIA completion")
            if completed_chain.read_text(encoding="utf-8").count("BEGIN CERTIFICATE") != 2:
                raise AssertionError("completed TLS chain must contain leaf plus one intermediate")
            run_command(
                [
                    openssl,
                    "verify",
                    "-purpose",
                    "sslserver",
                    "-CAfile",
                    str(root_cert),
                    "-untrusted",
                    str(completed_chain),
                    str(leaf_cert),
                ],
                cwd=directory,
            )

            supplied_chain = directory / "supplied-fullchain.pem"
            supplied_chain.write_text(
                leaf_cert.read_text(encoding="utf-8")
                + intermediate_cert.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            offline_output = directory / "offline-fullchain.pem"
            offline_env = dict(os.environ)
            offline_env["PLATFORM_TLS_AUTO_COMPLETE_CHAIN"] = "false"
            offline_result = run_command(
                [
                    bash,
                    str(TLS_CHAIN_HELPER),
                    str(supplied_chain),
                    str(offline_output),
                    str(root_cert),
                ],
                cwd=directory,
                env=offline_env,
            )
            if "tls_chain=verified" not in offline_result.stdout:
                raise AssertionError("supplied full chain was not accepted without network completion")

            rejected = run_command(
                [bash, str(TLS_CHAIN_HELPER), str(leaf_cert), str(directory / "rejected.pem"), str(root_cert)],
                cwd=directory,
                env=offline_env,
                check=False,
            )
            if rejected.returncode == 0 or "AIA completion is disabled" not in rejected.stderr:
                raise AssertionError("leaf-only certificate did not fail closed with AIA disabled")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def main() -> int:
    rendered_marker = "rendered by scripts/render_private_platform_values.py"
    for database_type in sorted(FORGEJO_NON_POSTGRES_DATABASE_TYPES):
        rendered_non_postgres = (
            f"# Forgejo profile {rendered_marker}.\n"
            f"    DB_TYPE: {database_type}\n"
        )
        if forgejo_postgres_tls_required(rendered_non_postgres):
            raise AssertionError(
                f"rendered Forgejo {database_type} profile unexpectedly requires PostgreSQL TLS"
            )
    if not forgejo_postgres_tls_required("# public premium Forgejo profile\n    DB_TYPE: sqlite3\n"):
        raise AssertionError("public premium Forgejo profile must remain PostgreSQL TLS strict")
    if not forgejo_postgres_tls_required(
        f"# Forgejo profile {rendered_marker}.\n    DB_TYPE: postgres\n"
    ):
        raise AssertionError("rendered Forgejo PostgreSQL profile must retain its TLS contract")
    modern_mysql_override = f"""# Forgejo profile {rendered_marker}.
gitea:
  additionalConfigFromEnvs:
    - name: FORGEJO__DATABASE__DB_TYPE
      value: mysql
  config:
    database:
      DB_TYPE: postgres
"""
    if forgejo_postgres_tls_required(modern_mysql_override):
        raise AssertionError("modern Forgejo MySQL override did not win database precedence")
    legacy_mysql_override = modern_mysql_override.replace(
        "FORGEJO__DATABASE__DB_TYPE",
        "GITEA__database__DB_TYPE",
    )
    if forgejo_postgres_tls_required(legacy_mysql_override):
        raise AssertionError("legacy Forgejo MySQL override did not win database precedence")
    modern_postgres_override = modern_mysql_override.replace("value: mysql", "value: postgres").replace(
        "DB_TYPE: postgres",
        "DB_TYPE: mysql",
    )
    if not forgejo_postgres_tls_required(modern_postgres_override):
        raise AssertionError("modern Forgejo PostgreSQL override did not win database precedence")
    opaque_sqlite_source = f"""# Forgejo profile {rendered_marker}.
gitea:
  additionalConfigSources:
    - secret:
        secretName: forgejo-database
  config:
    database:
      DB_TYPE: sqlite3
"""
    if not forgejo_postgres_tls_required(opaque_sqlite_source):
        raise AssertionError("opaque Forgejo database source bypassed PostgreSQL TLS validation")
    for unsupported_alias in ("sqlite", "mariadb"):
        rendered_alias = (
            f"# Forgejo profile {rendered_marker}.\n"
            f"    DB_TYPE: {unsupported_alias}\n"
        )
        if not forgejo_postgres_tls_required(rendered_alias):
            raise AssertionError(f"unsupported Forgejo DB_TYPE alias {unsupported_alias} was accepted")

    cert_manager_kustomization = read(PREMIUM / "cert-manager/kustomization.yaml")
    internal_ca = read(PREMIUM / "cert-manager/internal-ca.yaml")
    trust_bundle = read(PREMIUM / "trust-manager/bundles.yaml")
    trust_values = read(PREMIUM / "trust-manager/values.yaml")
    openbao_kustomization = read(PREMIUM / "openbao/kustomization.yaml")
    openbao_certificate = read(PREMIUM / "openbao/server-certificate.yaml")
    injector_patch = read(PREMIUM / "openbao/injector-ca-patch.yaml")
    openbao_values = read(PREMIUM / "openbao/values.yaml")
    postgres = read(PREMIUM / "platform-postgres/postgres-cluster.yaml")
    valkey_kustomization = read(PREMIUM / "platform-valkey/kustomization.yaml")
    valkey_certificate = read(PREMIUM / "platform-valkey/server-certificate.yaml")
    valkey_values = read(PREMIUM / "platform-valkey/values.yaml")
    valkey_statefulset = read(
        PREMIUM
        / "platform-valkey/charts/valkey-0.10.0/valkey/templates/statefulset.yaml"
    )
    valkey_deployment = read(
        PREMIUM
        / "platform-valkey/charts/valkey-0.10.0/valkey/templates/deploy_valkey.yaml"
    )
    forgejo = read(PREMIUM / "forgejo/values.yaml")
    woodpecker = read(PREMIUM / "woodpecker/values.yaml")
    keycloak = read(PREMIUM / "keycloak/values.yaml")
    harbor = read(PREMIUM / "harbor/values.yaml")
    harbor_kustomization = read(PREMIUM / "harbor/kustomization.yaml")
    harbor_ca_patch = read(PREMIUM / "harbor/ca-bundle-configmap-patch.yaml")
    harbor_ca_statefulset_patch = read(
        PREMIUM / "harbor/ca-bundle-configmap-statefulset-patch.yaml"
    )
    monitoring = read(PREMIUM / "monitoring/values.yaml")
    verifier = read(VERIFY_PLAYBOOK)
    secret_playbook = read(SECRET_PLAYBOOK)
    public_tls_playbook = read(PUBLIC_TLS_PLAYBOOK)
    public_tls_verifier = read(PUBLIC_TLS_VERIFY_PLAYBOOK)
    woodpecker_repair = read(WOODPECKER_REPAIR_PLAYBOOK)
    tls_chain_helper = read(TLS_CHAIN_HELPER)
    woodpecker_tls_repair_helper = read(WOODPECKER_TLS_REPAIR_HELPER)
    forgejo_runtime_repair_playbook = read(FORGEJO_RUNTIME_REPAIR_PLAYBOOK)
    forgejo_runtime_repair_helper = read(FORGEJO_RUNTIME_REPAIR_HELPER)
    makefile = read(MAKEFILE)
    pki_doc = read(PKI_DOC)
    readiness = read(PRODUCTION_READINESS)

    require(cert_manager_kustomization, "- internal-ca.yaml", "cert-manager kustomization")
    for needle in (
        "name: platform-internal-bootstrap",
        "name: platform-internal-root-ca",
        "name: platform-internal-ca",
        "selfSigned: {}",
        "rotationPolicy: Never",
        "secretName: platform-internal-root-ca",
    ):
        require(internal_ca, needle, "internal CA resources")

    for needle in (
        "name: platform-internal-roots",
        "name: platform-internal-root-ca",
        "key: tls.crt",
        "key: ca-certificates.crt",
    ):
        require(trust_bundle, needle, "trust-manager internal bundle")
    forbid(trust_bundle, "key: tls.key", "trust-manager internal bundle")
    forbid(trust_bundle, "key: ca.key", "trust-manager internal bundle")
    require(trust_values, "secretTargets:\n  enabled: false", "trust-manager values")

    require(openbao_kustomization, "- server-certificate.yaml", "OpenBao kustomization")
    require(openbao_kustomization, "- path: injector-ca-patch.yaml", "OpenBao kustomization")
    for needle in (
        "secretName: openbao-server-tls",
        "name: platform-internal-ca",
        "rotationPolicy: Always",
        "openbao.openbao.svc.cluster.local",
        '"*.openbao-internal.openbao.svc.cluster.local"',
    ):
        require(openbao_certificate, needle, "OpenBao Certificate")
    require(injector_patch, "AGENT_INJECT_VAULT_CACERT_BYTES", "OpenBao injector CA patch")
    require(injector_patch, "name: platform-internal-roots", "OpenBao injector CA patch")

    for needle in (
        "tlsDisable: false",
        "BAO_CACERT: /openbao/tls/ca.crt",
        "tls_cert_file = \"/openbao/tls/tls.crt\"",
        "tls_key_file = \"/openbao/tls/tls.key\"",
        "tls_min_version = \"tls12\"",
        "tls_max_version = \"tls13\"",
        "leader_api_addr = \"https://openbao-0.openbao-internal.openbao.svc.cluster.local:8200\"",
        "leader_api_addr = \"https://openbao-1.openbao-internal.openbao.svc.cluster.local:8200\"",
        "leader_api_addr = \"https://openbao-2.openbao-internal.openbao.svc.cluster.local:8200\"",
        "leader_tls_servername = \"openbao-0.openbao-internal.openbao.svc.cluster.local\"",
        "leader_tls_servername = \"openbao-1.openbao-internal.openbao.svc.cluster.local\"",
        "leader_tls_servername = \"openbao-2.openbao-internal.openbao.svc.cluster.local\"",
        "leader_ca_cert_file = \"/openbao/tls/ca.crt\"",
        "name: certificate-reloader",
        "kill -HUP",
        "serverName: openbao.openbao.svc.cluster.local",
    ):
        require(openbao_values, needle, "OpenBao TLS values")
    if openbao_values.count("retry_join {") != 3:
        raise AssertionError("OpenBao TLS values must declare one retry_join target per HA replica")
    forbid(openbao_values, "tls_disable = 1", "OpenBao TLS values")
    forbid(openbao_values, "insecureSkipVerify: true", "OpenBao TLS values")

    require(
        POSTGRES_SERVER_CERTIFICATE_SECRET,
        "platform-postgres-server-tls",
        "canonical PostgreSQL certificate Secret",
    )
    for needle in (
        "kind: Certificate",
        "name: platform-postgres-server",
        "secretName: platform-postgres-server-tls",
        "cnpg.io/reload",
        "rotationPolicy: Always",
        "platform-postgres-rw.platform-databases.svc.cluster.local",
        "serverCASecret: platform-postgres-server-tls",
        "serverTLSSecret: platform-postgres-server-tls",
    ):
        require(postgres, needle, "CloudNativePG TLS manifest")

    if forgejo_postgres_tls_required(forgejo):
        for needle in (
            "SSL_MODE: verify-full",
            "name: platform-internal-roots",
            "mountPath: /data/gitea/git/.postgresql",
            "name: SSL_CERT_FILE",
            "value: /data/gitea/git/.postgresql/ca-certificates.crt",
        ):
            require(forgejo, needle, "Forgejo PostgreSQL TLS values")
    for needle in (
        "name: platform-internal-roots",
        "mountPath: /etc/ssl/platform-postgres",
    ):
        require(woodpecker, needle, "Woodpecker PostgreSQL TLS values")
    for needle in (
        "materialize_from_postgres_server_ca",
        ".status.certificates.serverCASecret",
        ".status.certificates.serverTLSSecret",
        "serverCASecret",
        "platform-postgres-server-tls",
        "platform-postgres-ca",
        "cnpg.io/cluster=platform-postgres",
        "woodpecker_postgres_ca_bundle=materialized-from-postgres-server-ca",
        "load_active_postgres_server_leaf",
        "ca_file_verifies_active_postgres_server",
        "openssl verify -purpose sslserver",
        '-verify_hostname "${POSTGRES_HOST}"',
        "Hostname ${POSTGRES_HOST} does match certificate",
        "verification=does-not-match-active-server",
        "woodpecker_postgres_ca_bundle=verified-against-active-server",
        "materialize_from_cert_manager_root",
        "configmap/platform-internal-root-ca",
        "root-ca.pem",
        "Recycle stale Woodpecker server Pods after PostgreSQL CA mount repair",
        "woodpecker_postgres_ca_pod_recycle=recycled",
        "woodpecker-postgres-ca-pod-recycle-last-ready-server",
        "ownerReferences[?(@.kind==\"StatefulSet\")].name",
        "configMap.items[?(@.key==\"ca-certificates.crt\")].path",
        "reason=woodpecker-postgres-ca-configmap-invalid",
        "verification=container-file",
        "verification=projected-volume-contract",
        "reason=container-probe-tool-unavailable",
        "pvc=retained",
        "recover_immutable_server_statefulset",
        "--cascade=orphan --wait=true",
        "woodpecker_statefulset_immutable_recovery=requested",
        "woodpecker_statefulset_immutable_recovery=waiting-for-new-operation",
        "immutable_recovery_previous_started_at",
        "pvc_policy=retain",
    ):
        require(woodpecker_repair, needle, "Woodpecker PostgreSQL CA recovery")
    forbid(
        woodpecker_repair,
        "WOODPECKER_FORGEJO_SKIP_VERIFY: true",
        "Woodpecker PostgreSQL CA recovery",
    )
    forbid(
        woodpecker_repair,
        "for ordinal in $(seq 0 $((replicas - 1)))",
        "Woodpecker PostgreSQL CA Pod candidate discovery",
    )
    for needle in (
        "sslmode=verify-full&sslrootcert=/etc/ssl/platform-postgres/ca-certificates.crt",
        "name: platform-internal-roots",
        "mountPath: /etc/ssl/platform-postgres",
    ):
        require(keycloak, needle, "Keycloak PostgreSQL TLS values")
    for needle in (
        "caBundleSecretName: platform-internal-roots",
        "sslmode: verify-full",
        "tlsOptions:\n      enable: true",
    ):
        require(harbor, needle, "Harbor PostgreSQL TLS values")
    require(harbor_kustomization, "ca-bundle-configmap-patch.yaml", "Harbor kustomization")
    require(harbor_kustomization, "harbor-(core|exporter|jobservice|registry)", "Harbor kustomization")
    require(harbor_kustomization, "ca-bundle-configmap-statefulset-patch.yaml", "Harbor kustomization")
    require(harbor_kustomization, "name: harbor-trivy", "Harbor kustomization")
    require(harbor_ca_patch, "configMap:\n            name: platform-internal-roots", "Harbor CA patch")
    forbid(harbor_ca_patch, "secretName: platform-internal-roots", "Harbor CA patch")
    require(
        harbor_ca_statefulset_patch,
        "configMap:\n            name: platform-internal-roots",
        "Harbor StatefulSet CA patch",
    )
    forbid(
        harbor_ca_statefulset_patch,
        "secretName: platform-internal-roots",
        "Harbor StatefulSet CA patch",
    )
    for needle in (
        "ssl_mode: verify-full",
        "ca_cert_path: /etc/ssl/platform-postgres/ca-certificates.crt",
        "configMap: platform-internal-roots",
    ):
        require(monitoring, needle, "Grafana PostgreSQL TLS values")

    for needle in (
        "- server-certificate.yaml",
    ):
        require(valkey_kustomization, needle, "Valkey kustomization")
    for needle in (
        "name: platform-valkey-server",
        "secretName: platform-valkey-tls",
        "rotationPolicy: Always",
        "platform-valkey-primary.platform-cache.svc.cluster.local",
        '"*.platform-valkey-headless.platform-cache.svc.cluster.local"',
        "name: platform-internal-ca",
    ):
        require(valkey_certificate, needle, "Valkey Certificate")
    for needle in (
        "tls:\n  enabled: true",
        "existingSecret: platform-valkey-tls",
        "requireClientCertificate: false",
        "tls-auto-reload-interval 300",
        "port 0",
        "tls-port 26379",
        "tls-replication yes",
        "check-ssl",
        "verify required",
        "ca-file /trust/ca-certificates.crt",
        "REDIS_ADDR: rediss://localhost:6379",
        "REDIS_EXPORTER_SKIP_TLS_VERIFICATION: \"false\"",
    ):
        require(valkey_values, needle, "Valkey TLS values")
    forbid(valkey_values, "REDIS_EXPORTER_SKIP_TLS_VERIFICATION: \"true\"", "Valkey TLS values")
    for chart_template in (valkey_statefulset, valkey_deployment):
        require(chart_template, "name: REDISCLI_AUTH", "Valkey workload template")
        require(
            chart_template,
            "--tls{{ if .Values.auth.enabled }} --user default --no-auth-warning{{ end }} ping",
            "Valkey workload template",
        )

    for needle in (
        "HARBOR_REDIS_TLS:-true",
        "FORGEJO_REDIS_TLS:-true",
        "Reject plaintext Harbor cache transport in production mode",
        "HARBOR_REDIS_TLS=false is not allowed",
        "Reject plaintext Forgejo cache transport in production mode",
        "platform_forgejo_redis_url_from_env",
        'scheme = "rediss"',
        "state=reconciled",
    ):
        require(secret_playbook, needle, "cache URI secret automation")

    for needle in (
        "complete_tls_chain.sh",
        'fullchain="{{ platform_tls_remote_directory }}/tls.fullchain.crt"',
        '--cert="${fullchain}"',
        "signed AIA issuer path",
    ):
        require(public_tls_playbook, needle, "public TLS distribution")
    for needle in (
        "verify_chain_file",
        "-verify_return_error",
        "-CAfile \"${trust_bundle}\"",
        "-untrusted \"${intermediate_path}\"",
        "discovered_host",
        "host_source=%s",
    ):
        require(public_tls_verifier, needle, "public TLS verification")
    for needle in (
        "PLATFORM_WOODPECKER_REPAIR_AUTO_FORGEJO_TLS_CHAIN",
        "woodpecker_oauth_tls",
        "repair_woodpecker_oauth_tls.sh",
        "complete_tls_chain.sh",
    ):
        require(woodpecker_repair, needle, "Woodpecker OAuth TLS repair")
    for needle in (
        "openssl verify -purpose sslserver",
        "authorityInfoAccess",
        "--proto '=http,https'",
        "--max-filesize 1048576",
        "openssl verify -partial_chain",
        "CA:TRUE",
        "AIA issuer chain contains a cycle",
    ):
        require(tls_chain_helper, needle, "TLS chain completion helper")
    for needle in (
        "forgejo_oauth_tls_chain=verified",
        "forgejo-oauth-tls-chain-untrusted",
        "forgejo-oauth-tls-chain-self-signed",
        "WOODPECKER_FORGEJO_URL",
        "-verify_return_error",
        '-verify_hostname "${forgejo_host}"',
        'create secret tls "${secret}"',
        "refresh_traefik_certificate_cache",
        "reason=tls-secret-cache-refresh",
        "traefik-serial-refresh-timeout",
        "matching-wildcard-leaf-fingerprint",
        "reconcile_matching_tls_secrets",
        "woodpecker-forgejo-url-route-drift",
        "forgejo-route-hosts-ambiguous",
        "platform_route_hosts",
    ):
        require(woodpecker_tls_repair_helper, needle, "Woodpecker OAuth TLS repair helper")
    for needle in (
        "validate_storage_contract",
        "database_backend",
        "forgejo-database-type-unknown",
        "non-postgres-backend",
        "forgejo-object-storage-secret-missing",
        "forgejo-object-storage-mode-not-applied",
        "active_postgres_certificate",
        "POSTGRES_SERVER_CERTIFICATE_SECRET",
        "validate_postgres_server_certificate_secret",
        "reconcile_postgres_certificate_contract",
        "postgres_server_handshake_verifies",
        "tls.key",
        "platform-postgres-rw",
        "forgejo_postgres_certificates=reconciled",
        "forgejo_postgres_certificates=verified",
        "root.crt",
        "serverCASecret",
        "openssl",
        "configmap/platform-internal-roots",
        "POSTGRES_CA_BUNDLE_PATH",
        "mount_contract_ready",
        "container_mount_paths",
        "container_env_value",
        "tls_env_contract_ready",
        "stale_init_application_mount_patch",
        '"op": "test"',
        "metadata/resourceVersion",
        "STALE_INIT_MOUNT_CLEANUP_RETRIES",
        "forgejo_postgres_ca_init_mount=removed",
        "forgejo_postgres_ca_env=patched",
        "SSL_CERT_FILE",
        "redact_diagnostic_text",
        "--previous",
        "forgejo_container_log=",
        "last_exit_code=",
        "initContainerStatuses",
        'conditions.get("ready") is True',
        "application-only",
        "rollout",
        "result=ok",
    ):
        require(
            forgejo_runtime_repair_helper,
            needle,
            f"Forgejo runtime repair must retain fail-closed controls: {needle}",
        )
    forbid(
        forgejo_runtime_repair_helper,
        "delete pvc",
        "Forgejo runtime repair",
    )
    for needle in (
        "repair_forgejo_runtime.py",
        "Repair Forgejo runtime dependencies and PostgreSQL trust",
        "Stop when Forgejo runtime repair cannot converge",
    ):
        require(forgejo_runtime_repair_playbook, needle, "Forgejo runtime repair playbook")
    for needle in (
        "woodpecker_forgejo_url_repair=true",
        "reconcile-woodpecker-gitops-source.sh",
        "forgejo_ingress_repair=true",
        "forgejo_tls_self_signed=true",
        "platform-forgejo-runtime-repair",
        "applying the canonical Forgejo ingress contract",
    ):
        require(makefile, needle, "Woodpecker classified prerequisite recovery")
    test_forgejo_runtime_mount_contract_scope()
    test_forgejo_runtime_storage_preflight()
    test_forgejo_config_environment_runtime()
    test_forgejo_chart_dependency_env_precedence()
    test_forgejo_postgres_tls_probe()
    test_forgejo_postgres_probe_retry_deadline()
    test_forgejo_postgres_tunnel_failures()
    test_forgejo_runtime_still_requires_application_readiness()
    test_valkey_runtime_contract()
    test_valkey_longhorn_storage_preflight()
    test_forgejo_route_reconciliation()
    test_woodpecker_route_reconciler_bundle()
    test_public_tls_chain_completion()

    for needle in (
        "openssl s_client",
        "-verify_hostname \"$OPENBAO_DNS\"",
        "-starttls postgres",
        "-verify_hostname \"$POSTGRES_DNS\"",
        "-verify_hostname \"$VALKEY_DNS\"",
        "certificate/platform-valkey-server",
        "valkey-cli --tls --cacert /tls/ca.crt",
        "reason=valkey-plaintext-listener-accepted-command",
        "valkey_tls=verified",
        "plaintext_disabled=true",
        "pg_stat_ssl",
        "database_clients=verified",
        "private-key-present-in-trust-bundle",
        "AGENT_INJECT_VAULT_CACERT_BYTES",
        "platform_internal_tls=verified",
    ):
        require(verifier, needle, "live internal TLS verifier")

    production_check = read(PRODUCTION_CHECK)
    require(makefile, "platform-internal-tls-verify:", "Makefile")
    require(
        production_check,
        '"${make_command}" platform-internal-tls-verify',
        "production readiness gate",
    )
    require(readiness, "make platform-internal-tls-verify", "production readiness documentation")
    require(pki_doc, "platform-internal-root-ca", "internal PKI documentation")
    require(pki_doc, "SIGHUP", "internal PKI rotation documentation")

    print("Managed internal TLS contract passed for OpenBao, CloudNativePG, and Valkey.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
