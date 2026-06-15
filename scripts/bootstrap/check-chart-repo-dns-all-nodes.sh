#!/usr/bin/env bash
set -euo pipefail

repo_url="${1:?usage: check-chart-repo-dns-all-nodes.sh <repo-url> [check-image] [timeout-seconds] [poll-interval-seconds] [helm-attempts] [helm-timeout-seconds]}"
check_image="${2:-rancher/klipper-helm:v0.10.0-build20260513}"
timeout_seconds="${3:-300}"
poll_interval="${4:-5}"
helm_attempts="${5:-3}"
helm_timeout_seconds="${6:-60}"

kubectl_bin="${KUBECTL:-/var/lib/rancher/rke2/bin/kubectl}"
kubeconfig="${KUBECONFIG:-/etc/rancher/rke2/rke2.yaml}"
namespace="${CHECK_NAMESPACE:-kube-system}"
check_name="platform-chart-repo-dns-check"
check_id="$(printf '%s' "${repo_url}" | sha256sum | awk '{print substr($1, 1, 10)}')"
selector="app.kubernetes.io/name=${check_name},platform.gitops/check-id=${check_id}"
kube_dns_service_names="$("${kubectl_bin}" --kubeconfig "${kubeconfig}" -n kube-system get svc -l k8s-app=kube-dns -o jsonpath='{range .items[*]}{.metadata.name}{" "}{end}' 2>/dev/null | xargs || true)"
kube_dns_service_ips="$("${kubectl_bin}" --kubeconfig "${kubeconfig}" -n kube-system get svc -l k8s-app=kube-dns -o jsonpath='{range .items[*]}{.spec.clusterIP}{" "}{end}' 2>/dev/null | xargs || true)"
coredns_endpoint_ips=""
for kube_dns_service_name in ${kube_dns_service_names}; do
  endpointslice_ips="$("${kubectl_bin}" --kubeconfig "${kubeconfig}" -n kube-system get endpointslice -l "kubernetes.io/service-name=${kube_dns_service_name}" -o jsonpath='{range .items[*].endpoints[*].addresses[*]}{.}{" "}{end}' 2>/dev/null | xargs || true)"
  endpoints_ips="$("${kubectl_bin}" --kubeconfig "${kubeconfig}" -n kube-system get "endpoints/${kube_dns_service_name}" -o jsonpath='{range .subsets[*].addresses[*]}{.ip}{" "}{end}' 2>/dev/null | xargs || true)"
  coredns_endpoint_ips="${coredns_endpoint_ips}${coredns_endpoint_ips:+ }${endpointslice_ips}${endpointslice_ips:+ }${endpoints_ips}"
done
coredns_endpoint_ips="$(printf '%s\n' ${coredns_endpoint_ips} 2>/dev/null | awk 'NF && !seen[$0]++' | xargs || true)"

safe_name() {
  printf '%s' "$1" |
    tr '[:upper:]' '[:lower:]' |
    sed -E 's/[^a-z0-9-]+/-/g; s/^-+//; s/-+$//' |
    cut -c 1-34
}

cleanup_previous() {
  "${kubectl_bin}" --kubeconfig "${kubeconfig}" -n "${namespace}" \
    delete job -l "${selector}" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
}

print_diagnostics() {
  echo "===== per-node chart repository DNS check diagnostics ====="
  "${kubectl_bin}" --kubeconfig "${kubeconfig}" -n "${namespace}" get jobs,pods -l "${selector}" -o wide || true
  for job_ref in $("${kubectl_bin}" --kubeconfig "${kubeconfig}" -n "${namespace}" get jobs -l "${selector}" -o name 2>/dev/null || true); do
    job_name="${job_ref#job.batch/}"
    echo "----- ${job_ref} -----"
    "${kubectl_bin}" --kubeconfig "${kubeconfig}" -n "${namespace}" get "${job_ref}" -o wide || true
    "${kubectl_bin}" --kubeconfig "${kubeconfig}" -n "${namespace}" describe "${job_ref}" || true
    for pod_ref in $("${kubectl_bin}" --kubeconfig "${kubeconfig}" -n "${namespace}" get pods -l "job-name=${job_name}" -o name 2>/dev/null || true); do
      echo "----- ${pod_ref} -----"
      "${kubectl_bin}" --kubeconfig "${kubeconfig}" -n "${namespace}" get "${pod_ref}" -o wide || true
      "${kubectl_bin}" --kubeconfig "${kubeconfig}" -n "${namespace}" logs "${pod_ref}" --all-containers --tail=160 || true
      "${kubectl_bin}" --kubeconfig "${kubeconfig}" -n "${namespace}" describe "${pod_ref}" || true
    done
  done
}

cleanup_previous

