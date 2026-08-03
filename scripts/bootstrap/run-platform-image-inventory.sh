#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repo_root}"

env_file=""
if [[ -n "${PLATFORM_IMAGE_INVENTORY_ENV_FILE:-}" ]]; then
  env_file="${PLATFORM_IMAGE_INVENTORY_ENV_FILE}"
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

profile="${PLATFORM_PROFILE:-premium-3node}"
expected_registry="${PLATFORM_IMAGE_REGISTRY:-${PLATFORM_HARBOR_HOST:-}}"
rendered_summary="${PLATFORM_IMAGE_INVENTORY_RENDERED_SUMMARY:-rendered/schema-validation/summary.json}"
live_inventory="${PLATFORM_IMAGE_INVENTORY_LIVE_OUTPUT:-rendered/supply-chain/live-image-inventory.json}"
signature_report="${PLATFORM_IMAGE_INVENTORY_SIGNATURE_REPORT:-rendered/supply-chain/cosign-verification.json}"
exceptions="${PLATFORM_IMAGE_INVENTORY_EXCEPTIONS_FILE:-}"
evidence="${PLATFORM_IMAGE_INVENTORY_EVIDENCE_OUTPUT:-rendered/supply-chain/image-inventory-evidence.json}"

if [[ -z "${expected_registry}" ]]; then
  printf '%s\n' \
    'PLATFORM_IMAGE_REGISTRY or PLATFORM_HARBOR_HOST is required for image admission-scope reconciliation.' >&2
  exit 1
fi
for required_file in "${rendered_summary}" "${signature_report}"; do
  if [[ ! -f "${required_file}" ]]; then
    printf 'Required image inventory input is missing: %s\n' "${required_file}" >&2
    exit 1
  fi
done

mkdir -p "$(dirname "${live_inventory}")" "$(dirname "${evidence}")"
live_inventory_absolute="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "${live_inventory}")"

printf '%s\n' '== live-runtime-image-capture =='
PLATFORM_IMAGE_INVENTORY_CONTROLLER_OUTPUT="${live_inventory_absolute}" \
  ansible-playbook -i inventory/hosts.local.ini \
    ansible/playbooks/capture-platform-image-inventory.yml

reconcile_args=(
  --rendered-summary "${rendered_summary}"
  --live-inventory "${live_inventory}"
  --signature-report "${signature_report}"
  --expected-registry "${expected_registry}"
  --profile "${profile}"
  --commit "$(git rev-parse HEAD)"
  --output "${evidence}"
)
if [[ -n "${exceptions}" ]]; then
  reconcile_args+=(--exceptions "${exceptions}")
fi

printf '%s\n' '== rendered-live-image-reconciliation =='
python3 scripts/reconcile_image_inventory.py "${reconcile_args[@]}"
PLATFORM_PROFILE="${profile}" \
PLATFORM_EXPECTED_COMMIT="$(git rev-parse HEAD)" \
  python3 scripts/verify_image_inventory_evidence.py "${evidence}"
printf 'Image inventory evidence: %s\n' "${evidence}"
