#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repo_root}"

env_file=""
if [[ -n "${PLATFORM_FORGEJO_RECOVERY_ENV_FILE:-}" ]]; then
  env_file="${PLATFORM_FORGEJO_RECOVERY_ENV_FILE}"
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

require_value() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    printf '%s is required for the Forgejo recovery drill.\n' "${name}" >&2
    exit 1
  fi
}

require_value PLATFORM_FORGEJO_RECOVERY_CONFIRMATION
require_value PLATFORM_FORGEJO_RECOVERY_OPERATOR
require_value PLATFORM_FORGEJO_RECOVERY_APPROVER
if [[ "${PLATFORM_FORGEJO_RECOVERY_CONFIRMATION}" != "FAILOVER_FORGEJO_SINGLETON" ]]; then
  printf '%s\n' \
    'PLATFORM_FORGEJO_RECOVERY_CONFIRMATION must be FAILOVER_FORGEJO_SINGLETON.' >&2
  exit 1
fi
if [[ "${PLATFORM_FORGEJO_RECOVERY_OPERATOR,,}" == "${PLATFORM_FORGEJO_RECOVERY_APPROVER,,}" ]]; then
  printf '%s\n' 'Forgejo recovery operator and approver must be different people.' >&2
  exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
export PLATFORM_PROFILE="${PLATFORM_PROFILE:-premium-3node}"
export PLATFORM_FORGEJO_RECOVERY_DRILL_ID="${PLATFORM_FORGEJO_RECOVERY_DRILL_ID:-forgejo-${timestamp}}"
export PLATFORM_FORGEJO_RECOVERY_SOURCE_COMMIT="$(git rev-parse HEAD)"
export PLATFORM_FORGEJO_RECOVERY_EVIDENCE_FILE="${PLATFORM_FORGEJO_RECOVERY_EVIDENCE_FILE:-private/forgejo-recovery-evidence.json}"

case "${PLATFORM_FORGEJO_RECOVERY_EVIDENCE_FILE}" in
  private/*.json) ;;
  *)
    printf '%s\n' 'PLATFORM_FORGEJO_RECOVERY_EVIDENCE_FILE must be a JSON path below private/.' >&2
    exit 1
    ;;
esac
if [[ "${PLATFORM_FORGEJO_RECOVERY_EVIDENCE_FILE}" == *..* ]]; then
  printf '%s\n' 'PLATFORM_FORGEJO_RECOVERY_EVIDENCE_FILE must not contain .. path segments.' >&2
  exit 1
fi
mkdir -p "$(dirname "${PLATFORM_FORGEJO_RECOVERY_EVIDENCE_FILE}")"

export ANSIBLE_TIMEOUT="${ANSIBLE_TIMEOUT:-20}"
ansible-playbook \
  -i inventory/hosts.local.ini \
  ansible/playbooks/run-forgejo-recovery-drill.yml

python3 scripts/verify_forgejo_recovery_evidence.py \
  "${PLATFORM_FORGEJO_RECOVERY_EVIDENCE_FILE}" \
  --max-age-days 1 \
  --expected-profile "${PLATFORM_PROFILE}" \
  --expected-commit "${PLATFORM_FORGEJO_RECOVERY_SOURCE_COMMIT}"
