#!/usr/bin/env bash
set -euo pipefail

env_file="${PLATFORM_FIRST_DEPLOY_ENV_FILE:-private/first-deploy.env}"
# shellcheck source=scripts/bootstrap/load-env-file.sh
. scripts/bootstrap/load-env-file.sh
load_env_file "${env_file}"

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "Set ${name} in ${env_file} or export it before running this target." >&2
    exit 1
  fi
}

require_env PLATFORM_REPO_URL

PLATFORM_PROFILE="${PLATFORM_PROFILE:-premium-3node}"
PLATFORM_GITOPS_PLACEHOLDER_MODE="${PLATFORM_GITOPS_PLACEHOLDER_MODE:-skip-incomplete}"
PLATFORM_FIRST_DEPLOY_DNS_REPAIR="${PLATFORM_FIRST_DEPLOY_DNS_REPAIR:-true}"
PLATFORM_AUTO_RENDER_PRIVATE_VALUES="${PLATFORM_AUTO_RENDER_PRIVATE_VALUES:-true}"
PLATFORM_DEPLOY_BRANCH="${PLATFORM_DEPLOY_BRANCH:-main}"
PLATFORM_DEPLOY_REMOTE_NAME="${PLATFORM_DEPLOY_REMOTE_NAME:-deploy}"
PLATFORM_AUTO_COMMIT="${PLATFORM_AUTO_COMMIT:-false}"
PLATFORM_AUTO_COMMIT_MESSAGE="${PLATFORM_AUTO_COMMIT_MESSAGE:-Configure private platform deployment}"
PLATFORM_AUTO_PUSH="${PLATFORM_AUTO_PUSH:-true}"
PLATFORM_PUSH_WITH_TOKEN="${PLATFORM_PUSH_WITH_TOKEN:-true}"
PLATFORM_VALIDATE_BEFORE_PUSH="${PLATFORM_VALIDATE_BEFORE_PUSH:-true}"
PLATFORM_RUN_NO_SECRETS="${PLATFORM_RUN_NO_SECRETS:-true}"
PLATFORM_RUN_PROFILE_CHECK="${PLATFORM_RUN_PROFILE_CHECK:-true}"
PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES="${PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES:-true}"

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
  echo "Python is required for private first deploy validation; install python3 or set PYTHON." >&2
  exit 1
}

PYTHON_BIN="$(resolve_python)"

export PLATFORM_PROFILE PLATFORM_GITOPS_PLACEHOLDER_MODE PLATFORM_FIRST_DEPLOY_DNS_REPAIR PLATFORM_REPO_URL
export PLATFORM_AUTO_RENDER_PRIVATE_VALUES

if [[ "${PLATFORM_AUTO_RENDER_PRIVATE_VALUES}" == "true" ]]; then
  "${PYTHON_BIN}" scripts/render_private_platform_values.py --inventory inventory/hosts.local.ini
fi

if [[ "${PLATFORM_VALIDATE_BEFORE_PUSH}" == "true" ]]; then
  if [[ "${PLATFORM_RUN_PROFILE_CHECK}" == "true" ]]; then
    PYTHON="${PYTHON_BIN}" bash scripts/bootstrap/validate-gitops-selection.sh .
  fi
  PLATFORM_RUN_NO_SECRETS="${PLATFORM_RUN_NO_SECRETS}" \
    PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES="${PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES}" \
    "${PYTHON_BIN}" scripts/run_validation.py
fi

git rev-parse --is-inside-work-tree >/dev/null

if git remote get-url "${PLATFORM_DEPLOY_REMOTE_NAME}" >/dev/null 2>&1; then
  git remote set-url "${PLATFORM_DEPLOY_REMOTE_NAME}" "${PLATFORM_REPO_URL}"
else
  git remote add "${PLATFORM_DEPLOY_REMOTE_NAME}" "${PLATFORM_REPO_URL}"
fi

if [[ -n "$(git status --porcelain)" ]]; then
  if [[ "${PLATFORM_AUTO_COMMIT}" == "true" ]]; then
    git add -A
    if ! git diff --cached --quiet; then
      git commit -m "${PLATFORM_AUTO_COMMIT_MESSAGE}"
    fi
  else
    echo "Working tree has uncommitted changes and PLATFORM_AUTO_COMMIT is not true." >&2
    echo "Commit them manually, or set PLATFORM_AUTO_COMMIT=true in ${env_file}." >&2
    exit 1
  fi
fi

if [[ "${PLATFORM_AUTO_PUSH}" == "true" ]]; then
  if [[ "${PLATFORM_PUSH_WITH_TOKEN}" == "true" && -n "${PLATFORM_REPO_TOKEN:-}" ]]; then
    repo_username="${PLATFORM_REPO_USERNAME:-x-access-token}"
    auth_header="$(
      printf '%s:%s' "${repo_username}" "${PLATFORM_REPO_TOKEN}" |
        base64 |
        tr -d '\n'
    )"
    GIT_TERMINAL_PROMPT=0 git \
      -c credential.helper= \
      -c "http.extraHeader=Authorization: Basic ${auth_header}" \
      push "${PLATFORM_REPO_URL}" "HEAD:${PLATFORM_DEPLOY_BRANCH}"
  else
    GIT_TERMINAL_PROMPT=0 git push "${PLATFORM_DEPLOY_REMOTE_NAME}" "HEAD:${PLATFORM_DEPLOY_BRANCH}"
  fi
fi

export PLATFORM_APPLY_GITOPS=true
make platform-first-deploy
