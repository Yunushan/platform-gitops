#!/usr/bin/env python3
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parents[1]
conflict_marker_re = re.compile(r'^(<<<<<<< .+|=======|>>>>>>> .+)$', re.MULTILINE)
required = [
    'README.md', 'LICENSE', 'Makefile',
    'config/cluster.example.yaml',
    'inventory/hosts.example.ini',
    'docs/QUICK_START.md',
    'docs/ARCHITECTURE.md',
    'docs/SECRETS_AND_PRIVACY.md',
    'gitops/bootstrap/root-app.yaml',
    'ansible/playbooks/verify-platform-app-health.yml',
    'scripts/check_gitops_profile.py',
    'scripts/render_deployable_gitops_apps.py',
    'scripts/bootstrap/validate-gitops-selection.sh',
    'scripts/test_profile_checker.py',
    'scripts/test_deployable_renderer.py',
    'scripts/test_private_values_renderer.py',
    'scripts/validate_platform_contract.py',
]
missing = [p for p in required if not (root / p).exists()]
if missing:
    print('Missing required files:')
    for item in missing:
        print(f' - {item}')
    sys.exit(1)

conflicted = []
for path in root.rglob('*'):
    if path.is_dir():
        continue
    if any(part in {'.git', 'charts'} for part in path.parts):
        continue
    try:
        text = path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        continue
    except OSError:
        continue
    if conflict_marker_re.search(text):
        conflicted.append(path.relative_to(root))

if conflicted:
    print('Git conflict markers found:')
    for item in conflicted:
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
