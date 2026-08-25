#!/usr/bin/env bash
set -euo pipefail

env_file="${PLATFORM_SEED_DEPLOY_ENV_FILE:-private/seed-git.env}"
# shellcheck source=scripts/bootstrap/load-env-file.sh
. scripts/bootstrap/load-env-file.sh
load_env_file "${env_file}" preserve-existing

PLATFORM_DEPLOY_BRANCH="${PLATFORM_DEPLOY_BRANCH:-main}"
PLATFORM_PROFILE="${PLATFORM_PROFILE:-premium-3node}"
PLATFORM_GITOPS_PLACEHOLDER_MODE="${PLATFORM_GITOPS_PLACEHOLDER_MODE:-skip-incomplete}"
PLATFORM_SOURCE_REMOTE_NAME="${PLATFORM_SOURCE_REMOTE_NAME:-origin}"
PLATFORM_SEED_GIT_REMOTE_NAME="${PLATFORM_SEED_GIT_REMOTE_NAME:-seed}"
PLATFORM_SEED_GIT_ROOT="${PLATFORM_SEED_GIT_ROOT:-/opt/platform/seed-git}"
PLATFORM_SEED_GIT_REPO_NAME="${PLATFORM_SEED_GIT_REPO_NAME:-platform-gitops.git}"
PLATFORM_SEED_GIT_PORT="${PLATFORM_SEED_GIT_PORT:-9418}"
PLATFORM_SEED_GIT_OWNER="${PLATFORM_SEED_GIT_OWNER:-}"
PLATFORM_SEED_GIT_WAIT_TIMEOUT="${PLATFORM_SEED_GIT_WAIT_TIMEOUT:-45}"
PLATFORM_SEED_GIT_FORCE_WITH_LEASE="${PLATFORM_SEED_GIT_FORCE_WITH_LEASE:-true}"
PLATFORM_SEED_SYNC_PULL="${PLATFORM_SEED_SYNC_PULL:-true}"
PLATFORM_SEED_SYNC_PUSH_ORIGIN="${PLATFORM_SEED_SYNC_PUSH_ORIGIN:-false}"
PLATFORM_SEED_SYNC_ENSURE_SERVICE="${PLATFORM_SEED_SYNC_ENSURE_SERVICE:-true}"
PLATFORM_AUTO_RENDER_PRIVATE_VALUES="${PLATFORM_AUTO_RENDER_PRIVATE_VALUES:-false}"
PLATFORM_AUTO_RENDER_SCOPE="${PLATFORM_AUTO_RENDER_SCOPE:-all}"
PLATFORM_AUTO_COMMIT="${PLATFORM_AUTO_COMMIT:-false}"
PLATFORM_AUTO_COMMIT_MESSAGE="${PLATFORM_AUTO_COMMIT_MESSAGE:-Sync platform GitOps deployment}"
PLATFORM_VALIDATE_BEFORE_PUSH="${PLATFORM_VALIDATE_BEFORE_PUSH:-true}"
PLATFORM_RUN_NO_SECRETS="${PLATFORM_RUN_NO_SECRETS:-true}"
PLATFORM_RUN_PROFILE_CHECK="${PLATFORM_RUN_PROFILE_CHECK:-true}"
PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES="${PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES:-false}"

resolve_python() {
  if [[ -n "${PYTHON:-}" ]]; then
    printf '%s\n' "${PYTHON}"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' python3
    return
  fi
  if command -v python >/dev/null 2>&1; then
    printf '%s\n' python
    return
  fi
  echo "Python is required for seed Git sync validation; install python3 or set PYTHON." >&2
  exit 1
}

PYTHON_BIN="$(resolve_python)"

export PLATFORM_SEED_GIT_ROOT
export PLATFORM_SEED_GIT_REPO_NAME
export PLATFORM_SEED_GIT_PORT
export PLATFORM_SEED_GIT_OWNER
export PLATFORM_SEED_GIT_WAIT_TIMEOUT
export PLATFORM_PROFILE
export PLATFORM_GITOPS_PLACEHOLDER_MODE

