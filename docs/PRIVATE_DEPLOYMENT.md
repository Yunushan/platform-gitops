# Private Organization Deployment

This project can remain a public MIT-licensed template while each organization
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
Renders first-deploy private values for Forgejo, Argo CD, Woodpecker, Harbor,
monitoring, Loki, Velero, Longhorn, and optional step-ca when enabled
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

Object-storage backed apps are rendered with bucket names, endpoints, regions,
cache sizes, and Kubernetes secret names only. `make platform-app-secrets` can
create the Loki and Velero secrets from ignored env values such as
`LOKI_S3_ACCESS_KEY_ID`, `LOKI_S3_SECRET_ACCESS_KEY`,
`VELERO_CLOUD_CREDENTIALS`, or `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY`. Set
`PLATFORM_APP_SECRET_REQUIRE_OBJECT_STORAGE=true` for production so missing
Loki or Velero object-storage credential secrets fail before app sync.

The default `FORGEJO_DATABASE_MODE=sqlite` is a first-dashboard bootstrap mode:
it avoids requiring an already-running external PostgreSQL and Redis service.
For long-term premium production, switch the private deployment to
`FORGEJO_DATABASE_MODE=external` and provide `FORGEJO_DATABASE_HOST`,
`FORGEJO_DATABASE_NAME`, `FORGEJO_DATABASE_USER`, `FORGEJO_REDIS_HOST`, and
`FORGEJO_REDIS_URL` through the private env file or another private value flow.

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
harbor.<PLATFORM_DOMAIN>      -> Harbor hostname
grafana.<PLATFORM_DOMAIN>     -> Grafana hostname
prometheus.<PLATFORM_DOMAIN>  -> Prometheus hostname
```

## What Stays Public

The public MIT repo should contain:

```text
Automation
Examples
Placeholder manifests
Validation scripts
Documentation
Default profiles without real organization values
```

## What Stays Private

The private deployment repo should contain:

```text
Real internal domains
Real cluster sizing
Real storage classes and backup targets
Real Argo CD Application sources
SOPS-encrypted or sealed secret manifests
Organization-specific policy overlays
```

Plaintext secrets should still not be committed, even to a private repository.
Use encrypted secrets or an external secret manager.

## Important Boundary

`PLATFORM_REPO_URL` is a source URL. It tells Argo CD where to read manifests
from. It does not push cluster data, secrets, or application state back to that
repository.

For an organization-private deployment, do not use the public GitHub URL as the
long-term `PLATFORM_REPO_URL`. Use it only as the open-source template source.
