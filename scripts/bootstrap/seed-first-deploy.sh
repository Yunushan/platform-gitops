#!/usr/bin/env bash
set -euo pipefail

env_file="${PLATFORM_SEED_DEPLOY_ENV_FILE:-private/seed-git.env}"
if [[ -f "${env_file}" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "${env_file}"
  set +a
fi

PLATFORM_PROFILE="${PLATFORM_PROFILE:-premium-3node}"
PLATFORM_GITOPS_PLACEHOLDER_MODE="${PLATFORM_GITOPS_PLACEHOLDER_MODE:-skip-incomplete}"
PLATFORM_FIRST_DEPLOY_DNS_REPAIR="${PLATFORM_FIRST_DEPLOY_DNS_REPAIR:-true}"
PLATFORM_AUTO_RENDER_PRIVATE_VALUES="${PLATFORM_AUTO_RENDER_PRIVATE_VALUES:-true}"
PLATFORM_DEPLOY_BRANCH="${PLATFORM_DEPLOY_BRANCH:-main}"
PLATFORM_AUTO_COMMIT="${PLATFORM_AUTO_COMMIT:-false}"
PLATFORM_AUTO_COMMIT_MESSAGE="${PLATFORM_AUTO_COMMIT_MESSAGE:-Configure private platform deployment}"
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
  echo "Python is required for seed first deploy validation; install python3 or set PYTHON." >&2
  exit 1
}

PYTHON_BIN="$(resolve_python)"
PLATFORM_SEED_GIT_ROOT="${PLATFORM_SEED_GIT_ROOT:-/opt/platform/seed-git}"
PLATFORM_SEED_GIT_REPO_NAME="${PLATFORM_SEED_GIT_REPO_NAME:-platform-gitops.git}"
PLATFORM_SEED_GIT_PORT="${PLATFORM_SEED_GIT_PORT:-9418}"
PLATFORM_SEED_GIT_OWNER="${PLATFORM_SEED_GIT_OWNER:-}"
PLATFORM_SEED_GIT_WAIT_TIMEOUT="${PLATFORM_SEED_GIT_WAIT_TIMEOUT:-45}"
PLATFORM_SEED_GIT_REMOTE_NAME="${PLATFORM_SEED_GIT_REMOTE_NAME:-seed}"
PLATFORM_SEED_GIT_FORCE_WITH_LEASE="${PLATFORM_SEED_GIT_FORCE_WITH_LEASE:-true}"

export PLATFORM_PROFILE
export PLATFORM_GITOPS_PLACEHOLDER_MODE
export PLATFORM_FIRST_DEPLOY_DNS_REPAIR
export PLATFORM_AUTO_RENDER_PRIVATE_VALUES
export PLATFORM_SEED_GIT_ROOT
export PLATFORM_SEED_GIT_REPO_NAME
export PLATFORM_SEED_GIT_PORT
export PLATFORM_SEED_GIT_OWNER
export PLATFORM_SEED_GIT_WAIT_TIMEOUT
export PLATFORM_SEED_GIT_FORCE_WITH_LEASE

if [[ "${PLATFORM_AUTO_RENDER_PRIVATE_VALUES}" == "true" ]]; then
  "${PYTHON_BIN}" scripts/render_private_platform_values.py --inventory inventory/hosts.local.ini
fi

if [[ "${PLATFORM_VALIDATE_BEFORE_PUSH}" == "true" ]]; then
  "${PYTHON_BIN}" scripts/validate_project.py
  if [[ "${PLATFORM_RUN_PROFILE_CHECK}" == "true" ]]; then
    PYTHON="${PYTHON_BIN}" bash scripts/bootstrap/validate-gitops-selection.sh .
  fi
  "${PYTHON_BIN}" scripts/test_profile_checker.py
  "${PYTHON_BIN}" scripts/test_deployable_renderer.py
  "${PYTHON_BIN}" scripts/test_private_values_renderer.py
  "${PYTHON_BIN}" scripts/test_no_secrets.py
  "${PYTHON_BIN}" scripts/test_shell_syntax.py
  "${PYTHON_BIN}" scripts/test_docs_make_targets.py
  "${PYTHON_BIN}" scripts/test_ansible_playbook_references.py
  "${PYTHON_BIN}" scripts/validate_platform_contract.py
  if [[ "${PLATFORM_RUN_NO_SECRETS}" == "true" ]]; then
    PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES="${PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES}" \
      "${PYTHON_BIN}" scripts/validate_no_secrets.py
  fi
fi

git rev-parse --is-inside-work-tree >/dev/null

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

ANSIBLE_TIMEOUT="${ANSIBLE_TIMEOUT:-20}" ansible-playbook \
  -i inventory/hosts.local.ini \
  ansible/playbooks/deploy-seed-git.yml

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

export PLATFORM_REPO_URL="${seed_read_url}"
export PLATFORM_APPLY_GITOPS=true
make platform-first-deploy

cat <<EOF

Temporary seed Git is active.

Argo CD source URL:
${seed_read_url}

After Forgejo is deployed and the repository is migrated into Forgejo, remove
the temporary seed service with:

make platform-seed-git-remove
EOF
