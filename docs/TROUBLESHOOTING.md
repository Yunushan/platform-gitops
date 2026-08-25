# Troubleshooting

## Argo CD cannot access this repository

Check that you replaced `<THIS_REPO_URL>` at bootstrap time and configured repository credentials in Argo CD using a private secret flow.

Run the automated platform report:

```bash
make platform-status
```

It prints API VIP readiness, Argo CD pods/services, configured Argo CD
repo-server/Redis service endpoint readiness, Woodpecker server/agent runtime
readiness and expected image tag, per-GUI HTTPS status through the app VIP from
both the cluster side and the Ansible controller/client side, registered Argo CD
Applications, ingress state, expected GUI URLs, and the next command when the
GUI layer is not deployed yet.

Run the read-only production readiness gate after changes settle:

```bash
make platform-production-check
```

This chains repository validation, RKE2 verification, the platform status
report, and `make platform-app-health`. If it fails, read the first failing
section: app sync/health and pod readiness failures are GitOps/workload issues;
Argo CD source repository failures mean Applications still point to temporary
seed Git, insecure `git://`, or a missing repo URL instead of the intended
private Git source, or they point to a repo URL different from
`PLATFORM_APP_HEALTH_EXPECTED_REPO_URL` / `PLATFORM_REPO_URL`;
HA replica failures point to shrunken or unavailable Argo CD, Traefik, or
Woodpecker control-plane replicas; StorageClass and PVC failures point to
Longhorn/storage provisioning or stuck finalizers; Longhorn runtime failures
point to missing Longhorn node objects, disabled scheduling, unhealthy Longhorn
nodes, or degraded/faulted volumes; CloudNativePG cluster failures point to
missing or unhealthy PostgreSQL clusters; GUI backend endpoint failures point to
missing Ingress/IngressRoute objects or Services with no ready pods;
controller/client app VIP and HTTP
redirect failures usually point to MetalLB, Traefik, DNS, or client routing;
Harbor registry API failures point to a broken `/v2/` route, wrong Harbor
service backend, TLS/host routing, or registry pod health; Grafana
`/api/health` or Prometheus `/-/ready` failures point to monitoring service
health, route selection, or app VIP reachability; Loki `/ready` failures point
to logging service health or service routing; Velero `BackupStorageLocation`
failures point to backup object storage credentials, bucket policy, endpoint
reachability, or provider configuration; Velero backup schedule failures point
to missing or paused scheduled backups; generated app secret contract failures
point to missing Harbor, Forgejo, Woodpecker, Keycloak, Grafana, Loki, or Velero Secrets
or missing required keys, including the Forgejo SQL password Secret and
optional Redis Secret when `PLATFORM_APP_HEALTH_FORGEJO_PRODUCTION_SECRETS=true` and the
Grafana external PostgreSQL password Secret when
`PLATFORM_APP_HEALTH_GRAFANA_DATABASE_SECRET=true`; Argo CD or Woodpecker ClusterIP failures point to CNI, kube-proxy,
firewalld, or node-to-pod networking. The service-path section checks both the
node host path and short-lived diagnostic pods pinned to each RKE2 node, so
Woodpecker agent gRPC failures on only one or two nodes are reported directly.

If you only need to check the Argo CD runtime and Woodpecker CI path while
debugging `502`, `504`, or Woodpecker agent `CrashLoopBackOff`, use:

```bash
make platform-ci-health
```

This focused gate loads any ignored deployment environment, excludes unrelated
SSO-secret enforcement, and checks only Argo CD, Traefik, and Woodpecker. If an
Argo CD or Woodpecker hostname was not explicitly configured, it can select the
single host from the exact live application Ingress name. It fails closed on
ambiguous or malformed routes. `make platform-app-health` does not use this
fallback and remains the strict production-wide gate.

If Woodpecker server and agent pods report different or unexpected versions,
confirm the rendered values pin the Woodpecker server and agent image
repositories plus `WOODPECKER_IMAGE_TAG` (`v3.16.0` by default), then sync the
app again. The health gate also verifies the running Woodpecker server and agent images against
`PLATFORM_APP_HEALTH_WOODPECKER_IMAGE_TAG`, which falls back to
`WOODPECKER_IMAGE_TAG` and then `v3.16.0`.

For the common case where the Woodpecker Argo CD application is `Synced` but
still `Progressing`, or agents are stuck on an old `next-*` image and
`CrashLoopBackOff`, use the focused repair target:

```bash
make platform-woodpecker-repair
```

It hard-refreshes and syncs the Woodpecker application first, waits for the
server and agents, verifies the runtime server and agent image tags, refreshes
service-path consumers, and then runs `make platform-ci-health`. The repair
reconciles only Woodpecker's agent, database, and Forgejo OAuth secrets, so an
unrelated Harbor S3 or backup credential cannot block this focused workflow.
Use `make platform-app-secrets` to enforce the complete production secret gate.

The Argo CD repair phase also reconciles `prune=true`, `selfHeal=true`,
`allowEmpty=false`, and approval-gated foreground prune options for the exact
applications it refreshes. Existing application-specific sync options are
preserved. An emergency diagnostic can opt out with
`PLATFORM_ARGOCD_SERVICE_REPAIR_GUARDED_PRUNE=false`, but production recovery
should leave the default enabled.

Before those steps, the target runs the guarded node-storage cleanup in
pressure-only mode and waits for `DiskPressure` to clear. It prunes unused
Docker artifacts, stale GitLab Runner cache, and unused images from responsive
CRI endpoints on pressured nodes. Because a Longhorn replica can occupy the
pressured node while its volume is attached elsewhere, it performs a bounded
trim on mounted Longhorn XFS/EXT4 filesystems across all RKE2 servers whenever
any server has pressure. It also reconciles safe Longhorn orphan and
system-snapshot cleanup settings, then waits through the orphan grace period on
Ready nodes. Running containers and their images remain protected by CRI, and
Longhorn replicas, PVCs, application data, and valid snapshots are never
deleted.
If pressure remains, the target prints the kubelet condition, filesystems,
largest `/var/lib` consumers, and runtime services before failing closed. The
premium profile keeps three server and three agent replicas with hard hostname
spreading. Its idle CPU requests are `50m` per server and `100m` per agent,
with no CPU limit, so workloads can still burst. If the scheduler still reports
insufficient CPU or a non-pressure taint, repair fails with that classification
and does not reduce HA or weaken the topology policy automatically.

When ignored `private/seed-git.env` exists, the repair first renders the private
values from a clean working tree and synchronizes the current deployment branch
to the temporary seed Git source read by Argo CD. This makes a recovered
PostgreSQL CA mount declarative instead of leaving a live-only patch that Argo CD
could revert. Source-remote pull and push remain disabled for this focused
reconciliation. Set `PLATFORM_WOODPECKER_REPAIR_SYNC_GITOPS=false` only when an
external process owns the Argo CD source; use `true` to require the private seed
environment instead of the default `auto` detection.
The focused reconciliation renders only Woodpecker values and shared policy
artifacts. It intentionally leaves Forgejo, Longhorn, Harbor, backup, and
monitoring values unchanged, so a missing unrelated S3 endpoint cannot block a
Woodpecker repair. Configure those production values before running their own
deployment or health gates.

The premium renderer does not use the chart-generated
`woodpecker-default-agent-secret`. That chart secret depends on random Helm
rendering and can change during an Argo CD comparison. Instead,
`make platform-app-secrets` generates and preserves
`woodpecker/woodpecker-agent-secret`, and rendered server and agent workloads
both import it. If agents report `individual agent not found by token`, sync the
new values and rerun the repair. It restarts both roles after the sync and
removes the old chart secret only after no workload references it. Override the
name with `WOODPECKER_AGENT_SECRET_NAME`; set `WOODPECKER_AGENT_SECRET` only
through ignored private configuration when a fixed token is required.

