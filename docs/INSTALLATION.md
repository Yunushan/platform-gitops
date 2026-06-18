# Installation Guide

## Prerequisites

- Three Linux nodes for RKE2 server mode.
- A node operating system from `docs/NODE_OS_SUPPORT.md`.
- A virtual IP or DNS name for the Kubernetes API endpoint.
- SSH access from an admin workstation.
- Git, kubectl, Helm, and basic shell tools.
- Off-cluster backup location.

The default recommendation is Rocky Linux 10 on all three nodes, with RKE2 using Cilium as the CNI. For the premium profile, Rocky Linux 10 remains the zero-subscription default; SLES, RHEL, Oracle Linux, and Ubuntu Server LTS are also suitable enterprise choices. Debian, AlmaLinux, CentOS Stream, Fedora, Arch, Gentoo, and Linux Mint are documented as compatible or lab/workstation targets where upstream validation is limited.

## Step 1: Prepare local configuration

```bash
make init-local
```

Edit:

```text
config/cluster.local.yaml
inventory/hosts.local.ini
```

## Step 2: Configure VIP

Default: kube-vip.

Alternative examples are stored in:

```text
scripts/vip/haproxy.cfg.example
scripts/vip/keepalived.conf.example
```

## Step 3: Run preflight checks

The preflight playbook checks Ansible connectivity, confirms passwordless sudo, validates required VIP/domain variables, and writes the platform `/etc/hosts` block on all three nodes.

Set these in `inventory/hosts.local.ini`:

```ini
[rke2_servers:vars]
rke2_api_vip=<VIP_ADDRESS>
rke2_api_dns=<VIP_DNS_NAME>
rke2_ingress_vip=<INGRESS_VIP_ADDRESS>
rke2_platform_domain=<PLATFORM_DOMAIN>
```

If your public/internal DNS uses flat service names instead of
`<service>.<PLATFORM_DOMAIN>`, set explicit GUI FQDNs too:

```ini
platform_argocd_host=<ARGOCD_FQDN>
platform_git_host=<GIT_FQDN>
platform_ci_host=<CI_FQDN>
platform_registry_host=<REGISTRY_FQDN>
platform_grafana_host=<GRAFANA_FQDN>
platform_prometheus_host=<PROMETHEUS_FQDN>
```

Run:

```bash
make rke2-preflight
```

To also write the same block into the Ansible controller's `/etc/hosts`, run:

```bash
ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/preflight.yml \
  -e manage_controller_hosts=true
```

Use `--ask-become-pass` if your controller user needs a sudo password.

## Step 4: Install RKE2

Recommended Ansible flow:

```bash
make rke2-install
```

`make rke2-install` runs preflight, node preparation, and registry egress checks before installing RKE2. On Rocky Linux 10 and other RHEL 10-compatible nodes, preparation installs `kernel-modules-extra`, loads Kubernetes/CNI kernel modules, disables swap, applies Kubernetes sysctls, disables reverse-path filtering for CNI traffic on all active interfaces, opens required firewalld ports including Cilium VXLAN/Geneve overlay ports, trusts the RKE2 pod CIDR, RKE2 node IPs, and Cilium interfaces in firewalld, installs direct firewalld ACCEPT rules for pod CIDR and CNI interface forwarding, and configures NetworkManager to ignore CNI interfaces.

To check image registry egress without reinstalling:

```bash
make rke2-registry-check
```

If your enterprise network uses a private registry mirror or airgap image flow, set `rke2_registry_check_urls` to the mirror endpoints, or disable the public registry check only after the mirror is configured:

```bash
RKE2_REGISTRY_CHECK_ENABLED=false make rke2-install
```

If internet access requires an HTTP proxy, set `rke2_http_proxy`, `rke2_https_proxy`, and `rke2_no_proxy` in ignored local inventory, or export `RKE2_HTTP_PROXY`, `RKE2_HTTPS_PROXY`, and `RKE2_NO_PROXY` before running `make rke2-install`. The install playbook writes `/etc/default/rke2-server` for the RKE2 systemd service.

If bootstrap is interrupted or nodes fail to join after the first server starts, use the safe recovery flow:

```bash
make rke2-recover
```

The recovery flow uses 300-second service/API stage timeouts and a 600-second node readiness timeout by default. It prints stage diagnostics on failure and runs `make rke2-verify` after recovery.

For a failed bootstrap that never reached a healthy cluster state, use the guarded destructive reset:

```bash
CONFIRM_RKE2_RESET=YES_I_UNDERSTAND make rke2-reset
RKE2_JOIN_ENDPOINT=<NODE_1_IP> make rke2-install
```

