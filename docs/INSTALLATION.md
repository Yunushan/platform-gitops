# Installation Guide

## Prerequisites

- Three Linux nodes for RKE2 server mode.
- A node operating system from `docs/NODE_OS_SUPPORT.md`.
- Support tier and lifecycle acceptance from `docs/PLATFORM_SUPPORT.md`.
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

After the API VIP exists, `make rke2-verify` is strict by default: it checks
local RKE2 readiness, every expected node, unauthenticated VIP/DNS `/readyz`
reachability from every node, and authenticated `/readyz` through both the VIP
and DNS name. For a pre-VIP sanity check during early bootstrap only, run:

```bash
RKE2_VERIFY_API_VIP=false make rke2-verify
```

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

The bootstrap NodePort probe is soft by default because host firewalls or
node-local service routing can block direct NodePort access while the final
Traefik/MetalLB ingress path is still healthy. To make NodePort verification
fail-fast, set `PLATFORM_ARGOCD_BOOTSTRAP_NODEPORT_VERIFY_MODE=strict`.

To change the bootstrap HTTPS port:

```bash
PLATFORM_ARGOCD_BOOTSTRAP_NODEPORT_HTTPS=31443 make platform-argocd-expose
```

After Traefik and MetalLB are ready, prefer the platform ingress URL and remove the temporary bootstrap NodePort:

```bash
make platform-argocd-unexpose
```

To register platform applications in Argo CD with strict production validation, first replace or privately render all placeholders in the selected GitOps profile. Then run:

```bash
export PLATFORM_REPO_URL=<THIS_REPO_URL>
export PLATFORM_APPLY_GITOPS=true
export PLATFORM_GITOPS_PLACEHOLDER_MODE=strict
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

`platform-first-deploy`, `platform-first-deploy-auto`, and
`platform-first-deploy-seed` default to
`PLATFORM_GITOPS_PLACEHOLDER_MODE=skip-incomplete`. The bootstrap registers
deployable applications and prints skipped applications that still need private
values such as storage sizes, database DSNs, Redis endpoints, object storage
buckets, backup targets, or TLS secret names.

The unattended targets also default to
`PLATFORM_AUTO_RENDER_PRIVATE_VALUES=true`, which renders private bootstrap
values before validation, commit, and push. The renderer covers Forgejo, Argo
CD, Woodpecker, Harbor, Grafana, Prometheus, Loki, Velero, Longhorn, and
optional step-ca. Forgejo uses `platform_forgejo_host` or `platform_git_host`
from `inventory/hosts.local.ini` unless `PLATFORM_FORGEJO_HOST` is set. The
default `FORGEJO_DATABASE_MODE=postgres` points Forgejo at the CloudNativePG
service `platform-postgres-rw.platform-databases.svc.cluster.local:5432`.
Set `FORGEJO_DATABASE_MODE=sqlite` for a dependency-light lab bootstrap, or
`FORGEJO_DATABASE_MODE=mysql` / `FORGEJO_DATABASE_MODE=mariadb` with
`FORGEJO_DATABASE_HOST=<HOST>:3306` when that database family is the company
standard.

For Forgejo SQL backends, create the runtime password secret first, then render
values that reference only Secret names and non-secret endpoints:

```bash
FORGEJO_DATABASE_PASSWORD='<PASSWORD>' \
PLATFORM_APP_SECRET_REQUIRE_FORGEJO_DATABASE=true \
make platform-app-secrets

