#!/usr/bin/env python3
"""Self-test private platform value rendering."""
from __future__ import annotations

from contextlib import contextmanager
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import re
import sys
import tempfile

from synthetic_private_profile import (
    RENDERER_ENV_PREFIXES,
    TEST_COSIGN_PUBLIC_KEY,
    TEST_INVENTORY,
    prepare_synthetic_private_profile,
    synthetic_environment,
)


ROOT = Path(__file__).resolve().parents[1]
RENDERER_PATH = ROOT / "scripts/render_private_platform_values.py"
CHECKER_PATH = ROOT / "scripts/check_gitops_profile.py"
CONTRACT_VALIDATOR_PATH = ROOT / "scripts/validate_platform_contract.py"
PLACEHOLDER_RE = re.compile(r"<[A-Z0-9_]+>")
sys.dont_write_bytecode = True


def load_renderer():
    spec = importlib.util.spec_from_file_location("render_private_platform_values", RENDERER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {RENDERER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_checker():
    spec = importlib.util.spec_from_file_location("check_gitops_profile", CHECKER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {CHECKER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_contract_validator():
    spec = importlib.util.spec_from_file_location("validate_platform_contract", CONTRACT_VALIDATOR_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {CONTRACT_VALIDATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def patched_env(values: dict[str, str]):
    managed_keys = set(values)
    managed_keys.update(
        key
        for key in os.environ
        if key.startswith(RENDERER_ENV_PREFIXES)
    )
    previous = {key: os.environ.get(key) for key in managed_keys}
    try:
        for key in managed_keys:
            os.environ.pop(key, None)
        os.environ.update(values)
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def assert_no_placeholders(paths: list[Path]) -> None:
    findings: list[str] = []
    for path in paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if PLACEHOLDER_RE.search(line):
                findings.append(f"{path}:{line_number}: {line.strip()}")
    if findings:
        raise AssertionError("rendered private values still contain placeholders:\n" + "\n".join(findings))


def assert_contains(path: Path, *needles: str) -> None:
    text = path.read_text(encoding="utf-8")
    missing = [needle for needle in needles if needle not in text]
    if missing:
        raise AssertionError(f"{path} is missing expected text: {', '.join(missing)}")


def assert_not_contains(path: Path, *needles: str) -> None:
    text = path.read_text(encoding="utf-8")
    present = [needle for needle in needles if needle in text]
    if present:
        raise AssertionError(f"{path} contains unexpected text: {', '.join(present)}")


def assert_hardened_woodpecker_values(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    pod_security_contract = (
        "podSecurityContext:\n"
        "    runAsNonRoot: true\n"
        "    fsGroup: 1000\n"
        "    seccompProfile:\n"
        "      type: RuntimeDefault"
    )
    container_security_contract = (
        "securityContext:\n"
        "    allowPrivilegeEscalation: false\n"
        "    capabilities:\n"
        "      drop:\n"
        "        - ALL\n"
        "    runAsNonRoot: true\n"
        "    runAsUser: 1000\n"
        "    runAsGroup: 1000"
    )
    if text.count(pod_security_contract) != 2:
        raise AssertionError("rendered Woodpecker values must harden both pod roles")
    if text.count(container_security_contract) != 2:
        raise AssertionError("rendered Woodpecker values must harden both containers")
    assert_contains(
        path,
        "  resources:\n    requests:\n      cpu: 50m\n      memory: 256Mi\n    limits:\n      memory: 1Gi",
        "  resources:\n    requests:\n      cpu: 100m\n      memory: 256Mi\n    limits:\n      memory: 1Gi",
        "extraVolumes:\n    - name: agent-config\n      emptyDir: {}",
        "extraVolumeMounts:\n    - name: agent-config\n      mountPath: /etc/woodpecker",
    )
    assert_not_contains(path, "limits:\n      cpu:")


def render_real_premium_profile(renderer, checker, env: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory(prefix="platform-real-premium-render-") as tmp:
        repo = Path(tmp) / "repo"
        prepare_synthetic_private_profile(
            repo,
            source_root=ROOT,
            environment_overrides=env,
        )

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = checker.check_profile(repo, "premium-3node")
        if rc != 0:
            raise AssertionError(
                "rendered real premium profile did not pass profile check:\n"
                + stdout.getvalue()
                + stderr.getvalue()
            )
        assert_contains(
            repo / "gitops/clusters/rke2-main/premium-3node/apps/woodpecker/values.yaml",
            'WOODPECKER_ADMIN: "platform-admin"',
        )
        assert_hardened_woodpecker_values(
            repo / "gitops/clusters/rke2-main/premium-3node/apps/woodpecker/values.yaml"
        )


def test_forgejo_image_matches_reviewed_chart(renderer) -> None:
    chart_path = (
        ROOT
        / "gitops/clusters/rke2-main/premium-3node/apps/forgejo/charts/forgejo-17.1.4/forgejo/Chart.yaml"
    )
    chart_text = chart_path.read_text(encoding="utf-8")
    match = re.search(r"^appVersion:\s*v?([0-9]+\.[0-9]+\.[0-9]+)\s*$", chart_text, re.MULTILINE)
    if match is None:
        raise AssertionError("vendored Forgejo chart must declare a semantic appVersion")
    app_version = match.group(1)
    if renderer.FORGEJO_DEFAULT_IMAGE_TAG != app_version:
        raise AssertionError(
            "Forgejo renderer default image tag must match the vendored chart appVersion"
        )
    for values_path in (
        ROOT / "gitops/clusters/rke2-main/apps/forgejo/values.yaml",
        ROOT / "gitops/clusters/rke2-main/premium-3node/apps/forgejo/values.yaml",
    ):
        values_text = values_path.read_text(encoding="utf-8")
        tag_match = re.search(r'^\s*tag:\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*$', values_text, re.MULTILINE)
        if tag_match is None or tag_match.group(1) != app_version:
            raise AssertionError(
                f"{values_path} must pin Forgejo image tag {app_version} from the reviewed chart"
            )


def test_focused_woodpecker_cli_refreshes_only_forgejo_release_pin(renderer) -> None:
    """A Woodpecker render refreshes the reviewed Forgejo pin without private dependencies."""
    with tempfile.TemporaryDirectory(prefix="platform-woodpecker-render-") as tmp:
        repo = Path(tmp)
        inventory_path = write(repo / "inventory/hosts.local.ini", TEST_INVENTORY)
        forgejo_path = write(
            repo / "gitops/clusters/rke2-main/premium-3node/apps/forgejo/values.yaml",
            'image:\n  rootless: true\n  tag: "14.0.0"\n\n'
            'ingress:\n  hosts:\n    - host: forgejo.private.example.test\n',
        )
        woodpecker_path = write(
            repo / "gitops/clusters/rke2-main/premium-3node/apps/woodpecker/values.yaml"
        )
        public_key = write(repo / "private/cosign.pub", TEST_COSIGN_PUBLIC_KEY)
        focused_env = synthetic_environment(public_key)
        for key in (
            "FORGEJO_S3_ENDPOINT",
            "FORGEJO_OBJECT_STORAGE_ENDPOINT",
            "OBJECT_STORAGE_ENDPOINT",
            "LONGHORN_BACKUP_TARGET",
            "BACKUP_OBJECT_STORAGE_ENDPOINT",
        ):
            focused_env.pop(key, None)

        previous_cwd = Path.cwd()
        previous_argv = sys.argv[:]
        try:
            os.chdir(repo)
            sys.argv = [
                str(RENDERER_PATH),
                "--inventory",
                str(inventory_path),
                "--forgejo-values",
                str(forgejo_path),
                "--woodpecker-values",
                str(woodpecker_path),
                "--skip-forgejo",
                "--refresh-forgejo-release-pin",
            ]
            with patched_env(focused_env):
                rc = renderer.main()
        finally:
            os.chdir(previous_cwd)
            sys.argv = previous_argv

        if rc != 0:
            raise AssertionError("focused Woodpecker rendering returned a non-zero status")
        assert_contains(
            forgejo_path,
            'tag: "15.0.6"',
            "host: forgejo.private.example.test",
        )
        assert_not_contains(forgejo_path, 'tag: "14.0.0"')
        assert_contains(woodpecker_path, 'WOODPECKER_HOST: "https://ci.example.test"')


def test_strict_longhorn_render_requires_explicit_disk_path(renderer) -> None:
    """Strict rendering must not silently restore the root-backed default."""
    with tempfile.TemporaryDirectory(prefix="platform-longhorn-render-") as tmp:
        values_path = write(
            Path(tmp) / "longhorn-values.yaml",
            'defaultSettings:\n  defaultDataPath: "<PLATFORM_LONGHORN_DEFAULT_DISK_PATH>"\n',
        )
        env = synthetic_environment(Path(tmp) / "cosign.pub")
        env.pop("PLATFORM_LONGHORN_DEFAULT_DISK_PATH")
        with patched_env(env):
            try:
                renderer.render_longhorn(values_path, env["LONGHORN_BACKUP_TARGET"])
            except SystemExit as exc:
                if "PLATFORM_LONGHORN_DEFAULT_DISK_PATH is required" not in str(exc):
                    raise AssertionError(f"unexpected Longhorn path error: {exc}") from exc
            else:
                raise AssertionError("strict Longhorn rendering accepted a missing disk path")


def main() -> int:
    renderer = load_renderer()
    checker = load_checker()
    contract_validator = load_contract_validator()
    test_forgejo_image_matches_reviewed_chart(renderer)
    test_focused_woodpecker_cli_refreshes_only_forgejo_release_pin(renderer)
    test_strict_longhorn_render_requires_explicit_disk_path(renderer)

    with tempfile.TemporaryDirectory(prefix="platform-private-render-") as tmp:
        repo = Path(tmp)
        inventory_path = write(
            repo / "inventory/hosts.local.ini",
            TEST_INVENTORY,
        )
        inventory = renderer.read_inventory_vars(inventory_path)

        paths = {
            "argocd": write(
                repo / "gitops/clusters/rke2-main/premium-3node/apps/argocd-ha/values.yaml",
                "server:\n  ingress:\n    hostname: argocd.<PLATFORM_DOMAIN>\n"
                "configs:\n  cm:\n    admin.enabled: \"false\"\n",
            ),
            "longhorn": write(
                repo / "gitops/clusters/rke2-main/premium-3node/apps/longhorn/values.yaml",
                'defaultSettings:\n  defaultDataPath: "<PLATFORM_LONGHORN_DEFAULT_DISK_PATH>"\n'
                '  backupTarget: "<LONGHORN_BACKUP_TARGET>"\n'
                "  backupTargetCredentialSecret: <LONGHORN_BACKUP_CREDENTIAL_SECRET_NAME>\n"
                "  storageOverProvisioningPercentage: 100\n",
            ),
            "forgejo": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/forgejo/values.yaml"),
            "woodpecker": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/woodpecker/values.yaml"),
            "harbor": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/harbor/values.yaml"),
            "monitoring": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/monitoring/values.yaml"),
            "loki": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/loki/values.yaml"),
            "velero": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/velero/values.yaml"),
            "cnpg": write(
                repo / "gitops/clusters/rke2-main/premium-3node/apps/platform-postgres/postgres-cluster.yaml"
            ),
            "valkey": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/platform-valkey/values.yaml"),
            "minio": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/minio/values.yaml"),
            "keycloak": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/keycloak/values.yaml"),
            "step_ca": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/step-ca/values.yaml"),
            "secret_policy": write(
                repo / "gitops/clusters/rke2-main/premium-3node/apps/platform-policies/no-plaintext-secrets.yaml",
                "spec:\n  validationActions:\n    - Audit\n",
            ),
            "workload_policy": write(
                repo / "gitops/clusters/rke2-main/premium-3node/apps/platform-policies/require-workload-baseline.yaml",
                "spec:\n  validationActions:\n    - Audit\n",
            ),
            "pod_security_policy": write(
                repo / "gitops/clusters/rke2-main/premium-3node/apps/platform-policies/require-pod-security-baseline.yaml",
                "spec:\n  validationActions:\n    - Audit\n",
            ),
            "image_integrity_policy": write(
                repo
                / "gitops/clusters/rke2-main/premium-3node/apps/platform-image-integrity/verify-platform-images.yaml",
                (
                    ROOT
                    / "gitops/clusters/rke2-main/premium-3node/apps/platform-image-integrity/verify-platform-images.yaml"
                ).read_text(encoding="utf-8"),
            ),
        }
        cosign_public_key = write(repo / "private/cosign.pub", TEST_COSIGN_PUBLIC_KEY)

        env = synthetic_environment(cosign_public_key)

        with patched_env(env):
            if contract_validator.configured_longhorn_storage_over_provisioning_percentage() != 275:
                raise AssertionError(
                    "platform contract validator ignored the private Longhorn overprovisioning setting"
                )
            renderer.render_argocd(paths["argocd"], inventory)
            renderer.render_forgejo(paths["forgejo"], inventory)
            renderer.render_longhorn(paths["longhorn"], os.environ["LONGHORN_BACKUP_TARGET"])
            renderer.render_woodpecker(paths["woodpecker"], inventory)
            renderer.render_harbor(paths["harbor"], inventory)
            renderer.render_monitoring(paths["monitoring"], inventory)
            renderer.render_loki(paths["loki"], inventory)
            renderer.render_velero(paths["velero"])
            renderer.render_cnpg_postgres_cluster(paths["cnpg"])
            renderer.render_platform_valkey(paths["valkey"])
            renderer.render_minio(paths["minio"])
            renderer.render_keycloak(paths["keycloak"], inventory)
            renderer.render_step_ca(paths["step_ca"], inventory)
            renderer.render_platform_policy_enforcement(
                [
                    paths["secret_policy"],
                    paths["workload_policy"],
                    paths["pod_security_policy"],
                ]
            )
            renderer.render_platform_image_integrity(
                paths["image_integrity_policy"], inventory
            )

        rendered_paths = list(paths.values())
        assert_no_placeholders(rendered_paths)
        assert_contains(
            paths["argocd"],
            "argocd.example.test",
            'admin.enabled: "false"',
            "oidc.config: |",
            "issuer: https://sso.example.test/realms/platform",
            "clientSecret: $" + "platform-sso-argocd:client-secret",
            "requestedScopes: [\"openid\", \"profile\", \"email\", \"groups\"]",
        )
        assert_contains(paths["secret_policy"], "validationActions:\n    - Audit")
        assert_contains(paths["workload_policy"], "validationActions:\n    - Audit")
        assert_contains(paths["pod_security_policy"], "validationActions:\n    - Audit")
        assert_contains(
            paths["image_integrity_policy"],
            "apiVersion: policies.kyverno.io/v1",
            "kind: ImageValidatingPolicy",
            "image.registry == 'registry.example.test'",
            "validationActions:\n    - Audit",
            "https://rekor.example.test",
            "-----BEGIN PUBLIC KEY-----",
            "insecureIgnoreTlog: false",
            "mutateDigest: true",
            "required: true",
            "verifyDigest: true",
        )
        with patched_env(dict(env, PLATFORM_POLICY_ENFORCEMENT="Enforce")):
            renderer.render_platform_policy_enforcement(
                [
                    paths["secret_policy"],
                    paths["workload_policy"],
                    paths["pod_security_policy"],
                ]
            )
        assert_contains(paths["secret_policy"], "validationActions:\n    - Deny")
        assert_contains(paths["workload_policy"], "validationActions:\n    - Deny")
        assert_contains(paths["pod_security_policy"], "validationActions:\n    - Deny")
        with patched_env(dict(env, PLATFORM_IMAGE_INTEGRITY_MODE="Enforce")):
            renderer.render_platform_image_integrity(
                paths["image_integrity_policy"], inventory
            )
        assert_contains(
            paths["image_integrity_policy"], "validationActions:\n    - Deny"
        )
        with patched_env(dict(env, PLATFORM_IMAGE_INTEGRITY_MODE="disabled")):
            renderer.render_platform_image_integrity(
                paths["image_integrity_policy"], inventory
            )
        assert_contains(
            paths["image_integrity_policy"],
            "<PLATFORM_IMAGE_REGISTRY>",
            "<PLATFORM_COSIGN_PUBLIC_KEY>",
            "<PLATFORM_COSIGN_REKOR_URL>",
            "validationActions:\n    - Audit",
        )
        with patched_env(dict(env, PLATFORM_IMAGE_INTEGRITY_MODE="invalid")):
            try:
                renderer.render_platform_image_integrity(
                    paths["image_integrity_policy"], inventory
                )
            except SystemExit as exc:
                if "must be disabled, Audit, or Enforce" not in str(exc):
                    raise AssertionError(
                        f"unexpected image-integrity mode validation error: {exc}"
                    ) from exc
            else:
                raise AssertionError("image-integrity renderer accepted an unsupported mode")
        with patched_env(dict(env, PLATFORM_POLICY_ENFORCEMENT="invalid")):
            try:
                renderer.render_platform_policy_enforcement([paths["secret_policy"]])
            except SystemExit as exc:
                if "must be Audit or Enforce" not in str(exc):
                    raise AssertionError(f"unexpected policy-mode validation error: {exc}") from exc
            else:
                raise AssertionError("policy renderer accepted an unsupported enforcement mode")

        disabled_argocd_path = write(
            repo / "gitops/clusters/rke2-main/premium-3node/apps/argocd-ha/disabled-values.yaml",
            "global:\n  domain: argocd.<PLATFORM_DOMAIN>\n"
            "configs:\n  cm:\n    admin.enabled: \"true\"\n",
        )
        with patched_env(
            dict(
                env,
                PLATFORM_SSO_ENABLED="false",
                PLATFORM_ARGOCD_ADMIN_ENABLED="false",
            )
        ):
            try:
                renderer.render_argocd(disabled_argocd_path, inventory)
            except SystemExit as exc:
                if "requires PLATFORM_SSO_ENABLED=true" not in str(exc):
                    raise AssertionError(f"unexpected Argo CD login validation error: {exc}") from exc
            else:
                raise AssertionError("Argo CD renderer disabled every login method")
        assert_contains(
            paths["forgejo"],
            "git.example.test",
            "21Gi",
            'tag: "15.0.6-rootless"',
            "strategy:\n  type: Recreate",
            "podDisruptionBudget:\n  minAvailable: 1",
            "DB_TYPE: postgres",
            'HOST: "platform-postgres-rw.platform-databases.svc.cluster.local:5432"',
            'SSL_MODE: "verify-full"',
            "name: platform-postgres-ca",
            "name: platform-internal-roots",
            "mountPath: /data/gitea/git/.postgresql",
            "GITEA__cache__HOST",
            'name: "forgejo-redis"',
            "ADAPTER: redis",
            "TYPE: redis",
        )

        sqlite_forgejo_path = write(repo / "gitops/clusters/rke2-main/premium-3node/apps/forgejo/sqlite-values.yaml")
        sqlite_forgejo_env = dict(env)
        sqlite_forgejo_env.update(
            {
                "FORGEJO_DATABASE_MODE": "sqlite",
                "PLATFORM_PRODUCTION_STRICT": "false",
                "FORGEJO_OBJECT_STORAGE_MODE": "filesystem",
            }
        )
        with patched_env(sqlite_forgejo_env):
            renderer.render_forgejo(sqlite_forgejo_path, inventory)
        assert_contains(
            sqlite_forgejo_path,
            "git.example.test",
            "sqlite3",
            'tag: "15.0.6-rootless"',
            "strategy:\n  type: Recreate",
            "podDisruptionBudget:\n  minAvailable: 1",
        )
        assert_not_contains(sqlite_forgejo_path, "additionalConfigFromEnvs:", "DB_TYPE: postgres")

        external_forgejo_path = write(repo / "gitops/clusters/rke2-main/premium-3node/apps/forgejo/external-values.yaml")
        external_forgejo_env = dict(env)
        external_forgejo_env.update(
            {
                "FORGEJO_DATABASE_MODE": "external",
                "FORGEJO_DATABASE_HOST": "forgejo-postgres.example.test:5432",
                "FORGEJO_DATABASE_NAME": "forgejo",
                "FORGEJO_DATABASE_USER": "forgejo",
                "FORGEJO_DATABASE_SECRET_NAME": "forgejo-db-test",
                "FORGEJO_DATABASE_SSL_MODE": "verify-full",
                "FORGEJO_REDIS_MODE": "redis",
                "FORGEJO_REDIS_SECRET_NAME": "forgejo-redis-test",
                "FORGEJO_OBJECT_STORAGE_MODE": "s3",
                "FORGEJO_S3_ENDPOINT": "https://object.example.test",
                "FORGEJO_S3_REGION": "eu-test-1",
                "FORGEJO_S3_BUCKET": "platform-test-forgejo",
                "FORGEJO_S3_SECRET_NAME": "forgejo-object-test",
                "FORGEJO_S3_SECURE": "true",
            }
        )
        with patched_env(external_forgejo_env):
            renderer.render_forgejo(external_forgejo_path, inventory)
        assert_contains(
            external_forgejo_path,
            "additionalConfigFromEnvs:",
            "GITEA__database__PASSWD",
            'name: "forgejo-db-test"',
            "key: password",
            "GITEA__cache__HOST",
            "GITEA__queue__CONN_STR",
            'name: "forgejo-redis-test"',
            "key: uri",
            "GITEA__storage__MINIO_ACCESS_KEY_ID",
            "GITEA__storage__MINIO_SECRET_ACCESS_KEY",
            'name: "forgejo-object-test"',
            "key: access-key-id",
            "key: secret-access-key",
            "DB_TYPE: postgres",
            'HOST: "forgejo-postgres.example.test:5432"',
            'SSL_MODE: "verify-full"',
            "attachment:\n      STORAGE_TYPE: minio",
            "lfs:\n      STORAGE_TYPE: minio",
            "AVATAR_STORAGE_TYPE: minio",
            "'storage.packages':\n      STORAGE_TYPE: minio",
            'MINIO_ENDPOINT: "object.example.test"',
            'MINIO_LOCATION: "eu-test-1"',
            'MINIO_BUCKET: "platform-test-forgejo"',
            "MINIO_USE_SSL: true",
            "mountPath: /data/gitea/git/.postgresql",
            "name: SSL_CERT_FILE",
            "value: /etc/ssl/platform/ca-certificates.crt",
            "mountPath: /etc/ssl/platform",
        )
        assert_not_contains(external_forgejo_path, "FORGEJO_REDIS_URL", "redis://")

        insecure_forgejo_storage_env = dict(external_forgejo_env, FORGEJO_S3_SECURE="false")
        with patched_env(insecure_forgejo_storage_env):
            try:
                renderer.render_forgejo(external_forgejo_path, inventory)
            except SystemExit as exc:
                if "FORGEJO_S3_SECURE must be true" not in str(exc):
                    raise AssertionError(
                        f"unexpected Forgejo object-storage validation error: {exc}"
                    ) from exc
            else:
                raise AssertionError("Forgejo renderer accepted plaintext object storage in strict mode")

        local_forgejo_storage_env = dict(
            external_forgejo_env,
            FORGEJO_S3_ENDPOINT="http://platform-minio.object-storage.svc.cluster.local:9000",
        )
        with patched_env(local_forgejo_storage_env):
            try:
                renderer.render_forgejo(external_forgejo_path, inventory)
            except SystemExit as exc:
                if "off-cluster S3-compatible endpoint" not in str(exc):
                    raise AssertionError(
                        f"unexpected Forgejo local-storage validation error: {exc}"
                    ) from exc
            else:
                raise AssertionError("Forgejo renderer accepted cluster-local object storage in strict mode")

        mysql_forgejo_path = write(repo / "gitops/clusters/rke2-main/premium-3node/apps/forgejo/mysql-values.yaml")
        mysql_forgejo_env = dict(env)
        mysql_forgejo_env.update(
            {
                "FORGEJO_DATABASE_MODE": "mariadb",
                "FORGEJO_DATABASE_HOST": "forgejo-mariadb.example.test:3306",
                "FORGEJO_DATABASE_NAME": "forgejo",
                "FORGEJO_DATABASE_USER": "forgejo",
                "FORGEJO_DATABASE_SECRET_NAME": "forgejo-mariadb-test",
            }
        )
        with patched_env(mysql_forgejo_env):
            renderer.render_forgejo(mysql_forgejo_path, inventory)
        assert_contains(
            mysql_forgejo_path,
            "DB_TYPE: mysql",
            'HOST: "forgejo-mariadb.example.test:3306"',
            'name: "forgejo-mariadb-test"',
        )
        assert_contains(
            paths["longhorn"],
            'defaultDataPath: "/mnt/longhorn"',
            "s3://platform-test-longhorn@eu-test-1/",
            "backupTargetCredentialSecret: longhorn-backup-test",
            "storageOverProvisioningPercentage: 275",
        )
        if (
            contract_validator.yaml_integer_scalar(
                paths["longhorn"].read_text(encoding="utf-8"),
                "storageOverProvisioningPercentage",
                "rendered Longhorn test profile",
            )
            != 275
        ):
            raise AssertionError("platform contract validator rejected the rendered Longhorn override")
        assert_contains(
            paths["cnpg"],
            'namespace: "platform-databases"',
            "kind: Certificate",
            'name: "platform-postgres-server"',
            'secretName: "platform-postgres-server-tls"',
            'serverCASecret: "platform-postgres-server-tls"',
            'serverTLSSecret: "platform-postgres-server-tls"',
            'imageName: "ghcr.io/cloudnative-pg/postgresql:18.4-system-trixie"',
            'size: "80Gi"',
            'storageClass: "longhorn-critical-encrypted"',
            'database: "forgejo"',
            'owner: "forgejo"',
            'name: "forgejo-database"',
            'name: "woodpecker"',
            'name: "woodpecker-db-test"',
            'name: "harbor"',
            'name: "harbor-db-test"',
            'name: "grafana"',
            'name: "grafana-db-test"',
            'destinationPath: "s3://platform-test-cnpg/platform-postgres"',
            'endpointURL: "https://object.example.test"',
            'name: "cnpg-object-test"',
            "key: ACCESS_KEY_ID",
            "key: SECRET_ACCESS_KEY",
            'schedule: "20 2 * * *"',
        )
        assert_contains(
            paths["valkey"],
            'usersExistingSecret: "platform-valkey-test"',
            'passwordKey: "valkey-password-test"',
            'tag: "9.1.0"',
            'storageClass: "longhorn-critical-encrypted"',
            'size: "9Gi"',
            "replicas: 3",
            "podDisruptionBudget:\n  enabled: true",
            "whenUnsatisfiable: DoNotSchedule",
            "name: configure-ha",
            'password="$(cat /auth/valkey-password-test)"',
            "sentinel monitor platform-valkey",
            "sentinel down-after-milliseconds platform-valkey 5000",
            "tls:\n  enabled: true",
            "existingSecret: platform-valkey-tls",
            "tls-auto-reload-interval 300",
            "tls-port 26379",
            "tls-replication yes",
            "name: sentinel",
            'image: "valkey/valkey:9.1.0"',
            "name: primary-proxy",
            'image: "haproxy:3.4.2-alpine"',
            "tcp-check expect string role:master",
            "check-ssl",
            "verify required",
            "ca-file /trust/ca-certificates.crt",
            "server valkey-3 platform-valkey-3.platform-valkey-headless.platform-cache.svc.cluster.local:6379",
            "REDIS_ADDR: rediss://localhost:6379",
            'REDIS_EXPORTER_SKIP_TLS_VERIFICATION: "false"',
            "serviceMonitor:\n    enabled: true",
        )
        assert_contains(
            paths["keycloak"],
            'registry: "quay.io"',
            'repository: "keycloak/keycloak"',
            'tag: "26.7.0"',
            "allowInsecureImages: true",
            "- /opt/keycloak/bin/kc.sh",
            "- start",
            "runAsUser: 1000",
            "readOnlyRootFilesystem: false",
            "prepareWriteDirs:\n    enabled: false",
            "name: KC_DB",
            "value: postgres",
            "keycloakConfigCli:",
            'repository: "adorsys/keycloak-config-cli"',
            'tag: "6.5.1"',
            "- /app/keycloak-config-cli.jar",
            "runAsUser: 65534",
            "enabled: true",
            "IMPORT_VARSUBSTITUTION_ENABLED",
            'extraEnvVarsSecret: "platform-sso-clients"',
            '"realm": "platform"',
            '"clientId": "argocd"',
            '"clientId": "grafana"',
            '"clientId": "prometheus"',
            '"username": "platform-bootstrap-test"',
            '"requiredActions": [',
            '"CONFIGURE_TOTP"',
        )
        assert_contains(
            paths["minio"],
            'existingSecret: "minio-root-test"',
            'rootUserSecretKey: "root-user-test"',
            'rootPasswordSecretKey: "root-password-test"',
            "mode: distributed",
            "repository: bitnamilegacy/os-shell",
            "tag: 12-debian-12-r50",
            "replicaCount: 4",
            "zones: 1",
            "drivesPerNode: 1",
            'storageClass: "longhorn-critical-encrypted"',
            'size: "64Gi"',
            "prometheusAuthType: public",
            "serviceMonitor:\n    enabled: true",
            'name: "platform-test-velero"',
        )
        assert_contains(
            paths["keycloak"],
            "sso.example.test",
            'existingSecret: "keycloak-admin-test"',
            'passwordSecretKey: "admin-password"',
            "replicaCount: 2",
            'host: "platform-postgres-rw.platform-databases.svc.cluster.local"',
            'user: "keycloak"',
            'database: "keycloak"',
            'existingSecret: "keycloak-db-test"',
            "extraParams: sslmode=verify-full&sslrootcert=/etc/ssl/platform-postgres/ca-certificates.crt",
            "name: platform-internal-roots",
            "mountPath: /etc/ssl/platform-postgres",
            'defaultStorageClass: "longhorn-critical-encrypted"',
            "serviceMonitor:\n    enabled: true",
        )
        invalid_keycloak_env = dict(env, KEYCLOAK_REPLICAS="1")
        with patched_env(invalid_keycloak_env):
            try:
                renderer.render_keycloak(paths["keycloak"], inventory)
            except SystemExit as exc:
                if "KEYCLOAK_REPLICAS must be at least 2" not in str(exc):
                    raise AssertionError(f"unexpected Keycloak replica validation error: {exc}") from exc
            else:
                raise AssertionError("Keycloak renderer accepted a single premium replica")
        unstable_keycloak_env = dict(env, KEYCLOAK_IMAGE_TAG="latest")
        with patched_env(unstable_keycloak_env):
            try:
                renderer.render_keycloak(paths["keycloak"], inventory)
            except SystemExit as exc:
                if "KEYCLOAK_IMAGE_TAG must be a stable release tag" not in str(exc):
                    raise AssertionError(f"unexpected Keycloak image validation error: {exc}") from exc
            else:
                raise AssertionError("Keycloak renderer accepted an unstable image tag")
        legacy_sso_username_path = write(
            repo / "gitops/clusters/rke2-main/premium-3node/apps/keycloak/legacy-sso-values.yaml",
            paths["keycloak"].read_text(encoding="utf-8"),
        )
        legacy_sso_env = dict(env)
        legacy_sso_env.pop("PLATFORM_SSO_BOOTSTRAP_ADMIN_USERNAME")
        legacy_sso_env["PLATFORM_SSO_BOOTSTRAP_USERNAME"] = "legacy-platform-admin"
        with patched_env(legacy_sso_env):
            renderer.render_keycloak(legacy_sso_username_path, inventory)
        assert_contains(
            legacy_sso_username_path,
            '"username": "legacy-platform-admin"',
        )
        invalid_minio_env = dict(env, MINIO_REPLICA_COUNT="3")
        with patched_env(invalid_minio_env):
            try:
                renderer.render_minio(paths["minio"])
            except SystemExit as exc:
                if "distributed MinIO" not in str(exc):
                    raise AssertionError(f"unexpected MinIO replica validation error: {exc}") from exc
            else:
                raise AssertionError("MinIO renderer accepted a non-distributed replica count")
        assert_contains(
            paths["woodpecker"],
            "ci.example.test",
            "woodpecker-oauth-test",
            "woodpecker-agent-test",
        )
        assert_contains(
            paths["woodpecker"],
            'WOODPECKER_HOST: "https://ci.example.test"',
            'WOODPECKER_DATABASE_DRIVER: "postgres"',
            'WOODPECKER_SERVER_ADDR: ":8000"',
            'WOODPECKER_GRPC_ADDR: ":9000"',
            'WOODPECKER_LOG_LEVEL: "info"',
            'WOODPECKER_DEFAULT_PIPELINE_TIMEOUT: "60"',
            'WOODPECKER_MAX_PIPELINE_TIMEOUT: "120"',
            "failureThreshold: 30",
            '"woodpecker-db-test"',
            "createAgentSecret: false",
            "mapAgentSecret: false",
            "replicaCount: 3",
            "repository: woodpeckerci/woodpecker-server",
            "repository: woodpeckerci/woodpecker-agent",
            'tag: "v3.16.0"',
            'WOODPECKER_BACKEND_K8S_STORAGE_CLASS: "longhorn-standard-encrypted"',
            "app.kubernetes.io/name: server",
            "app.kubernetes.io/name: agent",
            "topologySpreadConstraints:\n    - maxSkew: 1",
            "whenUnsatisfiable: DoNotSchedule",
            "name: platform-postgres-ca",
            "name: platform-internal-roots",
            "key: ca-certificates.crt",
            "path: ca-certificates.crt",
            "mountPath: /etc/ssl/platform-postgres",
        )
        invalid_woodpecker_log_env = dict(env, WOODPECKER_LOG_LEVEL="verbose")
        with patched_env(invalid_woodpecker_log_env):
            try:
                renderer.render_woodpecker(paths["woodpecker"], inventory)
            except SystemExit as exc:
                if "WOODPECKER_LOG_LEVEL must be" not in str(exc):
                    raise AssertionError(f"unexpected Woodpecker log-level validation error: {exc}") from exc
            else:
                raise AssertionError("Woodpecker renderer accepted an unsupported log level")
        rendered_woodpecker_text = paths["woodpecker"].read_text(encoding="utf-8")
        if contract_validator.count_yaml_list_scalar(rendered_woodpecker_text, "woodpecker-agent-test") != 2:
            raise AssertionError("rendered Woodpecker values did not map the managed agent Secret into both roles")
        assert_hardened_woodpecker_values(paths["woodpecker"])
        mixed_yaml_scalar_styles = """
- woodpecker-agent-test
- "woodpecker-agent-test"
- 'woodpecker-agent-test'
"""
        if contract_validator.count_yaml_list_scalar(mixed_yaml_scalar_styles, "woodpecker-agent-test") != 3:
            raise AssertionError("platform contract validator rejected a valid quoted YAML Secret list item")
        assert_contains(paths["harbor"], "registry.example.test", "harbor-admin-test")
        assert_contains(
            paths["harbor"],
            'externalURL: "https://registry.example.test"',
            "caBundleSecretName: platform-internal-roots",
            "portal:\n  replicas: 2\n  podDisruptionBudget:",
            "core:\n  replicas: 2\n  podDisruptionBudget:",
            "jobservice:\n  replicas: 2\n  podDisruptionBudget:",
            "jobLoggers:\n    - database",
            "registry:\n  replicas: 2\n  podDisruptionBudget:",
            "trivy:\n  enabled: true\n  replicas: 2\n  podDisruptionBudget:",
            "exporter:\n  replicas: 2\n  podDisruptionBudget:",
            "whenUnsatisfiable: DoNotSchedule",
            "updateStrategy:\n  type: RollingUpdate",
            "persistence:\n  enabled: false",
            "imageChartStorage:\n    disableredirect: true\n    type: s3",
            'existingSecret: "harbor-s3-test"',
            "database:\n  type: external",
            'host: "platform-postgres-rw.platform-databases.svc.cluster.local"',
            'existingSecret: "harbor-db-test"',
            'sslmode: "verify-full"',
            "redis:\n  type: external",
            'addr: "platform-valkey-primary.platform-cache.svc.cluster.local:6379"',
            'existingSecret: "harbor-redis"',
            "tlsOptions:\n      enable: true",
            "extraEnvVars:\n    - name: _REDIS_URL_CORE",
            'name: "harbor-redis-url"',
        )

        external_harbor_path = write(repo / "gitops/clusters/rke2-main/premium-3node/apps/harbor/external-values.yaml")
        external_harbor_env = {
            "HARBOR_STORAGE_CLASS": "longhorn-critical",
            "HARBOR_TLS_CERT_SOURCE": "secret",
            "HARBOR_TLS_SECRET_NAME": "harbor-wildcard-test",
            "HARBOR_DATABASE_MODE": "external",
            "HARBOR_DATABASE_HOST": "harbor-postgres.example.test",
            "HARBOR_DATABASE_PORT": "5432",
            "HARBOR_DATABASE_NAME": "registry",
            "HARBOR_DATABASE_USER": "harbor",
            "HARBOR_DATABASE_SECRET_NAME": "harbor-db-test",
            "HARBOR_REDIS_MODE": "external",
            "HARBOR_REDIS_ADDR": "harbor-redis.example.test:6379",
            "HARBOR_REDIS_USERNAME": "harbor",
            "HARBOR_REDIS_SECRET_NAME": "harbor-redis-test",
            "HARBOR_STORAGE_MODE": "s3",
            "HARBOR_S3_BUCKET": "platform-test-harbor-registry",
            "HARBOR_S3_SECRET_NAME": "harbor-s3-test",
            "OBJECT_STORAGE_ENDPOINT": "https://object.example.test",
            "OBJECT_STORAGE_REGION": "eu-test-1",
        }
        with patched_env(external_harbor_env):
            renderer.render_harbor(external_harbor_path, inventory)
        assert_contains(
            external_harbor_path,
            "Uses external PostgreSQL, external Redis, and S3-compatible registry storage",
            'certSource: "secret"',
            'secretName: "harbor-wildcard-test"',
            "imageChartStorage:\n    disableredirect: true\n    type: s3",
            'bucket: "platform-test-harbor-registry"',
            'regionendpoint: "https://object.example.test"',
            'existingSecret: "harbor-s3-test"',
            "database:\n  type: external",
            'host: "harbor-postgres.example.test"',
            'existingSecret: "harbor-db-test"',
            "redis:\n  type: external",
            'addr: "harbor-redis.example.test:6379"',
            'username: "harbor"',
            'existingSecret: "harbor-redis-test"',
            "tlsOptions:\n      enable: true",
        )
        assert_not_contains(
            external_harbor_path,
            "database:\n  type: internal",
            "redis:\n  type: internal",
            "type: filesystem",
        )

        insecure_harbor_env = dict(external_harbor_env, HARBOR_REDIS_TLS="false")
        with patched_env(insecure_harbor_env):
            try:
                renderer.render_harbor(external_harbor_path, inventory)
            except SystemExit as exc:
                if "HARBOR_REDIS_TLS must be true" not in str(exc):
                    raise AssertionError(f"unexpected Harbor Redis TLS validation error: {exc}") from exc
            else:
                raise AssertionError("Harbor renderer accepted plaintext external Redis in strict mode")

        internal_harbor_path = write(repo / "gitops/clusters/rke2-main/premium-3node/apps/harbor/internal-values.yaml")
        internal_harbor_env = {
            "PLATFORM_PRODUCTION_STRICT": "false",
            "HARBOR_DATABASE_MODE": "internal",
            "HARBOR_REDIS_MODE": "internal",
            "HARBOR_STORAGE_MODE": "filesystem",
        }
        with patched_env(internal_harbor_env):
            renderer.render_harbor(internal_harbor_path, inventory)
        assert_contains(
            internal_harbor_path,
            "Uses internal PostgreSQL, internal Redis, and filesystem registry storage",
            "database:\n  type: internal\n  internal:\n    resources:",
            "redis:\n  type: internal\n  internal:\n    resources:",
            "imageChartStorage:\n    type: filesystem",
        )

        assert_contains(
            paths["monitoring"],
            "crds:\n  enabled: true",
            "grafana.example.test",
            "prometheus.example.test",
            "60Gi",
            "prometheus:\n  podDisruptionBudget:\n    enabled: true\n    minAvailable: 1",
            "prometheusSpec:\n    replicas: 2\n    podAntiAffinity: hard\n    podAntiAffinityTopologyKey: kubernetes.io/hostname",
            "retention: 15d",
            "    resources:\n      requests:\n        cpu: 250m\n        memory: 2Gi",
        )
        assert_contains(
            paths["monitoring"],
            'storageClassName: "longhorn-standard-encrypted"',
            'storage: "60Gi"',
            "alertmanager:\n  enabled: true\n  podDisruptionBudget:\n    enabled: true\n    minAvailable: 2",
            "alertmanagerSpec:\n    useExistingSecret: true\n    configSecret: alertmanager-platform-config\n    replicas: 3\n    podAntiAffinity: hard\n    podAntiAffinityTopologyKey: kubernetes.io/hostname\n    resources:",
            "grafana:\n  replicas: 2\n  deploymentStrategy:\n    type: RollingUpdate",
            "podDisruptionBudget:\n    minAvailable: 1",
            "whenUnsatisfiable: DoNotSchedule",
            'existingSecret: "grafana-admin-test"',
            "userKey: admin-user",
            "passwordKey: admin-password",
            "GF_DATABASE_PASSWORD:",
            "LOKI_GATEWAY_USERNAME:",
            "name: platform-loki-client",
            'name: "grafana-db-test"',
            "grafana.ini:\n    database:\n      type: postgres",
            'ssl_mode: "verify-full"',
            "ca_cert_path: /etc/ssl/platform-postgres/ca-certificates.crt",
            "extraConfigmapMounts:",
            "configMap: platform-internal-roots",
            "persistence:\n    enabled: false",
            'envFromSecret: "platform-sso-grafana"',
            "disable_login_form: true",
            "oauth_auto_login: true",
            "auth.generic_oauth:",
            "role_attribute_strict: true",
            "extraManifests:",
            "name: prometheus-oauth2-proxy",
            "image: quay.io/oauth2-proxy/oauth2-proxy:v7.15.3",
            'name: "platform-sso-prometheus"',
            "name: prometheus-authenticated",
        )

        external_monitoring_path = write(repo / "gitops/clusters/rke2-main/premium-3node/apps/monitoring/external-values.yaml")
        external_monitoring_env = {
            "MONITORING_STORAGE_CLASS": "longhorn-standard",
            "GRAFANA_ADMIN_SECRET_NAME": "grafana-admin-test",
            "GRAFANA_DATABASE_MODE": "postgres",
            "GRAFANA_DATABASE_HOST": "grafana-postgres.example.test",
            "GRAFANA_DATABASE_PORT": "5432",
            "GRAFANA_DATABASE_NAME": "grafana",
            "GRAFANA_DATABASE_USER": "grafana",
            "GRAFANA_DATABASE_SECRET_NAME": "grafana-db-test",
            "GRAFANA_DATABASE_SSL_MODE": "verify-full",
            "GRAFANA_REPLICAS": "2",
        }
        with patched_env(external_monitoring_env):
            renderer.render_monitoring(external_monitoring_path, inventory)
        assert_contains(
            external_monitoring_path,
            "Uses external PostgreSQL for Grafana state",
            "GF_DATABASE_PASSWORD:",
            'name: "grafana-db-test"',
            "grafana.ini:\n    database:\n      type: postgres",
            'host: "grafana-postgres.example.test:5432"',
            'name: "grafana"',
            'user: "grafana"',
            'password: "$__env{GF_DATABASE_PASSWORD}"',
            'ssl_mode: "verify-full"',
            "ca_cert_path: /etc/ssl/platform-postgres/ca-certificates.crt",
            "configMap: platform-internal-roots",
            "persistence:\n    enabled: false",
        )

        sqlite_monitoring_path = write(
            repo / "gitops/clusters/rke2-main/premium-3node/apps/monitoring/sqlite-values.yaml"
        )
        with patched_env(
            {
                "PLATFORM_PRODUCTION_STRICT": "false",
                "GRAFANA_DATABASE_MODE": "sqlite",
                "GRAFANA_REPLICAS": "1",
            }
        ):
            renderer.render_monitoring(sqlite_monitoring_path, inventory)
        assert_contains(
            sqlite_monitoring_path,
            "grafana:\n  replicas: 1\n  admin:",
            "persistence:\n    enabled: true",
            "type: pvc",
        )
        assert_not_contains(sqlite_monitoring_path, "GF_DATABASE_PASSWORD", "type: postgres")
        assert_contains(
            paths["loki"],
            "loki.example.test",
            "platform-test-loki-chunks",
            "loki-object-test",
            "${LOKI_S3_ACCESS_KEY_ID}",
            "${LOKI_S3_SECRET_ACCESS_KEY}",
        )
        assert_contains(
            paths["loki"],
            'endpoint: "https://object.example.test"',
            'chunks: "platform-test-loki-chunks"',
            'storageClass: "longhorn-standard-encrypted"',
            "write:\n  replicas: 3\n  resources:",
            "read:\n  replicas: 3\n  resources:",
            "backend:\n  replicas: 3\n  resources:",
            "gateway:\n  enabled: true\n  replicas: 3\n  basicAuth:\n    enabled: true\n    existingSecret: loki-gateway-basic-auth",
            "retention_period: \"720h\"",
            "retention_enabled: true",
            "locationSnippet: \"proxy_set_header X-Scope-OrgID platform;\"",
            "      cpu: 250m\n      memory: 1Gi",
        )
        loki_text = paths["loki"].read_text(encoding="utf-8")
        if loki_text.count("enableStatefulSetAutoDeletePVC: false") != 2:
            raise AssertionError(
                "rendered Loki values must retain both write and backend StatefulSet claims"
            )
        assert_contains(
            paths["velero"],
            "platform-test-velero",
            "https://object.example.test",
            "velero-cloud-test",
            'schedule: "15 2 * * *"',
            "snapshotMoveData: true",
            "- platform-databases",
            "- woodpecker",
        )
        assert_contains(
            paths["velero"],
            'provider: "aws"',
            'bucket: "platform-test-velero"',
            'existingSecret: "velero-cloud-test"',
            "deployNodeAgent: true\n\nresources:",
            "nodeAgent:\n  resources:",
            "      cpu: 250m\n      memory: 256Mi",
        )
        default_velero_path = write(
            repo / "gitops/clusters/rke2-main/premium-3node/apps/velero/default-values.yaml"
        )
        with patched_env({"BACKUP_PROVIDER": "aws", "PLATFORM_PRODUCTION_STRICT": "false"}):
            renderer.render_velero(default_velero_path)
        assert_contains(
            default_velero_path,
            's3Url: "http://platform-minio.object-storage.svc.cluster.local:9000"',
            'bucket: "platform-velero-backups"',
            'existingSecret: "velero-credentials"',
        )
        assert_contains(
            paths["step_ca"],
            "Platform Test CA",
            "ca.example.test",
            'size: "9Gi"',
            "service:\n  type: ClusterIP\n  port: 443\n  targetPort: 9000",
            "address: :9000",
            "accessModes:\n      - ReadWriteOnce",
            "ssh:\n    enabled: false",
            "autocert:\n  enabled: false",
            "resources:\n  requests:\n    cpu: 100m\n    memory: 256Mi",
        )

        render_real_premium_profile(renderer, checker, env)

        sqlite_woodpecker_path = write(repo / "gitops/clusters/rke2-main/premium-3node/apps/woodpecker/sqlite-values.yaml")
        sqlite_env = {
            "WOODPECKER_DATA_SIZE": "11Gi",
            "WOODPECKER_STORAGE_CLASS": "longhorn-standard",
            "WOODPECKER_ADMIN_USERS": "platform-admin",
            "WOODPECKER_OPEN": "true",
            "WOODPECKER_FORGEJO_OAUTH_SECRET_NAME": "woodpecker-oauth-test",
            "WOODPECKER_IMAGE_TAG": "3.16.0",
            "WOODPECKER_DATABASE_MODE": "sqlite",
            "WOODPECKER_AGENT_REPLICAS": "3",
        }
        with patched_env(sqlite_env):
            renderer.render_woodpecker(sqlite_woodpecker_path, inventory)
        assert_contains(
            sqlite_woodpecker_path,
            'WOODPECKER_HOST: "https://ci.example.test"',
            'WOODPECKER_SERVER_ADDR: ":8000"',
            'WOODPECKER_GRPC_ADDR: ":9000"',
            'WOODPECKER_OPEN: "true"',
            'WOODPECKER_DEFAULT_PIPELINE_TIMEOUT: "60"',
            'WOODPECKER_MAX_PIPELINE_TIMEOUT: "120"',
            "failureThreshold: 30",
            "replicaCount: 1",
            "repository: woodpeckerci/woodpecker-server",
            "repository: woodpeckerci/woodpecker-agent",
            'tag: "v3.16.0"',
            '"woodpecker-oauth-test"',
            '"woodpecker-agent-secret"',
            "createAgentSecret: false",
            "mapAgentSecret: false",
        )
        assert_not_contains(
            sqlite_woodpecker_path,
            'WOODPECKER_DATABASE_DRIVER: "postgres"',
            '"woodpecker-database"',
        )
        assert_hardened_woodpecker_values(sqlite_woodpecker_path)

        invalid_sqlite_env = dict(sqlite_env, WOODPECKER_DATABASE_MODE="sqlite", WOODPECKER_SERVER_REPLICAS="2")
        with patched_env(invalid_sqlite_env):
            try:
                renderer.render_woodpecker(sqlite_woodpecker_path, inventory)
            except SystemExit as exc:
                if "WOODPECKER_SERVER_REPLICAS must be 1" not in str(exc):
                    raise AssertionError(f"unexpected sqlite replica validation error: {exc}") from exc
            else:
                raise AssertionError("SQLite-backed Woodpecker accepted multiple server replicas")

        invalid_image_env = dict(sqlite_env, WOODPECKER_IMAGE_TAG="next")
        with patched_env(invalid_image_env):
            try:
                renderer.render_woodpecker(sqlite_woodpecker_path, inventory)
            except SystemExit as exc:
                if "WOODPECKER_IMAGE_TAG must be a stable release tag" not in str(exc):
                    raise AssertionError(f"unexpected Woodpecker image tag validation error: {exc}") from exc
            else:
                raise AssertionError("Woodpecker renderer accepted a mutable image tag")

        invalid_timeout_env = dict(sqlite_env, WOODPECKER_MAX_PIPELINE_TIMEOUT="59")
        with patched_env(invalid_timeout_env):
            try:
                renderer.render_woodpecker(sqlite_woodpecker_path, inventory)
            except SystemExit as exc:
                if "WOODPECKER_MAX_PIPELINE_TIMEOUT must be greater than or equal" not in str(exc):
                    raise AssertionError(f"unexpected Woodpecker timeout validation error: {exc}") from exc
            else:
                raise AssertionError("Woodpecker renderer accepted a maximum timeout below the default")

    print("Private platform values renderer self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
