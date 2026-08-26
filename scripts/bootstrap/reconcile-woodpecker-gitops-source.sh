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

# Load only through the repository's strict KEY=value parser. Seed transport
# overrides may live beside the private render inputs.
# shellcheck source=scripts/bootstrap/load-env-file.sh
. "${source_root}/scripts/bootstrap/load-env-file.sh"
load_env_file "${env_file_path}" preserve-existing
deploy_branch="${PLATFORM_DEPLOY_BRANCH:-main}"

resolve_seed_base_url() {
  local first_server_line first_server_name seed_push_host seed_push_user seed_user_prefix
  local seed_git_root seed_git_repo_name

  first_server_line="$(
    awk '
      /^\[rke2_servers\]$/ { in_group=1; next }
      /^\[/ { in_group=0 }
      in_group && $0 !~ /^[[:space:]]*(#|$)/ { print; exit }
    ' "${inventory_file}"
  )"
  if [[ -z "${first_server_line}" ]]; then
    echo "Could not find the first rke2_servers host in ${inventory_file}." >&2
    return 1
  fi

  first_server_name="$(awk '{ print $1 }' <<<"${first_server_line}")"
  seed_push_host="${PLATFORM_SEED_PUSH_HOST:-}"
  seed_push_user="${PLATFORM_SEED_PUSH_USER:-}"
  if [[ -z "${seed_push_host}" ]]; then
    seed_push_host="$(
      tr ' ' '\n' <<<"${first_server_line}" |
        awk -F= '$1 == "ansible_host" { print $2; exit }'
    )"
  fi
  if [[ -z "${seed_push_user}" ]]; then
    seed_push_user="$(
      tr ' ' '\n' <<<"${first_server_line}" |
        awk -F= '$1 == "ansible_user" { print $2; exit }'
    )"
  fi

  seed_push_host="${seed_push_host:-${first_server_name}}"
  seed_user_prefix=""
  if [[ -n "${seed_push_user}" ]]; then
    seed_user_prefix="${seed_push_user}@"
  fi
  seed_git_root="${PLATFORM_SEED_GIT_ROOT:-/opt/platform/seed-git}"
  seed_git_repo_name="${PLATFORM_SEED_GIT_REPO_NAME:-platform-gitops.git}"
  printf 'ssh://%s%s%s/%s\n' \
    "${seed_user_prefix}" "${seed_push_host}" "${seed_git_root}" "${seed_git_repo_name}"
}

seed_base_url="${PLATFORM_WOODPECKER_REPAIR_SEED_BASE_URL:-$(resolve_seed_base_url)}"
seed_base_ref="${PLATFORM_WOODPECKER_REPAIR_SEED_BASE_REF:-refs/heads/${deploy_branch}}"
allow_empty_seed="${PLATFORM_WOODPECKER_REPAIR_ALLOW_EMPTY_SEED:-false}"
case "${allow_empty_seed}" in
  true|false) ;;
  *)
    echo "PLATFORM_WOODPECKER_REPAIR_ALLOW_EMPTY_SEED must be true or false." >&2
    exit 2
    ;;
