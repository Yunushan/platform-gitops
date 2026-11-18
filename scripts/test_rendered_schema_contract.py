#!/usr/bin/env python3
"""Validate the rendered Kubernetes schema gate contract."""

from __future__ import annotations

import hashlib
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_rendered_crd_schemas import CatalogTest
import validate_rendered_manifests as validator
import validate_synthetic_private_profile as synthetic_validator
from validate_rendered_manifests import application_sources


ROOT = Path(__file__).resolve().parents[1]


def read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"required rendered-schema file is missing: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} is missing {needle!r}")


def test_missing_schema_outcomes() -> None:
    native_schema = ROOT / "schemas/kubernetes/v1.36.2/apiextensions.k8s.io/v1/customresourcedefinition.json"
    digest = hashlib.sha256(native_schema.read_bytes()).hexdigest()
    if digest != "a6a10a35fad1ba50bff9e7ce51bc775278d865e44dabd5cc6ce5bf66ec2a28c0":
        raise AssertionError("the shipped native CRD schema differs from the reviewed derivation")
    outcomes = {"v1/Serivce": 1, "apps/v1/Deploymnt": 1, "rbac.authorization.k8s.io/v1/Rloe": 1,
                "monitoring.coreos.com/v1/ServiceMonitor": 2, "gateway.networking.k8s.io/v1/Gateway": 1}
    rows = []
    for gvk, count in outcomes.items():
        version, kind = gvk.rsplit("/", 1)
        rows.extend([{"status": "statusSkipped", "kind": kind, "version": version, "name": "private"}] * count)
    counts = {"valid": 0, "invalid": 0, "errors": 0, "skipped": len(rows)}
    report = validator.summarize_schema_report(json.dumps({"resources": rows, "summary": counts}))
    assert report["skippedByGVK"] == outcomes and not report["resources"]
    assert set(validator.missing_native_schemas(report["skippedByGVK"])) == {
        "v1/Serivce", "apps/v1/Deploymnt", "rbac.authorization.k8s.io/v1/Rloe",
    }
    try:
        validator.summarize_schema_report(json.dumps({"resources": [], "summary": counts}))
    except ValueError:
        pass
    else:
        raise AssertionError("a non-verbose report concealed missing schemas")


def test_kubeconform_skip_gate() -> None:
    binary = shutil.which(os.environ.get("KUBECONFORM_BIN", "kubeconform"))
    if binary is None:
        return  # The always-on contract above also runs on hosts without the CLI.
    run_tool = validator.run
    with tempfile.TemporaryDirectory(prefix="kubeconform-gate-") as temp:
        root = Path(temp)
        source = root / "gitops" / "fixture"
        source.mkdir(parents=True)
        schema = {"type": "object", "properties": {"data": {"type": "object"}}}
        (root / "configmap.json").write_text(json.dumps(schema), encoding="utf-8")
        args = SimpleNamespace(profile=["test"], require_complete=True, kubernetes_version="1.36.2",
                               output_dir="rendered/schema-validation")

        def fixture_run(command, *, env, root):
            if command[1] == "build":
                return subprocess.CompletedProcess(command, 0, manifest, "")
            local = command.copy()
            # Keep the native versioned fallback, replace the network default.
            local[local.index("default")] = str(root / "{{.ResourceKind}}.json")
            return run_tool(local, env=env, root=root)

        for profile, api, kind, data, should_pass in (
            ("test", "v1", "ConfigMap", "{}", True), ("test", "v1", "ConfigMap", "[]", False),
            ("test", "v1", "ConfigMpa", "{}", False), ("test", "apps/v1", "Deploymnt", "{}", False),
            ("test", "networking.k8s.io/v1", "NetworkPolciy", "{}", False),
            ("test", "monitoring.coreos.com/v1", "ServiceMonitor", "{}", True),
            ("premium-3node", "monitoring.coreos.com/v1", "ServiceMonitor", "{}", False),
        ):
            args.profile = [profile]
            manifest = f"apiVersion: {api}\nkind: {kind}\nmetadata:\n  name: private-marker\ndata: {data}\n"
            with patch.object(validator, "required_tool", return_value=binary), \
                 patch.object(validator, "application_sources", return_value=[("fixture", source)]), \
                 patch.object(validator, "run", side_effect=fixture_run), \
                 io.StringIO() as log, redirect_stdout(log), redirect_stderr(log):
                assert (validator.validate(args, root=root) == 0) == should_pass, (api, kind, data, log.getvalue())
            output = root / args.output_dir
            evidence = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            skips = {} if kind == "ConfigMap" else {f"{api}/{kind}": 1}
            assert evidence["skippedSchemasByProfile"] == {profile: skips}
            record = (evidence["rendered"] or evidence["failures"])[0]
            assert record["skippedByGVK"] == skips
            if should_pass:
                assert "private-marker" not in (root / record["report"]).read_text(encoding="utf-8")

        crd = {
            "apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
            "metadata": {"name": "widgets.example.test", "creationTimestamp": None},
            "spec": {"group": "example.test", "scope": "Namespaced",
                     "names": {"plural": "widgets", "kind": "Widget"},
                     "versions": [{"name": "v1", "served": True, "storage": True,
                                   "schema": {"openAPIV3Schema": {"type": "object"}}}]},
        }
        manifest_path = root / "native-crd.json"
        command = [binary, "-strict", "-summary", "-verbose", "-output", "json",
                   "-kubernetes-version", "1.36.2", "-schema-location", validator.NATIVE_SCHEMA_LOCATION,
                   str(manifest_path)]
        for should_pass in (True, False):
            if not should_pass:
                crd["spec"]["versions"][0]["storagge"] = True
            manifest_path.write_text(json.dumps(crd), encoding="utf-8")
            result = run_tool(command, env=os.environ.copy(), root=root)
            totals = validator.summarize_schema_report(result.stdout)["summary"]
            assert (result.returncode == 0) == should_pass and totals["skipped"] == 0
            assert totals["valid" if should_pass else "invalid"] == 1


