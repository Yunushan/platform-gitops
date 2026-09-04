#!/usr/bin/env python3
"""Self-test canonical hostname loading for the Forgejo ingress runner."""

from __future__ import annotations

from pathlib import Path
import shlex
import subprocess
import tempfile

from test_bash_support import (
    BashRuntimeUnavailable,
    bash_executable,
    bash_path,
    run_bash,
)


ROOT = Path(__file__).resolve().parents[1]


def run_runner(env_contents: str, *, explicit_host: str = "") -> subprocess.CompletedProcess[str]:
    _, flavor = bash_executable()
    with tempfile.TemporaryDirectory(prefix=".forgejo-ingress-runner-", dir=ROOT) as temp_name:
        temp_root = Path(temp_name)
        bin_dir = temp_root / "bin"
        bin_dir.mkdir()
        fake_ansible = bin_dir / "ansible-playbook"
        fake_ansible.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\nprintf 'arg=%s\\n' \"$@\"\n",
            encoding="utf-8",
            newline="\n",
        )
        fake_ansible.chmod(0o755)

        env_file = temp_root / "private.env"
        env_file.write_text(env_contents, encoding="utf-8", newline="\n")

        commands = [
            f"cd {shlex.quote(bash_path(ROOT, flavor))}",
            "unset PLATFORM_FORGEJO_HOST PLATFORM_GIT_HOST",
            f"export PATH={shlex.quote(bash_path(bin_dir, flavor))}:$PATH",
            "export PLATFORM_FORGEJO_INGRESS_ENV_FILE="
            f"{shlex.quote(bash_path(env_file, flavor))}",
        ]
        if explicit_host:
            commands.append(f"export PLATFORM_FORGEJO_HOST={shlex.quote(explicit_host)}")
        commands.append("bash scripts/bootstrap/run-forgejo-ingress.sh")
        return run_bash(" && ".join(commands))


def require_success(
    result: subprocess.CompletedProcess[str], expected_host: str
) -> None:
    if result.returncode != 0:
        raise AssertionError(
            f"Forgejo ingress runner failed with {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    args = [line.removeprefix("arg=") for line in result.stdout.splitlines()]
    expected = [
        "-i",
        "inventory/hosts.local.ini",
        "--extra-vars",
        f"platform_forgejo_host={expected_host}",
        "ansible/playbooks/publish-forgejo-ingress.yml",
    ]
    if args != expected:
        raise AssertionError(f"unexpected ansible-playbook arguments: {args!r}")


def main() -> int:
    try:
        require_success(
            run_runner("PLATFORM_FORGEJO_HOST=git.private.example.test\n"),
            "git.private.example.test",
        )
        require_success(
            run_runner(
                "PLATFORM_FORGEJO_HOST=stale.private.example.test\n",
                explicit_host="explicit.private.example.test",
            ),
            "explicit.private.example.test",
        )
    except BashRuntimeUnavailable as exc:
        print(f"Forgejo ingress runner self-test skipped: {exc}.")
        return 0

    print("Forgejo ingress runner self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
