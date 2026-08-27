#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: repair_woodpecker_oauth_tls.sh AUTO_REPAIR CONVERGENCE_TIMEOUT CHAIN_HELPER" >&2
}

if [ "$#" -ne 3 ]; then
  usage
  exit 2
fi

auto_repair="$1"
convergence_timeout="$2"
chain_helper="$3"
kubectl_bin="${KUBECTL_BIN:-/var/lib/rancher/rke2/bin/kubectl}"
kubeconfig_path="${KUBECONFIG_PATH:-/etc/rancher/rke2/rke2.yaml}"
forgejo_tls_secret_name="forgejo-tls"

case "${auto_repair}" in
  true|false) ;;
  *)
    echo "AUTO_REPAIR must be true or false" >&2
    exit 2
    ;;
esac
case "${convergence_timeout}" in
  ''|*[!0-9]*)
    echo "CONVERGENCE_TIMEOUT must be a positive integer" >&2
    exit 2
    ;;
esac
if [ "${convergence_timeout}" -lt 1 ]; then
  echo "CONVERGENCE_TIMEOUT must be a positive integer" >&2
  exit 2
fi
if [ ! -x "${chain_helper}" ]; then
  echo "TLS chain completion helper is not executable" >&2
  exit 1
fi
if [ ! -x "${kubectl_bin}" ] || [ ! -s "${kubeconfig_path}" ]; then
  echo "RKE2 kubectl or kubeconfig is unavailable" >&2
  exit 1
fi

work_directory="$(mktemp -d /run/platform-woodpecker-oauth-tls.XXXXXX)"
cleanup() {
  rm -rf "${work_directory}"
}
trap cleanup EXIT

trust_bundle=""
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
if [ -z "${trust_bundle}" ]; then
  echo "result=fail reason=system-trust-bundle-missing"
  exit 1
fi

workload=""
for candidate in statefulset/woodpecker-server deployment/woodpecker-server; do
  if "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n woodpecker \
    get "${candidate}" >/dev/null 2>&1; then
    workload="${candidate}"
    break
  fi
done
if [ -z "${workload}" ]; then
  echo "result=fail reason=woodpecker-server-workload-missing"
  exit 1
fi

"${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n woodpecker \
  get "${workload}" -o json >"${work_directory}/woodpecker.json"
forgejo_host="$(python3 - "${work_directory}/woodpecker.json" <<'PY'
import json
import sys
from urllib.parse import urlparse

with open(sys.argv[1], encoding="utf-8") as handle:
    workload = json.load(handle)
forgejo_url = ""
for container in workload.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
    for item in container.get("env", []):
        if item.get("name") == "WOODPECKER_FORGEJO_URL":
            forgejo_url = str(item.get("value") or "").strip()
            break
    if forgejo_url:
        break
parsed = urlparse(forgejo_url if "://" in forgejo_url else f"https://{forgejo_url}")
print(parsed.hostname or "")
PY
)"
if [ -z "${forgejo_host}" ]; then
  echo "result=fail reason=woodpecker-forgejo-url-missing"
  exit 1
fi

"${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n forgejo \
  get ingress -o json >"${work_directory}/ingresses.json"
python3 - "${work_directory}/ingresses.json" "${forgejo_host}" \
  >"${work_directory}/ingress-selection" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
wanted = sys.argv[2]
secrets = set()
addresses = []
matched_tls = False
for ingress in data.get("items", []):
    spec = ingress.get("spec", {})
    rule_hosts = {str(rule.get("host") or "") for rule in spec.get("rules", [])}
    if wanted not in rule_hosts:
        continue
    for entry in ingress.get("status", {}).get("loadBalancer", {}).get("ingress", []) or []:
        address = str(entry.get("ip") or entry.get("hostname") or "").strip()
        if address:
            addresses.append(address)
    for tls in spec.get("tls", []) or []:
        tls_hosts = {str(host) for host in tls.get("hosts", []) or []}
        if tls_hosts and wanted not in tls_hosts:
            continue
        matched_tls = True
        secret = str(tls.get("secretName") or "").strip()
        if secret:
            secrets.add(secret)
print(f"address={addresses[0] if addresses else ''}")
print(f"tls_route={'true' if matched_tls else 'false'}")
for secret in sorted(secrets):
    print(f"secret={secret}")
PY

