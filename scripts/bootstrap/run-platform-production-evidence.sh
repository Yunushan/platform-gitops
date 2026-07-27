#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repo_root}"

env_file=""
if [[ -n "${PLATFORM_PRODUCTION_EVIDENCE_ENV_FILE:-}" ]]; then
  env_file="${PLATFORM_PRODUCTION_EVIDENCE_ENV_FILE}"
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

require_value() {
  local name="$1"
  local value="${!name:-}"
  if [[ -z "${value}" ]]; then
    printf '%s is required for production evidence.\n' "${name}" >&2
    exit 1
  fi
}

require_value PLATFORM_RELEASE_ID
require_value PLATFORM_EVIDENCE_OPERATOR
require_value PLATFORM_EVIDENCE_APPROVER
if [[ "${PLATFORM_EVIDENCE_OPERATOR,,}" == "${PLATFORM_EVIDENCE_APPROVER,,}" ]]; then
  printf '%s\n' 'PLATFORM_EVIDENCE_OPERATOR and PLATFORM_EVIDENCE_APPROVER must be different.' >&2
  exit 1
fi
if [[ ! "${PLATFORM_RELEASE_ID}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  printf '%s\n' 'PLATFORM_RELEASE_ID may contain only letters, digits, dot, underscore, and hyphen.' >&2
  exit 1
fi

profile="${PLATFORM_PROFILE:-premium-3node}"
commit="$(git rev-parse HEAD)"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
evidence_dir="${PLATFORM_PRODUCTION_EVIDENCE_DIR:-private/production-evidence}"
mkdir -p "${evidence_dir}"
log_path="${evidence_dir}/${timestamp}-${PLATFORM_RELEASE_ID}.log"
evidence_path="${evidence_dir}/${timestamp}-${PLATFORM_RELEASE_ID}.json"

set +e
{
  printf '%s\n' '== platform production evidence =='
  printf 'release=%s\nprofile=%s\ncommit=%s\n' "${PLATFORM_RELEASE_ID}" "${profile}" "${commit}"
  printf '%s\n' '== platform-production-check =='
  make platform-production-check
} 2>&1 | tee "${log_path}"
gate_status=${PIPESTATUS[0]}
set -e
if [[ "${gate_status}" -ne 0 ]]; then
  printf 'Production gate failed; no evidence record was created. Log: %s\n' "${log_path}" >&2
  exit "${gate_status}"
fi

log_hash="$(sha256sum "${log_path}" | awk '{print $1}')"
completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
PLATFORM_EVIDENCE_LOG_PATH="${log_path}" \
PLATFORM_EVIDENCE_OUTPUT_PATH="${evidence_path}" \
PLATFORM_EVIDENCE_LOG_SHA256="${log_hash}" \
PLATFORM_EVIDENCE_COMPLETED_AT="${completed_at}" \
PLATFORM_EVIDENCE_COMMIT="${commit}" \
PLATFORM_EVIDENCE_PROFILE="${profile}" \
python3 - <<'PY'
import json
import os
from pathlib import Path

document = {
    "schemaVersion": 1,
    "releaseId": os.environ["PLATFORM_RELEASE_ID"],
    "completedAt": os.environ["PLATFORM_EVIDENCE_COMPLETED_AT"],
    "operator": os.environ["PLATFORM_EVIDENCE_OPERATOR"],
    "approver": os.environ["PLATFORM_EVIDENCE_APPROVER"],
    "profile": os.environ["PLATFORM_EVIDENCE_PROFILE"],
    "commit": os.environ["PLATFORM_EVIDENCE_COMMIT"],
    "result": "passed",
    "logPath": os.environ["PLATFORM_EVIDENCE_LOG_PATH"],
    "logSha256": os.environ["PLATFORM_EVIDENCE_LOG_SHA256"],
    "gates": {
        "repository": "passed",
        "rke2": "passed",
        "platformStatus": "passed",
        "tls": "passed",
        "policyReadiness": "passed",
        "applicationHealth": "passed",
        "dataProtection": "passed",
    },
}
Path(os.environ["PLATFORM_EVIDENCE_OUTPUT_PATH"]).write_text(
    json.dumps(document, indent=2) + "\n", encoding="utf-8"
)
PY

PLATFORM_PROFILE="${profile}" \
PLATFORM_EXPECTED_COMMIT="${commit}" \
python3 scripts/verify_production_evidence.py "${evidence_path}"
printf 'Production evidence file: %s\n' "${evidence_path}"
