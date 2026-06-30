#!/usr/bin/env bash
set -euo pipefail

: "${PLATFORM_REPO_URL:?Set PLATFORM_REPO_URL to this repository URL.}"
: "${PLATFORM_PROFILE:=premium-3node}"
: "${PLATFORM_GITOPS_PLACEHOLDER_MODE:=skip-incomplete}"

export PLATFORM_REPO_URL
export PLATFORM_PROFILE
export PLATFORM_GITOPS_PLACEHOLDER_MODE
export PLATFORM_APPLY_GITOPS=true

cat <<'EOF'
scripts/bootstrap/bootstrap-argocd.sh is a compatibility wrapper.

Use the maintained Ansible bootstrap path so Argo CD CRDs are applied with
server-side apply, rollout repair/fallback is available, and GitOps
Applications are registered through the selected profile checker.
EOF

make platform-argocd
