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

Cluster targets automatically run an inventory preflight. To check the local
file without contacting any node, run:

```bash
make platform-inventory-preflight
```

This removes a UTF-8 BOM, normalizes line endings, and asks Ansible to parse
the inventory before a playbook starts. It requires exactly three
`rke2_servers` hosts with real `ansible_host` values. It deliberately does not
invent private IP addresses, SSH users, or credentials.

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

`make rke2-install` runs preflight, node preparation, and registry egress checks before installing RKE2. On Rocky Linux 10 and other RHEL 10-compatible nodes, preparation installs `kernel-modules-extra`, loads Kubernetes/CNI kernel modules, disables swap, applies Kubernetes sysctls, disables reverse-path filtering for CNI traffic on all active interfaces, opens required firewalld ports including Cilium VXLAN/Geneve overlay ports and supported alternate VXLAN port `8223/udp`, trusts the RKE2 pod CIDR, RKE2 node IPs, and stable Cilium interfaces in firewalld, installs direct firewalld ACCEPT rules for pod CIDR and CNI interface forwarding, and configures NetworkManager to ignore CNI interfaces.

To check image registry egress without reinstalling:

```bash
make rke2-registry-check
```

If your enterprise network uses a private registry mirror or airgap image flow, set `rke2_registry_check_urls` to the mirror endpoints, or disable the public registry check only after the mirror is configured:

```bash
RKE2_REGISTRY_CHECK_ENABLED=false make rke2-install
```

If internet access requires an HTTP proxy, set `rke2_http_proxy`, `rke2_https_proxy`, and `rke2_no_proxy` in ignored local inventory, or export `RKE2_HTTP_PROXY`, `RKE2_HTTPS_PROXY`, and `RKE2_NO_PROXY` before running `make rke2-install`. The install playbook writes `/etc/default/rke2-server` for the RKE2 systemd service.

When `PLATFORM_PRODUCTION_STRICT=true`, the install also enables Kubernetes API
audit logging with a metadata-only policy at
`/etc/rancher/rke2/audit-policy.yaml`. Audit logs are retained under
`/var/lib/rancher/rke2/server/logs/audit.log` with a 30-day/10-file/100-MiB
rotation default. Adjust `RKE2_AUDIT_LOG_MAXAGE`, `RKE2_AUDIT_LOG_MAXBACKUP`,
and `RKE2_AUDIT_LOG_MAXSIZE` in the ignored deployment environment. Metadata
logging intentionally avoids retaining request bodies such as Secret content.

For `PLATFORM_PRODUCTION_STRICT=true`, use an immutable RKE2 version and an
approved installer digest. Set `RKE2_VERSION` to the reviewed release and
`RKE2_INSTALL_SCRIPT_SHA256` to the 64-character SHA-256 recorded by your
change review or internal mirror. The installer refuses the moving `stable`
channel and an unchecked bootstrap script in strict mode. Keep those values in
the ignored private deployment environment and update them only through the
documented upgrade process.

RKE2 encrypts Kubernetes Secret data at rest. Every `make rke2-install` and
`make rke2-verify` run now checks `rke2 secrets-encrypt status` and requires
both `Encryption Status: Enabled` and matching encryption hashes across the HA
servers. Leave `RKE2_VERIFY_SECRETS_ENCRYPTION=true` in production. Set it to
`false` only during a documented, time-bounded legacy migration; restore the
gate immediately afterwards. Plan any encryption-key rotation with a current
etcd snapshot and the RKE2 rotation procedure.

Supply-chain promotion is also deliberate. Install Trivy, Gitleaks, Semgrep,
Syft, OpenSSF Scorecard, Cosign, Kustomize `v5.8.1`, Helm `v3.21.0`, and
Kubeconform `v0.7.0` on the promotion runner. Run
`scripts/bootstrap/install-kyverno-cli.sh <PRIVATE_TOOL_DIRECTORY>` to install
the checksum-pinned Kyverno CLI `v1.18.1` through its bounded, safely extracted,
atomic installer, or set `KYVERNO_BIN` to an equivalently verified binary.
Configure the digest-only private image inventory described in
`docs/SUPPLY_CHAIN.md`, then confirm both `make rendered-schema-verify` and
`make policy-cel-verify` pass before running `platform-production-check`.