FORGEJO_DATABASE_MODE=postgres \
FORGEJO_DATABASE_HOST=<POSTGRES_HOST>:5432 \
FORGEJO_DATABASE_NAME=forgejo \
FORGEJO_DATABASE_USER=forgejo \
FORGEJO_DATABASE_SECRET_NAME=forgejo-database \
FORGEJO_DATABASE_SSL_MODE=disable \
make platform-render-private-values
```

Set `FORGEJO_DATABASE_MODE=sqlite` to switch back to SQLite. For MySQL or
MariaDB, set `FORGEJO_DATABASE_MODE=mysql` or `FORGEJO_DATABASE_MODE=mariadb`,
`FORGEJO_DATABASE_HOST=<HOST>:3306`, and keep the same secret-name pattern.
For enterprise Redis-backed cache and queue, the premium profile uses the
shared `platform-valkey` HA service by default with `FORGEJO_REDIS_MODE=redis`.
`platform-app-secrets` generates `platform-cache/platform-valkey-auth`, then
creates the `forgejo/forgejo-redis` URI secret from it. To use a different
cache, provide a full `FORGEJO_REDIS_URL`, or provide `FORGEJO_REDIS_HOST` plus
`FORGEJO_REDIS_PASSWORD` and optional `FORGEJO_REDIS_PORT`,
`FORGEJO_REDIS_DB`, and `FORGEJO_REDIS_TLS`. Set
`FORGEJO_REDIS_MODE=memory` only for a minimal local cache.

The premium profile also renders Keycloak at `sso.<PLATFORM_DOMAIN>` by
default. `platform-app-secrets` generates `keycloak/keycloak-admin`,
`keycloak/keycloak-database`, and the matching
`platform-databases/keycloak-database` CloudNativePG role password secret unless
you provide `KEYCLOAK_ADMIN_PASSWORD` or `KEYCLOAK_DATABASE_PASSWORD`.

Woodpecker defaults to PostgreSQL-backed HA for the 3-node premium profile.
`platform-app-secrets` generates `woodpecker/woodpecker-database` and the
matching `platform-databases/woodpecker-database` CloudNativePG role password
secret unless you provide `WOODPECKER_DATABASE_PASSWORD` or a full
`WOODPECKER_DATABASE_DATASOURCE`. The renderer pins both Woodpecker server and
agent image repositories plus `WOODPECKER_IMAGE_TAG`, defaulting to `v3.16.0`;
change that only as an intentional upgrade. Render with 3 server replicas for
the shared `platform-postgres` cluster:

```bash
WOODPECKER_DATABASE_MODE=postgres \
WOODPECKER_DATABASE_SECRET_NAME=woodpecker-database \
WOODPECKER_DATABASE_HOST=platform-postgres-rw.platform-databases.svc.cluster.local:5432 \
WOODPECKER_DATABASE_NAME=woodpecker \
WOODPECKER_DATABASE_USER=woodpecker \
WOODPECKER_DATABASE_SSLMODE=disable \
PLATFORM_APP_SECRET_REQUIRE_WOODPECKER_DATABASE=true \
make platform-app-secrets

WOODPECKER_DATABASE_MODE=postgres \
WOODPECKER_DATABASE_SECRET_NAME=woodpecker-database \
WOODPECKER_IMAGE_TAG=v3.16.0 \
WOODPECKER_SERVER_REPLICAS=3 \
WOODPECKER_AGENT_REPLICAS=3 \
make platform-render-private-values
```

For a small non-HA bootstrap, set `WOODPECKER_DATABASE_MODE=sqlite` and keep
`WOODPECKER_SERVER_REPLICAS=1`.

Harbor defaults to internal PostgreSQL, the shared external
`platform-valkey` service, and filesystem registry storage so a first private
registry can come online without pre-existing database or object storage
services. For the premium production posture, create the dependency secrets
from ignored env values and render Harbor with external PostgreSQL, external
Redis, and S3-compatible registry storage. If you keep the shared Valkey
default, `platform-app-secrets` can derive `harbor/harbor-redis` from
`platform-cache/platform-valkey-auth`; set `HARBOR_REDIS_PASSWORD` and
`HARBOR_REDIS_ADDR` only when using a separate Redis or Valkey endpoint:

```bash
HARBOR_DATABASE_PASSWORD='<PASSWORD>' \
HARBOR_REDIS_PASSWORD='<PASSWORD>' \
HARBOR_S3_ACCESS_KEY_ID='<ACCESS_KEY>' \
HARBOR_S3_SECRET_ACCESS_KEY='<SECRET_KEY>' \
PLATFORM_APP_SECRET_REQUIRE_HARBOR_DATABASE=true \
PLATFORM_APP_SECRET_REQUIRE_HARBOR_REDIS=true \
PLATFORM_APP_SECRET_REQUIRE_HARBOR_REGISTRY_STORAGE=true \
make platform-app-secrets

