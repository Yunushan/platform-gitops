# Capacity Planning Runbook

This runbook defines a public-safe production capacity planning model for
private platform deployments. Keep real usage metrics, internal hostnames,
customer names, quotas, costs, capacity tickets, and scaling evidence in the
private deployment repository or operations system, not in this public
template.

Capacity planning is a production control, not a one-time sizing exercise. The
goal is to notice saturation early, test growth paths before they are urgent,
and record why each scale decision was made.

## Principles

- Capacity decisions must have an owner, evidence, and rollback plan.
- Scale storage, databases, ingress, CI, registry, backup, and observability
  through Git where possible.
- Use measured saturation signals instead of only pod restart or outage events.
- Keep enough headroom for one-node failure in the 3-node profile.
- Prefer predictable limits, requests, retention, and quotas over emergency
  cleanup.
- Treat retention changes as data-handling changes and review
  `docs/DATA_CLASSIFICATION.md` before pruning logs, metrics, backups,
  registry artifacts, or database history.
- Validate capacity-sensitive changes during a maintenance window when they can
  affect persistent data or shared services.

## Capacity Domains

Track each domain separately because saturation in one layer can look like a
failure in another layer.

| Domain | What to watch | Common growth action |
|---|---|---|
| Kubernetes nodes and API | CPU, memory, allocatable pods, API latency, etcd health, scheduling failures | Add nodes, rebalance workloads, tune requests and limits |
| CNI, CoreDNS, kube-proxy, and service path | DNS latency, ClusterIP timeouts, endpoint coverage, conntrack pressure | Repair or scale networking components, review firewall rules |
| Ingress and VIP | VIP reachability, Traefik or ingress-nginx replica health, local endpoints, HTTP latency | Scale ingress, add local endpoints, review MetalLB pools |
| Argo CD | Application queue depth, repo-server latency, controller reconciliation delay, Redis health | Scale repo-server and server replicas, tune reconciliation load |
| Forgejo, Gitea, or GitLab | Repository size, request latency, SSH clone load, webhook delivery, database growth | Increase storage, database capacity, and worker resources |
| Woodpecker CI | Pending pipeline count, runner saturation, agent failures, artifact publish time | Add agents, tune concurrency, split runner pools |
| Harbor | Registry storage, scan queue, robot account usage, project quotas, garbage collection duration | Expand storage, tune retention, scale core and registry services |
| CloudNativePG | Database size, WAL growth, replication lag, backup age, query latency | Increase PVC size, tune resources, add replicas, review backup policy |
| Longhorn or alternate storage | Usable capacity, replica health, degraded volumes, snapshot count, rebuild time | Add disks, add nodes, change replica policy, prune snapshots safely |
| Velero and object storage | Backup age, backup duration, failed backups, bucket growth, restore drill duration | Expand object storage, tune backup scope, fix retention |
| Prometheus, Grafana, and Loki | Metrics retention size, scrape failures, log ingestion rate, dashboard latency | Tune retention, expand storage, shard or scale observability services |
| cert-manager, trust-manager, and step-ca | Certificate renewal backlog, issuer failures, trust bundle sync, CA storage health | Repair issuers, rotate trust material, expand CA storage |

## Baseline Inventory

Record a private baseline before production launch and after major topology
changes:

- Selected profile and enabled components.
- Node count, roles, CPU, memory, local disk, and storage backends.
- Storage classes, replica policy, backup target, and restore expectation.
- App VIP provider, ingress controller, DNS model, and TLS issuer model.
- Expected user count, repository count, CI concurrency, registry retention,
  log retention, metric retention, backup frequency, and restore objectives.
- Per-component owners for capacity decisions and emergency approval.

Use safe placeholders in public issues and documentation. Real baselines belong
in private evidence only.

## Saturation Signals

Review these signals before increasing load or enabling more applications:

