#!/usr/bin/env python3
"""Validate supply-chain helper examples for Renovate and Cosign/Kyverno."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import tempfile

from vendored_chart_inventory import (
    chart_package_record,
    chart_tree_sha256,
    refresh_inventory,
    validate_inventory,
    verify_package_directory,
)


ROOT = Path(__file__).resolve().parents[1]
RENOVATE = ROOT / "renovate.json"
COSIGN_POLICY = ROOT / "policies/kyverno/verify-signed-images.example.yaml"
POLICY_README = ROOT / "policies/README.md"
README = ROOT / "README.md"
ARCHITECTURE = ROOT / "docs/ARCHITECTURE.md"
PREMIUM = ROOT / "docs/PREMIUM_3NODE.md"
INSTALLATION = ROOT / "docs/INSTALLATION.md"
SUPPLY_CHAIN = ROOT / "docs/SUPPLY_CHAIN.md"
TROUBLESHOOTING = ROOT / "docs/TROUBLESHOOTING.md"
EVIDENCE_VALIDATOR = ROOT / "scripts/verify_supply_chain_evidence.py"
EVIDENCE_TEST = ROOT / "scripts/test_supply_chain_evidence.py"
IMAGE_INVENTORY_TEST = ROOT / "scripts/test_image_inventory_evidence.py"
IMAGE_INVENTORY_RECONCILER = ROOT / "scripts/reconcile_image_inventory.py"
IMAGE_INVENTORY_VERIFIER = ROOT / "scripts/verify_image_inventory_evidence.py"
IMAGE_INVENTORY_WRAPPER = ROOT / "scripts/bootstrap/run-platform-image-inventory.sh"
POSTURE_SCRIPT = ROOT / "scripts/supply-chain-posture.sh"
SECURITY_SCAN_SCRIPT = ROOT / "scripts/security-scan.sh"
GITHUB_VALIDATION = ROOT / ".github/workflows/validate.yml"
GITHUB_RELEASE = ROOT / ".github/workflows/release.yml"
SCORECARD_WORKFLOW = ROOT / ".github/workflows/scorecard.yml"
VENDORED_CHART_PROVENANCE_WORKFLOW = (
    ROOT / ".github/workflows/vendored-chart-provenance.yml"
)
MAKEFILE = ROOT / "Makefile"
GITHUB_WORKFLOWS = ROOT / ".github/workflows"
GITLEAKS_CONFIG = ROOT / ".gitleaks.toml"
SEMGREP_CONFIG = ROOT / ".semgrep.yml"
SEMGREP_IGNORE = ROOT / ".semgrepignore"
NO_SECRETS_SCANNER = ROOT / "scripts/validate_no_secrets.py"
VENDORED_CHART_INVENTORY = ROOT / "config/vendored-charts.json"
VENDORED_CHART_INVENTORY_HELPER = ROOT / "scripts/vendored_chart_inventory.py"
RKE2_BOOTSTRAP_SCRIPTS = (
    ROOT / "scripts/bootstrap/install-rke2-first-server.sh",
    ROOT / "scripts/bootstrap/install-rke2-server.sh",
)
KYVERNO_CLI_INSTALLER = ROOT / "scripts/bootstrap/install-kyverno-cli.sh"
ARGOCD_BOOTSTRAP = ROOT / "ansible/playbooks/bootstrap-argocd.yml"
INGRESS_BOOTSTRAP = ROOT / "ansible/playbooks/deploy-platform-ingress.yml"
LONGHORN_BOOTSTRAP = ROOT / "ansible/playbooks/bootstrap-longhorn.yml"
LONGHORN_CRD_REPAIR = ROOT / "ansible/playbooks/repair-longhorn-crds.yml"
LONGHORN_CHART_SOURCE = ROOT / "gitops/clusters/rke2-main/premium-3node/apps/longhorn/charts/longhorn-1.12.0/longhorn"
LONGHORN_CHART_ARCHIVE = ROOT / "gitops/clusters/rke2-main/premium-3node/apps/longhorn/charts/longhorn-1.12.0/longhorn-1.12.0.tgz"
LONGHORN_CHART_ARCHIVE_MAX_BYTES = 1 * 1024 * 1024
LONGHORN_CHART_EXPANDED_MAX_BYTES = 4 * 1024 * 1024
LONGHORN_CHART_MEMBER_MAX = 128
METALLB_CHART_SOURCE = ROOT / "gitops/clusters/rke2-main/apps/metallb/charts/metallb-0.16.1/metallb"
METALLB_CHART_ARCHIVE = ROOT / "gitops/clusters/rke2-main/apps/metallb/charts/metallb-0.16.1/metallb-0.16.1.tgz"
TRAEFIK_CHART_SOURCE = ROOT / "gitops/clusters/rke2-main/premium-3node/apps/traefik/charts/traefik-41.0.1/traefik"
TRAEFIK_CHART_ARCHIVE = ROOT / "gitops/clusters/rke2-main/premium-3node/apps/traefik/charts/traefik-41.0.1/traefik-41.0.1.tgz"
METALLB_KUSTOMIZATION = ROOT / "gitops/clusters/rke2-main/apps/metallb/kustomization.yaml"
TRAEFIK_KUSTOMIZATION = ROOT / "gitops/clusters/rke2-main/premium-3node/apps/traefik/kustomization.yaml"
INGRESS_CHART_ARCHIVE_MAX_BYTES = 1 * 1024 * 1024
INGRESS_CHART_EXPANDED_MAX_BYTES = 8 * 1024 * 1024
INGRESS_CHART_MEMBER_MAX = 512


def fail(message: str) -> int:
    print(f"Supply-chain helper validation failed: {message}", file=sys.stderr)
    return 1


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AssertionError(f"missing required file: {path.relative_to(ROOT)}")


def load_renovate() -> dict[str, object]:
    try:
        data = json.loads(read(RENOVATE))
    except json.JSONDecodeError as exc:
        raise AssertionError(f"renovate.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AssertionError("renovate.json must contain a JSON object")
    return data


def assert_contains(text: str, *needles: str, label: str) -> None:
    for needle in needles:
        if needle not in text:
            raise AssertionError(f"{label} is missing required text: {needle}")


def validate_vendored_chart_inventory_contract() -> list[str]:
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="platform-chart-inventory-") as directory:
        root = Path(directory)
        chart_root = root / "charts" / "demo"
        chart_root.mkdir(parents=True)
        chart_files = {
            "Chart.yaml": b"apiVersion: v2\nname: demo\nversion: 1.2.3\n",
            "values.yaml": b"replicaCount: 1\n",
        }
        for relative, data in chart_files.items():
            (chart_root / relative).write_bytes(data)

        package_directory = root / "packages"
        package_directory.mkdir()
        package_path = package_directory / "demo-1.2.3.tgz"
        with package_path.open("wb") as output:
            with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as archive:
                    for relative, data in sorted(chart_files.items()):
                        member = tarfile.TarInfo(f"demo/{relative}")
                        member.size = len(data)
                        member.mtime = 0
                        member.mode = 0o644
                        member.uid = 0
                        member.gid = 0
                        member.uname = ""
                        member.gname = ""
                        archive.addfile(member, io.BytesIO(data))
        package_record = chart_package_record(package_path)

        inventory_path = root / "inventory.json"
        entry = {
            "path": "charts/demo",
            "repository": "https://charts.example.test/stable",
            "name": "stale",
            "version": "0.0.0",
            "packageSha256": package_record["packageSha256"],
            "upstreamTreeSha256": package_record["upstreamTreeSha256"],
            "treeSha256": chart_tree_sha256(chart_root),
            "patches": [],
        }
        inventory_path.write_text(
            json.dumps({"schemaVersion": 2, "charts": [entry]}, indent=2) + "\n",
            encoding="utf-8",
        )

        refresh_problems = refresh_inventory(root=root, inventory_path=inventory_path)
        if refresh_problems:
            problems.append(
                "vendored chart inventory refresh rejected a valid fixture: "
                + "; ".join(refresh_problems)
            )
            return problems
        refreshed = json.loads(inventory_path.read_text(encoding="utf-8"))
        refreshed_entry = refreshed["charts"][0]
        if refreshed_entry["name"] != "demo" or refreshed_entry["version"] != "1.2.3":
            problems.append("vendored chart inventory refresh did not use Chart.yaml metadata")
        if refreshed_entry["treeSha256"] != chart_tree_sha256(chart_root):
            problems.append("vendored chart inventory refresh did not bind the chart tree digest")
        if refreshed_entry["packageSha256"] != package_record["packageSha256"]:
            problems.append("vendored chart inventory refresh discarded the package digest")
        if refreshed_entry["upstreamTreeSha256"] != package_record["upstreamTreeSha256"]:
            problems.append("vendored chart inventory refresh discarded the upstream tree digest")

        valid_problems = validate_inventory(
            root=root,
            inventory_path=inventory_path,
            expected_paths={"charts/demo"},
        )
        if valid_problems:
            problems.append(
                "vendored chart inventory rejected a refreshed fixture: "
                + "; ".join(valid_problems)
            )
        package_problems = verify_package_directory(
            root=root,
            inventory_path=inventory_path,
            package_directory=package_directory,
        )
        if package_problems:
            problems.append(
                "vendored chart inventory rejected its exact upstream package: "
                + "; ".join(package_problems)
            )

        invalid_provenance = json.loads(json.dumps(refreshed))
        invalid_provenance["charts"][0]["packageSha256"] = "invalid"
        invalid_text = json.dumps(invalid_provenance)
        inventory_path.write_text(invalid_text, encoding="utf-8")
        invalid_refresh_problems = refresh_inventory(
            root=root,
            inventory_path=inventory_path,
        )
        if not any(
            "packageSha256 must be a lowercase SHA-256 digest" in problem
            for problem in invalid_refresh_problems
        ):
            problems.append("vendored chart refresh accepted invalid package provenance")
        if inventory_path.read_text(encoding="utf-8") != invalid_text:
            problems.append("failed vendored chart refresh changed the inventory")
        inventory_path.write_text(json.dumps(refreshed), encoding="utf-8")

        refreshed["charts"][0]["version"] = "1.2.4"
        inventory_path.write_text(json.dumps(refreshed), encoding="utf-8")
        version_problems = validate_inventory(root=root, inventory_path=inventory_path)
        if not any("does not match Chart.yaml" in problem for problem in version_problems):
            problems.append("vendored chart inventory accepted a version-only update")

        refreshed["charts"][0]["version"] = "1.2.3"
        inventory_path.write_text(json.dumps(refreshed), encoding="utf-8")
        (chart_root / "values.yaml").write_text("replicaCount: 2\n", encoding="utf-8")
        digest_problems = validate_inventory(root=root, inventory_path=inventory_path)
        if not any("treeSha256 does not match" in problem for problem in digest_problems):
            problems.append("vendored chart inventory accepted modified chart content")

        (chart_root / "values.yaml").write_text("replicaCount: 1\n", encoding="utf-8")
        refreshed["charts"][0]["treeSha256"] = chart_tree_sha256(chart_root)
        missing_problems = validate_inventory(
            root=root,
            inventory_path=inventory_path,
            expected_paths={"charts/demo", "charts/missing"},
        )
        if not any("consumed local chart is missing" in problem for problem in missing_problems):
            problems.append("vendored chart inventory accepted an unlisted local consumer")

        escaping = json.loads(json.dumps(refreshed))
        escaping["charts"][0]["path"] = "../outside"
        inventory_path.write_text(json.dumps(escaping), encoding="utf-8")
        escaping_problems = validate_inventory(root=root, inventory_path=inventory_path)
        if not any(
            "normalized, relative, and non-escaping" in problem
            for problem in escaping_problems
        ):
            problems.append("vendored chart inventory accepted an escaping chart path")

        linked_chart = root / "charts" / "linked-demo"
        try:
            linked_chart.symlink_to(chart_root, target_is_directory=True)
        except (NotImplementedError, OSError):
            pass
        else:
            linked = json.loads(json.dumps(refreshed))
            linked["charts"][0]["path"] = "charts/linked-demo"
            inventory_path.write_text(json.dumps(linked), encoding="utf-8")
            linked_problems = validate_inventory(root=root, inventory_path=inventory_path)
            if not any(
                "symbolic link or junction" in problem
                for problem in linked_problems
            ):
                problems.append("vendored chart inventory accepted a linked chart path")
            linked_chart.unlink()

        refreshed["charts"][0]["repository"] = "http://charts.example.test/stable"
        inventory_path.write_text(json.dumps(refreshed), encoding="utf-8")
        repository_problems = validate_inventory(root=root, inventory_path=inventory_path)
        if not any("HTTPS or OCI" in problem for problem in repository_problems):
            problems.append("vendored chart inventory accepted an insecure source URL")

        patched = json.loads(json.dumps(refreshed))
        patched["charts"][0]["repository"] = "https://charts.example.test/stable"
        (chart_root / "values.yaml").write_text("replicaCount: 2\n", encoding="utf-8")
        patched["charts"][0]["treeSha256"] = chart_tree_sha256(chart_root)
        patched["charts"][0]["patches"] = [
            {
                "path": "values.yaml",
                "reason": "Exercise an explicitly reviewed local chart override.",
            }
        ]
        inventory_path.write_text(json.dumps(patched), encoding="utf-8")
        patch_problems = verify_package_directory(
            root=root,
            inventory_path=inventory_path,
            package_directory=package_directory,
        )
        if patch_problems:
            problems.append(
                "vendored chart package verification rejected a declared patch: "
                + "; ".join(patch_problems)
            )

        undeclared = json.loads(json.dumps(patched))
        undeclared["charts"][0]["patches"] = []
        inventory_path.write_text(json.dumps(undeclared), encoding="utf-8")
        undeclared_refresh_problems = refresh_inventory(
            root=root,
            inventory_path=inventory_path,
        )
        if not any(
            "local tree differs from upstream but declares no patches" in problem
            for problem in undeclared_refresh_problems
        ):
            problems.append("vendored chart refresh accepted an undeclared patch")
        undeclared_problems = verify_package_directory(
            root=root,
            inventory_path=inventory_path,
            package_directory=package_directory,
        )
        if not any(
            "undeclared local chart patches" in problem
            for problem in undeclared_problems
        ):
            problems.append("vendored chart package verification accepted an undeclared patch")

        tampered = json.loads(json.dumps(patched))
        tampered["charts"][0]["packageSha256"] = "0" * 64
        inventory_path.write_text(json.dumps(tampered), encoding="utf-8")
        tampered_problems = verify_package_directory(
            root=root,
            inventory_path=inventory_path,
            package_directory=package_directory,
        )
        if not any("packageSha256 does not match" in problem for problem in tampered_problems):
            problems.append("vendored chart package verification accepted a changed package")

        unsafe_package = root / "unsafe.tgz"
        with unsafe_package.open("wb") as output:
            with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as archive:
                    data = b"escape"
                    member = tarfile.TarInfo("demo/../escape")
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
        try:
            chart_package_record(unsafe_package)
        except ValueError as exc:
            if "unsafe member path" not in str(exc):
                problems.append(
                    "vendored chart archive traversal produced an unclear error"
                )
        else:
            problems.append("vendored chart package inspection accepted path traversal")

        inventory_path.write_text(
            '{"schemaVersion":2,"schemaVersion":2,"charts":[]}',
            encoding="utf-8",
        )
        duplicate_problems = validate_inventory(root=root, inventory_path=inventory_path)
        if not any("duplicate JSON object keys" in problem for problem in duplicate_problems):
            problems.append("vendored chart inventory accepted duplicate JSON keys")
    return problems


def validate_vendored_chart_archive(
    *,
    archive_path: Path,
    source_path: Path,
    consumer_path: Path,
    chart_root: str,
    archive_max_bytes: int,
    expanded_max_bytes: int,
    member_max: int,
) -> list[str]:
    problems: list[str] = []
    label = str(archive_path.relative_to(ROOT))
    try:
        archive_size = archive_path.stat().st_size
        if not 0 < archive_size <= archive_max_bytes:
            return [
                f"{label} must be non-empty and no larger than "
                f"{archive_max_bytes} bytes"
            ]
        archive_bytes = archive_path.read_bytes()
        archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
        if archive_sha256 not in read(consumer_path):
            problems.append(
                f"{label} SHA-256 {archive_sha256} is not pinned by "
                f"{consumer_path.relative_to(ROOT)}"
            )

        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = archive.getmembers()
            if not 0 < len(members) <= member_max:
                problems.append(
                    f"{label} must contain 1 through "
                    f"{member_max} members"
                )
                return problems

            member_names: set[str] = set()
            expanded_size = 0
            for member in members:
                member_path = PurePosixPath(member.name)
                if (
                    member_path.is_absolute()
                    or ".." in member_path.parts
                    or "\\" in member.name
                    or len(member_path.parts) < 2
                    or member_path.parts[0] != chart_root
                ):
                    problems.append(f"{label} has unsafe member path: {member.name}")
                    continue
                if member.name in member_names:
                    problems.append(f"{label} has duplicate member: {member.name}")
                    continue
                member_names.add(member.name)
                if not member.isfile():
                    problems.append(
                        f"{label} may contain only regular files: {member.name}"
                    )
                    continue
                if member.size < 0 or member.size > expanded_max_bytes:
                    problems.append(
                        f"{label} member exceeds the expansion bound: {member.name}"
                    )
                    continue
                expanded_size += member.size
                if expanded_size > expanded_max_bytes:
                    problems.append(
                        f"{label} expands beyond "
                        f"{expanded_max_bytes} bytes"
                    )
                    break

                relative_source = Path(*member_path.parts[1:])
                member_source_path = source_path / relative_source
                if not member_source_path.is_file():
                    problems.append(
                        f"{label} member has no vendored source: {member.name}"
                    )
                    continue
                if member_source_path.stat().st_size != member.size:
                    problems.append(
                        f"{label} member size differs from vendored source: "
                        f"{member.name}"
                    )
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    problems.append(f"{label} cannot read member: {member.name}")
                    continue
                payload = stream.read(expanded_max_bytes + 1)
                if len(payload) != member.size:
                    problems.append(
                        f"{label} member size is inconsistent: {member.name}"
                    )
                    continue
                if payload != member_source_path.read_bytes():
                    problems.append(
                        f"{label} member differs from vendored source: {member.name}"
                    )

            required_members = {
                f"{chart_root}/{path.relative_to(source_path).as_posix()}"
                for path in source_path.rglob("*")
                if path.is_file()
            }
            missing = sorted(required_members - member_names)
            if missing:
                problems.append(
                    f"{label} omits deployable vendored chart members: "
                    + ", ".join(missing)
                )
    except (FileNotFoundError, OSError, tarfile.TarError) as exc:
        problems.append(f"cannot validate {label}: {exc}")
    return problems


def validate_longhorn_chart_archive() -> list[str]:
    return validate_vendored_chart_archive(
        archive_path=LONGHORN_CHART_ARCHIVE,
        source_path=LONGHORN_CHART_SOURCE,
        consumer_path=LONGHORN_BOOTSTRAP,
        chart_root="longhorn",
        archive_max_bytes=LONGHORN_CHART_ARCHIVE_MAX_BYTES,
        expanded_max_bytes=LONGHORN_CHART_EXPANDED_MAX_BYTES,
        member_max=LONGHORN_CHART_MEMBER_MAX,
    )


def main() -> int:
    problems: list[str] = []

    problems.extend(validate_vendored_chart_inventory_contract())
    problems.extend(validate_longhorn_chart_archive())
    problems.extend(
        validate_vendored_chart_archive(
            archive_path=METALLB_CHART_ARCHIVE,
            source_path=METALLB_CHART_SOURCE,
            consumer_path=INGRESS_BOOTSTRAP,
            chart_root="metallb",
            archive_max_bytes=INGRESS_CHART_ARCHIVE_MAX_BYTES,
            expanded_max_bytes=INGRESS_CHART_EXPANDED_MAX_BYTES,
            member_max=INGRESS_CHART_MEMBER_MAX,
        )
    )
    problems.extend(
        validate_vendored_chart_archive(
            archive_path=TRAEFIK_CHART_ARCHIVE,
            source_path=TRAEFIK_CHART_SOURCE,
            consumer_path=INGRESS_BOOTSTRAP,
            chart_root="traefik",
            archive_max_bytes=INGRESS_CHART_ARCHIVE_MAX_BYTES,
            expanded_max_bytes=INGRESS_CHART_EXPANDED_MAX_BYTES,
            member_max=INGRESS_CHART_MEMBER_MAX,
        )
    )

    try:
        argocd_bootstrap_text = read(ARGOCD_BOOTSTRAP)
        assert_contains(
            argocd_bootstrap_text,
            "platform_argocd_vendored_chart_metadata",
            "platform_argocd_core_manifest_sha256",
            "b0f9119821f2e19b852c842b9cb235eb9c3ef1549554fbda6aa5904e8d440eae",
            "platform_argocd_ha_manifest_sha256",
            "278787c5f36b790ab0338d5b30d4a3fec3fddb532bf0d12f78a8977c06ecea80",
            "Download, verify, and apply Argo CD bootstrap manifest",
            "Download, verify, and apply core Argo CD fallback manifest",
            "--proto '=https'",
            "--connect-timeout",
            "--max-time",
            "--max-filesize",
            "sha256sum --check --strict",
            label=str(ARGOCD_BOOTSTRAP.relative_to(ROOT)),
        )
        for forbidden in (
            "PLATFORM_ARGOCD_MANIFEST_URL",
            "/stable/manifests/",
            "-f {{ platform_argocd_manifest_url_effective }}",
            "curl --fail --show-error --silent --location",
        ):
            if forbidden in argocd_bootstrap_text:
                problems.append(
                    f"{ARGOCD_BOOTSTRAP.relative_to(ROOT)} contains unsafe "
                    f"Argo CD bootstrap artifact behavior: {forbidden}"
                )
        if argocd_bootstrap_text.count("sha256sum --check --strict") != 2:
            problems.append(
                f"{ARGOCD_BOOTSTRAP.relative_to(ROOT)} must verify both the "
                "selected and fallback Argo CD manifests"
            )
    except AssertionError as exc:
        problems.append(str(exc))

    for playbook, required in (
        (
            LONGHORN_BOOTSTRAP,
            (
                "platform_longhorn_vendored_chart_archive_sha256",
                "Verify vendored Longhorn chart archive",
                "chartContent:",
                "platform_longhorn_chart_archive.content",
                "Render vendored Longhorn CRDs",
                "- helm",
                "- template",
                "platform_longhorn_render_kube_version",
                "--kube-version",
                "--show-only",
                "templates/crds.yaml",
                "base64 --decode",
                "sha256sum --check --strict",
                "helm upgrade --install platform-longhorn /chart/longhorn.tgz",
            ),
        ),
        (
            LONGHORN_CRD_REPAIR,
            (
                "platform_longhorn_vendored_chart_path",
                "Render Longhorn CRDs from vendored chart",
                "helm",
                "template",
                "platform_longhorn_render_kube_version",
                "--kube-version",
                "--show-only",
                "templates/crds.yaml",
                "Restore missing Longhorn CRDs from vendored chart",
            ),
        ),
    ):
        try:
            playbook_text = read(playbook)
            assert_contains(
                playbook_text,
                *required,
                label=str(playbook.relative_to(ROOT)),
            )
            for forbidden in (
                "PLATFORM_LONGHORN_CHART_REPO",
                "PLATFORM_LONGHORN_CRD_MANIFEST_URL",
                "raw.githubusercontent.com/longhorn",
                "curl -fsSL",
            ):
                if forbidden in playbook_text:
                    problems.append(
                        f"{playbook.relative_to(ROOT)} must not use runtime "
                        f"Longhorn artifact input: {forbidden}"
                    )
        except AssertionError as exc:
            problems.append(str(exc))

    try:
        ingress_bootstrap_text = read(INGRESS_BOOTSTRAP)
        assert_contains(
            ingress_bootstrap_text,
            "platform_metallb_vendored_chart_archive_sha256",
            "fb06bb584fcb7856f15733b2a6a2aff5b61b5c350687e341c163ae24a5938adc",
            "platform_traefik_vendored_chart_archive_sha256",
            "150f5c608f2d25eaa292d306470cbfd1b0681d67d88da5985433354f716c5a7f",
            "Verify vendored platform ingress chart archives",
            "platform_metallb_chart_archive.content",
            "platform_traefik_chart_archive.content",
            "failurePolicy: abort",
            "platform_traefik_chart_repo_dns_check_effective",
            label=str(INGRESS_BOOTSTRAP.relative_to(ROOT)),
        )
        for forbidden in (
            "PLATFORM_METALLB_CHART_REPO",
            "repo: {{ platform_metallb_chart_repo_effective }}",
            "repo: {{ platform_traefik_chart_repo_effective }}",
            "chart: metallb",
            "chart: traefik",
        ):
            if forbidden in ingress_bootstrap_text:
                problems.append(
                    f"{INGRESS_BOOTSTRAP.relative_to(ROOT)} contains unsafe "
                    f"runtime ingress chart input: {forbidden}"
                )
        if ingress_bootstrap_text.count("chartContent:") < 2:
            problems.append(
                f"{INGRESS_BOOTSTRAP.relative_to(ROOT)} must embed both "
                "reviewed ingress chart archives"
            )
        if "platform-ingress: platform-dns-repair" in read(MAKEFILE):
            problems.append(
                "platform-ingress must not depend on external chart-repository "
                "DNS diagnostics when using local chartContent"
            )
        for path, chart_home, chart_repo in (
            (METALLB_KUSTOMIZATION, "charts/metallb-0.16.1", "https://metallb.github.io/metallb"),
            (TRAEFIK_KUSTOMIZATION, "charts/traefik-41.0.1", "https://traefik.github.io/charts"),
        ):
            kustomization_text = read(path)
            assert_contains(
                kustomization_text,
                "helmGlobals:",
                f"chartHome: {chart_home}",
                label=str(path.relative_to(ROOT)),
            )
            if chart_repo in kustomization_text:
                problems.append(
                    f"{path.relative_to(ROOT)} must render its reviewed local chart"
                )
    except AssertionError as exc:
        problems.append(str(exc))

    for script in RKE2_BOOTSTRAP_SCRIPTS:
        try:
            script_text = read(script)
            label = str(script.relative_to(ROOT))
            assert_contains(
                script_text,
                ': "${RKE2_VERSION:?',
                ': "${RKE2_INSTALL_SCRIPT_SHA256:?',
                "umask 077",
                'export INSTALL_RKE2_TYPE="server"',
                'export INSTALL_RKE2_VERSION="${RKE2_VERSION}"',
                "unset INSTALL_RKE2_CHANNEL",
                "10#${timeout_value}",
                "--proto '=https'",
                "--proto-redir '=https'",
                "sha256sum --check --strict",
                'chmod 0700 "${installer}"',
                "mktemp /etc/rancher/rke2/.config.yaml.XXXXXX",
                'chmod 0600 "${config_tmp}"',
                'mv -f -- "${config_tmp}" /etc/rancher/rke2/config.yaml',
                "quoted_cluster_credential=",
                label=label,
            )
            for insecure_pattern in (
                'export INSTALL_RKE2_TYPE="${INSTALL_RKE2_TYPE:-server}"',
                "cat >/etc/rancher/rke2/config.yaml",
                "curl -sfL https://get.rke2.io |",
            ):
                if insecure_pattern in script_text:
                    problems.append(
                        f"{label} retains insecure manual bootstrap pattern: "
                        f"{insecure_pattern}"
                    )

            checksum_index = script_text.index("sha256sum --check --strict")
            config_install_index = script_text.index(
                'mv -f -- "${config_tmp}" /etc/rancher/rke2/config.yaml'
            )
            execution_index = script_text.index(
                'timeout "${RKE2_INSTALL_TIMEOUT}" "${installer}"'
            )
            if not checksum_index < config_install_index < execution_index:
                problems.append(
                    f"{label} must verify the installer before installing config "
                    "and executing it"
                )
        except (AssertionError, ValueError) as exc:
            problems.append(str(exc))

    try:
        kyverno_installer_text = read(KYVERNO_CLI_INSTALLER)
        assert_contains(
            kyverno_installer_text,
            'version="1.18.1"',
            'sha256="5e6bba9ca85beec6c93e94ca7fb0972a66df3b2e67636a08bef090cd3fc6535c"',
            "umask 077",
            "max_archive_bytes=$((64 * 1024 * 1024))",
            "mktemp -d",
            "trap cleanup EXIT",
            "--proto '=https'",
            "--proto-redir '=https'",
            '--max-filesize "${max_archive_bytes}"',
            "sha256sum --check --strict",
            "--no-same-owner --no-same-permissions",
            'target_tmp="$(mktemp',
            'mv -f -- "${target_tmp}" "${target_dir}/kyverno"',
            label=str(KYVERNO_CLI_INSTALLER.relative_to(ROOT)),
        )
        for unsafe_pattern in (
            'archive="${download_dir}/${archive_name}"',
            'tar --extract --gzip --file "${archive}" --directory "${target_dir}"',
            'chmod 0755 "${target_dir}/kyverno"',
        ):
            if unsafe_pattern in kyverno_installer_text:
                problems.append(
                    f"{KYVERNO_CLI_INSTALLER.relative_to(ROOT)} retains unsafe "
                    f"artifact handling: {unsafe_pattern}"
                )
    except AssertionError as exc:
        problems.append(str(exc))

    try:
        installation_text = read(INSTALLATION)
        assert_contains(
            installation_text,
            "Manual bootstrap scripts always require an exact RKE2 release",
            "RKE2_INSTALL_SCRIPT_SHA256=<REVIEWED_INSTALLER_SHA256>",
            "`/etc/rancher/rke2/config.yaml`",
            "atomically with mode `0600`",
            label=str(INSTALLATION.relative_to(ROOT)),
        )
        if installation_text.count(
            "RKE2_INSTALL_SCRIPT_SHA256=<REVIEWED_INSTALLER_SHA256>"
        ) < len(RKE2_BOOTSTRAP_SCRIPTS):
            problems.append(
                "docs/INSTALLATION.md must show the reviewed installer digest "
                "for both manual RKE2 bootstrap commands"
            )
    except AssertionError as exc:
        problems.append(str(exc))

    for workflow in sorted(GITHUB_WORKFLOWS.glob("*.yml")):
        if "runs-on: ubuntu-latest" in read(workflow):
            problems.append(
                f"{workflow.relative_to(ROOT)} must pin the Ubuntu runner release"
            )

    try:
        gitleaks_text = read(GITLEAKS_CONFIG)
        assert_contains(
            gitleaks_text,
            'description = "Longhorn dm-crypt cipher algorithm constant"',
            'targetRules = ["generic-api-key"]',
            "aes-xts-plain64",
            "^ansible/playbooks/configure-platform-app-secrets[.]yml$",
            'description = "Literal-ellipsis private-key examples in exact vendored chart documentation"',
            'condition = "AND"',
            'targetRules = ["private-key"]',
            'regexTarget = "match"',
            ".*[.][.][.].*",
            "apps/step-ca/charts/step-certificates-1[.]30[.]1/",
            "premium-3node/apps/argocd-ha/charts/argo-cd-10[.]0[.]0/",
            "premium-3node/apps/keycloak/charts/keycloak-25[.]2[.]0/",
            label=str(GITLEAKS_CONFIG.relative_to(ROOT)),
        )
        if "paths = [\n  '''.*charts" in gitleaks_text:
            problems.append(".gitleaks.toml must not broadly allow every vendored chart path")
    except AssertionError as exc:
        problems.append(str(exc))
    try:
        semgrep_text = read(SEMGREP_CONFIG)
        assert_contains(
            semgrep_text,
            "id: shell-curl-pipe-shell",
            "id: kubernetes-latest-image-tag",
            "id: kubernetes-privileged-container",
            '        - "**/*.yaml"',
            '        - "**/*.yml"',
            "longhorn/charts/longhorn-1.12.0/longhorn/templates/daemonset-sa.yaml",
            "longhorn/charts/longhorn-1.12.0/longhorn/templates/preupgrade-job.yaml",
            "longhorn/charts/longhorn-1.12.0/longhorn/templates/psp.yaml",
            "tetragon/charts/tetragon-1.6.0/tetragon/values.yaml",
            label=str(SEMGREP_CONFIG.relative_to(ROOT)),
        )
        if '        - "**/charts/**"' in semgrep_text:
            problems.append(".semgrep.yml must not broadly exclude vendored charts")

        semgrep_ignored = {
            line.strip()
            for line in read(SEMGREP_IGNORE).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        expected_ignored = {".git/", "private/", "rendered/", "secrets/"}
        if semgrep_ignored != expected_ignored:
            problems.append(
                ".semgrepignore must contain only the reviewed generated/private paths; "
                f"expected={sorted(expected_ignored)} actual={sorted(semgrep_ignored)}"
            )
    except AssertionError as exc:
        problems.append(str(exc))

    try:
        no_secrets_text = read(NO_SECRETS_SCANNER)
        assert_contains(
            no_secrets_text,
            "vendored_document_exceptions = {",
            "traefik-41.0.1/traefik/Changelog.md",
            "traefik-41.0.1/traefik/EXAMPLES.md",
            "if rel_posix in vendored_document_exceptions:",
            label=str(NO_SECRETS_SCANNER.relative_to(ROOT)),
        )
        for broad_exception in (
            "if '/charts/' in rel_posix and rel.suffix == '.md'",
            "if '/charts/' in rel_posix and rel.suffix in {'.md'",
            'if "/charts/" in rel_posix and rel.suffix == ".md"',
            'if "/charts/" in rel_posix and rel.suffix in {".md"',
        ):
            if broad_exception in no_secrets_text:
                problems.append(
                    "scripts/validate_no_secrets.py must not broadly exempt "
                    "vendored Markdown files"
                )
                break
    except AssertionError as exc:
        problems.append(str(exc))
    inventory: dict[str, object] = {}
    try:
        assert_contains(
            read(VENDORED_CHART_INVENTORY_HELPER),
            "read_bounded_stream",
            "read_bounded_text",
            "loads_strict_json",
            "CHART_TREE_MAX_BYTES",
            "CHART_TREE_MAX_FILES",
            "CHART_PACKAGE_MAX_MEMBERS",
            "followlinks=False",
            "_is_link_like",
            "not stat.S_ISREG",
            "parsed.scheme not in {\"https\", \"oci\"}",
            "packageSha256",
            "upstreamTreeSha256",
            "verify_package_directory",
            "--verify-upstream",
            "run_bounded",
            "atomic_write_text",
            "mode=0o644",
            label=str(VENDORED_CHART_INVENTORY_HELPER.relative_to(ROOT)),
        )
        inventory = json.loads(read(VENDORED_CHART_INVENTORY))
        if inventory.get("schemaVersion") != 2 or not inventory.get("charts"):
            problems.append(
                "config/vendored-charts.json must contain schemaVersion 2 and chart entries"
            )
    except (AssertionError, json.JSONDecodeError) as exc:
        problems.append(str(exc))
    try:
        provenance_workflow_text = read(VENDORED_CHART_PROVENANCE_WORKFLOW)
        assert_contains(
            provenance_workflow_text,
            "name: vendored-chart-provenance",
            "pull_request:",
            "push:",
            "schedule:",
            "workflow_dispatch:",
            "gitops/clusters/rke2-main/**/charts/**",
            "permissions:\n  contents: read",
            "runs-on: ubuntu-24.04",
            "timeout-minutes: 30",
            "actions/checkout@08c6903cd8c0fde910a37f88322edcfb5dd907a8",
            "actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c",
            "go install helm.sh/helm/v3/cmd/helm@v3.21.0",
            "python scripts/vendored_chart_inventory.py",
            "--verify-upstream",
            label=str(VENDORED_CHART_PROVENANCE_WORKFLOW.relative_to(ROOT)),
        )
    except AssertionError as exc:
        problems.append(str(exc))
    try:
        renovate = load_renovate()
    except AssertionError as exc:
        problems.append(str(exc))
        renovate = {}

    expected_schema = "https://docs.renovatebot.com/renovate-schema.json"
    if renovate.get("$schema") != expected_schema:
        problems.append("renovate.json must use the official Renovate JSON schema")
    extends = renovate.get("extends")
    if not isinstance(extends, list) or "config:recommended" not in extends:
        problems.append("renovate.json must extend config:recommended")
    if renovate.get("dependencyDashboard") is not True:
        problems.append("renovate.json must enable the dependency dashboard")
    if renovate.get("automerge") is not False:
        problems.append("renovate.json must keep automerge disabled by default")
    if not isinstance(renovate.get("prConcurrentLimit"), int) or int(renovate["prConcurrentLimit"]) < 1:
        problems.append("renovate.json must set a positive prConcurrentLimit")
    if not isinstance(renovate.get("prHourlyLimit"), int) or int(renovate["prHourlyLimit"]) < 1:
        problems.append("renovate.json must set a positive prHourlyLimit")

    custom_managers = renovate.get("customManagers", [])
    vendored_manager: dict[str, object] | None = None
    semgrep_manager: dict[str, object] | None = None
    if isinstance(custom_managers, list):
        for manager in custom_managers:
            if not isinstance(manager, dict):
                continue
            manager_patterns = manager.get("managerFilePatterns", [])
            match_strings = manager.get("matchStrings", [])
            if (
                manager.get("customType") == "regex"
                and manager.get("datasourceTemplate") == "helm"
                and isinstance(manager_patterns, list)
                and any(
                    "vendored-charts" in pattern
                    for pattern in manager_patterns
                    if isinstance(pattern, str)
                )
                and isinstance(match_strings, list)
                and any(
                    all(
                        capture in match_string
                        for capture in (
                            "(?<registryUrl>",
                            "(?<depName>",
                            "(?<currentValue>",
                        )
                    )
                    for match_string in match_strings
                    if isinstance(match_string, str)
                )
            ):
                vendored_manager = manager
            if (
                manager.get("customType") == "regex"
                and manager.get("datasourceTemplate") == "docker"
                and isinstance(manager_patterns, list)
                and any(
                    ".github" in pattern and "workflows" in pattern
                    for pattern in manager_patterns
                    if isinstance(pattern, str)
                )
                and isinstance(match_strings, list)
                and any(
                    all(
                        capture in match_string
                        for capture in (
                            "SEMGREP_IMAGE",
                            "(?<depName>",
                            "(?<currentValue>",
                            "(?<currentDigest>",
                        )
                    )
                    for match_string in match_strings
                    if isinstance(match_string, str)
                )
            ):
                semgrep_manager = manager
    if vendored_manager is None:
        problems.append(
            "renovate.json must discover repository, name, and version from "
            "config/vendored-charts.json with the Helm datasource"
        )
    elif inventory:
        match_strings = vendored_manager.get("matchStrings", [])
        assert isinstance(match_strings, list)
        candidate_patterns = [
            pattern
            for pattern in match_strings
            if isinstance(pattern, str)
            and all(
                capture in pattern
                for capture in (
                    "(?<registryUrl>",
                    "(?<depName>",
                    "(?<currentValue>",
                )
            )
        ]
        try:
            renovate_pattern = candidate_patterns[0]
            python_pattern = re.sub(
                r"\(\?<([A-Za-z][A-Za-z0-9_]*)>",
                r"(?P<\1>",
                renovate_pattern,
            )
            matches = list(
                re.finditer(
                    python_pattern,
                    read(VENDORED_CHART_INVENTORY),
                    flags=re.DOTALL,
                )
            )
        except (IndexError, re.error) as exc:
            problems.append(f"Renovate vendored-chart regex is not executable: {exc}")
        else:
            chart_entries = inventory.get("charts", [])
            if isinstance(chart_entries, list):
                expected = [
                    (
                        entry.get("repository"),
                        entry.get("name"),
                        entry.get("version"),
                    )
                    for entry in chart_entries
                    if isinstance(entry, dict)
                ]
                actual = [
                    (
                        match.group("registryUrl"),
                        match.group("depName"),
                        match.group("currentValue"),
                    )
                    for match in matches
                ]
                if actual != expected:
                    problems.append(
                        "Renovate vendored-chart regex must extract every reviewed "
                        "repository, name, and version exactly once and in order"
                    )

    if semgrep_manager is None:
        problems.append(
            "renovate.json must discover the Semgrep image version and digest "
            "from GitHub validation and release workflows"
        )
    else:
        match_strings = semgrep_manager.get("matchStrings", [])
        assert isinstance(match_strings, list)
        candidate_patterns = [
            pattern
            for pattern in match_strings
            if isinstance(pattern, str)
            and all(
                capture in pattern
                for capture in (
                    "SEMGREP_IMAGE",
                    "(?<depName>",
                    "(?<currentValue>",
                    "(?<currentDigest>",
                )
            )
        ]
        try:
            semgrep_pattern = re.sub(
                r"\(\?<([A-Za-z][A-Za-z0-9_]*)>",
                r"(?P<\1>",
                candidate_patterns[0],
            )
            matches = [
                match
                for workflow in (GITHUB_VALIDATION, GITHUB_RELEASE)
                for match in re.finditer(semgrep_pattern, read(workflow))
            ]
        except (IndexError, re.error) as exc:
            problems.append(f"Renovate Semgrep image regex is not executable: {exc}")
        else:
            actual = [
                (
                    match.group("depName"),
                    match.group("currentValue"),
                    match.group("currentDigest"),
                )
                for match in matches
            ]
            expected = [
                (
                    "semgrep/semgrep",
                    "1.171.0",
                    "sha256:bdf7013b2c3634a487671158da77c554f531742326b543a9464d2adf6c433ac8",
                )
            ] * 2
            if actual != expected:
                problems.append(
                    "Renovate Semgrep image regex must extract both reviewed "
                    "workflow version/digest references exactly once"
                )

    rules = renovate.get("packageRules", [])
    if not isinstance(rules, list) or not rules:
        problems.append("renovate.json must define packageRules")
        rules = []
    if not any(
        isinstance(rule, dict)
        and "docker" in rule.get("matchDatasources", [])
        and rule.get("pinDigests") is True
        for rule in rules
    ):
        problems.append("renovate.json must pin Docker/container image digests")
    if not any(
        isinstance(rule, dict)
        and "helm" in rule.get("matchDatasources", [])
        and "helm" in str(rule.get("groupName", "")).lower()
        for rule in rules
    ):
        problems.append("renovate.json must group Helm chart updates")
    if not any(
        isinstance(rule, dict)
        and "major" in rule.get("matchUpdateTypes", [])
        and rule.get("dependencyDashboardApproval") is True
        for rule in rules
    ):
        problems.append("renovate.json must require dashboard approval for major updates")

    policy_text = read(COSIGN_POLICY)
    for needle in (
        "apiVersion: policies.kyverno.io/v1",
        "kind: ImageValidatingPolicy",
        "name: verify-signed-platform-images",
        "background:\n      enabled: true",
        "webhookConfiguration:",
        "failurePolicy: Fail",
        "matchImageReferences:",
        "image.registry == '<REGISTRY>'",
        "validationActions:",
        "- Audit",
        "mutateDigest: true",
        "required: true",
        "verifyDigest: true",
        "attestors:",
        "<COSIGN_PUBLIC_KEY>",
        "https://rekor.sigstore.dev",
        "insecureIgnoreTlog: false",
        "verifyImageSignatures(image, [attestors.approvedCosignKey])",
    ):
        if needle not in policy_text:
            problems.append(f"{COSIGN_POLICY.relative_to(ROOT)} is missing required text: {needle}")
    if "validationActions:\n    - Deny" in policy_text:
        problems.append("Cosign/Kyverno policy example must not default image verification to Deny")

    for path, required in (
        (
            EVIDENCE_VALIDATOR,
            (
                "validate_sbom",
                "validate_scorecard",
                "validate_signature_report",
                "strict evidence requires an OpenSSF Scorecard report",
                "strict evidence requires a Cosign signature report",
                "@sha256:",
            ),
        ),
        (
            EVIDENCE_TEST,
            (
                "Supply-chain evidence validator self-test passed.",
                "below-threshold Scorecard",
                "tag-only Cosign image",
                "empty SBOM",
            ),
        ),
        (
            IMAGE_INVENTORY_TEST,
            (
                "Rendered/live image inventory reconciliation self-test passed.",
                "unsigned private-registry image",
                "outside-registry image",
                "expired image exception",
            ),
        ),
        (
            IMAGE_INVENTORY_RECONCILER,
            (
                "rendered images were neither observed live nor resolved by exception",
                "image coverage is incomplete",
                "vulnerability report hash does not match",
                "must expire within 90 days",
            ),
        ),
        (
            IMAGE_INVENTORY_VERIFIER,
            (
                "private-registry image lacks signature or admission coverage",
                "outside-registry image lacks an admission-scope exception",
                "Image inventory evidence accepted:",
            ),
        ),
        (
            IMAGE_INVENTORY_WRAPPER,
            (
                "capture-platform-image-inventory.yml",
                "reconcile_image_inventory.py",
                "verify_image_inventory_evidence.py",
            ),
        ),
        (
            POSTURE_SCRIPT,
            (
                "SUPPLY_CHAIN_STRICT",
                "SUPPLY_CHAIN_MIN_SCORE",
                "COSIGN_IMAGES_FILE",
                "verify_supply_chain_evidence.py",
                "Strict supply-chain evidence did not verify any images.",
                "\nPath(sys.argv[2]).write_text(",
            ),
        ),
        (
            SECURITY_SCAN_SCRIPT,
            (
                "gitleaks_args=(\n  dir",
                'gitleaks_args+=("${ROOT}")',
                "trivy_args=(\n  fs",
                "semgrep",
            ),
        ),
        (
            GITHUB_VALIDATION,
            (
                "anchore/sbom-action/download-syft@",
                "gitleaks/gitleaks-action@",
                "SEMGREP_IMAGE: semgrep/semgrep:1.171.0@sha256:",
                "docker run --rm",
                "--network none",
                "--read-only",
                "requirements/ci-coverage.txt",
                "--require-hashes",
                "aquasecurity/trivy-action@",
                "github.com/rhysd/actionlint/cmd/actionlint@v1.7.12",
                "verify_supply_chain_evidence.py",
            ),
        ),
        (
            SCORECARD_WORKFLOW,
            (
                "permissions: read-all",
                "contents: read",
                "runs-on: ubuntu-24.04",
                "ossf/scorecard-action@",
                "publish_results: true",
                "github/codeql-action/upload-sarif@",
            ),
        ),
        (
            MAKEFILE,
            (
                "supply-chain-verify: security-scan",
                "SUPPLY_CHAIN_STRICT=true bash scripts/supply-chain-posture.sh",
                "platform-image-inventory-verify: rendered-schema-verify rendered-private-schema-verify supply-chain-verify",
                "@$(MAKE) platform-image-inventory-verify",
            ),
        ),
    ):
        try:
            assert_contains(read(path), *required, label=str(path.relative_to(ROOT)))
        except AssertionError as exc:
            problems.append(str(exc))

    if "gitleaks_args=(\n  detect" in read(SECURITY_SCAN_SCRIPT):
        problems.append("security-scan.sh must use the supported gitleaks dir command")
    posture_text = read(POSTURE_SCRIPT)
    if "\n  Path(sys.argv[2]).write_text(" in posture_text:
        problems.append("supply-chain-posture.sh contains an invalid indented top-level Python statement")
    try:
        embedded_python = posture_text.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
        compile(embedded_python, str(POSTURE_SCRIPT), "exec")
    except (IndexError, SyntaxError) as exc:
        problems.append(f"supply-chain-posture.sh embedded Python is invalid: {exc}")

    for path, text, required in (
        (
            POLICY_README,
            read(POLICY_README),
            (
                "kyverno/verify-signed-images.example.yaml",
                "Cosign",
                "Renovate",
                "renovate.json",
            ),
        ),
        (
            README,
            read(README),
            (
                "Cosign + Renovate supply-chain helpers",
                "renovate.json",
                "verify-signed-images.example.yaml",
            ),
        ),
        (
            ARCHITECTURE,
            read(ARCHITECTURE),
            ("Cosign", "Renovate", "image signature", "dependency update"),
        ),
        (
            PREMIUM,
            read(PREMIUM),
            ("renovate.json", "Cosign", "verify-signed-images.example.yaml", "pinDigests"),
        ),
        (
            SUPPLY_CHAIN,
            read(SUPPLY_CHAIN),
            (
                "direct Longhorn bootstrap and CRD recovery paths",
                "offline artifact",
                "HelmChart `chartContent`",
                "chart-repository and CRD-manifest URL overrides are rejected",
                "platform ingress bootstrap follows the same offline model",
                "MetalLB",
                "`0.16.1` and Traefik `41.0.1`",
                "chart-repository DNS probes remain explicit diagnostics",
                "active premium profile resolves every Helm chart from committed local",
                "zero chart-repository network dependency",
                "`config/vendored-charts.json`",
                "deterministic SHA-256",
                "exact upstream `.tgz` SHA-256",
                "complete changed-path set",
                "version-only Renovate change intentionally fails validation",
                "--inspect-package",
                "python scripts/vendored_chart_inventory.py --refresh",
                "--verify-packages",
                "make vendored-chart-provenance-verify",
                ".github/workflows/vendored-chart-provenance.yml",
                "Argo CD bootstrap derives its application release",
                "reviewed SHA-256 in the playbook",
                "HA-to-core fallback enforces the same policy",
            ),
        ),
        (
            TROUBLESHOOTING,
            read(TROUBLESHOOTING),
            (
                "reviewed chart archive",
                "Longhorn source",
                "verifies its pinned SHA-256",
                "Runtime overrides such as a remote CRD",
                "manifest URL are intentionally unsupported",
                "checksum-verified local chart",
                "chart-repository check is now an explicit diagnostic",
                "HelmChart uses embedded `chartContent`",
                "exact Argo CD release recorded by the vendored",
                "HA-to-core fallback verifies its separate core manifest",
            ),
        ),
    ):
        try:
            assert_contains(text, *required, label=str(path.relative_to(ROOT)))
        except AssertionError as exc:
            problems.append(str(exc))

    if problems:
        for problem in problems:
            print(f" - {problem}", file=sys.stderr)
        return 1

    print(
        "Supply-chain helper validation passed for verified Argo CD and manual "
        "RKE2 bootstrap, offline Longhorn and ingress charts, vendored chart "
        "inventory, CI scans, narrowed Gitleaks exceptions, SBOM evidence, "
        "Scorecard, Renovate, and Cosign."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