git rev-parse --is-inside-work-tree >/dev/null

commit_or_fail_dirty_worktree() {
  if [[ -z "$(git status --porcelain)" ]]; then
    return
  fi

  if [[ "${PLATFORM_AUTO_COMMIT}" == "true" ]]; then
    git add -A
    if ! git diff --cached --quiet; then
      git commit -m "${PLATFORM_AUTO_COMMIT_MESSAGE}"
    fi
  else
    echo "Working tree has uncommitted changes and PLATFORM_AUTO_COMMIT is not true." >&2
    echo "Commit them manually, or rerun with PLATFORM_AUTO_COMMIT=true." >&2
    exit 1
  fi
}

commit_or_fail_dirty_worktree

if [[ "${PLATFORM_SEED_SYNC_PULL}" == "true" ]]; then
  if git remote get-url "${PLATFORM_SOURCE_REMOTE_NAME}" >/dev/null 2>&1; then
    GIT_TERMINAL_PROMPT=0 git pull --rebase "${PLATFORM_SOURCE_REMOTE_NAME}" "${PLATFORM_DEPLOY_BRANCH}"
  else
    echo "Source remote ${PLATFORM_SOURCE_REMOTE_NAME} does not exist; skipping source remote pull." >&2
  fi
fi

if [[ "${PLATFORM_AUTO_RENDER_PRIVATE_VALUES}" == "true" ]]; then
  render_args=(--inventory inventory/hosts.local.ini)
  case "${PLATFORM_AUTO_RENDER_SCOPE}" in
    all)
      ;;
    woodpecker)
      # A focused CI repair must not re-render unrelated production apps and
      # accidentally require their private object-storage credentials.
      render_args+=(
        --skip-argocd
        --skip-forgejo
        --skip-longhorn
        --skip-harbor
        --skip-monitoring
        --skip-loki
        --skip-velero
        --skip-cnpg-postgres-cluster
        --skip-platform-valkey
        --skip-minio
        --skip-keycloak
        --skip-step-ca
        --skip-platform-image-integrity
      )
      ;;
    *)
      echo "PLATFORM_AUTO_RENDER_SCOPE must be all or woodpecker." >&2
      exit 1
      ;;
  esac
  "${PYTHON_BIN}" scripts/render_private_platform_values.py "${render_args[@]}"
  commit_or_fail_dirty_worktree
fi

if [[ "${PLATFORM_VALIDATE_BEFORE_PUSH}" == "true" ]]; then
  if [[ "${PLATFORM_RUN_PROFILE_CHECK}" == "true" ]]; then
    PYTHON="${PYTHON_BIN}" bash scripts/bootstrap/validate-gitops-selection.sh .
  fi
  PLATFORM_RUN_NO_SECRETS="${PLATFORM_RUN_NO_SECRETS}" \
    PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES="${PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES}" \
    "${PYTHON_BIN}" scripts/run_validation.py
fi

if [[ "${PLATFORM_SEED_SYNC_PUSH_ORIGIN}" == "true" ]]; then
  if git remote get-url "${PLATFORM_SOURCE_REMOTE_NAME}" >/dev/null 2>&1; then
    source_remote_head="$(
      git rev-parse --verify "${PLATFORM_SOURCE_REMOTE_NAME}/${PLATFORM_DEPLOY_BRANCH}" 2>/dev/null || true
    )"
    local_head="$(git rev-parse HEAD)"
    if [[ -n "${source_remote_head}" && "${source_remote_head}" == "${local_head}" ]]; then
      echo "${PLATFORM_SOURCE_REMOTE_NAME}/${PLATFORM_DEPLOY_BRANCH} already matches HEAD; skipping source remote push."
    else
      GIT_TERMINAL_PROMPT=0 git push "${PLATFORM_SOURCE_REMOTE_NAME}" "HEAD:${PLATFORM_DEPLOY_BRANCH}"
    fi
  else
    echo "Source remote ${PLATFORM_SOURCE_REMOTE_NAME} does not exist; skipping origin push." >&2
  fi