- Node CPU and memory pressure.
- Disk usage and inode usage.
- Pod scheduling failures and pending pods.
- Kubernetes API latency and etcd health.
- CoreDNS query failures and DNS latency.
- ClusterIP, service path, and endpoint failures.
- VIP reachability and ingress request latency.
- PVC usage, volume degraded state, storage rebuild duration, and snapshot
  count.
- Database storage, WAL growth, replication lag, connection count, and backup
  age.
- CI queue depth, pending jobs, runner saturation, and failed agent heartbeats.
- Registry storage usage, garbage collection duration, vulnerability scan
  backlog, and project quota pressure.
- Prometheus retention size, scrape failures, rule evaluation failures, and
  Alertmanager queue health.
- Loki ingestion rate, query latency, and retention storage.
- Velero backup age, backup duration, object storage usage, and restore drill
  duration.
- Certificate renewal failures, issuer errors, and trust bundle sync failures.

When a signal crosses the private deployment threshold, open a change ticket
before the next maintenance window unless the platform is already degraded.

## Load and Scale Tests

Run load and scale tests in a lab or staging cluster when available. At
minimum, test the production profile before major growth:

- Simulate concurrent Git clone, push, pull request, and webhook traffic.
- Run representative Woodpecker pipelines with the expected parallelism.
- Push and pull representative images through Harbor.
- Generate Argo CD reconciliation load by syncing multiple applications.
- Exercise Prometheus and Loki ingestion at the planned retention window.
- Grow representative PostgreSQL data and WAL volume.
- Fill and expand test PVCs on the chosen storage backend.
- Run Velero backup and restore drills with realistic object counts and PVC
  sizes.
- Restart or drain one node and confirm the platform keeps the required
  service level.

Record test inputs, observed bottlenecks, changes made, and follow-up limits in
the private deployment record.

## Scaling Decisions

Use this decision path for capacity changes:

- Confirm the symptom with at least one workload signal and one platform signal.
- Identify the owning component and data class.
- Decide whether to scale vertically, scale horizontally, reduce retention, or
  change scheduling.
- Review data handling with `docs/DATA_CLASSIFICATION.md` when retention,
  backup, registry, log, metric, or database policy changes.
- Review access and secret impact with `docs/ACCESS_CONTROL.md` when new
  robot accounts, credentials, or storage targets are needed.
- Create the Git change and run repository validation.
- Apply during a maintenance window when the change touches databases, storage,
  ingress, backup, or PKI.
- Run the relevant health gates and record private evidence.

Useful gates:

```bash
make platform-status
make platform-app-health
make platform-capacity-verify
PLATFORM_PROFILE=<PROFILE> make platform-production-check
```

`platform-capacity-verify` is a read-only, fail-closed snapshot gate. It checks
every RKE2 node for root and platform-storage filesystem headroom, then checks
Ready and pressure conditions, requested CPU and memory, scheduled pod density,
and Longhorn schedulable-disk capacity. Production mode requires the configured
platform storage path to exist on a filesystem distinct from `/`; the gate
compares both the backing device and filesystem ID so an ordinary directory on
the root volume cannot satisfy this requirement. With production encryption
enforcement enabled, it also requires `cryptsetup` and `dm_crypt` on every node,
validates the three encrypted Longhorn StorageClasses and all CSI Secret
references, and rejects any bound Longhorn PVC or PV that is not encrypted. Its
production defaults require 15% free root space, 20% free platform and Longhorn
space, no more than 85% CPU or memory requests, and no more than 80% pod capacity
on any node. Override the thresholds only through reviewed private deployment
settings:

The example below uses `/mnt/longhorn` as a dedicated filesystem mountpoint.
Create and mount that filesystem on every node before running the gate; do not
replace it with a directory on the operating-system filesystem.

