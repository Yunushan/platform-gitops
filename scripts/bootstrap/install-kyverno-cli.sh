#!/usr/bin/env bash
set -euo pipefail
umask 077

# renovate: datasource=github-releases depName=kyverno/kyverno extractVersion=^v(?<version>.*)$
readonly kyverno_version="1.18.1"
readonly kyverno_sha256="5e6bba9ca85beec6c93e94ca7fb0972a66df3b2e67636a08bef090cd3fc6535c"
readonly max_archive_bytes=$((64 * 1024 * 1024))
readonly download_timeout_seconds=180

target_dir="${1:-}"
if [ -z "${target_dir}" ] || [ "$#" -ne 1 ]; then
  printf 'usage: install-kyverno-cli.sh TARGET_DIRECTORY\n' >&2
  exit 2
fi

if ! command -v uname >/dev/null 2>&1; then
  printf 'Required Kyverno installer command is unavailable: uname\n' >&2
  exit 1
fi
case "$(uname -s):$(uname -m)" in
  Linux:x86_64|Linux:amd64) ;;
  *)
    printf 'Checksum-pinned Kyverno CLI supports only Linux amd64 runners.\n' >&2
    exit 1
    ;;
esac

for required_command in chmod curl install mkdir mktemp mv rm sha256sum tar timeout wc; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    printf 'Required Kyverno installer command is unavailable: %s\n' \
      "${required_command}" >&2
    exit 1
  fi
done

archive_name="kyverno-cli_v${kyverno_version}_linux_x86_64.tar.gz"
download_dir="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"

target_dir="${target_dir%/}"
download_dir="${download_dir%/}"
if [ -z "${target_dir}" ] || [ "${target_dir}" = "/" ]; then
  printf 'Kyverno CLI target must be a dedicated non-root directory.\n' >&2
  exit 1
fi
if [ -z "${download_dir}" ]; then
  download_dir="/"
fi

mkdir -p -- "${target_dir}"
if [ ! -d "${target_dir}" ] || [ -L "${target_dir}" ]; then
  printf 'Kyverno CLI target must be a regular directory: %s\n' "${target_dir}" >&2
  exit 1
fi
if [ ! -d "${download_dir}" ] || [ -L "${download_dir}" ]; then
  printf 'Kyverno CLI temporary root must be a regular directory: %s\n' "${download_dir}" >&2
  exit 1
fi
chmod 0700 -- "${target_dir}"

work_dir="$(mktemp -d "${download_dir}/platform-kyverno-cli.XXXXXX")"
archive="${work_dir}/${archive_name}"
staging="${work_dir}/extract"
target_tmp=""

cleanup() {
  if [ -n "${target_tmp}" ]; then
    rm -f -- "${target_tmp}"
  fi
  rm -rf -- "${work_dir}"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir --mode=0700 -- "${staging}"
curl --fail --show-error --silent --location --retry 3 \
  --retry-all-errors \
  --proto '=https' --proto-redir '=https' --tlsv1.2 \
  --max-redirs 3 \
  --connect-timeout 15 --max-time "${download_timeout_seconds}" \
  --max-filesize "${max_archive_bytes}" \
  --output "${archive}" \
  "https://github.com/kyverno/kyverno/releases/download/v${kyverno_version}/${archive_name}"
if [ ! -f "${archive}" ] || [ -L "${archive}" ]; then
  printf 'Kyverno CLI download is not a regular file.\n' >&2
  exit 1
fi
archive_size="$(wc -c < "${archive}" | tr -d '[:space:]')"
case "${archive_size}" in
  ''|*[!0-9]*)
    printf 'Kyverno CLI archive size is invalid.\n' >&2
    exit 1
    ;;
esac
if (( archive_size <= 0 || archive_size > max_archive_bytes )); then
  printf 'Kyverno CLI archive size is outside the accepted range: %s bytes.\n' \
    "${archive_size}" >&2
  exit 1
fi
printf '%s  %s\n' "${kyverno_sha256}" "${archive}" | sha256sum --check --strict
tar --extract --gzip --file "${archive}" --directory "${staging}" \
  --no-same-owner --no-same-permissions -- kyverno
if [ ! -f "${staging}/kyverno" ] || [ -L "${staging}/kyverno" ]; then
  printf 'Kyverno CLI archive did not contain a regular kyverno binary.\n' >&2
  exit 1
fi

target_tmp="$(mktemp "${target_dir}/.kyverno.XXXXXX")"
install --mode=0755 -- "${staging}/kyverno" "${target_tmp}"
version_output="$(timeout 15s "${target_tmp}" version 2>&1)"
case "${version_output}" in
  *"${kyverno_version}"*) ;;
  *)
    printf 'Installed Kyverno CLI did not report expected version %s.\n' \
      "${kyverno_version}" >&2
    exit 1
    ;;
esac
mv -f -- "${target_tmp}" "${target_dir}/kyverno"
target_tmp=""
printf 'installed=kyverno version=%s sha256=%s\n' \
  "${kyverno_version}" "${kyverno_sha256}"
