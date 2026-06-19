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

The target still refuses to register incomplete GitOps applications when the
selected profile contains unresolved placeholders such as storage sizes,
database endpoints, Redis endpoints, object storage, backup targets, or TLS
secret references.

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
Validates the repository
Optionally commits current changes when PLATFORM_AUTO_COMMIT=true
Pushes HEAD to PLATFORM_REPO_URL
Registers private Git credentials in Argo CD when PLATFORM_REPO_TOKEN is set
Runs platform-first-deploy
```

The Git token is used as a one-command Git HTTP authorization header during
push and is not written into the Git remote URL. Argo CD receives the same
token as a Kubernetes repository Secret only after the selected GitOps profile
passes the unresolved-placeholder guard.

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
