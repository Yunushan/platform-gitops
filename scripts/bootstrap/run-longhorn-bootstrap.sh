#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repo_root}"

if [[ -n "${PLATFORM_LONGHORN_ENV_FILE:-}" ]]; then
  env_file="${PLATFORM_LONGHORN_ENV_FILE}"
elif [[ -n "${PLATFORM_SEED_DEPLOY_ENV_FILE:-}" ]]; then
  env_file="${PLATFORM_SEED_DEPLOY_ENV_FILE}"
elif [[ -n "${PLATFORM_FIRST_DEPLOY_ENV_FILE:-}" ]]; then
  env_file="${PLATFORM_FIRST_DEPLOY_ENV_FILE}"
elif [[ -f private/seed-git.env ]]; then
  env_file=private/seed-git.env
else
  env_file=private/first-deploy.env
fi

# shellcheck source=scripts/bootstrap/load-env-file.sh
. scripts/bootstrap/load-env-file.sh
load_env_file "${env_file}" preserve-existing

export ANSIBLE_TIMEOUT="${ANSIBLE_TIMEOUT:-20}"
exec ansible-playbook \
  -i inventory/hosts.local.ini \
  ansible/playbooks/bootstrap-longhorn.yml
