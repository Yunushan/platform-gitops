#!/usr/bin/env python3
"""Behavior-test private atomic artifact writes."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import stat
import tempfile
from unittest import mock

from atomic_file import PRIVATE_FILE_MODE, atomic_binary_output, atomic_write_bytes, atomic_write_text


ROOT = Path(__file__).resolve().parents[1]
ATOMIC_ARTIFACT_PRODUCERS = (
    "scripts/capture_live_image_inventory.py",
    "scripts/configure_github_governance.py",
    "scripts/create_production_approval.py",
    "scripts/forge_cutover.py",
    "scripts/forge_migration.py",
    "scripts/forge_migration_live.py",
    "scripts/forge_transition.py",
    "scripts/reconcile_image_inventory.py",
    "scripts/render_deployable_gitops_apps.py",
    "scripts/render_private_platform_values.py",
    "scripts/synthetic_private_profile.py",
    "scripts/validate_platform_contract.py",
    "scripts/validate_rendered_manifests.py",
    "scripts/validate_synthetic_private_profile.py",
    "scripts/verify_active_kyverno_policies.py",
    "scripts/verify_github_governance.py",
    "scripts/verify_github_release_approval.py",
    "scripts/verify_github_release_ref.py",
    "scripts/verify_production_readiness_score.py",
)


def assert_no_temporary_files(directory: Path, destination: Path) -> None:
    leftovers = list(directory.glob(f".{destination.name}.*.tmp"))
    if leftovers:
        raise AssertionError(f"atomic writer left temporary files behind: {leftovers}")


def production_python_files() -> list[Path]:
    scripts = ROOT / "scripts"
    return sorted(
        path
        for path in scripts.rglob("*.py")
        if not path.name.startswith("test_")
    )


def direct_file_write(node: ast.Call) -> bool:
    if isinstance(node.func, ast.Attribute) and node.func.attr in {
        "write_text",
        "write_bytes",
    }:
        return True

    mode_index: int
    if isinstance(node.func, ast.Name) and node.func.id == "open":
        mode_index = 1
    elif isinstance(node.func, ast.Attribute) and node.func.attr == "open":
        if isinstance(node.func.value, ast.Name) and node.func.value.id == "os":
            return False
        if isinstance(node.func.value, ast.Call) and not (
            isinstance(node.func.value.func, ast.Name)
            and node.func.value.func.id == "Path"
        ):
            return False
        keywords = {keyword.arg for keyword in node.keywords}
        if "timeout" in keywords and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "opener":
            return False
        if isinstance(node.func.value, ast.Name) and node.func.value.id in {"gzip", "tarfile", "zipfile"}:
            # Archive mode follows its filename. With only fileobj the
            # stream was opened separately and that call is checked.
            if not node.args and "fileobj" in keywords and "name" not in keywords:
                return False
            mode_index = 1
        else:
            mode_index = 0
    else:
        return False

    mode_node: ast.expr | None = None
    if len(node.args) > mode_index:
        mode_node = node.args[mode_index]
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode_node = keyword.value
            break
    if mode_node is None:
        return False
    if not isinstance(mode_node, ast.Constant) or not isinstance(mode_node.value, str):
        return True
    return any(flag in mode_node.value for flag in "wax+")


def assert_direct_write_detection() -> None:
    for source in (
        'Path("output").write_text("value")',
        'open("output", "wb")',
        'path.open("a")',
        'Path("output").open(mode="r+")',
        'gzip.open("output", "wb")',
        'tarfile.open("output", mode="w:gz")',
    ):
        document = ast.parse(source)
        calls = [node for node in ast.walk(document) if isinstance(node, ast.Call)]
        if not any(direct_file_write(node) for node in calls):
            raise AssertionError(f"direct local write escaped static detection: {source}")

    for source in (
        'open("input", "rb")',
        'gzip.open(path, "rb")',
        'tarfile.open(path, "r|gz")',
        'tarfile.open(fileobj=output, mode="w")',
        'self.opener.open(request, timeout=10)',
        'archive.open(info, mode="r")',
    ):
        call = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call))
        if direct_file_write(call):
            raise AssertionError(f"non-writing file or stream open classified as local output: {source}")


def assert_production_writes_use_shared_policy() -> None:
    direct_writes: list[str] = []
    atomic_writes = 0
    for path in production_python_files():
        document = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(document):
            if not isinstance(node, ast.Call):
                continue
            if direct_file_write(node):
                direct_writes.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            if isinstance(node.func, ast.Name) and node.func.id == "atomic_write_text":
                atomic_writes += 1

    if direct_writes:
        raise AssertionError("direct production file writes remain:\n" + "\n".join(direct_writes))
    if atomic_writes < 30:
        raise AssertionError(f"atomic file scan covered too few calls: {atomic_writes}")


def main() -> int:
    assert_direct_write_detection()
    assert_production_writes_use_shared_policy()
    with tempfile.TemporaryDirectory(prefix="platform-atomic-file-") as temporary_name:
        root = Path(temporary_name)
        destination = root / "nested" / "evidence.json"

        atomic_write_text(destination, '{"result":"passed"}\n')
        if destination.read_text(encoding="utf-8") != '{"result":"passed"}\n':
            raise AssertionError("atomic writer did not preserve exact text")
        if os.name == "posix" and stat.S_IMODE(destination.stat().st_mode) != PRIVATE_FILE_MODE:
            raise AssertionError("atomic writer did not apply owner-only permissions")
        assert_no_temporary_files(destination.parent, destination)

        binary = root / "archive"
        with atomic_binary_output(binary, replace=False) as stream:
            stream.write(b"\x00\xff")
            if binary.exists():
                raise AssertionError("incomplete archive was published")
        try:
            atomic_write_bytes(binary, b"second", replace=False)
        except FileExistsError:
            pass
        else:
            raise AssertionError("create-only write replaced the original artifact")
        if binary.read_bytes() != b"\x00\xff":
            raise AssertionError("create-only write damaged the original artifact")
        assert_no_temporary_files(binary.parent, binary)

        destination.write_text("retained\n", encoding="utf-8")
        with mock.patch("atomic_file.os.replace", side_effect=OSError("simulated replace failure")):
            try:
                atomic_write_text(destination, "partial\n")
            except OSError as exc:
                if "simulated replace failure" not in str(exc):
                    raise
            else:
                raise AssertionError("atomic writer ignored a replacement failure")
        if destination.read_text(encoding="utf-8") != "retained\n":
            raise AssertionError("failed atomic write damaged the prior artifact")
        assert_no_temporary_files(destination.parent, destination)

    for relative_path in ATOMIC_ARTIFACT_PRODUCERS:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        if "from atomic_file import atomic_write_text" not in source:
            raise AssertionError(f"{relative_path} does not import the shared atomic writer")
        if "atomic_write_text(" not in source:
            raise AssertionError(f"{relative_path} does not use the shared atomic writer")

    production_runner = (
        ROOT / "scripts/bootstrap/run-platform-production-evidence.sh"
    ).read_text(encoding="utf-8")
    for control in (
        "umask 077",
        "from scripts.atomic_file import atomic_write_text",
        "atomic_write_text(",
    ):
        if control not in production_runner:
            raise AssertionError(
                "production evidence runner is missing private atomic output control: "
                f"{control}"
            )

    print("Private atomic artifact writer self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