HARBOR_DATABASE_MODE=external \
HARBOR_DATABASE_HOST=<POSTGRES_HOST> \
HARBOR_DATABASE_NAME=registry \
HARBOR_DATABASE_USER=harbor \
HARBOR_DATABASE_SECRET_NAME=harbor-database \
HARBOR_REDIS_MODE=external \
HARBOR_REDIS_ADDR=platform-valkey-primary.platform-cache.svc.cluster.local:6379 \
HARBOR_REDIS_SECRET_NAME=harbor-redis \
HARBOR_STORAGE_MODE=s3 \
HARBOR_S3_BUCKET=<HARBOR_REGISTRY_BUCKET> \
HARBOR_S3_SECRET_NAME=harbor-registry-s3 \
OBJECT_STORAGE_ENDPOINT=<S3_ENDPOINT> \
OBJECT_STORAGE_REGION=<S3_REGION> \
make platform-render-private-values
```

Grafana defaults to persistent SQLite so monitoring can come online during the
first bootstrap. For production monitoring, create a database password secret
and render Grafana with external PostgreSQL:

```bash
GRAFANA_DATABASE_PASSWORD='<PASSWORD>' \
PLATFORM_APP_SECRET_REQUIRE_GRAFANA_DATABASE=true \
make platform-app-secrets

GRAFANA_DATABASE_MODE=postgres \
GRAFANA_DATABASE_HOST=<POSTGRES_HOST> \
GRAFANA_DATABASE_NAME=grafana \
GRAFANA_DATABASE_USER=grafana \
GRAFANA_DATABASE_SECRET_NAME=grafana-database \
GRAFANA_DATABASE_SSL_MODE=disable \
make platform-render-private-values
```

The unattended targets also default to `PLATFORM_RUN_PROFILE_CHECK=true`, so
the selected GitOps registration mode is validated before any commit, push, or
seed mirror update. In `strict` mode the full rendered profile must be complete.
In the default `skip-incomplete` mode, the bootstrap validates that the
deployable Application subset can be rendered before Argo CD receives it.
They also keep `PLATFORM_RUN_NO_SECRETS=true`. Public template validation blocks
internal hostnames, but first private deploy and first seed deploy default
`PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES=true` so rendered private FQDNs
can be pushed to a private deployment repository while plaintext secrets,
private keys, kubeconfigs, and private IPs still fail the safety scan. Keep that
allowance unset or false when pushing back to a public source remote.
Set `PYTHON=/path/to/python` if the bootstrap workstation does not provide
`python3` on `PATH`; `make validate`, `make platform-argocd`, first deploy, and
seed sync all honor the same override.

For object-storage backed apps and external app databases, keep credentials in
ignored env files or your secret manager. `make platform-app-secrets` can create
the Kubernetes secrets for shared platform Valkey auth, Grafana admin
credentials, Grafana's external PostgreSQL password, Forgejo's external
PostgreSQL password and Redis URI, Woodpecker's PostgreSQL datasource,
Harbor's external database/Redis/S3 credentials, Loki, Velero, and
CloudNativePG from
`FORGEJO_DATABASE_PASSWORD`, `FORGEJO_REDIS_URL`,
`WOODPECKER_DATABASE_DATASOURCE`, `WOODPECKER_DATABASE_HOST` /
`WOODPECKER_DATABASE_PASSWORD`, `HARBOR_DATABASE_PASSWORD`,
`HARBOR_REDIS_PASSWORD`, `HARBOR_S3_ACCESS_KEY_ID` /
`HARBOR_S3_SECRET_ACCESS_KEY`, `KEYCLOAK_ADMIN_PASSWORD`,
`KEYCLOAK_DATABASE_PASSWORD`, `GRAFANA_DATABASE_PASSWORD`,
`LOKI_S3_ACCESS_KEY_ID` /
`LOKI_S3_SECRET_ACCESS_KEY`, `VELERO_CLOUD_CREDENTIALS`,
`CNPG_S3_ACCESS_KEY_ID` / `CNPG_S3_SECRET_ACCESS_KEY`, or
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`; committed values only store
bucket names, endpoints, regions, cache sizes, and secret names.
For production runs, set `PLATFORM_APP_SECRET_REQUIRE_OBJECT_STORAGE=true` so
the secret automation fails immediately if Loki, Velero, or CloudNativePG object-storage
credential secrets are still missing.
Set `PLATFORM_APP_SECRET_REQUIRE_CNPG_OBJECT_STORAGE=true` when you only want to
enforce the CloudNativePG backup credential secret without enforcing the rest of
the object-storage stack.
`CNPG_RENDER_POSTGRES_CLUSTER=true` is the default so
`make platform-render-private-values` materializes the premium CloudNativePG
PostgreSQL cluster used by Forgejo. Set `CNPG_RENDER_POSTGRES_CLUSTER=false`
only when you provide PostgreSQL outside this profile. Set
`CNPG_BACKUP_ENABLED=true` with CloudNativePG object-storage credentials when
you want the rendered cluster to include WAL/archive backups.
Keep `PLATFORM_APP_SECRET_REQUIRE_WOODPECKER_DATABASE=true` for the premium
default so a missing Woodpecker PostgreSQL datasource secret fails before
Argo CD rolls Woodpecker.
Set `PLATFORM_APP_SECRET_REQUIRE_HARBOR_DATABASE=true`,
`PLATFORM_APP_SECRET_REQUIRE_HARBOR_REDIS=true`, and
`PLATFORM_APP_SECRET_REQUIRE_HARBOR_REGISTRY_STORAGE=true` before enabling
Harbor external PostgreSQL, Redis, and S3 registry storage.
Set `PLATFORM_APP_SECRET_REQUIRE_FORGEJO_DATABASE=true` before enabling an
external Forgejo SQL backend. Set `PLATFORM_APP_SECRET_REQUIRE_FORGEJO_REDIS=true`
only when `FORGEJO_REDIS_MODE=redis`.
Set `PLATFORM_APP_SECRET_REQUIRE_GRAFANA_DATABASE=true` before enabling
`GRAFANA_DATABASE_MODE=postgres`.

