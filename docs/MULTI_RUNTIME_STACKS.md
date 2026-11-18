# Multi-runtime stack expansion

Status: implementation in progress, not a support or production-HA claim.
The existing deployment profiles remain unchanged. Scores and rankings supplied
with the request are selection preferences, not acceptance evidence.

## Requested end state

All ten combinations below need installation, configuration, integration,
upgrade, recovery and live acceptance on RKE2, Docker, native Linux (`raw`),
K3s and existing Kubernetes (`k8s`):

| Stack identifier | Forge | CI | CD |
|---|---|---|---|
| gerrit-zuul-argocd | Gerrit | Zuul | Argo CD |
| gitlab-ce-runner-argocd | GitLab CE hybrid HA | GitLab Runner | Argo CD |
| gerrit-zuul-flux | Gerrit | Zuul | Flux |
| gerrit-tekton-argocd | Gerrit | Tekton | Argo CD |
| gitlab-ce-runner-flux | GitLab CE hybrid HA | GitLab Runner | Flux |
| gerrit-jenkins-argocd | Gerrit | Jenkins | Argo CD |
| forgejo-tekton-argocd | Forgejo | Tekton | Argo CD |
| forgejo-woodpecker-argocd | Forgejo | Woodpecker | Argo CD |
| gitea-tekton-argocd | Gitea Community | Tekton | Argo CD |
| gitea-woodpecker-argocd | Gitea Community | Woodpecker | Argo CD |

Additional forge scope from the supplied lists: OneDev Community, SCM-Manager,
Apache Allura, RhodeCode Community, SourceHut, GitBucket, Kallithea and Gogs.
Edition limitations must be explicit; installing more replicas must not be
presented as a substitute for a supported application topology.

Interpretation pending backend implementation: Docker and native Linux refer
to application hosting, not emulation of Kubernetes controllers. Management
and workload Kubernetes targets must be explicit for Kubernetes-dependent
components. Do not silently install a hidden cluster, ignore the requested
backend, or claim a local container replaces cluster quorum or HA storage.

## Current evidence and gaps

[SourceHut](SOURCEHUT.md) now has account and Git hosting packages for all
five runtimes, exposed by `--forge sourcehut`. The image builds from signed
Alpine packages. The isolated Docker lab initialized both PostgreSQL schemas,
verified account creation and login, and confirmed that Git HTTP returned
the exact public ref and denied a private repository with a stale export
marker. All three Kubernetes packages passed restricted Pod Security
admission in the web-only mode. The opt-in Docker SSH service verified a
private owner push, read-only access enforcement, unknown-key denial, and
host-key and commit persistence after SSH container replacement. K3s and RKE2
VM labs verified first-push private repository creation and access control
through HAProxy TCP listeners. In both runs, a replacement application Pod
served the exact commit from the original local PVC with strict SSH host-key
checking. Native SSH, redundant edge and storage, other SourceHut services,
production foundation and lifecycle verification remain open.

[RhodeCode Community](RHODECODE.md) has a single-active adapter for all five
runtimes, exposed by `--forge rhodecode`. It pins the official 5.14.0 image
and keeps the unauthenticated VCS process on loopback. All three Kubernetes
packages passed restricted Pod Security admission. An isolated Docker test
with SQLite and Valkey verified a private Git write through Celery hooks,
anonymous access denial, and the exact commit and file after replacing all
three application containers. Live cluster and native installs, external
database, gateway, upgrades and backup restore remain open.

[Apache Allura](ALLURA.md) has a renderer for the web app and task worker
across all five runtimes, exposed by `--forge allura`, with a build recipe
for the checksum-pinned 1.22.1 source release. All three Kubernetes packages
passed restricted Pod Security admission. An isolated Docker run used
MongoDB 8.0 and an adapted Solr 9.11 core, served the login page, and initialized
a private project's Git repository through the task worker. Bearer-authenticated
Git HTTPS clone and push, read-only Member permissions, private-repository
isolation, token revocation, and commit persistence across restart passed.
On an isolated two-worker K3s lab, a certificate-verified HAProxy listener
served authenticated Git clone and push and enforced the Member role. A
replacement Allura Pod served the exact private commit from the original
node-local PVC. Live native and RKE2 deployment, Cilium Gateway, Git SSH,
upgrade, backup restore and host failover remain open.

[Kallithea](KALLITHEA.md) has a single-active renderer for all five runtimes,
exposed by `--forge kallithea`, and a build recipe for a pinned upstream revision.
The non-root Docker image with SQLite passed authenticated HTTP Git and
Mercurial push and clone, anonymous access denial and exact revision verification
after restart. All three Kubernetes packages passed restricted Pod Security
admission. Native and Kubernetes runtime, gateway, PostgreSQL, SSH and recovery
acceptance remain open.

[OneDev Community](ONEDEV.md) has a single-active 17.2.0 renderer for all five
runtimes, exposed by `--forge onedev`. Native Linux and the non-root Docker
image passed private HTTP Git push and exact commit recovery after restart.
Native SSH clone and push also passed, with the same host key after restart.
Native OneDev recovered through the same HAProxy endpoint after a planned
promotion of replicated PostgreSQL 18.6. It preserved the private Git ref,
accepted new writes and rejected invalid TLS identity and database credentials.
The native test also restored the stopped server's state and a logical database
backup into separate targets, preserving administrator access and the exact ref.
A separate native upgrade from 17.1.1 to 17.2.0 with HSQLDB preserved private Git
state and a new push, and a cold rollback recovered the original commit.
All three Kubernetes packages passed Kubernetes 1.35 API admission under
restricted Pod Security. The rendered K3s workload used the CloudNativePG
write Service with verified TLS and a mounted password. After the primary pod
was deleted and a replica promoted, the original OneDev container kept the
private Git ref and accepted a new push and project creation. Live RKE2 and
existing Kubernetes deployments, gateway, CI and off-platform recovery remain open.

[SCM-Manager](SCM_MANAGER.md) has a single-active 3.12.2 renderer for all five
runtimes, exposed by `--forge scm-manager`. The native archive and pinned
non-root Docker image passed private HTTP Git push and clone with exact commit
preservation after restart. A native 3.12.1 to 3.12.2 upgrade preserved private
Git state and a new write. A cold rollback restored the previous commit with
the old release. All three Kubernetes packages passed Kubernetes 1.35 API
admission under restricted Pod Security. The K3s VM lab verified private Git
push, Pod replacement with the original PVC and cold restore to a separate
PVC on another worker. The restored instance kept the original account and
commit. Live RKE2 and existing Kubernetes deployments, gateway and CI/CD
acceptance remain open.

[Gogs](GOGS.md) has a single-active renderer for all five runtimes, exposed by
`--forge gogs`. The v0.14.4 native binary passed authenticated HTTP and SSH
clone and push and preserved a private repository's commit and SSH host key
across restart. The pinned non-root Docker image passed HTTP push and restart
persistence. The three Kubernetes packages passed Kubernetes 1.35 admission
under restricted Pod Security. A disposable K3s cluster ran the rendered
workload and verified HTTP and SSH writes, pod replacement on the original
PVC, and cold restore to a second worker's PVC. The repository remained private
and preserved its commit, file contents and SSH host key. Those runtime checks
used SQLite. A separate native Gogs run against three PostgreSQL 18.6 instances
verified database TLS, rejected a wrong hostname, CA and password, and recovered
through the same HAProxy endpoint after a planned promotion. The running Gogs
process preserved the private repository and accepted new Git and database
writes. The rendered K3s workload also connected by DNS to the CloudNativePG
write Service using a mounted CA and a password from a Kubernetes Secret.
After the original PostgreSQL pod was deleted, the operator promoted a
replica. The same Gogs container kept the database endpoint and accepted a new
push and repository creation. Production foundation and stack acceptance
remain open.

[GitBucket](GITBUCKET.md) now has a single-active adapter for the five requested
runtimes. The planner exposes it through `--forge gitbucket`. The upstream
4.48.0 WAR passed authenticated Git HTTP and SSH push and clone, anonymous access
denial for a private repository, and exact ref preservation after restart
in an isolated Linux process. The derived non-root Docker image passed HTTP
clone, push and ref verification after restart. All three Kubernetes packages
passed Kubernetes 1.35 admission under restricted Pod Security. In a K3s VM lab,
the rendered deployment retained a private repository through Pod replacement
on its original local PVC. A cold backup restored the same commit and privacy
settings to a separate PVC on another worker. The runtime checks used embedded
H2. External database, gateway, shared storage and platform integration acceptance
remain open.

