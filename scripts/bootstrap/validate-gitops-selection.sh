#!/usr/bin/env bash
set -euo pipefail

repo_root="${1:-.}"
cd "${repo_root}"

profile="${PLATFORM_PROFILE:-premium-3node}"
mode="${PLATFORM_GITOPS_PLACEHOLDER_MODE:-strict}"
repo_url="${PLATFORM_REPO_URL:-git://placeholder/platform-gitops.git}"
python_bin="${PYTHON:-}"

if [[ -z "${python_bin}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    python_bin=python3
  elif command -v python >/dev/null 2>&1; then
    python_bin=python
  else
    echo "Python is required to validate GitOps profile selection; install python3 or set PYTHON." >&2
    exit 1
  fi
fi

case "${mode}" in
  strict)
    "${python_bin}" scripts/check_gitops_profile.py --repo-root . --profile "${profile}"
    ;;
  skip-incomplete)
    rendered_file=".platform-gitops-selection-$$.yaml"
    trap 'rm -f "${rendered_file}"' EXIT
    "${python_bin}" scripts/render_deployable_gitops_apps.py \
      --repo-root . \
      --profile "${profile}" \
      --repo-url "${repo_url}" \
      --output "${rendered_file}" \
      --required-path gitops/clusters/rke2-main/projects
    ;;
  *)
    echo "Unsupported PLATFORM_GITOPS_PLACEHOLDER_MODE=${mode}; expected strict or skip-incomplete." >&2
    exit 1
    ;;
esac