ingress_address="$(sed -n 's/^address=//p' "${work_directory}/ingress-selection" | head -1)"
mapfile -t tls_secrets < <(sed -n 's/^secret=//p' "${work_directory}/ingress-selection")
tls_route="$(sed -n 's/^tls_route=//p' "${work_directory}/ingress-selection" | head -1)"
if [ "${#tls_secrets[@]}" -lt 1 ]; then
  if [ "${tls_route}" = "true" ] &&
    "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n forgejo \
      get "secret/${forgejo_tls_secret_name}" >/dev/null 2>&1; then
    tls_secrets=("${forgejo_tls_secret_name}")
    # Older fallback routes enabled TLS without naming the managed Secret.
    # Repair only the known platform resources; explicit custom Secrets stay untouched.
    for ingress_name in platform-forgejo forgejo; do
      if "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n forgejo \
        get "ingress/${ingress_name}" >/dev/null 2>&1; then
        "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n forgejo patch \
          "ingress/${ingress_name}" --type=merge \
          -p "{\"spec\":{\"tls\":[{\"hosts\":[\"${forgejo_host}\"],\"secretName\":\"${forgejo_tls_secret_name}\"}]}}" \
          >/dev/null
        echo "forgejo_ingress_tls_binding=${ingress_name} secret=${forgejo_tls_secret_name} state=repaired"
      fi
    done
    if "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n forgejo \
      get ingressroute/forgejo-http >/dev/null 2>&1; then
      "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n forgejo patch \
        ingressroute/forgejo-http --type=merge \
        -p "{\"spec\":{\"tls\":{\"secretName\":\"${forgejo_tls_secret_name}\"}}}" \
        >/dev/null
      echo "forgejo_ingressroute_tls_binding=forgejo-http secret=${forgejo_tls_secret_name} state=repaired"
    fi
  elif [ "${tls_route}" = "true" ]; then
    echo "result=fail reason=forgejo-ingress-tls-material-missing resource=forgejo/${forgejo_tls_secret_name} host=${forgejo_host}"
    exit 1
  else
    echo "result=fail reason=forgejo-ingress-tls-secret-missing host=${forgejo_host}"
    exit 1
  fi
fi
if [ -z "${ingress_address}" ]; then
  ingress_address="$(
    "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n traefik get service \
      -l app.kubernetes.io/name=traefik \
      -o jsonpath='{.items[0].status.loadBalancer.ingress[0].ip}' 2>/dev/null || true
  )"
fi
if [ -z "${ingress_address}" ]; then
  echo "result=fail reason=forgejo-ingress-address-missing host=${forgejo_host}"
  exit 1
fi

probe_served_chain() {
  timeout 20 openssl s_client \
    -connect "${ingress_address}:443" \
    -servername "${forgejo_host}" \
    -verify_return_error \
    -verify_hostname "${forgejo_host}" \
    -verify_depth 5 \
    -CAfile "${trust_bundle}" \
    </dev/null >"${work_directory}/served-probe" 2>&1
}

ready_traefik_pods() {
  "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n traefik get pods \
    -l app.kubernetes.io/name=traefik \
    -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.status.phase}{"|"}{range .status.containerStatuses[*]}{.ready}{","}{end}{"\n"}{end}' |
    awk -F '|' '$2 == "Running" && $3 ~ /true/ && $3 !~ /false/ { print $1 }'
}

refresh_traefik_certificate_cache() {
  local deployment=""
  local expected_ready=""
  local pod=""
  local ready_count=""
  local refresh_deadline=$((SECONDS + convergence_timeout))
  local -a original_pods=()

  for candidate in platform-traefik traefik; do
    if "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n traefik \
      get "deployment/${candidate}" >/dev/null 2>&1; then
      deployment="${candidate}"
      break
    fi
  done
  if [ -z "${deployment}" ]; then
    echo "result=fail reason=traefik-deployment-missing"
    return 1
  fi

  expected_ready="$(
    "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n traefik \
      get "deployment/${deployment}" -o jsonpath='{.spec.replicas}'
  )"
  mapfile -t original_pods < <(ready_traefik_pods)
  if [ "${#original_pods[@]}" -lt "${expected_ready}" ]; then
    echo "result=fail reason=traefik-not-fully-ready ready=${#original_pods[@]} expected=${expected_ready}"
    return 1
  fi

  for pod in "${original_pods[@]}"; do
    "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n traefik \
      delete "pod/${pod}" --wait=false >/dev/null
    echo "traefik_pod=${pod} action=recycled reason=tls-secret-cache-refresh"

    while [ "${SECONDS}" -lt "${refresh_deadline}" ]; do
      ready_count="$(ready_traefik_pods | wc -l | tr -d ' ')"
      if ! "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n traefik \
        get "pod/${pod}" >/dev/null 2>&1 &&
        [ "${ready_count}" -ge "${expected_ready}" ]; then
        break
      fi
      sleep 3
    done

    ready_count="$(ready_traefik_pods | wc -l | tr -d ' ')"
    if "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n traefik \
      get "pod/${pod}" >/dev/null 2>&1 ||
      [ "${ready_count}" -lt "${expected_ready}" ]; then
      echo "result=fail reason=traefik-serial-refresh-timeout ready=${ready_count} expected=${expected_ready}"
      return 1
    fi
  done
}

certificate_fingerprint() {
  openssl x509 -in "$1" -outform DER | sha256sum | awk '{print $1}'
}

certificate_public_key_hash() {
  openssl x509 -in "$1" -pubkey -noout |
    openssl pkey -pubin -outform DER |
    sha256sum | awk '{print $1}'
}

private_key_hash() {
  openssl pkey -in "$1" -pubout -outform DER |
    sha256sum | awk '{print $1}'
}

