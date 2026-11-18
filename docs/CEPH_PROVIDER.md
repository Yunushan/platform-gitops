# Optional fresh Ceph provider creation

The [Rook/CSI bootstrap helper](ROOK_BOOTSTRAP.md) can install a fresh pinned
operator and drivers first. Its private `provider_operator` handoff supplies
the original operator/API UIDs and immutable operator/CSI ConfigMap identities
and data hashes required here. Configuration snapshots are revalidated before
device inspection or cluster creation. This permits exact bootstrap ConfigMaps
in a shared namespace without permitting unrelated old storage state. The fresh
check accepts Rook's generated `rook-ceph-csi-config` ConfigMap in that
namespace when the original operator Deployment owns it and its cluster list
is empty.

`render_ceph_provider.py` and `deploy_ceph_provider.py` prepare and explicitly
create a fresh Rook `CephCluster` after read-only inspection of every selected
OSD device. They target an already prepared Kubernetes/RKE2/K3s management
cluster with a compatible, pinned Rook operator. Docker/native platform
consumers can select that external provider through the same explicit target.
Rook operator/CSI installation uses the separate bootstrap helper. The separate
[native CephFS mount helper](CEPH_NATIVE_MOUNT.md) prepares explicit client
mounts for native and Docker consumers; real provider/application integration
and recovery still require acceptance.

The helper creates an immutable probe program/configuration, one original probe
Job per host, and one original CephCluster. It initializes only explicitly
listed devices that all passed the fresh-device checks. It never adopts an
existing Ceph cluster, wipes a nonempty disk, upgrades a provider, deletes data
or replaces an uncertain create request. Readiness is scoped to the original
provider resources and daemon placement; storage I/O, node-loss, restore and
complete-stack acceptance remain unverified.

## Prepare private inputs and hosts

Copy `examples/ceph-provider.json` into ignored `private/`. Replace every
placeholder and example hardware value with independently reviewed observations:

- A new independent cluster UUID, short provider name/namespace, namespace UID,
  explicit management context and kube-system namespace UID. That UUID also
  identifies this original operation and its exclusive state directory under
  `/var/lib/rook/`. Preserve the same UUID and private journal during resume.
- The preinstalled Rook operator deployment, namespace and CRD UIDs, and its
  digest-pinned image. The served/storage v1 CephCluster schema must match the
  pinned upstream Rook v1.21.2 schema. If the operator is in another namespace,
  its deployment must explicitly set `ROOK_CURRENT_NAMESPACE_ONLY=false` as a
  literal environment value. Configure appropriate namespace access/RBAC through
  reviewed operator provisioning; this helper does not broaden its privileges.
- An exact Ceph 20.2 patch/image digest and a reviewed Python 3.12-or-newer probe
  image digest. Prepare image-pull access on the intended nodes. The finite
  probe policy rejects injected pod containers, mounts and unapproved service
  credentials. Default-service-account mutation is not an image-pull workaround.
- Four to thirty-two independent, Ready, schedulable Linux storage hosts,
  without NoSchedule/NoExecute taints. Bind Node UIDs, hostname/failure-domain
  labels, boot UUIDs and SHA-256 of each stripped `/etc/machine-id`. Node-reported
  identity must match the device probe's host reads. The current encrypted
  messenger/kernel-client policy requires Linux 5.11 or newer.
- One to eight explicitly assigned whole devices per host, using stable
  `/dev/disk/by-id/` paths. Supply their actual major/minor numbers, exact byte
  sizes and hardware-identity digests. Partition suffixes, device aliases and
  duplicate identities across hosts are refused. Supported device sizes are
  64 GiB through 128 TiB; plan these from actual workload/recovery needs.
- CPU/memory budgets for monitor, manager and OSD services. OSD requests must
  reserve at least 4 GiB per device; account for every OSD, control process and
  other workload on each host. Node readiness is not a free-resource reservation.

The hardware-identity digest is SHA-256 of sorted compact JSON, UTF-8 without a
trailing newline: `{"serial":"...","wwid":"..."}`. Obtain actual attributes
from the reviewed physical whole-device sysfs directory. For each field, use
the first nonempty value from `wwid` then `device/wwid`, or `serial` then
`device/serial`, stripping surrounding whitespace. Empty fields are encoded as
empty strings; at least one must exist. Keep raw hardware inventory private.
`ceph_device_probe.fingerprint` implements the canonical JSON hash.

Select only storage exclusively assigned to this installation. Quiesce other
provisioning/host administration for the entire probe-to-initialization window.
The helper's software/device observations are not fencing against another
privileged writer, and a hardware identifier is not cryptographic attestation.

