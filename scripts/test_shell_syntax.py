#!/usr/bin/env python3
from pathlib import Path
import sys
import tempfile

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


def main() -> int:
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
