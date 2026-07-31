#!/usr/bin/env bash
set -euo pipefail

umask 077

readonly max_archive_bytes=$((64 * 1024 * 1024))
readonly download_timeout_seconds=180

# renovate: datasource=github-releases depName=rhysd/actionlint extractVersion=^v(?<version>.*)$
readonly actionlint_version="1.7.12"
readonly actionlint_sha256="8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"

# renovate: datasource=github-releases depName=kubernetes-sigs/kustomize extractVersion=^kustomize/v(?<version>.*)$
readonly kustomize_version="5.8.1"
readonly kustomize_sha256="029a7f0f4e1932c52a0476cf02a0fd855c0bb85694b82c338fc648dcb53a819d"

# renovate: datasource=github-releases depName=helm/helm extractVersion=^v(?<version>.*)$
readonly helm_version="3.21.0"
readonly helm_sha256="0093eb572e3d2380f094df162ddb525e219249de88957afe24cfbb19632acd36"

# renovate: datasource=github-releases depName=yannh/kubeconform extractVersion=^v(?<version>.*)$
readonly kubeconform_version="0.7.0"
readonly kubeconform_sha256="c31518ddd122663b3f3aa874cfe8178cb0988de944f29c74a0b9260920d115d3"

target_dir="${1:-}"
if [ -z "${target_dir}" ]; then
  printf 'usage: install-ci-tools.sh TARGET_DIRECTORY TOOL [TOOL ...]\n' >&2
  exit 2
fi
shift
if [ "$#" -eq 0 ]; then
  printf 'At least one CI tool must be selected.\n' >&2
  exit 2
fi

case "$(uname -s):$(uname -m)" in
  Linux:x86_64|Linux:amd64) ;;
  *)
    printf 'Checksum-pinned CI tools support only Linux amd64 runners.\n' >&2
    exit 1
    ;;
esac

for required_command in curl install mktemp mv sha256sum tar timeout wc; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    printf 'Required installer command is unavailable: %s\n' "${required_command}" >&2
    exit 1
  fi
done

target_dir="${target_dir%/}"
if [ -z "${target_dir}" ] || [ "${target_dir}" = "/" ]; then
  printf 'CI tool target must be a dedicated non-root directory.\n' >&2
  exit 1
fi
mkdir -p -- "${target_dir}"
if [ ! -d "${target_dir}" ] || [ -L "${target_dir}" ]; then
  printf 'CI tool target must be a regular directory: %s\n' "${target_dir}" >&2
  exit 1
fi
chmod 0700 -- "${target_dir}"

temp_root="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
temp_root="${temp_root%/}"
if [ -z "${temp_root}" ]; then
  temp_root="/"
fi
if [ ! -d "${temp_root}" ] || [ -L "${temp_root}" ]; then
  printf 'CI tool temporary root must be a regular directory: %s\n' "${temp_root}" >&2
  exit 1
fi

work_dir="$(mktemp -d "${temp_root}/platform-ci-tools.XXXXXX")"
pending_destination=""
cleanup() {
  if [ -n "${pending_destination}" ]; then
    rm -f -- "${pending_destination}"
  fi
  rm -rf -- "${work_dir}"
}
trap cleanup EXIT HUP INT TERM

tool_version=""
tool_sha256=""
archive_name=""
archive_url=""
archive_member=""
executable_name=""
version_marker=""
declare -a version_arguments=()