```bash
PLATFORM_CAPACITY_ROOT_FREE_PERCENT=15 \
PLATFORM_CAPACITY_STORAGE_FREE_PERCENT=20 \
PLATFORM_CAPACITY_STORAGE_PATH=/mnt/longhorn \
PLATFORM_CAPACITY_DEDICATED_STORAGE_REQUIRED=true \
PLATFORM_CAPACITY_MAX_CPU_PERCENT=85 \
PLATFORM_CAPACITY_MAX_MEMORY_PERCENT=85 \
PLATFORM_CAPACITY_MAX_PODS_PERCENT=80 \
PLATFORM_CAPACITY_LONGHORN_FREE_PERCENT=20 \
PLATFORM_STORAGE_ENCRYPTION_REQUIRED=true \
make platform-capacity-verify
```

Set `PLATFORM_CAPACITY_DEDICATED_STORAGE_REQUIRED=false` only for a reviewed
non-production lab. It is not an acceptable setting for production evidence.

The check measures Kubernetes requests rather than instantaneous utilization.
Keep Prometheus saturation and forecast evidence alongside this gate; neither
source is a substitute for the other.

Longhorn bootstrap has the same protection before it creates a disk: strict
private rendering requires `PLATFORM_LONGHORN_DEFAULT_DISK_PATH`, and the
bootstrap and Forgejo storage-repair tasks compare the path's backing device
and filesystem ID with `/`. This prevents an empty installation from silently
placing replicated PVC data on the operating-system disk. Mount the dedicated
filesystem on every node first; do not use a directory on `/` as a substitute.

## Component Planning

Use these component notes as prompts for private sizing decisions:

| Component | Planning notes |
|---|---|
| Argo CD | Size repo-server and controller capacity for repository count, generated manifest size, and sync frequency. Keep Redis and repo-server service paths healthy before increasing reconciliation load. |
| Forgejo, Gitea, or GitLab | Plan for repository growth, LFS policy, SSH clone load, database size, backup duration, and admin recovery. |
| Woodpecker CI | Separate trusted and untrusted runner pools when needed. Track queue latency, agent count, and registry push throughput. |
| Harbor | Define project quotas, retention windows, scan backlog tolerance, robot account ownership, and garbage collection windows. |
| CloudNativePG | Size storage and WAL retention from recovery objectives. Watch replication lag before changing backup or failover behavior. |
| Longhorn or alternate storage | Keep free capacity for replica rebuilds and node maintenance. Test expansion and restore before increasing production PVCs. |
| Velero and object storage | Size object storage from backup frequency, retention, restore drills, and regulatory hold requirements. |
| Prometheus, Grafana, and Loki | Size retention and storage from SLO review, incident review, audit expectations, and query latency. |
| cert-manager, trust-manager, and step-ca | Plan issuer limits, renewal windows, trust bundle propagation, CA backup, and secret rotation. |
| MetalLB, kube-vip, and ingress | Track VIP ownership, ARP or routing convergence, local endpoint coverage, and client-visible latency. |

## Review Cadence

Minimum cadence for private deployments:

- Daily: review `make platform-status`, pending pods, degraded volumes, backup
  age, and critical alerts.
- Weekly: run `make platform-app-health` and review ingress, CI, registry,
  database, storage, backup, and observability trends.
- Monthly: review quotas, retention, storage growth, CI concurrency, registry
  usage, alert noise, and SLO/error budget state.
- Quarterly: run restore drills, node failure tests, and capacity tabletop
  review with operations, storage, database, CI, and security owners.
- Before major releases: compare expected growth with the latest private
  baseline and load-test evidence.

## Production Evidence

Keep private evidence for:

- Current capacity baseline and enabled component list.
- Current threshold or alert settings for saturation signals.
- Latest `make platform-status`.
- Latest `make platform-app-health`.
- Latest `PLATFORM_PROFILE=<PROFILE> make platform-production-check`.
- Latest load or scale test results.
- Latest restore drill and backup duration.
- Latest storage, database, registry, CI, and observability growth review.
- Latest retention review from `docs/DATA_CLASSIFICATION.md`.
- Latest capacity-sensitive maintenance window and rollback evidence.

Do not commit private capacity reports, real utilization graphs, customer
growth forecasts, object storage usage, registry usage, or cost records to this
public template.
