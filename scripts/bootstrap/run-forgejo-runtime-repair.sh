#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$repo_root"

env_file=
if [ -n "${PLATFORM_FORGEJO_RUNTIME_REPAIR_ENV_FILE:-}" ]; then
  env_file="$PLATFORM_FORGEJO_RUNTIME_REPAIR_ENV_FILE"
elif [ -n "${PLATFORM_SEED_DEPLOY_ENV_FILE:-}" ]; then
  env_file="$PLATFORM_SEED_DEPLOY_ENV_FILE"
elif [ -n "${PLATFORM_FIRST_DEPLOY_ENV_FILE:-}" ]; then
  env_file="$PLATFORM_FIRST_DEPLOY_ENV_FILE"
elif [ -f private/seed-git.env ]; then
  env_file=private/seed-git.env
elif [ -f private/first-deploy.env ]; then
  env_file=private/first-deploy.env
fi

if [ -n "$env_file" ]; then
  # shellcheck source=scripts/bootstrap/load-env-file.sh
  . scripts/bootstrap/load-env-file.sh
  load_env_file "$env_file" preserve-existing
fi

ANSIBLE_TIMEOUT="${ANSIBLE_TIMEOUT:-20}" ansible-playbook \
  -i inventory/hosts.local.ini \
  ansible/playbooks/repair-forgejo-runtime.yml
