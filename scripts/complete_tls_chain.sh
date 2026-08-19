#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: complete_tls_chain.sh INPUT_CERT OUTPUT_FULLCHAIN [TRUST_BUNDLE]" >&2
}

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  usage
  exit 2
fi

input_cert="$1"
output_fullchain="$2"
trust_bundle="${3:-}"
auto_complete="${PLATFORM_TLS_AUTO_COMPLETE_CHAIN:-true}"
maximum_issuers="${PLATFORM_TLS_MAX_AIA_ISSUERS:-5}"

case "${auto_complete}" in
  true|false) ;;
  *)
    echo "PLATFORM_TLS_AUTO_COMPLETE_CHAIN must be true or false" >&2
    exit 2
    ;;
esac
case "${maximum_issuers}" in
  ''|*[!0-9]*)
    echo "PLATFORM_TLS_MAX_AIA_ISSUERS must be a positive integer" >&2
    exit 2
    ;;
esac
if [ "${maximum_issuers}" -lt 1 ]; then
  echo "PLATFORM_TLS_MAX_AIA_ISSUERS must be a positive integer" >&2
  exit 2
fi

if [ ! -s "${input_cert}" ]; then
  echo "input certificate is missing or empty: ${input_cert}" >&2
  exit 1
fi

if [ -z "${trust_bundle}" ]; then
  for candidate in \
    /etc/pki/tls/certs/ca-bundle.crt \
    /etc/ssl/certs/ca-certificates.crt \
    /etc/ssl/ca-bundle.pem
  do
    if [ -s "${candidate}" ]; then
      trust_bundle="${candidate}"
      break
    fi
  done
fi
if [ -z "${trust_bundle}" ] || [ ! -s "${trust_bundle}" ]; then
  echo "no readable system trust bundle was found" >&2
  exit 1
fi

work_directory="$(mktemp -d "${TMPDIR:-/tmp}/platform-tls-chain.XXXXXX")"
cleanup() {
  rm -rf "${work_directory}"
}
trap cleanup EXIT

certificate_directory="${work_directory}/certificates"
mkdir -p "${certificate_directory}"

awk -v directory="${certificate_directory}" '
  /-----BEGIN CERTIFICATE-----/ {
    count++
    path=sprintf("%s/cert-%02d.pem", directory, count)
    capture=1
  }
  capture { print > path }
  /-----END CERTIFICATE-----/ {
    close(path)
    capture=0
  }
  END {
    if (capture) exit 2
    print count + 0
  }
' "${input_cert}" >"${work_directory}/count"

certificate_count="$(cat "${work_directory}/count")"
if [ "${certificate_count}" -lt 1 ]; then
  echo "input does not contain a PEM certificate" >&2
  exit 1
fi

