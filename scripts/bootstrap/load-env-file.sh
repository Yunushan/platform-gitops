#!/usr/bin/env bash
set -euo pipefail

load_env_file() {
  local env_file="$1"
  local mode="${2:-overwrite}"
  local line_number=0
  local line key value

  if [[ "${mode}" != "overwrite" && "${mode}" != "preserve-existing" ]]; then
    echo "load_env_file: mode must be overwrite or preserve-existing, got: ${mode}" >&2
    exit 1
  fi

  [[ -f "${env_file}" ]] || return 0

  while IFS= read -r line || [[ -n "${line}" ]]; do
    line_number=$((line_number + 1))
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"

    if [[ -z "${line}" || "${line}" == \#* ]]; then
      continue
    fi

    if [[ "${line}" =~ ^export[[:space:]]+ ]]; then
      line="${line#export}"
      line="${line#"${line%%[![:space:]]*}"}"
    fi

    if [[ ! "${line}" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
      echo "${env_file}:${line_number}: expected KEY=value or export KEY=value" >&2
      exit 1
    fi

    key="${line%%=*}"
    value="${line#*=}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"

    if [[ "${value}" == \"*\" && "${value}" == *\" ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "${value}" == \'*\' && "${value}" == *\' ]]; then
      value="${value:1:${#value}-2}"
    fi

    if [[ "${mode}" == "preserve-existing" && -v ${key} ]]; then
      continue
    fi

    export "${key}=${value}"
  done < "${env_file}"
}
