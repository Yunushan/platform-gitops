# Troubleshooting

## Argo CD cannot access this repository

Check that you replaced `<THIS_REPO_URL>` at bootstrap time and configured repository credentials in Argo CD using a private secret flow.

Run the automated platform report:

```bash
make platform-status
```

It prints API VIP readiness, Argo CD pods/services, registered Argo CD Applications, ingress state, expected GUI URLs, and the next command when the GUI layer is not deployed yet.

To bootstrap Argo CD without manually copying commands:

```bash
make platform-argocd
```

`make platform-argocd` also exposes Argo CD through a temporary bootstrap NodePort. The default browser URL is `https://<NODE_1_IP>:30443`, and the same port works on every RKE2 node IP. To expose an already-installed Argo CD instance again:

```bash
make platform-argocd-expose
```

To use a different bootstrap port:

```bash
PLATFORM_ARGOCD_BOOTSTRAP_NODEPORT_HTTPS=31443 make platform-argocd-expose
```

After Traefik and MetalLB provide the real ingress URL, remove the temporary NodePort exposure:

```bash
make platform-argocd-unexpose
```

If Argo CD bootstrap fails with `metadata.annotations: Too long` for `applicationsets.argoproj.io`, rerun `make platform-argocd` after updating to this version of the playbook. The bootstrap uses server-side apply so large Argo CD CRDs are not stored in the client-side `last-applied` annotation.

If the playbook is waiting at Argo CD rollout, it polls for up to 600 seconds by default and prints pod/event diagnostics plus a likely-cause summary on failure. To extend the wait for slow image pulls:

```bash
PLATFORM_ARGOCD_ROLLOUT_TIMEOUT=1200 make platform-argocd
```

After an Argo CD timeout, collect the live state again without changing the cluster:

```bash
make platform-argocd-diagnose
```

The diagnostic target prints pods, workloads, services, CRDs, images, pod events/details, recent logs, recent events, and registry reachability checks for the image registries detected in Argo CD pods.

If only the Argo CD HA Redis pods are failing, you can continue with a simpler bootstrap control plane while investigating Redis HA separately:

```bash
make platform-argocd-core
make platform-status
```

`make platform-argocd-core` removes stale Argo CD HA Redis bootstrap resources and applies the standard Argo CD install manifest. The default `make platform-argocd` starts with the HA manifest, but automatically falls back to core mode when the known HA Redis announce-service bootstrap failure is detected. Use `make platform-argocd-ha` for strict HA-only behavior with no automatic core fallback.

To register platform applications, provide the repository URL and explicitly allow GitOps app registration:

```bash
PLATFORM_REPO_URL=<THIS_REPO_URL> PLATFORM_APPLY_GITOPS=true make platform-argocd
```

The playbook checks the selected GitOps profile for unresolved placeholders before it registers applications. This prevents Argo CD from syncing incomplete domains, storage sizes, backup targets, or secret references.

## MetalLB does not assign addresses

Check that the address pool was customized from placeholders to your private network range in ignored or encrypted configuration.

Deploy or repair the full ingress foundation from `inventory/hosts.local.ini`:

```bash
make platform-ingress
make platform-status
```

This installs MetalLB and Traefik through the RKE2 Helm controller, applies the configured app VIP, publishes Argo CD at `https://argocd.<PLATFORM_DOMAIN>`, verifies it on 443, and removes the temporary Argo CD NodePort exposure.

To shorten a MetalLB or Traefik wait while testing:

```bash
PLATFORM_INGRESS_ROLLOUT_TIMEOUT=180 make platform-ingress
```

To wait longer on slow chart/image pulls:

```bash
PLATFORM_INGRESS_ROLLOUT_TIMEOUT=1200 make platform-ingress
```

If applying the app VIP fails with `failed calling webhook` or `context deadline exceeded` for `metallb-webhook-service`, the Kubernetes API server could not reach MetalLB's validating webhook yet. The ingress playbook now waits for the webhook service endpoints and runs a server-side dry-run of the MetalLB pool before creating the real resources. To wait longer for that webhook phase:

```bash
PLATFORM_METALLB_WEBHOOK_TIMEOUT=1200 make platform-ingress
```

Each webhook service-path and admission probe is bounded separately so a bad webhook path does not make every retry wait on Kubernetes' default admission timeout. The default is 5 seconds per probe:

```bash
PLATFORM_METALLB_WEBHOOK_PROBE_TIMEOUT=3 PLATFORM_METALLB_WEBHOOK_TIMEOUT=120 make platform-ingress
```

If `helm-install-platform-metallb` or `helm-install-platform-traefik` stays `Running` for many minutes with restarts, rerun:

```bash
make platform-ingress
```

The ingress playbook cleans stale platform Helm install jobs before retrying by default and prints HelmChart/job/pod logs if CRDs still do not appear. To disable cleanup while debugging:

```bash
PLATFORM_INGRESS_CLEANUP_HELM_JOBS=false make platform-ingress
```

If Helm logs show `lookup ... on ...:53: i/o timeout`, pod DNS cannot resolve external chart repositories through CoreDNS. The `platform-ingress` target runs DNS repair first. To run that step directly:

```bash
make platform-dns-repair
```

The repair excludes Kubernetes DNS service IPs from CoreDNS upstream candidates. If a node resolver points back to the cluster DNS service, forwarding CoreDNS to that address creates a DNS loop and pod lookups will time out. The playbook also tests discovered upstream candidates from inside a pod and configures CoreDNS only with candidates that resolve the chart repository from the cluster network.

If direct upstream DNS works from pods but Kubernetes DNS service lookups still time out, the problem is the Kubernetes DNS service path rather than the upstream resolver. The repair now applies the CNI service-path host prerequisites on all nodes, including reverse-path-filter sysctls, Cilium VXLAN/Geneve firewalld ports, trusted pod CIDR firewalld sources, and trusted Cilium firewalld interfaces, then restarts kube-proxy when present, Cilium, and CoreDNS. To disable that bootstrap repair step:

```bash
PLATFORM_DNS_SERVICE_PATH_REPAIR=false make platform-ingress
```

The service-path repair is split into visible kube-proxy, Cilium, and CoreDNS tasks. If kube-proxy is delivered as static RKE2 pods instead of a DaemonSet, the playbook deletes those pods and waits for all three replacements to become Running before retrying DNS. Each rollout waits up to 120 seconds by default and polls every 5 seconds. To shorten that while troubleshooting:

```bash
PLATFORM_DNS_SERVICE_PATH_ROLLOUT_TIMEOUT=45 \
PLATFORM_DNS_SERVICE_PATH_POLL_INTERVAL=5 \
make platform-ingress
```

After those component restarts, the playbook re-detects the current CoreDNS endpoint IPs and reruns the service-path DNS probe before printing the final classification. This avoids diagnosing stale CoreDNS pod IPs after a rollout.

The static kube-proxy delete request is non-blocking and uses a 30-second Kubernetes API request timeout by default. To make that fail faster:

```bash
PLATFORM_DNS_KUBE_PROXY_DELETE_TIMEOUT=10 make platform-ingress
```

If the playbook says direct upstream DNS works but direct CoreDNS endpoint DNS fails, pod-to-pod overlay traffic is still broken. Rerun node preparation so firewalld trusts the pod CIDR and Cilium interfaces on every node:

```bash
make rke2-prepare
make platform-ingress
```

For non-default RKE2 pod CIDRs, override the trusted CIDR:

```bash
PLATFORM_DNS_POD_CIDRS="<RKE2_POD_CIDR>" make platform-ingress
```

To force explicit CoreDNS upstreams:

```bash
PLATFORM_DNS_UPSTREAMS="DNS_SERVER_1 DNS_SERVER_2" make platform-ingress
```

To shorten or extend the DNS test window:

```bash
PLATFORM_DNS_CHECK_TIMEOUT=60 make platform-ingress
```

To make each in-pod DNS/HTTPS probe fail faster while keeping the outer check window:

```bash
PLATFORM_DNS_PROBE_TIMEOUT=10 PLATFORM_DNS_CHECK_TIMEOUT=60 make platform-ingress
```

Helm repository add/update uses a separate timeout because chart repository access can be slower than DNS probes. The default is 90 seconds per Helm command:

```bash
PLATFORM_DNS_HELM_TIMEOUT=180 make platform-ingress
```

If the DNS check resolves a public IPv6 address and then fails with `network is unreachable`, keep the default IPv4-only DNS repair mode enabled. The repair suppresses external AAAA answers through CoreDNS so in-cluster Helm jobs use IPv4. Disable it only on networks with working IPv6 egress:

```bash
PLATFORM_DNS_IPV4_ONLY=false make platform-ingress
```