else
  echo "Skipping source remote push. Set PLATFORM_SEED_SYNC_PUSH_ORIGIN=true to push ${PLATFORM_SOURCE_REMOTE_NAME}/${PLATFORM_DEPLOY_BRANCH}."
fi

if [[ "${PLATFORM_SEED_SYNC_ENSURE_SERVICE}" == "true" ]]; then
  ANSIBLE_TIMEOUT="${ANSIBLE_TIMEOUT:-20}" ansible-playbook \
    -i inventory/hosts.local.ini \
    ansible/playbooks/deploy-seed-git.yml
fi

first_server_line="$(
  awk '
    /^\[rke2_servers\]$/ { in_group=1; next }
    /^\[/ { in_group=0 }
    in_group && $0 !~ /^[[:space:]]*(#|$)/ { print; exit }
  ' inventory/hosts.local.ini
)"

if [[ -z "${first_server_line}" ]]; then
  echo "Could not find the first rke2_servers host in inventory/hosts.local.ini." >&2
  exit 1
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

seed_push_url="ssh://${seed_user_prefix}${seed_push_host}${PLATFORM_SEED_GIT_ROOT}/${PLATFORM_SEED_GIT_REPO_NAME}"
seed_read_host="${PLATFORM_SEED_READ_HOST:-${seed_push_host}}"
seed_read_url="git://${seed_read_host}:${PLATFORM_SEED_GIT_PORT}/${PLATFORM_SEED_GIT_REPO_NAME}"

if git remote get-url "${PLATFORM_SEED_GIT_REMOTE_NAME}" >/dev/null 2>&1; then
  git remote set-url "${PLATFORM_SEED_GIT_REMOTE_NAME}" "${seed_push_url}"
else
  git remote add "${PLATFORM_SEED_GIT_REMOTE_NAME}" "${seed_push_url}"
fi

if [[ "${PLATFORM_SEED_GIT_FORCE_WITH_LEASE}" == "true" ]]; then
  seed_remote_head="$(
    GIT_TERMINAL_PROMPT=0 git ls-remote --heads "${PLATFORM_SEED_GIT_REMOTE_NAME}" "${PLATFORM_DEPLOY_BRANCH}" |
      awk '{ print $1; exit }'
  )"
  if [[ -n "${seed_remote_head}" ]]; then
    echo "Updating temporary seed Git mirror with --force-with-lease."
    GIT_TERMINAL_PROMPT=0 git push \
      --force-with-lease="refs/heads/${PLATFORM_DEPLOY_BRANCH}:${seed_remote_head}" \
      "${PLATFORM_SEED_GIT_REMOTE_NAME}" "HEAD:${PLATFORM_DEPLOY_BRANCH}"
  else
    GIT_TERMINAL_PROMPT=0 git push "${PLATFORM_SEED_GIT_REMOTE_NAME}" "HEAD:${PLATFORM_DEPLOY_BRANCH}"
  fi
else
  GIT_TERMINAL_PROMPT=0 git push "${PLATFORM_SEED_GIT_REMOTE_NAME}" "HEAD:${PLATFORM_DEPLOY_BRANCH}"
fi

cat <<EOF

Platform GitOps repository synchronized.

Source remote:
- ${PLATFORM_SOURCE_REMOTE_NAME}/${PLATFORM_DEPLOY_BRANCH} (push enabled: ${PLATFORM_SEED_SYNC_PUSH_ORIGIN})

Temporary seed Git:
- write: ${seed_push_url}
- read:  ${seed_read_url}

Argo CD source URL should remain:
${seed_read_url}
EOF
