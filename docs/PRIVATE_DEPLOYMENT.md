# Private Organization Deployment

This project can remain a public 0BSD-licensed template while each organization
installation stays private.

## Recommended Model

Use two repositories:

```text
Public repo:
  https://github.com/<owner>/platform-gitops
  Purpose: open source template, examples, automation, docs
  Contains: placeholders only

Private deployment repo:
  https://<PRIVATE_GIT_HOST>/<ORG>/platform-gitops-deploy.git
  Purpose: real organization cluster desired state
  Contains: real internal FQDNs, safe non-secret values, encrypted secrets
```

The private deployment repo is the source Argo CD should read for the real
organization cluster.

## Bootstrap Flow

Forgejo cannot be the first Git source until Forgejo is running. Use a
temporary private Git source first, then move the source of truth inside
Forgejo.

1. Keep this repository public with placeholders.
2. Create a private deployment repository from this template.
3. Put organization FQDNs and safe deployment values in the private repo.
4. Store credentials with SOPS, External Secrets, Vault/OpenBao, Sealed
   Secrets, or another private secret flow.
5. Bootstrap Argo CD with the private repo URL:

```bash
PLATFORM_PROFILE=premium-3node \
PLATFORM_REPO_URL=https://<PRIVATE_GIT_HOST>/<ORG>/platform-gitops-deploy.git \
PLATFORM_APPLY_GITOPS=true \
make platform-argocd
```

6. Let Argo CD deploy Forgejo at the organization hostname.
7. Create the long-term GitOps repo in Forgejo.
8. Mirror or push the private deployment repo into Forgejo.
9. Re-register or update Argo CD Applications so their `repoURL` points to
   Forgejo.

After that, the long-term source can be:

```text
https://<GIT_FQDN>/<ORG>/platform-gitops.git
```

## Easier First Deployment

Use `platform-first-deploy` when the cluster already has RKE2, the API VIP,
and a reachable app VIP. This target bootstraps Argo CD, registers private Git
credentials when supplied, registers the platform applications, configures the
app ingress VIP, and prints the access summary.

For a private GitHub, GitLab, or internal Git repository:

```bash
export PLATFORM_REPO_URL=https://<PRIVATE_GIT_HOST>/<ORG>/platform-gitops-deploy.git
export PLATFORM_REPO_USERNAME=<GIT_USERNAME>
read -rsp "Private Git token/password: " PLATFORM_REPO_TOKEN
echo
export PLATFORM_REPO_TOKEN

make platform-first-deploy
```

For a public read-only repository, omit `PLATFORM_REPO_USERNAME` and
`PLATFORM_REPO_TOKEN`.

By default, first deployment uses
`PLATFORM_GITOPS_PLACEHOLDER_MODE=skip-incomplete`. It registers deployable
applications and prints skipped applications that still contain unresolved
placeholders such as storage sizes, database endpoints, Redis endpoints, object
storage, backup targets, or TLS secret references.

Set `PLATFORM_GITOPS_PLACEHOLDER_MODE=strict` when all private values are
resolved and you want the deployment to fail before registering anything if a
placeholder remains.

First deployment also runs the platform DNS/ClusterIP service-path repair before
waiting on Argo CD. Leave `PLATFORM_FIRST_DEPLOY_DNS_REPAIR=true` for new
clusters. Set it to `false` only when pod-to-service networking is already
known healthy.

## Fully Non-Interactive First Deployment

For unattended bootstrap, put all first-deploy settings in the ignored file
`private/first-deploy.env`:

```bash
cp config/first-deploy.env.example private/first-deploy.env
${EDITOR:-vi} private/first-deploy.env
```

Then run:

```bash
make platform-first-deploy-auto
```

The automated target:

```text
Loads private/first-deploy.env
Renders first-deploy private values for Forgejo, Argo CD, Woodpecker, Keycloak,
Harbor, monitoring, Loki, Velero, Longhorn, and optional step-ca when enabled
Validates the repository
Validates the selected rendered GitOps profile
Optionally commits current changes when PLATFORM_AUTO_COMMIT=true
Pushes HEAD to PLATFORM_REPO_URL
Registers private Git credentials in Argo CD when PLATFORM_REPO_TOKEN is set
Runs platform-first-deploy
```

The Git token is used as a one-command Git HTTP authorization header during
push and is not written into the Git remote URL. Argo CD receives the same
token as a Kubernetes repository Secret before application registration when a
private repository token is supplied.

