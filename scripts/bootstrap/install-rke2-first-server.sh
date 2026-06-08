#!/usr/bin/env bash
set -euo pipefail

: "${RKE2_TOKEN:?Set RKE2_TOKEN in your private shell environment, never in git.}"
: "${RKE2_API_ENDPOINT:?Set RKE2_API_ENDPOINT to your VIP DNS or VIP address.}"
: "${RKE2_CNI:=cilium}"

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run as root or through sudo on the first Linux node." >&2
  exit 1
fi

mkdir -p /etc/rancher/rke2
cat >/etc/rancher/rke2/config.yaml <<EOF
token: ${RKE2_TOKEN}
cni: ${RKE2_CNI}
tls-san:
  - ${RKE2_API_ENDPOINT}
write-kubeconfig-mode: "0640"
EOF

curl -sfL https://get.rke2.io | sh -
systemctl enable --now rke2-server