Kyverno policy promotion is deliberate. Keep `PLATFORM_POLICY_ENFORCEMENT=Audit`
while remediating report findings. Run `make platform-policy-readiness` to show
violations from the three managed stable CEL policies. Only after it reports zero
violations should you set `PLATFORM_POLICY_ENFORCEMENT=Enforce`, rerender private
values, sync GitOps, and rerun the same target. In Enforce mode the readiness
target fails rather than allowing a policy mode change with known violations.
The renderer maps that operator-facing `Enforce` value to Kyverno's stable
`validationActions: [Deny]`; `Audit` remains `Audit`.

When upgrading an existing deployment, the `platform-policies` Application will
show the superseded `kyverno.io/v1` ClusterPolicies as resources to prune. Review
the diff and approve that guarded prune after the replacement
`policies.kyverno.io/v1` ValidatingPolicies are Ready. The readiness gate fails
while either legacy object remains, preventing duplicate admission behavior.

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
RKE2_VERSION='v1.36.2+rke2r1' make rke2-install
```

or Ansible extra vars:

```bash
ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/install-rke2.yml \
  -e rke2_version='v1.36.2+rke2r1'
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
sudo RKE2_TOKEN=<TOKEN> RKE2_API_ENDPOINT=<VIP_DNS_NAME> RKE2_VERSION=<RKE2_VERSION> RKE2_INSTALL_SCRIPT_SHA256=<REVIEWED_INSTALLER_SHA256> scripts/bootstrap/install-rke2-first-server.sh
sudo RKE2_TOKEN=<TOKEN> RKE2_API_ENDPOINT=<VIP_DNS_NAME> RKE2_VERSION=<RKE2_VERSION> RKE2_INSTALL_SCRIPT_SHA256=<REVIEWED_INSTALLER_SHA256> scripts/bootstrap/install-rke2-server.sh
```

Manual bootstrap scripts always require an exact RKE2 release and a reviewed
SHA-256 for the installer returned by `https://get.rke2.io`, including outside
strict production mode. They permit only TLS 1.2 HTTPS redirects, cap the
installer at 2 MiB, verify its regular-file shape, byte count, and digest before
changing the node configuration or executing it, and require a bounded
`timeout` implementation rather than falling back to unbounded execution. They
also reject conflicting installer channel/type overrides and write
`/etc/rancher/rke2/config.yaml` atomically with mode `0600`. Never store the
real token in git. The installer digest is not secret, but treat it as a
reviewed release input and obtain it through your approved release or internal
mirror process rather than trusting the download being verified.

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

The bootstrap release is derived from the vendored `argo-cd` chart. Its core
and HA install manifests use exact release URLs and reviewed SHA-256 values;
the playbook rejects redirects, bounds each download, verifies it before
server-side apply, and does not accept a runtime manifest URL override. An
Argo CD upgrade therefore requires the vendored chart and both reviewed
bootstrap-manifest digests to move together.

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

This production profile requires maintained external S3-compatible object
storage and does not register the archived MinIO server. For isolated lab or
migration testing only, use `PLATFORM_PROFILE=premium-3node-lab`. Before
changing an existing MinIO-backed deployment to the production profile,
migrate and restore-test its objects; Argo CD requires explicit prune approval
before removing the old application.

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
FORGEJO_DATABASE_SSL_MODE=verify-full \
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
The renderer defaults to the maintained upstream
`quay.io/keycloak/keycloak:26.7.0` image and
`quay.io/adorsys/keycloak-config-cli:6.5.1` for declarative realm import. Pin
intentional upgrades with `KEYCLOAK_IMAGE_TAG` and
`KEYCLOAK_CONFIG_CLI_IMAGE_TAG`; floating development tags are rejected.