nodes="$("${kubectl_bin}" --kubeconfig "${kubeconfig}" get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')"
echo "KUBE_DNS_SERVICE_IPS=${kube_dns_service_ips:-none}"
echo "KUBE_DNS_SERVICE_NAMES=${kube_dns_service_names:-none}"
echo "COREDNS_ENDPOINT_IPS=${coredns_endpoint_ips:-none}"
expected=0
for node in ${nodes}; do
  expected=$((expected + 1))
  node_safe="$(safe_name "${node}")"
  job_name="platform-dns-${check_id}-${node_safe}"
  cat <<YAML | "${kubectl_bin}" --kubeconfig "${kubeconfig}" apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job_name}
  namespace: ${namespace}
  labels:
    app.kubernetes.io/name: ${check_name}
    platform.gitops/check-id: ${check_id}
    platform.gitops/node: "${node}"
    platform.gitops/node-safe: ${node_safe}
spec:
  backoffLimit: 0
  activeDeadlineSeconds: ${timeout_seconds}
  ttlSecondsAfterFinished: 300
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ${check_name}
        platform.gitops/check-id: ${check_id}
        platform.gitops/node: "${node}"
        platform.gitops/node-safe: ${node_safe}
    spec:
      restartPolicy: Never
      nodeName: "${node}"
      tolerations:
        - operator: Exists
      containers:
        - name: check
          image: "${check_image}"
          imagePullPolicy: IfNotPresent
          env:
            - name: REPO_URL
              value: "${repo_url}"
            - name: HELM_ATTEMPTS
              value: "${helm_attempts}"
            - name: HELM_TIMEOUT_SECONDS
              value: "${helm_timeout_seconds}"
            - name: CHECK_NODE_NAME
              value: "${node}"
            - name: KUBE_DNS_SERVICE_IPS
              value: "${kube_dns_service_ips}"
            - name: COREDNS_ENDPOINT_IPS
              value: "${coredns_endpoint_ips}"
          command:
            - sh
            - -c
          args:
            - |
              set -e
              run_bounded() {
                seconds="\$1"
                shift
                if command -v timeout >/dev/null 2>&1; then
                  timeout "\${seconds}" "\$@"
                else
                  "\$@"
                fi
              }
              helm_output_has_failure() {
                grep -E 'Unable to get an update|Error:|i/o timeout|no such host|bad address|TLS handshake timeout|connection timed out|context deadline exceeded|network is unreachable'
              }
              probe_dns_server() {
                dns_server="\$1"
                if [ -z "\${dns_server}" ] || [ "\${dns_server}" = "None" ]; then
                  return 0
                fi
                echo "--- \${dns_server} ---"
                run_bounded 10 nslookup "\${REPO_HOST}" "\${dns_server}" || true
              }
              REPO_HOST="\${REPO_URL#http://}"
              REPO_HOST="\${REPO_HOST#https://}"
              REPO_HOST="\${REPO_HOST%%/*}"
              echo "===== node ====="
              printf '%s\n' "\${CHECK_NODE_NAME:-unknown}"
              echo "===== pod resolver ====="
              cat /etc/resolv.conf
              default_dns_servers="\$(awk '/^nameserver / {print \$2}' /etc/resolv.conf | xargs || true)"
              echo "===== repository host ====="
              printf '%s\n' "\${REPO_HOST}"
              echo "===== default Kubernetes DNS resolution ====="
              getent ahostsv4 "\${REPO_HOST}" || nslookup "\${REPO_HOST}" || true
              echo "===== Kubernetes DNS service IP checks ====="
              dns_service_targets="\${KUBE_DNS_SERVICE_IPS:-\${default_dns_servers}}"
              if [ -n "\${dns_service_targets}" ]; then
                for dns_server in \${dns_service_targets}; do
                  probe_dns_server "\${dns_server}"
                done
              else
                echo "No Kubernetes DNS service IPs or pod nameservers were discovered."
              fi
              echo "===== CoreDNS endpoint IP checks ====="
              if [ -n "\${COREDNS_ENDPOINT_IPS:-}" ]; then
                for dns_server in \${COREDNS_ENDPOINT_IPS}; do
                  probe_dns_server "\${dns_server}"
                done
              else
                echo "No CoreDNS endpoint IPs were discovered before this check."
              fi
              attempt=1
              while [ "\${attempt}" -le "\${HELM_ATTEMPTS}" ]; do
                echo "===== helm repository attempt \${attempt}/\${HELM_ATTEMPTS} ====="
                helm repo remove platform-chart-repo-dns-check >/dev/null 2>&1 || true
                set +e
                add_output="\$(run_bounded "\${HELM_TIMEOUT_SECONDS}" helm repo add platform-chart-repo-dns-check "\${REPO_URL}" 2>&1)"
                add_rc="\$?"
                printf '%s\n' "\${add_output}"
                update_rc="not-run"
                update_unhealthy=false
                if [ "\${add_rc}" -eq 0 ]; then
                  update_output="\$(run_bounded "\${HELM_TIMEOUT_SECONDS}" helm repo update platform-chart-repo-dns-check 2>&1)"
                  update_rc="\$?"
                  printf '%s\n' "\${update_output}"
                  if printf '%s\n' "\${update_output}" | helm_output_has_failure >/dev/null 2>&1; then
                    update_unhealthy=true
                    update_rc=1
                    echo "helm repo update returned an unhealthy repository access result." >&2
                  fi
                fi
                set -e
                if [ "\${add_rc}" -eq 0 ] && [ "\${update_rc}" = "0" ] && [ "\${update_unhealthy}" = "false" ]; then
                  echo "Helm repository check succeeded on attempt \${attempt}."
                  exit 0
                fi
                echo "Helm repository check attempt \${attempt}/\${HELM_ATTEMPTS} failed: repo_add_rc=\${add_rc} repo_update_rc=\${update_rc} repo_update_unhealthy=\${update_unhealthy}" >&2
                if [ "\${attempt}" -lt "\${HELM_ATTEMPTS}" ]; then
                  sleep 5
                fi
                attempt=\$((attempt + 1))
              done
              exit 1
