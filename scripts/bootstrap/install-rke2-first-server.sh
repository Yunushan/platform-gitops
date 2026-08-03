#!/usr/bin/env bash
set -euo pipefail

: "${RKE2_TOKEN:?Set RKE2_TOKEN in your private shell environment, never in git.}"
: "${RKE2_API_ENDPOINT:?Set RKE2_API_ENDPOINT to your VIP DNS or VIP address.}"
: "${RKE2_VERSION:?Set RKE2_VERSION to an exact release such as v1.35.6+rke2r1.}"
: "${RKE2_INSTALL_SCRIPT_SHA256:?Set RKE2_INSTALL_SCRIPT_SHA256 to the reviewed get.rke2.io installer digest.}"
: "${RKE2_CNI:=cilium}"
: "${RKE2_INSTALL_TIMEOUT:=1200}"
: "${RKE2_INSTALL_DOWNLOAD_TIMEOUT:=120}"
umask 077
readonly RKE2_INSTALLER_MAX_BYTES=$((2 * 1024 * 1024))

if [[ -n "${INSTALL_RKE2_TYPE:-}" && "${INSTALL_RKE2_TYPE}" != "server" ]]; then
  echo "INSTALL_RKE2_TYPE must be server for this bootstrap script." >&2
  exit 1
fi
if [[ ! "${RKE2_VERSION}" =~ ^v?[0-9]+[.][0-9]+[.][0-9]+[+]rke2r[0-9]+$ ]]; then
  echo "RKE2_VERSION must be an exact release such as v1.35.6+rke2r1." >&2
  exit 1
fi
if [[ -n "${INSTALL_RKE2_VERSION:-}" && "${INSTALL_RKE2_VERSION}" != "${RKE2_VERSION}" ]]; then
  echo "INSTALL_RKE2_VERSION must match RKE2_VERSION when both are set." >&2
  exit 1
fi
installer_sha256="${RKE2_INSTALL_SCRIPT_SHA256,,}"
if [[ ! "${installer_sha256}" =~ ^[a-f0-9]{64}$ ]]; then
  echo "RKE2_INSTALL_SCRIPT_SHA256 must be exactly 64 hexadecimal characters." >&2
  exit 1
fi
if [[ ! "${RKE2_API_ENDPOINT}" =~ ^[A-Za-z0-9][A-Za-z0-9.:-]*$ ]]; then
  echo "RKE2_API_ENDPOINT contains unsupported characters." >&2
  exit 1
fi
if [[ ! "${RKE2_CNI}" =~ ^[A-Za-z0-9][A-Za-z0-9,._-]*$ ]]; then
  echo "RKE2_CNI contains unsupported characters." >&2
  exit 1
fi
if [[ "${RKE2_TOKEN}" == *$'\n'* || "${RKE2_TOKEN}" == *$'\r'* ]]; then
  echo "RKE2_TOKEN must not contain line breaks." >&2
  exit 1
fi
for timeout_value in "${RKE2_INSTALL_TIMEOUT}" "${RKE2_INSTALL_DOWNLOAD_TIMEOUT}"; do
  if [[ ! "${timeout_value}" =~ ^[1-9][0-9]*$ ]] ||
    (( ${#timeout_value} > 5 )) || (( 10#${timeout_value} > 86400 )); then
    echo "RKE2 install timeouts must be whole seconds between 1 and 86400." >&2
    exit 1
  fi
done
export INSTALL_RKE2_TYPE="server"
export INSTALL_RKE2_VERSION="${RKE2_VERSION}"
unset INSTALL_RKE2_CHANNEL

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run as root or through sudo on the first Linux node." >&2
  exit 1
fi

for required_command in chmod chown curl id install mktemp mv rm sed sha256sum systemctl timeout wc; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    printf 'Required RKE2 bootstrap command is unavailable: %s\n' \
      "${required_command}" >&2
    exit 1
  fi
done

config_tmp=""
installer=""
cleanup() {
  [[ -z "${config_tmp:-}" ]] || rm -f -- "${config_tmp}"
  [[ -z "${installer:-}" ]] || rm -f -- "${installer}"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

installer="$(mktemp)"
curl --fail --show-error --silent --location --retry 3 \
  --retry-all-errors \
  --proto '=https' --proto-redir '=https' --tlsv1.2 \
  --max-redirs 3 \
  --connect-timeout 20 --max-time "${RKE2_INSTALL_DOWNLOAD_TIMEOUT}" \
  --max-filesize "${RKE2_INSTALLER_MAX_BYTES}" \
  --output "${installer}" https://get.rke2.io
if [[ ! -f "${installer}" || -L "${installer}" ]]; then
  echo "Downloaded RKE2 installer is not a regular file." >&2
  exit 1
fi
installer_bytes="$(wc -c <"${installer}")"
if [[ ! "${installer_bytes}" =~ ^[1-9][0-9]*$ ]] ||
  (( 10#${installer_bytes} > RKE2_INSTALLER_MAX_BYTES )); then
  echo "Downloaded RKE2 installer has an unsafe size." >&2
  exit 1
fi
printf '%s  %s\n' "${installer_sha256}" "${installer}" | sha256sum --check --strict
chmod 0700 "${installer}"

install -d -o root -g root -m 0750 /etc/rancher/rke2
config_tmp="$(mktemp /etc/rancher/rke2/.config.yaml.XXXXXX)"
quoted_cluster_credential="$(printf '%s' "${RKE2_TOKEN}" | sed "s/'/''/g")"
{
  printf "token: '%s'\n" "${quoted_cluster_credential}"
  printf 'cni: %s\n' "${RKE2_CNI}"
  printf 'tls-san:\n  - %s\n' "${RKE2_API_ENDPOINT}"
  printf 'write-kubeconfig-mode: "0640"\n'
  printf 'disable:\n  - rke2-ingress-nginx\n  - rke2-traefik\n'
  printf 'etcd-expose-metrics: true\n'
} >"${config_tmp}"
chmod 0600 "${config_tmp}"
chown root:root "${config_tmp}"
mv -f -- "${config_tmp}" /etc/rancher/rke2/config.yaml
config_tmp=""

timeout "${RKE2_INSTALL_TIMEOUT}" "${installer}"
systemctl enable rke2-server
systemctl --no-block restart rke2-server
echo "RKE2 first server start requested. Follow progress with: journalctl -u rke2-server -f"