## Root device-probe admission

The provider namespace must explicitly allow privileged workloads. Probe Jobs
request privileged root execution because a plain hostPath bind does not grant
the device-cgroup access required by standard CRI runtimes. The rendered
security context makes this privilege explicit; there is no automatic privilege
fallback. Review the concrete manifests and trusted image/runtime before use.

The fixed Python program has no shell commands, source API calls, formatting or
device writes. It opens each reviewed device read-only, with Linux `O_EXCL`,
`O_NOFOLLOW` and close-on-exec. The container root filesystem and declared host
bind mounts are read-only. Its host inputs are the selected block devices,
sysfs, PID-1 mount inventory, machine/boot identity and Rook state parent.
Privileged mode can bypass container restrictions, so the read-only behavior
depends on the reviewed program, image, admission and runtime trust.

The kubelet may create the fixed `/var/lib/rook` parent through the declared
DirectoryOrCreate volume. The probe itself never creates the per-cluster state
directory; it requires that UUID directory to be absent, even if an existing
directory would be empty. Service-account token mounting and service-link
injection are disabled. Each Job is tied to its selected node, uses a unique
manual selector, has zero retries, and has a bounded active deadline.

Each probe rejects virtual device stacks, partitions, lower devices, holders,
host mounts, changed hardware identity, changed major/minor, wrong size and
read-only devices that cannot become new OSDs. It scans every accessible byte
and requires all zeroes; checking a few signatures is insufficient. Any
nonzero byte causes refusal. Previously used disks and factory partition tables
need a separately approved preparation/recovery process. No erase operation is
provided here.

Large full scans add storage load and can take substantial time. The Python
deadline bounds normal reads; Kubernetes also limits Job duration. A kernel
read stuck in uninterruptible I/O is not an established shutdown/fencing event.
The installer never initializes devices while a required probe is incomplete.
The probe prints only scoped identities/counts/digests; raw device contents,
serial numbers and private input values are not printed on errors.

## Render and approve concrete admission

From the repository root:

```bash
python3 scripts/render_ceph_provider.py --runtime rke2 \
  --plan private/ceph-provider.json --output-dir private/ceph-provider-package
```

Rendering emits a private policy package and probe preview. It contacts no
server or device. Local validation is also the deployment helper's default:

```bash
python3 scripts/deploy_ceph_provider.py --runtime rke2 \
  --plan private/ceph-provider.json
```

Inspect the explicit target/prerequisites and perform server create dry-runs:

```bash
python3 scripts/deploy_ceph_provider.py --runtime rke2 \
  --plan private/ceph-provider.json --check-server \
  --output-dir private/ceph-provider-check-001
```

This writes concrete admitted resource bodies into the new private directory
and returns an aggregate `admitted_sha256`. Review the root probes, images,
device list, namespace scope and Ceph initialization policy, then pin that hash
in the private plan. Checks refuse weakened expected fields, extra probe pod
policy and unreviewed Ceph spec fields; only exact defaults from the pinned CRD
schema may supplement the selected cluster policy. Server admission itself is
part of the trusted control plane; a malicious or nondeterministic admission
chain is not neutralized by a client-side read-back check.

## Create once, then observe original handles

```bash
python3 scripts/deploy_ceph_provider.py --runtime rke2 \
  --plan private/ceph-provider.json --create \
  --output-dir private/ceph-provider-create-001 --wait-seconds 7200
```

The helper freezes static management credentials, verifies identities and
refuses existing Ceph resources/generated identities. It creates the original
immutable program and per-host probe Jobs with durable intent records. Before
cluster creation, every Job must have exactly one original successful pod,
matching scope and executable ConfigMap digests, no restart, unchanged
pod/container observation and fresh proof. The executable digest covers the
packaged Python program and its embedded dependency. Probe timestamps must be
within five minutes, with at most 30 seconds of forward clock skew. Synchronize
the host clocks before provisioning.
It rechecks node/boot/operator identities and implementation source pins before
the single cluster-create request. Device observations are consumed by that
original cluster intent and never converted into a general approval to reuse
disks later.

The CephCluster has three monitors, two managers, required domain placement,
explicit hosts/devices, encrypted OSD configuration and encrypted messenger-v2
connections. Device-wide discovery, foreign-cluster wiping, unsafe upgrade
checks, automatic OSD removal and uninstall-with-volumes are disabled. The
state path is unique to this cluster UUID. Preserve generated encryption keys
and monitor credentials through independent recovery arrangements. The separate
optional [host-OSD key recovery helper](CEPH_KEY_RECOVERY.md) prepares encrypted
key archives and create-only OpenBao escrow. It does not replace monitor-store
backups or establish real Ceph restoration; its runtime acceptance remains
separate from provider creation.

