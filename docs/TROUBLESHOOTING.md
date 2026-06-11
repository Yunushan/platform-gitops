# Troubleshooting

## Argo CD cannot access this repository

Check that you replaced `<THIS_REPO_URL>` at bootstrap time and configured repository credentials in Argo CD using a private secret flow.

## MetalLB does not assign addresses

Check that the address pool was customized from placeholders to your private network range in ignored or encrypted configuration.

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
The `rke2-install` target also runs preflight and node preparation first, including Rocky/RHEL 10 `kernel-modules-extra`, kernel modules, swap disablement, sysctls, firewalld ports, and NetworkManager CNI handling.

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

This does not delete `/var/lib/rancher/rke2` cluster data. It reuses the existing first-server token, repairs config, opens firewalld ports, restarts services in the correct order, and waits for all three nodes to report Ready.

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