First deployment also runs the cluster DNS/ClusterIP service-path repair before
waiting on Argo CD. This protects against kube-proxy, Cilium, or firewalld
service-routing problems where pods cannot reach Kubernetes service IPs such as
the Kubernetes API service IP or the Argo CD Redis service. To skip that
pre-repair on a known healthy cluster:

```bash
PLATFORM_FIRST_DEPLOY_DNS_REPAIR=false make platform-first-deploy
```

If Argo CD still reports a ClusterIP service-path timeout during bootstrap,
`platform-first-deploy` runs the DNS/service-path repair again, applies the
Argo CD internal repo-server/Redis service repair, and retries Argo CD once by
default. Disable that retry with:

```bash
PLATFORM_FIRST_DEPLOY_ARGOCD_REPAIR_RETRY=false make platform-first-deploy
```

The Argo CD controller may log one Redis timeout while pods are still starting.
The bootstrap waits at least `PLATFORM_ARGOCD_SERVICE_PATH_FAST_FAIL_AFTER=90`
seconds and requires repeated matching timeouts before treating it as a real
ClusterIP service-path failure. The same detector covers controller access to
`argocd-redis:6379`, `argocd-repo-server:8081`, and the Kubernetes API service.

For fully unattended bootstrap, copy the env template and run the automatic
target:

```bash
cp config/first-deploy.env.example private/first-deploy.env
${EDITOR:-vi} private/first-deploy.env
make platform-first-deploy-auto
```

If no previous Git server exists, use the temporary internal seed Git path:

```bash
cp config/seed-git.env.example private/seed-git.env
${EDITOR:-vi} private/seed-git.env
make platform-first-deploy-seed
```

