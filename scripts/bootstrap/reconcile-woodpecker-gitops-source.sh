#!/usr/bin/env bash
set -euo pipefail

mode="${PLATFORM_WOODPECKER_REPAIR_SYNC_GITOPS:-auto}"
env_file="${PLATFORM_SEED_DEPLOY_ENV_FILE:-private/seed-git.env}"
source_root="$(git rev-parse --show-toplevel)"
source_head="$(git rev-parse HEAD)"

case "${mode}" in
  auto|true|false) ;;
  *)
    echo "PLATFORM_WOODPECKER_REPAIR_SYNC_GITOPS must be auto, true, or false." >&2
    exit 2
    ;;
esac

if [[ "${mode}" == "false" ]]; then
  echo "woodpecker_gitops_source_sync=skipped reason=disabled"
  exit 0
fi

if [[ ! -f "${env_file}" ]]; then
  if [[ "${mode}" == "true" ]]; then
    echo "Woodpecker GitOps source reconciliation requires ${env_file}." >&2
    exit 2
  fi
  echo "woodpecker_gitops_source_sync=skipped reason=private-seed-env-absent"
  exit 0
fi

cd "${source_root}"
if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  echo "Woodpecker GitOps source reconciliation requires a clean working tree." >&2
  echo "Commit or stash unrelated changes, then rerun make platform-woodpecker-repair." >&2
  exit 2
fi

env_file_dir="$(cd "$(dirname "${env_file}")" && pwd -P)"
env_file_path="${env_file_dir}/$(basename "${env_file}")"
inventory_file="${source_root}/inventory/hosts.local.ini"
if [[ ! -f "${inventory_file}" ]]; then
  echo "Woodpecker GitOps source reconciliation requires ${inventory_file}." >&2
  exit 2
fi

umask 077
temporary_root="$(mktemp -d "${TMPDIR:-/tmp}/platform-woodpecker-seed.XXXXXX")"
seed_checkout="${temporary_root}/repo"
touch "${temporary_root}/.platform-woodpecker-seed-workspace"

cleanup() {
  if [[ -n "${temporary_root:-}" && -f "${temporary_root}/.platform-woodpecker-seed-workspace" ]]; then
    rm -rf -- "${temporary_root}"
  fi
}
trap cleanup EXIT

git clone --quiet --no-hardlinks --no-checkout "${source_root}" "${seed_checkout}"
git -C "${seed_checkout}" checkout --quiet --detach "${source_head}"
mkdir -p "${seed_checkout}/inventory"
cp "${inventory_file}" "${seed_checkout}/inventory/hosts.local.ini"

echo "Reconciling rendered private Woodpecker values in an isolated temporary seed Git checkout."
(
  cd "${seed_checkout}"
  PLATFORM_SEED_DEPLOY_ENV_FILE="${env_file_path}" \
  PLATFORM_SEED_SYNC_PULL=false \
  PLATFORM_SEED_SYNC_PUSH_ORIGIN=false \
  PLATFORM_AUTO_RENDER_SCOPE=woodpecker \
  PLATFORM_AUTO_RENDER_PRIVATE_VALUES=true \
  PLATFORM_AUTO_COMMIT=true \
  PLATFORM_AUTO_COMMIT_MESSAGE="${PLATFORM_WOODPECKER_REPAIR_COMMIT_MESSAGE:-Reconcile Woodpecker GitOps source}" \
  PLATFORM_VALIDATE_BEFORE_PUSH=true \
  PLATFORM_RUN_PROFILE_CHECK=true \
  PLATFORM_RUN_NO_SECRETS=true \
  PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES=true \
  make platform-seed-git-sync
)

echo "woodpecker_gitops_source_sync=synced"
