#!/usr/bin/env python3
from pathlib import Path
import sys
import tempfile
from unittest import mock

import test_bash_support
from test_bash_support import BashRuntimeUnavailable, bash_executable, bash_path, run_bash_args

# The Bash adapter centralizes the subprocess.run call so WSL and Git Bash use
# the same path translation without duplicating process-launch behavior here.

root = Path(__file__).resolve().parents[1]
exclude_dirs = {
    '.git', '.cache', '.pytest_cache', '.terraform', '.venv',
    '__pycache__', 'build', 'charts', 'dist', 'private', 'rendered', 'secrets',
}


def shell_scripts() -> list[Path]:
    scripts: list[Path] = []
    for path in root.rglob('*.sh'):
        if path.is_dir():
            continue
        if any(
            part in exclude_dirs
            or part.startswith('.shell-syntax-')
            or part.startswith('.ansible-shell-syntax-')
            for part in path.parts
        ):
            continue
        scripts.append(path.relative_to(root))
    return sorted(scripts)


def test_git_bash_discovery() -> None:
    with tempfile.TemporaryDirectory(prefix='.git-bash-contract-') as temp_root_name:
        temp_root = Path(temp_root_name)
        executable = temp_root / 'Git' / 'bin' / 'bash.exe'
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text('', encoding='utf-8')
        with (
            mock.patch.object(test_bash_support.os, 'name', 'nt'),
            # Patching os.name on a POSIX runner makes pathlib.Path try to
            # construct WindowsPath. Keep the host's concrete path class so
            # this Windows-behavior test remains portable.
            mock.patch.object(test_bash_support, 'Path', type(temp_root)),
            mock.patch.dict(
                test_bash_support.os.environ,
                {
                    'ProgramFiles': temp_root_name,
                    'ProgramFiles(x86)': '',
                    'LOCALAPPDATA': '',
                },
                clear=False,
            ),
            mock.patch.object(test_bash_support.shutil, 'which', return_value=None),
        ):
            discovered = test_bash_support.git_bash_executable()
    if discovered is None or Path(discovered).resolve() != executable.resolve():
        raise AssertionError('Git Bash discovery must find the standard Windows installation path')


def test_git_bash_fallback() -> None:
    path_type = type(Path(__file__).resolve())
    with (
        mock.patch.object(test_bash_support.os, 'name', 'nt'),
        # See test_git_bash_discovery: pathlib must keep a constructible
        # concrete path implementation when os.name is simulated on Linux.
        mock.patch.object(test_bash_support, 'Path', path_type),
        mock.patch.object(test_bash_support.shutil, 'which', return_value='C:/Windows/System32/bash.exe'),
        mock.patch.object(
            test_bash_support,
            'git_bash_executable',
            return_value='C:/Program Files/Git/bin/bash.exe',
        ) as git_bash_mock,
    ):
        executable, flavor = bash_executable()
    if executable != 'C:/Program Files/Git/bin/bash.exe' or flavor != 'git-bash':
        raise AssertionError('A runnable Git Bash install must replace the WSL shim on Windows')
    if not git_bash_mock.called:
        raise AssertionError('WSL shim detection must consult Git Bash fallback discovery')


def main() -> int:
    test_git_bash_discovery()
    test_git_bash_fallback()
    try:
        _, flavor = bash_executable()
    except BashRuntimeUnavailable as exc:
        print(f'Shell syntax validation skipped: {exc}; bash is required for shell syntax validation.')
        return 0

    scripts = shell_scripts()
    failures: list[tuple[Path, str]] = []
    with tempfile.TemporaryDirectory(prefix='.shell-syntax-', dir=root) as temp_root_name:
        temp_root = Path(temp_root_name)
        for rel in scripts:
            source = root / rel
            normalized = temp_root / rel
            normalized.parent.mkdir(parents=True, exist_ok=True)
            normalized.write_text(source.read_text(encoding='utf-8'), encoding='utf-8', newline='\n')
            try:
                result = run_bash_args(['-n', bash_path(normalized, flavor)])
            except BashRuntimeUnavailable as exc:
                print(f'Shell syntax validation skipped: {exc}; bash is required for shell syntax validation.')
                return 0
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or '').strip()
                failures.append((rel, detail))

    if failures:
        print('Shell syntax validation failed:')
        for rel, detail in failures:
            print(f' - {rel}')
            if detail:
                print(f'   {detail}')
        return 1

    print(f'Shell syntax validation passed for {len(scripts)} scripts.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
