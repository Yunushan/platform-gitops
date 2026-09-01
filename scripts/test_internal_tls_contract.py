#!/usr/bin/env python3
"""Validate managed internal trust and encrypted OpenBao/PostgreSQL/Valkey paths."""

from __future__ import annotations

import functools
import http.server
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

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
        '"-starttls"',
        '"-verify_return_error"',
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
