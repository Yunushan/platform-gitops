# Optional replicated Ceph storage provisioning

After provisioned resource readiness, use the separate
[client data-path check](CEPH_CLIENT_CHECK.md) to exercise a fresh RBD handoff
between two nodes and concurrent CephFS exchange. Storage resource readiness
alone does not prove client I/O or node-loss recovery.

`scripts/render_ceph_storage.py` prepares a three-copy RBD pool, a replicated
CephFS filesystem and two expandable Retain storage classes.
`scripts/provision_ceph_storage.py` can explicitly create these resources on an
identified, prepared Rook provider. This supplies the RBD database path and RWX
filesystem path needed by the multi-runtime foundation.

RKE2, K3s and existing Kubernetes consumers use an explicitly selected provider
cluster. Docker/native consumers can prepare the same external provider through
an explicit Kubernetes management target; native Ceph client installation,
restricted CephX identities, mounts, fencing and application binding remain
integration work. This helper does not install a Rook operator, Ceph cluster,
CSI stack or raw-disk OSDs. The operator-only storage profile still needs that
provider installation path before it can become a complete storage deployment.
The separate [fresh-provider helper](CEPH_PROVIDER.md) now guards CephCluster
creation with explicit whole-device probes and original resource handles.
The [Rook bootstrap](ROOK_BOOTSTRAP.md) supplies the guarded operator/CSI
installation path; live provider acceptance remains required.

## Review the prepared provider

Copy `examples/ceph-storage.json` into ignored `private/`. Replace every
placeholder using independently reviewed provider observations. The storage
plan uses `schema_version: 2`; provider, bootstrap and receipt formats retain
their own version numbers. Version 1 storage plans are refused because they
lack explicit operator/API/configuration identities and the access inventory.
Prepare and review a new plan before any new operation. Do not rewrite an old
operation's journal, proof, source hash or original resource handles to resume
under a different contract.

The required observations are:

- An explicit kubeconfig environment-variable reference, context and kube-system
  namespace UID. Server operations freeze self-contained static credentials,
  require verified HTTPS and check that cluster identity. Tokens, credential
  plugins, certificates and private kubeconfig contents belong outside public Git.
- Rook namespace/cluster UIDs, Ceph FSID, exact Ceph 20.2 patch, digest-pinned
  Ceph image and SHA-256 of the complete canonical live CephCluster specification.
  The cluster must have a current observed generation and Ready status. The
  storage CRD UIDs and served/storage v1 schemas must match the pinned Rook
  v1.21.2 schemas. Review artifact provenance and actual operator compatibility;
  schema compatibility alone does not establish a supported deployment.
- The complete bootstrap `provider_operator` object in `provider.operator`:
  namespace/UID, live deployment UID/image, cluster CRD UID and the two immutable
  configuration references with original UIDs and canonical data hashes. The
  operator can run in a separate namespace. Its discovery, hotplug, loop-device
  and cleanup controls are rechecked; a separate namespace requires explicit
  cluster-wide watch scope in both configuration and the operator environment.
  The live cluster API schema must match the pinned Rook schema.
- Original toolbox deployment/pod UIDs,
  toolbox container name and the same pinned Ceph image. The toolbox must have
  its trusted `/etc/ceph/ceph.conf` and private keyring provisioned separately.
  The helper reads Ceph state through fixed commands, without fetching Secret
  contents or printing a keyring. It checks the toolbox container handle around
  each read. Privileged operator/toolbox access requires restricted RBAC and
  trust in the provider's administrators and runtime.
- Four to thirty-two storage hosts, their Node UIDs, hostname labels, unique
  physical failure-domain labels and exact CRUSH host names. Four domains permit
  placing three copies after one host is unavailable. Three hosts alone cannot
  rebuild the third host-separated copy until replacement capacity arrives.
  A failure domain can use the zone or Rook rack label; labels must reflect
  actual independent hardware. Declared labels do not prove physical isolation.
