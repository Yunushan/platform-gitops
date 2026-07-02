#!/usr/bin/env python3
"""Syntax-check inline shell blocks embedded in Ansible playbooks.

The standalone shell validator catches scripts under scripts/, but a lot of the
production repair logic lives in ansible.builtin.shell blocks. This test extracts
literal/folded shell blocks, normalizes common Jinja expressions to shell-safe
tokens, and runs bash -n over each block.
"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK_DIR = ROOT / "ansible" / "playbooks"
SHELL_BLOCK_RE = re.compile(
    r"^(?P<indent>\s*)(?:ansible\.builtin\.)?(?:shell|command):\s*(?P<style>[|>])(?:[-+])?\s*(?:#.*)?$"
)
EXCLUDE_DIRS = {
    ".git",
    ".cache",
    ".pytest_cache",
    ".terraform",
    ".venv",
    "__pycache__",
    "build",
    "charts",
    "dist",
    "private",
    "rendered",
    "secrets",
}


def should_skip(path: Path) -> bool:
    return any(part in EXCLUDE_DIRS or part.startswith(".ansible-shell-syntax-") for part in path.parts)


def normalize_jinja(text: str) -> str:
    text = re.sub(r"{#.*?#}", "", text, flags=re.S)
    text = re.sub(r"{%.*?%}", ":", text, flags=re.S)
    text = re.sub(r"{{.*?}}", "JINJA_VALUE", text, flags=re.S)
    return text


def deindent_block(raw_lines: list[str]) -> str:
    min_indent: int | None = None
    for line in raw_lines:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        min_indent = indent if min_indent is None else min(min_indent, indent)
    if min_indent is None:
        return ""
    return "\n".join(line[min_indent:] if len(line) >= min_indent else "" for line in raw_lines)


def shell_blocks(path: Path) -> list[tuple[int, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = SHELL_BLOCK_RE.match(line)
        if not match:
            continue
        parent_indent = len(match.group("indent"))
        raw_block: list[str] = []
        for next_line in lines[index + 1 :]:
            if not next_line.strip():
                raw_block.append(next_line)
                continue
            indent = len(next_line) - len(next_line.lstrip())
            if indent <= parent_indent:
                break
            raw_block.append(next_line)
        block = deindent_block(raw_block)
        if block:
            blocks.append((index + 1, normalize_jinja(block)))
    return blocks


def playbooks() -> list[Path]:
    if not PLAYBOOK_DIR.exists():
        return []
    return [path for path in sorted(PLAYBOOK_DIR.glob("*.yml")) if not should_skip(path)]


def main() -> int:
    bash = shutil.which("bash")
    if not bash:
        print("bash is required for Ansible inline shell syntax validation.")
        return 1

    failures: list[tuple[Path, int, str]] = []
    block_count = 0
    with tempfile.TemporaryDirectory(prefix=".ansible-shell-syntax-", dir=ROOT) as temp_root_name:
        temp_root = Path(temp_root_name)
        for playbook in playbooks():
            for line_no, script in shell_blocks(playbook):
                block_count += 1
                rel = playbook.relative_to(ROOT)
                normalized = temp_root / f"{rel.as_posix().replace('/', '__')}-{line_no}.sh"
                normalized.write_text(script + "\n", encoding="utf-8", newline="\n")
                result = subprocess.run(
                    [bash, "-n", normalized.relative_to(ROOT).as_posix()],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "").strip()
                    failures.append((rel, line_no, detail))

    if failures:
        print("Ansible inline shell syntax validation failed:")
        for rel, line_no, detail in failures:
            print(f" - {rel}:{line_no}")
            if detail:
                print(f"   {detail}")
        return 1

    print(f"Ansible inline shell syntax validation passed for {block_count} blocks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
