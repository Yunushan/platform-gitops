#!/usr/bin/env python3
"""Run the repository validation suite with one portable entrypoint."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATION_SCRIPTS = (
    "scripts/validate_project.py",
    "scripts/test_python_syntax.py",
    "scripts/test_validation_runner.py",
    "scripts/test_line_endings.py",
    "scripts/test_profile_checker.py",
    "scripts/test_deployable_renderer.py",
    "scripts/test_gitops_selection_helper.py",
    "scripts/test_bootstrap_env_loader.py",
    "scripts/test_forge_migration.py",
    "scripts/test_forge_cutover.py",
    "scripts/test_forge_transition.py",
    "scripts/test_forge_fuzz_contract.py",
    "scripts/test_forge_coverage_contract.py",
    "scripts/test_forge_migration_live.py",
    "scripts/test_forge_migration_live_workflow.py",
    "scripts/test_private_values_renderer.py",
    "scripts/test_platform_secret_contract.py",
    "scripts/test_policy_examples.py",
    "scripts/test_image_integrity_contract.py",
    "scripts/test_pod_security_contract.py",
    "scripts/test_network_policy_contract.py",
    "scripts/test_internal_tls_contract.py",
    "scripts/test_observability_contract.py",
    "scripts/test_capacity_runtime_contract.py",
    "scripts/test_rendered_schema_contract.py",
    "scripts/test_sops_age_policy.py",
    "scripts/test_supply_chain_helpers.py",
    "scripts/test_supply_chain_evidence.py",
    "scripts/test_image_inventory_evidence.py",
    "scripts/test_release_workflow.py",
    "scripts/test_github_release_ref.py",
    "scripts/test_github_governance.py",
    "scripts/test_github_governance_configuration.py",
    "scripts/test_dependency_review_workflow.py",
    "scripts/test_backup_restore_runbook.py",
    "scripts/test_data_protection_contract.py",
    "scripts/test_production_evidence.py",
    "scripts/test_production_readiness_score.py",
    "scripts/test_business_continuity.py",
    "scripts/test_service_catalog.py",
    "scripts/test_architecture_decisions.py",
    "scripts/test_operations_runbook.py",
    "scripts/test_production_readiness_checklist.py",
    "scripts/test_platform_support.py",
    "scripts/test_incident_response_runbook.py",
    "scripts/test_access_control_runbook.py",
    "scripts/test_capacity_planning_runbook.py",
    "scripts/test_compliance_audit_runbook.py",
    "scripts/test_release_promotion_runbook.py",
    "scripts/test_alerting_runbook.py",
    "scripts/test_data_classification.py",
    "scripts/test_security_policy.py",
    "scripts/test_threat_model.py",
    "scripts/test_repository_governance.py",
    "scripts/test_codeowners_starter.py",
    "scripts/test_no_secrets.py",
    "scripts/test_private_artifact_boundary.py",
    "scripts/test_ci_reference_pinning.py",
    "scripts/test_shell_syntax.py",
    "scripts/test_shell_strict_mode.py",
    "scripts/test_ansible_shell_blocks.py",
    "scripts/test_ansible_curl_timeout_contract.py",
    "scripts/test_ansible_until_contract.py",
    "scripts/test_ansible_failed_when_contract.py",
    "scripts/test_ansible_no_log_contract.py",
    "scripts/test_empty_faulted_longhorn_claim_repair.py",
    "scripts/test_stuck_longhorn_attachment_repair.py",
    "scripts/test_docs_make_targets.py",
    "scripts/test_markdown_links.py",
    "scripts/test_example_templates.py",
    "scripts/test_ansible_playbook_references.py",
    "scripts/test_gitops_application_contract.py",
    "scripts/test_argocd_project_isolation.py",
    "scripts/test_kustomization_references.py",
    "scripts/test_gitops_helm_chart_pinning.py",
    "scripts/test_gitops_image_pinning.py",
    "scripts/test_makefile_help.py",
    "scripts/test_validation_surface_parity.py",
    "scripts/validate_platform_contract.py",
    "scripts/validate_no_secrets.py",
)


def env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def selected_scripts(skip_no_secrets: bool) -> list[str]:
    scripts = list(VALIDATION_SCRIPTS)
    if skip_no_secrets:
        scripts = [script for script in scripts if script != "scripts/validate_no_secrets.py"]
    return scripts


def run_script(script: str) -> int:
    env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    print(f"== {script} ==", flush=True)
    return subprocess.run([sys.executable, str(ROOT / script)], cwd=ROOT, env=env).returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run repository validation scripts.")
    parser.add_argument("--list", action="store_true", help="Print validation scripts and exit.")
    parser.add_argument(
        "--skip-no-secrets",
        action="store_true",
        help="Skip the final public secret/privacy scan.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    skip_no_secrets = args.skip_no_secrets or not env_flag("PLATFORM_RUN_NO_SECRETS", True)
    scripts = selected_scripts(skip_no_secrets)
    if args.list:
        for script in scripts:
            print(script)
        return 0

    for script in scripts:
        returncode = run_script(script)
        if returncode != 0:
            return returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
