#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repo_root}"

# shellcheck source=scripts/bootstrap/ensure-local-helm.sh
. scripts/bootstrap/ensure-local-helm.sh
ensure_local_helm

export ANSIBLE_TIMEOUT="${ANSIBLE_TIMEOUT:-20}"
exec ansible-playbook \
  -i inventory/hosts.local.ini \
  ansible/playbooks/repair-longhorn-crds.yml
