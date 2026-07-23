#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repo_root}"

env_file=""
if [[ -n "${PLATFORM_APP_HEALTH_ENV_FILE:-}" ]]; then
  env_file="${PLATFORM_APP_HEALTH_ENV_FILE}"
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

health_mode="${PLATFORM_APP_HEALTH_MODE:-auto}"
if [[ "${health_mode}" == "auto" ]]; then
  case "${env_file##*/}" in
    seed-git.env|first-deploy.env)
      health_mode=bootstrap
      ;;
    *)
      health_mode=production
      ;;
  esac
fi

case "${health_mode}" in
  bootstrap)
    export PLATFORM_APP_HEALTH_MODE=bootstrap
    if [[ ! -v PLATFORM_APP_HEALTH_FORBID_TEMPORARY_REPO ]]; then
      export PLATFORM_APP_HEALTH_FORBID_TEMPORARY_REPO=false
    fi
    if [[ ! -v PLATFORM_APP_HEALTH_OPENBAO_READY ]]; then
      export PLATFORM_APP_HEALTH_OPENBAO_READY=false
    fi
    ;;
  production)
    export PLATFORM_APP_HEALTH_MODE=production
    ;;
  *)
    printf 'Unsupported PLATFORM_APP_HEALTH_MODE=%s; use auto, bootstrap, or production.\n' "${health_mode}" >&2
    exit 2
    ;;
esac

rendered_apps=()
rendered_namespaces=()
if [[ -n "${env_file}" && -f private/platform-apps.rendered.yaml ]]; then
  mapfile -t rendered_profile < <(python3 - private/platform-apps.rendered.yaml <<'PY'
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()
apps = []
namespaces = ["argocd"]
for document in re.split(r"(?m)^---\s*$", text):
    if not re.search(r"(?m)^kind:\s*Application\s*$", document):
        continue
    metadata = document.split("\nspec:", 1)[0]
    name = re.search(r"(?m)^  name:\s*([^\s#]+)", metadata)
    namespace = re.search(
        r"(?ms)^  destination:\s*$.*?^    namespace:\s*([^\s#]+)",
        document,
    )
    if name and name.group(1) not in apps:
        apps.append(name.group(1))
    if namespace and namespace.group(1) not in namespaces:
        namespaces.append(namespace.group(1))

print(" ".join(apps))
print(" ".join(namespaces))
PY
  )
  read -r -a rendered_apps <<<"${rendered_profile[0]:-}"
  read -r -a rendered_namespaces <<<"${rendered_profile[1]:-}"
fi

if ((${#rendered_apps[@]} > 0)); then
  if [[ ! -v PLATFORM_APP_HEALTH_REQUIRED_APPS ]]; then
    export PLATFORM_APP_HEALTH_REQUIRED_APPS="${rendered_apps[*]}"
  fi
  if [[ ! -v PLATFORM_APP_HEALTH_NAMESPACES && ${#rendered_namespaces[@]} -gt 0 ]]; then
    export PLATFORM_APP_HEALTH_NAMESPACES="${rendered_namespaces[*]}"
  fi
  if [[ " ${rendered_apps[*]} " != *" step-ca "* && ! -v PLATFORM_APP_HEALTH_STEP_CA_API ]]; then
    export PLATFORM_APP_HEALTH_STEP_CA_API=false
  fi
fi

export ANSIBLE_TIMEOUT="${ANSIBLE_TIMEOUT:-20}"
exec ansible-playbook \
  -i inventory/hosts.local.ini \
  ansible/playbooks/verify-platform-app-health.yml
