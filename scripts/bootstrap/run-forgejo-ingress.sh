#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$repo_root"

env_file=
if [[ -n "${PLATFORM_FORGEJO_INGRESS_ENV_FILE:-}" ]]; then
  env_file="${PLATFORM_FORGEJO_INGRESS_ENV_FILE}"
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

ansible_args=(
  -i inventory/hosts.local.ini
)
forgejo_host_override="${PLATFORM_FORGEJO_HOST:-${PLATFORM_GIT_HOST:-}}"
if [[ -n "${forgejo_host_override}" ]]; then
  # Match the private renderer's env-first hostname contract even when a stale
  # optional inventory alias remains on the controller.
  ansible_args+=(--extra-vars "platform_forgejo_host=${forgejo_host_override}")
fi
ansible_args+=(ansible/playbooks/publish-forgejo-ingress.yml)

ANSIBLE_TIMEOUT="${ANSIBLE_TIMEOUT:-20}" ansible-playbook "${ansible_args[@]}"
