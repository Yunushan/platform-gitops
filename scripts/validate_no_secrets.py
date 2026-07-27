#!/usr/bin/env python3
"""Conservative repository safety scanner.

This scanner is not a replacement for professional secret scanning. It blocks
obvious secrets, private keys, kubeconfigs, and real-looking private IPs.
"""
from pathlib import Path
import os
import re
import sys

root = Path(__file__).resolve().parents[1]
exclude_dirs = {
    '.git', '.cache', '.pytest_cache', '.terraform', '.venv',
    'dist', 'build',
    'private', 'rendered', 'secrets', '__pycache__',
}
secret_assignment = re.compile(r"""(?ix)
    \b(password|passwd|secret|token|api[_-]?key|private[_-]?key|access[_-]?key|client[_-]?secret)\b
    \s*[:=]\s*
    (?!<[^>]+>)(?!\$\{[^}]+\})(?![A-Za-z_][A-Za-z0-9_.]*\s*\()(?!changeme\b)(?!example\b)(?!dummy\b)(?!false\b)(?!true\b)(?!null\b)(?!from_secret\b)(?!"?<[^>]+>"?)
    ['"]?([A-Za-z0-9_./+=:@!#$%^&*~-]{8,})['"]?
""")
private_key = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')
kubeconfig = re.compile(r'(?m)^\s*kind:\s*Config\s*$')
# Documentation-reserved IP ranges are allowed, real private ranges are not.
private_ip = re.compile(r'\b(?:(?:10)\.(?:\d{1,3}\.){2}\d{1,3}|172\.(?:1[6-9]|2\d|3[0-1])\.(?:\d{1,3}\.)\d{1,3}|192\.168\.(?:\d{1,3}\.)\d{1,3})\b')
known_company_fragment = 'is' + 'bak'
known_private_host_stem = 'gitops' + '-arge'
forbidden_private_markers = [
    ('company domain fragment', re.compile(rf'\b{known_company_fragment}\b', re.IGNORECASE)),
    ('private deployment hostname', re.compile(rf'\b(?:argocd|ci|grafana|prometheus|registry)?-?{known_private_host_stem}\b', re.IGNORECASE)),
    ('private node address range', re.compile(r'\b172\.16\.134\.\d{1,3}\b')),
    ('private node username', re.compile(r'\bgitlab[1-3]\b', re.IGNORECASE)),
]

default_rke2_pod_cidr = '.'.join(('10', '42', '0', '0')) + '/16'

allow_fragments = [
    '<GENERATE_WITH_PASSWORD_MANAGER>', '<NODE_1_IP>', '<NODE_2_IP>', '<NODE_3_IP>',
    '<VIP_ADDRESS>', '<PLATFORM_DOMAIN>', '<VIP_DNS_NAME>', 'example.com',
    'password: <', 'token: <', 'secret: <', 'api_key: <',
    default_rke2_pod_cidr,
]


def should_scan(path: Path) -> bool:
    if path.is_dir():
        return False
    if any(
        part in exclude_dirs
        or part.startswith('.shell-syntax-')
        or part.startswith('.ansible-shell-syntax-')
        for part in path.parts
    ):
        return False
    rel = path.relative_to(root)
    rel_posix = rel.as_posix()
    if rel.name.endswith('.zip'):
        return False
    if '/crds/' in rel_posix and rel.suffix in {'.yaml', '.yml'}:
        return False
    if '/charts/' in rel_posix and rel.suffix in {'.yaml', '.yml', '.json', '.tpl'}:
        return False
    if rel.name.startswith('.env') and rel.name != '.env.example':
        return False
    if any(
        rel.name.endswith(suffix)
        for suffix in (
            '.local.yaml', '.local.yml', '.local.ini',
            '.private.yaml', '.private.yml',
            '.rendered.yaml', '.rendered.yml',
        )
    ):
        return False
    if rel_posix == 'scripts/validate_no_secrets.py':
        return False
    return True


def scan_text(rel: Path, data: str, include_internal_markers: bool = True) -> list[tuple[Path, str]]:
    problems: list[tuple[Path, str]] = []
    normalized = data
    for frag in allow_fragments:
        normalized = normalized.replace(frag, '')
    if private_key.search(normalized):
        problems.append((rel, 'private key block'))
    if kubeconfig.search(normalized) and 'clusters:' in normalized and 'users:' in normalized:
        problems.append((rel, 'possible kubeconfig'))
    for m in secret_assignment.finditer(normalized):
        line = normalized[:m.start()].count('\n') + 1
        problems.append((rel, f'possible plaintext secret at line {line}: {m.group(1)}'))
    for m in private_ip.finditer(normalized):
        line = normalized[:m.start()].count('\n') + 1
        problems.append((rel, f'private IP-like value at line {line}: {m.group(0)}'))
    if include_internal_markers:
        for label, pattern in forbidden_private_markers:
            for m in pattern.finditer(normalized):
                line = normalized[:m.start()].count('\n') + 1
                problems.append((rel, f'{label} at line {line}: {m.group(0)}'))
    return problems


def scan_repo(include_internal_markers: bool = True) -> list[tuple[Path, str]]:
    problems: list[tuple[Path, str]] = []
    for path in root.rglob('*'):
        if not should_scan(path):
            continue
        rel = path.relative_to(root)
        try:
            data = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        problems.extend(scan_text(rel, data, include_internal_markers=include_internal_markers))
    return problems


def main() -> int:
    allow_internal_hostnames = os.environ.get('PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES', '').lower() in {
        '1', 'true', 'yes', 'on'
    }
    problems = scan_repo(include_internal_markers=not allow_internal_hostnames)
    if problems:
        print('Repository safety scan failed:')
        for rel, msg in problems:
            print(f' - {rel}: {msg}')
        return 1

    if allow_internal_hostnames:
        print('No obvious plaintext secrets, private keys, kubeconfigs, or private IPs found.')
    else:
        print('No obvious plaintext secrets, private keys, kubeconfigs, private IPs, or internal hostnames found.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