### Central SSO and monitoring access

The production profile enables Keycloak-backed SSO by default. During
`make platform-app-secrets`, it creates a private Keycloak realm import secret,
an Argo CD OIDC secret, a Grafana OIDC secret, and an OAuth2 Proxy secret for
Prometheus. None of their values are committed to Git.

The bootstrap account is `platform-admin`; its generated password is available
only from the Kubernetes secret and the user must enroll TOTP on first sign-in:

```bash
kubectl -n keycloak get secret platform-sso-clients \
  -o go-template='{{ index .data "PLATFORM_SSO_BOOTSTRAP_ADMIN_PASSWORD" }}' | base64 -d; echo
```

Grafana redirects users to Keycloak and its local password form is disabled.
Prometheus is not exposed directly: unauthenticated traffic is routed through
OAuth2 Proxy and must be redirected to Keycloak or rejected. Keep
`PLATFORM_SSO_ENABLED=true` for production. Turning it off is a temporary
break-glass compatibility option only and must be accompanied by an explicit
review of Argo CD, Grafana, and Prometheus public access.

Woodpecker defaults to PostgreSQL-backed HA for the 3-node premium profile.
`platform-app-secrets` generates `woodpecker/woodpecker-database` and the
matching `platform-databases/woodpecker-database` CloudNativePG role password
secret unless you provide `WOODPECKER_DATABASE_PASSWORD` or a full
`WOODPECKER_DATABASE_DATASOURCE`. The renderer pins both Woodpecker server and
agent image repositories plus `WOODPECKER_IMAGE_TAG`, defaulting to `v3.16.0`;
change that only as an intentional upgrade. It also disables the chart's
render-time random agent token and maps both server and agents to the preserved
`woodpecker/woodpecker-agent-secret` generated by `platform-app-secrets`.
Override only the Secret name with `WOODPECKER_AGENT_SECRET_NAME`; optionally
provide `WOODPECKER_AGENT_SECRET` through the ignored private environment.
Registration remains closed by default. Set `WOODPECKER_ADMIN_USERS` to the
exact Forgejo login(s) that must administer Woodpecker. For first-time OAuth
onboarding, set `WOODPECKER_OPEN=true` in the ignored private environment,
render and sync the private values, let approved Forgejo users sign in once,
then set it back to `false` and render/sync again.
Render with 3 server replicas for the shared `platform-postgres` cluster:

```bash
WOODPECKER_DATABASE_MODE=postgres \
WOODPECKER_DATABASE_SECRET_NAME=woodpecker-database \
WOODPECKER_AGENT_SECRET_NAME=woodpecker-agent-secret \
WOODPECKER_DATABASE_HOST=platform-postgres-rw.platform-databases.svc.cluster.local:5432 \
WOODPECKER_DATABASE_NAME=woodpecker \
WOODPECKER_DATABASE_USER=woodpecker \
WOODPECKER_DATABASE_SSLMODE=verify-full \
WOODPECKER_DATABASE_SSLROOTCERT=/etc/ssl/platform-postgres/ca-certificates.crt \
PLATFORM_APP_SECRET_REQUIRE_WOODPECKER_DATABASE=true \
make platform-app-secrets

WOODPECKER_DATABASE_MODE=postgres \
WOODPECKER_DATABASE_SECRET_NAME=woodpecker-database \
WOODPECKER_AGENT_SECRET_NAME=woodpecker-agent-secret \
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
HARBOR_DATABASE_SSLMODE=verify-full \
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
GRAFANA_DATABASE_SSL_MODE=verify-full \
make platform-render-private-values
```

