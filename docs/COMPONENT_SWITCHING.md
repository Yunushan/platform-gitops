# Component Switching

This project is designed to be changed without restructuring the repository.
Replacement profiles keep the shared platform services, such as cert-manager,
MetalLB, storage, database operator, registry, monitoring, logging, and
backups, unless the profile explicitly lists a component under `remove`.
Use the profile name without the `.yaml` suffix when bootstrapping:

```bash
PLATFORM_PROFILE=<profile-name> PLATFORM_APPLY_GITOPS=true PLATFORM_REPO_URL=<PRIVATE_REPO_URL> make platform-argocd
```

## Use the premium 3-node profile

Use profile:

```text
profiles/premium-3node.yaml
```

Bootstrap by selecting the profile at registration time:

```bash
PLATFORM_PROFILE=premium-3node PLATFORM_APPLY_GITOPS=true PLATFORM_REPO_URL=<PRIVATE_REPO_URL> make platform-argocd
```

This registers applications from:

```text
gitops/clusters/rke2-main/premium-3node
```

The premium path uses Rocky Linux 10, RKE2, Cilium, Traefik, Forgejo, Woodpecker, and Argo CD, then adds hardened values for Harbor, Longhorn, CloudNativePG, monitoring, Loki, and Velero.

## Use the premium 3-node GitLab CE profile for 100+ users

Use profile:

```text
profiles/premium-3node-gitlab-ce.yaml
```

Bootstrap by selecting the profile at registration time:

```bash
PLATFORM_PROFILE=premium-3node-gitlab-ce PLATFORM_APPLY_GITOPS=true PLATFORM_REPO_URL=<PRIVATE_REPO_URL> make platform-argocd
```

The profile inherits the premium 3-node platform, removes the Forgejo and Woodpecker applications, and adds the premium GitLab apps:

```text
gitops/clusters/rke2-main/premium-3node/apps/gitlab-ce
gitops/clusters/rke2-main/premium-3node/apps/gitlab-runner
```

The GitLab values are a starting budget for 100+ users on three 8-vCPU, 32-GiB nodes. They configure two to three webservice, Sidekiq, and GitLab Shell replicas, CloudNativePG PostgreSQL, the platform Valkey TLS primary Service, and external S3-compatible object storage. The default repository tier uses one Gitaly replica with a 200Gi `longhorn-critical-encrypted` volume. The Runner allows up to 20 concurrent jobs. Load-test the profile and record the result in the private capacity baseline before production use.

Before production registration:

