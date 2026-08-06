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
# Force every opt-out health control on for production acceptance. These are
# exported after loading the private environment so debug settings cannot
# silently weaken the production gate.
export PLATFORM_APP_HEALTH_INCLUDE_EXISTING_APPS=true
export PLATFORM_APP_HEALTH_FORBID_TEMPORARY_REPO=true
export PLATFORM_APP_HEALTH_OPENBAO_READY=true
export PLATFORM_APP_HEALTH_REGISTRY_API=true
export PLATFORM_APP_HEALTH_MONITORING_API=true
export PLATFORM_APP_HEALTH_STEP_CA_API=true
export PLATFORM_APP_HEALTH_LOKI_API=true
export PLATFORM_APP_HEALTH_VELERO_BACKUP_STORAGE=true
export PLATFORM_APP_HEALTH_VELERO_SCHEDULES=true
export PLATFORM_APP_HEALTH_APP_SECRETS=auto
export PLATFORM_APP_HEALTH_HARBOR_PRODUCTION_SECRETS=true
export PLATFORM_APP_HEALTH_FORGEJO_PRODUCTION_SECRETS=true
export PLATFORM_APP_HEALTH_GRAFANA_DATABASE_SECRET=true
export PLATFORM_APP_HEALTH_CNPG_OBJECT_STORAGE_SECRET=true
export PLATFORM_APP_HEALTH_SSO=true
export PLATFORM_APP_HEALTH_ARGOCD_GUARDED_PRUNE=true
export PLATFORM_APP_HEALTH_ARGOCD_RUNTIME=true
export PLATFORM_APP_HEALTH_LONGHORN_RUNTIME=true
export PLATFORM_APP_HEALTH_HA_REPLICAS=true
export PLATFORM_APP_HEALTH_FORGEJO_SINGLETON_SAFETY=true
export PLATFORM_APP_HEALTH_HTTP_REDIRECT=true
export PLATFORM_APP_HEALTH_NODE_INGRESS_STRICT=true

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
