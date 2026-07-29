#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${FORGE_COVERAGE_OUTPUT_DIR:-${ROOT}/rendered/coverage}"
MINIMUM="${FORGE_COVERAGE_MIN:-81.0}"
CONFIG="${ROOT}/.coveragerc"
PYTHON_BIN="${PYTHON_BIN:-python}"

cd "${ROOT}"
mkdir -p "${OUTPUT_DIR}"
export COVERAGE_FILE="${OUTPUT_DIR}/.coverage"

"${PYTHON_BIN}" -m coverage erase --rcfile="${CONFIG}"
for test_script in \
  scripts/test_forge_migration.py \
  scripts/test_forge_cutover.py \
  scripts/test_forge_transition.py
do
  "${PYTHON_BIN}" -m coverage run --rcfile="${CONFIG}" "${test_script}"
done

"${PYTHON_BIN}" -m coverage combine --rcfile="${CONFIG}" "${OUTPUT_DIR}"
"${PYTHON_BIN}" -m coverage json --rcfile="${CONFIG}" -o "${OUTPUT_DIR}/forge-coverage.json"
"${PYTHON_BIN}" -m coverage xml --rcfile="${CONFIG}" -o "${OUTPUT_DIR}/forge-coverage.xml"
"${PYTHON_BIN}" -m coverage report --rcfile="${CONFIG}" --fail-under="${MINIMUM}"
