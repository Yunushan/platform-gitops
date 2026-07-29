#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${SUPPLY_CHAIN_OUTPUT_DIR:-${ROOT}/rendered/supply-chain}"
SBOM_OUTPUT="${SBOM_OUTPUT:-${OUTPUT_DIR}/platform-gitops.spdx.json}"
SCORECARD_OUTPUT="${SCORECARD_OUTPUT:-${OUTPUT_DIR}/scorecard.json}"
SIGNATURE_OUTPUT="${SIGNATURE_OUTPUT:-${OUTPUT_DIR}/cosign-verification.json}"
STRICT="${SUPPLY_CHAIN_STRICT:-false}"
MINIMUM_SCORE="${SUPPLY_CHAIN_MIN_SCORE:-7.0}"
COSIGN_IMAGES_FILE="${COSIGN_IMAGES_FILE:-}"
scorecard_generated=false
signature_generated=false

case "${STRICT}" in
  true|false) ;;
  *)
    printf 'SUPPLY_CHAIN_STRICT must be true or false, got: %s\n' "${STRICT}" >&2
    exit 2
    ;;
esac

require_tool() {
  local tool="$1"
  local install_hint="$2"
  if ! command -v "${tool}" >/dev/null 2>&1; then
    printf 'supply-chain-posture requires %s. %s\n' "${tool}" "${install_hint}" >&2
    return 1
  fi
}

mkdir -p "${OUTPUT_DIR}"

temporary_dir="$(mktemp -d)"
trap 'rm -rf "${temporary_dir}"' EXIT
verified_images_tsv="${temporary_dir}/verified-images.tsv"
: >"${verified_images_tsv}"

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
  scorecard_generated=true
  printf 'scorecard=%s\n' "${SCORECARD_OUTPUT}"
else
  if [ "${STRICT}" = "true" ]; then
    require_tool scorecard "Install OpenSSF Scorecard from https://github.com/ossf/scorecard."
  fi
  printf 'Skipping OpenSSF Scorecard: scorecard binary is not installed.\n' >&2
fi

verify_image() {
  local image="$1"
  local key="$2"

  case "${image}" in
    *@sha256:????????????????????????????????????????????????????????????????) ;;
    *)
      printf 'Cosign image must use an immutable sha256 digest: %s\n' "${image}" >&2
      return 1
      ;;
  esac
  if [ ! -f "${key}" ]; then
    printf 'Cosign public key does not exist: %s\n' "${key}" >&2
    return 1
  fi
  printf 'verifying_image=%s key=%s\n' "${image}" "$(basename "${key}")"
  cosign verify --key "${key}" "${image}" >"${temporary_dir}/cosign.json"
  printf '%s\t%s\n' "${image}" "$(basename "${key}")" >>"${verified_images_tsv}"
}

if [ -n "${COSIGN_IMAGES_FILE}" ]; then
  require_tool cosign "Install Cosign from https://docs.sigstore.dev/cosign/installation/."
  if [ ! -f "${COSIGN_IMAGES_FILE}" ]; then
    printf 'COSIGN_IMAGES_FILE does not exist: %s\n' "${COSIGN_IMAGES_FILE}" >&2
    exit 1
  fi
  printf '== cosign-verify ==\n'
  while IFS='|' read -r image key extra; do
    case "${image}" in
      ''|'#'*) continue ;;
    esac
    if [ -n "${extra}" ] || [ -z "${key}" ]; then
      printf 'Invalid COSIGN_IMAGES_FILE row; expected IMAGE@sha256:DIGEST|PUBLIC_KEY: %s\n' "${image}" >&2
      exit 1
    fi
    case "${key}" in
      /*) ;;
      *) key="${ROOT}/${key}" ;;
    esac
    verify_image "${image}" "${key}"
  done <"${COSIGN_IMAGES_FILE}"
elif [ -n "${COSIGN_IMAGE:-}" ] && [ -n "${COSIGN_PUBLIC_KEY:-}" ]; then
  require_tool cosign "Install Cosign from https://docs.sigstore.dev/cosign/installation/."
  printf '== cosign-verify ==\n'
  verify_image "${COSIGN_IMAGE}" "${COSIGN_PUBLIC_KEY}"
else
  if [ "${STRICT}" = "true" ]; then
    printf 'Strict supply-chain evidence requires COSIGN_IMAGES_FILE with at least one digest-pinned image.\n' >&2
    exit 1
  fi
  printf 'Skipping Cosign verification: set COSIGN_IMAGES_FILE or COSIGN_IMAGE and COSIGN_PUBLIC_KEY.\n'
fi

if [ -s "${verified_images_tsv}" ]; then
  python3 - "${verified_images_tsv}" "${SIGNATURE_OUTPUT}" <<'PY'
import json
from pathlib import Path
import sys

rows = []
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    image, key = line.split("\t", 1)
    rows.append({"image": image, "key": key, "verified": True})
Path(sys.argv[2]).write_text(
    json.dumps({"schemaVersion": 1, "verifiedImages": rows}, indent=2) + "\n",
    encoding="utf-8",
)
PY
  signature_generated=true
  printf 'cosign_report=%s\n' "${SIGNATURE_OUTPUT}"
elif [ "${STRICT}" = "true" ]; then
  printf 'Strict supply-chain evidence did not verify any images.\n' >&2
  exit 1
fi

evidence_args=(--sbom "${SBOM_OUTPUT}" --minimum-score "${MINIMUM_SCORE}")
if [ "${scorecard_generated}" = "true" ]; then
  evidence_args+=(--scorecard "${SCORECARD_OUTPUT}")
fi
if [ "${signature_generated}" = "true" ]; then
  evidence_args+=(--signature-report "${SIGNATURE_OUTPUT}")
fi
if [ "${STRICT}" = "true" ]; then
  evidence_args+=(--strict)
fi

printf '== evidence-validation ==\n'
python3 "${ROOT}/scripts/verify_supply_chain_evidence.py" "${evidence_args[@]}"

printf 'Supply-chain posture artifacts written to %s\n' "${OUTPUT_DIR}"