During first deployment, Argo CD bootstrap is retried once after automatic
DNS/ClusterIP service-path repair and Argo CD internal repo-server/Redis service
repair when the controller cannot reach services such as `argocd-redis` or
`argocd-repo-server`. Set `PLATFORM_FIRST_DEPLOY_ARGOCD_REPAIR_RETRY=false`
to disable that retry. The failure detector waits
`PLATFORM_ARGOCD_SERVICE_PATH_FAST_FAIL_AFTER=90` seconds before fast-failing on
repeated Redis or repo-server ClusterIP timeouts so fresh Argo CD pods can warm
up normally.

By default, `PLATFORM_AUTO_RENDER_PRIVATE_VALUES=true` renders a bootstrap
platform profile and a Longhorn backup target value before validation and push.
Forgejo hostname is inferred from `platform_forgejo_host` or
`platform_git_host` in `inventory/hosts.local.ini`; set
`PLATFORM_FORGEJO_HOST=<GIT_FQDN>` to override it.
`PLATFORM_RUN_PROFILE_CHECK=true` validates the selected GitOps registration
mode before commit or push. With
`PLATFORM_GITOPS_PLACEHOLDER_MODE=strict`, it runs the full selected profile
through `scripts/check_gitops_profile.py`. With the default
`skip-incomplete`, it renders and validates the deployable Application subset
that Argo CD will receive, while still allowing optional unresolved apps to be
skipped during first bootstrap. Disable it only for a temporary local debug run.
`PLATFORM_RUN_NO_SECRETS=true` also runs the safety scanner. First private
deploy and first seed deploy default
`PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES=true` so real internal FQDNs can
live in the private deployment repo, while plaintext secrets, private keys,
kubeconfigs, and private IPs are still blocked. Leave that allowance unset or
false for any workflow that might push rendered values back to a public source
remote, such as `make platform-seed-git-sync` with a public `origin`.
Set `PYTHON=/path/to/python` in the env file if the bootstrap workstation does
not expose `python3`; `make platform-argocd`, validation, rendering, and
selected GitOps profile checks use the same interpreter.

The private values renderer pins both Woodpecker server and agent images with
`WOODPECKER_IMAGE_TAG`, defaulting to `v3.16.0`. Treat changes to that value as
an intentional Woodpecker upgrade: render, validate, sync, then prove the
Woodpecker server and agents are healthy.

Object-storage backed apps and external app databases are rendered with bucket
names, endpoints, regions, cache sizes, database hosts, and Kubernetes secret
names only. Runtime credentials stay outside Git. `make platform-app-secrets`
can create shared platform Valkey auth, Grafana admin credentials, Grafana's
external PostgreSQL password, Forgejo's external PostgreSQL password and Redis
URI, Woodpecker's PostgreSQL datasource secret, Keycloak admin/database
secrets, Harbor's external
database/Redis/S3 secrets, Forgejo object-storage credentials, plus Loki,
Velero, and CloudNativePG object-storage secrets from ignored env values such
as `FORGEJO_DATABASE_PASSWORD`, `FORGEJO_REDIS_URL`,
`FORGEJO_S3_ACCESS_KEY_ID`, `FORGEJO_S3_SECRET_ACCESS_KEY`,
`WOODPECKER_DATABASE_PASSWORD` or a full `WOODPECKER_DATABASE_DATASOURCE`,
`HARBOR_DATABASE_PASSWORD`,
`HARBOR_REDIS_PASSWORD`, `HARBOR_S3_ACCESS_KEY_ID`,
`HARBOR_S3_SECRET_ACCESS_KEY`, `KEYCLOAK_ADMIN_PASSWORD`,
`KEYCLOAK_DATABASE_PASSWORD`, `GRAFANA_DATABASE_PASSWORD`, `LOKI_S3_ACCESS_KEY_ID`,
`LOKI_S3_SECRET_ACCESS_KEY`, `VELERO_CLOUD_CREDENTIALS`,
`CNPG_S3_ACCESS_KEY_ID`, `CNPG_S3_SECRET_ACCESS_KEY`, or
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. Keep
`PLATFORM_APP_SECRET_REQUIRE_WOODPECKER_DATABASE=true` for the premium
Woodpecker HA default so a missing datasource secret fails before rollout. Set
`PLATFORM_APP_SECRET_REQUIRE_OBJECT_STORAGE=true` for production so missing
Loki, Velero, or CloudNativePG object-storage credential secrets fail before app sync.
Production strict mode also requires `ALERTMANAGER_WEBHOOK_URL` or a complete
`ALERTMANAGER_CONFIG`; it generates synchronized Loki gateway/Grafana client
credentials automatically. See `docs/OBSERVABILITY.md`.
Set `PLATFORM_APP_SECRET_REQUIRE_CNPG_OBJECT_STORAGE=true` when only the
CloudNativePG backup credential secret should be mandatory.
Set `PLATFORM_APP_SECRET_REQUIRE_HARBOR_DATABASE=true`,
`PLATFORM_APP_SECRET_REQUIRE_HARBOR_REDIS=true`, and
`PLATFORM_APP_SECRET_REQUIRE_HARBOR_REGISTRY_STORAGE=true` before enabling
Harbor external PostgreSQL, Redis, and S3 registry storage.
Set `PLATFORM_APP_SECRET_REQUIRE_FORGEJO_DATABASE=true` before enabling an
external Forgejo SQL backend. Set `PLATFORM_APP_SECRET_REQUIRE_FORGEJO_REDIS=true`
only when `FORGEJO_REDIS_MODE=redis`; the premium default uses shared
`platform-valkey` and can generate the `forgejo-redis` URI secret
automatically.
Set `PLATFORM_APP_SECRET_REQUIRE_FORGEJO_OBJECT_STORAGE=true` for production
Forgejo. Strict rendering requires `FORGEJO_OBJECT_STORAGE_MODE=s3`, an
HTTPS-compatible external S3 endpoint, and the managed
`forgejo/forgejo-object-storage` Secret. This keeps attachments, LFS, avatars,
and packages out of Forgejo's single RWO filesystem and makes the storage
boundary explicit before synchronization.
Set `PLATFORM_APP_SECRET_REQUIRE_GRAFANA_DATABASE=true` before enabling
`GRAFANA_DATABASE_MODE=postgres`.
Set `PLATFORM_APP_SECRET_REQUIRE_KEYCLOAK_DATABASE=true` when you want a
predefined Keycloak database password instead of generated first-deploy
credentials.