The install playbook reads these from `inventory/hosts.local.ini`:

```ini
[rke2_servers:vars]
rke2_api_vip=<VIP_ADDRESS>
rke2_api_dns=<VIP_DNS_NAME>
```

If `rke2_token` is omitted or left as a placeholder, the playbook generates a private controller-side token at:

```text
~/.config/platform-gitops/rke2-token
```

To pin an exact RKE2 version, use either environment variable style:

```bash
RKE2_VERSION='v1.35.4+rke2r1' make rke2-install
```

or Ansible extra vars:

```bash
ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/install-rke2.yml \
  -e rke2_version='v1.35.4+rke2r1'
```

If no version is pinned, the playbook uses the configured channel:

```ini
rke2_channel=stable
```

The playbook defaults to:

```text
rke2_cni=cilium
```

Override `rke2_cni` only if you intentionally choose another supported RKE2 CNI.

If the API VIP is not active yet, temporarily point joining servers at node-1 while keeping the API VIP in TLS SANs:

```bash
RKE2_JOIN_ENDPOINT=<NODE_1_IP> make rke2-install
```

or:

```bash
ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/install-rke2.yml \
  -e rke2_join_endpoint=<NODE_1_IP>
```

Manual scripts remain available for debugging:

```bash
sudo RKE2_TOKEN=<TOKEN> RKE2_API_ENDPOINT=<VIP_DNS_NAME> RKE2_VERSION=<RKE2_VERSION> scripts/bootstrap/install-rke2-first-server.sh
sudo RKE2_TOKEN=<TOKEN> RKE2_API_ENDPOINT=<VIP_DNS_NAME> RKE2_VERSION=<RKE2_VERSION> scripts/bootstrap/install-rke2-server.sh
```

Never store the real token in git.

## Step 5: Bootstrap the platform control plane

Before bootstrapping GitOps, deploy and verify the Kubernetes API VIP:

```bash
make rke2-api-vip
make rke2-controller-hosts
curl -k https://<VIP_ADDRESS>:6443/readyz
curl -k https://<VIP_DNS_NAME>:6443/readyz
```

Plain `curl` may return `401 Unauthorized` when anonymous API access is disabled; that still proves the VIP reaches the API server. Use kubeconfig for an authenticated readiness check:

```bash
kubectl --kubeconfig <PATH_TO_PRIVATE_KUBECONFIG> --server=https://<VIP_ADDRESS>:6443 get --raw=/readyz
kubectl --kubeconfig <PATH_TO_PRIVATE_KUBECONFIG> --server=https://<VIP_DNS_NAME>:6443 get --raw=/readyz
```

`make rke2-api-vip` deploys kube-vip as a control-plane DaemonSet in ARP mode. It uses `rke2_api_vip`, `rke2_api_dns`, and the node default interface unless `kube_vip_interface` is set. Pin kube-vip with `kube_vip_version`, `kube_vip_image`, or the matching `KUBE_VIP_*` environment variables.

For the normal post-RKE2 flow, use the higher-level automation:

```bash
make platform-bootstrap
```

This verifies RKE2, deploys/verifies the API VIP, writes controller host entries, bootstraps Argo CD, verifies or repairs pod DNS, installs MetalLB and Traefik, binds the app VIP, publishes Argo CD on HTTPS 443, and prints an access report with API endpoints, GUI URLs, service state, and ingress state.

To show the same report later without changing the cluster:

```bash
make platform-status
```

To install only Argo CD and expose it through a temporary bootstrap NodePort:

```bash
make platform-argocd
```

The default bootstrap Argo CD URL is:

```text
https://<NODE_1_IP>:30443
```

The same NodePort works through all three node IPs. To change the bootstrap HTTPS port:

```bash
PLATFORM_ARGOCD_BOOTSTRAP_NODEPORT_HTTPS=31443 make platform-argocd-expose
```

After Traefik and MetalLB are ready, prefer the platform ingress URL and remove the temporary bootstrap NodePort:

```bash
make platform-argocd-unexpose
```

To register platform applications in Argo CD, first replace or privately render all placeholders in the selected GitOps profile. Then run:

```bash
export PLATFORM_REPO_URL=<THIS_REPO_URL>
export PLATFORM_APPLY_GITOPS=true
export PLATFORM_PROFILE=default
make platform-argocd
```

For the premium profile:

```bash
export PLATFORM_PROFILE=premium-3node
```

For first private deployments, `platform-first-deploy` performs the Argo CD
bootstrap, optional private repository credential registration, application
registration, ingress publishing, and status report in one flow:

```bash
export PLATFORM_REPO_URL=https://<PRIVATE_GIT_HOST>/<ORG>/platform-gitops-deploy.git
export PLATFORM_REPO_USERNAME=<GIT_USERNAME>
read -rsp "Private Git token/password: " PLATFORM_REPO_TOKEN
echo
export PLATFORM_REPO_TOKEN

make platform-first-deploy
```

If unresolved placeholders remain, the playbook stops before registering applications so Argo CD does not sync incomplete production configuration.

To deploy or repair the final ingress path separately:

```bash
make platform-ingress
make platform-status
```

To limit ingress rollout waiting, set the timeout in seconds:

```bash
PLATFORM_INGRESS_ROLLOUT_TIMEOUT=180 make platform-ingress
```

Traefik rollout and app-VIP assignment can be controlled separately:

```bash
PLATFORM_TRAEFIK_ROLLOUT_TIMEOUT=180 make platform-ingress
```

MetalLB admission webhooks are checked separately before the app VIP pool is applied. If your cluster is slow to make `metallb-webhook-service` reachable from the API server, extend only that phase:

```bash
PLATFORM_METALLB_WEBHOOK_TIMEOUT=1200 make platform-ingress
```

For faster webhook troubleshooting, reduce the per-probe request timeout while keeping a short outer wait:

```bash
PLATFORM_METALLB_WEBHOOK_PROBE_TIMEOUT=3 PLATFORM_METALLB_WEBHOOK_TIMEOUT=120 make platform-ingress
```

If the webhook path is unhealthy, the ingress playbook automatically restarts MetalLB controller, refreshes kube-proxy, restarts Cilium, and retries the webhook dry-run. To collect diagnostics without that repair pass:

```bash
PLATFORM_METALLB_WEBHOOK_REPAIR=false make platform-ingress
```

If your enterprise network requires internal Helm mirrors:

```bash
PLATFORM_METALLB_CHART_REPO="https://<INTERNAL_HELM_MIRROR>/metallb" \
PLATFORM_TRAEFIK_CHART_REPO="https://<INTERNAL_HELM_MIRROR>/traefik" \
make platform-ingress
```

`make platform-ingress` first verifies pod DNS and repairs CoreDNS upstreams when Helm jobs cannot resolve external chart repositories. It checks the MetalLB chart repository, then the Traefik chart repository, then verifies the Traefik chart repository from a pod pinned to every Kubernetes node before installing either controller. The per-node Traefik check prints Kubernetes DNS service IP and CoreDNS endpoint probes, retries Helm repository add/update inside each pinned pod, and waits for all node checks before printing diagnostics. If a single node still cannot use the Kubernetes DNS service path, the playbook repairs CNI sysctls, active-interface reverse-path filtering, firewalld service-path and node-peer trust, direct pod/CNI ACCEPT rules on every RKE2 node, refreshes kube-proxy/Cilium, and retries. For IPv4-only environments it also suppresses external AAAA answers by default so in-cluster Helm jobs do not select unreachable public IPv6 addresses. Disable that only if the cluster has working IPv6 egress:

```bash
PLATFORM_DNS_IPV4_ONLY=false make platform-ingress
```

If your network has short DNS or chart-repository flaps, increase only the per-node Helm check tolerance:

```bash
PLATFORM_TRAEFIK_DNS_HELM_ATTEMPTS=5 PLATFORM_TRAEFIK_DNS_HELM_TIMEOUT=60 make platform-ingress
```

It then installs MetalLB and Traefik through the RKE2 Helm controller, assigns `rke2_ingress_vip`, publishes Argo CD at the effective Argo CD hostname, verifies the route, and removes the temporary Argo CD NodePort exposure.

`make platform-status` prints the effective GUI URLs, including explicit FQDN overrides such as `platform_git_host`, `platform_ci_host`, and `platform_registry_host` when configured. For browser access from Windows, create equivalent Windows hosts-file or internal DNS records pointing those names at `rke2_ingress_vip`.

The legacy local-kubeconfig bootstrap script remains available:

```bash
export PLATFORM_REPO_URL=<THIS_REPO_URL>
export KUBECONFIG=<PATH_TO_PRIVATE_KUBECONFIG>
scripts/bootstrap/bootstrap-argocd.sh
```

## Step 6: Let GitOps take over

Argo CD reads:

```text
gitops/bootstrap/root-app.yaml
gitops/clusters/rke2-main/platform-apps.yaml
```

For the premium 3-node profile, use:

```text
gitops/bootstrap/root-app-premium-3node.yaml
gitops/clusters/rke2-main/premium-3node/platform-apps.yaml
```

Review each platform application before enabling automatic sync in production.