- An `access_nodes` inventory of four to 128 approved Linux nodes for CSI
  controllers and clients, each with name, UID and failure domain. It must
  contain every OSD host from `nodes` with the exact same identity/domain.
  Additional workers may share physical domains. CSI controllers must have
  at least two ready replicas in distinct domains; every replica must be on
  an approved access node. Both node-plugin DaemonSets must cover precisely
  this complete inventory. Readiness, scheduling and node identities are
  rechecked live. OSD capacity, CRUSH accounting and metadata-server placement
  continue to use only `nodes`, never this larger client inventory.
- Both live CSI driver UIDs, controller deployment and node-plugin DaemonSet
  names/UIDs, their namespace/UID, explicit CSI driver names, and every
  digest-pinned container/init-container image. The CSI/operator namespace and
  driver prefix can differ from the provider namespace; storage-class
  `clusterID` and credential namespaces still identify the actual Ceph provider.
  Copy the bootstrap receipt's `storage_csi` object and review the original
  UIDs and complete image inventories, including sidecars. Its
  `storage_access_nodes` list can seed `access_nodes` if it still matches the
  intended client topology. Each current controller must have at least
  two ready replicas across reviewed failure domains. Node plugins must be
  current and ready on all approved access nodes. RBD requires the
  driver's attachment policy. CSI availability does not prove a volume mount.

The provider must have healthy current monitor quorum with three monitor
processes in distinct reviewed domains, two managers in distinct domains and
all OSDs up/in at the reviewed patch. Every active OSD must belong to one of
the declared CRUSH hosts. This initial policy supports the `default` CRUSH root
with those hosts as direct children. Class-filtered, hierarchical and additional
roots require their own reviewed adapter. Kernel RBD and CephFS clients and
appropriate network reachability are prerequisites for subsequent mounts.

The canonical specification hash uses sorted compact JSON, UTF-8, without a
trailing newline. `render_ceph_storage.fingerprint(spec)` implements that rule.
Retain the independently reviewed live specification and hashes privately.
No automatic discovery command should overwrite those approval inputs.

## Quotas and recovery headroom

Set finite byte quotas for the RBD pool, CephFS data pool and CephFS metadata
pool. Size these from measured workload needs, including metadata growth.
Set the `mds_resources` CPU/memory requests and limits for each active/standby
metadata process from workload sizing and available node resources. Examples
are starting values; large directory trees and client caches need deliberate
metadata-service sizing.
Reserve 20–50 percent of raw capacity for recovery. These are pool quotas,
not a promise that arbitrarily many PVC requests fit the available storage.

The preflight reads live per-OSD size/used/available data and accounts for the
complete active cluster. It uses the greater of reported used space and
`size - available`. Every OSD must already satisfy the reserve. Let `Q` be the
sum of the three logical quotas, `T` total raw capacity, `U` current raw used,
`L` the largest host's raw capacity and `R` the reserve fraction. It requires:

```text
U + 3*Q <= (T - L) * (1 - R)
```

It also requires, for every host, enough remaining capacity below the reserve
ceiling for `Q` plus the largest host's existing used bytes. This deliberately
conservative per-host test refuses imbalanced providers that could pass an
aggregate free-space check. The observations are not reservations against
concurrent tenants and cannot predict all CRUSH/PG behavior. Serialize allocation
through an exclusive provider workflow, monitor capacity continuously and test
actual recovery before HA acceptance.

### Observe a one-host replica rebuild

On a disposable provider, choose a reviewed storage node whose OSDs
hold replicas for each of the three platform pools. Keep the management API and the
original toolbox reachable from a surviving node. Run the observer before
the outage using the original storage plan and completed provisioning
receipt. Review the receipt's SHA-256 before supplying it.

```bash
python3 scripts/verify_ceph_host_recovery.py \
  --runtime rke2 \
  --storage-plan private/ceph-storage.json \
  --storage-proof private/ceph-storage-create/ceph-storage-proof.json \
  --storage-proof-sha256 "$REVIEWED_STORAGE_RECEIPT_SHA256" \
  --host "$REVIEWED_STORAGE_NODE" \
  --phase baseline \
  --output-dir private/ceph-host-drill
```

Keep the same arguments and output directory for the next two observations.

- With the selected host still offline, run `--phase rebuilt` when its
  OSDs are down and out and the pools have finished rebuilding. Every
  RBD, CephFS data and CephFS metadata PG must be active and clean, with
  three acting replicas on three surviving hosts. At least two of the
  original monitors must be in quorum.
