#!/usr/bin/env bash
set -euo pipefail

# Ensure delegated localhost Helm renders use the repository's reviewed,
# checksum-pinned tool installer when Helm is not already available.
ensure_local_helm() {
  if command -v helm >/dev/null 2>&1; then
    return 0
  fi

  case "${PLATFORM_AUTO_INSTALL_LOCAL_HELM:-true}" in
    1|true|TRUE|yes|YES|on|ON) ;;
    0|false|FALSE|no|NO|off|OFF)
      printf '%s\n' \
        'Helm is required for local vendored-chart rendering but was not found in PATH.' \
        'Install Helm or set PLATFORM_AUTO_INSTALL_LOCAL_HELM=true to use the checksum-pinned repository installer.' >&2
      return 1
      ;;
    *)
      printf 'PLATFORM_AUTO_INSTALL_LOCAL_HELM must be true or false, got: %s\n' \
        "${PLATFORM_AUTO_INSTALL_LOCAL_HELM}" >&2
      return 2
      ;;
  esac

  local tool_dir
  tool_dir="${PLATFORM_LOCAL_TOOL_CACHE_DIR:-${XDG_CACHE_HOME:-${HOME}/.cache}/platform-gitops/tools}"
  printf 'Helm was not found; installing the checksum-pinned repository version into %s\n' \
    "${tool_dir}" >&2
  scripts/bootstrap/install-ci-tools.sh "${tool_dir}" helm
  export PATH="${tool_dir}:${PATH}"

  if ! command -v helm >/dev/null 2>&1; then
    printf 'Checksum-pinned Helm installation completed but helm is still unavailable in PATH.\n' >&2
    return 1
  fi
}
