#!/usr/bin/env bash
set -euo pipefail
umask 077

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repo_root}"

env_file=""
if [[ -n "${PLATFORM_PRODUCTION_CHECK_ENV_FILE:-}" ]]; then
  env_file="${PLATFORM_PRODUCTION_CHECK_ENV_FILE}"
elif [[ -n "${PLATFORM_SEED_DEPLOY_ENV_FILE:-}" ]]; then
  env_file="${PLATFORM_SEED_DEPLOY_ENV_FILE}"
elif [[ -n "${PLATFORM_FIRST_DEPLOY_ENV_FILE:-}" ]]; then
  env_file="${PLATFORM_FIRST_DEPLOY_ENV_FILE}"
elif [[ -f private/seed-git.env ]]; then
  env_file=private/seed-git.env
elif [[ -f private/first-deploy.env ]]; then
  env_file=private/first-deploy.env
fi

if [[ -n "${env_file}" && ! -f "${env_file}" ]]; then
  printf 'Selected production environment file does not exist: %s\n' "${env_file}" >&2
  exit 1
fi

if [[ -n "${env_file}" ]]; then
  # shellcheck source=scripts/bootstrap/load-env-file.sh
  . scripts/bootstrap/load-env-file.sh
  load_env_file "${env_file}" preserve-existing
fi

# This target is a production gate, so a private bootstrap environment cannot
# downgrade its strictness after it has been loaded.
export PLATFORM_PRODUCTION_STRICT=true

make_command="${MAKE:-make}"

"${make_command}" platform-profile-check
"${make_command}" rke2-verify
"${make_command}" platform-status
"${make_command}" platform-tls-verify
"${make_command}" platform-image-inventory-verify
"${make_command}" policy-cel-verify
PLATFORM_POLICY_ENFORCEMENT=Enforce \
PLATFORM_IMAGE_INTEGRITY_MODE=Enforce \
PLATFORM_IMAGE_INTEGRITY_REQUIRED=true \
  "${make_command}" platform-policy-readiness
"${make_command}" platform-network-isolation-verify
"${make_command}" platform-internal-tls-verify
"${make_command}" platform-openbao-verify
PLATFORM_ALERT_DELIVERY_TEST=true "${make_command}" platform-observability-verify
"${make_command}" platform-capacity-verify
# Keep the child health gate on the exact same selected private environment.
PLATFORM_APP_HEALTH_ENV_FILE="${env_file}" PLATFORM_APP_HEALTH_MODE=production "${make_command}" platform-app-health
"${make_command}" platform-data-protection