def test_sanitized_artifact_retention() -> None:
    with tempfile.TemporaryDirectory(prefix="platform-rendered-artifact-test-") as temporary:
        root = Path(temporary)
        schema_output = root / "source"
        reports = schema_output / "reports"
        reports.mkdir(parents=True)
        (reports / "app.render.log").write_text("stdout_sha256=test\n", encoding="utf-8")
        (reports / "app.kubeconform.json").write_text("{}\n", encoding="utf-8")
        raw_manifests = schema_output / "manifests"
        raw_manifests.mkdir()
        (raw_manifests / "secret.yaml").write_text("kind: Secret\n", encoding="utf-8")
        summary = {
            "skippedSchemasByProfile": {"test": {"example.test/v1/Widget": 1}},
            "rendered": [
                {
                    "application": "app",
                    "report": "rendered/schema-validation/reports/app.kubeconform.json",
                    "imageReferences": ["registry.example.test/private/app:v1"],
                    "skippedByGVK": {"example.test/v1/Widget": 1},
                }
            ],
            "skipped": [],
            "failures": [],
        }
        destination = root / "retained"
        synthetic_validator.retain_sanitized_artifacts(
            schema_output,
            destination,
            summary,
        )
        retained = json.loads(
            (destination / "schema-validation/summary.json").read_text(encoding="utf-8")
        )
        if retained.get("artifactPolicy") != "sanitized-reports-only":
            raise AssertionError("retained schema summary omitted its artifact policy")
        if retained["rendered"][0].get("report") != "reports/app.kubeconform.json":
            raise AssertionError("retained schema summary contains a temporary report path")
        if "imageReferences" in retained["rendered"][0]:
            raise AssertionError("private image references crossed the retained artifact boundary")
        if retained["skippedSchemasByProfile"]["test"] != retained["rendered"][0]["skippedByGVK"]:
            raise AssertionError("private artifact sanitization changed the schema skip evidence")
        if (destination / "schema-validation/manifests").exists():
            raise AssertionError("raw schema manifests crossed the retained artifact boundary")

        (reports / "unexpected.yaml").write_text("kind: Secret\n", encoding="utf-8")
        try:
            synthetic_validator.retain_sanitized_artifacts(
                schema_output,
                destination,
                summary,
            )
        except RuntimeError as exc:
            if "unexpected rendered-schema report artifact" not in str(exc):
                raise
        else:
            raise AssertionError("unexpected rendered-schema report artifact was retained")


