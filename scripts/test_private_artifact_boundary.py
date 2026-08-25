#!/usr/bin/env python3
"""Ensure private/generated artifact directories do not contain tracked payloads."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

from test_bash_support import BashRuntimeUnavailable, bash_executable, bash_path, run_bash


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRS = ("private", "rendered", "secrets")
WOODPECKER_RECONCILER = ROOT / "scripts" / "bootstrap" / "reconcile-woodpecker-gitops-source.sh"
ALLOWED_TRACKED_PATHS = {
    "private/.gitkeep",
    "private/README.md",
    "secrets/.gitkeep",
    "secrets/README.md",
}
REQUIRED_GITIGNORE_RULES = {
    "private/*",
    "!private/README.md",
    "!private/.gitkeep",
    "rendered/",
    "secrets/*",
    "!secrets/README.md",
    "!secrets/.gitkeep",
}


def git_available() -> bool:
    if shutil.which("git") is None:
        return False
    result = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def tracked_private_paths() -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--", *PRIVATE_DIRS],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.strip() or "git ls-files failed")
    return [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]


def check_gitignore_rules() -> list[str]:
    lines = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    return sorted(REQUIRED_GITIGNORE_RULES.difference(lines))


def check_woodpecker_seed_isolation() -> list[str]:
    text = WOODPECKER_RECONCILER.read_text(encoding="utf-8")
    required = {
        'temporary_root="$(mktemp -d': "a private temporary workspace",
        "git clone --quiet --no-hardlinks --no-checkout": "an isolated Git checkout",
        'cp "${inventory_file}" "${seed_checkout}/inventory/hosts.local.ini"': "isolated inventory input",
        "PLATFORM_SEED_SYNC_PULL=false": "source pull isolation",
        "PLATFORM_SEED_SYNC_PUSH_ORIGIN=false": "source push isolation",
        "PLATFORM_VALIDATE_BEFORE_PUSH=true": "pre-push validation",
        "PLATFORM_RUN_NO_SECRETS=true": "private-data scanning",
        "PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES=true": "private seed hostname policy",
        'config user.name "${seed_git_user_name:-Platform GitOps Repair}"': "isolated commit author name",
        'config user.email "${seed_git_user_email:-platform-gitops-repair@localhost}"': "isolated commit author email",
        "config commit.gpgSign false": "noninteractive isolated commits",
        'cd "${seed_checkout}"': "execution inside the isolated checkout",
    }
    problems = [
        f"Woodpecker seed reconciliation is missing {description}"
        for needle, description in required.items()
        if needle not in text
    ]
    for unsafe_override in (
        "PLATFORM_WOODPECKER_REPAIR_SYNC_PULL",
        "PLATFORM_WOODPECKER_REPAIR_SYNC_PUSH_ORIGIN",
    ):
        if unsafe_override in text:
            problems.append(
                f"Woodpecker seed reconciliation must not allow {unsafe_override} to reach the public checkout"
            )
    return problems


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def check_woodpecker_seed_behavior() -> list[str]:
    try:
        _, flavor = bash_executable()
    except BashRuntimeUnavailable as exc:
        print(f"Woodpecker seed isolation behavior test skipped: {exc}.")
        return []

    with tempfile.TemporaryDirectory(prefix=".woodpecker-seed-boundary-", dir=ROOT) as temp_name:
        temp_root = Path(temp_name)
        repo = temp_root / "source"
        script = repo / "scripts" / "bootstrap" / WOODPECKER_RECONCILER.name
        fake_bin = temp_root / "bin"
        marker = temp_root / "isolated-checkout"
        script.parent.mkdir(parents=True)
        fake_bin.mkdir()
        (repo / "inventory").mkdir()
        (repo / "private").mkdir()
        script.write_text(WOODPECKER_RECONCILER.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        (repo / ".gitignore").write_text(
            "inventory/hosts.local.ini\nprivate/*\n",
            encoding="utf-8",
            newline="\n",
        )
        (repo / "tracked.txt").write_text("public template\n", encoding="utf-8", newline="\n")
        (repo / "inventory" / "hosts.local.ini").write_text(
            "[rke2_servers]\nnode-1 ansible_host=192.0.2.10 ansible_user=test\n",
            encoding="utf-8",
            newline="\n",
        )
        (repo / "private" / "seed-git.env").write_text(
            "WOODPECKER_OPEN=false\n",
            encoding="utf-8",
            newline="\n",
        )
        fake_make = fake_bin / "make"
        fake_make.write_text(
            r'''#!/usr/bin/env bash
set -euo pipefail
test "$1" = "platform-seed-git-sync"
test "${PLATFORM_SEED_SYNC_PULL}" = "false"
test "${PLATFORM_SEED_SYNC_PUSH_ORIGIN}" = "false"
test "${PLATFORM_AUTO_RENDER_PRIVATE_VALUES}" = "true"
test "${PLATFORM_VALIDATE_BEFORE_PUSH}" = "true"
test "${PLATFORM_RUN_NO_SECRETS}" = "true"
test "${PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES}" = "true"
test -f "${PLATFORM_SEED_DEPLOY_ENV_FILE}"
test -f inventory/hosts.local.ini
test "$(git config user.name)" = "Test"
test "$(git config user.email)" = "test@example.test"
test "$(git config --bool commit.gpgSign)" = "false"
pwd -P > "${TEST_MARKER}"
printf '%s\n' 'private deployment render' >> tracked.txt
git add tracked.txt
git commit --quiet -m isolated-render
''',
            encoding="utf-8",
            newline="\n",
        )
        if os.name != "nt":
            fake_make.chmod(0o700)

        init = run_git(repo, "init", "--quiet")
        if init.returncode != 0:
            return [f"could not initialize Woodpecker seed isolation test repository: {init.stderr.strip()}"]
        run_git(repo, "config", "user.name", "Test")
        run_git(repo, "config", "user.email", "test@example.test")
        run_git(repo, "add", ".gitignore", "scripts", "tracked.txt")
        committed = run_git(repo, "commit", "--quiet", "-m", "public-template")
        if committed.returncode != 0:
            return [f"could not commit Woodpecker seed isolation fixture: {committed.stderr.strip()}"]
        source_head = run_git(repo, "rev-parse", "HEAD").stdout.strip()

        repo_bash = bash_path(repo, flavor)
        fake_bin_bash = bash_path(fake_bin, flavor)
        marker_bash = bash_path(marker, flavor)
        command = "\n".join(
            [
                "set -euo pipefail",
                f"cd {shlex.quote(repo_bash)}",
                f"export PATH={shlex.quote(fake_bin_bash)}:$PATH",
                f"export TEST_MARKER={shlex.quote(marker_bash)}",
                "export PLATFORM_WOODPECKER_REPAIR_SYNC_GITOPS=true",
                "export PLATFORM_SEED_DEPLOY_ENV_FILE=private/seed-git.env",
                "bash scripts/bootstrap/reconcile-woodpecker-gitops-source.sh",
                'isolated_checkout="$(cat "${TEST_MARKER}")"',
                'test ! -e "${isolated_checkout}"',
            ]
        )
        result = run_bash(command)
        if result.returncode != 0:
            return [
                "Woodpecker seed reconciliation behavior test failed:\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            ]

        problems: list[str] = []
        if run_git(repo, "rev-parse", "HEAD").stdout.strip() != source_head:
            problems.append("Woodpecker seed reconciliation changed the source repository HEAD")
        if run_git(repo, "status", "--porcelain", "--untracked-files=normal").stdout.strip():
            problems.append("Woodpecker seed reconciliation changed the source working tree")
        if (repo / "tracked.txt").read_text(encoding="utf-8") != "public template\n":
            problems.append("Woodpecker seed reconciliation wrote private render data into the source checkout")
        if "woodpecker_gitops_source_sync=synced" not in result.stdout:
            problems.append("Woodpecker seed reconciliation did not report successful isolated sync")
        return problems


def main() -> int:
    missing_rules = check_gitignore_rules()
    problems: list[str] = []
    if missing_rules:
        problems.append(".gitignore is missing private artifact boundary rule(s): " + ", ".join(missing_rules))

    problems.extend(check_woodpecker_seed_isolation())
    problems.extend(check_woodpecker_seed_behavior())

    if git_available():
        disallowed = sorted(set(tracked_private_paths()).difference(ALLOWED_TRACKED_PATHS))
        if disallowed:
            problems.append(
                "Disallowed tracked private artifact path(s): "
                + ", ".join(disallowed)
                + ". Only README.md and .gitkeep may be tracked under private/ or secrets/, "
                + "and rendered/ must stay untracked."
            )
    else:
        print("Git is not available; skipped tracked private artifact path check.")

    if problems:
        print("Private artifact boundary validation failed:")
        for problem in problems:
            print(f" - {problem}")
        return 1

    print("Private artifact boundary validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