configure_tool() {
  case "$1" in
    actionlint)
      tool_version="${actionlint_version}"
      tool_sha256="${actionlint_sha256}"
      archive_name="actionlint_${tool_version}_linux_amd64.tar.gz"
      archive_url="https://github.com/rhysd/actionlint/releases/download/v${tool_version}/${archive_name}"
      archive_member="actionlint"
      executable_name="actionlint"
      version_marker="${tool_version}"
      version_arguments=(-version)
      ;;
    kustomize)
      tool_version="${kustomize_version}"
      tool_sha256="${kustomize_sha256}"
      archive_name="kustomize_v${tool_version}_linux_amd64.tar.gz"
      archive_url="https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize/v${tool_version}/${archive_name}"
      archive_member="kustomize"
      executable_name="kustomize"
      version_marker="v${tool_version}"
      version_arguments=(version)
      ;;
    helm)
      tool_version="${helm_version}"
      tool_sha256="${helm_sha256}"
      archive_name="helm-v${tool_version}-linux-amd64.tar.gz"
      archive_url="https://get.helm.sh/${archive_name}"
      archive_member="linux-amd64/helm"
      executable_name="helm"
      version_marker="v${tool_version}"
      version_arguments=(version --short)
      ;;
    kubeconform)
      tool_version="${kubeconform_version}"
      tool_sha256="${kubeconform_sha256}"
      archive_name="kubeconform-linux-amd64.tar.gz"
      archive_url="https://github.com/yannh/kubeconform/releases/download/v${tool_version}/${archive_name}"
      archive_member="kubeconform"
      executable_name="kubeconform"
      version_marker="v${tool_version}"
      version_arguments=(-v)
      ;;
    *)
      printf 'Unsupported checksum-pinned CI tool: %s\n' "$1" >&2
      return 1
      ;;
  esac
}

declare -A selected_tools=()
for tool in "$@"; do
  configure_tool "${tool}"
  if [ -n "${selected_tools[$tool]:-}" ]; then
    printf 'CI tool was selected more than once: %s\n' "${tool}" >&2
    exit 1
  fi
  selected_tools["${tool}"]=1
done

for tool in "$@"; do
  configure_tool "${tool}"

  archive="${work_dir}/${tool}-${archive_name}"
  staging="${work_dir}/extract-${tool}"
  mkdir --mode=0700 -- "${staging}"

  curl --fail --show-error --silent --location --retry 3 \
    --retry-all-errors \
    --proto '=https' --proto-redir '=https' --tlsv1.2 \
    --max-redirs 3 \
    --connect-timeout 20 --max-time "${download_timeout_seconds}" \
    --max-filesize "${max_archive_bytes}" \
    --output "${archive}" \
    "${archive_url}"

  if [ ! -f "${archive}" ] || [ -L "${archive}" ]; then
    printf 'Downloaded CI tool archive is not a regular file: %s\n' "${tool}" >&2
    exit 1
  fi
  archive_bytes="$(wc -c <"${archive}")"
  if [ "${archive_bytes}" -le 0 ] || [ "${archive_bytes}" -gt "${max_archive_bytes}" ]; then
    printf 'Downloaded CI tool archive has an unsafe size: %s\n' "${tool}" >&2
    exit 1
  fi
  printf '%s  %s\n' "${tool_sha256}" "${archive}" |
    sha256sum --check --strict

  tar --extract --gzip --file "${archive}" \
    --directory "${staging}" \
    --no-same-owner --no-same-permissions \
    "${archive_member}"

  candidate="${staging}/${archive_member}"
  if [ ! -f "${candidate}" ] || [ -L "${candidate}" ]; then
    printf 'Expected CI tool executable is missing after extraction: %s\n' "${tool}" >&2
    exit 1
  fi

  destination="${target_dir}/${executable_name}"
  pending_destination="$(mktemp "${target_dir}/.${executable_name}.XXXXXX")"
  install --mode=0755 -- "${candidate}" "${pending_destination}"
  version_output="$(timeout 15s "${pending_destination}" "${version_arguments[@]}" 2>&1)"
  case "${version_output}" in
    *"${version_marker}"*) ;;
    *)
      printf 'Installed CI tool did not report the expected version: %s %s\n' \
        "${tool}" "${version_marker}" >&2
      exit 1
      ;;
  esac
  mv -f -- "${pending_destination}" "${destination}"
  pending_destination=""
  printf 'installed=%s version=%s sha256=%s\n' \
    "${tool}" "${tool_version}" "${tool_sha256}"
done
