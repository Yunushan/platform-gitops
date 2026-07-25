#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repo_root}"

env_file=""
if [[ -n "${PLATFORM_DATA_PROTECTION_ENV_FILE:-}" ]]; then
  env_file="${PLATFORM_DATA_PROTECTION_ENV_FILE}"
elif [[ -n "${PLATFORM_SEED_DEPLOY_ENV_FILE:-}" ]]; then
  env_file="${PLATFORM_SEED_DEPLOY_ENV_FILE}"
elif [[ -n "${PLATFORM_FIRST_DEPLOY_ENV_FILE:-}" ]]; then
  env_file="${PLATFORM_FIRST_DEPLOY_ENV_FILE}"
elif [[ -f private/seed-git.env ]]; then
  env_file=private/seed-git.env
elif [[ -f private/first-deploy.env ]]; then
  env_file=private/first-deploy.env
fi

if [[ -n "${env_file}" ]]; then
  # shellcheck source=scripts/bootstrap/load-env-file.sh
  . scripts/bootstrap/load-env-file.sh
  load_env_file "${env_file}" preserve-existing
fi

evidence_file="${PLATFORM_RESTORE_EVIDENCE_FILE:-}"
if [[ -z "${evidence_file}" ]]; then
  printf '%s\n' \
    'PLATFORM_RESTORE_EVIDENCE_FILE is required for production readiness.' \
    'Copy examples/restore-evidence.example.json into private/, record a real isolated restore drill, and reference that ignored file.' >&2
  exit 1
fi

python3 scripts/verify_restore_evidence.py \
  "${evidence_file}" \
  --max-age-days "${PLATFORM_RESTORE_DRILL_MAX_AGE_DAYS:-92}" \
  --expected-profile "${PLATFORM_PROFILE:-premium-3node}"

export ANSIBLE_TIMEOUT="${ANSIBLE_TIMEOUT:-20}"
exec ansible-playbook \
  -i inventory/hosts.local.ini \
  ansible/playbooks/verify-platform-data-protection.yml
