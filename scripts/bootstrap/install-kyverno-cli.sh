#!/usr/bin/env bash
set -euo pipefail
umask 077

version="1.18.1"
sha256="5e6bba9ca85beec6c93e94ca7fb0972a66df3b2e67636a08bef090cd3fc6535c"
max_archive_bytes=$((64 * 1024 * 1024))
target_dir="${1:?usage: install-kyverno-cli.sh TARGET_DIRECTORY}"
archive_name="kyverno-cli_v${version}_linux_x86_64.tar.gz"
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

mkdir -p -- "${target_dir}" "${download_dir}"
if [ ! -d "${target_dir}" ] || [ -L "${target_dir}" ]; then
  printf 'Kyverno CLI target must be a regular directory: %s\n' "${target_dir}" >&2
  exit 1
fi
if [ ! -d "${download_dir}" ] || [ -L "${download_dir}" ]; then
  printf 'Kyverno CLI temporary root must be a regular directory: %s\n' "${download_dir}" >&2
  exit 1
fi

work_dir="$(mktemp -d "${download_dir}/platform-kyverno-cli.XXXXXX")"
archive="${work_dir}/${archive_name}"
staging="${work_dir}/extract"
target_tmp=""

cleanup() {
  status=$?
  trap - EXIT
  if [ -n "${target_tmp}" ]; then
    rm -f -- "${target_tmp}" || true
  fi
  rm -f -- "${staging}/kyverno" "${archive}" || true
  rmdir -- "${staging}" 2>/dev/null || true
  rmdir -- "${work_dir}" 2>/dev/null || true
  exit "${status}"
}
trap cleanup EXIT

mkdir --mode=0700 -- "${staging}"
curl --fail --show-error --location --retry 3 \
  --retry-all-errors \
  --proto '=https' --proto-redir '=https' \
  --connect-timeout 15 --max-time 180 \
  --max-filesize "${max_archive_bytes}" \
  --output "${archive}" \
  "https://github.com/kyverno/kyverno/releases/download/v${version}/${archive_name}"
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
printf '%s  %s\n' "${sha256}" "${archive}" | sha256sum --check --strict
tar --extract --gzip --file "${archive}" --directory "${staging}" \
  --no-same-owner --no-same-permissions -- kyverno
if [ ! -f "${staging}/kyverno" ] || [ -L "${staging}/kyverno" ]; then
  printf 'Kyverno CLI archive did not contain a regular kyverno binary.\n' >&2
  exit 1
fi

target_tmp="$(mktemp "${target_dir}/.kyverno.XXXXXX")"
install --mode=0755 -- "${staging}/kyverno" "${target_tmp}"
mv -f -- "${target_tmp}" "${target_dir}/kyverno"
target_tmp=""
"${target_dir}/kyverno" version
