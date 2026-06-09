#!/usr/bin/env bash
set -euo pipefail

: "${RKE2_TOKEN:?Set RKE2_TOKEN in your private shell environment, never in git.}"
: "${RKE2_API_ENDPOINT:?Set RKE2_API_ENDPOINT to your VIP DNS or VIP address.}"
: "${RKE2_CNI:=cilium}"
export INSTALL_RKE2_TYPE="${INSTALL_RKE2_TYPE:-server}"
if [[ -n "${RKE2_VERSION:-}" && -z "${INSTALL_RKE2_VERSION:-}" ]]; then
  export INSTALL_RKE2_VERSION="${RKE2_VERSION}"
fi

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run as root or through sudo on the target Linux node." >&2
  exit 1
fi

mkdir -p /etc/rancher/rke2
cat >/etc/rancher/rke2/config.yaml <<EOF
server: https://${RKE2_API_ENDPOINT}:9345
token: ${RKE2_TOKEN}
cni: ${RKE2_CNI}
tls-san:
  - ${RKE2_API_ENDPOINT}
write-kubeconfig-mode: "0640"
disable:
  - rke2-ingress-nginx
  - rke2-traefik
etcd-expose-metrics: true
EOF

curl -sfL https://get.rke2.io | sh -
systemctl enable rke2-server
systemctl --no-block restart rke2-server
echo "RKE2 server join requested. Follow progress with: journalctl -u rke2-server -f"