for certificate in "${certificate_directory}"/*.pem; do
  openssl x509 -in "${certificate}" -noout >/dev/null
done

leaf="${certificate_directory}/cert-01.pem"
intermediates="${work_directory}/intermediates.pem"
fullchain="${work_directory}/fullchain.pem"
: >"${intermediates}"

is_self_signed() {
  local certificate="$1"
  local subject issuer
  subject="$(openssl x509 -in "${certificate}" -noout -subject -nameopt RFC2253 | sed 's/^subject=//')"
  issuer="$(openssl x509 -in "${certificate}" -noout -issuer -nameopt RFC2253 | sed 's/^issuer=//')"
  [ "${subject}" = "${issuer}" ] &&
    openssl verify -CAfile "${certificate}" "${certificate}" >/dev/null 2>&1
}

last_chain_certificate="${leaf}"
if [ "${certificate_count}" -gt 1 ]; then
  index=2
  while [ "${index}" -le "${certificate_count}" ]; do
    certificate="${certificate_directory}/cert-$(printf '%02d' "${index}").pem"
    if ! is_self_signed "${certificate}"; then
      cat "${certificate}" >>"${intermediates}"
      last_chain_certificate="${certificate}"
    fi
    index=$((index + 1))
  done
fi

write_fullchain() {
  cat "${leaf}" >"${fullchain}"
  if [ -s "${intermediates}" ]; then
    cat "${intermediates}" >>"${fullchain}"
  fi
}

verify_chain() {
  if [ -s "${intermediates}" ]; then
    openssl verify -purpose sslserver \
      -CAfile "${trust_bundle}" \
      -untrusted "${intermediates}" \
      "${leaf}" >/dev/null 2>&1
  else
    openssl verify -purpose sslserver \
      -CAfile "${trust_bundle}" \
      "${leaf}" >/dev/null 2>&1
  fi
}

certificate_fingerprint() {
  openssl x509 -in "$1" -outform DER | sha256sum | awk '{print $1}'
}

chain_contains() {
  local wanted="$1"
  local excluded="${2:-}"
  local certificate
  for certificate in "${certificate_directory}"/*.pem "${work_directory}"/issuer-*.pem; do
    [ -f "${certificate}" ] || continue
    [ "${certificate}" = "${excluded}" ] && continue
    if [ "$(certificate_fingerprint "${certificate}")" = "${wanted}" ]; then
      return 0
    fi
  done
  return 1
}

write_fullchain
if verify_chain; then
  install -m 0600 "${fullchain}" "${output_fullchain}"
  echo "tls_chain=verified certificates=$(grep -c -- '-----BEGIN CERTIFICATE-----' "${fullchain}") aia_downloads=0"
  exit 0
fi

if [ "${auto_complete}" != "true" ]; then
  echo "certificate chain is incomplete or not trusted and AIA completion is disabled" >&2
  exit 1
fi

current="${work_directory}/current.pem"
cp "${last_chain_certificate}" "${current}"
downloads=0

while [ "${downloads}" -lt "${maximum_issuers}" ]; do
  issuer_uri="$(
    openssl x509 -in "${current}" -noout -ext authorityInfoAccess 2>/dev/null |
      sed -n 's/^[[:space:]]*CA Issuers - URI://p' |
      head -1
  )"
  case "${issuer_uri}" in
    http://*|https://*) ;;
    '')
      echo "certificate chain is incomplete and the issuer certificate has no HTTP(S) AIA URI" >&2
      exit 1
      ;;
    *)
      echo "certificate issuer AIA URI uses an unsupported scheme" >&2
      exit 1
      ;;
  esac

  downloads=$((downloads + 1))
  downloaded="${work_directory}/issuer-${downloads}.download"
  issuer="${work_directory}/issuer-${downloads}.pem"
  curl --fail --silent --show-error --location \
    --proto '=http,https' \
    --proto-redir '=http,https' \
    --max-filesize 1048576 \
    --max-redirs 3 \
    --connect-timeout 10 \
    --max-time 30 \
    --output "${downloaded}" \
    "${issuer_uri}"

  if openssl x509 -in "${downloaded}" -noout >/dev/null 2>&1; then
    openssl x509 -in "${downloaded}" -out "${issuer}"
  elif openssl x509 -inform DER -in "${downloaded}" -noout >/dev/null 2>&1; then
    openssl x509 -inform DER -in "${downloaded}" -out "${issuer}"
  else
    echo "downloaded AIA issuer is not an X.509 certificate" >&2
    exit 1
  fi

  if ! openssl x509 -in "${issuer}" -noout -text |
    grep -A2 'Basic Constraints' |
    grep -F 'CA:TRUE' >/dev/null; then
    echo "downloaded AIA issuer is not a CA certificate" >&2
    exit 1
  fi
  if ! openssl verify -partial_chain -CAfile "${issuer}" "${current}" >/dev/null 2>&1; then
    echo "downloaded AIA issuer did not cryptographically sign the child certificate" >&2
    exit 1
  fi

  fingerprint="$(certificate_fingerprint "${issuer}")"
  if chain_contains "${fingerprint}" "${issuer}"; then
    echo "AIA issuer chain contains a cycle" >&2
    exit 1
  fi

  if is_self_signed "${issuer}"; then
    if ! verify_chain; then
      echo "certificate chain terminates at a root that is not in the selected trust bundle" >&2
      exit 1
    fi
    break
  fi

  cat "${issuer}" >>"${intermediates}"
  cp "${issuer}" "${current}"
  write_fullchain
  if verify_chain; then
    install -m 0600 "${fullchain}" "${output_fullchain}"
    echo "tls_chain=completed certificates=$(grep -c -- '-----BEGIN CERTIFICATE-----' "${fullchain}") aia_downloads=${downloads}"
    exit 0
  fi
done

echo "certificate chain could not be completed within ${maximum_issuers} AIA issuer downloads" >&2
exit 1