def main() -> int:
    catalog_tests = unittest.defaultTestLoader.loadTestsFromTestCase(CatalogTest)
    if not unittest.TextTestRunner().run(catalog_tests).wasSuccessful():
        return 1
    test_missing_schema_outcomes()
    test_kubeconform_skip_gate()
    test_sanitized_artifact_retention()
    base_sources = dict(application_sources("base", ROOT))
    if base_sources != dict(application_sources("default", ROOT)):
        raise AssertionError("the base schema profile must select the default applications")
    expected_gitlab_sources = dict(application_sources("premium-3node", ROOT))
    del expected_gitlab_sources["forgejo"], expected_gitlab_sources["woodpecker"]
    premium_apps = ROOT / "gitops/clusters/rke2-main/premium-3node/apps"
    expected_gitlab_sources.update(
        {name: premium_apps / name for name in ("gitlab-ce", "gitlab-runner")}
    )
    if dict(application_sources("premium-3node-gitlab-ce", ROOT)) != expected_gitlab_sources:
        raise AssertionError("the GitLab schema profile must include the same applications as deployment")

    validator = read(ROOT / "scripts/validate_rendered_manifests.py")
    synthetic_fixture = read(ROOT / "scripts/synthetic_private_profile.py")
    synthetic_validator = read(ROOT / "scripts/validate_synthetic_private_profile.py")
    workflow = read(ROOT / ".github/workflows/validate.yml")
    release = read(ROOT / ".github/workflows/release.yml")
    makefile = read(ROOT / "Makefile")
    supply_chain = read(ROOT / "docs/SUPPLY_CHAIN.md")
    readiness = read(ROOT / "docs/PRODUCTION_READINESS.md")

    for needle in (
        "required_tool(\"KUSTOMIZE_BIN\", \"kustomize\")",
        "required_tool(\"KUBECONFORM_BIN\", \"kubeconform\")",
        "required_tool(\"HELM_BIN\", \"helm\")",
        'shutil.copytree(root / "gitops"',
        '"--enable-helm"',
        '"--helm-command"',
        '"--helm-kube-version"',
        '"LoadRestrictionsNone"',
        '"-strict"',
        '"-ignore-missing-schemas"',
        '"-kubernetes-version"',
        'DEFAULT_KUBERNETES_SCHEMA_VERSION = "1.36.2"',
        'manifests_dir = temporary_root / "manifests"',
        "stdout_sha256=",
        '"manifestSha256"',
        "PLATFORM_RENDERED_SCHEMA_ALLOW_INCOMPLETE",
        '"--require-complete"',
        '"--repo-root"',
        "unresolved-placeholders",
        "summary.json",
        "rendered schema verification produced no validated applications",
    ):
        require(validator, needle, "rendered schema validator")

    for needle in (
        "prepare_synthetic_private_profile",
        'platform_argocd_host=argocd.example.test',
        '"OBJECT_STORAGE_ENDPOINT": "https://object.example.test"',
        '"STEP_CA_MODE": "bootstrap"',
        '"PLATFORM_IMAGE_INTEGRITY_MODE": "Audit"',
        "sanitized_environment",
    ):
        require(synthetic_fixture, needle, "synthetic premium fixture")

    for needle in (
        "profile=[profile]",
        "require_complete=True",
        'output_dir="rendered/schema-validation"',
        "application_sources(profile, repo_root)",
        "rendered != expected",
        'tempfile.TemporaryDirectory(prefix="platform-synthetic-private-schema-")',
        "retain_sanitized_artifacts",
        'retained["artifactPolicy"] = "sanitized-reports-only"',
        "unexpected rendered-schema report artifact",
        "synthetic_private_profile=passed",
    ):
        require(synthetic_validator, needle, "synthetic premium validator")

    for needle in (
        "scripts/bootstrap/install-ci-tools.sh",
        "kustomize helm kubeconform",
        "PLATFORM_RENDERED_SCHEMA_PROFILES: base,premium-3node",
        "PLATFORM_RENDERED_SCHEMA_ALLOW_INCOMPLETE: \"true\"",
        "make rendered-schema-verify",
        "make rendered-private-schema-verify",
        "rendered/schema-validation",
        "rendered/synthetic-private-schema/schema-validation",
        "rendered/synthetic-gitlab-schema/schema-validation",
    ):
        require(workflow, needle, "GitHub validation workflow")

    gitlab_gate = (
        "- name: Validate complete synthetic GitLab schemas\n"
        "        env:\n"
        "          PLATFORM_PROFILE: premium-3node-gitlab-ce\n"
        "        run: make rendered-private-schema-verify"
    )
    require(workflow, gitlab_gate, "GitHub validation workflow")
    require(release, gitlab_gate, "GitHub signed release workflow")

    require(makefile, "rendered-schema-verify:", "Makefile")
    require(makefile, "rendered-private-schema-verify:", "Makefile")
    require(
        makefile,
        "platform-image-inventory-verify: override export PLATFORM_RENDERED_SCHEMA_INCLUDE_IMAGE_REFERENCES := true",
        "image inventory prerequisite",
    )
    require(
        makefile,
        "platform-image-inventory-verify: rendered-schema-verify rendered-private-schema-verify supply-chain-verify",
        "production readiness gate",
    )
    require(supply_chain, "make rendered-schema-verify", "supply-chain documentation")
    require(supply_chain, "make rendered-private-schema-verify", "supply-chain documentation")
    require(readiness, "make rendered-schema-verify", "production readiness documentation")
    require(readiness, "make rendered-private-schema-verify", "production readiness documentation")

    print("Rendered Kubernetes schema validation contract passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
