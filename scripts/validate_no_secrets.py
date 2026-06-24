#!/usr/bin/env python3
"""Conservative repository safety scanner.

This scanner is not a replacement for professional secret scanning. It blocks
obvious secrets, private keys, kubeconfigs, and real-looking private IPs.
"""
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parents[1]
exclude_dirs = {
    '.git', '.cache', 'dist', 'build', '.terraform',
    'private', 'rendered', 'secrets',
}
text_suffixes = {
    '.md', '.txt', '.yaml', '.yml', '.json', '.ini', '.sh', '.ps1', '.py',
    '.toml', '.conf', '.cfg', '.gitignore', '.example', '.env', '.dockerfile'
}
secret_assignment = re.compile(r"""(?ix)
    \b(password|passwd|secret|token|api[_-]?key|private[_-]?key|access[_-]?key|client[_-]?secret)\b
    \s*[:=]\s*
    (?!<[^>]+>)(?!\$\{[^}]+\})(?!changeme\b)(?!example\b)(?!dummy\b)(?!false\b)(?!true\b)(?!null\b)(?!from_secret\b)(?!"?<[^>]+>"?)
    ['"]?([A-Za-z0-9_./+=:@!#$%^&*~-]{8,})['"]?
""")
private_key = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')
kubeconfig = re.compile(r'(?m)^\s*kind:\s*Config\s*$')
# Documentation-reserved IP ranges are allowed, real private ranges are not.
private_ip = re.compile(r'\b(?:(?:10)\.(?:\d{1,3}\.){2}\d{1,3}|172\.(?:1[6-9]|2\d|3[0-1])\.(?:\d{1,3}\.)\d{1,3}|192\.168\.(?:\d{1,3}\.)\d{1,3})\b')

allow_fragments = [
    '<GENERATE_WITH_PASSWORD_MANAGER>', '<NODE_1_IP>', '<NODE_2_IP>', '<NODE_3_IP>',
    '<VIP_ADDRESS>', '<PLATFORM_DOMAIN>', '<VIP_DNS_NAME>', 'example.com',
    'password: <', 'token: <', 'secret: <', 'api_key: <'
]

problems = []
for path in root.rglob('*'):
    if path.is_dir():
        continue
    if any(part in exclude_dirs for part in path.parts):
        continue
    rel = path.relative_to(root)
    rel_posix = rel.as_posix()
    if rel.name.endswith('.zip'):
        continue
    if '/crds/' in rel_posix and rel.suffix in {'.yaml', '.yml'}:
        continue
    if rel.name.startswith('.env') and rel.name != '.env.example':
        continue
    if any(
        rel.name.endswith(suffix)
        for suffix in (
            '.local.yaml', '.local.yml', '.local.ini',
            '.private.yaml', '.private.yml',
            '.rendered.yaml', '.rendered.yml',
        )
    ):
        continue
    if rel_posix == 'scripts/validate_no_secrets.py':
        continue
    try:
        data = path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        continue
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

if problems:
    print('Repository safety scan failed:')
    for rel, msg in problems:
        print(f' - {rel}: {msg}')
    sys.exit(1)

print('No obvious plaintext secrets, private keys, kubeconfigs, or private IPs found.')
