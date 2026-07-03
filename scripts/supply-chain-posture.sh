#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${SUPPLY_CHAIN_OUTPUT_DIR:-${ROOT}/rendered/supply-chain}"
SBOM_OUTPUT="${SBOM_OUTPUT:-${OUTPUT_DIR}/platform-gitops.spdx.json}"
SCORECARD_OUTPUT="${SCORECARD_OUTPUT:-${OUTPUT_DIR}/scorecard.json}"

require_tool() {
  local tool="$1"
  local install_hint="$2"
  if ! command -v "${tool}" >/dev/null 2>&1; then
    printf 'supply-chain-posture requires %s. %s\n' "${tool}" "${install_hint}" >&2
    return 1
  fi
}

mkdir -p "${OUTPUT_DIR}"

require_tool syft "Install Syft from https://github.com/anchore/syft."

printf '== sbom ==\n'
syft "dir:${ROOT}" --exclude "${ROOT}/.git" --exclude "${ROOT}/rendered" -o "spdx-json=${SBOM_OUTPUT}"
printf 'sbom=%s\n' "${SBOM_OUTPUT}"

if command -v scorecard >/dev/null 2>&1; then
  printf '== openssf-scorecard ==\n'
  if [ -n "${SCORECARD_REPO:-}" ]; then
    scorecard --repo "${SCORECARD_REPO}" --show-details --format json >"${SCORECARD_OUTPUT}"
  else
    scorecard --local "${ROOT}" --show-details --format json >"${SCORECARD_OUTPUT}"
  fi
  printf 'scorecard=%s\n' "${SCORECARD_OUTPUT}"
else
  printf 'Skipping OpenSSF Scorecard: scorecard binary is not installed.\n' >&2
fi

if [ -n "${COSIGN_IMAGE:-}" ] && [ -n "${COSIGN_PUBLIC_KEY:-}" ]; then
  require_tool cosign "Install Cosign from https://docs.sigstore.dev/cosign/installation/."
  printf '== cosign-verify ==\n'
  cosign verify --key "${COSIGN_PUBLIC_KEY}" "${COSIGN_IMAGE}"
else
  printf 'Skipping Cosign verification: set COSIGN_IMAGE and COSIGN_PUBLIC_KEY to verify a signed image.\n'
fi

printf 'Supply-chain posture artifacts written to %s\n' "${OUTPUT_DIR}"