- Provision the `gitlabhq_production` database and `gitlab` role in `platform-postgres`.
- Create the PostgreSQL, Valkey, Valkey CA, object-storage, registry-storage, and toolbox backup-storage Secrets and the `gitlab-tls` certificate outside Git. The toolbox backup Secret uses the S3 client format expected by the pinned chart. The consolidated object-storage and registry Secrets use their own formats.
- Set the `GITLAB_*_SECRET_NAME` values and `PLATFORM_DOMAIN` in the private deployment environment, then run `PLATFORM_PROFILE=premium-3node-gitlab-ce make platform-render-private-values` from the internal GitOps checkout. The renderer fills the GitLab and Runner references together. For a fresh three-replica repository tier, also set `GITLAB_PRAEFECT_PLAN_FILE` to the version 3 plan under `private/`. [GitLab repository HA setup](GITLAB_REPOSITORY_HA.md#optional-fresh-kubernetes-chart-overlay)
- Create a GitLab runner through the GitLab UI or API, then create its
  authentication Secret in the runner namespace through a private mechanism.
  The Secret needs `runner-token` containing the issued authentication token and
  `runner-registration-token` containing an empty string. Set the placeholder
  secret name privately; never put the token in Helm values or Git. Set runner
  scope, tags, protection and untagged-job policy on the GitLab runner itself.
  The chart creates a namespace-scoped role using the documented Kubernetes
  executor permissions in `gitlab-jobs`; the manager and its authentication
  Secret stay in `gitlab-runner`. Jobs use the `gitlab-job` service account with
  token automount disabled. The job namespace denies ingress and allows DNS and
  the listed web/Git egress ports; further destinations or ports need an explicit
  policy. Kubernetes executor permissions include secret access and pod execution
  within the job namespace, so keep other sensitive workloads out of it. Review
  the [GitLab Runner RBAC guidance](https://docs.gitlab.com/runner/install/kubernetes_helm_chart_configuration/).
  A local disposable Kubernetes 1.35 API admitted the rendered resources and
  confirmed that the manager can create job pods but cannot read Secrets in its
  own namespace through the job RoleBinding. The job service account has neither
  that manager-namespace Secret access nor permission to create job pods. This
  is an API/RBAC test; it does not enforce network policy or run a live CI job.
  A separate local fixture runs the official checksum-verified Runner 19.1.1
  binary as an unprivileged user against a synthetic loopback coordinator. It
  checks that a controlled shell job runs, uploads a trace, and reports success.
  This is not a GitLab server, Kubernetes-executor, clone, or GitOps test.
  An additional isolated test runs that same Runner with the rendered Kubernetes
  executor configuration and a synthetic coordinator against a disposable
  Kubernetes 1.35 API. It authenticates using a short-lived manager service-account
  token and verifies that the Runner creates a non-privileged, tokenless job pod
  in `gitlab-jobs`. The API has no worker, so the pod remains Pending: this does
  not establish container execution, network enforcement, or GitLab integration.
  Separately, a disposable K3s 1.35 VM test ran Runner 19.1.1 with the rendered
  executor configuration, a digest-pinned helper image and a synthetic coordinator.
  A job checked out a generated repository from a private lab Git daemon,
  verified its unique content on an actual worker, returned the marker in its
  trace, and reported success. Its original guests and temporary scope were
  removed. This case disabled K3s network-policy enforcement; it does not verify
  checkout from a real GitLab server, Cilium policy, GitOps handoff or HA.
  A separate five-VM Cilium 1.19.10 case passed after the control-plane quorum
  exercise: the Runner checked out the synthetic repository over allowed TCP
  9418, while the same job could not read a separately listening lab service on
  TCP 19000. The service was reachable from the lab host before and after the
  check. This tests those two egress cases, not every rule, tenant isolation,
  physical-node failure, or real GitLab integration.
- Run `PLATFORM_PROFILE=premium-3node-gitlab-ce make platform-profile-check` after rendering private values.

The default Gitaly layout relies on node recovery and tested Longhorn/Velero restores. The optional Praefect layout requires a fresh installation and a separately provisioned database and TLS Secrets. GitLab SSO through Keycloak remains a site integration.

The chart's ingress provider defaults to nginx even though this profile selects
the Traefik ingress class. Review and test the web and Git SSH routes with the
site's Traefik configuration before use; the present profile does not establish
a working Git SSH entry point. Switching the chart provider to Traefik also
creates a Traefik `IngressRouteTCP` and requires the matching CRD, entry point,
and exposed listener, so do not assume the class name alone configures SSH.
For an explicitly selected Traefik entry point, the optional
`scripts/render_gitlab_traefik_entry.py` renders matching GitLab and Traefik
values. For example, a private plan may contain:

```json
{"schema_version":1,"ssh_host":"git.example.test","public_port":2222,
 "listener_port":2223,"entrypoint":"gitlab-shell","ingress_class":"traefik",
 "gitlab_namespace":"gitlab","gitlab_release":"gitlab",
 "traefik_namespace":"traefik","traefik_instance":"traefik-traefik"}
```

Run `python3 scripts/render_gitlab_traefik_entry.py --plan private/gitlab-ssh.json
--output-dir private/gitlab-ssh-overlay`. Merge each generated values file only
into its corresponding chart after verifying that the entry point and TCP port
are dedicated, the Traefik CRD provider and `IngressRouteTCP` CRD are installed,
and external routing and firewall rules reach the load balancer. Include the
generated NetworkPolicy in GitLab's namespace after checking that the selected
Traefik pod labels and GitLab release match the rendered workloads. It permits
only those Traefik pods to reach the chart's shell target port 2222. SSH has no HTTP
Host or TLS SNI to distinguish services on the same listener. The generated
route sends all SSH traffic on that entry point to GitLab. A chart render does
not prove SSH host keys, user authentication, clone/push, or node-failure
behavior. This option is separate from the Cilium Gateway and HAProxy design.

## Switch Forgejo to Gitea

The profile selection alone currently leaves Woodpecker configured for
Forgejo. Before deploying it, prepare Gitea OAuth credentials privately and
render reviewed Woodpecker chart values with the Gitea driver. The optional
`scripts/render_woodpecker_forge.py` accepts an existing values file and a
private copy of [the synthetic plan](../examples/woodpecker-gitea-forge.json),
then writes the resulting values to standard output. Its selected Kubernetes
Secret must contain `WOODPECKER_GITEA_CLIENT` and `WOODPECKER_GITEA_SECRET`.
It retains the other named secrets, server settings and agent values; it does
not create OAuth credentials, modify the profile, sync Argo CD or verify login.
The [Woodpecker Gitea configuration](https://woodpecker-ci.org/docs/3.18/administration/configuration/forges/gitea)
describes the driver settings. The current project's Gitea profile needs this
additional integration and live acceptance before it can be treated as ready.
The private values renderer can select the same driver explicitly with
`WOODPECKER_FORGE_PROVIDER=gitea`, `WOODPECKER_GITEA_URL` set to the reviewed
HTTPS origin, and optionally `WOODPECKER_GITEA_OAUTH_SECRET_NAME`. Its default
remains Forgejo for other profiles. With `PLATFORM_PROFILE=gitea-woodpecker-argocd`,
the renderer requires the Gitea provider setting and defaults to
`gitops/clusters/rke2-main/apps/woodpecker/values.yaml`.
It also resolves the Gitea chart's hostname and storage placeholders. Set
`GITEA_DATA_SIZE`, `GITEA_STORAGE_CLASS`, and either `PLATFORM_GITEA_HOST` or
`PLATFORM_DOMAIN` in the private environment. The renderer adds the Woodpecker
HTTPS hostname on port 443 to Gitea's `security.ALLOWED_HOST_LIST` for webhook
delivery to a private address. Set `WOODPECKER_ADMIN_USERS` to the Gitea
login names authorized to administer Woodpecker when registration is closed.
Woodpecker defaults to a single
server with SQLite for this profile. Set `WOODPECKER_DATABASE_MODE` explicitly
if you provision a separate PostgreSQL datasource.
Use `--woodpecker-values` to select a different file. Keep the rendered URL
and other private configuration in the private deployment repository.
Provision the Gitea OAuth Secret separately; this option does
not change the profile's Argo CD source. When the shared secret playbook is run
with this selection, it requires the Gitea Secret and skips the Forgejo OAuth
application task; other Forgejo-related application tasks remain separate.
The shared health-check playbook also selects the Gitea Secret name and keys
when `WOODPECKER_FORGE_PROVIDER=gitea` is set. This is a credential-contract
check; the remaining Gitea application and end-to-end health checks still need
their own acceptance.
An offline test renders the vendored Woodpecker chart with synthetic Gitea
values and checks the server's driver, URL and OAuth Secret reference. A
disposable Kubernetes API accepts server and agent Pods in a namespace that
enforces restricted Pod Security. A separate loopback-only Linux test with
Gitea 28.3.0 and Woodpecker 3.16.0 verified OAuth sign-in, private repository
activation, a push-triggered pipeline that checked out the exact commit, and
the resulting Gitea commit status. This test used Woodpecker's local backend.
A disposable K3s VM test also ran a private Gitea pipeline through the
Helm-rendered Woodpecker agent after replacing its Pod. The
[optional Argo CD handoff](CI_GITOPS_HANDOFF.md#woodpecker-and-argo-cd)
promoted the exact verified commit in both cases and observed its ConfigMap
in a disposable Kubernetes API.

For Forgejo 15 with a private Woodpecker endpoint, set
`FORGEJO_WOODPECKER_WEBHOOK_HOST` to the Woodpecker DNS hostname in the private
environment before rendering Forgejo values. This adds that host to
`webhook.ALLOWED_HOST_LIST` alongside Forgejo's default `external` entry.
The setting accepts a single hostname and keeps TLS verification enabled.
On an existing installation with a custom list, add the hostname to the
existing private values instead of replacing that list.
Production profile checking now rejects a Forgejo-configured Woodpecker, and
the skip-incomplete application renderer skips it until Gitea and the selected
Secret name are present in the values. These checks do not read the Secret or
verify its keys, OAuth login, webhooks or pipeline execution.

For an existing Gitea OAuth application, the optional
`scripts/provision_woodpecker_gitea_oauth.py` can create the selected Kubernetes
Secret from `client-id` and `client-secret` files in an owner-only directory.
Copy [the synthetic plan](../examples/woodpecker-gitea-oauth.json) privately,
bind the intended context and both cluster/namespace UIDs, and provide the
credential files with owner-only permissions. The default invocation checks
local inputs only; `--check-server` reads the selected cluster, and `--apply`
explicitly creates the Secret if absent. A different existing Secret is refused,
not rotated. No credential values are printed. This helper does not create the
Gitea OAuth application or verify a login. A disposable Kubernetes API test
verified creation, read-back, and refusal to overwrite different credentials.

Use profile:

```text
profiles/gitea-woodpecker-argocd.yaml
```

Enable:

```text
gitops/clusters/rke2-main/alternatives/gitea
```

Disable:

```text
gitops/clusters/rke2-main/apps/forgejo
```

## Switch Forgejo/Woodpecker to GitLab CE

Use profile:

```text
profiles/gitlab-ce-runner-argocd.yaml
```

Enable:

```text
gitops/clusters/rke2-main/alternatives/gitlab-ce
gitops/clusters/rke2-main/alternatives/gitlab-runner
```

Disable:

```text
gitops/clusters/rke2-main/apps/forgejo
gitops/clusters/rke2-main/apps/woodpecker
```

## Switch Longhorn to Rook/Ceph

Use profile:

```text
profiles/storage-rook-ceph.yaml
```

## Switch Traefik to ingress-nginx

Use profile:

```text
profiles/ingress-nginx.yaml
```

## Switch kube-vip to HAProxy/Keepalived

Use profile:

```text
profiles/vip-haproxy-keepalived.yaml
```