For PostgreSQL-backed Woodpecker, the repair also resolves ready EndpointSlice
addresses and probes DNS, ClusterIP, and direct pod endpoints from the
Woodpecker server node. A ClusterIP or direct-endpoint timeout triggers one
bounded refresh of kube-proxy and Cilium on only the affected source/endpoint
nodes. Pod readiness can precede Cilium peer/tunnel convergence, so the repair
then repeats the real DNS, ClusterIP, and direct-endpoint probes for a bounded
180-second convergence window instead of failing after one immediate retry.
Change that window with
`PLATFORM_WOODPECKER_REPAIR_SERVICE_PATH_CONVERGENCE_TIMEOUT`. If the direct
endpoint still times out, the Make target applies the existing all-node CNI,
reverse-path filter, and firewalld recovery once, then reruns the Woodpecker
repair. The firewalld recovery installs explicit forwarding rules for every
detected source/destination pod-CIDR pair. This matters when RKE2 assigns a
different `/24` to each node: a same-CIDR rule permits local pod traffic but
still drops cross-node TCP, even though Cilium health ICMP succeeds. Cilium
network policy remains the workload-level policy layer.

If Cilium health then shows host connectivity working and remote endpoint ICMP
working while remote endpoint HTTP/TCP times out, the repair recognizes the
[documented Cilium VMware VXLAN failure](https://docs.cilium.io/en/stable/installation/k8s-install-broadcom-vmware-esxi-nsx/#pod-communication-failure-across-hosts).
Before restarting any RKE2 server, it opens `8223/udp` on every node, merges
`tunnelProtocol=vxlan` and `tunnelPort=8223` into the existing RKE2
`rke2-cilium` `HelmChartConfig`, waits for Helm and the Cilium DaemonSet, and
retries the real PostgreSQL paths. The merge preserves unrelated Cilium values.
Disable this guarded overlay workaround with
`PLATFORM_CILIUM_VXLAN_WORKAROUND=false`, choose another supported alternate
port with `PLATFORM_CILIUM_VXLAN_TUNNEL_PORT`, or adjust the rollout wait with
`PLATFORM_CILIUM_VXLAN_ROLLOUT_TIMEOUT`.

If the direct endpoint path still fails on the premium three-node profile, the
target verifies that two peer control-plane nodes are Ready and performs a
guarded rolling RKE2 restart on only the Woodpecker source and PostgreSQL
endpoint nodes. It waits for each node, Cilium, and kube-proxy before moving to
the next node; PVCs, PVs, Longhorn volumes, and database objects are retained.
Disable this final fallback with
`PLATFORM_WOODPECKER_REPAIR_FAILED_NODE_RESTART=false`, or adjust its wait with
`PLATFORM_WOODPECKER_REPAIR_FAILED_NODE_RESTART_TIMEOUT`. Disable the first
targeted recovery with `PLATFORM_WOODPECKER_REPAIR_AUTO_SERVICE_PATH=false`, or
adjust its per-node wait with
`PLATFORM_WOODPECKER_REPAIR_SERVICE_PATH_ROLLOUT_TIMEOUT`.

The same bounded fallback also recognizes CloudNativePG webhook and pod-probe
timeouts. If a node has lost `driver.longhorn.io` registration, or a
Woodpecker replica reports `volume ... is not ready for workloads`, it runs
`make platform-longhorn-runtime-repair` to recover the Longhorn manager/CSI
runtime, force-refresh the CSI sidecars for an attach-readiness fault, and
remove only empty duplicate disk registrations. It then retries the Woodpecker
repair once. This focused target does not enforce cluster-wide Longhorn
capacity.

If trust-manager has not materialized `woodpecker/platform-internal-roots`, the
repair first uses the `ca.crt` from CloudNativePG's active `serverCASecret`, then
falls back to `cert-manager/platform-internal-root-ca`. It validates that the
decoded value contains a PEM certificate before creating the ConfigMap and
never disables PostgreSQL certificate verification. If neither authoritative
source exists, the repair still fails closed.

When the StatefulSet template has the trust mount but an older, unhealthy Pod
still lacks it, `platform-woodpecker-repair` recycles stale server Pods one at a
time, starting with the highest unready ordinal. Every PVC is retained, each
replacement must contain the CA mount and become Ready before repair continues,
and the last Ready server is never removed without another Ready replica.
Candidate discovery only includes existing Pods owned by the StatefulSet, so an
`OrderedReady` rollout never waits for a higher ordinal that has not been
created yet.

Runtime CA verification checks the PEM-bearing ConfigMap plus the exact Pod
volume item and server mount before probing the file in the container. Some
minimal Woodpecker images do not include a standalone `test` executable. Only
that specific tool-unavailable error may fall back to the validated Kubernetes
projected-volume contract; a missing ConfigMap key, wrong item path, wrong
mount, empty file, or any other exec failure still fails closed. The premium
agent also receives a writable ephemeral `/etc/woodpecker` directory while
both roles enforce non-root execution, dropped capabilities, and the runtime
default seccomp profile.

The premium CloudNativePG profile keeps its mutating and validating webhooks
enabled with `failurePolicy: Ignore`. Healthy requests still pass through both
webhooks, while a temporary API-server-to-webhook ClusterIP outage no longer
deadlocks every PostgreSQL GitOps sync. The Woodpecker repair enforces the same
policy before requesting the `platform-postgres` sync; workload and database
readiness checks still have to pass before the repair succeeds.

If CloudNativePG reports `Instance Status Extraction Error: HTTP communication
issue` after the all-node service-path repair, the focused repair can recycle
one stale, unready current-primary Pod. This recovery is enabled by default but
requires zero ready PostgreSQL instances and endpoints, an exact CNPG phase
match, a Pod age of at least 600 seconds, and only healthy `Bound` PVC
references. It never deletes or modifies those PVCs. Disable it with
`PLATFORM_WOODPECKER_REPAIR_RECYCLE_STALE_POSTGRES_INSTANCE=false`, or change
the minimum age with
`PLATFORM_WOODPECKER_REPAIR_STALE_POSTGRES_INSTANCE_MIN_AGE`.

The focused Woodpecker repair validates the PostgreSQL service, ready endpoint,
credentials, and backing PVC directly. It does not run the full Longhorn
bootstrap or require every storage node to have capacity for new replicas.
Automatic failed-replica cleanup is limited to an initializing CNPG replica
with no active pod and a detached, zero-byte Longhorn volume. It verifies the
original PVC and PV UIDs before requesting cleanup and never resets the active
PostgreSQL primary.

The same data-safety rule applies to a blocked Woodpecker HA server replica.
Only ordinals above `woodpecker-server-0` qualify, and only when the container
has never started, the attach event says the Longhorn volume is not ready, the
PVC/PV/Longhorn identity matches, and Longhorn reports exactly zero bytes. The
repair temporarily scales to the blocked ordinal, deletes that empty replica
PVC, and restores the requested replica count. It never deletes
`data-woodpecker-server-0` or any replica volume containing data. Disable this
recovery with
`PLATFORM_WOODPECKER_REPAIR_RESET_FAILED_SERVER_REPLICA_PVCS=false`.

Use `make platform-longhorn-bootstrap` separately when repairing cluster-wide
storage capacity or Longhorn installation state.

If `make platform-status` shows a published GUI host returning `502` or `504`
through the app VIP from either the cluster-side probe or the controller/client
probe, the DNS/VIP reached Traefik but Traefik could not complete the backend
request. Check that app's pods, service endpoints, and node-to-pod service path
with `make platform-app-health`. If the controller/client probe returns `000`
while the cluster-side probe works, focus on client-to-VIP routing, firewall, or
ARP/neighbor-cache state.

If `make platform-status` reports `Production source readiness: NOT READY`,
Argo CD is still using temporary seed Git, insecure `git://`, a missing repo
URL, or a repo URL different from the expected private source. `make
platform-status` prints `Expected production repo URL`; set
`PLATFORM_STATUS_EXPECTED_REPO_URL=<PRIVATE_REPO_URL>` or
`PLATFORM_REPO_URL=<PRIVATE_REPO_URL>` if you want that report to enforce an
exact value. Migrate the platform repository into the intended private Git
service, rerun `PLATFORM_REPO_URL=<PRIVATE_REPO_URL> PLATFORM_APPLY_GITOPS=true
make platform-argocd`, then remove the seed service with
`make platform-seed-git-remove`. For bootstrap-only troubleshooting, bypass just
the seed/insecure-source check with
`PLATFORM_APP_HEALTH_FORBID_TEMPORARY_REPO=false make platform-app-health`.
For final health proof with exact source matching, run:

```bash
PLATFORM_APP_HEALTH_EXPECTED_REPO_URL=<PRIVATE_REPO_URL> make platform-app-health
```

That target still checks Argo CD pods, configured repo-server/Redis service
endpoints, Argo CD and Woodpecker GUI routing, Traefik, Woodpecker generated
secrets, Woodpecker HA replica coverage, and Argo CD / Woodpecker ClusterIP
service paths, but skips Harbor, monitoring, Loki, Velero, CloudNativePG,
Longhorn runtime, and StorageClass gates. It also
sets `PLATFORM_APP_HEALTH_INCLUDE_EXISTING_APPS=false`, so unrelated existing
Argo CD Applications do not block the focused repair check.

For Argo CD repo-server/Redis timeouts, Woodpecker agent gRPC timeouts, or
node-specific ClusterIP service failures, run the explicit service-path repair
alias before rechecking health. The alias repairs CoreDNS/CNI service routing,
then refreshes Woodpecker agents and verifies the Woodpecker gRPC ClusterIP
from every RKE2 node host and from diagnostic pods pinned to every RKE2 node so
CrashLoopBackOff agents do not wait on exponential backoff. If the refreshed
agents still do not become Ready, the repair prints the final Woodpecker
pods/services plus node and pod-pinned gRPC probe output before failing:

```bash
make platform-service-path-repair
make platform-argocd-service-repair
make platform-app-health
```

To require RKE2 node-originated app VIP self-probes as well:

```bash
PLATFORM_APP_HEALTH_NODE_INGRESS_STRICT=true make platform-app-health
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

To skip only HTTP-to-HTTPS redirect enforcement during a temporary debug run:

```bash
PLATFORM_APP_HEALTH_HTTP_REDIRECT=false make platform-app-health
```

To skip only Harbor `/v2/` registry API enforcement during a temporary debug
run. The health gate expects the Docker Distribution API header from that route,
not just a reachable Harbor web UI:

```bash
PLATFORM_APP_HEALTH_REGISTRY_API=false make platform-app-health
```

To skip only Grafana `/api/health` and Prometheus `/-/ready` enforcement during
a temporary debug run:

```bash
PLATFORM_APP_HEALTH_MONITORING_API=false make platform-app-health
```

When Grafana or Prometheus returns `no available server`, repair the shared
service path and storage prerequisites, reconcile the monitoring application,
and wait for real backend endpoints with:

```bash
make platform-monitoring-repair
```

The repair stops with pod, PVC, and Longhorn diagnostics when capacity or a
volume attachment still prevents either backend from becoming Ready.

Longhorn duplicate-disk evacuation also stops early when ready disks lack the
logical headroom needed to rebuild evicted replicas. Prefer adding dedicated
storage or removing confirmed-unused `Released` PVs after backup. If physical
usage, alerts, and growth forecasts justify thin provisioning, set
`PLATFORM_LONGHORN_STORAGE_OVER_PROVISIONING_PERCENTAGE` in the ignored private
deployment environment, rerender and sync the private GitOps values, then rerun
`make platform-longhorn-bootstrap`. The target automatically loads
`private/seed-git.env`, or `private/first-deploy.env` when no seed environment
exists. Explicitly exported values take precedence, and
`PLATFORM_LONGHORN_ENV_FILE` can select another ignored environment file. The
default remains `100`; increasing it does not create physical capacity.

To skip only Loki `/ready`, Velero `BackupStorageLocation`, or Velero backup
schedule enforcement during a temporary debug run:

```bash
PLATFORM_APP_HEALTH_LOKI_API=false make platform-app-health
PLATFORM_APP_HEALTH_VELERO_BACKUP_STORAGE=false make platform-app-health
PLATFORM_APP_HEALTH_VELERO_SCHEDULES=false make platform-app-health
```

When Harbor, Forgejo, Woodpecker, Keycloak, Grafana, Loki, Velero, or CloudNativePG are required, `platform-app-health`
also verifies the generated app secret contracts created by
`make platform-app-secrets`. It checks the expected Secret names and keys,
including Woodpecker OAuth and `WOODPECKER_DATABASE_DATASOURCE`, without
printing secret values. To skip only that check during a temporary debug run:

```bash
PLATFORM_APP_HEALTH_APP_SECRETS=skip make platform-app-health
```

If Woodpecker redirects back to `/login?error=oauth_error`, inspect the server
log before rotating OAuth credentials. When it reports `x509: certificate
signed by unknown authority` for Forgejo's token endpoint, the OAuth client and
redirect can still be correct; the ingress is serving an incomplete TLS chain.
Run the normal repair target:

```bash
make platform-woodpecker-repair
```

The repair verifies Forgejo from the ingress VIP with SNI and the system trust
store. If an intermediate is missing, it completes the existing Forgejo TLS
Secret from the certificate's cryptographically verified CA Issuers AIA path,
updates other platform TLS Secrets only when they contain the exact same wildcard
leaf fingerprint, waits for Traefik to serve the repaired chain, and then
continues Woodpecker reconciliation. If ready Traefik replicas retain the old certificate cache, the
repair recycles them serially and waits for full replica readiness after each
replacement. It never enables Woodpecker's TLS skip-verification setting.

If Woodpecker redirects to `/login?error=registration_closed`, OAuth has
completed but the Woodpecker user is not registered. This is caused by the
intentional `WOODPECKER_OPEN=false` production default, not by Forgejo TLS.
For a controlled deployment, set `WOODPECKER_ADMIN_USERS` to the exact Forgejo
login and rerender the private values. For initial onboarding, temporarily set
`WOODPECKER_OPEN=true` in the ignored private environment, sync the rendered
values, let approved users sign in, then set it back to `false` and sync again.
Do not patch only the live Deployment because Argo CD will reconcile it back to
the GitOps value.

The `platform-tls-verify` gate uses each live Ingress TLS Secret binding as the
authoritative hostname when one exists. This keeps verification aligned with
custom hostnames even when optional hostname variables are absent from the local
inventory.

For production Harbor, add
`PLATFORM_APP_HEALTH_HARBOR_PRODUCTION_SECRETS=true` so the same gate also
requires the external PostgreSQL password, external Redis password, and registry
S3 credential secrets referenced by `HARBOR_DATABASE_MODE=external`,
`HARBOR_REDIS_MODE=external`, and `HARBOR_STORAGE_MODE=s3`. In the premium
default, Harbor's external Redis secret can be derived from shared
`platform-cache/platform-valkey-auth`; provide a separate Redis password only
when using a separate Redis or Valkey endpoint.

For production Forgejo, add
`PLATFORM_APP_HEALTH_FORGEJO_PRODUCTION_SECRETS=true` so the same gate also
requires the external PostgreSQL password and Redis URI secrets referenced by
`FORGEJO_DATABASE_MODE=postgres`, `FORGEJO_DATABASE_SECRET_NAME`, and
`FORGEJO_REDIS_SECRET_NAME` when `FORGEJO_REDIS_MODE=redis`. The premium
default derives that URI from shared `platform-valkey`.

For production Grafana, add
`PLATFORM_APP_HEALTH_GRAFANA_DATABASE_SECRET=true` so the same gate also
requires the external PostgreSQL password Secret referenced by
`GRAFANA_DATABASE_MODE=postgres`.

Certificate and trust checks default to `auto`, which verifies any existing
cert-manager `Certificate` resources and trust-manager `Bundle` resources while
permitting controller-only bootstrap installs. To require exact resources:

```bash
PLATFORM_APP_HEALTH_CERTIFICATES="argocd/argocd-server-tls" make platform-app-health
PLATFORM_APP_HEALTH_TRUST_BUNDLES="platform-public-roots" make platform-app-health
```

To bypass only those checks during a temporary debug run:

```bash
PLATFORM_APP_HEALTH_CERTIFICATES=skip make platform-app-health
PLATFORM_APP_HEALTH_TRUST_BUNDLES=skip make platform-app-health
```

When step-ca is required, `platform-app-health` also probes its in-cluster
HTTPS `/health` endpoint through the ClusterIP service. To bypass only that
probe during a temporary debug run:

```bash
PLATFORM_APP_HEALTH_STEP_CA_API=false make platform-app-health
```

CloudNativePG checks default to `auto`, which verifies any existing PostgreSQL
clusters but permits an operator-only bootstrap install. To require a specific
cluster:

```bash
PLATFORM_APP_HEALTH_CNPG_CLUSTERS="platform-databases/platform-postgres" make platform-app-health
```

To also require the CloudNativePG object-storage Secret used by the premium
backup example:

```bash
PLATFORM_APP_HEALTH_CNPG_OBJECT_STORAGE_SECRET=true \
CNPG_OBJECT_STORE_SECRET_NAME=cnpg-object-store \
make platform-app-health
```

If a cluster intentionally deploys only part of the stack, override the app,
namespace, and GUI route lists together. For example:

```bash
PLATFORM_APP_HEALTH_REQUIRED_APPS="cert-manager trust-manager metallb traefik longhorn cloudnativepg platform-postgres platform-valkey forgejo woodpecker" \
PLATFORM_APP_HEALTH_NAMESPACES="argocd cert-manager cnpg-system platform-databases platform-cache forgejo woodpecker longhorn-system metallb-system traefik" \
PLATFORM_APP_HEALTH_GUI_APPS="argocd forgejo woodpecker" \
make platform-app-health
```

To bootstrap Argo CD without manually copying commands:

```bash
make platform-argocd
```

This target downloads only the exact Argo CD release recorded by the vendored
chart, rejects redirects, caps the manifest size and transfer time, and checks
its reviewed SHA-256 before applying cluster-scoped resources. The automatic
HA-to-core fallback verifies its separate core manifest the same way. A digest
failure is a hard stop: update the vendored chart and both reviewed manifest
hashes together instead of bypassing verification with a runtime URL.

`make platform-argocd` also exposes Argo CD through a temporary bootstrap NodePort. The default browser URL is `https://<NODE_1_IP>:30443`. The NodePort probe is soft by default because some host firewalls or CNI/kube-proxy paths block direct NodePort access even though the final Traefik/MetalLB ingress will work. To expose an already-installed Argo CD instance again:

```bash
make platform-argocd-expose
```

To use a different bootstrap port:

```bash
PLATFORM_ARGOCD_BOOTSTRAP_NODEPORT_HTTPS=31443 make platform-argocd-expose
```

To require the temporary NodePort to pass from every node:

```bash
PLATFORM_ARGOCD_BOOTSTRAP_NODEPORT_VERIFY_MODE=strict make platform-argocd-expose
```

After Traefik and MetalLB provide the real ingress URL, remove the temporary NodePort exposure:

```bash
make platform-argocd-unexpose
```

If Argo CD bootstrap fails with `metadata.annotations: Too long` for `applicationsets.argoproj.io`, rerun `make platform-argocd` after updating to this version of the playbook. The bootstrap uses server-side apply so large Argo CD CRDs are not stored in the client-side `last-applied` annotation.

If the playbook is waiting at Argo CD rollout, it polls for up to 600 seconds by default and prints pod/event diagnostics plus a likely-cause summary on failure. To extend the wait for slow image pulls:

```bash
PLATFORM_ARGOCD_ROLLOUT_TIMEOUT=1200 make platform-argocd
```

After an Argo CD timeout, collect the live state again without changing the cluster:

```bash
make platform-argocd-diagnose
```

The diagnostic target prints pods, workloads, services, CRDs, images, pod events/details, recent logs, recent events, and registry reachability checks for the image registries detected in Argo CD pods.

If Argo CD applications stay `Unknown` and the controller logs show timeouts to
`argocd-repo-server:8081` or `argocd-redis:6379`, repair Argo CD's internal
service path:

```bash
make platform-argocd-service-repair
```

This creates headless internal services for the Argo CD repo-server and Redis,
points Argo CD at those services, restarts the Argo CD workloads, and refreshes
Longhorn and Forgejo. It is useful when ordinary ClusterIP routing is unhealthy
but pod-to-pod routing is still working.

Legacy Traefik chart resources are classified and pruned only after the Argo CD
controllers, repo server, and application retry path have stabilized. The
pruner waits for a newly observed reconciliation and refuses resources outside
its explicit legacy allowlist, so an unavailable application controller cannot
block its own service-path repair with a stale hard-refresh annotation.
Platform application hard refreshes are requested together and monitored within
one shared timeout, which defaults to 120 seconds and can be changed with
`PLATFORM_ARGOCD_SERVICE_REPAIR_REFRESH_TIMEOUT`. The repair clears a stale
refresh hint before issuing a new one. Completion accepts either controller
consumption of the refresh annotation or an advanced `status.reconciledAt`, while
still waiting for any active sync operation to finish. If Argo CD does not
acknowledge the hint but the application remains idle and `Synced`, the hint is
removed and repair continues. Legacy Traefik pruning is skipped in that case so
the repair never prunes from an unrefreshed resource inventory.

If the controller then times out to a pod IP such as `10.42.x.x:8081`, the
cluster still has a pod-to-pod path problem. The repair target defaults to a
bootstrap node-local and host-network fallback for Argo CD's controller,
repo-server, and Redis so first deployment can continue while the wider CNI path
is investigated:

```bash
make platform-argocd-service-repair
```

To disable that node-local fallback and only create the headless services:

```bash
PLATFORM_ARGOCD_SERVICE_REPAIR_NODE_LOCAL=false make platform-argocd-service-repair
```

To keep node-local placement but avoid the host-network/direct-node-IP fallback:

```bash
PLATFORM_ARGOCD_SERVICE_REPAIR_HOST_NETWORK=false make platform-argocd-service-repair
```

If Forgejo is synced but stuck in `Pending` because the `longhorn-critical`
StorageClass does not exist and the Longhorn Argo CD application remains
`Unknown`, bootstrap Longhorn directly through the RKE2 Helm controller:

```bash
make platform-longhorn-bootstrap
```

This applies the premium Longhorn storage classes, installs the Longhorn Helm
chart, creates the default Longhorn data path on every node, configures a
schedulable default disk on each Longhorn node object, refreshes the Longhorn
and Forgejo Argo CD applications, and prints storage/PVC status. If the
`longhorn-critical` priority class already exists, the bootstrap adopts it for
Helm metadata instead of changing immutable fields. It is a first-deployment
recovery path for the storage chicken-and-egg case; after Argo CD and pod
networking are healthy, GitOps continues to own the desired Longhorn manifests.
The bootstrap loads the reviewed chart archive committed beside the vendored
Longhorn source, verifies its pinned SHA-256, and places it in RKE2 HelmChart
`chartContent`. It does not download a chart repository index or CRD manifest at
runtime. Helm `v3.21.0` must be installed on the Ansible controller so the CRDs
can be rendered from the same vendored chart with the project's Kubernetes
`1.35` API capabilities. `PLATFORM_LONGHORN_CHART_VERSION`
may only select the version recorded in that chart's `Chart.yaml`; upgrading
Longhorn requires a reviewed source, archive, checksum, and GitOps pin update.

If Longhorn pods show `ImagePullBackOff` for `docker.io/longhornio/*` with
`TLS handshake timeout` or `connection reset by peer`, the Longhorn chart is
installed but node image egress to Docker Hub is flaky. The bootstrap discovers
the Longhorn image set and pre-pulls it on every RKE2 node with retries before
waiting for workloads. For slow enterprise links, increase the per-image pull
timeout:

```bash
PLATFORM_LONGHORN_IMAGE_PULL_TIMEOUT=600 \
PLATFORM_LONGHORN_IMAGE_PULL_RETRIES=6 \
PLATFORM_LONGHORN_WAIT_TIMEOUT=2400 \
make platform-longhorn-bootstrap
```

To skip node pre-pulls and only rely on kubelet image pulls:

```bash
PLATFORM_LONGHORN_PREPULL_IMAGES=false make platform-longhorn-bootstrap
```

For a quick diagnostic failure instead:

```bash
PLATFORM_LONGHORN_IMAGE_PULL_FAST_FAIL=true make platform-longhorn-bootstrap
```

For production, configure a local registry mirror or preload the Longhorn images
on all RKE2 nodes.

All Forgejo diagnostics and repair targets validate and normalize
`inventory/hosts.local.ini` before invoking Ansible. If the inventory is
missing, malformed, or still contains example node values, the target stops
before contacting the cluster. Replace the example node addresses and SSH
users in the ignored local inventory, then rerun the target:

```bash
make platform-inventory-preflight
make platform-forgejo-diagnose
```

After node disk pressure, eviction, or a Longhorn CSI registration outage, use
the data-safe end-to-end recovery target:

```bash
make platform-forgejo-repair
```

It repairs the Longhorn runtime, removes only terminal Forgejo pods or old
unscheduled `Pending` pods when no ready Forgejo backend exists, waits for a
ready `forgejo-http` endpoint, and then verifies the published ingress. When an
old Kubernetes `VolumeAttachment` says `attached=true` while Longhorn proves
that its RWO engine is stopped and unassigned, the repair deletes the Pending
controller pod and recycles only that stale attachment record so CSI can retry.
It does not enable `PLATFORM_FORGEJO_RESET_STUCK_PVC` and never deletes the
Forgejo PVC/PV, Longhorn volume, engine, or replicas. If storage cannot be
recovered without risking data, the target stops with focused diagnostics.

If the Forgejo PVC is `Bound` but the Forgejo pod remains in `Init:*`, Longhorn
has provisioned storage and the next useful signal is the Forgejo pod's
init-container state, logs, PVC/PV mapping, and Longhorn volume attachment:

```bash
make platform-forgejo-diagnose
```

If the Forgejo PVC is stuck in `Terminating` during the first deployment, stop
before manually removing finalizers. The repair target diagnoses the state by
default, and only resets storage when explicitly allowed:

```bash
make platform-forgejo-storage-repair
PLATFORM_FORGEJO_RESET_STUCK_PVC=true make platform-forgejo-storage-repair
```

Use the reset flag only when Forgejo is still empty. It scales Forgejo down,
removes the stuck first-deploy PVC/PV state, optionally removes the old Longhorn
volume, and refreshes the Forgejo Argo CD application so the chart can recreate
clean storage.

If Forgejo events show `AttachVolume.Attach failed` with `node <name> not
found`, the pod cannot start because Longhorn has not registered that Kubernetes
node as a healthy Longhorn node yet, or the instance-manager/engine-image pods
for that node are still unhealthy. Check the Longhorn node objects and manager
logs:

```bash
kubectl api-resources --api-group=longhorn.io -o wide
kubectl get crd | grep 'longhorn\.io'
kubectl -n longhorn-system get nodes.longhorn.io instancemanagers.longhorn.io engineimages.longhorn.io -o wide
kubectl -n longhorn-system logs -l app=longhorn-manager --all-containers --tail=180
```

The Forgejo storage repair target also detects this attach failure, restarts the
Longhorn manager DaemonSet, waits for Longhorn node objects to match Kubernetes
nodes, and restarts the Forgejo pod so the volume attach is retried:

```bash
make platform-forgejo-storage-repair
```

For slow clusters:

```bash
PLATFORM_FORGEJO_VOLUME_ATTACH_REPAIR_TIMEOUT=900 make platform-forgejo-storage-repair
```

To fail faster after the repair retry and print attach diagnostics sooner:

```bash
PLATFORM_FORGEJO_POD_IP_WAIT_TIMEOUT=120 make platform-forgejo-storage-repair
```

If the PV has `longhorn.io/volume-scheduling-error: precheck new replica failed:
disks are unavailable` and `nodes.longhorn.io` shows empty `Spec.Disks`, the
repair target validates the Longhorn path before creating or registering a
schedulable disk. In strict mode it stops when the path is missing or shares
the root filesystem, because using `/var/lib/longhorn` on `/` can trigger
DiskPressure even when user repositories are empty. Mount a dedicated disk on
each node and use the same path:

```bash
PLATFORM_LONGHORN_DEFAULT_DISK_PATH=/mnt/longhorn make platform-forgejo-storage-repair
```

The same path is required when rendering the premium Longhorn values. A
reviewed non-production lab can explicitly set
`PLATFORM_LONGHORN_DEDICATED_STORAGE_REQUIRED=false`, but this is not a
production-capacity exception.

If the Forgejo pod is `1/1 Running` but
`https://<GIT_FQDN>` returns Traefik's plain `404 page not found`, the app VIP
and Traefik are reachable but no Forgejo router matched the hostname. Publish
and verify the explicit Forgejo Traefik route:

```bash
make platform-forgejo-ingress
```

To fail faster or wait longer while debugging VIP convergence:

```bash
PLATFORM_FORGEJO_INGRESS_VERIFY_TIMEOUT=60 make platform-forgejo-ingress
PLATFORM_FORGEJO_INGRESS_VERIFY_TIMEOUT=600 make platform-forgejo-ingress
```

If Forgejo is `1/1 Running` and the host returns `502 Bad Gateway` or `504`,
the VIP and Traefik are reachable but the native Kubernetes Service path may
be failing. `platform-forgejo-ingress` now retries the published route in
endpoint mode automatically after the first VIP probe fails. The fallback is
enabled by default and can be disabled when native Service load balancing is
required:

```bash
PLATFORM_FORGEJO_INGRESS_NATIVE_LB_FALLBACK=false make platform-forgejo-ingress
```

If endpoint mode also fails, the target prints the Forgejo Service,
EndpointSlice, Traefik, and node-path diagnostics. A failure with no ready
endpoints still requires repairing the Forgejo pod, database, or PVC before
ingress can work; the fallback does not bypass an unhealthy application.

If Longhorn manager logs repeat `the server could not find the requested
resource` for `nodes.longhorn.io`, `engines.longhorn.io`, or
`engineimages.longhorn.io`, restore the missing CRDs and restart Longhorn:

```bash
make platform-longhorn-crd-repair
```

The repair renders `templates/crds.yaml` from the reviewed vendored Longhorn
chart and applies only that local output. Runtime overrides such as a remote CRD
manifest URL are intentionally unsupported, preventing cluster-scoped recovery
from trusting mutable network content.

Both `platform-longhorn-crd-repair` and the full Longhorn bootstrap need Helm
only for this local render. If Helm is absent, their runners automatically
install the repository's checksum-pinned version under
`${XDG_CACHE_HOME:-$HOME/.cache}/platform-gitops/tools`. Set
`PLATFORM_AUTO_INSTALL_LOCAL_HELM=false` to require a preinstalled Helm binary,
or set `PLATFORM_LOCAL_TOOL_CACHE_DIR` to use an approved local tool cache.

If `kubectl apply` reports `PriorityClass "longhorn-critical" is invalid:
value: Forbidden: may not be changed in an update`, leave the existing
PriorityClass value alone. The bootstrap now only patches Helm ownership
metadata on an existing `longhorn-critical` PriorityClass and no longer tries to
update its immutable `value`.

If only the Argo CD HA Redis pods are failing, you can continue with a simpler bootstrap control plane while investigating Redis HA separately:

```bash
make platform-argocd-core
make platform-status
```

`make platform-argocd-core` removes stale Argo CD HA Redis bootstrap resources and applies the standard Argo CD install manifest. The default `make platform-argocd` starts with the HA manifest, but automatically falls back to core mode when the known HA Redis announce-service bootstrap failure is detected. Use `make platform-argocd-ha` for strict HA-only behavior with no automatic core fallback.

To register platform applications, provide the repository URL and explicitly allow GitOps app registration:

```bash
PLATFORM_REPO_URL=<THIS_REPO_URL> PLATFORM_APPLY_GITOPS=true make platform-argocd
```

The playbook checks the selected GitOps profile for unresolved placeholders before it registers applications. This prevents Argo CD from syncing incomplete domains, storage sizes, backup targets, or secret references.

For the premium profile, the unattended renderer can clear most first-deploy
placeholders automatically:

```bash
make platform-render-private-values
make platform-app-secrets
PLATFORM_PROFILE=premium-3node make platform-profile-check
```

Set Forgejo dependency, Woodpecker datasource, Keycloak admin/database, Harbor dependency, Grafana database, and
object-storage values in ignored env files or your secret manager before running
`platform-app-secrets`:
`FORGEJO_DATABASE_PASSWORD`, `FORGEJO_REDIS_URL`,
`WOODPECKER_DATABASE_DATASOURCE`, or `WOODPECKER_DATABASE_HOST` plus
`WOODPECKER_DATABASE_PASSWORD`, plus `HARBOR_DATABASE_PASSWORD`,
`HARBOR_REDIS_PASSWORD`, `HARBOR_S3_ACCESS_KEY_ID`,
`HARBOR_S3_SECRET_ACCESS_KEY`, `KEYCLOAK_ADMIN_PASSWORD`,
`KEYCLOAK_DATABASE_PASSWORD`, `GRAFANA_DATABASE_PASSWORD`,
`LOKI_S3_ACCESS_KEY_ID`,
`LOKI_S3_SECRET_ACCESS_KEY`, `VELERO_CLOUD_CREDENTIALS`,
`CNPG_S3_ACCESS_KEY_ID`, `CNPG_S3_SECRET_ACCESS_KEY`, or the shared
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.
For production verification, set
`PLATFORM_APP_SECRET_REQUIRE_OBJECT_STORAGE=true`; `platform-app-secrets` will
then fail immediately if the Loki, Velero, or CloudNativePG credential secret is still missing.
Set `PLATFORM_APP_SECRET_REQUIRE_CNPG_OBJECT_STORAGE=true` to require only the
CloudNativePG object-storage secret.
Set `PLATFORM_APP_SECRET_REQUIRE_WOODPECKER_DATABASE=true` when enabling
Woodpecker HA so a missing PostgreSQL datasource secret fails before rollout.
Set `PLATFORM_APP_SECRET_REQUIRE_HARBOR_DATABASE=true`,
`PLATFORM_APP_SECRET_REQUIRE_HARBOR_REDIS=true`, and
`PLATFORM_APP_SECRET_REQUIRE_HARBOR_REGISTRY_STORAGE=true` when enabling Harbor
external PostgreSQL, Redis, and S3 registry storage.
Set `PLATFORM_APP_SECRET_REQUIRE_FORGEJO_DATABASE=true` when enabling an
external Forgejo SQL backend. Set `PLATFORM_APP_SECRET_REQUIRE_FORGEJO_REDIS=true`
only when `FORGEJO_REDIS_MODE=redis`; the default secret names are
`FORGEJO_DATABASE_SECRET_NAME=forgejo-database` and
`FORGEJO_REDIS_SECRET_NAME=forgejo-redis`.
Set `PLATFORM_APP_SECRET_REQUIRE_FORGEJO_OBJECT_STORAGE=true` for production
Forgejo and provide `FORGEJO_S3_ACCESS_KEY_ID` plus
`FORGEJO_S3_SECRET_ACCESS_KEY`. If this check fails, verify that the managed
`forgejo-object-storage` Secret contains `access-key-id` and
`secret-access-key`, that `FORGEJO_S3_ENDPOINT` is an external HTTPS
S3-compatible endpoint, and that the configured bucket exists.
Set `PLATFORM_APP_SECRET_REQUIRE_GRAFANA_DATABASE=true` when enabling
`GRAFANA_DATABASE_MODE=postgres`.
Set `PLATFORM_APP_SECRET_REQUIRE_KEYCLOAK_DATABASE=true` when you require a
specific Keycloak database password instead of generated first-deploy
credentials.

If Argo CD controller logs show timeouts to the Kubernetes API service IP or an
Argo CD Redis ClusterIP, the pod-to-service path is unhealthy. First deployment
runs `platform-dns-repair` automatically by default; for a standalone repair
run:

```bash
make platform-dns-repair
```

For first bootstrap, use skip mode to register only deployable apps and print
the incomplete apps:

```bash
PLATFORM_GITOPS_PLACEHOLDER_MODE=skip-incomplete \
PLATFORM_REPO_URL=<THIS_REPO_URL> \
PLATFORM_APPLY_GITOPS=true \
make platform-argocd
```

Use strict mode after all private values are resolved:

```bash
PLATFORM_GITOPS_PLACEHOLDER_MODE=strict \
PLATFORM_REPO_URL=<THIS_REPO_URL> \
PLATFORM_APPLY_GITOPS=true \
make platform-argocd
```

## MetalLB does not assign addresses

Check that the address pool was customized from placeholders to your private network range in ignored or encrypted configuration.

Deploy or repair the full ingress foundation from `inventory/hosts.local.ini`:

```bash
make platform-ingress
make platform-status
```

This installs MetalLB and Traefik through the RKE2 Helm controller, applies the configured app VIP, publishes Argo CD at the effective Argo CD hostname, verifies it on 443, and removes the temporary Argo CD NodePort exposure.

Traefik is configured to redirect HTTP traffic on the app VIP to HTTPS. The ingress playbook also publishes a Traefik Middleware/IngressRoute that redirects direct app VIP browser requests such as `https://<APP_VIP>/` to the canonical Argo CD hostname. By default the target is the effective Argo CD hostname: `platform_argocd_host` when set, otherwise `argocd.<PLATFORM_DOMAIN>`. To override only the direct-IP redirect target:

```bash
PLATFORM_IP_REDIRECT_TARGET_HOST=<ARGOCD_FQDN> make platform-ingress
```

The target hostname must also have a real Argo CD route. If you change service FQDNs later, update the Argo CD ingress host and the redirect target together. Browsers may still show a certificate warning before redirecting from `https://<APP_VIP>/`, because production certificates normally cover DNS names, not private IP addresses.

To disable the direct app VIP to hostname redirect:

```bash
PLATFORM_IP_REDIRECT_ENABLED=false make platform-ingress
```

To shorten a MetalLB or Traefik wait while testing:

```bash
PLATFORM_INGRESS_ROLLOUT_TIMEOUT=180 make platform-ingress
```

To wait longer on slow chart/image pulls:

```bash
PLATFORM_INGRESS_ROLLOUT_TIMEOUT=1200 make platform-ingress
```

Traefik has its own rollout/VIP wait. If the playbook is waiting at `Wait for Traefik deployment`, use this instead of changing every ingress phase:

```bash
PLATFORM_TRAEFIK_ROLLOUT_TIMEOUT=180 make platform-ingress
PLATFORM_TRAEFIK_ROLLOUT_TIMEOUT=1200 make platform-ingress
```

If Traefik still times out, the final failure message includes a compact status summary with HelmChart/job state, pods, waiting reasons, images, and recent events. Use that summary first; the longer diagnostics printed above it contain full pod descriptions and logs.

The Traefik rollout poll is an instant readiness check, so the retry counter maps closely to `PLATFORM_TRAEFIK_ROLLOUT_TIMEOUT` and `PLATFORM_INGRESS_POLL_INTERVAL` without an extra hidden `kubectl rollout status` wait on every attempt.

If that summary shows `helm-install-platform-traefik` in `BackOff` or repeatedly `Running` with no Traefik deployment created, the Helm install job is failing before Traefik starts. Rerun `make platform-ingress` with the current playbook so stale Helm jobs are cleaned and the checksum-verified local chart plus schema-compatible values are reapplied. If it still fails, read the `Helm install pod log tail` in the final failure summary; it normally shows the exact rejected value, image-pull failure, admission failure, or scheduling problem. A chart download or chart-repository DNS failure is not expected because the HelmChart uses embedded `chartContent`.

To run only the Traefik chart-repository DNS repair:

```bash
make platform-dns-repair-traefik
```

The per-node chart-repository check is now an explicit diagnostic rather than a deployment prerequisite. Enable it with `PLATFORM_TRAEFIK_CHART_REPO_DNS_CHECK=true make platform-ingress`, or run `make platform-dns-repair-traefik` directly. It creates one short-lived pod per Kubernetes node so a node-specific failure such as `<POD_IP> -> <CLUSTER_DNS_SERVICE_IP>:53 i/o timeout` cannot slip through. Each pinned pod prints the Kubernetes DNS service IP probe, live CoreDNS endpoint probes, CoreDNS endpoint placement by node, explicit `PLATFORM_NODE_DNS_SERVICE_OK` / `PLATFORM_NODE_COREDNS_ENDPOINT_OK` markers, and retries Helm repository add/update before failing. When enabled, the same host CNI, kube-proxy, Cilium, CoreDNS, firewalld, and failed-node recovery path remains available for network diagnosis.

The checker treats Helm output such as `Unable to get an update` as unhealthy even when Helm exits with status `0`, because that usually means the repo path is still flaky and a later Helm install job may fail on the same node. If the DNS service IP probe fails but CoreDNS endpoint probes work, focus on kube-proxy or service NAT rules. If both service and endpoint probes fail from the same node, focus on Cilium pod routing, host firewall zones, VXLAN/Geneve, or node egress. If Cilium health shows host connectivity OK but remote endpoint HTTP timeouts, the node-to-node underlay is reachable but pod-to-pod L4 forwarding is still blocked or filtered. If the post-repair retry still fails, the final message includes failed-node Kubernetes diagnostics plus host network/firewalld/Cilium/kube-proxy, iptables, nft, and conntrack diagnostics for the affected node.

If the explicitly enabled host service-path diagnostic and retry still fail on a specific node, `platform-ingress` restarts `rke2-server` only on the failed node, waits for it to report Ready, waits for Cilium and kube-proxy on that node, and performs one final per-node DNS retry. To disable that heavier recovery step:

```bash
PLATFORM_TRAEFIK_CHART_REPO_DNS_CHECK=true PLATFORM_TRAEFIK_DNS_FAILED_NODE_RESTART=false make platform-ingress
```

To adjust how long the playbook waits for the restarted node:

```bash
PLATFORM_TRAEFIK_CHART_REPO_DNS_CHECK=true PLATFORM_TRAEFIK_DNS_FAILED_NODE_RESTART_TIMEOUT=300 make platform-ingress
```

For a three-node HA control plane, the repair path targets three CoreDNS replicas with topology spread and preferred anti-affinity so every node can get a local DNS endpoint when the scheduler can place one. It also patches the CoreDNS service with `internalTrafficPolicy: Local`, so a pod uses the CoreDNS endpoint on its own node instead of randomly hitting a remote CoreDNS endpoint when cross-node pod DNS is flaky. RKE2's CoreDNS autoscaler can reconcile the Deployment back to two replicas, so the repair path temporarily scales that autoscaler to zero before enforcing the fixed HA CoreDNS placement. To override the Traefik per-node repair target replica count:

```bash
PLATFORM_TRAEFIK_CHART_REPO_DNS_CHECK=true PLATFORM_TRAEFIK_DNS_COREDNS_REPLICAS=3 make platform-ingress
```

The default per-node check timeout is 300 seconds. To change it:

```bash
PLATFORM_TRAEFIK_CHART_REPO_DNS_CHECK=true PLATFORM_TRAEFIK_DNS_CHECK_TIMEOUT=300 make platform-ingress
```

The post-repair per-node retry uses the same timeout by default. To make only the retry fail faster:

```bash
PLATFORM_TRAEFIK_CHART_REPO_DNS_CHECK=true PLATFORM_TRAEFIK_DNS_RETRY_TIMEOUT=120 make platform-ingress
```

To tolerate short intermittent CoreDNS or chart-repository lookup failures during the per-node check:

```bash
PLATFORM_TRAEFIK_CHART_REPO_DNS_CHECK=true PLATFORM_TRAEFIK_DNS_HELM_ATTEMPTS=5 PLATFORM_TRAEFIK_DNS_HELM_TIMEOUT=60 make platform-ingress
```

To disable the host service-path repair pass while collecting diagnostics:

```bash
PLATFORM_TRAEFIK_CHART_REPO_DNS_CHECK=true PLATFORM_TRAEFIK_DNS_SERVICE_PATH_REPAIR=false make platform-ingress
```

If applying the app VIP fails with `failed calling webhook` or `context deadline exceeded` for `metallb-webhook-service`, the Kubernetes API server could not reach MetalLB's validating webhook yet. The ingress playbook now waits for the webhook service endpoints and runs a server-side dry-run of the MetalLB pool before creating the real resources. To wait longer for that webhook phase:

```bash
PLATFORM_METALLB_WEBHOOK_TIMEOUT=1200 make platform-ingress
```

Each webhook service-path and admission probe is bounded separately so a bad webhook path does not make every retry wait on Kubernetes' default admission timeout. The default is 5 seconds per probe:

```bash
PLATFORM_METALLB_WEBHOOK_PROBE_TIMEOUT=3 PLATFORM_METALLB_WEBHOOK_TIMEOUT=120 make platform-ingress
```

When the webhook check still fails, `platform-ingress` automatically restarts the MetalLB controller, refreshes kube-proxy, restarts Cilium, and retries the webhook dry-run before stopping. Disable that recovery path only when you want diagnostics without component restarts:

```bash
PLATFORM_METALLB_WEBHOOK_REPAIR=false make platform-ingress
```

If `helm-install-platform-metallb` or `helm-install-platform-traefik` stays `Running` for many minutes with restarts, rerun:

```bash
make platform-ingress
```

The ingress playbook cleans stale platform Helm install jobs before retrying by default and prints HelmChart/job/pod logs if CRDs still do not appear. To disable cleanup while debugging:

```bash
PLATFORM_INGRESS_CLEANUP_HELM_JOBS=false make platform-ingress
```

After Traefik receives the app VIP, `platform-ingress` verifies Argo CD through the effective Argo CD hostname using the app VIP. Before that check, the playbook enforces Traefik `externalTrafficPolicy: Local` and `internalTrafficPolicy: Local`, then verifies every RKE2 node has a ready local Traefik endpoint. It publishes Argo CD with both a standard Kubernetes Ingress and, when Traefik CRDs are available, a native Traefik `IngressRoute`. Node-originated VIP probes still run and print diagnostics, but they are advisory by default because MetalLB L2 VIP self-probes from Kubernetes nodes can fail even when real clients can reach the VIP. The hard gate is the Ansible controller/client path to the app VIP. If that check returns `http_code=404`, the VIP reached Traefik but no Argo CD router matched; review the printed Ingress and IngressRoute diagnostics. If it times out with `curl: (28)` or HTTP code `000`, the playbook automatically refreshes MetalLB speaker announcements, flushes the app VIP neighbor cache on the RKE2 nodes and Ansible controller, attempts a Windows host ARP flush when running from WSL, and retries before failing. A remaining timeout normally means app VIP L2/ARP, host firewall/routing, or client-to-VIP path rather than Argo CD itself. To shorten just this final verification while debugging:

```bash
PLATFORM_ARGOCD_INGRESS_VERIFY_TIMEOUT=120 make platform-ingress
```

To classify the live path without redeploying anything:

```bash
make platform-ingress-diagnose
```

The diagnose target checks the Traefik LoadBalancer service, MetalLB pool and L2Advertisement, Traefik and Argo CD endpoints, app VIP TCP reachability, direct Traefik NodePort reachability from the Ansible controller, and Windows/WSL ARP state when those tools are available. If direct NodePort works but `<APP_VIP>:443` times out, focus on MetalLB L2/ARP, same-VLAN reachability, duplicate VIP ownership, host firewall forwarding, or virtualization switch security such as MAC address changes and forged transmits. If one node's direct Traefik NodePort returns HTTP but other nodes accept TCP and then time out, the playbook treats that as a node-local backend path problem and repairs Argo CD server placement plus Traefik native Kubernetes service load balancing before retrying.

To disable the automatic MetalLB speaker and neighbor-cache repair pass:

```bash
PLATFORM_ARGOCD_INGRESS_VIP_REPAIR=false make platform-ingress
```

To make node-originated VIP probes a strict deployment gate:

```bash
PLATFORM_ARGOCD_INGRESS_NODE_STRICT=true make platform-ingress
```

If image-pull or other workload logs show `lookup ... on ...:53: i/o timeout`, pod DNS cannot resolve an external endpoint through CoreDNS. Ingress charts no longer need external repositories, so DNS repair is an explicit operation:

```bash
make platform-dns-repair
```

The repair excludes Kubernetes DNS service IPs from CoreDNS upstream candidates. If a node resolver points back to the cluster DNS service, forwarding CoreDNS to that address creates a DNS loop and pod lookups will time out. The playbook also tests discovered upstream candidates from inside a pod and configures CoreDNS only with candidates that resolve the chart repository from the cluster network.

If direct upstream DNS works from pods but Kubernetes DNS service lookups still time out, the problem is the Kubernetes DNS service path rather than the upstream resolver. The repair now applies the CNI service-path host prerequisites on all nodes, including reverse-path-filter sysctls, active-interface reverse-path filtering, Cilium VXLAN/Geneve firewalld ports, trusted pod CIDR and node IP firewalld sources, stable Cilium/firewalld interfaces, and direct pod/CNI ACCEPT rules, then restarts kube-proxy when present, Cilium, and CoreDNS. Before contacting firewalld, it atomically removes stale per-pod `lxc*`, `veth*`, `cni*`, and non-stable `cilium*` interface bindings from the permanent trusted zone. Those transient names are already covered by CIDR and wildcard rules and must not accumulate as pods churn; a large trusted zone can otherwise cause excessive firewalld CPU and memory usage or exceed its systemd startup deadline. Stable Cilium, WireGuard, and `cni0` interfaces are retained. Firewalld reloads are serialized across the RKE2 nodes; if firewalld is enabled but failed or stuck after a D-Bus timeout, the repair resets and restarts it from the cleaned configuration, waits for it to answer, and verifies the loaded runtime rules before proceeding. To disable that bootstrap repair step:

```bash
PLATFORM_DNS_SERVICE_PATH_REPAIR=false make platform-dns-repair
```

The service-path repair is split into visible kube-proxy, Cilium, and CoreDNS tasks. If kube-proxy is delivered as static RKE2 pods instead of a DaemonSet, the playbook deletes those pods and waits for all three replacements to become Running before retrying DNS. Each rollout waits up to 120 seconds by default and polls every 5 seconds. To shorten that while troubleshooting:

```bash
PLATFORM_DNS_SERVICE_PATH_ROLLOUT_TIMEOUT=45 \
PLATFORM_DNS_SERVICE_PATH_POLL_INTERVAL=5 \
make platform-dns-repair
```

After those component restarts, the playbook enforces CoreDNS HA placement, patches the CoreDNS service with `internalTrafficPolicy: Local`, re-detects the current CoreDNS endpoint IPs, and reruns the service-path DNS probe before printing the final classification. This avoids diagnosing stale CoreDNS pod IPs after a rollout and avoids kube-proxy load-balancing DNS requests to remote CoreDNS endpoints when every node has a local CoreDNS pod. To override the generic DNS repair target replica count:

```bash
PLATFORM_DNS_COREDNS_REPLICAS=3 make platform-dns-repair
```

The static kube-proxy delete request is non-blocking and uses a 30-second Kubernetes API request timeout by default. To make that fail faster:

```bash
PLATFORM_DNS_KUBE_PROXY_DELETE_TIMEOUT=10 make platform-dns-repair
```

If the playbook says direct upstream DNS works but direct CoreDNS endpoint DNS fails, pod-to-pod overlay traffic is still broken. Rerun node preparation so firewalld trusts the pod CIDR, RKE2 node IPs, and Cilium interfaces on every node, and so active-interface reverse-path filtering is disabled:

```bash
make rke2-prepare
make platform-dns-repair
```

For non-default RKE2 pod CIDRs, override the trusted CIDR:

```bash
PLATFORM_DNS_POD_CIDRS="<RKE2_POD_CIDR>" make platform-dns-repair
```

To force explicit CoreDNS upstreams:

```bash
PLATFORM_DNS_UPSTREAMS="DNS_SERVER_1 DNS_SERVER_2" make platform-dns-repair
```

To shorten or extend the DNS test window:

```bash
PLATFORM_DNS_CHECK_TIMEOUT=60 make platform-dns-repair
```

To make each in-pod DNS/HTTPS probe fail faster while keeping the outer check window:

```bash
PLATFORM_DNS_PROBE_TIMEOUT=10 PLATFORM_DNS_CHECK_TIMEOUT=60 make platform-dns-repair
```

If a previous interrupted run left a stale DNS check Job and Kubernetes is slow to delete it, the playbook waits up to 30 seconds before recreating the Job. To fail faster while debugging:

```bash
PLATFORM_DNS_JOB_CLEANUP_TIMEOUT=10 make platform-dns-repair
```

Helm repository add/update uses a separate retry and timeout because chart repository access can be slower or briefly flakier than DNS probes. The default is 3 attempts and 90 seconds per Helm command:

```bash
PLATFORM_DNS_HELM_ATTEMPTS=5 PLATFORM_DNS_HELM_TIMEOUT=60 make platform-dns-repair
```

If you only want to increase the per-command wait without adding attempts:

```bash
PLATFORM_DNS_HELM_TIMEOUT=180 make platform-dns-repair
```

If the DNS check resolves a public IPv6 address and then fails with `network is unreachable`, keep the default IPv4-only DNS repair mode enabled. The repair suppresses external AAAA answers through CoreDNS so in-cluster Helm jobs use IPv4. Disable it only on networks with working IPv6 egress:

```bash
PLATFORM_DNS_IPV4_ONLY=false make platform-dns-repair
```

If resolution succeeds but the optional Helm repository diagnostic times out,
the problem is pod egress rather than CoreDNS, or the diagnostic timeout is too
short for your network path. Check firewall, NAT/masquerade, proxy policy, and
TLS inspection. MetalLB and Traefik deployment artifacts are local and do not
accept mirror overrides. To test a specific internal Traefik mirror without
using it as executable deployment input:

```bash
PLATFORM_TRAEFIK_CHART_REPO="https://<INTERNAL_HELM_MIRROR>/traefik" \
make platform-dns-repair-traefik
```

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

`make rke2-verify` enforces the API VIP and API DNS path by default. It probes
`/readyz` from every RKE2 node and then performs authenticated readiness checks
through both configured API endpoints. Use the pre-VIP mode only before the VIP
provider exists:

```bash
RKE2_VERIFY_API_VIP=false make rke2-verify
```

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
The `rke2-install` target also runs preflight and node preparation first, including Rocky/RHEL 10 `kernel-modules-extra`, kernel modules, swap disablement, CNI sysctls, active-interface reverse-path filtering, Cilium overlay firewalld ports, trusted pod CIDR/node IP/Cilium firewalld handling, direct pod/CNI firewalld ACCEPT rules, and NetworkManager CNI handling.

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

This does not delete `/var/lib/rancher/rke2` cluster data. It reuses the existing first-server token, repairs config, opens firewalld ports, trusts the pod CIDR, node IPs, and Cilium interfaces, restarts services in the correct order, and waits for all three nodes to report Ready.

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
RKE2_VERSION='v1.36.2+rke2r1' make rke2-install
```

You can also use:

```bash
ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/install-rke2.yml \
  -e rke2_version='v1.36.2+rke2r1'
```

## CI cannot push images

Check Harbor robot account permissions. Do not commit robot account credentials.

## Secret scanner fails

Replace real values with placeholders or move them to ignored local files or encrypted secret workflows.
