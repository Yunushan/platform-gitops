#!/usr/bin/env python3
"""Self-test the bootstrap env-file loader."""

from __future__ import annotations

from pathlib import Path
import os
import shutil
import shlex
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def bash_path(path: Path) -> str:
    resolved = path.resolve()
    if os.name != "nt":
        return resolved.as_posix()
    drive = resolved.drive.rstrip(":").lower()
    rest = resolved.as_posix().split(":", 1)[1].lstrip("/")
    return f"/mnt/{drive}/{rest}"


def run_bash(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None:
        raise AssertionError("bash is required for bootstrap env loader validation")

    with tempfile.TemporaryDirectory(prefix=".env-loader-script-", dir=ROOT) as temp_root_name:
        temp_root = Path(temp_root_name)
        script_path = temp_root / "run.sh"
        script_path.write_text(script, encoding="utf-8", newline="\n")
        rel_script_path = script_path.relative_to(ROOT).as_posix()
        command = " ".join(
            [
                "cd",
                shlex.quote(bash_path(ROOT)),
                "&&",
                "bash",
                shlex.quote(rel_script_path),
                *(shlex.quote(arg) for arg in args),
            ]
        )
        return subprocess.run(
            [bash, "-lc", command],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )


def assert_success(result: subprocess.CompletedProcess[str], description: str) -> None:
    if result.returncode != 0:
        raise AssertionError(
            f"expected {description} to succeed, got {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def assert_failure(result: subprocess.CompletedProcess[str], description: str) -> None:
    if result.returncode == 0:
        raise AssertionError(
            f"expected {description} to fail\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix=".env-loader-", dir=ROOT) as temp_root_name:
        temp_root = Path(temp_root_name)
        env_file = temp_root / "seed-git.env"
        env_file.write_text(
            "\n".join(
                [
                    "# comments and blank lines are ignored",
                    "",
                    "PLATFORM_AUTO_COMMIT_MESSAGE=Configure private platform deployment",
                    'DOUBLE_QUOTED_VALUE="quoted value with spaces"',
                    "SINGLE_QUOTED_VALUE='single quoted value with spaces'",
                    "export EXPORTED_VALUE=exported value with spaces",
                    "EMPTY_VALUE=",
                    "HASH_VALUE=value # kept literally",
                    "",
                ]
            ),
            encoding="utf-8",
            newline="\n",
        )
        rel_env_file = env_file.relative_to(ROOT).as_posix()

        result = run_bash(
            r"""
set -euo pipefail
. scripts/bootstrap/load-env-file.sh
load_env_file "$1"
assert_value() {
  local name="$1"
  local actual="$2"
  local expected="$3"
  if [ "${actual}" != "${expected}" ]; then
    printf '%s expected <%s> got <%s>\n' "${name}" "${expected}" "${actual}" >&2
    exit 1
  fi
}
assert_value PLATFORM_AUTO_COMMIT_MESSAGE "${PLATFORM_AUTO_COMMIT_MESSAGE}" "Configure private platform deployment"
assert_value DOUBLE_QUOTED_VALUE "${DOUBLE_QUOTED_VALUE}" "quoted value with spaces"
assert_value SINGLE_QUOTED_VALUE "${SINGLE_QUOTED_VALUE}" "single quoted value with spaces"
assert_value EXPORTED_VALUE "${EXPORTED_VALUE}" "exported value with spaces"
assert_value EMPTY_VALUE "${EMPTY_VALUE}" ""
assert_value HASH_VALUE "${HASH_VALUE}" "value # kept literally"
if [ "$(printenv PLATFORM_AUTO_COMMIT_MESSAGE)" != "Configure private platform deployment" ]; then
  echo "PLATFORM_AUTO_COMMIT_MESSAGE was not exported" >&2
  exit 1
fi
""",
            rel_env_file,
        )
        assert_success(result, "loading literal env values with spaces")

        invalid_env_file = temp_root / "invalid.env"
        invalid_env_file.write_text("this is not an assignment\n", encoding="utf-8", newline="\n")
        rel_invalid_env_file = invalid_env_file.relative_to(ROOT).as_posix()
        invalid_result = run_bash(
            r"""
set -euo pipefail
. scripts/bootstrap/load-env-file.sh
load_env_file "$1"
""",
            rel_invalid_env_file,
        )
        assert_failure(invalid_result, "loading invalid env syntax")
        if "expected KEY=value" not in invalid_result.stderr:
            raise AssertionError(
                "invalid env syntax did not print a useful error\n"
                f"stdout:\n{invalid_result.stdout}\n"
                f"stderr:\n{invalid_result.stderr}"
            )

    print("Bootstrap env loader self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