The unattended targets also default to `PLATFORM_RUN_PROFILE_CHECK=true`, so
the selected GitOps registration mode is validated before any commit, push, or
seed mirror update. In `strict` mode the full rendered profile must be complete.
In the default `skip-incomplete` mode, the bootstrap validates that the
deployable Application subset can be rendered before Argo CD receives it. Both
modes also verify selected Kustomization resources, components, patch files,
Helm values, vendored chart metadata, and premium internal-TLS support files;
skip-incomplete permits unresolved template placeholders only, not missing
application-tree files.
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
Also set `ALERTMANAGER_WEBHOOK_URL` or provide `ALERTMANAGER_CONFIG`. Production
strict mode rejects a null alert route, while Loki gateway and Grafana client
credentials are generated and preserved automatically. See
`docs/OBSERVABILITY.md` for retention, collection, and delivery proof.
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
Set `PLATFORM_APP_SECRET_REQUIRE_FORGEJO_OBJECT_STORAGE=true` for production
Forgejo and provide `FORGEJO_S3_ACCESS_KEY_ID` plus
`FORGEJO_S3_SECRET_ACCESS_KEY`; strict rendering stores attachments, LFS,
avatars, and packages in HTTPS S3-compatible storage instead of the RWO
filesystem.
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

When `make platform-app-health` loads `private/seed-git.env` or
`private/first-deploy.env`, it automatically uses bootstrap mode. That mode
accepts the temporary seed source and a verified `initialized=false`,
`sealed=true` OpenBao server while its private initialization ceremony is still
pending. `make platform-production-check` always forces production mode and
rejects both states. Migrate the platform repository into the intended private
Git service, rerun `PLATFORM_REPO_URL=<PRIVATE_REPO_URL>
PLATFORM_APPLY_GITOPS=true make platform-argocd`, initialize and unseal OpenBao,
then remove seed Git. Use `PLATFORM_APP_HEALTH_MODE=production make
platform-app-health` to invoke the same strict app gate directly. For a
one-off bootstrap probe without an env file, set
`PLATFORM_APP_HEALTH_FORBID_TEMPORARY_REPO=false` and
`PLATFORM_APP_HEALTH_OPENBAO_READY=false` explicitly.

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

`make platform-ingress` verifies the reviewed MetalLB `0.16.1` and Traefik
`41.2.0` archives committed beside their vendored chart source, then embeds the
base64 payloads in RKE2 HelmChart `chartContent`. It does not download a chart
index or accept a runtime chart-repository override. A chart update requires a
reviewed source/archive change and matching SHA-256 contract update.

External DNS repair is therefore not a prerequisite for chart installation.
Run it explicitly when image pulls or another in-cluster external request shows
resolver or service-path failures. For IPv4-only environments it suppresses
external AAAA answers by default; disable that only if the cluster has working
IPv6 egress:

```bash
PLATFORM_DNS_IPV4_ONLY=false make platform-dns-repair
```

The older per-node Traefik repository probe remains available as an explicit
network diagnostic, but it is disabled during normal ingress installation:

```bash
make platform-dns-repair-traefik
PLATFORM_TRAEFIK_CHART_REPO_DNS_CHECK=true make platform-ingress
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
StorageClass enforcement. It loads the ignored deployment environment through
the normal health runner, but does not require unrelated platform SSO secrets.
When no Argo CD or Woodpecker host was explicitly configured, this focused gate
accepts the single host on the exact live Argo CD and Woodpecker Ingress names.
Ambiguous or malformed live routes are never selected. The full
`platform-app-health` gate remains strict and does not enable this discovery.
The focused gate also sets
`PLATFORM_APP_HEALTH_INCLUDE_EXISTING_APPS=false`, so unrelated existing Argo
CD Applications do not block this focused repair check.

The premium Argo CD profile intentionally uses headless repo-server and Redis
HAProxy Services. `platform-status` and the health gates treat those Services as
ready when they have ready EndpointSlice addresses; `clusterIP: None` is not a
failure for these internal DNS-discovered Services. Redis HAProxy also uses a
zero-surge rolling strategy so its required three-node anti-affinity cannot
leave a fourth rollout pod permanently Pending.

If Woodpecker remains `Synced` but `Progressing`, or agents still show an old
`next-*` image after you pushed corrected values, run the focused repair. It
reconciles the focused secrets and private seed source before Argo CD service
repair, then syncs the Woodpecker application, waits for the server and agents,
verifies the running image tags, refreshes service-path consumers so agents are
not blocked by stale ClusterIP routing, and runs the focused CI health gate:

```bash
make platform-woodpecker-repair
```

This focused repair reconciles only Woodpecker's agent, database, and Forgejo
OAuth secrets. Run `make platform-app-secrets` separately when validating the
complete production secret posture, including Harbor S3 and backup credentials.
Its Argo CD service repair also restores approval-gated automated pruning on the
applications it refreshes without replacing their existing sync options. Set
`PLATFORM_ARGOCD_SERVICE_REPAIR_GUARDED_PRUNE=false` only for an intentional,
temporary diagnostic run.

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

It runs repository validation, supply-chain evidence, RKE2 verification, the
platform status report, the selected GitOps profile placeholder check, live
capacity, network-isolation, internal-TLS, observability, and platform app
health checks, plus off-cluster backup freshness and restore-evidence
validation in one command. Set `PLATFORM_RESTORE_EVIDENCE_FILE` to a completed
schema-v2 private record based on `examples/restore-evidence.example.json` and
set `PLATFORM_FORGEJO_RECOVERY_EVIDENCE_FILE` to the record created by the
approved `make platform-forgejo-recovery-drill`. The production gate refuses
to pass without recent, independently approved, hash-bound restore/failover/
failback proof from a separate failure domain or without Forgejo recovery
proof for the exact tested commit. Repository validation also rejects mutable explicit image or
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

In production mode, `platform-app-health` requires the controller/client path
through the app VIP, verifies that Argo CD Applications use production-safe repository sources
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

To deploy a pre-issued wildcard certificate, keep the certificate and private
key in ignored local paths, then let the TLS target validate the matching key,
minimum remaining validity, hostname coverage, and complete system-trusted
issuer chain before it creates namespace-local Secrets. A PEM full chain is
accepted directly. When the input contains only the leaf certificate, the
target follows the certificate's HTTP(S) CA Issuers AIA path with bounded
downloads and accepts each intermediate only after its signature and CA
constraints are verified. Disconnected environments should provide the leaf
and intermediates in the certificate file. Neither input is committed and the
temporary server copy is removed automatically:

```bash
PLATFORM_WILDCARD_TLS_CERT_FILE=/secure/path/wildcard.crt \
PLATFORM_WILDCARD_TLS_KEY_FILE=/secure/path/wildcard.key \
PLATFORM_WILDCARD_TLS_MIN_VALIDITY_DAYS=30 \
make platform-tls
```

The target updates `argocd-server-tls`, `forgejo-tls`, `woodpecker-tls`,
`harbor-tls`, `keycloak-tls`, `grafana-tls`, `prometheus-tls`, and `loki-tls`
in their respective namespaces. Set `HARBOR_TLS_CERT_SOURCE=secret` and
`HARBOR_TLS_SECRET_NAME=harbor-tls` before rendering Harbor values so it uses
the same managed wildcard Secret.

`make platform-tls-verify` verifies both the Secret contents and every chain
served by the ingress VIP against the node's system trust store. A leaf-only
Secret therefore fails before applications such as Woodpecker encounter an
`x509: certificate signed by unknown authority` error during OAuth.

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
FORGEJO_S3_SECRET_NAME=forgejo-object-storage \
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
PLATFORM_APP_HEALTH_SERVICE_CHECK_CREATE_ATTEMPTS=5 \
make platform-app-health
```

Probe Job creation is retried automatically. If admission or API availability
still prevents creation, the health report includes each apply error and recent
Job events instead of reporting only a missing probe pod count.

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