reconcile_matching_tls_secrets() {
  local source_chain="$1"
  local source_fingerprint=""
  local source_key_hash=""
  local namespace=""
  local secret=""
  local candidate_cert=""
  local candidate_key=""
  local apply_result=""
  local state=""
  local index=0
  local matched=0

  source_fingerprint="$(certificate_fingerprint "${source_chain}")"
  source_key_hash="$(certificate_public_key_hash "${source_chain}")"
  while IFS='|' read -r namespace secret; do
    [ -n "${namespace}" ] && [ -n "${secret}" ] || continue
    index=$((index + 1))
    candidate_cert="${work_directory}/matching-${index}.crt"
    candidate_key="${work_directory}/matching-${index}.key"
    "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n "${namespace}" \
      get "secret/${secret}" -o jsonpath='{.data.tls\.crt}' |
      base64 -d >"${candidate_cert}"
    if [ ! -s "${candidate_cert}" ] ||
      ! openssl x509 -in "${candidate_cert}" -noout >/dev/null 2>&1 ||
      [ "$(certificate_fingerprint "${candidate_cert}")" != "${source_fingerprint}" ]; then
      continue
    fi

    matched=$((matched + 1))
    "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n "${namespace}" \
      get "secret/${secret}" -o jsonpath='{.data.tls\.key}' |
      base64 -d >"${candidate_key}"
    if [ ! -s "${candidate_key}" ] ||
      [ "$(private_key_hash "${candidate_key}")" != "${source_key_hash}" ]; then
      echo "result=fail reason=matching-wildcard-tls-key-mismatch secret=${namespace}/${secret}"
      return 1
    fi

    apply_result="$(
      "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n "${namespace}" \
        create secret tls "${secret}" \
          --cert="${source_chain}" \
          --key="${candidate_key}" \
          --dry-run=client -o yaml |
        "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" apply -f -
    )"
    case "${apply_result}" in
      *created|*configured) state=fullchain-applied ;;
      *) state=present ;;
    esac
    echo "platform_tls_secret=${namespace}/${secret} state=${state} reason=matching-wildcard-leaf-fingerprint"
  done < <(
    "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" get secrets -A \
      -o jsonpath='{range .items[?(@.type=="kubernetes.io/tls")]}{.metadata.namespace}{"|"}{.metadata.name}{"\n"}{end}'
  )

  if [ "${matched}" -lt 1 ]; then
    echo "result=fail reason=matching-wildcard-tls-secret-missing"
    return 1
  fi
}

if probe_served_chain; then
  echo "forgejo_oauth_tls_chain=verified host=${forgejo_host} address=${ingress_address} state=present"
  exit 0
fi
if [ "${auto_repair}" != "true" ]; then
  tail -20 "${work_directory}/served-probe" || true
  echo "result=fail reason=forgejo-oauth-tls-chain-untrusted auto_repair=disabled"
  exit 1
fi

repaired=0
for secret in "${tls_secrets[@]}"; do
  certificate="${work_directory}/${secret}.crt"
  private_key="${work_directory}/${secret}.key"
  fullchain="${work_directory}/${secret}.fullchain.crt"
  "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n forgejo \
    get "secret/${secret}" -o jsonpath='{.data.tls\.crt}' | base64 -d >"${certificate}"
  "${kubectl_bin}" --kubeconfig "${kubeconfig_path}" -n forgejo \
    get "secret/${secret}" -o jsonpath='{.data.tls\.key}' | base64 -d >"${private_key}"
  if [ ! -s "${certificate}" ] || [ ! -s "${private_key}" ]; then
    echo "result=fail reason=forgejo-ingress-tls-material-missing secret=${secret}"
    exit 1
  fi

  "${chain_helper}" "${certificate}" "${fullchain}" "${trust_bundle}"
  if [ "$(certificate_public_key_hash "${fullchain}")" != "$(private_key_hash "${private_key}")" ]; then
    echo "result=fail reason=forgejo-ingress-tls-key-mismatch secret=${secret}"
    exit 1
  fi
  reconcile_matching_tls_secrets "${fullchain}"
  repaired=1
done

initial_wait=$((convergence_timeout / 3))
if [ "${initial_wait}" -lt 5 ]; then
  initial_wait=5
elif [ "${initial_wait}" -gt 30 ]; then
  initial_wait=30
fi
deadline=$((SECONDS + initial_wait))
while [ "${SECONDS}" -lt "${deadline}" ]; do
  if probe_served_chain; then
    echo "forgejo_oauth_tls_chain=verified host=${forgejo_host} address=${ingress_address} state=repaired"
    exit 0
  fi
  sleep 3
done

echo "forgejo_oauth_tls_chain=stale action=refresh-traefik-certificate-cache"
refresh_traefik_certificate_cache

deadline=$((SECONDS + convergence_timeout))
while [ "${SECONDS}" -lt "${deadline}" ]; do
  if probe_served_chain; then
    echo "forgejo_oauth_tls_chain=verified host=${forgejo_host} address=${ingress_address} state=repaired"
    exit 0
  fi
  sleep 3
done

tail -20 "${work_directory}/served-probe" || true
echo "result=fail reason=forgejo-oauth-tls-chain-did-not-converge repaired=${repaired}"
exit 1
