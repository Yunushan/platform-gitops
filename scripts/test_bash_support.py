"""Run Bash-backed self-tests across POSIX, WSL, and Git Bash hosts."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from subprocess_timeout import bounded_timeout_seconds


class BashRuntimeUnavailable(RuntimeError):
    """Raised when the host exposes Bash but cannot spawn its runtime."""


def bash_flavor(bash: str) -> str:
    if os.name != "nt":
        return "posix"
    resolved = Path(bash).resolve()
    if resolved.name.lower() == "bash.exe" and resolved.parent.name.lower() == "system32":
        return "wsl"
    return "git-bash"


def bash_path(path: Path, flavor: str) -> str:
    resolved = path.resolve()
    if os.name != "nt":
        return resolved.as_posix()
    drive = resolved.drive.rstrip(":").lower()
    rest = resolved.as_posix().split(":", 1)[1].lstrip("/")
    if flavor == "wsl":
        return f"/mnt/{drive}/{rest}"
    return f"/{drive}/{rest}"


def bash_executable() -> tuple[str, str]:
    bash = shutil.which("bash")
    if bash is None:
        raise BashRuntimeUnavailable("Bash is not installed on this host")
    return bash, bash_flavor(bash)


def bash_argv(arguments: Sequence[str]) -> list[str]:
    bash, flavor = bash_executable()
    if flavor != "wsl":
        return [bash, *arguments]
    wsl = shutil.which("wsl") or str(Path(bash).with_name("wsl.exe"))
    return [wsl, "--exec", "bash", *arguments]


def _run(
    argv: Sequence[str],
    *,
    flavor: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        timeout = bounded_timeout_seconds(300, "PLATFORM_BASH_TEST_TIMEOUT_SECONDS")
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except OSError as exc:
        if flavor == "wsl" and isinstance(exc, PermissionError):
            raise BashRuntimeUnavailable(
                "the WSL launcher could not be spawned by the host Python process"
            ) from exc
        raise
    if flavor == "wsl" and result.returncode in (-1, 0xFFFFFFFF):
        raise BashRuntimeUnavailable(
            "the WSL launcher could not be spawned by the host Python process"
        )
    return result


def run_bash(
    command: str,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    _, flavor = bash_executable()
    return _run(bash_argv(["-lc", command]), flavor=flavor, cwd=cwd, env=env)


def run_bash_args(
    arguments: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    _, flavor = bash_executable()
    return _run(bash_argv(arguments), flavor=flavor, cwd=cwd, env=env)