- After returning the host and waiting for its original OSDs to come up
  and back in, run `--phase returned`. This requires all original
  storage nodes Ready, all three monitors in quorum, and clean PGs in a
  newer OSD map.

By default, the observer reads Ceph and Kubernetes state. An observation
rejected during recovery can be repeated with the same inputs. The receipt
binds the three accepted observations to the original OSD UUIDs, CRUSH hosts,
filesystem, pools and PG inventory.
[Ceph OSD and placement group states](https://docs.ceph.com/en/tentacle/rados/operations/monitoring-osd-pg/)

#### Include reads from the original client volumes

Complete the [CSI client check](CEPH_CLIENT_CHECK.md) while all hosts are
healthy. Keep its two PVCs and bound PVs. Pick a storage host in a different
failure domain from the original RBD reader node. Review the completed
client receipt and its SHA-256, then add these arguments to the observer
command for **every phase**, starting with a fresh baseline:

```bash
--client-plan private/ceph-client-check.json \
--client-proof private/ceph-client-create/ceph-client-proof.json \
--client-proof-sha256 "$REVIEWED_CLIENT_RECEIPT_SHA256"
```

Each phase creates one immutable ConfigMap and two finite, non-root Jobs in
the original restricted canary namespace. The Jobs mount the original claims
read-only on the original reader node. They compare the RBD payload and both
CephFS payloads to their expected bytes. The verifier rechecks the PVC/PV
bindings and requires matching Ceph phase observations before and after the
Jobs, including the OSD map epoch. Preserve the ConfigMap and Jobs for
inspection, including any left by an interrupted attempt. If a previous Job
still uses a claim, wait for it to finish before repeating the phase.
[Kubernetes Job lifecycle](https://kubernetes.io/docs/concepts/workloads/controllers/job/)

The resulting receipt records point-in-time canary reads before the outage,
after the replica rebuild while the host is still offline, and after its
return. Workload writes, reads during the rebuild itself, application
failover and backup restoration require separate tests.

## Render and inspect server admission

All commands run from the repository root. First render the finite resources:

```bash
python3 scripts/render_ceph_storage.py --runtime rke2 \
  --plan private/ceph-storage.json --output-dir private/ceph-storage-package
```

The private output must be new. It includes readable resources, a plan/resource
contract and result flags. It creates no server objects. The provisioning
helper's default mode validates local inputs and pinned schemas only:

```bash
python3 scripts/provision_ceph_storage.py --runtime rke2 \
  --plan private/ceph-storage.json
```

Explicitly inspect the provider and perform create-only server dry-runs:

```bash
python3 scripts/provision_ceph_storage.py --runtime rke2 \
  --plan private/ceph-storage.json --check-server \
  --output-dir private/ceph-storage-check-001
```

It refuses existing CRs/storage-class names and existing Ceph pool/filesystem
names. It writes each full server-admitted resource body, excluding metadata
and status, into the private check directory. Review these concrete admitted
policies and the returned aggregate `admitted_sha256`. Put that hash in the
private plan before requesting creation. Mutations that weaken the requested
replication, quotas, placement or retention fail even before the pin comparison.
Server admission can invoke provider webhooks and still needs appropriate
provider access; local validation alone makes no API call.

## Create and observe the new storage

```bash
python3 scripts/provision_ceph_storage.py --runtime rke2 \
  --plan private/ceph-storage.json --create \
  --output-dir private/ceph-storage-create-001 --wait-seconds 600
```

The helper rechecks provider identity, access services, live headroom, admission
and unused data names before creation. It creates only the RBD pool, CephFS
filesystem and two storage classes. No arbitrary manifests, pool updates,
provider adoption, disk preparation, deletions or automatic rollback are used.
The private journal is locked, and each resource's create intent is synced
before sending the request. Each returned resource UID is retained.

Both pool types use three host-separated replicas, safe-replica enforcement,
minimum size two, autoscaling and explicit quotas. CephFS uses replicated
metadata and data pools, one active MDS plus a warm standby, resource budgets
and required placement across distinct domains. Its filesystem/pools are marked
for preservation on CR deletion. Storage classes use Retain,
WaitForFirstConsumer and expansion. Retain protects provisioned volume
reclamation; it is not a backup and does not make deletion of a CephBlockPool CR
safe. Keep deletion privileges separate from routine allocation.

New classes also bind encrypted client transport to the reviewed provider
FSID: RBD uses `mapOptions=krbd:ms_mode=secure,fsid=...` with fallback disabled,
and CephFS uses `kernelMountOptions=ms_mode=secure,fsid=...`. RBD filesystem
mount options are kept separate from kernel device-map options. All reviewed
access nodes must report Linux kernel 5.11 or newer before allocation. Review
the selected CSI/client images and kernels; declared options do not establish
live encrypted traffic. Existing immutable class parameters are not patched
in place; retain prior source/journals and use a separately reviewed new
allocation or migration plan.

These fields follow the primary [CSI RBD example](https://github.com/ceph/ceph-csi/blob/v3.15.0/examples/rbd/storageclass.yaml),
[CSI CephFS example](https://github.com/ceph/ceph-csi/blob/v3.15.0/examples/cephfs/storageclass.yaml)
and [kernel RBD options](https://docs.ceph.com/en/tentacle/man/8/rbd/#kernel-rbd-krbd-options).

Completion requires the original CRs to report Ready, runtime pool quotas and
three-copy/min-size-two policy to match, and each pool's CRUSH rule to take the
reviewed default root and choose distinct hosts. It checks the exact filesystem
pool IDs, one active MDS and one standby, and current ready metadata processes
in distinct domains. Provider prerequisites and source pins are rechecked
before completion. The journal reports `storage_resources_provisioned`, with
capacity observations and original resource identities.

This flag does not establish client mounts, data integrity, node-loss handling,
HA or complete-stack support. `provider_installed`, `client_mounts_verified`,
`data_roundtrip_verified`, `node_loss_verified`, `recovery_verified`,
`ha_verified` and `full_stack_verified` remain false.

## Interrupted operation

Preserve the private plan, helper revision, admitted pin and journal. Resume
observation and any resource requests that were never attempted with:

```bash
python3 scripts/provision_ceph_storage.py --runtime rke2 \
  --plan private/ceph-storage.json --resume \
  --output-dir private/ceph-storage-create-001 --wait-seconds 600
```

A lost acknowledgment can be resolved only by finding the object bearing that
original operation/plan identity and matching admitted policy. An uncertain
create that remains absent is refused rather than repeated. A deleted,
replaced, foreign or modified object is never adopted or recreated. Pending
unattempted pool/filesystem names are also checked against live Ceph data on
resume. A temporarily unready provider can stop a partial operation; inspect
its state and resume the same handles once prerequisites hold. An existing
output directory cannot start a fresh operation. No automatic cleanup removes
private evidence or data.

## Validation and remaining integration

The default test suite exercises all five consumer runtime selections, fixed
resource scope, capacity refusal, API/daemon identity, redundant placement,
weakened admission, backend collisions, proof failures and lost acknowledgments
using a simulated provider. It is not a live Ceph acceptance fixture.

With `jsonschema` installed, validate against the exact upstream API schemas:

```bash
CEPH_STORAGE_TEST_SCHEMA=1 python3 -B scripts/test_ceph_storage.py
```

The extracted public schemas and upstream license are in
`schemas/rook/v1.21.2/`. Their documented source and hashes are enforced locally.
JSON-schema checks do not replace live admission, Kubernetes CEL, controller
behavior or an actual RBD/CephFS I/O test.

The separate provider helper supplies guarded creation and exclusive disk
inspection; those paths still require live provider acceptance. Remaining work
includes application/native-client credential and mount integration, key
recovery, restricted network policies, storage metrics/alerts, volume snapshots,
off-platform backup/restore, upgrades and failure/load/RPO/RTO drills.

Primary references: [Rook replicated pools](https://rook.io/docs/rook/v1.21/CRDs/Block-Storage/ceph-block-pool-crd/),
[Rook CephFS](https://rook.io/docs/rook/v1.21/CRDs/Shared-Filesystem/ceph-filesystem-crd/)
and [Ceph OSD/placement-group monitoring](https://docs.ceph.com/en/tentacle/rados/operations/monitoring-osd-pg/).