`provider_resource_ready` requires the original cluster UID/spec, current Ready
and HEALTH_OK status, one stable FSID, exact reported daemon version/counts,
current owned deployments/pods and the expected OSD-per-node distribution.
Monitors and managers must span distinct reviewed domains. The private
`ceph-provider-proof.json` binds operation/source/plan identity, each create
intent/UID, original probe pod/result digests, FSID and phases.

`devices_observed` and `provider_resource_ready` are scoped observations.
`provider_installed`, `data_roundtrip_verified`, `node_loss_verified`,
`recovery_verified`, `ha_verified` and `full_stack_verified` remain false.
The remaining provider acceptance includes live Ceph CLI/device association,
CSI access, actual RBD/CephFS I/O, network paths, capacity/recovery tests,
key recovery and full application integration. Successful synthetic-controller
tests do not establish those properties.

Resume the same private journal after a timeout or lost acknowledgment:

```bash
python3 scripts/deploy_ceph_provider.py --runtime rke2 \
  --plan private/ceph-provider.json --resume \
  --output-dir private/ceph-provider-create-001 --wait-seconds 7200
```

Resume observes matching original resource UIDs and issues only requests never
attempted before. A missing uncertain create is never repeated. A replaced,
foreign, modified or deleted resource cannot be adopted. Completed probe
results remain bound to the original cluster attempt; they are not rerun on
initialized disks. Failed, expired, replaced or incomplete probes block a new
cluster intent. Preserve the evidence for operator review; there is no automatic
cleanup, disk wipe, replacement Job or failed-initialization rollback.

After separate provider acceptance and preparation of its trusted toolbox/CSI
services, use [storage provisioning](CEPH_STORAGE.md) for new replicated pools,
CephFS and Retain classes. That plan explicitly identifies CSI namespace and
driver names, which can differ from the new provider namespace. Its version 2
contract accepts the same complete bootstrap operator object under
`provider.operator`. Keep actual OSD hosts in `nodes`; review the bootstrap
`storage_access_nodes` and `storage_csi` handoffs for its separate client
inventory and CSI handles. Additional client workers do not add OSD capacity.

## Validation

Default tests exercise finite policy, all five consumer runtime selections,
device/scope refusal, source program bindings, unapproved host-mount/device
injection, expired probes and original-handle recovery using a simulated
Kubernetes/Rook controller. They initialize no Ceph disk.

An isolated four-node K3s run exercised the installer against the live Rook
and CSI bootstrap. Real privileged Jobs checked the node and device
identities and read all four disposable 64 GiB block devices. An independent
calculation matched the zero-stream checksum from each original Job. The
installer accepted the four receipts and reached the CephCluster create
boundary, where the lab intercepted the request before submission. In a
second operation, a nonzero marker on one disposable disk caused refusal
before the installer reached that boundary. The lab removed the guests and
their disks. Physical-device association, Ceph initialization, client I/O,
and storage failover remain separate acceptance steps.

On a disposable root Linux host with Python, PyYAML 6.0.3, `jsonschema`, loop
support and `losetup`:

```bash
CEPH_STORAGE_TEST_SCHEMA=1 CEPH_DEVICE_TEST_LOOP=1 \
  python3 -B -m unittest discover -s scripts -p 'test_ceph*.py'
```

The optional fixture creates an 8 MiB backing file only beneath its unique
`/run/ceph-probe-fixture-*` directory, maps a new loop device and exercises real
block-size/read-only ioctls, a complete zero scan, nonzero rejection and an
exclusive-open collision. It verifies the original backing-file association
before detaching that device. It does not read a physical company disk, run a
privileged Kubernetes pod, simulate WWID hardware attestation or initialize a
live Rook provider. Schema validation uses the pinned upstream cluster schema
and does not execute Kubernetes CEL or actual admission.

Primary references: [Rook explicit host/device selection](https://rook.io/docs/rook/v1.21/CRDs/Cluster/host-cluster/),
[CephCluster policy](https://rook.io/docs/rook/v1.21/CRDs/Cluster/ceph-cluster-crd/),
[Kubernetes hostPath/device types](https://kubernetes.io/docs/concepts/storage/volumes/#hostpath)
and [Linux open/exclusive semantics](https://man7.org/linux/man-pages/man2/open.2.html).