If resolution succeeds but Helm repository add/update times out, the problem is pod egress rather than CoreDNS, or the Helm timeout is too short for your network path. Check firewall, NAT/masquerade, proxy policy, TLS inspection, or use an internal chart mirror reachable from pods. The direct repository HTTPS index probe is diagnostic only; the playbook no longer fails before the Helm repository check just because `curl` or `wget` behaves differently from the pod resolver.

When using internal chart mirrors, override the platform chart repos:

```bash
PLATFORM_METALLB_CHART_REPO="https://<INTERNAL_HELM_MIRROR>/metallb" \
PLATFORM_TRAEFIK_CHART_REPO="https://<INTERNAL_HELM_MIRROR>/traefik" \
make platform-ingress
```

## API VIP or API DNS does not answer

If all RKE2 nodes are `Ready` but the VIP or API DNS fails:

```bash
curl -k https://<VIP_ADDRESS>:6443/readyz
curl -k https://<VIP_DNS_NAME>:6443/readyz
```

deploy kube-vip and write controller host resolution:

```bash
make rke2-api-vip
make rke2-controller-hosts
```

Then retest the same `curl` commands. `make rke2-api-vip` deploys kube-vip as a control-plane DaemonSet in ARP mode. The default image is pulled from `ghcr.io`, so include that endpoint in registry/proxy/mirror rules.

If plain `curl` returns `401 Unauthorized`, the VIP is already reaching the Kubernetes API server. Use an authenticated kubeconfig check to verify readiness:

```bash
kubectl --kubeconfig <PATH_TO_PRIVATE_KUBECONFIG> --server=https://<VIP_ADDRESS>:6443 get --raw=/readyz
```

If kube-vip pods enter `CrashLoopBackOff` while the image is already present, check the pod logs. On SELinux-enforcing enterprise Linux nodes, kube-vip may need IPVS modules loaded on the host before the container starts. `make rke2-api-vip` loads and persists `ip_vs` and `ip_vs_rr` for this reason.

If logs show an invalid CIDR like `invalid CIDR address: <VIP>32`, use the default `kube_vip_subnet=/32` value. The slash is required by kube-vip when building the VIP CIDR.

## Ansible or host resolution fails

Run:

```bash
make rke2-preflight
```

This checks SSH, passwordless sudo, required VIP/domain variables, and node `/etc/hosts` entries.

If the WSL/controller machine cannot resolve `api.platform.local` or platform app names, also update the controller:

```bash
ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/preflight.yml \
  -e manage_controller_hosts=true
```

## RKE2 install appears stuck

Use the Ansible install playbook instead of a long ad-hoc shell command:

```bash
make rke2-install
```

The playbook runs the package installer asynchronously, polls progress, starts `rke2-server` without blocking Ansible output, verifies service readiness, and prints diagnostics if install or startup exceeds the timeout.
The `rke2-install` target also runs preflight and node preparation first, including Rocky/RHEL 10 `kernel-modules-extra`, kernel modules, swap disablement, CNI sysctls, Cilium overlay firewalld ports, trusted pod CIDR/Cilium firewalld handling, and NetworkManager CNI handling.

Collect current process, service, journal, disk, and memory diagnostics:

```bash
make rke2-status
```

If only one host appears stuck, limit the check to that node:

```bash
make rke2-ping HOST=node-1
make rke2-status HOST=node-1
```

If you interrupted `make rke2-install`, clean stale installer processes before rerunning it:

```bash
make rke2-cleanup-installers HOST=node-1
```

If logs show `no route to host` for `:9345`, run node preparation again to open firewalld ports, then test node-to-node reachability:

```bash
make rke2-prepare
make rke2-network-check
```

If node-1 repeatedly logs `Pod for etcd not synced (pod sandbox not found)` and `127.0.0.1:2379: connect: connection refused`, the first server did not get embedded etcd running. First rerun the prepared install path:

```bash
RKE2_JOIN_ENDPOINT=<NODE_1_IP> make rke2-install
```

If this is still a failed partial bootstrap and there is no production cluster data yet, reset the failed bootstrap state and reinstall:

```bash
CONFIRM_RKE2_RESET=YES_I_UNDERSTAND RKE2_RESET_CONTROLLER_TOKEN=true make rke2-reset
RKE2_JOIN_ENDPOINT=<NODE_1_IP> make rke2-install
```

The install and recovery playbooks print kernel module, swap, sysctl, `kernel-modules-extra`, CRI, containerd, listener, process, disk, and memory diagnostics for this failure pattern. You can collect the same diagnostics directly:

```bash
make rke2-diagnose HOST=node-1
```

If diagnostics show `net/http: TLS handshake timeout` while pulling images such as `rancher/hardened-etcd`, `rancher/hardened-kubernetes`, or `rancher/rke2-cloud-provider`, the first server is blocked by registry egress, not local etcd configuration. Check the node-to-registry path:

```bash
make rke2-registry-check
```

When only one node fails after a network change, retest that node directly:

```bash
make rke2-registry-check HOST=node-2
make rke2-registry-check HOST=node-3
```

Fix firewall, proxy, DNS, MTU, TLS inspection, or internet egress from all three nodes to Docker Hub. For enterprise environments, prefer an internal registry mirror or airgap image flow, then set `rke2_registry_check_urls` to the mirror endpoints. Disable the check only after the mirror is configured:

```bash
RKE2_REGISTRY_CHECK_ENABLED=false make rke2-install
```

If nodes are registered but remain `NotReady` and Cilium pods show `Init:ImagePullBackOff`, check the Cilium pod events and image names. Depending on the chart image settings, the required registry may include `quay.io` as well as Docker Hub:

```bash
ansible -i inventory/hosts.local.ini node-1 -b -m shell -a '
K=/var/lib/rancher/rke2/bin/kubectl
C=/etc/rancher/rke2/rke2.yaml
$K --kubeconfig "$C" -n kube-system get ds cilium -o jsonpath="{range .spec.template.spec.initContainers[*]}init:{.name}={.image}{\"\\n\"}{end}{range .spec.template.spec.containers[*]}container:{.name}={.image}{\"\\n\"}{end}"
$K --kubeconfig "$C" -n kube-system describe pod -l k8s-app=cilium | sed -n "/Events:/,\$p"
'
```

If the nodes must use an HTTP proxy for internet access, provide proxy settings through ignored local inventory or private environment variables:

```bash
RKE2_HTTP_PROXY=http://proxy.example.com:8080 \
RKE2_HTTPS_PROXY=http://proxy.example.com:8080 \
RKE2_NO_PROXY=<LOOPBACK>,localhost,<RFC1918_CIDRS>,<NODE_1_IP>,<NODE_2_IP>,<NODE_3_IP>,<API_VIP>,api.platform.local \
make rke2-registry-check
```

When install runs with these variables, the playbook writes `/etc/default/rke2-server` so RKE2, embedded containerd, kubelet, control-plane pods, etcd, and kube-proxy receive the proxy configuration.

For interrupted bootstrap, token mismatch, stale process, or node join recovery, use the automated safe recovery flow:

```bash
make rke2-recover
```

This does not delete `/var/lib/rancher/rke2` cluster data. It reuses the existing first-server token, repairs config, opens firewalld ports, trusts the pod CIDR/Cilium interfaces, restarts services in the correct order, and waits for all three nodes to report Ready.

Recovery defaults are intentionally short: 300 seconds for service/API stages and 600 seconds for node readiness. On failure, the playbook prints service status, RKE2 journals, listeners, process state, resources, nodes, pods, and events for the failed stage.

Verify the cluster after recovery:

```bash
make rke2-verify
```

Collect focused diagnostics for a failed node:

```bash
make rke2-diagnose HOST=node-1
```

If the first server never became healthy and diagnostics show embedded etcd stuck in authentication handshake failures, use the guarded destructive reset for a failed bootstrap:

```bash
CONFIRM_RKE2_RESET=YES_I_UNDERSTAND make rke2-reset
make rke2-prepare
RKE2_JOIN_ENDPOINT=<NODE_1_IP> make rke2-install
```

This deletes RKE2 cluster state on the selected nodes. Use it only before production data exists or after restoring from backup.

If the network or image pulls are slow, extend the timeouts:

```bash
RKE2_INSTALL_TIMEOUT=1800 RKE2_START_TIMEOUT=1200 make rke2-install
```

If logs show image pull failures such as `image ... not found`, pin a known-good RKE2 version:

```bash
RKE2_VERSION='v1.35.4+rke2r1' make rke2-install
```

You can also use:

```bash
ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/install-rke2.yml \
  -e rke2_version='v1.35.4+rke2r1'
```

## CI cannot push images

Check Harbor robot account permissions. Do not commit robot account credentials.

## Secret scanner fails

Replace real values with placeholders or move them to ignored local files or encrypted secret workflows.
