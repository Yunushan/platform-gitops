# GitLab CE repository HA: implementation boundary

The existing GitLab CE Kubernetes profile uses a single Gitaly storage. It is
not a Praefect-backed repository cluster, and the GitLab/Runner stack planners
retain explicit repository HA, migration, and CI integration gaps for every
runtime. The optional chart overlay and local failover test are described below.

## Required design

GitLab's [Gitaly Cluster architecture](https://docs.gitlab.com/administration/gitaly/praefect/)
requires an independently available Praefect metadata database, a resilient
Praefect entry point, and multiple Gitaly storage nodes. A virtual storage
routes requests to repository replicas; three Gitaly nodes in distinct failure
domains and replication factor three are the target for this project. Validate
the effective replication factor of existing repositories individually: adding
a storage node does not automatically increase a custom replication factor.
Follow the [upstream node-management procedure](https://docs.gitlab.com/administration/gitaly/praefect/recovery/).

The database, routing, object store, GitLab web and background workers, and
Runner infrastructure each have independent failure modes. Multiple application
replicas or replicated volumes do not establish repository-level failover.
GitLab documents Gitaly Cluster on Kubernetes as beta; assess the
[current Kubernetes guidance](https://docs.gitlab.com/administration/gitaly/kubernetes/)
and the precise pinned release before choosing that deployment. Vendor support
for Gitaly Cluster is limited to Premium and Ultimate customers.

Repository writes may be asynchronously replicated; an accepted write that has
not reached a surviving replica can be lost. Do not claim zero data loss or
prescribe an RPO from replica count alone. Backups of repositories, the GitLab
database, Praefect metadata, object storage, application secrets and independent
bootstrap material require a coordinated, tested recovery procedure.

## Migration and acceptance gates

* Inventory source repositories, storage assignments, refs and LFS objects;
  preserve a restorable backup before changing repository storage.
* Configure a distinct virtual storage and follow the release-specific upstream
  migration procedure. Do not repoint existing storage or copy data blindly.
* Compare refs and relevant repository data after migration, then check actual
  replica health and repository consistency. Praefect's `dataloss` command
  alone does not establish that replicas match; GitLab recommends the
  [replica checksum task](https://docs.gitlab.com/administration/gitaly/praefect/troubleshooting/).
  Before testing primary loss, check `praefect metadata` immediately before
  stopping the primary. Each intended survivor must be healthy, current in
  Praefect's generation metadata, and eligible to become primary. Matching
  branch tips alone can miss a pending generation update.
* Exercise reads and writes through the normal SSH and HTTPS entry points,
  fail each relevant component and node, and measure acknowledged-write loss,
  recovery time, replication lag and behavior when no valid primary exists.
* Register a narrowly scoped GitLab Runner, run a real pipeline, verify its
  output and the selected Argo CD or Flux reconciliation, and test recovery.
* Repeat deployment and recovery against each intended runtime. Docker and raw
  hosting still need an explicit Kubernetes target for Argo CD or Flux.

The separate multi-host test lab remains necessary to establish physical
failure-domain and recovery behavior. No production system is modified by the
planner or this document.

## Local GitLab CE repository failover test

A disposable local Docker lab used GitLab CE 19.1.1, three Gitaly 19.1.1
processes with separate repository directories, three Praefect 19.1.1
processes, and HAProxy 3.2.26. GitLab used Praefect as its default storage
from first boot. All three routers connected to the same dedicated PostgreSQL
17.8 metadata database with `verify-full`. The test checked that PostgreSQL
reported TLS sessions from the Praefect role.

An authenticated Git HTTPS push reached all three Gitaly replicas. The test
stopped the primary after confirming the same branch tip on each replica.
Praefect identified a new primary, and GitLab served the old tip and accepted
a new commit while the original primary was still down. The read and write
checks completed 5.9 seconds after the test observed the unhealthy state.
When the original process restarted, it caught up to the new tip. Stopping
HAProxy's preferred Praefect backend also preserved Git reads and writes
through the surviving routers. All three replica refs matched the final push.

This verifies process failover after replication caught up. The lab shared one
host, one HAProxy listener and one Praefect database. Physical host loss,
database and load balancer failover, Git over SSH, Kubernetes chart runtime
behavior, and recovery of writes made before replication catches up remain
unverified. The timing is a local observation and excludes failure detection.

## Optional fresh Kubernetes chart overlay

`scripts/render_gitlab_praefect.py` writes a private Helm values overlay for
the vendored GitLab 10.1.1 chart. It is intended for a **new**, empty GitLab
installation on RKE2, K3s or Kubernetes; it does not migrate an existing
single-Gitaly installation. The plan must explicitly set `fresh_install` to
`true` and the virtual storage to `default`. Prepare the Praefect database,
database role, TLS certificate and CA, three credential secrets, and a storage
class before deploying. No credentials belong in the plan or Git.

The local Praefect migration changes are declared in the
[vendored chart inventory](../config/vendored-charts.json), with eight exact
file paths and their reasons. The local tree digest is separate from the
unchanged upstream package and tree digests. The patch declarations were
checked against the checksum-pinned upstream archive.

For example, save a private JSON plan with this shape, replacing the example
names and endpoint with values appropriate to the isolated environment:

```json
{
  "schema_version": 3,
  "fresh_install": true,
  "virtual_storage": "default",
  "storage_class": "replicated-block",
  "storage_size_gi": 100,
  "postgres_host": "postgres.internal.example",
  "postgres_port": 5432,
  "postgres_database": "praefect",
  "postgres_user": "praefect",
  "postgres_secret": "praefect-db",
  "postgres_secret_key": "password",
  "postgres_ca_secret": "postgres-ca",
  "gitaly_auth_secret": "gitaly-auth",
  "praefect_auth_secret": "praefect-auth",
  "gitaly_tls_secret": "gitaly-tls",
  "praefect_tls_secret": "praefect-tls",
  "repository_ca_secret": "repository-ca"
}
```

Run `python3 scripts/render_gitlab_praefect.py --plan private/praefect-plan.json
--output-dir private/praefect-overlay` to create a new private directory; it
refuses to overwrite one. The overlay references secrets, uses three Gitaly and
three Praefect instances with hard hostname anti-affinity, and requests TLS
hostname verification for PostgreSQL. Versions 2 and 3 configure GitLab
clients to reach Praefect over TLS and Praefect to reach Gitaly over TLS.
Version 3 adds repository ingress policies for the premium GitLab application.
Version 1 remains available for existing database-TLS-only plans.

The overlay enables one Praefect migration Job and disables automatic
migrations in the router containers. Competing migrations under the previous
parallel startup had prevented all three routers from starting with SQLSTATE
`42P07` for `repository_replica_path_index`. The Job uses the router image,
rendered config, and PostgreSQL Secret and CA. Its name is derived from the
migration inputs, so reconciling the same render reuses the completed Job.

Argo CD applies the Praefect ConfigMap and bootstrap permissions in wave
`-3`, the Job in wave `-2`, and the routers in the default wave. Each
router's init container also waits for the named Job to complete, so the
gate applies when Helm or another controller submits the resources
together. The included bootstrap policy allows the API connection. If you
write your own isolated namespace policy, allow that connection from the
router pods. The gate has permission to get its Job and mounts its API token
only in the init container with the default service account settings.

Regenerate older private overlays to pick up the new values. Prepare the
Praefect database and credentials before syncing, and retain the completed
Job while its routers use it as their startup gate. A failed Job holds router
startup until the migration is reviewed and completed.
[GitLab migration sequencing](https://docs.gitlab.com/update/zero_downtime/#upgrade-gitaly-cluster-praefect-nodes)

In a four-VM K3s run with the rendered chart, all three router pods entered
the gate before the Job existed. The same pods became Ready after the Job
completed. Praefect connected to PostgreSQL over verified TLS and reached
all three Gitaly replicas. The toolbox restored and backed up the exact
fixture ref, including a backup while the primary Gitaly pod was absent.
The replacement reused its local volume and rejoined at the same generation.
This run used Flannel, local PVs, and a standalone disposable PostgreSQL
instance.

A separate six-VM K3s run deployed the same migration gate with Cilium
1.19.10 and kube-proxy replacement. The original router pods waited for
the Job and became Ready after it completed. The network policy allowed
the Toolbox TLS path and blocked untrusted repository clients. The fixture
backed up the exact ref with the Gitaly primary absent and recovered that
replica on its local PV. After a planned promotion in the external
PostgreSQL 18.6 fixture, the original routers reconnected over TLS, read the
seeded ref, and wrote a new repository. The lab used three database processes
on one host and finished without OOM kills.

The rendered chart also passed on RKE2 `v1.36.4+rke2r1` with bundled Cilium
1.19.6 and kube-proxy replacement. The four-VM lab had three etcd members.
The repository pods ran across two mixed-role servers and one agent. The
first attempt reached the migration gate and left two Gitaly pods Pending
with insufficient memory on their PV nodes. Increasing those two guests
to 3328 MiB let all three replicas bind separate local PVs. The same router
pods started after the migration Job completed, enforced repository network
policy, and backed up the exact ref with the primary Gitaly pod absent.
After the replacement caught up, the planned PostgreSQL promotion kept the
database Service address and TLS hostname. The original routers reconnected,
read the seeded ref, and wrote a new repository. The lab removed its VMs
and temporary state.

Create both repository TLS Secrets with `tls.crt` and `tls.key` in the
release namespace. The repository CA Secret must contain `repository-ca.crt`
and the database CA Secret must contain `postgres-ca.crt`. For release
`gitlab` in namespace `gitlab` with the default chart service names, include
`gitlab-praefect.gitlab.svc` on the Praefect certificate. Include
`gitlab-gitaly-default-N.gitlab-gitaly-default.gitlab.svc` for ordinals
`0`, `1`, and `2` on the Gitaly certificate. Use the actual hostnames from
the Helm render if the release or namespace changes. See the
[GitLab chart TLS setup](https://docs.gitlab.com/charts/charts/gitlab/praefect/#running-praefect-over-tls).

For version 3, run `make platform-render-private-values` in the **internal**
checkout with `PLATFORM_PROFILE=premium-3node-gitlab-ce` and
`GITLAB_PRAEFECT_PLAN_FILE=private/gitlab-praefect.json`. Set the GitLab and
Runner Secret names from the deployment environment examples. The private
renderer wires the values and network component into the pinned `gitlab`
release. It preserves additional values files and custom CAs. The following
steps show the equivalent manual wiring and render checks:

1. Copy `praefect-values.yaml` beside the application's existing values file.
   Append it to `helmCharts[].additionalValuesFiles`.
2. Copy the generated `kustomization.yaml` and
   `repository-network-policies.yaml` into an app-local `repository-network/`
   directory. Append `repository-network` to the application's `components`,
   after the existing `network-isolation` component.
3. Render with Helm enabled. Check that `platform-allow-same-namespace` has
   only its egress rule and that both repository StatefulSets carry the
   `platform.gitops/repository-tier: "true"` pod label. Check for any site
   ingress policies that also select these pods, since their allowances add
   together. Use a CNI with NetworkPolicy enforcement and verify that an
   ordinary same-namespace pod cannot dial either repository port.

On Argo CD, the premium application creates its namespace and policies before
running GitLab's bootstrap Jobs. The upgrade check uses a namespace-scoped
service account to inspect the existing chart metadata. A fresh namespace
can proceed without it. When the existing GitLab workloads or repository
claims are present, missing metadata stops the sync. Preserve the
`gitlab-gitlab-chart-info` ConfigMap during recovery so later upgrades use
the recorded chart and GitLab versions. Existing installations still run
the pinned GitLab upgrade check before the chart updates that ConfigMap.
[Argo CD Helm hook behavior](https://argo-cd.readthedocs.io/en/stable/user-guide/helm/#helm-hooks)

### Provision the databases with CloudNativePG

For a fresh premium profile using the bundled CloudNativePG 1.30 operator,
set `GITLAB_CNPG_MANAGED_DATABASES=true` when rendering the private checkout.
The renderer adds retained `DatabaseRole` and `Database` resources to
`platform-postgres`. It declares `amcheck`, `pg_trgm`, and `btree_gist`
for GitLab's application database. If the Praefect plan uses the same CNPG
write Service on port 5432, it also declares a separate Praefect database
and login. A Praefect plan targeting external PostgreSQL keeps its existing
database provisioning workflow.
[CloudNativePG database management](https://cloudnative-pg.io/docs/1.30/declarative_database_management/)

Provision each referenced password in both namespaces from the same private
secret source. In `platform-databases`, use the specified Secret name, type
`kubernetes.io/basic-auth`, keys `username` and `password`, and label
`cnpg.io/reload: "true"`. In `gitlab`, use the chart Secret reference and
key (`psql-password` for the application and `postgres_secret_key` from
the Praefect plan). Keep password values out of Git.
[CloudNativePG role password management](https://cloudnative-pg.io/docs/1.30/declarative_role_management/)

Sync the CNPG operator and shared PostgreSQL application before syncing
GitLab. Verify `status.applied: true` at the current generation for each
generated `DatabaseRole` and `Database`. The roles own only their dedicated
databases. CloudNativePG can adopt and reconcile pre-existing names, so
verify the target roles and databases are dedicated before first applying
the manifests. Once generated, repeat renders preserve the declared
identities. The retain policies preserve SQL objects if their custom
resources are removed.

With `CNPG_RENDER_POSTGRES_CLUSTER=true`, set `CNPG_SYNCHRONOUS_STANDBYS=1`
in the private deployment environment to require synchronous acknowledgments
from one standby and use CloudNativePG failover quorum. This applies to the
whole shared database cluster. Use at least three instances across separate
failure domains. The renderer also accepts `2` for two acknowledgments.
With three instances and a requirement for two acknowledgments, losing one
standby can block writes. Review the rendered Cluster before syncing. Omitting
the setting or using `0` omits the synchronous configuration from a full
render. For an existing cluster managed outside the full renderer, make the
same change in its authoritative Cluster manifest.
[CloudNativePG failover quorum](https://cloudnative-pg.io/docs/1.30/failover/#failover-quorum-quorum-based-failover)

The component allows TLS on TCP/8076 from GitLab webservice, Sidekiq,
GitLab Shell, Toolbox and Gitaly to Praefect. Praefect can reach the Gitaly
backends and Gitaly pods can reach their peers for replication on that port.
It allows monitoring ingress on TCP/9236. If the monitoring namespace restricts egress,
allow that port from the scraper to the repository pods. The site's database
policies must also admit Praefect to its PostgreSQL endpoint and the GitLab
application to its own database. Check both directions for site dependencies.
[GitLab repository-tier traffic requirements](https://docs.gitlab.com/administration/gitaly/praefect/configure/)

The chart retains plaintext TCP/8075 for kubelet probes. The policy restricts
pod-network access, and access from the node hosting the pod follows
Kubernetes node traffic semantics. Restrict pod creation and label changes
in the GitLab namespace to trusted operators. Validate database trust and the
actual TLS connections in the target environment before using the cluster.
[Kubernetes NetworkPolicy behavior](https://kubernetes.io/docs/concepts/services-networking/network-policies/#the-two-sorts-of-pod-isolation)

It does not deploy, configure the GitLab Runner, provision storage or databases,
or establish HA. Render the full chart with the site's complete values before
applying it. The checked-in premium base profile uses the chart-10
`installCertmanager` flag, disables the chart's Gateway API/Envoy defaults in
favor of its configured ingress, and requires a separate toolbox backup-storage
secret reference. It pins the stable API versions for PodDisruptionBudget,
HorizontalPodAutoscaler, CronJob and Ingress so offline Helm rendering also
works with current Kubernetes. Replace deployment placeholders privately.

The pinned chart rendered with plan versions 1 and 2. With synthetic site
settings, an isolated Kubernetes 1.35 API accepted all 56 built-in
resources via server-side dry run and created the two three-replica
StatefulSet objects. This API fixture had no workers or monitoring CRDs.

### Local K3s repository runtime

On K3s v1.35.0+k3s1, the two rendered StatefulSets started with the chart's
GitLab CNG 19.1.1 images across three workers. Gitaly bound three distinct
statically provisioned local PVs. Praefect dialed the three Gitaly backends
over TLS, and PostgreSQL reported encrypted sessions for the Praefect role.

The test restored a bundled Git repository through the Praefect TLS Service.
After Praefect reported three healthy, current replicas eligible to become
primary, a backup returned the expected Git ref. The test then cordoned the
active Gitaly pod's worker and removed that pod. A second backup returned the
same ref while only two Gitaly pods were running. After uncordoning, the
replacement pod reused its local volume and rejoined at the same generation.

The fixture used smaller Gitaly CPU and memory requests, preloaded images,
local volumes, and a single external PostgreSQL instance. The production
resource settings remain unchanged. This run exercised repository restore,
read continuity and pod recovery.

With the version 3 component and the K3s network policy controller enabled,
a same-namespace pod without an approved client label could reach neither
repository port on any of the six pods. The Toolbox test client could
reach Praefect over TLS and restore the repository. It could reach neither
the plaintext port on either tier nor Gitaly's TLS port directly. The
Gitaly readiness checks and Praefect's TLS connections to Gitaly remained
functional.

The same repository test passed on Cilium 1.19.10 with kube-proxy replacement
on six disposable KVM guests. The lab used three K3s control-plane voters,
three separate workers, and HAProxy in front of the Kubernetes API. It first
proved etcd quorum behavior and worker HTTP continuity during control-plane
outages. After the original voters recovered, it started Gitaly and Praefect
on the three workers and applied the version 3 ingress policies. The six
repository pods enforced the same allowed and denied connections. Praefect
restored and backed up the exact Git ref over TLS, read it while the primary
Gitaly pod was absent, and recovered the replacement replica on its original
local PV. The 969.163-second run finished with no OOM kills, and the private
receipt matched all 498 Python source hashes after VM teardown.

### Planned PostgreSQL promotion

A second six-VM Cilium run used three PostgreSQL 18.6 processes with
`synchronous_commit = remote_apply` and a one-of-two synchronous standby
quorum. The database processes ran in the fixture's isolated Linux network
namespace. A TCP pass-through HAProxy listener kept the Praefect database
Service address and TLS hostname stable.

The fixture blocked new sessions and closed existing sessions on the old
route, cleanly stopped the primary, and verified that the selected standby
replayed the last shutdown checkpoint. It promoted that standby, observed
a new timeline with the same database system identifier, and verified both
standbys had rejoined before reopening the route. The three original Praefect
containers reconnected over TLS, backed up the original Git ref, then restored
and backed up a second repository through the promoted database. The run
finished in 985.234 seconds and removed all local VMs and database processes.

### GitLab HTTPS and SSH during Gitaly pod loss

A separate four-VM K3s run connected a pinned GitLab CE 19.1.1 Docker
application to the chart's three Praefect and three Gitaly pods. The
fixture used HAProxy TLS pass-through over three pod port-forwards for
the application's Praefect route. The Gitaly pods called back to GitLab's
HTTPS API with the shared Shell secret. The clients verified the HTTPS
certificate and pinned the GitLab SSH host key.

After the first HTTPS push, the fixture checked the exact Git ref and
current generation on all three Gitaly replicas. It cordoned the primary
replica's worker and deleted that pod. With two Gitaly pods running,
the same GitLab application accepted an SSH push and a subsequent HTTPS
push. Both protocols returned each new commit and both surviving Gitaly
replicas held the expected ref. After uncordoning, a replacement pod
caught up on its existing local volume. The 487.469-second run completed
with no OOM kills and removed its Docker application and VMs.

The application used one bundled database and cache, and Praefect used a
single external PostgreSQL process. This run covers Gitaly pod loss with
the control plane available. Application replication, a production entry
path, automatic database failover, Ceph-backed storage, RKE2 deployment,
and physical failure domains remain open.

### PostgreSQL promotion during Gitaly pod loss

A separate four-VM K3s run used the same GitLab CE application and chart
repository tier with three synchronous PostgreSQL 18.6 processes for
Praefect metadata. After an SSH push with the first Gitaly primary pod
absent, the fixture fenced the old database route, verified replay of the
last shutdown checkpoint, and promoted a standby. The database retained its
TLS name, and all three original Praefect containers reconnected.

GitLab returned the pre-promotion commit over HTTPS and SSH. A subsequent
HTTPS push reached both surviving Gitaly replicas before the absent replica
was allowed back. Its replacement caught up, and a second private project
accepted its first Git push with the exact ref verified on all three replicas.
The run finished in 558 seconds and cleaned up the disposable environment.
The database promotion was planned. The GitLab application still used its
bundled database and cache, and Gitaly used local PVs.

### Automatic CloudNativePG failover during Gitaly pod loss

In a four-guest K3s lab, the vendored CloudNativePG 1.30.0 operator ran
three PostgreSQL 18.4 instances using the rendered one-standby synchronous
policy and failover quorum. The Kubernetes Praefect routers used the
CloudNativePG write Service with certificate and hostname verification.
After GitLab accepted an SSH push with the original Gitaly primary pod
absent, the fixture deleted the PostgreSQL primary pod. CloudNativePG
elected a new primary and moved the existing write Service to it. The
three original Praefect containers reconnected over TLS.

GitLab returned the exact pre-failover ref over HTTPS and SSH and accepted
an HTTPS push while that Gitaly pod was still absent. The Gitaly replacement
caught up, and all three replicas held the first push to a new private
repository. The original PostgreSQL instance rejoined as a standby. The
lab used local PVs, one GitLab CE application with its bundled Rails
database and cache, and deliberate database pod deletion. It did not
exercise a database host partition or CI during failover.

### Shared CloudNativePG failover with external GitLab Rails

A separate four-guest K3s run configured GitLab CE 19.1.1 in a Docker
container and the three Kubernetes Praefect routers to use the same
CloudNativePG 1.30.0 write Service. Both clients verified the database
CA and hostname. Rails and Praefect used separate databases and roles.
The fixture loaded the fresh Rails schema into the operator-created
database with the application's own role, which had no `CREATEDB`
privilege.

With the original Gitaly primary absent, the fixture deleted the
PostgreSQL primary pod. One attempt timed out at 210 seconds. In a
second run with a 420-second observation window, a standby became
writable and took over the same write Service in 191.1 seconds. The
original Puma process and all three original Praefect containers kept
running and reconnected over TLS. The project ID and path survived.
GitLab returned the saved Git revision over HTTPS and SSH, accepted
a new push with the Gitaly pod absent, and later verified the exact
revision on all three replicas. A second private repository accepted
its first push, and the old PostgreSQL primary rejoined as a standby.

The result covers sequential pod loss and recovery in a disposable
environment with local PVs, one Rails instance, and its bundled cache.
The measured time comes from one successful run. CI jobs, an external
cache, Ceph storage, database host partitions, and multi-replica Rails
were outside this run. CloudNativePG 1.30.1 documents a fix for pending
failover stalling or reverting its target. This run used 1.30.0.
[CloudNativePG 1.30.1 release notes](https://github.com/cloudnative-pg/cloudnative-pg/releases/tag/v1.30.1)

### Kubernetes Runner job across shared database failover

The same four-guest topology ran with the digest-pinned CloudNativePG
1.30.1 operator and PostgreSQL 18.4. A real GitLab Runner 19.1.1
Kubernetes executor checked out an exact revision from the private
GitLab CE repository over verified HTTPS while the Gitaly primary was
absent. The build pod used the chart's job service account with
token automount disabled.

After that checkout, the fixture deleted the shared PostgreSQL primary.
CloudNativePG made a standby writable on the same write Service after
221.1 seconds in this run. The same Runner process, job ID, and build
pod UID were present after promotion. The job reported success against
the protected branch's exact revision, and the job trace contained the
unique marker released to that pod after promotion. Rails and Praefect
reconnected, the repository accepted another push, all three Gitaly
replicas matched, and the original database primary rejoined as a
standby.

This was an isolated, digest-pinned lab override of the operator image.
The run used one GitLab application instance, its bundled cache, and local
volumes.

### Argo CD handoff after shared database failover

An extension of the K3s fixture installed digest-pinned Argo CD 3.5.0.
Argo CD applied the baseline from a protected deployment branch. Its
repository credential had only `read_repository` scope. The GitLab CI
verifier and promotion writer used separate `read_api` and
`write_repository` tokens.

With the Gitaly primary absent, the handoff rejected the unfinished
pipeline and the deployment branch and live ConfigMap retained their
baseline. Following CloudNativePG promotion, the same Runner job
completed, the handoff advanced the deployment branch to its exact
tested revision, and Argo CD reported a healthy, successful sync of
that revision. The live ConfigMap contained the value checked by the CI
job. A second handoff verified the same revision without another push.

In the completed run, CloudNativePG 1.30.1 promoted the standby and
restored the same write Service in 188.5 seconds. GitLab accepted new
writes with one Gitaly replica absent, and the returning replica caught
up. A second private repository accepted its first push and had matching
refs on all three replicas. The old PostgreSQL primary rejoined as a
standby, and the fixture removed its VMs and temporary state.

Earlier runs reached the Argo CD sync but timed out before teardown, or
stopped before promotion on intermittent Git reads or a failed CI job.
The completed run used a longer bounded service deadline and retried
exact-ref reads. It covered one Argo CD controller, one GitLab application
instance, bundled cache, and local PVs. Acceptance on the other runtimes
remains open.

### Flux handoff after shared database failover

The same lab can select Flux v2.6.4 and use its reviewed deployment
helper. The three workers mounted a shared NFSv4 artifact volume
exported by the disposable control VM. Flux fetched and applied the
protected deployment branch. The unfinished pipeline left the baseline
revision in both the GitRepository artifact and the Kustomization.

In the completed run, CloudNativePG 1.30.1 restored the same write
Service in 190.2 seconds. The fixture waited until GitLab reported
the same running job and verified its original Kubernetes pod UID.
That job completed successfully. After confirming the exact revision
over HTTPS and SSH, the handoff checked both refs and fetched both
commits with the promotion credential before advancing the protected
deployment branch. The Flux GitRepository artifact and Kustomization
reported the exact tested revision, and the live ConfigMap held the
tested value. A repeat verified the result without another push.

GitLab accepted new writes with the Gitaly primary still absent.
All three Gitaly replicas caught up, a second private repository accepted
its first push, and the former PostgreSQL primary rejoined as a standby.
The service exited successfully and the disposable VMs and files were
removed. Earlier attempts stopped on Git ref reads or job-status reads
before the handoff finished. The completed run checked job availability
and identity after promotion and used a bounded read-only Git preflight.

This run used a single GitLab application instance, bundled cache, local
PVs, and an NFS export on one VM. External cache, independent artifact
storage, and acceptance on the other runtimes remain open.

## Optional native Linux and Docker host bundles

For a fresh GitLab CE 19.1.1 installation, render the repository tier from a
private JSON plan. The three Gitaly hosts each need persistent repository
storage. The three Praefect hosts share a dedicated, UTF-8 PostgreSQL database
through its TLS endpoint. Assign the hosts to independent failure domains and
provision the database, certificates, DNS, firewall rules, and redundant
HAProxy entry point for the environment.

Local checks validated all six Compose files, the Ruby configurations, and
the HAProxy configuration. Using the rendered Docker role configurations,
Gitaly served a hostname-verified TLS connection and Praefect applied 43
schema migrations over PostgreSQL TLS, passed `sql-ping`, and served a
hostname-verified HTTP/2 TLS connection.

A subsequent test ran all six rendered roles together in separate containers
with a GitLab CE application, a dedicated PostgreSQL instance, and the
rendered HAProxy TLS pass-through settings. The harness launched the roles
with Docker directly on a private bridge and used private fixture addresses
for HAProxy. All three Praefect routers connected to all three Gitaly servers
over hostname-verified TLS. GitLab used the rendered Praefect route and
Gitaly made its Git push callbacks to GitLab over HTTPS.

After confirming all replicas were current, the test killed the active
Gitaly container. A host-key-verified SSH read and push succeeded, and HTTPS
returned the new commit. The restarted replica caught up. After a Praefect
container was stopped, another HTTPS push replicated to all three stores and
both protocols returned that commit. An earlier test stopped the primary
before the surviving replicas' generation metadata had caught up and
Praefect refused the read. The completed failover case covers a caught-up
repository on one Docker host. Native Linux rollout, Compose startup across
separate hosts, physical failure domains, and database or HAProxy failover
still need runtime acceptance.

```json
{
  "schema_version": 1,
  "fresh_install": true,
  "gitaly": [
    {"host": "gitaly-1.example.org", "bind_ipv4": "192.0.2.11"},
    {"host": "gitaly-2.example.org", "bind_ipv4": "192.0.2.12"},
    {"host": "gitaly-3.example.org", "bind_ipv4": "192.0.2.13"}
  ],
  "praefect": [
    {"host": "praefect-1.example.org", "bind_ipv4": "192.0.2.21"},
    {"host": "praefect-2.example.org", "bind_ipv4": "192.0.2.22"},
    {"host": "praefect-3.example.org", "bind_ipv4": "192.0.2.23"}
  ],
  "praefect_endpoint": "repository.example.org",
  "gitlab_internal_url": "https://gitlab-internal.example.org",
  "postgres": {"host": "database.example.org", "port": 5432, "database": "praefect", "user": "praefect"}
}
```

```sh
python3 scripts/render_gitlab_repository_hosts.py --runtime raw \
  --plan private/repository-hosts.json --output-dir private/repository-raw
python3 scripts/render_gitlab_repository_hosts.py --runtime docker \
  --plan private/repository-hosts.json --output-dir private/repository-docker
```

1. Keep the host lists in their original order after deployment because the
   renderer derives persistent storage names from list position. Install the
   matching GitLab CE package on native Linux. Place each role's `.gitlab.rb`
   at `/etc/gitlab/gitlab.rb`. For Docker, place the role's Compose file on its
   assigned host beside precreated `config`, `data`, and `logs` directories.
   Put the role configuration at `config/gitlab.rb`. Keep each Gitaly data
   directory on that host's own persistent storage. The Compose files pin the
   GitLab CE image digest and publish only the role's TLS port on its private IP.
2. Create root-readable credential files under
   `/etc/gitlab/repository-cluster/` or the corresponding Docker `config`
   directory. Put `gitaly-token` on Gitaly and Praefect, `praefect-token` on
   Praefect and GitLab application nodes, `shell-token` on Gitaly and GitLab
   application nodes, and `postgres-password` on Praefect. Use a distinct
   value for each credential and keep these files outside Git.
3. Install `gitaly.crt` and `gitaly.key` on Gitaly and `praefect.crt` and
   `praefect.key` on Praefect under `/etc/gitlab/ssl/`. Each Gitaly certificate
   must cover its host name. Each Praefect certificate must cover the shared
   entry point. The service account `git` must be able to read its own private
   key. Install the issuing CAs in the appropriate clients' GitLab
   `trusted-certs` directory. Put the database CA at
   `/etc/gitlab/trusted-certs/praefect-db.crt` and the Praefect CA on each
   HAProxy host at `/etc/haproxy/praefect-ca.crt`. Gitaly also needs to trust
   the HTTPS certificate of `gitlab_internal_url` for Git push callbacks.
4. Start the Gitaly roles and reconfigure Praefect to generate
   `/var/opt/gitlab/praefect/config.toml`. On one Praefect host, run the schema
   migration with the bundled binary as `git`. For Docker, run it inside the
   designated container. Then start or restart the Praefect services and run
   `sql-ping` and `dial-nodes` on all three routers.

   ```sh
   sudo -u git -- /opt/gitlab/embedded/bin/praefect \
     --config /var/opt/gitlab/praefect/config.toml sql-migrate
   ```

5. Install the rendered HAProxy `listen` section on both load balancers and
   bind the shared endpoint according to the site's VIP or routing design.
   Include `gitlab-application.rb` in the fresh application nodes' GitLab
   configuration before first startup. Version 1 sets repository routing and
   the shared shell credential. Configure the application database, cache,
   object storage, backups, Runner and GitOps separately.
   Confirm the repo tier over Git HTTPS and SSH and perform the failure tests
   above before migrating data. See the [upstream configuration procedure](https://docs.gitlab.com/administration/gitaly/praefect/configure/).

For a fresh application with existing external PostgreSQL and Valkey
services, change `schema_version` to `2` and add the following field to
the same plan. The application fragment will include the external backend
settings for both native Linux and Docker.

```json
{
  "application_backends": {
    "postgres": {
      "host": "database.example.org",
      "port": 5432,
      "database": "gitlabhq_production",
      "user": "gitlab"
    },
    "valkey": {"host": "cache.example.org", "port": 6380, "mutual_tls": true}
  }
}
```

Use the stable writable endpoints. Provision a dedicated Rails database
and login, separate from Praefect, with GitLab's PostgreSQL extensions.
GitLab's production requirements call for a separate PostgreSQL instance
for Praefect. [GitLab installation requirements](https://docs.gitlab.com/install/requirements/#gitaly-cluster-praefect)
The application fragment disables automatic database migrations so the
designated application node can initialize and migrate the database once.
Initialize the empty application database on that node with
`gitlab-rake db:schema:load gitlab:db:lock_writes db:seed_fu` before enabling
traffic. Store `rails-postgres-password` and `rails-valkey-password` alongside
the other root-readable application credential files. Install
`rails-postgres-ca.crt` and `rails-valkey-ca.crt` under
`/etc/gitlab/trusted-certs/`. PostgreSQL uses `verify-full`. When the
Valkey writer endpoint requires a client certificate, set `mutual_tls`
and mount `rails-valkey-client.crt` and `rails-valkey-client.key` under
`/etc/gitlab/ssl/`. Set their owner and group to `root:git` and mode to
`0640`. Use the container's `git` group ID for bind mounts. Allow the
`git` user to traverse the certificate directories and read both CA
files. Keep the password files readable only by root.
Use an endpoint that accepts password authentication as Valkey's
`default` user, as in the shared platform Valkey profile. Use
`noeviction` when it serves GitLab queues or shared state.
[External cache configuration](https://docs.gitlab.com/administration/redis/replication_and_failover_external/),
[GitLab TLS certificate permissions](https://docs.gitlab.com/administration/redis/tls/).

Both native Linux and Docker variants passed syntax checks with GitLab CE
19.1.1's bundled Ruby interpreter, with and without a Valkey client
certificate. A local component test used the expected `default` account
over a client-certificate-required HAProxy endpoint. It verified that the
endpoint rejected a client without a certificate and accepted the same
credentials after Valkey and Sentinel promoted a replacement writer. The
former writer rejoined as a replica.

#### Optional replicated Rails hosts for native Linux and Docker

For fresh installations, set `schema_version` to `3`, keep the version 2
`application_backends` block, and add `application_hosts`. Set
`gitlab_internal_url` to the canonical HTTPS origin reachable by clients
and Gitaly's Git push callbacks. Point its DNS record to `entry_ipv4`.

```json
{
  "application_hosts": {
    "nodes": [
      {"host": "rails-1.example.org", "bind_ipv4": "192.0.2.31"},
      {"host": "rails-2.example.org", "bind_ipv4": "192.0.2.32"}
    ],
    "entry_ipv4": "192.0.2.40",
    "proxy_ipv4": ["192.0.2.41", "192.0.2.42"],
    "ssh_port": 2222,
    "object_storage": {
      "endpoint": "https://objects.example.org",
      "region": "us-east-1",
      "buckets": {
        "artifacts": "gitlab-artifacts",
        "external_diffs": "gitlab-external-diffs",
        "lfs": "gitlab-lfs",
        "uploads": "gitlab-uploads",
        "packages": "gitlab-packages",
        "dependency_proxy": "gitlab-dependency-proxy",
        "terraform_state": "gitlab-terraform-state",
        "ci_secure_files": "gitlab-ci-secure-files",
        "ci_catalog_bundles": "gitlab-ci-catalog-bundles",
        "pages": "gitlab-pages"
      }
    }
  }
}
```

1. Initialize the empty Rails database once using the version 2 procedure.
   Back up that node's `gitlab-secrets.json` and provision identical copies
   at `/etc/gitlab/gitlab-secrets.json` for every Rails node before startup.
   Use the same Ed25519 SSH host key on each node at
   `/var/opt/gitlab/gitlab-sshd/ssh_host_ed25519_key`, readable by `git`.
   This gives clients one stable Git SSH identity. Keep both files private.
2. Pre-create the buckets and grant the Rails application identity access to
   them. Store the S3 credentials in root-readable `rails-s3-access-key`
   and `rails-s3-secret-key` files under `/etc/gitlab/repository-cluster`.
   Install the S3 issuing CA if it uses a private PKI. Each Rails node
   uses the shared PostgreSQL and Valkey endpoints and its own local data,
   config and log directories.
3. Install each rendered `rails-N.gitlab.rb` as that node's GitLab
   configuration. For Docker, use the associated Compose file and mount
   directories as for the repository roles. Install `rails.crt` and
   `rails.key` under `/etc/gitlab/ssl/`. The certificate must cover the
   canonical origin's hostname. The backend ports are 8443 and 2222.
4. Install `gitlab-application-haproxy.cfg` on both HAProxy peers using
   the site's redundant VIP mechanism. Install the public TLS certificate
   and key at `/etc/haproxy/gitlab-public.pem` and the CA used by the
   Rails listeners at `/etc/haproxy/gitlab-rails-ca.crt`. Allow access to
   the backend ports from the two configured HAProxy peer source addresses
   only. HAProxy checks Rails readiness over verified TLS, replaces
   client-forwarding headers, and supplies the original SSH client address
   through PROXY protocol.

See GitLab's [load balancer](https://docs.gitlab.com/administration/load_balancer/),
[object storage](https://docs.gitlab.com/administration/object_storage/)
and [SSH daemon](https://docs.gitlab.com/administration/operations/gitlab_sshd/).

The local Docker drill ran two GitLab CE 19.1.1 Rails containers with
separate data directories and network namespaces, three Gitaly stores,
three Praefect routers, synchronous PostgreSQL 18.6, Valkey 8.1.11 over
mutual TLS, and a SeaweedFS 4.53 S3 endpoint over TLS. The test populated
a private repository and uploaded a file through the first Rails node.
After that node stopped, the stable HAProxy address served the same
authenticated browser session, host-key-verified SSH login, exact Git
HTTPS ref and upload bytes through the surviving node. The session and
upload also survived PostgreSQL promotion and Valkey writer loss.
Restarting the first Rails node restored both healthy HTTPS and SSH
backends.

The drill used one disposable workstation, one HAProxy process for
GitLab ingress and one S3 process. The native Linux configuration passed
the same bundled Ruby syntax check. The later RKE2 rehearsals below
combined the Rails pair with Runner and either Argo CD or Flux. Native multi-host
deployment, independent entry-host loss and redundant object storage
remain open.

#### Optional native application entry VIP

For a fresh replicated Rails installation, schema version `4` adds
`application_vip` to the version 3 plan. The Rails hosts may use native
Linux or Docker; the two external entry peers use native HAProxy,
Keepalived and Python 3. This option renders configuration and systemd
units. It does not install packages, change interfaces or enable services.

```json
{
  "application_vip": {
    "vip": "192.0.2.40/24",
    "vrid": 83,
    "health_port": 8404,
    "entry_hosts": [
      {"name": "entry-a", "address": "192.0.2.41", "interface": "eth0", "failure_domain": "rack-a"},
      {"name": "entry-b", "address": "192.0.2.42", "interface": "eth0", "failure_domain": "rack-b"}
    ],
    "client_cidrs": ["192.0.2.0/24"]
  }
}
```

The VIP address must match `application_hosts.entry_ipv4`. Peer addresses
must match `application_hosts.proxy_ipv4` in the same order, and share
the VIP subnet. Use actual separate failure domains, an unused VRID and
a distinct loopback health port. Client networks must be explicit
canonical IPv4 CIDRs. Versions 1 through 3 retain their existing behavior.

Render using the existing host renderer with `--runtime raw` or
`--runtime docker`. On each entry host, provision reviewed HAProxy and
Keepalived packages, a `haproxy` service account and the required private
TLS material. Install `gitlab-application-haproxy.cfg` under
`/etc/haproxy/`, readable by the `haproxy` group. The public certificate
and private key belong in `/etc/haproxy/gitlab-public.pem`; keep the key
restricted to root and that service group. The Rails issuing CA belongs
in `/etc/haproxy/gitlab-rails-ca.crt`. The public and Rails leaf certificates
must cover the canonical GitLab hostname. Retain the shared Rails SSH host keys and
backend source restrictions described above.

Install `gitlab-entry-health.py` as a root-owned file under
`/etc/haproxy/`. It embeds its Python dependency and runs under the
isolated interpreter. Install only that host's `keepalived-entry-a.conf`
or `keepalived-entry-b.conf` under `/etc/haproxy/`. Install the two
rendered service units under `/etc/systemd/system/`. Check the selected
interface and peer address locally before enabling the matching
`gitlab-entry-vrrp@entry-a.service` or `gitlab-entry-vrrp@entry-b.service`
alongside `gitlab-entry-haproxy.service`.

Both proxies can start before they own the VIP because their public
listeners bind the local port without requiring a nonlocal-IP sysctl.
The health listener binds loopback only. Keepalived advertises the VIP
only when the exact plan identity is returned and at least one verified
HTTPS readiness backend **and** one PROXY-v2 SSH backend are healthy.
The HTTP proxy replaces client-forwarding headers, and SSH carries the
original client address through PROXY v2. Both peers start BACKUP with
`init_fail` and `nopreempt`, so an unready peer cannot advertise the VIP.

Allow IP protocol 112 only between the two peers with TTL 255, and permit
VIP movement and gratuitous ARP on their shared network. VRRP is not
quorum fencing: a network partition can produce two owners. Test the
site's isolation and fencing procedure before claiming host HA.
The selected rendered plan still reports `ha_verified: false`.

The opt-in `scripts/test_gitlab_application_entry_fixture.py` probe passed
using the rendered configuration, HAProxy 3.2.26 and Keepalived 2.3.4.
It used two separate network namespaces and synthetic TLS/PROXY-v2 SSH
backends. Both proxies started before VIP ownership. Stopping the active
proxy moved the VIP to the peer, which served verified HTTPS and SSH.
HTTPS-only loss, SSH-only loss and a trusted-CA certificate with the wrong
upstream hostname each withdrew the VIP; restoring health restored service.
The client refused the frontend certificate without the fixture CA.
The proxy replaced a client's spoofed forwarding header with the observed address.
The fixture relocated certificate/helper paths and limited HAProxy to
one thread. The probe observed at most
one owner in 210 samples, attested the five original/restarted daemon
processes, checked 557 source hashes, and removed all temporary processes
and data with zero OOM events. The rejoin check waits for Keepalived's
BACKUP state and then confirms ten seconds without VIP preemption.
This scoped result does not establish
failure-domain independence, partition fencing, systemd host lifecycle,
or a complete stack. Later RKE2 entry-VIP rehearsals add the joined
Rails/Runner path with
[Argo CD](#gitlab-entry-vip-failover-with-runner-and-argo-cd-on-rke2)
and [Flux](#gitlab-entry-vip-failover-with-runner-and-flux-on-rke2).

### GitLab Rails bootstrap and external PostgreSQL promotion

A disposable GitLab CE 19.1.1 application loaded and seeded its database
on three synchronous PostgreSQL 18.6 processes. With PostgreSQL's default
`max_locks_per_transaction=64`, loading the full schema exhausted the
shared lock table. The lab used `256` on all three members, including the
standbys. Use the same or a higher value on standbys when sizing the lock
table for your deployment. [PostgreSQL lock management](https://www.postgresql.org/docs/18/runtime-config-locks.html)

The lab closed connections to the former writer, promoted a synchronized
standby, then reopened the same TLS endpoint. The original GitLab Puma
process served the sign-in page over HTTPS and re-established encrypted
database sessions. The test used a single Rails container and a planned
PostgreSQL handover in one isolated Linux network namespace.

The disk-backed variant also passed with the bootstrap container and
database proxy stopped and restarted before the drill. It reused the
initialized schema and verified the former writer's clean shutdown,
replay of its final shutdown checkpoint, and the return to synchronous
three-member replication. Checkpoint I/O required a longer deadline in
the disk-backed fixture.

### GitLab Runner and Argo CD on RKE2 during PostgreSQL promotion

The combined run used RKE2 `v1.36.4+rke2r1` with three control-plane
members, a fourth agent guest, bundled Cilium 1.19.6 and Envoy,
GitLab CE 19.1.1, GitLab Runner 19.1.1, and Argo CD 3.5.0. The lab
initialized Rails before guest boot, kept the PostgreSQL replicas on
disk, and staged images before restarting Rails. Importing the pinned
OCI archives under their canonical names let all seven Argo CD pods
start. The full run stayed within the 12 GiB memory limit with zero
OOM kills.

The chart's migration Job released all three Praefect routers. Git
HTTPS and host-key-verified SSH reached all three Gitaly replicas. With
the original Gitaly primary pod absent, the Runner pod checked out the
exact revision and stayed in the same job through a planned synchronous
PostgreSQL promotion. Argo CD kept the baseline deployment while the job
was unfinished. After the same job passed, promotion advanced the
protected deployment branch, Argo CD applied the exact tested commit,
and repeating promotion made no second push.

The original Puma process and Praefect routers reconnected over TLS.
Git HTTPS and SSH read and wrote exact revisions during the Gitaly pod
loss. The replacement Gitaly pod caught up, and a second private
repository accepted its first push through the promoted database.
All three Gitaly replicas matched, and the former PostgreSQL writer
rejoined as a standby.

The run used one Dockerized Rails application, bundled Redis, three
synchronous PostgreSQL processes in one isolated network namespace,
and local repository PVs. The next drill used external Valkey.

### GitLab Runner and Argo CD on RKE2 across PostgreSQL and Valkey failover

The four-guest RKE2 v1.36.4+rke2r1 fixture ran bundled Cilium 1.19.6,
GitLab CE and Runner 19.1.1, synchronous PostgreSQL 18.6, Valkey
8.1.11 with Sentinel, and Argo CD 3.5.0. GitLab initialized its Rails
schema against the external backends before booting the guests.
The Valkey writer gateway used mutual TLS and refused connections
without a client certificate.

With the Gitaly primary pod absent and the original Runner job
paused, the fixture promoted a PostgreSQL standby, then stopped
the Valkey writer and its colocated Sentinel. The original Praefect
routers and GitLab Puma process reconnected to PostgreSQL over TLS.
Rails read and wrote shared state through the same Valkey gateway
after promotion. The deployment branch and live ConfigMap stayed
at baseline through both backend changes. The same Runner pod
passed, Argo CD applied the exact tested commit, and a repeat
promotion made no second push.

Git writes over HTTPS and SSH succeeded during the Gitaly outage.
All three replicas matched the original repository and the first
push to a new private repository after database promotion. The
first attempt's three-minute deadline expired with two current
replicas on the new repository. The rerun passed with a seven-minute
deadline that includes Praefect's default five-minute reconciliation
interval. The former PostgreSQL and Valkey writers rejoined as
replicas, and the fixture removed the guests and temporary data.

This run used one Rails container, local Gitaly PVs, and database,
cache and gateway processes on one workstation. The later Rails-pair
rehearsal added application process failover. Ceph storage and
independent physical failure domains remain open for the full RKE2
HA profile.

#### Run the local Rails-pair rehearsal

With the private fixture assets staged, launch the disposable RKE2
Rails-pair rehearsal from the repository root in the WSL lab.

```bash
GITLAB_REPOSITORY_APPLICATION=1 \
GITLAB_REPOSITORY_GITOPS=argocd \
GITLAB_REPOSITORY_VALKEY=1 \
GITLAB_REPOSITORY_RAILS_PAIR=1 \
bash private/validation/run-rke2-gitlab-repository.sh --journal
```

Set `GITLAB_REPOSITORY_GITOPS=flux` instead to select the Flux variant.
Add `GITLAB_REPOSITORY_ENTRY_VIP=1` to select the native application
entry pair. That variant uses a distinct GitLab VIP and adds entry-proxy
loss to the held CI job before the application and backend failovers.

Keep the launcher running in its WSL session and follow the systemd unit
name it prints with `journalctl -fu <unit>` in a separate terminal. The
service journal retains the output after that terminal closes. WSL
requires a live session to keep the distro running during this test.
[Microsoft's WSL systemd notes](https://devblogs.microsoft.com/commandline/systemd-support-is-now-available-in-wsl/)

The launcher bounds the service at 12 GiB RAM and 2 GiB swap for this
profile. A completed run writes
`private/validation/rke2-gitlab-repository-<id>.json`.
Use its acceptance checks to confirm the outcome. A stopped unit
without that report leaves the run incomplete.

### GitLab Rails-pair failover with Runner and Argo CD on RKE2

The combined local rehearsal used two rendered GitLab CE 19.1.1
Rails containers, GitLab Runner 19.1.1, RKE2 v1.36.4+rke2r1 with
bundled Cilium 1.19.6, and Argo CD 3.5.0. Three Gitaly stores and
Praefect routers ran on separate guests. The Rails pair shared
synchronous PostgreSQL 18.6, a Valkey 8.1.11 mTLS writer gateway,
application secrets, an SSH host identity and S3-compatible object
storage.

After the original Runner job checked out the exact tested revision,
the test kept the Gitaly primary pod absent and stopped the preferred
Rails container. The standby served the same project and uploaded
file. The same Runner job and pod stayed active through PostgreSQL
promotion and Valkey writer loss. The deployment branch and live
ConfigMap remained at baseline until CI passed. Argo CD then applied
the exact tested commit, and the repeat promotion made no second push.

Git HTTPS and host-key-verified SSH reads and writes succeeded during
the Gitaly outage. All three Gitaly replicas matched the existing
repository and a new repository's first push. The original Rails node
returned and both backends passed HTTPS and SSH readiness. The shared
upload remained readable. The former PostgreSQL and Valkey writers
rejoined as replicas, and the fixture removed its guests and temporary
data with zero out-of-memory kills.

The test stages pinned Runner and Helm binaries on WSL's disk before
guest boot and starts the Rails replicas sequentially. Earlier attempts
exposed intermittent post-failover Git-ref and pipeline-jobs reads.
The handoff now retries failed post-push Git-ref observations within its
existing deadline. The read-only GitLab pipeline requester allows three
attempts for HTTP 500, 502, 503 and 504. Authentication and validation
errors still fail immediately, and a superseding deployment revision
still aborts promotion.

This rehearsal used one workstation, one ingress HAProxy, one
SeaweedFS 4.53 S3 process and local Gitaly PVs. The Rails-pair profile
used a 12 GiB memory cap and up to 1 GiB swap. Independent host loss,
redundant ingress and object storage, and Ceph remain open. The Flux
counterpart is described below.

### GitLab Rails-pair failover with Runner and Flux on RKE2

The combined Flux rehearsal passed with two GitLab CE 19.1.1 Rails
containers, Runner 19.1.1, RKE2 v1.36.4+rke2r1 with bundled Cilium
1.19.6, and Flux 2.6.4. The six Flux controllers each had two replicas.
Three workers shared the NFSv4 artifact filesystem, and the original
Runner job used the reserved control-plane guest. PostgreSQL 18.6 and
Valkey 8.1.11 supplied the replicated application backends.

With the Gitaly primary pod absent and the exact CI revision checked
out, the test stopped the preferred Rails node. The standby served the
same project and uploaded file. During that same held job, the test
promoted PostgreSQL, deleted the elected Flux source-controller pod,
and stopped the Valkey writer and its colocated Sentinel. The original
Flux standby served bytes matching the recorded artifact digest before
deletion, after deletion, and after leadership recovered. The surviving
Rails process read and wrote shared state through the same mTLS gateway.
The deployment branch, Flux revisions and live ConfigMap stayed at
baseline while the job was unfinished.

The same Runner job and pod succeeded. Flux's GitRepository artifact,
Kustomization and deployed ConfigMap matched the tested commit.
Repeating promotion made no second push. HTTPS and host-key-verified
SSH reads and writes succeeded with the Gitaly primary absent. Its
replacement caught up, and all three replicas matched the first push
to a new private repository. The original Rails node returned, both
application backends passed readiness, and the shared upload remained
readable. PostgreSQL restored its three-member synchronous quorum and
the former Valkey writer rejoined as a replica.

The run completed in 44 minutes 45 seconds, removed its guests and
temporary data, and recorded zero out-of-memory events. Its 554 source
hashes and 11 additional fixture-input hashes matched the tested files.
The local launcher keeps a 12 GiB RAM cap and allows 2 GiB swap. It now
copies and verifies HAProxy on Linux storage before guest boot; an earlier
attempt failed while reading the Windows-mounted binary under memory
pressure. Runner and Helm are also staged before guest boot.

This is one-workstation evidence using one ingress HAProxy, one
SeaweedFS 4.53 S3 process, one NFSv4 artifact export and local Gitaly
PVs. The following rehearsal adds entry-proxy failover. Independent host
loss, redundant object storage, artifact-server failover and Ceph
recovery remain unverified.

### GitLab entry VIP failover with Runner and Argo CD on RKE2

The Argo CD counterpart passed using GitLab CE and Runner 19.1.1,
Argo CD 3.5.0, RKE2 v1.36.4+rke2r1 with bundled Cilium 1.19.6,
and the rendered HAProxy 3.2.26/Keepalived 2.3.4 entry pair. PostgreSQL
18.6 and Valkey 8.1.11 provided the shared application backends.
The rehearsal now selects Argo's repository CA entry from the repository
URL hostname, which binds trust to the distinct GitLab VIP. Argo read
the private repository with its deploy token and applied the protected
baseline through that endpoint.

After the same Runner job and pod checked out the exact tested revision,
the test kept the Gitaly primary absent and killed the owning entry proxy.
The original peer acquired the VIP and retained authenticated project,
shared-upload and Git access. The same job then survived preferred-Rails
loss, synchronous PostgreSQL promotion and Valkey writer loss. Pending
CI left the deployment branch and live workload at baseline through
those faults. After the job succeeded, Argo's Application revision and
deployed ConfigMap matched the exact tested commit. A repeat promotion
made no second push.

HTTPS and host-key-verified SSH writes succeeded through the surviving
entry peer. The replacement Gitaly pod caught up, and all three replicas
matched the first push to a new private repository. The original Rails
node returned with the shared upload intact. The recovered entry proxy
reached BACKUP and left the serving peer in ownership for ten seconds.
The test observed at most one owner in 53 samples and attested the five
original/restarted entry daemons.

The run completed in 40 minutes 28 seconds with zero OOM events.
Its 557 source hashes and 15 fixture-input hashes matched the tested
files. PostgreSQL restored its synchronous quorum, the original Valkey
writer rejoined, and all temporary guests, processes and data were
removed. The host namespace's forwarding value was unchanged.

This earlier Argo installation used one replica per component. Argo controller
and repository-service failover were outside that combined case. The lab
also used one workstation, local Gitaly PVs and one S3 fixture; independent
host loss, partition fencing, systemd host lifecycle and Ceph recovery
remain unverified. The rendered plan still reports `ha_verified: false`.

### GitLab entry VIP failover with Runner and Argo HA recovery on RKE2

A follow-up passed with the pinned upstream Argo CD 3.5.0 HA manifest,
GitLab CE and Runner 19.1.1, RKE2 v1.36.4+rke2r1, PostgreSQL 18.6 and
Valkey 8.1.11. The entry pair used HAProxy 3.2.26 and Keepalived 2.3.4.
All four Argo image references were digest-verified and preloaded.
The manifest retained two repository replicas, two API replicas, three
Redis HAProxy replicas, three Redis/Sentinel members and one application
controller. A lab worker selector kept Argo on three guests; the original
Runner job used the reserved control-plane guest. Required anti-affinity
and upstream replica counts were retained.

An independent Application applied a ConfigMap from the pinned baseline
commit. It used the same restricted AppProject and repository deploy
token. During the held CI job, the test kept the Gitaly primary absent,
moved the entry VIP to its peer, stopped the preferred Rails node and
promoted PostgreSQL. It then removed a repository-server pod, replaced
the application-controller pod and removed the Redis primary pod.
The original repository peer remained Ready and present in the Service's
EndpointSlices. The replacement controller recreated the canary with a
new ConfigMap UID. Two surviving Sentinels agreed on an original Redis
standby as primary; its announcement Service was bound to that pod UID.
Argo recreated the pinned canary after each fault. The deployment branch
and main workload stayed at baseline while CI was unfinished.

The same job and pod also survived Valkey writer loss and reported success.
Argo deployed the exact tested commit once; repeating promotion made no
second push. HTTPS and pinned-key SSH writes succeeded while the Gitaly
primary was absent. Its replacement caught up, and all three replicas
matched the first push to a new private repository. The original Rails
node returned with the shared upload intact. The entry proxy returned
to BACKUP without preempting the serving peer. The test observed at most
one VIP owner in 52 samples and attested five entry-daemon processes.

The checked Argo workloads restored their configured replica counts;
Redis had one primary and two linked replicas. PostgreSQL restored its
three-member synchronous quorum and the former Valkey writer rejoined.
The run completed in 47 minutes 51 seconds with zero parent-cgroup OOM
events under a 12 GiB RAM cap and 3 GiB swap allowance. All 21 selected
acceptance checks passed. Its 557 public Python source hashes and 16
selected fixture-input hashes matched. The guests, launcher and owned
temporary data were removed.

This is one-workstation evidence using local Gitaly PVs and one S3 fixture.
The application controller recovers by replacement of its single replica.
Pod removals used normal termination, including Redis's pre-stop handling.
API-replica loss, independent host loss, partition fencing, systemd host
lifecycle and Ceph recovery remain unverified. The rendered plan still
reports `ha_verified: false`.

### GitLab entry VIP failover with Runner and Flux on RKE2

The follow-up combined the optional native entry pair with the real
Rails-pair, Runner and Flux rehearsal. Two HAProxy 3.2.26 and Keepalived
2.3.4 peers ran in separate network namespaces. They used the version 4
rendered configuration, verified Rails HTTPS backends, PROXY-v2 SSH and
one health-gated GitLab VIP. The database and repository routes retained
their separate stable lab address. Keepalived and its pinned dependencies
were staged on Linux storage before guest boot.

After the original Runner job checked out its exact revision, the test
kept the Gitaly primary pod absent and killed the owning entry proxy.
The original peer acquired the VIP. Authenticated project and shared-upload
access continued, and HTTPS and host-key-verified SSH returned the expected
Git revision. The same job and pod then survived preferred-Rails loss,
synchronous PostgreSQL promotion, elected Flux source-controller deletion
and Valkey writer loss. The original Flux standby served the digest-verified
baseline artifact. The deployment branch and live workload stayed at
baseline until CI succeeded.

Flux applied the exact tested commit in its GitRepository artifact,
Kustomization and live ConfigMap; repeating promotion made no second push.
Git HTTPS and SSH writes succeeded through the surviving entry peer.
The replacement Gitaly replica caught up, and all three replicas matched
the first push to a new private repository. The original Rails node
returned with the shared upload intact. The recovered entry proxy reached
Keepalived BACKUP state and left the serving peer in ownership throughout
a ten-second observation window. The test sampled one owner at most in
53 observations and attested all five original/restarted entry daemons.

The run completed in 43 minutes 13 seconds with zero OOM events.
Its 557 source hashes and 14 fixture-input hashes matched the tested
files. PostgreSQL restored its three-member synchronous quorum, the
former Valkey writer rejoined, and all temporary guests, processes and
data were removed. Forwarding changed only in the disposable network
namespace; the host namespace's before/after value was unchanged.

This remains a one-workstation test with local Gitaly PVs, one S3 fixture
and one NFSv4 artifact server. It does not establish independent entry-host
loss, partition fencing, systemd host lifecycle or Ceph recovery.
The Argo CD counterpart is described above. The rendered plan still reports
`ha_verified: false`.

### GitLab Runner and Flux on RKE2 during repository and source-controller recovery

The separate Flux run used RKE2 `v1.36.4+rke2r1`, bundled Cilium 1.19.6,
GitLab CE and Runner 19.1.1, and Flux 2.6.4. The deployment helper
installed six Flux controllers with two replicas each, and all three
workers mounted the shared NFSv4 artifact volume. To fit the lab's
12 GiB limit, the Runner job used the reserved control-plane guest. The
run moved CoreDNS, its autoscaler, and metrics-server to the worker pool
and verified all three rollouts before the Runner job. The fixture now
performs that same placement step.

With the Gitaly primary pod absent, the same Runner job and Kubernetes
pod stayed active through planned synchronous PostgreSQL promotion.
The drill then deleted the elected Flux source-controller pod. Both
source replicas had served bytes matching the baseline artifact digest.
The original standby still served those bytes after deletion and after
leadership recovered. The protected deployment branch, Flux artifact,
and deployed object remained at baseline while CI was unfinished.

After the same job succeeded, the handoff advanced the protected
deployment branch once. Flux reported the exact tested commit in both
its GitRepository artifact and Kustomization, and the deployed object
matched the CI marker. Repeating promotion made no second push.
The original Puma process reconnected over TLS, all Gitaly replicas
caught up, and a new private repository accepted its first push through
the promoted database.

The drill used one Dockerized Rails application with bundled Redis,
local Gitaly PVs, and one NFSv4 export for Flux artifacts on the same
workstation. The follow-up drill used external Valkey.

### GitLab Runner and Flux on RKE2 across PostgreSQL and Valkey failover

The follow-up repeated the RKE2/Cilium, GitLab CE and Runner 19.1.1,
and Flux 2.6.4 drill with synchronous PostgreSQL 18.6 and external
Valkey 8.1.11 with Sentinel. The fixture automatically placed the
DNS and metrics workloads on the worker pool and reserved the first
control-plane guest for the Runner job.

While the Gitaly primary pod was absent and the original job was
paused, the fixture promoted a PostgreSQL standby, deleted the
elected Flux source-controller pod, and stopped the Valkey writer
and its colocated Sentinel. The original Flux standby served the
baseline artifact bytes, and the deployment branch, Flux revisions,
and live ConfigMap stayed at baseline through the faults. The original
GitLab process read and wrote shared state through the same Valkey
mTLS gateway after promotion.

The same Runner job and pod passed. Flux reported the exact tested
commit in its GitRepository and Kustomization and applied the matching
ConfigMap. Repeating promotion made no second push. All three Gitaly
replicas matched the existing repository and the first push to a new
repository. The former PostgreSQL and Valkey writers rejoined as
replicas. The fixture cleaned up with zero out-of-memory kills.

This test used one Rails container, local Gitaly PVs, and a single
NFSv4 export for Flux artifacts. The Rails-pair rehearsal above adds
application process failover. Redundant artifact storage and
independent physical failure domains remain open.

### GitLab Rails with external PostgreSQL and Valkey mTLS

The isolated K3s lab ran GitLab CE 19.1.1 with the rendered external backend
settings. Before database initialization, the lab corrected the mounted
client certificate and key to match the pinned container's `git` group.
The key retained mode `0640`. CloudNativePG restored the same write Service
in 223 seconds while the Gitaly primary was absent. Git HTTPS and SSH
verified the exact revisions, all three Gitaly replicas caught up, and a
second private repository accepted its first push. The former PostgreSQL
primary rejoined as a standby.

Rails wrote a shared-state value through the mTLS gateway and all three
Valkey members returned it. After Valkey promotion, Rails read that value
and wrote a new one through the same gateway. The original Puma process
still served the existing private project, and the former Valkey writer
rejoined as a replica. The lab verified cleanup. This run used one Rails
container, Flannel, and local repository PVs.

### GitLab Runner and Argo CD across PostgreSQL and Valkey failover

The four-VM K3s fixture combined GitLab CE and Runner 19.1.1, CloudNativePG
1.30.1, PostgreSQL 18.4, the external Valkey 8.1.11 mTLS writer gateway,
and Argo CD 3.5.0. With the original Gitaly primary pod absent, the
Kubernetes Runner job checked out the exact protected-branch commit and
paused after checking the deployment manifest.

CloudNativePG promoted a standby and restored the same write Service in
186.7 seconds. The original Praefect routers reconnected over TLS. While
the job was still paused, the fixture stopped the Valkey primary and its
colocated Sentinel. After promotion, the original GitLab Puma process
served the project and Rails read and wrote shared state through the same
mTLS gateway. The gateway rejected clients without a certificate.

The handoff rejected the pending pipeline both before and after the
failovers, preserving the deployment branch and live ConfigMap. The
original Runner job and pod then completed successfully. Argo CD synced
the tested commit, the live ConfigMap matched, and a repeat verified the
same result without a second push. GitLab accepted new HTTPS and SSH
writes while the Gitaly pod was absent. All three Gitaly replicas caught
up, the former PostgreSQL and Valkey primaries rejoined as replicas, and
the fixture cleaned up the guests and temporary Valkey state.

The successful run used Flannel, local PVs, and one Rails instance.
Independent Ceph storage and multiple Rails instances still need live
qualification.

### GitLab Runner and Flux across PostgreSQL and Valkey failover

The four-VM K3s fixture ran the same GitLab, PostgreSQL and Valkey topology
with Flux v2.6.4. A single Praefect Job migrated the fresh database before
the routers started. CloudNativePG 1.30.1 promoted a standby and restored the
same write Service in 190.3 seconds with the Gitaly primary pod absent. The
original Rails process retained project access and read and wrote shared
state through the mTLS gateway after the Valkey writer changed.

While the original Runner job was paused, the handoff checked that the
deployment branch, the Flux source artifact and Kustomization revisions,
and the live ConfigMap remained at baseline before and after both faults.
The same job and build pod then succeeded. The handoff advanced the
protected deployment branch, and the Flux source artifact, Kustomization
and live ConfigMap matched the exact tested commit. A repeat verified that
commit without another push.

The three Gitaly replicas recovered and agreed on the original and newly
created repositories. The former PostgreSQL and Valkey primaries rejoined
as replicas, and the fixture removed its guests and temporary Valkey data.
Earlier attempts encountered a transient first-push HTTP 500 and timeouts
in Rails startup and PostgreSQL rejoin. The fixture now checks the exact
remote ref before retrying a transient first-push failure, creates the
scoped test tokens through GitLab's API, and records which standby-rejoin
condition is pending if it times out.

The run used Flannel, one Rails instance, local PostgreSQL and Gitaly PVs,
and one NFSv4 export for Flux artifacts. It reached the VM lab's 12 GiB
memory limit without an OOM kill. Independent storage, Cilium and the
other runtimes need their own acceptance runs.
