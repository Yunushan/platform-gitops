#!/usr/bin/env bash
set -euo pipefail

copy_if_missing() {
  local src="$1"
  local dst="$2"
  if [[ ! -f "$dst" ]]; then
    cp "$src" "$dst"
    echo "created $dst"
  else
    echo "kept existing $dst"
  fi
}

copy_if_missing config/cluster.example.yaml config/cluster.local.yaml
copy_if_missing inventory/hosts.example.ini inventory/hosts.local.ini

echo ""
echo "Edit only the .local files. They are ignored by git."