The premium GitLab CE profile defaults to single Gitaly. For a fresh instance,
its [private deployment workflow](GITLAB_REPOSITORY_HA.md#optional-fresh-kubernetes-chart-overlay)
can render the TLS-enabled Praefect and Gitaly tier and register GitLab CE and
Runner with Argo CD under their scoped AppProject. A synthetic private checkout
verified the render and application selection. The Kubernetes 1.35 API fixture
admitted the GitLab CE and Runner resources and created both three-replica
StatefulSets. The premium application now orders the namespace, network
policies, and chart bootstrap Jobs for Argo CD. The rendered upgrade guard
passed fresh-install, supported-upgrade, unsupported-upgrade, missing-metadata,
and API-error tests. Live Argo CD reconciliation of the GitLab installation
and complete GitLab stack acceptance remain open for the multi-runtime planner.

The private renderer can also declare dedicated GitLab and Praefect login
roles and retained databases in the shared CloudNativePG application. The
synthetic private render and Kubernetes admission using the vendored
CloudNativePG 1.30 schemas passed. In a disposable K3s VM cluster, the
vendored CloudNativePG 1.30.0 operator applied both roles and databases
on PostgreSQL 18.4. SQL checks verified ownership, role privileges, and
the GitLab extensions. Both roles authenticated over TLS and wrote through
the primary Service. With required synchronous replication to one standby,
deleting the primary pod triggered promotion and both databases retained
their test rows and accepted new writes. The lab used local VM volumes,
operator-generated TLS, and synthetic credentials.

The private renderer also supports `CNPG_SYNCHRONOUS_STANDBYS=1` or `2` to
enable required synchronous replication and failover quorum for the shared
cluster. A separate K3s VM test of the rendered one-standby policy paused
the primary worker and one standby worker. The operator logged its refusal
to promote with only one of two eligible standbys reachable. Returning the
second standby allowed promotion. Both databases retained their committed
test rows and accepted new writes through the Service IP while the original
primary worker was still paused. The original primary rejoined as a standby
and replayed the writes in both databases. The test used local VM disks and
exercised database failover independently of DNS and physical storage.
[Database provisioning](GITLAB_REPOSITORY_HA.md#provision-the-databases-with-cloudnativepg)

The optional [GitLab SSH edge listener](GATEWAY_EDGE.md#gitlab-shell) uses
the same private plan for the advertised clone endpoint, GitLab Shell
NodePort and HAProxy/Keepalived configuration. A local HAProxy process
routed both healthy backends, excluded a non-SSH response, and continued
serving after one backend stopped. An authenticated Git write through the
combined VIP and K3s NodePort also passed in a disposable hybrid lab.
After the active HAProxy stopped, the other Keepalived peer acquired the VIP
and an SSH clone with host-key verification returned the exact commit.
An SSH push through the new owner succeeded with the original Gitaly primary
pod absent. The replacement replica caught up to the final ref.
The first lab used Flannel and a Docker-hosted GitLab CE application reached
through an EndpointSlice. A second lab verified the same SSH workflow with
Cilium 1.19.10 on three K3s controls and three workers. The rendered NodePort
selected two chart-managed GitLab Shell pods on separate workers, and an
unlisted direct client received no SSH banner. The host-key-pinned clone
returned the exact commit after VIP failover. SSH and HTTPS pushes succeeded
with the original Gitaly primary pod absent, and the replacement replica
caught up before the lab cleaned up. This test used Docker-hosted Rails and
a fixture-specific TLS bridge to Praefect. Complete chart deployment and
physical failure testing remain open.

The [live GitLab CE, Runner, and Flux integration test](GITLAB_PIPELINE_VERIFICATION.md#local-gitlab-ce-kubernetes-runner-and-flux-integration)
used GitLab CE and Runner 19.1.1 with the Kubernetes executor and live Flux
controllers on K3s. It rejected queued and failed push pipelines and verified
the promoted commit in Git, Flux, and a live ConfigMap. The test used a single
GitLab instance with bundled Gitaly and a single NFS artifact store.

The [live GitLab CE, Runner, and Argo CD integration test](GITLAB_PIPELINE_VERIFICATION.md#local-gitlab-ce-kubernetes-runner-and-argo-cd-integration)
used the same GitLab CE and Runner releases with Argo CD 3.5.0. It verified
that pending and failed jobs left the protected deployment branch and live
resource at the baseline. The successful job advanced both to the exact
tested revision, and a repeat verified that revision without another push.
The lab used a single GitLab instance and Argo CD application controller.
A separate [K3s scheduler run](GITLAB_PIPELINE_VERIFICATION.md#optional-kubernetes-scheduler)
scheduled a read-only preflight and retained private evidence on a local PVC.
Two Jobs created from the suspended CronJob with promotion explicitly enabled
verified exact Argo CD reconciliation and replay without a second push.
Both the Argo CD and Flux cases verified a controller-scheduled promotion Job,
the exact applied revision, the live ConfigMap, and replay without a second
push. The scheduler retained its proofs on a local PVC. The Argo CD case used
the generated read-only Kubernetes identity. Hybrid HA still needs acceptance.
An [optional Cilium scheduler policy](GITLAB_PIPELINE_VERIFICATION.md#optional-cilium-egress-policy)
also passed on a five-guest K3s lab after the control-plane quorum drill.
The selected Pod was denied an unapproved port at the GitLab address.
Scheduled Argo CD promotion and replay passed with the policy active.
Another [K3s scheduler run](GITLAB_PIPELINE_VERIFICATION.md#credentials-and-verification)
used an NFSv4.1 shared proof claim. A write-enabled Job on the second worker
honored the first worker's advisory lock. After release and successful
promotion, the second worker read the original proofs byte for byte and
verified the applied revision without pushing again. The fixture used one
NFS server, and CephFS and storage-server failover remain open.

A separate [GitLab CE repository failover test](GITLAB_REPOSITORY_HA.md#local-gitlab-ce-repository-failover-test)
verified Git HTTPS reads and writes after Gitaly primary loss, replica catch-up
after restart, and loss of the preferred Praefect router behind HAProxy. This
was a single-host process failover test. The complete hybrid HA stack and
physical failure domains still need acceptance.

The [native Linux and Docker repository host renderer](GITLAB_REPOSITORY_HA.md#optional-native-linux-and-docker-host-bundles)
generates separate Gitaly and Praefect host configurations, digest-pinned
Docker Compose definitions, a GitLab application fragment, and HAProxy TLS
pass-through configuration for a fresh installation.
The [replicated Rails option](GITLAB_REPOSITORY_HA.md#optional-replicated-rails-hosts-for-native-linux-and-docker)
adds two or more application hosts, shared S3-compatible storage and
health-checked HTTPS and SSH entry points. In the local Docker drill,
the surviving Rails node served the same authenticated session,
repository ref and upload after the first node stopped. The session
and upload also survived PostgreSQL and Valkey writer changes.

The [K3s repository runtime test](GITLAB_REPOSITORY_HA.md#local-k3s-repository-runtime)
started the rendered Gitaly and Praefect StatefulSets across three workers,
verified TLS and local PV binding, and read the exact restored ref through
Praefect with the original Gitaly pod stopped. The replacement reused its
volume and rejoined. The optional repository ingress component blocked
ordinary same-namespace pods from both repository ports with the native K3s
policy controller and with Cilium 1.19.10. A six-VM Cilium run also verified
Praefect TLS reconnection and new repository creation after a planned
promotion in a three-process synchronous PostgreSQL 18.6 fixture.
The [chart's migration Job and router startup gate](GITLAB_REPOSITORY_HA.md#optional-fresh-kubernetes-chart-overlay)
also passed on K3s with Cilium 1.19.10 and RKE2 with bundled Cilium 1.19.6.
Both runs verified the exact backed-up ref with a Gitaly pod absent, replica
catch-up, and the original routers reconnecting after planned PostgreSQL
promotion. They used local PVs and a three-process database fixture.
The [separate Rails bootstrap and PostgreSQL promotion test](GITLAB_REPOSITORY_HA.md#gitlab-rails-bootstrap-and-external-postgresql-promotion)
verified that the existing Rails process served HTTPS and reconnected to
the promoted database over TLS, including in a disk-backed run after a
container and proxy restart. The combined RKE2/Cilium drill reached Argo
CD with the canonical offline image imports. The [combined RKE2 Runner and Argo CD test](GITLAB_REPOSITORY_HA.md#gitlab-runner-and-argo-cd-on-rke2-during-postgresql-promotion)
verified the same Runner job and Kubernetes pod across a planned
PostgreSQL promotion with the Gitaly primary pod absent. Argo CD applied
the exact tested commit after CI succeeded, and the third Gitaly replica
caught up. This run used one Dockerized Rails application, bundled Redis,
local PVs and a single disposable workstation.
The [RKE2 Argo CD run with external Valkey](GITLAB_REPOSITORY_HA.md#gitlab-runner-and-argo-cd-on-rke2-across-postgresql-and-valkey-failover)
also kept the original Runner job alive through PostgreSQL promotion and
Valkey writer loss. GitLab read and wrote through the same mTLS gateway.
Argo CD applied the exact tested commit after CI passed. PostgreSQL and
Valkey restored their replica sets and all three Gitaly replicas matched
both the existing and a new repository.
The [RKE2 Rails-pair rehearsal](GITLAB_REPOSITORY_HA.md#gitlab-rails-pair-failover-with-runner-and-argo-cd-on-rke2)
also stopped the preferred application container during the held Runner
job. The standby retained project and shared-upload access. The same
job and pod survived PostgreSQL and Valkey writer changes, and Argo CD
deployed the exact tested commit once. All three Gitaly replicas caught
up, and the original Rails node rejoined. This run used one ingress
HAProxy and one S3 fixture on the same workstation.
The [joined RKE2 entry-VIP and Argo CD rehearsal](GITLAB_REPOSITORY_HA.md#gitlab-entry-vip-failover-with-runner-and-argo-cd-on-rke2)
also killed the owning entry proxy during the same Runner job. The peer
retained project, upload and Git access; the job then survived Rails,
PostgreSQL and Valkey failures. Argo deployed the exact tested commit
once, and the original entry proxy rejoined without preemption. Argo's
own workloads had one replica each and were not deliberately faulted;
independent host and storage failure acceptance remain open.
The [RKE2 entry-VIP and Argo HA recovery rehearsal](GITLAB_REPOSITORY_HA.md#gitlab-entry-vip-failover-with-runner-and-argo-ha-recovery-on-rke2)
also passed with CI on a reserved guest and Argo on the three-worker pool.
During the same held job, an original repository replica remained Ready,
the application controller was replaced, and an original Redis standby
was promoted. Argo recreated an independent pinned canary after each
fault while the deployment branch stayed at baseline. The same job and
pod succeeded, exact-commit promotion made one push, and the checked
Argo replica counts and three-member Redis replication were restored.
Independent host loss, partition fencing and Ceph acceptance remain open.
The [RKE2 Rails-pair and Flux rehearsal](GITLAB_REPOSITORY_HA.md#gitlab-rails-pair-failover-with-runner-and-flux-on-rke2)
also passed. The same Runner job survived Rails, PostgreSQL and Valkey
failover with the Gitaly primary absent. The original Flux standby served
the digest-verified baseline artifact after source-leader deletion. Flux
deployed the exact tested commit once; the original Rails node and all
three Gitaly replicas recovered. The run used a single S3 fixture and
NFSv4 export on one workstation, so independent storage and host-loss
acceptance remain open.
The [joined RKE2 entry-VIP rehearsal](GITLAB_REPOSITORY_HA.md#gitlab-entry-vip-failover-with-runner-and-flux-on-rke2)
added real entry-proxy loss to that same CI job. The original peer took
the VIP and retained project, upload and Git access. The same job then
survived Rails, PostgreSQL, Flux-leader and Valkey failures, and Flux
deployed its exact commit once. All repository replicas recovered, and
the original entry proxy rejoined in BACKUP without preempting the peer.
This used two network namespaces on one workstation; independent host
loss, partition fencing and redundant storage remain unverified.
The [combined RKE2 Runner and Flux test](GITLAB_REPOSITORY_HA.md#gitlab-runner-and-flux-on-rke2-during-repository-and-source-controller-recovery)
also removed the active Flux source-controller pod during the running
job. Its original standby served the verified baseline artifact as
leadership recovered. Flux applied the exact tested commit after CI
succeeded, while the Gitaly primary remained absent. The fixture used a
reserved Runner guest and a single shared NFSv4 artifact export.
The [RKE2 Flux run with external Valkey](GITLAB_REPOSITORY_HA.md#gitlab-runner-and-flux-on-rke2-across-postgresql-and-valkey-failover)
added Valkey writer loss to the same Runner job while the Gitaly primary
was absent. The original Flux standby served the exact baseline artifact,
GitLab retained shared-state access through the Valkey mTLS gateway, and
Flux deployed the exact commit after CI passed. A single NFSv4 export
served the artifacts during the run.
An optional [native GitLab application entry pair](GITLAB_REPOSITORY_HA.md#optional-native-application-entry-vip)
now renders health-gated HAProxy/Keepalived VIP configuration for external
native or Docker Rails nodes. A real two-namespace probe verified proxy
failover, HTTPS and PROXY-v2 SSH, client-header replacement, and withdrawal
on HTTPS loss, SSH loss or upstream certificate hostname mismatch. That
probe used synthetic backends; the separate joined RKE2/Runner cases
above exercised real Rails with both Argo CD and Flux. Physical
entry-host loss remains unverified for this option.
Another run paired a GitLab CE Docker application with the K3s repository
tier. It verified Git HTTPS and SSH pushes while the primary Gitaly pod
was absent and exact ref catch-up on its replacement.
A [combined repository fault test](GITLAB_REPOSITORY_HA.md#postgresql-promotion-during-gitaly-pod-loss)
promoted Praefect's synchronous PostgreSQL standby with a Gitaly replica
still down. The original routers reconnected over TLS. GitLab retained the
pre-promotion ref, accepted an HTTPS push to the remaining replicas, and
created a new private repository after the third replica caught up.
A further [CloudNativePG test](GITLAB_REPOSITORY_HA.md#automatic-cloudnativepg-failover-during-gitaly-pod-loss)
deleted the PostgreSQL primary pod while the first Gitaly primary pod
was absent. CloudNativePG automatically switched the write Service,
the original Praefect routers reconnected, and GitLab read and wrote
the exact refs through the promoted database. Both tiers recovered
their third replicas.

The [shared-database failover and Argo CD drill](GITLAB_REPOSITORY_HA.md#argo-cd-handoff-after-shared-database-failover)
also connected GitLab Rails to CloudNativePG over verified TLS. With one
Gitaly pod absent, the same Kubernetes Runner job survived the database
promotion. The handoff rejected the unfinished job and advanced the
protected deployment branch only after the job passed. Argo CD then applied
its exact commit, and a repeat made no second push. PostgreSQL and Gitaly
recovered their third replicas. The lab used local PVs, a single GitLab
application instance, and its bundled cache.

The [combined PostgreSQL and Valkey drill](GITLAB_REPOSITORY_HA.md#gitlab-runner-and-argo-cd-across-postgresql-and-valkey-failover)
kept a Gitaly primary pod absent while the same Kubernetes Runner job survived
both shared PostgreSQL failover and external Valkey failover through an mTLS
gateway. The pending pipeline held the deployment branch and live workload
at baseline through both faults. Argo CD applied the exact tested revision
after the job succeeded. The former database and cache writers rejoined as
replicas, and all three Gitaly replicas caught up. Full hybrid HA with
independent storage and multiple application instances remains open.

The [equivalent Flux drill](GITLAB_REPOSITORY_HA.md#flux-handoff-after-shared-database-failover)
used Flux 2.6.4 and the same combined Gitaly and CloudNativePG fault.
Following database promotion, the original Runner job completed and the
Flux source artifact, Kustomization, and live ConfigMap matched the tested
commit. Repeating the handoff made no second push. The source artifact
store in this run was an NFS export on one disposable VM.

The [combined failover run with Flux](GITLAB_REPOSITORY_HA.md#gitlab-runner-and-flux-across-postgresql-and-valkey-failover)
also used the external Valkey mTLS gateway. The original Runner job survived
both PostgreSQL and Valkey writer changes with the Gitaly primary absent.
Flux kept the baseline through both faults and applied the exact tested
commit after the job succeeded. The original database and cache writers
rejoined as replicas, and all Gitaly replicas caught up. A single migration
Job also resolved the Praefect startup race in the lab. The
[chart overlay now includes that gate](GITLAB_REPOSITORY_HA.md#optional-fresh-kubernetes-chart-overlay).
The Flux artifact store remained an NFS export on one disposable VM.

An optional [TLS Valkey application gateway](VALKEY_APPLICATION_GATEWAY.md) now
packages authenticated quorum-based primary selection and verified upstream mTLS
for all five hosting formats. A real native Valkey/HAProxy component case verifies
TLS forwarding, preserved backend mTLS, peer-forwarder process loss, reconnect
after primary promotion and checker-loss fail-closed behavior. Full Harbor
applications, Kubernetes worker deployment, independent gateway hosts and full-stack
qualification remain open; existing Harbor/cache defaults are unchanged.

The exact Harbor 2.15.1 cache and legacy-pool constructors also pass a separate
[native client component test](HARBOR_CACHE_CLIENTS.md): scoped authentication,
verified TLS, logical database selection, refusal of wrong credentials/CA, and
reconnect through the same original process after actual primary promotion.
Constructor evidence alone does not close the actual service or full Harbor HA gates.

A separate [Harbor registry daemon fixture](HARBOR_REGISTRY_RUNTIME.md) now runs
the exact selected distribution fork, two authenticated HTTPS processes,
cross-replica OCI uploads, peer process loss and cache promotion/outage. Its
temporary shared local filesystem does not verify Ceph/S3 availability or
independent host loss. Complete-stack gates remain open; library and service
component evidence are kept distinct.

Pinned [Harbor native service builds](HARBOR_SERVICE_BUILD.md) are available
after the release's required API generation, with original source/module locks
unchanged. The separate [actual service integration test](HARBOR_SERVICE_RUNTIME.md)
passes release SQL migrations, live TLS PostgreSQL connections, authenticated
core project APIs and real jobservice dry-run jobs before and after cache
promotion through the same original service processes. Trust/authentication
negatives, original process/data cleanup and ordinary contract checks also pass.
An optional [planned database-promotion component case](HARBOR_SERVICE_RUNTIME.md#optional-planned-database-promotion-and-client-reconnection)
also passes final source-checkpoint replay, original-primary fencing, timeline
advancement, strict three-member TLS/quorum rejoin and original Harbor service
reconnection through an unchanged client endpoint.
The separate [Harbor bearer-token OCI workflow](HARBOR_JWT_OCI_RUNTIME.md)
also passes real core-issued RSA tokens, project-guest pull/write boundaries,
image/config/layer/manifest digest round trips, database-backed artifact tags,
signed-token refusal checks and earlier-token continuity across both promotions.
The test found and wired the required registry-state cache URL without widening
the backend registry account. Native HTTP protocol evidence does not qualify
Docker/Kubernetes image-client deployment or full Harbor HA.
A separate [Harbor core replica-loss component](HARBOR_CORE_REPLICA_RUNTIME.md)
now passes two original core processes behind verified HTTPS health routing,
direct token/image access on each peer, exact primary SIGKILL, fresh work through
the survivor, and continued cache/database promotion. The combined case exposed
old connections to the database node after it became a standby; the local route
now fences sessions before handover and reopens only after strict new-primary
verification. This does not establish independent core/entry/storage hosts or
jobservice replica failover.
The stronger [jobservice replica/SQL continuity case](HARBOR_JOBSERVICE_REPLICA_RUNTIME.md)
currently fails on the original worker's first real SQL audit read after database
handover (`unexpected EOF`). Earlier audit dry runs omitted an event-type filter
and could complete without a SQL query; their success must not qualify this gate.
The [separate local pool-health candidate](HARBOR_DB_POOL_HEALTH.md) passes the
combined process/SQL workflow with distinct patched-binary pins; it does not
qualify the original release, upstream support, independent shared storage or
physical HA. Production worker recovery and full stack acceptance remain open.
This remains local component evidence: automatic database failover/operator
integration, service replica failover, independent storage, full SSO/MFA/admission and image-client deployment,
upgrade/restore and physical failure/load qualification are still open.

A separate [Harbor CI robot helper](HARBOR_CI_ROBOT.md) now creates one expiring
project-only pull/push identity, retains its private one-time secret response
and resumes only its original ID/ownership marker. The actual native case passes
OCI publication/read-back across both peer losses and both promotions while
refusing delete, project administration and cross-project access. It consumes
the separately pinned candidate build; it does not qualify the original release
or actual CI worker secret delivery/publication, signed-image admission or a
full stack. Existing identities and deployment profiles are not changed by
rendering or preview.

An optional [prepared Gerrit pair activation adapter](GERRIT_ACTIVATION.md)
now supports explicit fresh `rke2`/`k3s`/`k8s` workload creation and retained-handle
resume. It pins target/node/claim identities and requires both site-check Jobs
to complete before daemon creation. Modeled failure/recovery tests and actual
disposable Kubernetes API admission/create/read-back pass. No live Gerrit/Ceph
pair, peer/write/failover acceptance or complete stack is established by this
component. The separate full storage lab is not available yet; its provider
requirements have not been reduced to fit the local machine.

The separate [Docker/native Gerrit activation adapter](GERRIT_HOST_ACTIVATION.md)
now creates a prepared member on its pinned host and resumes only its original
unit/container and invocation. It checks private inputs, real CephFS scope and
runtime policy, then requires original listener ownership, verified HTTPS health
and a matching presented SSH host key. Recovery behavior is tested with modeled
hosts; a real disposable Docker engine verifies create/inspect policy without
starting the synthetic container. Actual application startup, remote pair
orchestration and Ceph/Gerrit HA acceptance are still open.

The [prepared Gerrit host-pair coordinator](GERRIT_PAIR_COORDINATION.md) now
adds closed-tool SSH delivery, two-host preflight, original remote reservations,
PID-1-owned jobs and fresh final observations for Docker/native members.
Modeled coordinator/agent failure tests and an actual transient-systemd job
surviving launcher exit pass. The coordinator still needs real SSH delivery
and end-to-end startup and failover verification on separate Ceph-backed hosts.

The [version 2 Gerrit site profile](GERRIT_SITES.md#version-2-health-configuration-guard)
now binds the CephFS scope and private healthcheck configuration for all five
runtime packages. Required probes must remain explicitly enabled with bounded
timeouts, disk-space policy and private authentication-check inputs. Preparation
and initialization check the actual configuration before writes and preserve
credential-bearing health inputs across initialization. This closes a startup
policy gap; it does not qualify the live plugin, shared storage or Gerrit HA.
The [separate real HAProxy protocol test](GERRIT_ROUTING.md#optional-actual-haproxy-protocol-test)
passed in 65.103 seconds with 352 source hashes and independently verified
original master/worker cleanup. Its HTTPS/SSH backends and writes are modeled;
real Gerrit/Ceph integration remains required.

Version 2 now additionally inspects bounded WAR/JAR metadata before initialization
or daemon startup. An actual stable WAR passes its embedded-version check; three
inspected stable-branch CI JARs declaring a snapshot API were rejected despite
valid artifact hashes. A separate audited canonical-source build produces exact
3.13.10 API HA, healthcheck and global-refdb JARs, with immutable private hashes
and preserved source/tool inputs. Compiler identity, source revisions, metadata
and terminal cleanup were independently inspected. These are build/identity
prerequisites. See the [artifact policy and build scope](GERRIT_SITES.md#exact-stable-artifact-identity).

The [Gerrit routing fixture](GERRIT_ROUTING.md#routed-git-uploads-against-gerrit)
runs two Gerrit 3.13.10 processes with the corresponding reviewed plugins,
separate Lucene indexes, shared local Git repositories, and HAProxy 3.2.26.
It exercises peer forwarding, HTTPS and SSH review uploads, primary loss,
startup reindex while the rendered router holds the preferred peer,
and failback after both sites verify the outage revisions. The fixture uses
two independent routers, restarts a held router while both sites are healthy,
and rejects partial hold and release rollouts until both routers agree.
The [client entry check](GERRIT_ROUTING.md#verify-the-client-facing-entry-address)
also verifies the expected HTTPS peer and an exact Git patchset over SSH
through each local router.
A [separate integrated VIP drill](GERRIT_ROUTING.md#exercise-vip-movement-in-the-isolated-lab)
uses real HAProxy and Keepalived in two local network namespaces to verify
Gerrit patchset reads and new review uploads after entry-router loss and
again after preferred-peer loss. Each new patchset is fetched over the other
Git protocol.
The drill also interrupts HTTPS and SSH pushes before Gerrit receives
the update and after it stores the review. It retries the original commit
after checking absence in the first case and fetches the already accepted
patchset in the second.
The [native VIP bundle](GERRIT_ROUTING.md#render-and-install-the-native-vip-pair)
now generates both Keepalived configurations, their common health helper
and the systemd unit. The drill consumes the bundle's daemon configurations.
The native systemd test also installs the units at their deployment paths
in two isolated Debian guests and checks startup, VIP transfer, withdrawal,
and recovery with modeled backends. It applies the primary hold one host at
a time and then restores the regular bundle. The router check rejects both
partial rollouts, and both protocols reach the intended backend through the
VIP after each completed change.
It checks VIP withdrawal when both Gerrit peers stop.
The CephFS pair, physical failure domains, and production entry-point
coordination still require live acceptance.

The optional [Gerrit/Tekton review submitter](TEKTON_GERRIT_BRIDGE.md) adds
bounded authenticated current-patch-set discovery and manual event submission
for all five hosting formats. It binds project/branch/change/ref/commit,
performs server dry-run and uses retained reservation/run UIDs for deduplication.
The job fetches and verifies the Gerrit ref before building a detached commit.
The separate [result reporter](TEKTON_GERRIT_RESULTS.md) now checks the original
run/TaskRun/Pod/worker chain and exact current review revision before optional
Verified votes, with separate credentials and retained publication handles.
The [merged-commit Argo CD handoff](GERRIT_GITOPS_HANDOFF.md) separately requires
the original successful CI/publication receipts, an exact merged patch-set SHA
and the current source head before a normal deployment-branch fast-forward.
A disposable real Gerrit 3.13.10 fixture verifies native-token authentication,
review/ref checkout, actual review publication and merged-commit Git promotion.
Its protocol-only case models Argo CD status. A separate native Argo CD 3.5.0
case now exercises repository/application-controller processes against a real
disposable Kubernetes API, verified repository/cache TLS with client certificates,
and exact-revision ConfigMap reconciliation. It also checks that the promoter
cannot write the Application. That Gerrit/Argo CD case with a standalone API still models
Tekton controller/worker records. Separately, the
[KVM-backed K3s worker case](K3S_VM_WORKERS.md) now passes actual scheduler,
kubelet and container execution, real Tekton controller installation,
fail-closed webhook checks and an explicitly requested infrastructure
PipelineRun. It verifies the real TaskRun/Pod/worker/result chain and removes
the private guests before recording source-bound evidence. Its fresh-namespace
preview creates nothing; explicit installation bootstraps reviewed namespaces
before full admission validation. A further scoped case now passes native
Gerrit discovery, exact patch-set checkout, two real application tests on a
worker, evidence-gated bot voting, an actual failing build with a negative vote,
replay and stale-patch-set rejection with
separate restricted identities. A further accepted case now consumes a real
worker result and native bot vote, merges the exact tested commit, exercises
the production fast-forward handoff and observes actual Argo CD reconciliation
of a restricted two-worker HTTP workload. It verifies live ownership, image
identity, credential absence, endpoints and HTTP responses, denied promoter
Application writes, read-only preflight and idempotent promotion. Its private
receipt matched 337 Python sources after native/VM cleanup. These helpers are
invoked manually in that serving case. A separate accepted native watch case
now starts the packaged submitter/reporter entry points as distinct non-root
transient services with private read-only mounts and the rendered hardening.
They automatically discover and execute two real patch sets, report positive
and negative native votes, and survive forced process restarts without duplicate
runs or reviews. Its receipt matched 338 Python sources, and independent
inspection confirmed removal of units, credential stores and native/VM scopes.
The fixture uses ephemeral systemd identities. A further joined case now passes
automatic polling/voting, actual worker tests, service restart/replay and
operator-approved promotion through the packaged CLI to a real two-worker
Argo CD serving workload. Its promoter uses a dedicated read-only CI identity
with verified write/Secret denials and four separate non-root private journals.
The receipt matched 340 Python sources after independent unit/credential/native
and guest cleanup. Normal fleet installation with static service accounts,
unattended release authorization, built artifact publication and fleet CI/CD
remain unverified.
An optional [promotion reader access package](GERRIT_GITOPS_HANDOFF.md#optional-promotion-reader-access-package)
now renders separate CI and CD ServiceAccounts and read-only RBAC from the
original handoff, explicit control/Application namespaces and reviewed worker
inventory. It commits both management targets and manifest hashes without
creating credentials or applying resources. The manifests require separate
target review and effective-permission checks; rendering does not certify a
deployment or remove unrelated grants on an existing identity.
The joined worker case now provisions those generated roles, verifies all ten
live objects against the source-bound access package, and requires denial of
out-of-scope reads as well as writes. This is scoped laboratory acceptance;
separate physical CI/CD clusters and fleet credential delivery remain open.
Fleet serving/failover probes, multi-node HA and end-to-end
failure/load/upgrade/restore acceptance remain open. These scoped results do not
certify the complete stack or a Kubernetes distribution.

A separate [five-guest foundation case](K3S_VM_WORKERS.md#three-control-plane-quorum-and-fixed-api-entry)
now passes actual three-server embedded-etcd membership, original leader VM
loss/election, acknowledged writes with two voters, unacknowledged writes with
one voter, stable verified API access, worker HTTP continuity and original
member/node recovery. Its source-bound 341-file receipt and independent cleanup
audit cover virtual control-plane outages. It still uses the fixture's Flannel
networking and one HAProxy process. Physical failure domains, redundant API
entry, the requested Cilium/Ceph foundation and full-stack acceptance remain
unverified; no readiness flag is promoted by this laboratory result.

The optional [Keycloak runtime package](KEYCLOAK_HA.md) now provides two or three
members with an explicit PostgreSQL 17 endpoint, TLS verification, encrypted
database-discovered cache transport and a separate new-realm browser MFA
definition for all five runtimes. Kubernetes packages include domain placement,
disruption and network policies; Docker/native packages require separate hosts
and private credentials. Local API admission/read-back is exercised without
workers. A separate real two-process Linux fixture now checks strict TLS,
OTP enrollment/login, negative authentication and refresh through the second
process after stopping the first, using one disposable PostgreSQL 17 database.
It exposed and corrected build-time flags wrongly passed to optimized startup;
the build recipe is now separate. Ordinary ORM parameter dumps are suppressed
and private runtime diagnostics classified without raw values. The separate
[fresh Keycloak workload installer](KEYCLOAK_INSTALLATION.md) now pins explicit
Kubernetes targets, nodes, immutable Secret metadata and the prepared Cilium
agent/configuration. It reviews admission, creates policy/references before the
Deployment and observes original UIDs after lost acknowledgments. Simulated
controllers and real isolated API admission cover this path. The separate
[new-realm bootstrap helper](KEYCLOAK_REALM_BOOTSTRAP.md) now creates a dedicated
MFA realm, verifies nested browser executions and OTP enrollment policy, refuses
existing realms, and resumes original realm/flow handles after uncertain writes.
The disposable TLS runtime fixture exercises the actual helper before its OTP
and session checks. A separate [Argo CD identity handoff](KEYCLOAK_ARGOCD.md)
creates one confidential client with the original MFA flow, exact callback,
S256 PKCE and full group claims, then renders private, unapplied Argo CD
OIDC/trust/role settings with no default readonly grant. Original client/mapper
handles and credentials are checked on resume; existing clients are refused.
The separate [application helper](ARGOCD_IDENTITY_APPLICATION.md) consumes that
original handoff, reviews admission on an explicit management cluster, creates
one immutable credential Secret, patches the original settings/RBAC ConfigMaps
and requests a reviewed rolling restart. Deployment spec/UID, project roles,
admission hashes and uncertain-write handles are pinned. An isolated real API
fixture exercises these operations and refuses recreation of deleted handles.
A separate native integration fixture runs real Argo CD, Keycloak, PostgreSQL,
Valkey and Kubernetes API processes. It exercises the actual configuration
helper and server restart, MFA login, readonly project access, rejected reader
writes, administrator project creation and denial without a mapped group. It
exposed stale OIDC CA trust retained across listener reloads; the helper now
requests a guarded Pod-process restart. This single-host fixture uses
synthetic Deployment readiness, not worker Pods or physical failure domains.
Actual fleet deployment and end-to-end HA still need acceptance. A separate
[host activation path](KEYCLOAK_HOST_ACTIVATION.md) now creates/starts one
prepared Docker/native member, binds original host/boot/artifact handles,
observes cgroup-owned TLS readiness and refuses to repeat uncertain writes.
Simulated runtime tests cover both paths; a real isolated systemd/TLS fixture
checks the service-control and listener mechanics. A separate distribution
fixture now activates real optimized Keycloak through the native helper,
verifies original-invocation resume and exact kernel TLS listener ownership,
and removes the scoped service and files. A separate real Docker workload
fixture has passed optimized Keycloak 26.4.7 preparation and activation through
the same host helper, original-container resume, kernel-owned TLS listeners,
OIDC discovery and cleanup. Its actual Argo CD test creates a separate MFA
realm through that activated container, verifies reader access and denied
writes, administrator writes and denial without a mapped group. Native and
Docker application checks share the same acceptance implementation. Explicit
public HTTPS ports are bound in the package; the client helper refuses
mismatched issuer ports before API access.
A separate disposable Docker-engine fixture stages a pinned engine on its own
local socket and checks actual container kernel isolation with a synthetic
probe. The host helper accepts explicitly pinned local CLI/socket paths. This
engine prerequisite alone does not establish application startup or
authentication; the separate workload case supplies that single-host evidence.
Its temporary loopback HTTP registry carries a public fixture image and does
not verify Harbor, registry TLS or publisher signatures. Live fleet installation,
multi-host lifecycle, database/bootstrap binding, master-realm controls,
deployed application clients, physical-node/database failover, upgrades and
independent recovery remain open; this does not certify shared identity.

The optional [OpenBao Raft package](OPENBAO_HA.md) now provides three-member
TLS/join/HMAC-audit configuration for all five runtimes, with separate data and
audit storage, constrained service identities and explicit peer/client network
boundaries. It does not initialize, unseal or install production workloads.
The separate [Kubernetes installer](OPENBAO_INSTALLATION.md) now pins the
explicit cluster, nodes, immutable TLS references, retained Ceph RBD claims and
Cilium rollout before create-only installation. It preserves original resource
and process handles and leaves external ceremony/HA controls unverified.
The opt-in real Raft fixture checks synthetic local initialization, restricted
access, root revocation, process loss/restart and snapshot capture. A separate
[snapshot helper](OPENBAO_SNAPSHOTS.md) now binds TLS/source membership, exact
read-only backup scope, bounded streaming and offline archive integrity checks.
The real fixture additionally exercises recovery into a fresh isolated data
scope after every source process is stopped; this remains single-host evidence.
The [read-only runtime verifier](OPENBAO_RUNTIME.md) observes pinned direct TLS
member identities, voting membership, selected canary reads/denials and enabled
audit configuration. Its actual three-process acceptance passed; it does not
certify every workload's authentication, audit delivery or physical-node HA.
The [host activation helper](OPENBAO_HOST_ACTIVATION.md) now creates one fresh
Docker or native member from the exact package and observes its original
uninitialized TLS process. It pins host/boot, prepared TLS, storage and runtime
handles and never reissues uncertain writes. Actual native and disposable-image
Docker acceptance passed; external ceremony and full-fleet HA remain open.

The optional [Valkey replication/Sentinel package](VALKEY_HA.md) adds TLS-only
data/coordination configuration for all five formats, three data members and
three Sentinels, restricted canary/discovery identities and preserved mutable
role state. Its private materializer keeps outbound authentication files out
of public configuration. Application-specific client/ACL integration,
failover/loss-window acceptance and independent restore remain
required; protocol-level checks cannot certify all application compatibility.
The separate [Kubernetes installer](VALKEY_INSTALLATION.md) now pins prepared
TLS/auth Secret and Ceph/Cilium identities, binds concrete server admission and
creates eleven original resources. Its operation-bound mutable seed refuses
state from another installation, and resume observes original resource/process
handles without reissuing uncertain creates. Serving status remains separate
from TLS, replication and application acceptance.
The [runtime verifier](VALKEY_RUNTIME.md) now observes pinned direct mTLS member
identities, replication links and complete Sentinel quorum, with an optional
expiring replicated canary. The real six-process fixture exercises this helper
before failure and after original-member rejoin. Application compatibility,
lossless writes and physical-node HA remain unverified.
The [native/Docker host helper](VALKEY_HOST_ACTIVATION.md) now activates one
prepared server/Sentinel pair with pinned host/storage/artifact identities,
private operation-bound seeds and original service/container invocation custody.
It refuses automatic replay of uncertain seed/create/start actions and does
not claim group availability from an individual pair's TLS startup.
The optional [Argo CD client helper](ARGOCD_VALKEY.md) prepares dedicated-group
Sentinel/mTLS arguments and private application credentials across all five
cache formats. It refuses shared client namespaces and does not apply a live
workload change. App-specific runtime evidence remains separate from generic
Valkey readiness and from the other requested applications' compatibility.
Production
custody, complete live installation, physical-node HA, auth integration, upgrades and
off-platform restore remain open.

The optional [PostgreSQL HA policy package](POSTGRES_HA.md) declares strict
synchronous replication, three-domain placement and TLS trust for CNPG on the
three Kubernetes runtimes and Patroni on Docker/native Linux. A separate
non-root helper can verify private TLS material and build an immutable runtime
credential file. A separate guarded Kubernetes installer now checks the exact
target/operator/schema, immutable Secret snapshots, admission policy and fresh
data scope before create-only installation. Its journal observes the original
resource after acknowledgment loss without repeating creation or adopting data.
A separate read-only runtime verifier checks the original CNPG database versions,
system identity, active durability settings, TLS streaming peers and quorum,
rejecting changed pod/process/storage identities during observation. Its fixed
SQL has been exercised on disposable PostgreSQL processes; it does not perform
application writes, promotion, quorum-loss or Kubernetes HA acceptance.
A separate planned CNPG switchover helper now pins the original installation
and runtime evidence, rechecks live SQL/topology and submits one guarded status
request. Interrupted acknowledgments resume observation of that same request.
Its scoped completion flag does not establish fencing, write survival or HA;
the operator transition has been simulated and its JSON patch exercised with
real client-only kubectl, while live cluster acceptance remains open.
The Docker and native Linux packages now include per-member Patroni startup
units and Compose services with private state identity, an exclusive lock and
required watchdog access. The packaged preflight passed with the reviewed
Patroni 4.1.5 wheel and PostgreSQL 18.6. The wheel enables ZooKeeper hostname
verification. A live ZooKeeper endpoint accepted its certified name and
rejected a second DNS alias for the same listener. A separate three-guest native
test ran the generated Patroni 4.1.5 and ZooKeeper 3.9.7 services with
PostgreSQL 18.4. A watchdog fenced the original primary. A survivor promoted
under synchronous quorum, preserved the committed row and replicated a second
write. The original member rebooted, rejoined as a standby, and received a
third write along with both survivors. A separate three-guest run exercised
the same recovery sequence with the rendered Docker Compose services after
the registry was stopped. Both runtime paths subsequently passed application
database provisioning and access isolation tests. Two application logins wrote
to their respective databases after promotion and could read those writes on
the rejoined standby. Both runtime paths also verified client reconnection
and writes through the generated two-router HAProxy endpoint after a router
outage and a PostgreSQL primary promotion. The native and Docker Compose
Forgejo and Gitea services retained their repositories and wrote new files
across both faults using their dedicated TLS database connections. Other
application clients, physical HA and complete database/application acceptance
remain open for this endpoint.
An optional [physical backup helper](POSTGRES_BACKUPS.md) can now capture a
prepared PostgreSQL source for any of the five runtime choices using a separate
Linux runner, verified TLS, pinned database/tool identities and required WAL
integrity checks. Real disposable capture and corruption tests exercise this
path. A separate [database restore drill](POSTGRES_RESTORE.md) boots a fresh
verified backup copy, replays required WAL, verifies committed canary data and
checks clean shutdown. Real disposable recovery and wrong-witness tests cover
this path. Source account provisioning, independent storage, complete
application/cluster restoration and RPO/RTO acceptance remain open.

The working tree contains RKE2 bootstrap playbooks and Forgejo, Gitea,
GitLab CE, Woodpecker, GitLab Runner and Argo CD Kubernetes templates.
It also contains Rook/Ceph and HAProxy/Keepalived alternative examples.

The optional [Forgejo and Gitea host renderer](FORGE_HOST.md) adds native
systemd and Docker Compose artifacts for a single active instance. It uses
loopback HTTP and SSH ports, persistent state, and separately provisioned
credentials. The planner lists these host artifacts for the four relevant
stacks while tracking CI/CD integration and full-stack readiness separately.
Both forges passed a [PostgreSQL and filesystem restore drill](FORGE_HOST.md#back-up-and-restore-a-host-forge-with-postgresql)
with fresh native and Compose targets. The recovered services retained account
access, read-only collaborator permissions and the backup-time Git refs.
The [host upgrade workflow](FORGE_HOST.md#upgrade-a-prepared-host-forge) has
also been exercised on both runtimes with Forgejo 15.0.6 to 15.0.11 and
Gitea 28.2.0 to 28.3.0. Both accepted SSH pushes after the upgrade and after
recovery on the former release from the pre-upgrade backup.
The [Kubernetes update workflow](PLATFORM_UPDATES.md#maintain-a-single-active-forge-during-an-upgrade)
also passed Forgejo and Gitea upgrades and backup recovery with vendored
charts and local claims on two workers in disposable K3s labs. Forgejo used
CloudNativePG with `verify-full` TLS and Gitea used SQLite.
The [Forgejo and Gitea Compose cases](TEKTON_FORGE_RESULTS.md#docker-compose-forge-with-k3s)
verified signed private pushes through Tekton and native Argo CD on disposable
K3s clusters, then checked the repositories and results after container replacement.
The [Woodpecker Compose cases](CI_GITOPS_HANDOFF.md#docker-compose-forge-with-woodpecker-and-k3s)
also verified OAuth, private pipelines on a Helm-rendered agent, and native
Argo CD deployment to two workers in the same cluster.
The [Woodpecker server host renderer](CI_GITOPS_HANDOFF.md#run-woodpecker-on-a-docker-or-native-linux-host)
adds Docker Compose and systemd packages for a single scheduler. Both Compose
forge tests also passed with the rendered Woodpecker container, including
authenticated read-back of the same completed pipeline after replacement.
The [Argo CD Docker reconciliation pair](CI_GITOPS_HANDOFF.md#run-argo-cd-reconciliation-from-docker)
also deployed the exact tested revision from both Compose forges to K3s.
These cases used an external Kubernetes API, a Helm agent, and a native
mTLS Valkey endpoint. Both forges also passed the optional Argo CD API/UI
test over HTTPS, with a project-scoped observer and sync denied.
The optional Docker ApplicationSet controller generated and updated the
Application for both forges using the List generator and its own namespace
role. The generated Application synced the tested commit to both K3s workers.
The optional notifications service delivered the exact synced commit for
both forges to an authenticated HTTPS receiver with a private CA.
After an outage during sync, replacing the service delivered the pending
notification. A second replacement processed a new trigger and suppressed
the previously recorded delivery. These were single-host recovery tests.
The optional Sentinel client was exercised with both forges and Woodpecker.
After Valkey primary loss and promotion, the Forgejo run recovered after a
repo-server replacement and the Gitea run after an application-controller
replacement. Both runs verified manifest regeneration, resource-tree reads
through the original API, and the former primary's rejoin as a replica.
With the optional mTLS gateway, both stacks recovered manifest generation
and resource-tree reads with the original Argo CD containers. The test used
a surviving gateway process after stopping its peer on the same lab host.
Both runs also passed through a Keepalived VIP shared by two isolated
gateway namespaces. The standby took over from the failed gateway and
Argo CD recovered after Valkey promotion through the same address.

The [Ceph client check](CEPH_CLIENT_CHECK.md) adds fresh-claim RBD handoff and
concurrent CephFS data-path acceptance on an explicit Kubernetes provider.
The version 2 storage plan carries the original bootstrap operator identity
across separate operator/provider namespaces and distinguishes physical OSD
hosts from CSI access workers. Controllers must span approved access domains,
and node plugins cover the full approved access inventory; capacity and CRUSH
checks remain tied to OSD hosts. Simulated tests exercise this split topology.
Native/Docker mounts, node-loss recovery and complete-stack acceptance remain
separate requirements.

The [native CephFS peer check](CEPH_NATIVE_CHECK.md) now packages raw Linux and
restricted Docker-bind client runs against prepared encrypted subtree mounts.
It pins host/boot identities, source programs and the original storage proof,
keeps the checked mount descriptor open during data exchange, and rechecks the
provider before accepting both peer receipts. Real local POSIX tests cover the
file protocol; Ceph mount and provider behavior remain simulated. A managed
Docker helper now pins the local engine/image/binary, verifies each original
container before start and uses separate durable create/start intents for
interrupted requests. Docker pair acceptance requires both execution proofs;
the lifecycle is simulated and its socket/package metadata checked locally.
Client/key provisioning, native RBD, live Docker/Ceph execution and
storage failure acceptance remain open.

The [native CephFS mount helper](CEPH_NATIVE_MOUNT.md) adds one explicit
mount request on a reviewed root Linux host for raw/Docker clients. It checks
the helper digest, kernel/boot/namespace and private credential-file handles,
uses encrypted v2 transport and records intent before the request. Resume
observes matching kernel mount state instead of repeating an uncertain mount.
Real Linux tests cover private credential descriptors and mount-table reads;
the mount action itself is simulated. Package/key provisioning, boot-time
mount services and live failure/recovery acceptance remain open.

The [fresh Rook/CSI bootstrap](ROOK_BOOTSTRAP.md) adds pinned APIs and runtime
access grants, one Rook operator, two leader-elected CSI operator replicas,
redundant CSI controllers and node plugins. It hands immutable configuration
identities to provider creation. The isolated four-node K3s run verified
operator reconciliation and both driver registrations on every node.
Live provider, client and failure acceptance remain required for the full
stack and runtime matrix.
These are starting points, not proof that a requested full combination is ready.
The [Ceph storage provisioner](CEPH_STORAGE.md) can now create finite replicated
RBD/CephFS pools and expandable Retain classes on a pinned prepared Rook provider.
It checks failure domains, redundant access services and conservative recovery
headroom, with per-object intent/UID tracking for interrupted requests. All
five consumer selections can name an explicit provider management target;
provider installation, Docker/native client mounts, live I/O and storage
failure/recovery acceptance remain unfinished.
The [fresh-provider creation helper](CEPH_PROVIDER.md) now adds pinned node/boot
identities, complete read-only blank-device scans and guarded CephCluster
creation. The isolated K3s provider run verified all four 64 GiB scans with
live probe Jobs and verified refusal on a nonblank disk before cluster
creation. Physical-device association, Ceph daemon initialization, storage
I/O and failure acceptance remain open.
The [Gerrit entry-point adapter](GERRIT_ROUTING.md) now renders HAProxy routing
artifacts for all five runtimes. It does not yet install Gerrit or validate a
complete Gerrit/CI/CD stack; the planner reports it as an auxiliary adapter.
The [initialized Gerrit site packages](GERRIT_SITES.md) add startup artifact
locks, separate local state and shared Git mounts for all five runtimes.
Ceph-scoped plans now check the shared cluster/filesystem/subtree and encrypted
kernel mount before initialization writes or Java startup, and bind Gerrit's
repository and HA shared-directory configuration to that mount. The immutable
guard/policy files are included in Kubernetes, Docker and native packages;
legacy unguarded plans remain explicitly unverified for this architecture.
Fresh installation, configuration compatibility and integrated HA acceptance remain
open requirements; these packages do not make a full Gerrit stack ready.
The [fresh native initialization helper](GERRIT_INITIALIZATION.md) adds an
explicit, guarded preparation path for new sites and empty shared storage;
[a disposable Linux drill](GERRIT_INITIALIZATION.md#local-verification)
created the shared repositories with Gerrit 3.13.10, preserved the supplied
private files and started both prepared daemons with the HA plugin on one
host. It used a local directory and a schema version 1 plan.
[Container preparation workloads](GERRIT_CONTAINER_PREPARATION.md) now package
that same helper for Kubernetes and Docker. The Docker preparation service
passed an offline toolchain build and live initialization with bound local
directories and a persistent draft. Real K3s and RKE2 Jobs on local claims
passed restricted admission, created both site directories and shared Git
refs, and refused a repeat initialization. The RKE2 run used a dedicated
worker and bundled Cilium. The existing-Kubernetes selection renders the
same Job. Independent existing-Kubernetes execution, CephFS and full stack
acceptance remain unverified.
The [K3s preparation-to-daemon drill](GERRIT_CONTAINER_PREPARATION.md#handoff-and-remaining-acceptance)
verified two rendered Gerrit Deployments on distinct workers with shared NFSv4
Git storage. The surviving Pod accepted a new SSH review upload and served
both exact review commits after the first Pod stopped. Both were contacted
through direct port-forwards. A [separate routed test](GERRIT_ROUTING.md#verify-route-selection-with-the-prepared-k3s-pair)
used both generated HAProxy replicas, verified the fallback peer's TLS identity
and exact SSH review refs, and exercised the all-backends-down state. The
[joined Cilium drill](GERRIT_ROUTING.md#verify-https-and-ssh-failover-with-cilium)
verified exact Git review refs and Gerrit peer certificates through Envoy on both
workers and SSH writes through a separate policy-scoped NodePort. The
[shared VIP drill](GERRIT_ROUTING.md#verify-review-uploads-through-the-shared-vip)
then verified authenticated review writes after loss of the VIP-owning HAProxy
and the preferred Gerrit Pod. The edge peers used separate network namespaces
on one kernel. Independent-host and shared-storage failover remain open.
The [Zuul SSH launch boundary](ZUUL_SERVICES.md#startup-contract) now requires
parseable pins for every configured Gerrit SSH endpoint before daemon exec,
using the selected runtime's own interpreter and Paramiko lookup semantics.
The optional pinned 14.3.0 transport case passes actual Gerrit SSH authentication,
changed-key rejection, missing/wrong-port refusal before network opening, exact
upstream source/dependency checks and original process cleanup. It does not
qualify scheduler jobs, event streams, other releases or full CI/HA integration.

The [Zuul configuration contract](ZUUL_CONFIGURATION.md) now provides private
Gerrit connections, explicit tenant trust boundaries and local secret/TLS-input
checks. [Zuul service packages](ZUUL_SERVICES.md) add core runtime artifacts and
a guarded native executor path. [Optional container executors](ZUUL_EXECUTORS.md)
add dedicated placement and host-policy references without a privilege fallback.
Live executor isolation, workers, event recovery and live CI remain open.
The [static worker-supply contract](NODEPOOL_STATIC.md) adds pinned host identity,
declared failure domains and explicit tenant label restrictions. It does not yet
prove worker allocation, reset/isolation or recovery. [Nodepool launcher
packages](NODEPOOL_SERVICES.md) now add guarded artifacts for all five runtime
targets, still without live allocation/HA acceptance.
The [review-to-worker canary](ZUUL_CANARY.md) adds a concrete trusted pipeline
and remote worker playbook for one explicitly selected Gerrit project. It reports
comments without review votes or submission. Rendering it is not proof of event
delivery, a completed job, application CI or production gating.
The optional [trusted static-image publication package](ZUUL_STATIC_IMAGE.md)
now prepares a final post-review job for all five Zuul hosting selections. The
job and trigger pass the pinned Zuul 14.3.0 schemas. Its non-root publisher CLI
also passed a real Harbor component run with authenticated TLS Git, exact source
witnesses, digest publication/read-back and original OCI file verification.
The CLI component used an authenticated Git HTTP fixture. A separate native
Linux run passed with an actual Gerrit ref update, encrypted Zuul 14.3.0
secret delivery, a final voting build and persisted artifact metadata. An
independent read of the local Harbor pool-health candidate confirmed the exact
reviewed file in the digest-addressed layer. The [image promotion command](ZUUL_GITOPS_HANDOFF.md#promote-a-published-image)
binds that receipt to the digest in a reviewed Deployment for Argo CD or Flux.
One isolated K3s run verified both controllers rolling out the same published
Harbor digest on two workers, exact HTTP content and replay without another
push. Full stack acceptance and physical failure testing remain open.

The [Zuul GitOps handoff](ZUUL_GITOPS_HANDOFF.md) now checks a reviewed
successful Gerrit branch buildset and required jobs before promoting the exact
revision through the shared Argo CD or Flux workflow. An isolated Gerrit and
Zuul 14.3.0 fixture verified a real post-review buildset against the resulting
branch revision and worker marker. The same tested revision was promoted to
separate deployment branches through the handoff CLI. Real Argo CD and Flux
controller processes each applied a ConfigMap from that exact revision against
a disposable Kubernetes API. Replaying each promotion performed no push.
The [local RKE2 worker rehearsal](ZUUL_GITOPS_HANDOFF.md#local-rke2-worker-deployment-rehearsal)
also passed with native Gerrit/Zuul, native Argo CD, three RKE2 control-plane
VMs and two dedicated agent VMs. A real Zuul worker validated the reviewed
manifest, Argo deployed the exact tested revision, and both agents served the
expected HTTP response before and after a serving Pod replacement. Replay
made no second push. This hybrid run used pinned Cilium/Envoy and verified
resource ownership and credential isolation. Its private coordinator is
acceptance tooling; physical Gerrit/Ceph HA, built-artifact publication,
target-cluster deployment and full-stack failure acceptance remain open.
The separate [worker-backed Flux rehearsal](ZUUL_GITOPS_HANDOFF.md#worker-backed-flux-validation)
also passed on the three-control-plane, two-agent RKE2 topology. The real Zuul
worker validated the reviewed resource selection; Flux deployed the exact tested
revision, retained ownership and inventory, and served HTTP on both agents after
Pod replacement. Preflight and replay made no extra Git push, restricted reads
and writes were denied, and archived-source and original-process cleanup checks
passed. The native Flux processes and all guests ran on one workstation; physical
Gerrit/Ceph HA and complete stack qualification remain open.
The [ZooKeeper dependency package](ZOOKEEPER.md) adds three persistent member
identities, client/peer TLS and guarded startup artifacts for all five targets.
It cross-checks the Zuul endpoints. Production PKI/ACL provisioning and full-runtime
upgrade/restore acceptance remain open; the optional native quorum case below
provides separate local process and network evidence.
The [Flux adapter](FLUX.md) transforms a pinned upstream export for an explicit
Kubernetes management target, including when application hosting is Docker or
native Linux. It adds shared artifact storage, independent read-only artifact
serving, scoped Git sync objects and an opt-in guarded deployment sequence.
The pinned Flux v2.6.4 package reconciled a private Git repository on disposable
K3s, served digest-verified artifacts from both NGINX candidates, and applied
the next commit after the source leader's VM was paused. It rolled out a
Deployment on the surviving worker and repaired ConfigMap and replica drift
at the same Git revision. The NFS artifact store stayed up. Application-host
CD integration and full stack acceptance remain open.
The [Tekton adapter](TEKTON.md) adds pinned release transformation, controller
election/scaling configuration, guarded deployment and an explicitly started
infrastructure canary for the three requested Tekton stacks. It uses an explicit
Kubernetes target for every application-hosting runtime. SCM events, application
builds, review feedback and full stack acceptance remain required work.
The [Forgejo/Gitea push bridge](TEKTON_FORGE_BRIDGE.md) adds an authenticated
receiver, immutable event receipts, exact-commit checkout and a reviewed inline
build pipeline. Its Kubernetes, Docker and native service artifacts preserve
the explicit job cluster. Native Forgejo and Gitea instances each delivered a
signed private push to a real Tekton build on disposable K3s. Each build
checked out the exact commit, and replay did not create a second run.
Gerrit's separate review-event adapter is
described above with its scoped live controller/worker cases; full stack
acceptance remains open.
The [separate Tekton result reporter](TEKTON_FORGE_RESULTS.md) now packages
terminal-outcome checks and commit-status publication with a distinct identity,
write-token Secret and durable report receipts. Both live cases also verified
that a separate reporter published exactly one success status for the original
commit. Full stack readiness remains open.
The optional [CI-to-GitOps handoff](CI_GITOPS_HANDOFF.md) adds reviewed
fast-forward-only deployment-branch promotion after fresh CI/result verification,
plus exact-revision checks for an existing Argo CD or Flux target. The native
Forgejo and Gitea cases each deployed the exact tested commit through Tekton
on K3s and native Argo CD. Two K3s workers served the expected response in
each case. The handoff
operates on Kubernetes manifests in the tested repository. Native/Docker workload
deployment, separate image/GitOps-repository promotion and full live lifecycle
acceptance remain open requirements.
The [Woodpecker recovery test](CI_GITOPS_HANDOFF.md#woodpecker-server-scheduling-and-recovery)
used a real K3s worker to stop an orphaned job after scheduler loss, confirm its
container exited, and retry the exact commit on a fresh workspace. A separate
two-worker K3s run fenced a VM with an active job and verified a manual retry
on the surviving worker. The
PostgreSQL-backed Woodpecker profile uses a single active scheduler. The local
test also showed why a second server sharing the database cannot pick up new
work queued by the first while both are running.

An optional [real local ZooKeeper quorum case](ZOOKEEPER.md#optional-real-local-quorum-check)
now passes the rendered native configuration and packaged guard against the
signature-verified 3.9.7 distribution. Three original TLS members and distinct
private networks exercise authenticated sessions, subtree ACL denials, leader
process loss, the same client session through election, state-preserving member
rejoin, unacknowledged writes without quorum and a separate link partition with
an acknowledged survivor write. The retained timed-out request completed only
after recovery; no zero-RPO claim follows. Source-bound independent inspection
verified original process/network identities and cleanup. This closes a local
quorum prerequisite, while physical storage/network independence, production
PKI/ACL provisioning, Zuul/Nodepool execution, upgrades and restore remain open.

The optional [external Gateway entry adapter](GATEWAY_EDGE.md) now provides
HAProxy/Keepalived packages for all five runtimes and ordered Kubernetes rollout
with private partial-failure journals. Its explicit local Docker/native host
helper now adds immutable releases, pinned native artifacts, guarded activation,
acknowledgment-loss resume and retained-release rollback. The
[staged pair coordinator](GATEWAY_EDGE_PAIR.md) adds two-host reservations,
ordered root jobs, VIP observation and same-handle resume. Optional verified SSH
staging distributes reviewed tools, bundles, CA and native binaries before either
reservation. The separate [isolated real VIP runtime probe](GATEWAY_EDGE_VIP_RUNTIME.md)
now verifies strict HTTPS, actual health-gated VIP movement, withdrawal and recovery
on two private network namespaces. Its independently audited 346-source snapshot
passed in 64.285 seconds, with 3.192 seconds measured to a serving peer after
HAProxy loss. This supplies scoped protocol evidence. The separate
[native host VM lifecycle probe](GATEWAY_EDGE_HOST_VM_RUNTIME.md) passed in
101.674 seconds against 347 Python sources, exercising real service activation,
configuration update, retained rollback and strict HTTPS through the guest VIP.
It corrected the unused checker/BFD PID-file paths and Keepalived packet-socket
restriction. The [real SSH pair-coordinator probe](GATEWAY_EDGE_PAIR_RUNTIME.md)
subsequently passed three staged operations and six durable jobs in 136.955
seconds against 348 Python sources, including source/journal/job reconstruction
and original unit/process cleanup. Its ordinary install/update/rollback path
is accepted in disposable guests. Its additional recovery profile passed in
181.173 seconds against 349 Python sources, verifying coordinator loss while
preserving the original live job, a bounded uncertain resume, three idempotent
completed resumes and consistent terminal receipts with retained failure history.
The terminated-worker profile subsequently passed in 197.104 seconds against
350 Python sources: ordinary resume retained the unverified handle, explicit
verified recovery permitted one replacement, and the audit accounted for six
completed records plus the terminated original and three idempotent resumes.
One earlier guest-boot stop remains unexplained, with bounded diagnostics now
retained for recurrence. Daemon version upgrades, physical paths, partition
fencing, combined live Cilium/application entry and end-to-end acceptance
remain required work.
The planner retains the remaining gaps.
Explicit terminated-job recovery additionally requires matching host/job
ownership, dead recorded processes, terminal systemd unit state and an empty
cgroup before permitting the existing bounded resume retry. Local transient
worker fixtures exercise session loss and descendant cleanup; they do not
establish fleet or application failover.
The existing default foundation uses Traefik/MetalLB and Longhorn rather than
the newly requested Gateway API/Envoy and Ceph architecture.
The optional [Cilium Gateway route package](CILIUM_GATEWAY.md) adds explicit
HTTPS/gRPC routes, backend policies, topology checks and guarded installation
onto a prepared Cilium target. CNI/CRD installation and migration, redundant
external entry, effective datapath checks and failure/restore acceptance remain
required work. Rendering or programming routes does not certify that foundation.
The [owned Cilium installation adapter](CILIUM_INSTALLATION.md) now renders
pinned CNI/Gateway CRD packages and adds fresh installation, reviewed patch
upgrades, explicit patch rollback and interrupted-operation resume. It refuses
existing Helm/distribution-owned CNI takeover. Pinned 1.19.10/1.4.1 artifacts add
the Kubernetes 1.35 compatibility lane, and a fresh real API case now verifies
all chart resource fields after admission and rejects a changed mount flag.
It runs without workers and cannot establish a functioning CNI. A separate
opt-in five-VM case now passes the production installer with explicit containerd
CNI paths, original-Node readiness, cross-worker Pod/Service traffic, allowed and
denied policies, quorum outages/recovery and verified TLS Gateway traffic through
both serving workers. Its 574.023-second receipt matched 342 Python sources,
and an independent audit confirmed removal of the original unit, guest/proxy
processes and private scopes. This is scoped virtual networking acceptance;
physical failure domains, redundant external entry, distribution/bootstrap
migration, minor upgrades, load and independent restoration remain open
requirements. An extended run verified Service-name lookups and HTTP by name
from both workers after control-plane recovery and after CoreDNS replacement.
No complete-stack readiness flag is promoted.

A further accepted [joined five-VM case](K3S_VM_WORKERS.md#optional-cilium-and-native-cicd-application-case)
now combines that Cilium foundation with native Gerrit polling/voting, actual
Tekton worker tests, the packaged promotion CLI and native Argo CD reconciliation.
The original Git-tested application is served through verified Gateway TLS on
both workers while direct Service/Pod traffic is denied. Named backend ports
resolve against original ready endpoint/Pod identities, preserving original
workload specs, Pod UIDs and container IDs. All three control services and
direct APIs must remain stable after the quorum pauses and after the workload.
The 1166.732-second receipt binds a 345-Python-source snapshot and adds actual
guest clock synchronization and forced control-process recovery. Independent inspection
confirms cleanup of all seven transient units, credential stores, native/VM
processes and private scopes. This closes a scoped networking/CI/CD integration
gap. It does not establish Gerrit HA, artifact-registry publication, Ceph,
physical failure domains, fleet upgrades/restores or full-stack readiness.

The separate [joined seven-guest native entry case](CILIUM_NATIVE_ENTRY_RUNTIME.md)
now passes that original flow through two standalone native HAProxy/Keepalived
edge VMs. The actual CI-tested, Argo-CD-deployed application recovered through
the peer VIP in 6.96 seconds after the deliberate owner HAProxy stop, with
verified TLS, preserved original workload identities and continued direct-access
policy denials. Independent inspection accepted all 351 source hashes, original
contracts/VMs, actual pair-job receipts and terminal cleanup for the
1253.023-second run. This extends scoped virtual integration acceptance;
physical HA, the remaining foundation and all fifty complete combinations
still require their own implementation and live evidence.

The [RKE2 Cilium Gateway drill](CILIUM_GATEWAY.md#configure-bundled-cilium-on-a-fresh-rke2-cluster)
joined two native HAProxy/Keepalived entry VMs to an isolated network with
three RKE2 servers and a separate application worker. It verified HTTPS
via the same VIP after entry-owner loss and separate Envoy-node loss. The
receipts record the observed request failures and recovery times.

The [RKE2 signature admission rehearsal](GERRIT_ARTIFACT_PROMOTION.md#rke2-signature-admission)
joined Gerrit, Tekton, Harbor, Cosign, Argo CD, and three Kyverno admission
replicas in a five-VM lab. It verified admission of the signed CI digest on
both workers and all 12 expected policy denials.

The [signed Forgejo/Tekton image rehearsal](CI_GITOPS_HANDOFF.md#optional-signed-forgejo-or-gitea-tekton-image-promotion)
also passed with native Forgejo and Argo CD, three RKE2 control-plane guests,
two application workers, and three Kyverno admission replicas. It verified
the protected webhook, exact CI image publication, all 12 admission denials,
single-push promotion and replay, Harbor continuity, and lab cleanup.

Full-stack acceptance remains open for all 50 stack and runtime selections.
The component evidence above records implemented adapters and scoped live
results. For each selection, the planner reports the remaining adapter,
integration, foundation and acceptance gaps.

Inspect the requested scope without loading private configuration or accessing
servers:

```bash
python3 scripts/plan_platform_stack.py --list
python3 scripts/plan_platform_stack.py --stack gerrit-zuul-argocd --runtime rke2
python3 scripts/plan_platform_stack.py --stack forgejo-woodpecker-argocd --runtime docker --require-ready
```

The last command exits 2 while implementation or acceptance is missing. A normal
plan exits 0 to indicate successful reporting, NOT deployment readiness. The
planner does not render, install, change profiles, connect to services, or
certify a live environment. It conservatively lists unverified requirements.

## Shared foundation acceptance requirements

The [independent HTTPS monitor](ALERTING.md#independent-https-checks)
renders Prometheus, Blackbox Exporter and Alertmanager configuration for a
native Linux or Docker host outside the monitored platform. Local integration
tests verified HTTPS probes and webhook delivery for endpoint outages,
exporter loss, and recovery in both runtimes.
The existing premium RKE2 profile includes Loki and Alloy. Its
`make platform-observability-verify` gate checks authenticated Loki
write/query and Alloy collection.

### Forward native Linux and Docker logs to Loki

Run the host collector on each Linux host that runs forge, CI, or CD
services. It uses the existing authenticated Loki gateway. The receiver
can run on a separate platform so operators can query collected logs
during a workload outage. Use an HTTPS endpoint with a trusted CA and
authorize its writer account for the selected tenant. For the premium
RKE2 gateway the tenant is `platform`.
[Loki gateway authentication](https://grafana.com/docs/loki/latest/operations/authentication/)

Copy the [host log collector example](../examples/host-log-collector.json)
into ignored private storage. Set a distinct `node` value per host, a
shared `cluster` label, the gateway URL, and the writer username. Map
each log label to the exact Compose service name. For native Linux,
use the exact systemd unit name, such as `jenkins.service`, as the value.
Use `files` for application logs written directly to disk, such as
GitLab component logs. Select the files individually and grant the
collector read access, including to replacements created by log rotation.

```bash
python3 scripts/render_host_log_collector.py \
  --plan private/host-log-collector.json --runtime docker \
  --output-dir private/rendered-host-logs
```

Set `--runtime raw` for native Linux. Install Grafana Alloy at
`/usr/bin/alloy` on the host, create a dedicated `platform-alloy`
system user and group, and install the bundle's `config.alloy` into
`/etc/platform-log-collector` as root with read permission for that group.
Place the CA bundle and password in root-owned `loki-ca.pem` and
`loki-password` files in that same directory. Store the password as
the file's exact contents without a trailing newline. systemd supplies
both files to the unprivileged collector with `LoadCredential=`.
Keep the rendered bundle and credential files in private storage.

```bash
sudo install -d -o root -g platform-alloy -m 0750 /etc/platform-log-collector
sudo install -o root -g platform-alloy -m 0640 \
  private/rendered-host-logs/config.alloy /etc/platform-log-collector/config.alloy
sudo install -m 0644 private/rendered-host-logs/platform-log-collector.service \
  /etc/systemd/system/platform-log-collector.service
sudo systemctl daemon-reload
# Run after installing the CA and password files.
sudo systemctl enable --now platform-log-collector.service
```

On Docker hosts with systemd-journald, include the generated
`compose.logging.yaml` after the existing Compose files on subsequent
deployments. Compose 2.24.4 or newer replaces the prior driver and its
options together. Check the merged config and recreate the selected
services in their normal maintenance window for the new driver to take
effect. The host collector reads the journal without access to the Docker
socket.
[Compose merge rules](https://docs.docker.com/reference/compose-file/merge/#replace-value)
[Docker journald driver](https://docs.docker.com/engine/logging/drivers/journald/)

Use an authorized Loki query to find a fresh known log marker from each
workload, for example `{cluster="platform",node="ci-host-01",app="forge"}`.
The collector sends the journal message or file line with its workload
labels. Review the applications' logging settings for sensitive data
before enrolling their logs. Alloy stores read positions locally and retries
transient push failures. A long receiver outage or a collector crash can
leave gaps, so preserve the local logs through the incident and monitor
the collector's loopback metrics for dropped entries.
[Alloy journal source](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.journal/)
[Alloy Loki delivery](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.write/)

An isolated Linux test with Alloy 1.21.3 and Loki 3.7.10 verified that
selected Docker and systemd output and a directly written log file
arrived in the configured tenant through the authenticated HTTPS gateway.
It verified that unselected containers and units were excluded and that
an entry reached Loki after the gateway recovered from a temporary 503.

### Forward Kubernetes pod logs to Loki

For RKE2, K3s, and existing Kubernetes, copy the
[collector plan](../examples/kubernetes-log-collector.json) to ignored
private storage. Select the application namespaces, specify the cluster DNS
suffix and configure the HTTPS Loki gateway. Choose a distinct cluster
label for each cluster that writes to the same Loki tenant. The plan
generates namespace-scoped pod discovery and log-read permissions for
each selected namespace. Select the namespaces that contain the forge,
CI and CD pods, including any separate CI job namespaces.
[Alloy Kubernetes log source](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.kubernetes/)

The collector needs a dedicated namespace, two schedulable Linux nodes,
and a Secret in its namespace containing `loki-ca.pem` and
`loki-password`. Use the private secret provisioning mechanism of that
cluster. The password file contains the exact password bytes. Run the
following against the intended cluster, with `KUBECONFIG` and `CONTEXT`
set to the reviewed target and existing source namespaces.

```bash
python3 scripts/render_kubernetes_log_collector.py \
  --plan private/kubernetes-log-collector.json --runtime k3s \
  --output-dir private/rendered-pod-logs

kubectl --kubeconfig "$KUBECONFIG" --context "$CONTEXT" apply --server-side \
  --dry-run=server -f private/rendered-pod-logs/pod-logs.yaml
kubectl --kubeconfig "$KUBECONFIG" --context "$CONTEXT" apply --server-side \
  -f private/rendered-pod-logs/pod-logs.yaml
kubectl --kubeconfig "$KUBECONFIG" --context "$CONTEXT" -n platform-logging \
  rollout status deployment/platform-pod-logs --timeout=180s
```

Use `--runtime rke2` or `--runtime k8s` for those targets. The example
uses `platform-logging` as the collector namespace. The two Alloy
replicas use a private headless Service for peer discovery and partition
the collection work. A single surviving replica can keep collecting.
During startup or a peer network partition, both replicas may temporarily
collect the same entries until membership converges. The ingress
NetworkPolicy allows peers within the collector namespace to reach the
HTTP listener. If that namespace
restricts egress, allow peer communication, cluster DNS, the API server,
and the Loki gateway. Use the host collector for node journals and
application logs that are written only to disk. For an RKE2 profile
that already collects the same pod logs, use one collector per source
and tenant to avoid duplicate collection.
[Configure Alloy clustering](https://grafana.com/docs/alloy/latest/configure/clustering/configure-alloy/)

Query Loki for a fresh known log marker with the expected cluster,
namespace, pod, and container labels. When changing the list of source
namespaces, prune the generated Role and RoleBinding from each removed
namespace using the previous manifest or GitOps ownership. During
collector or API failure, the recoverable history is limited to logs
still readable through the Kubernetes API and the collector's retries.

In an isolated K3s 1.35 test with two Linux workers, both replicas joined
the Alloy cluster and one collected the selected workload. The test
verified authenticated HTTPS delivery to Loki and denied log access
outside the selected namespace. After graceful removal of the active
collector, the surviving replica delivered a fresh log while the
replacement pod was still Pending. The same bundle renders for RKE2 and
existing Kubernetes. Deployment on those targets still needs verification.

### Deploy a replicated Loki receiver

Use the vendored Loki chart with the [receiver plan](../examples/loki-receiver.json)
for a fresh receiver deployment on RKE2, K3s or existing Kubernetes. For
independent recovery access, place the receiver and its HTTPS ingress
outside the workloads' failure domain. The plan runs three Loki replicas
with replication factor three and three retained WAL claims on separate
Linux nodes. Two NGINX gateways forward authenticated push and query
requests through the receiver's internal Kubernetes Service. The gateway
assigns the configured tenant even when the client supplies a different
tenant header. Use a dedicated receiver for each trust boundary for which
that single-tenant access model is appropriate.
[Loki deployment modes](https://grafana.com/docs/loki/latest/get-started/deployment-modes/)

Provide a dedicated S3 bucket on an HTTPS endpoint that is independently
reachable by the receiver pods, with a scoped S3 identity permitted to
list, read, write, and delete objects in that bucket. Provide three
Secrets in the receiver namespace. Use `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` in the S3 credential Secret, `ca.crt` in the
S3 CA Secret, and `.htpasswd` in the gateway authentication Secret.
Provision the ingress's TLS Secret in the receiver namespace and
set the ingress class and ingress controller namespace in the plan.
Configure the receiver namespace's network policies to permit its required
DNS, object storage and peer traffic. The generated ingress policies
allow the ingress controller to reach the gateway and limit backend
access to the gateway and Loki peers. Use a CNI that enforces them.

Copy the plan to private storage and select the storage class, retention,
DNS service and image digests for the receiving cluster. Use a new
namespace and bucket for this installation. The existing premium RKE2
Loki release uses a different topology and requires a data migration
plan if it is being replaced.

```bash
python3 scripts/render_loki_receiver.py --plan private/loki-receiver.json \
  --runtime k3s --output-dir private/rendered-log-receiver

LOKI_CHART=gitops/clusters/rke2-main/premium-3node/apps/loki/charts/loki
helm template platform-loki "$LOKI_CHART" -n platform-log-receiver \
  -f private/rendered-log-receiver/loki-values.yaml > private/rendered-log-receiver/manifests.yaml
kubectl --kubeconfig "$KUBECONFIG" --context "$CONTEXT" apply --server-side \
  --dry-run=server -f private/rendered-log-receiver/manifests.yaml
helm upgrade --install platform-loki "$LOKI_CHART" -n platform-log-receiver \
  --kubeconfig "$KUBECONFIG" --kube-context "$CONTEXT" \
  -f private/rendered-log-receiver/loki-values.yaml --wait --timeout 10m
```

Validate via the ingress hostname before onboarding collectors. Confirm
unauthenticated reads fail, push a unique marker with the configured
credentials, and query it. Repeat with one gateway and one Loki replica
unavailable in a disposable environment. Verify that the surviving
replicas accept new writes and can query the earlier marker. Check object
storage resilience, ingress failover, and restoration after loss of the
receiving cluster separately.

In a disposable K3s cluster, Loki 3.7.10 ran on three workers with local
WAL claims and a shared SeaweedFS S3 endpoint over HTTPS. The test queried
the same gateway Service while reducing the gateway from two replicas
to one and Loki from three replicas to two. A new write and queries for
both markers succeeded while reduced and after restoring the replicas.
The test verified WAL claim retention, HTTP 401 without a valid login,
and HTTP 404 on the gateway's administrative config route. This was a
graceful scale-down test using a single S3 fixture server and in-cluster
gateway traffic.

A separate K3s drill paused a worker carrying both a Loki replica and a
gateway without draining it. Kubernetes removed that worker from both
Services' ready endpoints. The same gateway Service acknowledged a fresh
write and returned both the earlier and new markers 57.1 seconds after
the pause. The original WAL claim stayed bound and all three markers
were readable after resuming the worker and writing again. The receiver
uses per-ingester readiness so the two healthy ingesters remain in
service when a peer misses heartbeats. This test used a VM pause, local
WAL volumes, and one S3 endpoint.

A separate K3s process-crash drill killed one Loki container with
`SIGKILL` after its WAL write counter advanced. Kubernetes reported
exit code 137 and restarted the container in the same Pod using the
same bound WAL volume. Loki reported a successful segment replay and
its recovery counter recorded two entries. Both pre-crash markers and
a fresh post-restart marker were readable through the gateway.

When investigating an ingester restart, compare the Pod's container
termination reason and restart count with the PVC UID and bound volume.
Check the restarted ingester's log for WAL segment recovery and inspect
`loki_ingester_wal_recovered_entries_total` and
`loki_ingester_wal_corruptions_total` on that replica. Query an acknowledged
marker through the gateway and submit a fresh marker to check readiness
after recovery. Scrape per-replica metrics through a restricted monitoring
path and alert on WAL corruption and disk pressure. The gateway exposes
only ingestion and query routes.
[Loki WAL behavior and metrics](https://grafana.com/docs/loki/latest/operations/storage/wal/)

Permanent disk loss, power loss, ingress failover, production object-store
failover, independent monitoring-host availability, and delivery to
production incident receivers still require acceptance.

### Keep a GitOps source copy outside the platform

Capture the reviewed deployment branch from a local checkout or bare seed
repository before the forge is unavailable. Use the full commit ID that
corresponds to the deployed revision. Store the resulting bundle, recorded
SHA-256, branch and commit with your off-platform recovery material. Keep
the digest in an authenticated recovery record. The bundle includes all
Git objects reachable from the selected branch, including historical
files. Store it as private material, especially when the branch contains
internal deployment values.
[Git bundle prerequisites](https://git-scm.com/docs/git-bundle#_object_prerequisites)

```bash
python3 scripts/bootstrap_git_bundle.py create \
  --source /path/to/platform-gitops --branch "$DEPLOY_BRANCH" \
  --commit "$DEPLOY_COMMIT" --output-dir /private/recovery/gitops-capture
```

On a separate recovery host, use the trusted helper with its
`bounded_subprocess.py` dependency and a local Git installation. Restore to
a new location. Restore verifies the digest, expected commit, branch and
reachable Git objects in a fresh repository.

```bash
python3 scripts/bootstrap_git_bundle.py restore \
  --bundle /private/recovery/gitops-capture/gitops.bundle \
  --sha256 "$BUNDLE_SHA256" --branch "$DEPLOY_BRANCH" \
  --commit "$DEPLOY_COMMIT" --output-dir /private/recovery/platform-gitops.git
git clone -- /private/recovery/platform-gitops.git /private/recovery/platform-gitops
```

Local tests restored the selected worktree's committed tip and complete
reachable tree into a bare repository. A separate isolated Linux test
restored a synthetic branch after taking its checkout offline and
cloned the same commit from a loopback-only seed Git service. Restores
rejected corrupted bundles, missing prerequisites and a wrong commit,
and preserved an existing destination.

For a cluster that reads from a seed Git service, populate a fresh seed
repository from the recovered bare repository, verify the advertised
branch tip, and point the Argo CD or Flux bootstrap at that source. Verify
that controllers running inside the target cluster can reach it before
reconciliation. The [Flux recovery test](FLUX.md#local-recovery-from-a-standalone-git-bundle)
used a disposable K3s cluster to fetch the restored repository over
authenticated HTTPS and apply the reviewed commit with a commit pin.
Keep the [verified Flux controller cache](FLUX.md#prepare-an-offline-controller-cache)
with the recovery material for clusters without registry access.

For Argo CD on RKE2, start from the recovered checkout at the recorded
commit so profile selection uses the same Git tree that Argo CD will read.
Use the normal bootstrap with `PLATFORM_GITOPS_RECOVERY_COMMIT` set to the
full lowercase SHA-1 and `PLATFORM_REPO_URL` set to the reachable recovery
repository. Provision the read credential and HTTPS CA for the recovery
host in Argo CD before registering the Applications.
[Argo CD private repository trust](https://argo-cd.readthedocs.io/en/stable/user-guide/private-repositories/)

To bootstrap without downloading manifests, place the pinned release's
core `install.yaml` and HA `install-ha.yaml` in a root-owned cache on the
first RKE2 server. Use the release and SHA-256 values recorded in the
bootstrap playbook for your checkout. Save the upstream
`manifests/ha/install.yaml` under the cache name `install-ha.yaml`.
Core mode needs only `install.yaml`. HA mode also needs it when automatic
core fallback is enabled. The cache is checked before cluster changes,
and the exact bytes submitted to Kubernetes are checked again.
[Argo CD release manifests](https://github.com/argoproj/argo-cd/tree/v3.5.0/manifests)

```bash
PLATFORM_REPO_URL="$RECOVERY_REPO_URL" \
PLATFORM_GITOPS_RECOVERY_COMMIT="$DEPLOY_COMMIT" \
PLATFORM_ARGOCD_MANIFEST_DIR=/private/recovery/argocd-manifests \
PLATFORM_PROFILE="$DEPLOY_PROFILE" \
PLATFORM_APPLY_GITOPS=true make platform-argocd
```

The upstream manifests include containers with an `Always` pull policy.
For recovery using only preloaded images, set
`PLATFORM_ARGOCD_OFFLINE_IMAGE_LOCK` to an absolute path on the first
RKE2 server and install Python 3 with PyYAML there. The lock is a JSON
object mapping every upstream image tag in the selected manifest and
enabled fallback to a reviewed `sha256:…` manifest digest. Keep the lock
with the authenticated recovery material. The
[v3.5.0 Linux amd64 lock](../examples/argocd-v3.5.0-linux-amd64-images.json)
records the four public registry digests used in the dry-run. When all
eligible nodes run Linux amd64, copy the lock to the recovery server and
set the path in the bootstrap environment.

Include images in both
`containers` and `initContainers`. Import each image under its full
`registry/repository@sha256:…` reference on every eligible node, using
image artifacts for the node architecture. The bootstrap verifies the
cached upstream YAML before applying the digest substitutions and setting
`imagePullPolicy: Never`. A node missing the exact local reference will
report `ErrImageNeverPull`.
[RKE2 air-gap image staging](https://docs.rke2.io/install/airgap)

#### Assemble and verify the Argo CD recovery directory

Use `prepare_argocd_recovery.py` with the reviewed checkout and image lock.
The source cache contains the upstream manifests and uncompressed OCI tar
archives named `<manifest-digest-without-sha256-prefix>.oci.tar`. Each archive
must carry the exact digest reference as its containerd image name. The
helper verifies the Linux amd64 manifest, config and layer digests and
copies the required files into a new directory. It also writes the image
lock used by bootstrap.

```bash
LOCK=examples/argocd-v3.5.0-linux-amd64-images.json
KIT=/private/recovery/argocd
python3 scripts/prepare_argocd_recovery.py create \
  --source "$ARGOCD_ARTIFACT_CACHE" --directory "$KIT" \
  --reviewed-lock "$LOCK" --mode ha --core-fallback

# Run again from the reviewed checkout after copying the directory.
python3 scripts/prepare_argocd_recovery.py verify \
  --directory "$KIT" --reviewed-lock "$LOCK" --mode ha --core-fallback
```

Keep the reviewed image lock with the trusted checkout independently of
the transfer. Core-only recovery uses `--mode core`. For HA without core
fallback, omit `--core-fallback` and also set
`PLATFORM_ARGOCD_AUTO_CORE_FALLBACK=false` during bootstrap. After importing
the verified images on every eligible node, set `PLATFORM_ARGOCD_MANIFEST_DIR`
to the transferred directory and `PLATFORM_ARGOCD_OFFLINE_IMAGE_LOCK` to its
`images.json` on the first RKE2 server.

For recovery through a reachable registry mirror, omit the image lock
and ensure the mirror can resolve the image references in the manifest.
Omit the manifest directory to use the existing pinned HTTPS downloads.
In a network-isolated Kubernetes API test, the cached core, HA and fallback
tasks passed server-side dry-run. The admitted workloads retained the
locked digests and `Never` pull policy. A missing fallback, an incomplete
image lock and a modified manifest stopped before API submission.

The bootstrap passes the pin to the Application renderer. The renderer
requires each selected Application to use the recovery repository as its
single Git source, then puts the exact commit in `spec.source.targetRevision`.
Check the applied Applications' sync revision and workload health before
resuming branch tracking. Run the reviewed bootstrap again with the normal
repository URL and the recovery override unset when ready to resume.

A network-isolated test used native Argo CD v3.5.0 and a fresh Kubernetes
v1.35 API. The renderer pinned a ConfigMap Application to a commit from
the bundle. Argo CD fetched it with authentication and CA verification
after the original checkout was removed, and reported the reviewed SHA
in both the sync status and the successful operation result. The live
ConfigMap matched that commit while the recovery branch pointed elsewhere.

A second test bootstrapped the upstream Argo CD v3.5.0 HA workloads on
K3s v1.35.0+k3s1. Three disposable workers each imported all four
Linux amd64 image references after the test verified the OCI manifests
and blobs against their digests. The laboratory had no upstream route.
The Redis HA StatefulSet, HAProxy and the Argo CD workloads completed
rollout, and the reported container image IDs matched the preloaded
manifest digests. Argo CD fetched the restored repository over
authenticated HTTPS and applied the pinned commit. The live ConfigMap
matched the pinned commit while the branch advertised its newer tip.
The full RKE2 bootstrap and whole-platform restore still require a separate drill.

Keep separate copies of Kubernetes and OS installers,
charts and image archives that the Git tree references, private
configuration, decryption keys, application backups, and any Git LFS or
submodule data. The Git bundle is the committed source portion of the
recovery kit.

- Three control-plane nodes, stable API endpoint and capacity for workers.
- Cilium policies and redundant network paths.
- Cilium Gateway API/Envoy with multiple serving nodes and resilient entry.
- HAProxy Community, redundant VIP/routing, protocol health checks and
  application-correct Git write routing.
- PostgreSQL through CloudNativePG or external HA; deliberate synchronous
  replication, promotion and recovery tests.
- Ceph, Rook where applicable, failure-domain placement, redundant access
  services and recovery headroom.
- Keycloak with multiple instances, HA database, MFA and working integrations.
- OpenBao HA, restricted workload identities, recoverable unseal and audit.
- Harbor application, database, cache and storage availability.
- Valkey replication/failover only where application-compatible.
- HA Kyverno and Cosign verification policies with negative tests.
- Monitoring, centralized logs, actionable alerts, independent bootstrap,
  off-platform backups and tested restoration.

## Implementation sequence and completion evidence

1. Scope and truthful capability reporting: initial planner and 50-selection
   tests implemented; no deployment support claimed by this milestone.
2. Runtime adapters: separate RKE2, existing Kubernetes, K3s, Docker and native
   Linux installation/lifecycle contracts. Verify versions, resources,
   credentials, network/storage prerequisites and explicit Kubernetes targets.
3. Foundation: implement selectable requested components, secret references,
   topology, TLS, resource limits, policies, observability and recovery.
4. Forge adapters: implement pinned community-edition artifacts, persistent
   state, database/cache compatibility, SSH/HTTPS routes, identity and upgrades.
   Gerrit HA needs plugin, storage and write-routing acceptance; GitLab CE
   hybrid HA needs its full repository/database/cache topology and edition
   review. Preserve qualified single-active/recovery models for Forgejo,
   Gitea and Jenkins; never invent unsupported active-active behavior.
5. CI/CD integrations: implement event authentication, review feedback,
   repository permissions, isolated workers, artifacts, secret boundaries and
   GitOps reconciliation for each declared combination.
6. Gates: offline render/schema/idempotence and negative tests for every
   selection, followed by isolated live installation and upgrade drills.
   Test SSH/HTTPS Git operations, CI trigger/report, SSO/RBAC, mail, drift,
   one-node loss, restore, load, RPO/RTO and rollback. Reports must bind the
   actual runtime, topology, exact artifact versions and tested source revision.
7. Documentation and release: operator runbooks, sizing, license/edition review,
   disaster recovery and explicit remaining limitations. Full completion
   requires all requested combinations and additional forge options to have
   matching implementation AND acceptance evidence.

Private hosts, company email, keys, tokens, passwords, certificates, inventories
and runtime proofs stay in ignored private storage. No live deployment, commit,
push or merge is implied by generating a plan.
