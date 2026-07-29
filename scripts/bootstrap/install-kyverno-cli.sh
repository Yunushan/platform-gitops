#!/usr/bin/env bash
set -euo pipefail

version="1.18.1"
sha256="5e6bba9ca85beec6c93e94ca7fb0972a66df3b2e67636a08bef090cd3fc6535c"
target_dir="${1:?usage: install-kyverno-cli.sh TARGET_DIRECTORY}"
archive_name="kyverno-cli_v${version}_linux_x86_64.tar.gz"
download_dir="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
archive="${download_dir}/${archive_name}"

mkdir -p "${target_dir}"
curl --fail --show-error --location --retry 3 \
  --connect-timeout 15 --max-time 180 \
  --output "${archive}" \
  "https://github.com/kyverno/kyverno/releases/download/v${version}/${archive_name}"
printf '%s  %s\n' "${sha256}" "${archive}" | sha256sum --check --strict
tar --extract --gzip --file "${archive}" --directory "${target_dir}" kyverno
chmod 0755 "${target_dir}/kyverno"
"${target_dir}/kyverno" version
