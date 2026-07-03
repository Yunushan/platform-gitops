#!/usr/bin/env python3
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parents[1]
conflict_marker_re = re.compile(r'^(<<<<<<< .+|=======|>>>>>>> .+)$', re.MULTILINE)
exclude_dirs = {
    '.git', '.cache', '.pytest_cache', '.terraform', '.venv',
    '__pycache__', 'build', 'charts', 'dist', 'private', 'rendered', 'secrets',
}
required = [
    'README.md', 'LICENSE', 'Makefile', '.gitattributes',
    'SECURITY.md', 'CONTRIBUTING.md', 'CODE_OF_CONDUCT.md', 'NOTICE',
    '.github/pull_request_template.md',
    '.github/CODEOWNERS.example',
    '.github/ISSUE_TEMPLATE/config.yml',
    '.github/ISSUE_TEMPLATE/bug_report.yml',
    '.github/ISSUE_TEMPLATE/feature_request.yml',
    'renovate.json',
    '.gitleaks.toml',
    '.semgrep.yml',
    'trivy.yaml',
    'config/cluster.example.yaml',
    'inventory/hosts.example.ini',
    'docs/QUICK_START.md',
    'docs/ARCHITECTURE.md',
    'docs/BACKUP_RESTORE.md',
    'docs/BUSINESS_CONTINUITY.md',
    'docs/SERVICE_CATALOG.md',
    'docs/ARCHITECTURE_DECISIONS.md',
    'docs/adr/0000-template.md',
    'docs/OPERATIONS.md',
    'docs/PRODUCTION_READINESS.md',
    'docs/PLATFORM_SUPPORT.md',
    'docs/NODE_OS_SUPPORT.md',
    'docs/INCIDENT_RESPONSE.md',
    'docs/ACCESS_CONTROL.md',
    'docs/CAPACITY_PLANNING.md',
    'docs/COMPLIANCE_AUDIT.md',
    'docs/RELEASE_PROMOTION.md',
    'docs/ALERTING.md',
    'docs/DATA_CLASSIFICATION.md',
    'docs/THREAT_MODEL.md',
    'docs/SECRETS_AND_PRIVACY.md',
    'config/sops.age.example.yaml',
    'gitops/bootstrap/root-app.yaml',
    'ansible/playbooks/verify-platform-app-health.yml',
    'ansible/playbooks/repair-platform-service-path-consumers.yml',
    'ansible/playbooks/repair-woodpecker.yml',
    'scripts/check_gitops_profile.py',
    'scripts/render_deployable_gitops_apps.py',
    'scripts/run_validation.py',
    'scripts/bootstrap/validate-gitops-selection.sh',
    'scripts/supply-chain-posture.sh',
    'scripts/test_python_syntax.py',
    'scripts/test_validation_runner.py',
    'scripts/test_line_endings.py',
    'scripts/test_profile_checker.py',
    'scripts/test_deployable_renderer.py',
    'scripts/test_gitops_selection_helper.py',
    'scripts/test_private_values_renderer.py',
    'scripts/test_platform_secret_contract.py',
    'scripts/test_policy_examples.py',
    'scripts/test_sops_age_policy.py',
    'scripts/test_supply_chain_helpers.py',
    'scripts/test_backup_restore_runbook.py',
    'scripts/test_business_continuity.py',
    'scripts/test_service_catalog.py',
    'scripts/test_architecture_decisions.py',
    'scripts/test_operations_runbook.py',
    'scripts/test_production_readiness_checklist.py',
    'scripts/test_platform_support.py',
    'scripts/test_incident_response_runbook.py',
    'scripts/test_access_control_runbook.py',
    'scripts/test_capacity_planning_runbook.py',
    'scripts/test_compliance_audit_runbook.py',
    'scripts/test_release_promotion_runbook.py',
    'scripts/test_alerting_runbook.py',
    'scripts/test_data_classification.py',
    'scripts/test_security_policy.py',
    'scripts/test_threat_model.py',
    'scripts/test_repository_governance.py',
    'scripts/test_codeowners_starter.py',
    'scripts/test_no_secrets.py',
    'scripts/test_private_artifact_boundary.py',
    'scripts/test_ci_reference_pinning.py',
    'scripts/test_shell_syntax.py',
    'scripts/test_shell_strict_mode.py',
    'scripts/test_ansible_shell_blocks.py',
    'scripts/test_ansible_curl_timeout_contract.py',
    'scripts/test_ansible_until_contract.py',
    'scripts/test_ansible_failed_when_contract.py',
    'scripts/test_ansible_no_log_contract.py',
    'scripts/test_docs_make_targets.py',
    'scripts/test_markdown_links.py',
    'scripts/test_example_templates.py',
    'scripts/test_ansible_playbook_references.py',
    'scripts/test_gitops_application_contract.py',
    'scripts/test_kustomization_references.py',
    'scripts/test_gitops_helm_chart_pinning.py',
    'scripts/test_gitops_image_pinning.py',
    'scripts/test_makefile_help.py',
    'scripts/test_validation_surface_parity.py',
    'scripts/validate_platform_contract.py',
]
missing = [p for p in required if not (root / p).exists()]
if missing:
    print('Missing required files:')
    for item in missing:
        print(f' - {item}')
    sys.exit(1)

gitattributes_lines = {
    line.strip()
    for line in (root / '.gitattributes').read_text(encoding='utf-8').splitlines()
    if line.strip() and not line.lstrip().startswith('#')
}
for required_attr in (
    '.gitattributes text eol=lf',
    '.gitignore text eol=lf',
    '.helmignore text eol=lf',
    '.gitkeep text eol=lf',
    'LICENSE text eol=lf',
    'Makefile text eol=lf',
    'Dockerfile text eol=lf',
    '*.env text eol=lf',
    '*.env.example text eol=lf',
    '*.json text eol=lf',
    '*.lock text eol=lf',
    '*.gotmpl text eol=lf',
    '*.tpl text eol=lf',
    '*.txt text eol=lf',
    '*.sh text eol=lf',
    '*.py text eol=lf',
    '*.yml text eol=lf',
    '*.yaml text eol=lf',
    '*.ini text eol=lf',
    '*.cfg text eol=lf',
    '*.md text eol=lf',
):
    if required_attr not in gitattributes_lines:
        print(f'Missing required git attribute: {required_attr}')
        sys.exit(1)


def should_skip(path: Path) -> bool:
    return any(
        part in exclude_dirs
        or part.startswith('.shell-syntax-')
        or part.startswith('.ansible-shell-syntax-')
        for part in path.parts
    )


conflicted = []
for path in root.rglob('*'):
    if path.is_dir():
        continue
    if should_skip(path):
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
    committed_like = [p for p in root.rglob(pattern) if not should_skip(p)]
    if committed_like:
        print('Local/private files exist in working tree. They are ignored, but verify before pushing:')
        for p in committed_like:
            print(f' - {p.relative_to(root)}')

print('Project structure validation passed.')