Repeat seed bootstrap runs update the temporary seed Git mirror with
`--force-with-lease` by default, controlled by
`PLATFORM_SEED_GIT_FORCE_WITH_LEASE=true`. This avoids manual reconciliation
when the seed branch is stale after local/private bootstrap commits.
`make platform-seed-git-sync` is seed-only by default for private deployments:
it may pull the configured source remote when `PLATFORM_SEED_SYNC_PULL=true`,
but it does not push back to `origin` unless
`PLATFORM_SEED_SYNC_PUSH_ORIGIN=true` is set. Keep that disabled when `origin`
is the public template repository.

After Forgejo is deployed and becomes the long-term source, remove the
temporary seed service:

```bash
make platform-seed-git-remove
```

Final production health treats the temporary seed service as a bootstrap-only
source. `make platform-app-health` fails if Argo CD Applications still point at
`git://...:9418` or another seed Git URL. Migrate the platform repository into
the intended private Git service, rerun `PLATFORM_REPO_URL=<PRIVATE_REPO_URL>
PLATFORM_APPLY_GITOPS=true make platform-argocd`, then remove seed Git. During
bootstrap-only troubleshooting, bypass just this source check with
`PLATFORM_APP_HEALTH_FORBID_TEMPORARY_REPO=false`.

For final proof that every Argo CD Application is reading from the exact
intended private repository, keep `PLATFORM_REPO_URL=<PRIVATE_REPO_URL>`
exported when running `make platform-production-check`, or set
`PLATFORM_APP_HEALTH_EXPECTED_REPO_URL=<PRIVATE_REPO_URL>` for
`make platform-app-health`. The check fails on seed Git, insecure `git://`, a
missing repo URL, or any repo URL different from the expected private source.

If unresolved placeholders remain and `PLATFORM_GITOPS_PLACEHOLDER_MODE=strict`
is set, the playbook stops before registering applications so Argo CD does not
sync incomplete production configuration.

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

`make platform-status` prints the effective GUI URLs, Argo CD runtime
repo-server/Redis endpoint readiness, Woodpecker server/agent runtime readiness
and expected image tag, per-GUI HTTPS status through the app VIP from both the
cluster side and the Ansible controller/client side, Argo CD Application
sync/health readiness summary, and explicit FQDN overrides such as
`platform_git_host`, `platform_ci_host`, and `platform_registry_host` when
configured. For browser access from Windows, create equivalent Windows
hosts-file or internal DNS records pointing those names at `rke2_ingress_vip`.

The legacy bootstrap script remains available as a compatibility wrapper around
the maintained Ansible path. It no longer applies `gitops/bootstrap/root-app.yaml`
directly; it exports `PLATFORM_APPLY_GITOPS=true` and calls `make
platform-argocd` so the same server-side apply, rollout repair, repository
credential, and profile-check behavior is used:

```bash
export PLATFORM_REPO_URL=<THIS_REPO_URL>
export PLATFORM_PROFILE=premium-3node
scripts/bootstrap/bootstrap-argocd.sh
```

## Step 6: Let GitOps take over

Argo CD registers the selected profile's Application list:

```text
gitops/clusters/rke2-main/platform-apps.yaml
```

For the premium 3-node profile, use:

```text
gitops/clusters/rke2-main/premium-3node/platform-apps.yaml
```

`PLATFORM_GITOPS_PLACEHOLDER_MODE=skip-incomplete` registers only deployable
Applications during first bootstrap. `strict` fails before registration if any
selected Application path still contains unresolved private placeholders.

Before declaring the platform app layer ready, verify Argo CD Application
sync/health, active or failed Argo CD operations, platform pod readiness,
critical HA workload replica coverage, platform PVC readiness, GUI ingress,
and required StorageClasses, GUI backend endpoints, plus the critical Argo CD /
Woodpecker service paths from every RKE2 node and from diagnostic pods pinned to
every RKE2 node. CloudNativePG cluster checks run in `auto` mode by default:
any existing PostgreSQL clusters are verified, while operator-only bootstrap
installs are allowed:

```bash
make platform-app-health
```

To isolate only the Argo CD runtime plus Woodpecker CI path while debugging
502/504 ingress errors or Woodpecker agent `CrashLoopBackOff`, run:

```bash
make platform-ci-health
```

