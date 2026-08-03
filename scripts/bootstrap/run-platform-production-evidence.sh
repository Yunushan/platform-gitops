#!/usr/bin/env bash
set -euo pipefail
umask 077

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
require_value PLATFORM_PRODUCTION_APPROVAL_APPROVER
require_value PLATFORM_OPENBAO_CEREMONY_EVIDENCE_FILE
require_value PLATFORM_RESTORE_EVIDENCE_FILE
require_value PLATFORM_FORGEJO_RECOVERY_EVIDENCE_FILE
if [[ "${PLATFORM_EVIDENCE_OPERATOR,,}" == "${PLATFORM_EVIDENCE_APPROVER,,}" ]]; then
  printf '%s\n' 'PLATFORM_EVIDENCE_OPERATOR and PLATFORM_EVIDENCE_APPROVER must be different.' >&2
  exit 1
fi
if [[ "${PLATFORM_EVIDENCE_APPROVER,,}" != "${PLATFORM_PRODUCTION_APPROVAL_APPROVER,,}" ]]; then
  printf '%s\n' \
    'PLATFORM_EVIDENCE_APPROVER must match the configured PLATFORM_PRODUCTION_APPROVAL_APPROVER.' >&2
  exit 1
fi
if [[ ! "${PLATFORM_RELEASE_ID}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  printf '%s\n' 'PLATFORM_RELEASE_ID may contain only letters, digits, dot, underscore, and hyphen.' >&2
  exit 1
fi

profile="${PLATFORM_PROFILE:-premium-3node}"
commit="$(git rev-parse HEAD)"
if [[ ! -f "${PLATFORM_OPENBAO_CEREMONY_EVIDENCE_FILE}" ]]; then
  printf 'OpenBao ceremony evidence file does not exist: %s\n' \
    "${PLATFORM_OPENBAO_CEREMONY_EVIDENCE_FILE}" >&2
  exit 1
fi
if [[ ! -f "${PLATFORM_RESTORE_EVIDENCE_FILE}" ]]; then
  printf 'Restore evidence file does not exist: %s\n' \
    "${PLATFORM_RESTORE_EVIDENCE_FILE}" >&2
  exit 1
fi
if [[ ! -f "${PLATFORM_FORGEJO_RECOVERY_EVIDENCE_FILE}" ]]; then
  printf 'Forgejo recovery evidence file does not exist: %s\n' \
    "${PLATFORM_FORGEJO_RECOVERY_EVIDENCE_FILE}" >&2
  exit 1
fi
openbao_configuration_sha256="$(
  python3 scripts/verify_openbao_ceremony_evidence.py \
    --print-configuration-sha256 \
    --expected-profile "${profile}"
)"
PLATFORM_PROFILE="${profile}" \
PLATFORM_OPENBAO_SOURCE_COMMIT="${commit}" \
PLATFORM_OPENBAO_CONFIGURATION_SHA256="${openbao_configuration_sha256}" \
python3 scripts/verify_openbao_ceremony_evidence.py \
  "${PLATFORM_OPENBAO_CEREMONY_EVIDENCE_FILE}"

branch="$(git symbolic-ref --quiet --short HEAD || true)"
if [[ -z "${branch}" ]]; then
  printf '%s\n' \
    'Production evidence requires a named branch; detached HEAD checkouts are rejected.' >&2
  exit 1
fi

worktree_status="$(git status --porcelain=v1 --untracked-files=all)"
if [[ -n "${worktree_status}" ]]; then
  printf '%s\n' \
    'Production evidence requires a clean worktree. Commit, intentionally ignore, or remove every pending path first:' >&2
  printf '%s\n' "${worktree_status}" >&2
  exit 1
fi

expected_ref="${PLATFORM_PRODUCTION_EVIDENCE_EXPECTED_REF:-}"
if [[ -z "${expected_ref}" ]]; then
  expected_ref="$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)"
fi
if [[ -z "${expected_ref}" ]]; then
  printf '%s\n' \
    'PLATFORM_PRODUCTION_EVIDENCE_EXPECTED_REF is required when the current branch has no upstream.' \
    'Set it to a fetched remote-tracking ref such as seed/main.' >&2
  exit 1
fi
if [[ ! "${expected_ref}" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+$ ]] ||
  [[ "${expected_ref}" == *..* ]] || [[ "${expected_ref}" == */ ]] ||
  [[ "${expected_ref}" == *//* ]]; then
  printf 'Invalid production evidence remote-tracking ref: %s\n' "${expected_ref}" >&2
  exit 1
fi

remote_name="${expected_ref%%/*}"
full_expected_ref="refs/remotes/${expected_ref}"
if ! git show-ref --verify --quiet "${full_expected_ref}"; then
  printf 'Production evidence remote-tracking ref is missing; fetch it first: %s\n' \
    "${expected_ref}" >&2
  exit 1
fi
expected_commit="$(git rev-parse "${full_expected_ref}^{commit}")"
if [[ "${commit}" != "${expected_commit}" ]]; then
  printf 'Production evidence requires HEAD to exactly match %s. HEAD=%s expected=%s\n' \
    "${expected_ref}" "${commit}" "${expected_commit}" >&2
  exit 1
fi

remote_url="$(git remote get-url "${remote_name}")"
remote_url_sha256="$(printf '%s' "${remote_url}" | sha256sum | awk '{print $1}')"
tree="$(git rev-parse 'HEAD^{tree}')"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
evidence_dir="${PLATFORM_PRODUCTION_EVIDENCE_DIR:-private/production-evidence}"
mkdir -p "${evidence_dir}"
log_path="${evidence_dir}/${timestamp}-${PLATFORM_RELEASE_ID}.log"
evidence_path="${evidence_dir}/${timestamp}-${PLATFORM_RELEASE_ID}.json"

set +e
{
  printf '%s\n' '== platform production evidence =='
  printf 'release=%s\nprofile=%s\ncommit=%s\n' "${PLATFORM_RELEASE_ID}" "${profile}" "${commit}"
  printf 'source_branch=%s\nsource_expected_ref=%s\nsource_tree=%s\nsource_remote=%s\nsource_remote_url_sha256=%s\n' \
    "${branch}" "${expected_ref}" "${tree}" "${remote_name}" "${remote_url_sha256}"
  printf '%s\n' '== platform-production-check =='
  make platform-production-check
} 2>&1 | tee "${log_path}"
gate_status=${PIPESTATUS[0]}
set -e
if [[ "${gate_status}" -ne 0 ]]; then
  printf 'Production gate failed; no evidence record was created. Log: %s\n' "${log_path}" >&2
  exit "${gate_status}"
fi

mapfile -t openbao_cluster_ids < <(
  grep -Eo 'cluster_id_sha256=[a-f0-9]{64}' "${log_path}" |
    cut -d= -f2 |
    sort -u
)
if [[ "${#openbao_cluster_ids[@]}" -ne 1 ]]; then
  printf '%s\n' \
    'Production log must contain exactly one sanitized OpenBao cluster identity.' >&2
  exit 1
fi
openbao_cluster_id_sha256="${openbao_cluster_ids[0]}"
PLATFORM_PROFILE="${profile}" \
PLATFORM_OPENBAO_SOURCE_COMMIT="${commit}" \
PLATFORM_OPENBAO_CONFIGURATION_SHA256="${openbao_configuration_sha256}" \
PLATFORM_OPENBAO_CLUSTER_ID_SHA256="${openbao_cluster_id_sha256}" \
python3 scripts/verify_openbao_ceremony_evidence.py \
  "${PLATFORM_OPENBAO_CEREMONY_EVIDENCE_FILE}"

image_inventory_source="${PLATFORM_IMAGE_INVENTORY_EVIDENCE_OUTPUT:-rendered/supply-chain/image-inventory-evidence.json}"
if [[ ! -f "${image_inventory_source}" ]]; then
  printf 'Production gate did not retain required image inventory evidence: %s\n' \
    "${image_inventory_source}" >&2
  exit 1
fi
openbao_ceremony_path="${evidence_dir}/${timestamp}-${PLATFORM_RELEASE_ID}-openbao-ceremony.json"
image_inventory_path="${evidence_dir}/${timestamp}-${PLATFORM_RELEASE_ID}-image-inventory.json"
restore_evidence_path="${evidence_dir}/${timestamp}-${PLATFORM_RELEASE_ID}-restore-evidence.json"
forgejo_recovery_path="${evidence_dir}/${timestamp}-${PLATFORM_RELEASE_ID}-forgejo-recovery.json"

python3 - \
  "${PLATFORM_OPENBAO_CEREMONY_EVIDENCE_FILE}" "${openbao_ceremony_path}" \
  "${image_inventory_source}" "${image_inventory_path}" \
  "${PLATFORM_RESTORE_EVIDENCE_FILE}" "${restore_evidence_path}" \
  "${PLATFORM_FORGEJO_RECOVERY_EVIDENCE_FILE}" "${forgejo_recovery_path}" <<'PY'
import sys
from pathlib import Path

from scripts.atomic_file import atomic_write_text
from scripts.bounded_file import read_bounded_text

arguments = sys.argv[1:]
if len(arguments) % 2:
    raise SystemExit("evidence retention requires source/destination pairs")
for index in range(0, len(arguments), 2):
    source = Path(arguments[index])
    destination = Path(arguments[index + 1])
    atomic_write_text(destination, read_bounded_text(source))
PY

openbao_ceremony_hash="$(sha256sum "${openbao_ceremony_path}" | awk '{print $1}')"
image_inventory_hash="$(sha256sum "${image_inventory_path}" | awk '{print $1}')"
restore_evidence_hash="$(sha256sum "${restore_evidence_path}" | awk '{print $1}')"
forgejo_recovery_hash="$(sha256sum "${forgejo_recovery_path}" | awk '{print $1}')"
log_hash="$(sha256sum "${log_path}" | awk '{print $1}')"
completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
PLATFORM_EVIDENCE_LOG_PATH="${log_path}" \
PLATFORM_EVIDENCE_OUTPUT_PATH="${evidence_path}" \
PLATFORM_EVIDENCE_LOG_SHA256="${log_hash}" \
PLATFORM_EVIDENCE_IMAGE_INVENTORY_PATH="${image_inventory_path}" \
PLATFORM_EVIDENCE_IMAGE_INVENTORY_SHA256="${image_inventory_hash}" \
PLATFORM_EVIDENCE_OPENBAO_CEREMONY_PATH="${openbao_ceremony_path}" \
PLATFORM_EVIDENCE_OPENBAO_CEREMONY_SHA256="${openbao_ceremony_hash}" \
PLATFORM_EVIDENCE_RESTORE_PATH="${restore_evidence_path}" \
PLATFORM_EVIDENCE_RESTORE_SHA256="${restore_evidence_hash}" \
PLATFORM_EVIDENCE_FORGEJO_RECOVERY_PATH="${forgejo_recovery_path}" \
PLATFORM_EVIDENCE_FORGEJO_RECOVERY_SHA256="${forgejo_recovery_hash}" \
PLATFORM_EVIDENCE_COMPLETED_AT="${completed_at}" \
PLATFORM_EVIDENCE_COMMIT="${commit}" \
PLATFORM_EVIDENCE_PROFILE="${profile}" \
PLATFORM_EVIDENCE_SOURCE_BRANCH="${branch}" \
PLATFORM_EVIDENCE_SOURCE_EXPECTED_REF="${expected_ref}" \
PLATFORM_EVIDENCE_SOURCE_REMOTE="${remote_name}" \
PLATFORM_EVIDENCE_SOURCE_REMOTE_URL_SHA256="${remote_url_sha256}" \
PLATFORM_EVIDENCE_SOURCE_TREE="${tree}" \
python3 - <<'PY'
import json
import os
from pathlib import Path

from scripts.atomic_file import atomic_write_text

document = {
    "schemaVersion": 7,
    "releaseId": os.environ["PLATFORM_RELEASE_ID"],
    "completedAt": os.environ["PLATFORM_EVIDENCE_COMPLETED_AT"],
    "operator": os.environ["PLATFORM_EVIDENCE_OPERATOR"],
    "approver": os.environ["PLATFORM_EVIDENCE_APPROVER"],
    "profile": os.environ["PLATFORM_EVIDENCE_PROFILE"],
    "commit": os.environ["PLATFORM_EVIDENCE_COMMIT"],
    "result": "passed",
    "logPath": os.environ["PLATFORM_EVIDENCE_LOG_PATH"],
    "logSha256": os.environ["PLATFORM_EVIDENCE_LOG_SHA256"],
    "imageInventory": {
        "path": os.environ["PLATFORM_EVIDENCE_IMAGE_INVENTORY_PATH"],
        "sha256": os.environ["PLATFORM_EVIDENCE_IMAGE_INVENTORY_SHA256"],
    },
    "openbaoCeremony": {
        "path": os.environ["PLATFORM_EVIDENCE_OPENBAO_CEREMONY_PATH"],
        "sha256": os.environ["PLATFORM_EVIDENCE_OPENBAO_CEREMONY_SHA256"],
    },
    "restoreEvidence": {
        "path": os.environ["PLATFORM_EVIDENCE_RESTORE_PATH"],
        "sha256": os.environ["PLATFORM_EVIDENCE_RESTORE_SHA256"],
    },
    "forgejoRecovery": {
        "path": os.environ["PLATFORM_EVIDENCE_FORGEJO_RECOVERY_PATH"],
        "sha256": os.environ["PLATFORM_EVIDENCE_FORGEJO_RECOVERY_SHA256"],
    },
    "source": {
        "branch": os.environ["PLATFORM_EVIDENCE_SOURCE_BRANCH"],
        "expectedRef": os.environ["PLATFORM_EVIDENCE_SOURCE_EXPECTED_REF"],
        "remote": os.environ["PLATFORM_EVIDENCE_SOURCE_REMOTE"],
        "remoteUrlSha256": os.environ["PLATFORM_EVIDENCE_SOURCE_REMOTE_URL_SHA256"],
        "tree": os.environ["PLATFORM_EVIDENCE_SOURCE_TREE"],
        "clean": True,
    },
    "gates": {
        "sourceProvenance": "passed",
        "repository": "passed",
        "profile": "passed",
        "renderedSchema": "passed",
        "supplyChain": "passed",
        "runtimeImageInventory": "passed",
        "rke2": "passed",
        "platformStatus": "passed",
        "tls": "passed",
        "policyReadiness": "passed",
        "networkIsolation": "passed",
        "internalTls": "passed",
        "openbaoReadiness": "passed",
        "openbaoCeremony": "passed",
        "observability": "passed",
        "capacity": "passed",
        "applicationHealth": "passed",
        "dataProtection": "passed",
    },
}
atomic_write_text(
    Path(os.environ["PLATFORM_EVIDENCE_OUTPUT_PATH"]),
    json.dumps(document, indent=2) + "\n",
)
PY

PLATFORM_PROFILE="${profile}" \
PLATFORM_EXPECTED_COMMIT="${commit}" \
python3 scripts/verify_production_evidence.py "${evidence_path}"
printf 'Production evidence file: %s\n' "${evidence_path}"