esac
if [[ ! "${seed_base_ref}" =~ ^refs/heads/[A-Za-z0-9._/-]+$ &&
  ! "${seed_base_ref}" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "PLATFORM_WOODPECKER_REPAIR_SEED_BASE_REF must be a full branch ref or commit SHA." >&2
  exit 2
fi

is_known_private_render_output() {
  case "$1" in
    gitops/clusters/rke2-main/premium-3node/apps/forgejo/values.yaml | \
      gitops/clusters/rke2-main/premium-3node/apps/longhorn/values.yaml | \
      gitops/clusters/rke2-main/premium-3node/apps/longhorn/storageclasses.yaml | \
      gitops/clusters/rke2-main/premium-3node/apps/argocd-ha/values.yaml | \
      gitops/clusters/rke2-main/premium-3node/apps/woodpecker/values.yaml | \
      gitops/clusters/rke2-main/premium-3node/apps/harbor/values.yaml | \
      gitops/clusters/rke2-main/premium-3node/apps/monitoring/values.yaml | \
      gitops/clusters/rke2-main/premium-3node/apps/loki/values.yaml | \
      gitops/clusters/rke2-main/premium-3node/apps/velero/values.yaml | \
      gitops/clusters/rke2-main/premium-3node/apps/platform-postgres/postgres-cluster.yaml | \
      gitops/clusters/rke2-main/premium-3node/apps/platform-valkey/values.yaml | \
      gitops/clusters/rke2-main/premium-3node/apps/minio/values.yaml | \
      gitops/clusters/rke2-main/premium-3node/apps/keycloak/values.yaml | \
      gitops/clusters/rke2-main/premium-3node/apps/step-ca/values.yaml | \
      gitops/clusters/rke2-main/premium-3node/apps/platform-policies/no-plaintext-secrets.yaml | \
      gitops/clusters/rke2-main/premium-3node/apps/platform-policies/require-workload-baseline.yaml | \
      gitops/clusters/rke2-main/premium-3node/apps/platform-policies/require-pod-security-baseline.yaml | \
      gitops/clusters/rke2-main/premium-3node/apps/platform-image-integrity/verify-platform-images.yaml)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

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
seed_git_user_name="${PLATFORM_WOODPECKER_REPAIR_GIT_USER_NAME:-$(git -C "${source_root}" config user.name || true)}"
seed_git_user_email="${PLATFORM_WOODPECKER_REPAIR_GIT_USER_EMAIL:-$(git -C "${source_root}" config user.email || true)}"
git -C "${seed_checkout}" config user.name "${seed_git_user_name:-Platform GitOps Repair}"
git -C "${seed_checkout}" config user.email "${seed_git_user_email:-platform-gitops-repair@localhost}"
git -C "${seed_checkout}" config commit.gpgSign false

seed_base_head=""
seed_destination_head=""
if GIT_TERMINAL_PROMPT=0 git -C "${seed_checkout}" fetch --quiet --no-tags \
  "${seed_base_url}" "${seed_base_ref}"; then
  seed_base_head="$(git -C "${seed_checkout}" rev-parse FETCH_HEAD)"
  if ! seed_destination_refs="$(GIT_TERMINAL_PROMPT=0 git -C "${seed_checkout}" ls-remote \
    --heads "${seed_base_url}" "refs/heads/${deploy_branch}")"; then
    echo "Could not inspect destination seed branch refs/heads/${deploy_branch} at ${seed_base_url}." >&2
    exit 2
  fi
  seed_destination_head="$(awk '{ print $1; exit }' <<<"${seed_destination_refs}")"
  git -C "${seed_checkout}" checkout --quiet --detach "${seed_base_head}"
  if ! git -C "${seed_checkout}" merge --no-edit "${source_head}"; then
    conflict_paths=()
    mapfile -d '' conflict_paths < <(
      git -C "${seed_checkout}" diff --name-only --diff-filter=U -z
    )
    if (( ${#conflict_paths[@]} == 0 )); then
      echo "The existing private seed merge failed without resolvable file conflicts." >&2
      echo "No source or seed remote was changed; inspect the private seed base, then rerun." >&2
      exit 2
    fi

    unsafe_conflicts=()
    for conflict_path in "${conflict_paths[@]}"; do
      if ! is_known_private_render_output "${conflict_path}"; then
        unsafe_conflicts+=("${conflict_path}")
      fi
    done
    if (( ${#unsafe_conflicts[@]} > 0 )); then
      for conflict_path in "${unsafe_conflicts[@]}"; do
        printf 'private_seed_conflict=stop reason=outside-rendered-private-boundary path=%s\n' \
          "${conflict_path}" >&2
      done
      git -C "${seed_checkout}" merge --abort >/dev/null 2>&1 || true
      echo "The private seed conflicts with public source outside known rendered outputs." >&2
      echo "No source or seed remote was changed; reconcile those files manually, then rerun." >&2
      exit 2
    fi

    for conflict_path in "${conflict_paths[@]}"; do
      if git -C "${seed_checkout}" cat-file -e ":2:${conflict_path}" 2>/dev/null; then
        git -C "${seed_checkout}" checkout --ours -- "${conflict_path}"
        git -C "${seed_checkout}" add -- "${conflict_path}"
      else
        git -C "${seed_checkout}" rm --quiet --force --ignore-unmatch -- "${conflict_path}"
      fi
      printf 'private_seed_conflict=preserve-seed path=%s\n' "${conflict_path}"
    done

    unresolved_conflicts=()
    mapfile -d '' unresolved_conflicts < <(
      git -C "${seed_checkout}" diff --name-only --diff-filter=U -z
    )
    if (( ${#unresolved_conflicts[@]} > 0 )); then
      git -C "${seed_checkout}" merge --abort >/dev/null 2>&1 || true
      echo "Known private rendered conflicts could not be resolved safely." >&2
      echo "No source or seed remote was changed; inspect the private seed base, then rerun." >&2
      exit 2
    fi
    git -C "${seed_checkout}" commit --no-edit
    echo "Private seed merge retained known rendered private outputs and accepted public source elsewhere."
  fi
  echo "Private seed base preserved: ${seed_base_ref} (${seed_base_head})."
elif [[ "${allow_empty_seed}" == "true" ]]; then
  git -C "${seed_checkout}" checkout --quiet --detach "${source_head}"
  echo "Private seed base is absent; initializing from public source by explicit opt-in."
else
  echo "Could not fetch existing private seed base ${seed_base_ref} from ${seed_base_url}." >&2
  echo "Focused reconciliation stopped to avoid replacing previously rendered private applications." >&2
  echo "Set PLATFORM_WOODPECKER_REPAIR_ALLOW_EMPTY_SEED=true only for a confirmed empty seed repository." >&2
  exit 2
fi

mkdir -p "${seed_checkout}/inventory"
cp "${inventory_file}" "${seed_checkout}/inventory/hosts.local.ini"

echo "Reconciling rendered private Woodpecker values in an isolated temporary seed Git checkout."
(
  cd "${seed_checkout}"
  PLATFORM_SEED_DEPLOY_ENV_FILE="${env_file_path}" \
  PLATFORM_SEED_SYNC_PULL=false \
  PLATFORM_SEED_SYNC_PUSH_ORIGIN=false \
  PLATFORM_SEED_GIT_EXPECTED_HEAD="${seed_destination_head:-absent}" \
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