Premium first-deploy rendering defaults persistent workloads to
`longhorn-standard-encrypted` or `longhorn-critical-encrypted`.
Run `make platform-app-secrets` before any encrypted PVC is provisioned so
`longhorn-system/longhorn-crypto` exists. The task preserves an existing key,
uses `LONGHORN_ENCRYPTION_KEY` when supplied, or generates a high-entropy key
when `LONGHORN_ENCRYPTION_AUTO_GENERATE=true`. It also writes the authoritative
cluster key to the Git-ignored, mode-`0600`
`LONGHORN_ENCRYPTION_RECOVERY_FILE` (default
`private/longhorn-encryption.key`). Point that setting at an encrypted mounted
recovery system or copy the resulting file into a separate failure domain. The
global application secret rotation switch deliberately does not rotate this
key because replacing it would make existing encrypted volumes unreadable. If
the live Secret and an existing recovery file differ, automation fails closed
instead of overwriting either copy.

StorageClass changes do not encrypt an existing PVC in place. The Argo CD
Applications preserve immutable storage-class fields on existing PVCs and
StatefulSet claim templates while using encrypted classes for newly created
objects. Migrate existing data in a maintenance window by restoring or copying
it into a new encrypted PVC, validating the application, and retiring the old
PVC only after retained backup and rollback evidence exist. Production capacity
verification reports every remaining bound Longhorn PVC that still uses a
legacy plaintext class.

For production Woodpecker HA, use the shared `platform-postgres` CloudNativePG
cluster by default. `platform-app-secrets` generates the datasource and the
matching `platform-databases/woodpecker-database` role password secret unless
you provide `WOODPECKER_DATABASE_PASSWORD` or a full
`WOODPECKER_DATABASE_DATASOURCE`. It also generates the stable
`woodpecker/woodpecker-agent-secret` used by both servers and agents. Set
`WOODPECKER_AGENT_SECRET_NAME` consistently in the renderer and secret
automation when overriding its name:

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
```

The Grafana admin password is generated into
`monitoring/grafana-admin` by default. To use another Secret name, set
`GRAFANA_ADMIN_SECRET_NAME` before running `platform-render-private-values` and
`platform-app-secrets`.

For production Grafana, create the database password Secret and render values
that point Grafana at external PostgreSQL:

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

or provide `WOODPECKER_DATABASE_HOST`, `WOODPECKER_DATABASE_NAME`,
`WOODPECKER_DATABASE_USER`, and `WOODPECKER_DATABASE_PASSWORD` in
`private/first-deploy.env` or your secret manager and use the same required
flag.

For production Harbor, provide dependency credentials first:

```bash
HARBOR_DATABASE_PASSWORD='<PASSWORD>' \
HARBOR_REDIS_PASSWORD='<PASSWORD>' \
HARBOR_S3_ACCESS_KEY_ID='<ACCESS_KEY>' \
HARBOR_S3_SECRET_ACCESS_KEY='<SECRET_KEY>' \
PLATFORM_APP_SECRET_REQUIRE_HARBOR_DATABASE=true \
PLATFORM_APP_SECRET_REQUIRE_HARBOR_REDIS=true \
PLATFORM_APP_SECRET_REQUIRE_HARBOR_REGISTRY_STORAGE=true \
make platform-app-secrets
```

Then render Harbor values with external services:

```bash
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

