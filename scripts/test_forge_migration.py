#!/usr/bin/env python3
"""Self-test the forge migration proof helper with local Git repositories."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_SCRIPT = ROOT / "scripts" / "forge_migration.py"


def run(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed rc={result.returncode}: {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def git(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], cwd=cwd, check=check)


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def create_source_repo(root: Path) -> Path:
    work = root / "source-work"
    bare = root / "source.git"
    git(["init", str(work)])
    git(["config", "user.email", "migration-test@example.invalid"], cwd=work)
    git(["config", "user.name", "Migration Test"], cwd=work)
    (work / "README.md").write_text("# migration test\n", encoding="utf-8")
    git(["add", "README.md"], cwd=work)
    git(["commit", "-m", "initial"], cwd=work)
    git(["checkout", "-b", "feature/migration-proof"], cwd=work)
    (work / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(["add", "feature.txt"], cwd=work)
    git(["commit", "-m", "feature"], cwd=work)
    git(["tag", "v1.0.0"], cwd=work)
    git(["checkout", "master"], cwd=work)
    git(["clone", "--bare", str(work), str(bare)])
    return bare


def test_mirror_migration() -> None:
    with tempfile.TemporaryDirectory(prefix="forge-migration-test-") as temp:
        root = Path(temp)
        source = create_source_repo(root)
        destination = root / "destination.git"
        git(["init", "--bare", str(destination)])
        plan = {
            "direction": "github-to-forgejo",
            "repositories": [
                {
                    "name": "example",
                    "source_url": str(source),
                    "destination_url": str(destination),
                    "wiki": False,
                    "lfs": False,
                    "metadata": {"issues": "skip", "pull_requests": "skip"},
                }
            ],
        }
        plan_path = root / "plan.json"
        proof_path = root / "proof.json"
        work_dir = root / "work"
        write_json(plan_path, plan)

        run(
            [
                sys.executable,
                str(MIGRATION_SCRIPT),
                "migrate",
                str(plan_path),
                "--work-dir",
                str(work_dir),
                "--proof",
                str(proof_path),
            ],
            cwd=ROOT,
        )
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        if not proof["verified"]:
            raise AssertionError("migration proof was not verified")
        repo = proof["repositories"][0]
        if repo["git"]["branch_count"] != 2:
            raise AssertionError(f"expected two branches, got {repo['git']['branch_count']}")
        if repo["git"]["tag_count"] != 1:
            raise AssertionError(f"expected one tag, got {repo['git']['tag_count']}")

        verify_proof = root / "verify-proof.json"
        run(
            [
                sys.executable,
                str(MIGRATION_SCRIPT),
                "verify",
                str(plan_path),
                "--proof",
                str(verify_proof),
            ],
            cwd=ROOT,
        )
        verified_again = json.loads(verify_proof.read_text(encoding="utf-8"))
        if not verified_again["verified"]:
            raise AssertionError("verify command did not prove migrated refs")


def test_required_metadata_fails_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="forge-migration-test-") as temp:
        root = Path(temp)
        source = create_source_repo(root)
        destination = root / "destination.git"
        git(["init", "--bare", str(destination)])
        plan_path = root / "plan.json"
        write_json(
            plan_path,
            {
                "direction": "gitlab-to-forgejo",
                "repositories": [
                    {
                        "name": "metadata-required",
                        "source_url": str(source),
                        "destination_url": str(destination),
                        "metadata": {"issues": "required"},
                    }
                ],
            },
        )
        result = run(
            [sys.executable, str(MIGRATION_SCRIPT), "validate-plan", str(plan_path)],
            cwd=ROOT,
            check=False,
        )
        if result.returncode == 0:
            raise AssertionError("required unsupported metadata unexpectedly passed")
        if "metadata migration is not implemented" not in result.stderr:
            raise AssertionError(result.stderr)


def main() -> int:
    if not shutil.which("git"):
        print("git is required for forge migration tests", file=sys.stderr)
        return 1
    test_mirror_migration()
    test_required_metadata_fails_closed()
    print("Forge migration helper self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