YAML
done

if [ "${expected}" -eq 0 ]; then
  echo "No Kubernetes nodes found." >&2
  exit 1
fi

deadline=$((SECONDS + timeout_seconds))
while [ "${SECONDS}" -lt "${deadline}" ]; do
  total=0
  succeeded=0
  succeeded_nodes=""
  failed=0
  failed_nodes=""
  active=0
  terminal=0
  for job_ref in $("${kubectl_bin}" --kubeconfig "${kubeconfig}" -n "${namespace}" get jobs -l "${selector}" -o name 2>/dev/null || true); do
    total=$((total + 1))
    job_name="${job_ref#job.batch/}"
    node_name="$("${kubectl_bin}" --kubeconfig "${kubeconfig}" -n "${namespace}" get "${job_ref}" -o jsonpath='{.metadata.labels.platform\.gitops/node}' 2>/dev/null || true)"
    if [ -z "${node_name}" ]; then
      node_name="${job_name#platform-dns-${check_id}-}"
    fi
    job_succeeded="$("${kubectl_bin}" --kubeconfig "${kubeconfig}" -n "${namespace}" get "${job_ref}" -o jsonpath='{.status.succeeded}' 2>/dev/null || true)"
    job_failed="$("${kubectl_bin}" --kubeconfig "${kubeconfig}" -n "${namespace}" get "${job_ref}" -o jsonpath='{.status.failed}' 2>/dev/null || true)"
    job_active="$("${kubectl_bin}" --kubeconfig "${kubeconfig}" -n "${namespace}" get "${job_ref}" -o jsonpath='{.status.active}' 2>/dev/null || true)"
    if [ "${job_succeeded}" = "1" ]; then
      succeeded=$((succeeded + 1))
      succeeded_nodes="${succeeded_nodes}${succeeded_nodes:+ }${node_name}"
    fi
    if [ -n "${job_failed}" ] && [ "${job_failed}" != "0" ]; then
      failed=$((failed + 1))
      failed_nodes="${failed_nodes}${failed_nodes:+ }${node_name}"
      echo "Job ${job_name} failed."
    fi
    if [ -n "${job_active}" ] && [ "${job_active}" != "0" ]; then
      active=$((active + 1))
    fi
  done

  terminal=$((succeeded + failed))
  pending=$((expected - total))
  if [ "${pending}" -lt 0 ]; then
    pending=0
  fi
  echo "chart repo DNS per-node status: total=${total}/${expected} pending=${pending} succeeded=${succeeded} failed=${failed} active=${active} terminal=${terminal}/${expected} succeeded_nodes=${succeeded_nodes:-none} failed_nodes=${failed_nodes:-none}"
  if [ "${total}" -eq "${expected}" ] && [ "${succeeded}" -eq "${expected}" ]; then
    cleanup_previous
    exit 0
  fi
  if [ "${total}" -eq "${expected}" ] && [ "${active}" -eq 0 ] && [ "${terminal}" -eq "${expected}" ]; then
    break
  fi
  sleep "${poll_interval}"
done

echo "PLATFORM_CHART_REPO_DNS_SUCCEEDED_NODES=${succeeded_nodes:-none}"
echo "PLATFORM_CHART_REPO_DNS_FAILED_NODES=${failed_nodes:-none}"
print_diagnostics
exit 1
