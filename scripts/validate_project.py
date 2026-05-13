#!/usr/bin/env python3
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
required = [
    'README.md', 'LICENSE', 'Makefile',
    'config/cluster.example.yaml',
    'inventory/hosts.example.ini',
    'docs/QUICK_START.md',
    'docs/ARCHITECTURE.md',
    'docs/SECRETS_AND_PRIVACY.md',
    'gitops/bootstrap/root-app.yaml',
]
missing = [p for p in required if not (root / p).exists()]
if missing:
    print('Missing required files:')
    for item in missing:
        print(f' - {item}')
    sys.exit(1)

# Ensure local files are not accidentally committed.
for pattern in ('*.local.yaml', '*.local.yml', '*.local.ini', '.env'):
    committed_like = [p for p in root.rglob(pattern) if '.git' not in p.parts]
    if committed_like:
        print('Local/private files exist in working tree. They are ignored, but verify before pushing:')
        for p in committed_like:
            print(f' - {p.relative_to(root)}')

print('Project structure validation passed.')
