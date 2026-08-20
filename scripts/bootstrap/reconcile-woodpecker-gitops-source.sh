#!/usr/bin/env bash
set -euo pipefail

mode="${PLATFORM_WOODPECKER_REPAIR_SYNC_GITOPS:-auto}"
env_file="${PLATFORM_SEED_DEPLOY_ENV_FILE:-private/seed-git.env}"

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

git rev-parse --is-inside-work-tree >/dev/null
if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  echo "Woodpecker GitOps source reconciliation requires a clean working tree." >&2
  echo "Commit or stash unrelated changes, then rerun make platform-woodpecker-repair." >&2
  exit 2
fi

echo "Reconciling rendered private Woodpecker values into the temporary seed Git source."
PLATFORM_SEED_DEPLOY_ENV_FILE="${env_file}" \
PLATFORM_SEED_SYNC_PULL="${PLATFORM_WOODPECKER_REPAIR_SYNC_PULL:-false}" \
PLATFORM_SEED_SYNC_PUSH_ORIGIN="${PLATFORM_WOODPECKER_REPAIR_SYNC_PUSH_ORIGIN:-false}" \
PLATFORM_AUTO_RENDER_PRIVATE_VALUES=true \
PLATFORM_AUTO_COMMIT=true \
PLATFORM_AUTO_COMMIT_MESSAGE="${PLATFORM_WOODPECKER_REPAIR_COMMIT_MESSAGE:-Reconcile Woodpecker GitOps source}" \
make platform-seed-git-sync

echo "woodpecker_gitops_source_sync=synced"