The default `FORGEJO_DATABASE_MODE=postgres` uses the CloudNativePG service
`platform-postgres-rw.platform-databases.svc.cluster.local:5432`. Set
`FORGEJO_DATABASE_MODE=sqlite` for a lab bootstrap, or
`FORGEJO_DATABASE_MODE=mysql` / `FORGEJO_DATABASE_MODE=mariadb` with
`FORGEJO_DATABASE_HOST=<HOST>:3306` when those databases are the company
standard. Create the runtime credentials as Kubernetes Secrets, then render
only endpoints and Secret names:

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

Configure Forgejo's shared object storage before rendering. The S3 credentials
are stored only in the cluster Secret; the rendered values contain the endpoint,
bucket, region, and Secret name. Use the same external S3-compatible provider as
the other production data paths, or select a separate bucket with a least-
privilege access key:

```bash
FORGEJO_S3_ACCESS_KEY_ID='<ACCESS_KEY>' \
FORGEJO_S3_SECRET_ACCESS_KEY='<SECRET_KEY>' \
PLATFORM_APP_SECRET_REQUIRE_FORGEJO_OBJECT_STORAGE=true \
make platform-app-secrets

FORGEJO_OBJECT_STORAGE_MODE=s3 \
FORGEJO_S3_ENDPOINT=https://<S3_ENDPOINT> \
FORGEJO_S3_REGION=<S3_REGION> \
FORGEJO_S3_BUCKET=platform-forgejo \
FORGEJO_S3_SECRET_NAME=forgejo-object-storage \
FORGEJO_S3_SECURE=true \
make platform-render-private-values
```

`PLATFORM_PRODUCTION_STRICT=false` with
`FORGEJO_OBJECT_STORAGE_MODE=filesystem` is a lab-only escape hatch. Do not use
it for the premium production profile: Forgejo's RWO filesystem is not a
replica-safe shared asset store.

For Redis-backed cache and queue, the premium default is
`FORGEJO_REDIS_MODE=redis` using shared `platform-valkey`. Provide a full
`FORGEJO_REDIS_URL`, or provide `FORGEJO_REDIS_HOST`,
`FORGEJO_REDIS_PASSWORD`, and optional `FORGEJO_REDIS_PORT`,
`FORGEJO_REDIS_DB`, and `FORGEJO_REDIS_TLS` before
`make platform-app-secrets` only when overriding the shared cache. TLS defaults
to enabled and production-strict rendering rejects a plaintext override.

For an existing installation created before managed Valkey TLS, run
`make platform-app-secrets` before synchronizing `platform-valkey`, `forgejo`,
and `harbor`. The command idempotently reconciles existing Forgejo and Harbor
cache URI Secrets from `redis://` to `rediss://`; it does not print the URI or
password. Then synchronize the three applications and run
`make platform-internal-tls-verify`.

## Optional Internal PKI

The platform deploys cert-manager for certificate lifecycle and trust-manager
for distributing trust bundles. This is useful even when certificates come
from public ACME providers because workloads can consume a consistent CA bundle
from Kubernetes ConfigMaps.

step-ca is optional and should be enabled only when the organization needs an
internal CA for private certificates, mTLS, or private/offline environments.
Leave it disabled for normal public-ACME installs:

```bash
STEP_CA_MODE=disabled
```

For a first internal bootstrap, set:

```bash
STEP_CA_MODE=bootstrap
STEP_CA_NAME="Platform Internal CA"
STEP_CA_DNS_NAMES=step-ca.step-ca.svc.cluster.local,step-ca.step-ca.svc,step-ca
STEP_CA_URL=https://step-ca.step-ca.svc.cluster.local
STEP_CA_STORAGE_CLASS=longhorn-critical
STEP_CA_DB_SIZE=10Gi
```

If you expose step-ca through ingress, also set `STEP_CA_HOST=<STEP_CA_FQDN>`.
The CA remains a singleton because the upstream `step-certificates` chart only
supports one CA replica. Treat its persistent volume and generated CA material
as critical infrastructure: back it up immediately and keep CA keys/passwords
out of plaintext Git.