This focused gate keeps Argo CD runtime component and configured
repo-server/Redis service endpoint checks, plus the Argo CD, Traefik, and
Woodpecker namespace, backend, ingress, generated Woodpecker secret, HA
replica, runtime image tag, and ClusterIP service-path checks, while skipping
Harbor, monitoring, Loki, Velero, CloudNativePG, Longhorn runtime, and
StorageClass enforcement. It also sets
`PLATFORM_APP_HEALTH_INCLUDE_EXISTING_APPS=false`, so unrelated existing Argo
CD Applications do not block this focused repair check.

If Woodpecker remains `Synced` but `Progressing`, or agents still show an old
`next-*` image after you pushed corrected values, run the focused repair. It
hard-refreshes and syncs the Woodpecker Argo CD application first, waits for
the server and agents, verifies the running server and agent image tags,
refreshes service-path consumers so agents are not blocked by stale ClusterIP
routing, and then runs the focused CI health gate:

```bash
make platform-woodpecker-repair
```

Before final production registration, also prove the selected GitOps profile is
fully rendered and has no unresolved placeholders:

```bash
PLATFORM_PROFILE=premium-3node make platform-profile-check
```

If this fails, repair the service and ingress paths, then run it again:

```bash
make platform-dns-repair
make platform-argocd-service-repair
make platform-ingress
make platform-app-health
```

For a final read-only readiness proof, run the combined production gate:

```bash
make platform-production-check
```

It runs repository validation, RKE2 verification, the platform status report,
the selected GitOps profile placeholder check, and the platform app health gate
in one command. Repository validation also rejects mutable explicit image or
chart tags such as `latest`, `next`, `nightly`, `dev`, or branch-style tags in
curated GitOps app manifests.
Use `docs/PRODUCTION_READINESS.md` as the final go/no-go checklist that ties
this live gate to restore proof, access review, exceptions, release evidence,
and post-launch validation.

To prove the cluster is using the intended private source repository, run the
production gate with the same repository URL used to register the Argo CD
Applications:

```bash
PLATFORM_REPO_URL=<PRIVATE_REPO_URL> make platform-production-check
```

`platform-app-health` requires the controller/client path through the app VIP,
verifies that Argo CD Applications use production-safe repository sources
instead of temporary seed Git or insecure `git://` URLs and match
`PLATFORM_APP_HEALTH_EXPECTED_REPO_URL` / `PLATFORM_REPO_URL` when one is set,
verifies that
configured GUI HTTP routes redirect to HTTPS, checks that GUI hosts have an
Ingress/IngressRoute with ready backend endpoints, verifies the premium
Longhorn StorageClasses by default, verifies Longhorn node and volume runtime
health when Longhorn is part of the required app set, verifies critical HA
replica coverage for Argo CD HA, Traefik, and Woodpecker when those apps are
required, verifies Woodpecker server and agent pods are running the expected
pinned image tag, verifies Argo CD server, repo-server, application-controller,
and configured repo-server/Redis service endpoints, and checks Argo CD /
Woodpecker ClusterIP paths from every RKE2 node host and from diagnostic pods
pinned to every RKE2 node. It also fails if
platform PVCs are Pending, Lost, or stuck Terminating. To require a specific
CloudNativePG PostgreSQL cluster, pass it as `namespace/name`:

```bash
PLATFORM_APP_HEALTH_CNPG_CLUSTERS="platform-databases/platform-postgres" make platform-app-health
```

Certificate and trust checks default to `auto`: any existing cert-manager
`Certificate` resources must be `Ready`, and any existing trust-manager
`Bundle` resources must be synced. Controller-only bootstrap installs with no
private certificates yet are allowed. To require exact resources:

```bash
PLATFORM_APP_HEALTH_CERTIFICATES="argocd/argocd-server-tls" make platform-app-health
PLATFORM_APP_HEALTH_TRUST_BUNDLES="platform-public-roots" make platform-app-health
```

To deploy a pre-issued wildcard certificate, create the same TLS certificate in
each application namespace using the secret name expected by that ingress. Keep
the certificate and private key in an ignored local path, not Git:

```bash
CERT=/secure/path/wildcard.crt
KEY=/secure/path/wildcard.key
K=/var/lib/rancher/rke2/bin/kubectl
C=/etc/rancher/rke2/rke2.yaml

for item in \
  argocd/argocd-server-tls \
  forgejo/forgejo-tls \
  woodpecker/woodpecker-tls \
  monitoring/grafana-tls \
  monitoring/prometheus-tls \
  logging/loki-tls
do
  ns="${item%/*}"
  secret="${item#*/}"
  "$K" --kubeconfig "$C" create namespace "$ns" --dry-run=client -o yaml | "$K" --kubeconfig "$C" apply -f -
  "$K" --kubeconfig "$C" -n "$ns" create secret tls "$secret" \
    --cert="$CERT" \
    --key="$KEY" \
    --dry-run=client -o yaml | "$K" --kubeconfig "$C" apply -f -
done
```

For Harbor, set `HARBOR_TLS_CERT_SOURCE=secret` and
`HARBOR_TLS_SECRET_NAME=harbor-tls` before rendering Harbor values, then create
`secret/harbor-tls` in the `harbor` namespace with the same `kubectl create
secret tls` pattern.

When step-ca is required, the health gate probes its in-cluster HTTPS
`/health` endpoint through the ClusterIP service. To skip that during a
temporary debug run:

```bash
PLATFORM_APP_HEALTH_STEP_CA_API=false make platform-app-health
```

When Harbor is part of `PLATFORM_APP_HEALTH_GUI_APPS`, the same gate also
checks the container registry API at `https://<registry-host>/v2/` through the
app VIP and requires the Docker Distribution API header. To skip that during a
temporary debug run:

```bash
PLATFORM_APP_HEALTH_REGISTRY_API=false make platform-app-health
```

When Grafana or Prometheus are part of `PLATFORM_APP_HEALTH_GUI_APPS`, the gate
also checks Grafana `/api/health` and Prometheus `/-/ready` through the app VIP.
To skip those monitoring API probes during a temporary debug run:

```bash
PLATFORM_APP_HEALTH_MONITORING_API=false make platform-app-health
```

For logging and backups, the health gate verifies a known Loki `/ready` service
endpoint when Loki is required, and requires Velero `BackupStorageLocation`
objects to be `Available` plus at least one enabled Velero backup schedule when
Velero is required. It also verifies the generated app secret contracts for
Harbor, Forgejo, Woodpecker, Keycloak, Grafana, Loki, Velero, and CloudNativePG when those apps are required, checking that
the expected Secret objects exist with the required keys. Temporary bypasses:

```bash
PLATFORM_APP_HEALTH_LOKI_API=false make platform-app-health
PLATFORM_APP_HEALTH_VELERO_BACKUP_STORAGE=false make platform-app-health
PLATFORM_APP_HEALTH_VELERO_SCHEDULES=false make platform-app-health
PLATFORM_APP_HEALTH_APP_SECRETS=skip make platform-app-health
```

For production Harbor, also enforce the external PostgreSQL, Redis, and
registry S3 credential secrets:

```bash
PLATFORM_APP_HEALTH_HARBOR_PRODUCTION_SECRETS=true make platform-app-health
```

For production Forgejo, also enforce the SQL password secret and, when
`FORGEJO_REDIS_MODE=redis`, the Redis URI secret:

```bash
PLATFORM_APP_HEALTH_FORGEJO_PRODUCTION_SECRETS=true make platform-app-health
```

For production Grafana with external PostgreSQL, also enforce the database
password secret:

```bash
PLATFORM_APP_HEALTH_GRAFANA_DATABASE_SECRET=true make platform-app-health
```

For custom secret names, set the same names used by `platform-app-secrets` and
the private values renderer:

```bash
HARBOR_ADMIN_SECRET_NAME=harbor-admin \
HARBOR_SECRET_KEY_SECRET_NAME=harbor-secret-key \
HARBOR_DATABASE_SECRET_NAME=harbor-database \
HARBOR_REDIS_SECRET_NAME=harbor-redis \
HARBOR_S3_SECRET_NAME=harbor-registry-s3 \
FORGEJO_DATABASE_SECRET_NAME=forgejo-database \
FORGEJO_REDIS_SECRET_NAME=forgejo-redis \
WOODPECKER_FORGEJO_OAUTH_SECRET_NAME=woodpecker-forgejo-oauth \
WOODPECKER_DATABASE_SECRET_NAME=woodpecker-database \
GRAFANA_ADMIN_SECRET_NAME=grafana-admin \
GRAFANA_DATABASE_SECRET_NAME=grafana-database \
LOKI_OBJECT_STORAGE_SECRET_NAME=loki-object-storage \
VELERO_CREDENTIALS_SECRET_NAME=velero-credentials \
CNPG_OBJECT_STORE_SECRET_NAME=cnpg-object-store \
PLATFORM_APP_HEALTH_CNPG_OBJECT_STORAGE_SECRET=true \
make platform-app-health
```

Temporary certificate/trust bypasses:

```bash
PLATFORM_APP_HEALTH_CERTIFICATES=skip make platform-app-health
PLATFORM_APP_HEALTH_TRUST_BUNDLES=skip make platform-app-health
```

If Argo CD, Woodpecker, or CoreDNS service checks report node-specific
ClusterIP timeouts, run the service-path repair alias and then rerun the health
gate. The alias repairs DNS/CNI service routing, refreshes Woodpecker agents,
and verifies the Woodpecker gRPC ClusterIP from every RKE2 node host and from
diagnostic pods pinned to every RKE2 node:

```bash
make platform-service-path-repair
make platform-app-health
```

RKE2 node-originated app VIP self-probes are advisory by default because MetalLB
L2 self-access can differ from real client access. To enforce those too:

```bash
PLATFORM_APP_HEALTH_NODE_INGRESS_STRICT=true make platform-app-health
```

The pod-pinned service-path probe uses `rancher/klipper-helm:v0.10.0-build20260513`
by default because the bootstrap flow already pulls it for DNS diagnostics. For
restricted registries or slow pulls, override the image or timeout:

```bash
PLATFORM_APP_HEALTH_SERVICE_CHECK_IMAGE=<internal-image-with-curl-or-wget> \
PLATFORM_APP_HEALTH_SERVICE_CHECK_TIMEOUT=300 \
make platform-app-health
```

To skip required StorageClass enforcement during a temporary non-Longhorn subset
debug run:

```bash
PLATFORM_APP_HEALTH_STORAGE_CLASSES=skip make platform-app-health
```

To skip only Longhorn runtime node/volume enforcement during a temporary storage
repair run:

```bash
PLATFORM_APP_HEALTH_LONGHORN_RUNTIME=false make platform-app-health
```

To skip only Argo CD runtime component and configured repo-server/Redis service
endpoint enforcement during a temporary control-plane repair run:

```bash
PLATFORM_APP_HEALTH_ARGOCD_RUNTIME=false make platform-app-health
```

To skip only critical HA replica enforcement during a temporary scale or repair
run:

```bash
PLATFORM_APP_HEALTH_HA_REPLICAS=false make platform-app-health
```

After an intentional Woodpecker upgrade, set the expected running image tag for
the health gate:

```bash
PLATFORM_APP_HEALTH_WOODPECKER_IMAGE_TAG=v3.16.0 make platform-app-health
```

To skip only HTTP-to-HTTPS redirect enforcement during a temporary debug run:

```bash
PLATFORM_APP_HEALTH_HTTP_REDIRECT=false make platform-app-health
```

If you intentionally run a subset profile, keep the app, namespace, and GUI
route lists aligned so the gate checks only the services that should exist:

```bash
PLATFORM_APP_HEALTH_REQUIRED_APPS="cert-manager trust-manager metallb traefik longhorn cloudnativepg platform-postgres platform-valkey forgejo woodpecker" \
PLATFORM_APP_HEALTH_NAMESPACES="argocd cert-manager cnpg-system platform-databases platform-cache forgejo woodpecker longhorn-system metallb-system traefik" \
PLATFORM_APP_HEALTH_GUI_APPS="argocd forgejo woodpecker" \
make platform-app-health
```
