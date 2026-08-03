#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

require_tool() {
  tool="$1"
  install_hint="$2"
  if ! command -v "${tool}" >/dev/null 2>&1; then
    printf 'security-scan requires %s. %s\n' "${tool}" "${install_hint}" >&2
    return 1
  fi
}

require_tool trivy "Install Trivy from https://aquasecurity.github.io/trivy/."
require_tool gitleaks "Install Gitleaks from https://github.com/gitleaks/gitleaks."
require_tool semgrep "Install Semgrep from https://semgrep.dev/docs/getting-started/."

trivy_severity="${TRIVY_SEVERITY:-HIGH,CRITICAL}"
trivy_exit_code="${TRIVY_EXIT_CODE:-1}"
semgrep_config="${SEMGREP_CONFIG:-${ROOT}/.semgrep.yml}"

trivy_args=(
  fs
  --scanners vuln,secret,misconfig
  --severity "${trivy_severity}"
  --exit-code "${trivy_exit_code}"
)

if [ "${TRIVY_IGNORE_UNFIXED:-true}" = "true" ]; then
  trivy_args+=(--ignore-unfixed)
fi

if [ -f "${ROOT}/trivy.yaml" ]; then
  trivy_args+=(--config "${ROOT}/trivy.yaml")
fi

trivy_args+=("${ROOT}")

gitleaks_args=(
  dir
  --redact
  --verbose
)

if [ -f "${ROOT}/.gitleaks.toml" ]; then
  gitleaks_args+=(--config "${ROOT}/.gitleaks.toml")
fi

gitleaks_args+=("${ROOT}")

semgrep_args=(
  scan
  --config "${semgrep_config}"
  --error
  "${ROOT}"
)

printf '== trivy ==\n'
trivy "${trivy_args[@]}"

printf '== gitleaks ==\n'
gitleaks "${gitleaks_args[@]}"

printf '== semgrep ==\n'
semgrep "${semgrep_args[@]}"

printf 'Security scan completed.\n'