For long-term production PKI, prefer a private secret flow such as SOPS,
External Secrets, Vault/OpenBao, or an HSM/KMS-backed process for CA material.
The public template should contain only placeholders and safe values.

## No Previous Git Server

If no private GitHub, GitLab, Forgejo, or internal Git server exists yet, use
the temporary seed Git path:

```bash
cp config/seed-git.env.example private/seed-git.env
${EDITOR:-vi} private/seed-git.env
make platform-first-deploy-seed
```

This creates a temporary read-only Git service on the first RKE2 node, pushes
the current repository into it over SSH, and points Argo CD at:

```text
git://<NODE_1_IP_OR_DNS>:9418/platform-gitops.git
```

Use this only as a bootstrap bridge. It is intentionally simple, internal, and
read-only. Do not put plaintext secrets in the repository. After Forgejo is
deployed, create the long-term private repository in Forgejo, push or mirror
the deployment repo there, update Argo CD to the Forgejo URL, then remove the
temporary seed service:

The seed push defaults to `PLATFORM_SEED_GIT_FORCE_WITH_LEASE=true` because the
seed repository is a temporary mirror of the current private deployment state.
This lets repeat bootstrap runs update a stale seed branch without manual
pull/merge work, while still refusing the push if the remote branch changes
between the pre-push check and the update.
`make platform-seed-git-sync` does not push back to the source remote by
default. Leave `PLATFORM_SEED_SYNC_PUSH_ORIGIN=false` while `origin` points to
the public template repo; set it to `true` only after `origin` is the intended
private deployment repository.

```bash
make platform-seed-git-remove
```

## Example Private FQDN Mapping

Keep mappings like this in private DNS and private deployment config, not in
the public template:

```text
<API_FQDN>        -> API VIP
<ARGOCD_FQDN>     -> app ingress VIP
<GIT_FQDN>        -> app ingress VIP
<CI_FQDN>         -> app ingress VIP
<REGISTRY_FQDN>   -> app ingress VIP
<GRAFANA_FQDN>    -> app ingress VIP
<PROMETHEUS_FQDN> -> app ingress VIP
```

The matching private deployment values should replace:

```text
argocd.<PLATFORM_DOMAIN>      -> Argo CD hostname
forgejo.<PLATFORM_DOMAIN>     -> Forgejo hostname
woodpecker.<PLATFORM_DOMAIN>  -> Woodpecker hostname
sso.<PLATFORM_DOMAIN>         -> Keycloak SSO hostname
harbor.<PLATFORM_DOMAIN>      -> Harbor hostname
grafana.<PLATFORM_DOMAIN>     -> Grafana hostname
prometheus.<PLATFORM_DOMAIN>  -> Prometheus hostname
```

## What Stays Public

The public 0BSD repo should contain:

```text
Automation
Examples
Placeholder manifests
Validation scripts
Documentation
Default profiles without real organization values
```

Public examples may describe supply-chain evidence schemas, but they must not
contain an organization's registry inventory, vulnerability findings, ticket
identifiers, approver identities, or accepted-risk records.

## What Stays Private

The private deployment repo should contain:

```text
Real internal domains
Real cluster sizing
Real storage classes and backup targets
Real Argo CD Application sources
SOPS-encrypted or sealed secret manifests
Organization-specific policy overlays
restore drill evidence from docs/BACKUP_RESTORE.md
business continuity evidence from docs/BUSINESS_CONTINUITY.md
service catalog evidence from docs/SERVICE_CATALOG.md
architecture decision records from docs/ARCHITECTURE_DECISIONS.md
operations evidence from docs/OPERATIONS.md
production readiness evidence from docs/PRODUCTION_READINESS.md
incident response evidence from docs/INCIDENT_RESPONSE.md
access control evidence from docs/ACCESS_CONTROL.md
capacity planning evidence from docs/CAPACITY_PLANNING.md
compliance and audit evidence from docs/COMPLIANCE_AUDIT.md
release and environment promotion evidence from docs/RELEASE_PROMOTION.md
alert routing and SLO evidence from docs/ALERTING.md
exact rendered and live image inventory evidence from docs/SUPPLY_CHAIN.md
reviewed external-image exceptions and their hash-bound Trivy reports
```

Plaintext secrets should still not be committed, even to a private repository.
Use encrypted secrets or an external secret manager.

## Important Boundary

`PLATFORM_REPO_URL` is a source URL. It tells Argo CD where to read manifests
from. It does not push cluster data, secrets, or application state back to that
repository.

For an organization-private deployment, do not use the public GitHub URL as the
long-term `PLATFORM_REPO_URL`. Use it only as the open-source template source.
