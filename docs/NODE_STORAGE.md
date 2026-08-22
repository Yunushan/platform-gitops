# Node Storage Operations

The RKE2 root filesystem and Longhorn data filesystem are different capacity
domains. A Longhorn PVC can be almost empty from the application's point of
view while its replicas, snapshots, and volume-head files still consume node
storage. Never remove files from `/var/lib/longhorn/replicas`, Longhorn volume
heads, or CSI mount paths by hand.

## Diagnose

Run the read-only report on every node:

```bash
make platform-node-storage-diagnose
```

The report prints root usage, the filesystem backing
`PLATFORM_LONGHORN_DEFAULT_DISK_PATH`, the largest root directories, journal
and container-log usage, and whether the Longhorn default path shares the root
filesystem. It also separates thin-provisioned requested capacity from logical
volume data and estimated replicated data, then identifies the largest PVCs.
Snapshots, metadata, and stale unregistered replica directories are additional
physical usage and are deliberately not mislabeled as live logical data.

When `longhorn_default_path_shares_root=true`, the cluster is storing Longhorn
replicas on the operating-system disk. The durable fix is to attach a separate
SSD or block device, format and mount it consistently on every node, and point
Longhorn at that filesystem. The bootstrap task refuses to treat a directory
on the same filesystem as an extra disk, by design.

## Safe Cleanup

The guarded cleanup target removes unused RKE2 container images, package
manager caches, and system-managed temporary files. On mounted Longhorn XFS or
EXT4 filesystems it also issues a bounded `fstrim`, which releases blocks that
the guest filesystem has already marked unused without deleting files:

```bash
make platform-node-storage-cleanup
```

It never deletes registered Longhorn replicas, PVCs, snapshots, volume heads,
or mounted application files. A separate fail-closed recovery phase can remove
one old replica directory only when repeated cluster-state checks prove that it
is an unregistered duplicate of a fully redundant detached volume. Docker
cleanup and journal deletion require explicit retention choices:

```bash
PLATFORM_NODE_STORAGE_DOCKER_PRUNE=true \
PLATFORM_NODE_STORAGE_DOCKER_PRUNE_UNTIL=168h \
PLATFORM_NODE_STORAGE_JOURNAL_VACUUM_SIZE=2G \
make platform-node-storage-cleanup
```

Docker pruning removes unused images, stopped containers, unused networks, and
old build cache, but does not remove Docker volumes. Journal vacuuming removes
the oldest entries until the requested size remains, so use it only when the
retention policy allows it.

GitLab Runner Docker cache volumes can be reclaimed separately. The cleanup is
restricted to unused volumes carrying both GitLab Runner managed-cache labels,
matching the runner cache naming convention, and older than the retention
window:

```bash
PLATFORM_NODE_STORAGE_GITLAB_RUNNER_CACHE_PRUNE=true \
PLATFORM_NODE_STORAGE_GITLAB_RUNNER_CACHE_PRUNE_UNTIL=168h \
make platform-node-storage-cleanup
```

`make platform-forgejo-repair` and `make platform-woodpecker-repair`
automatically enable this guarded cleanup, wait for pressure to clear, and then
continue the focused repair. Image, cache, package, journal, and temporary-file
cleanup remains limited to nodes where Kubernetes reports
`DiskPressure=True`. Longhorn volumes are different: a replica can consume the
pressured node while its filesystem is attached on another node. When any RKE2
server has pressure, the playbook therefore runs bounded `fstrim` on eligible
mounted Longhorn XFS/EXT4 filesystems across every server. It also runs
`crictl rmi --prune` against every responsive CRI socket on each pressured
node. CRI retains images used by existing containers; an unused image may need
to be pulled again later. Active runner volumes, ordinary Docker volumes,
registered Longhorn replicas, PVC data, and valid snapshots are never selected
for deletion.

The storage diagnose and cleanup targets use a 60-second Ansible host timeout by
default because a node under disk pressure can respond slowly during SSH
privilege escalation. Override it explicitly with `ANSIBLE_TIMEOUT` when the
environment requires a different bound.

Root-backed Longhorn disks reserve 25 percent free space. This keeps Longhorn's
scheduling floor above Kubernetes' usual root-filesystem pressure boundary;
the old 10 percent floor could permit replica growth after kubelet had already
started applying `DiskPressure` eviction controls.

The Longhorn profile schedules a weekly `filesystem-trim` recurring job for
volumes in the default recurring-job group. It explicitly keeps
`removeSnapshotsDuringFilesystemTrim=false`, so trim never marks valid user
snapshots for removal. Longhorn 1.12 orphan cleanup uses the supported
`orphanResourceAutoDeletion` setting with a five-minute grace period; only
resources that Longhorn itself classifies as orphaned are eligible, and
resources on down or unknown nodes remain protected.

During pressure recovery the playbook reconciles those safe settings in the
live cluster before trimming. It also enables cleanup of Longhorn's own
system-generated rebuild snapshots and recurring-backup snapshots, then waits
up to seven minutes for eligible `replica-data` and `instance` orphans on Ready
nodes. Override that bounded wait with
`PLATFORM_NODE_STORAGE_LONGHORN_ORPHAN_WAIT_TIMEOUT`; values below 300 seconds
are rejected because they would be shorter than the orphan grace period.

Kubernetes `DiskPressure` makes the corresponding Longhorn node unavailable.
Longhorn then cannot complete replica eviction or ordinary orphan deletion on
that node. Waiting for native eviction while pressure remains is therefore a
circular recovery path. The cleanup restores any request left by the legacy
pressure-eviction helper and does not start a new Longhorn eviction under
active Kubernetes pressure.

When Longhorn's orphan controller cannot process an old duplicate on that
unavailable node, the cleanup can reclaim one proven-unregistered replica-data
directory. Every one of these gates must pass:

- Kubernetes reports `Ready=True` and `DiskPressure=True`, with memory, PID,
  and network conditions clear. The manager topology must match the active
  Longhorn nodes and a strict majority of managers must be Ready on distinct
  nodes. The local manager may be unready or offline only when the Longhorn
  Node reports `Ready=False` specifically because of
  `KubernetesNodePressure`; the detached volume controller owner must still be
  one of the Ready manager nodes;
- Longhorn's supported `replica-data` orphan policy and at least a five-minute
  grace period are active, while the candidate is at least the configured age
  and allocated-size floor;
- the strict `pvc-UUID-8hex` directory is on a registered Longhorn filesystem
  disk that shares `/`, is absent from every current Replica CR, and has exactly
  one registered sibling replica directory for the same volume on that disk;
- the v1 volume is detached, data-bearing, not migrating, cloning, restoring,
  or deleting, has at least two desired replicas, and its controller owner is
  one of the Ready manager nodes;
- the number of current Replica CRs exactly matches the desired count, all are
  active, healthy-history, registered, and placed on distinct nodes;
- every engine is stopped and unassigned, Longhorn has no attachment ticket,
  Kubernetes has no `VolumeAttachment`, and no Pod references the bound PVC;
  and
- exactly one candidate passes globally on that node during the run.

The helper inventories the cluster twice around a bounded settle period and
requires an identical candidate fingerprint. It checks `/proc` for open files,
atomically renames the directory to a hidden quarantine name, inventories the
entire state a third time, checks open files again, and only then removes the
quarantined directory. A crash leaves the recognizable quarantine name for a
later run to revalidate. At most one directory is reclaimed per node and run.
Registered replicas, PVCs, snapshots, and attached data are never deleted.

The Longhorn manager DaemonSet tolerates Kubernetes' disk-pressure
`NoSchedule` taint. This permits a replacement manager to start during
recovery; it does not bypass Longhorn disk scheduling safeguards or make
application workloads eligible for the pressured node.

This phase is enabled by default. It can be disabled or made more conservative:

```bash
PLATFORM_NODE_STORAGE_LONGHORN_STALE_REPLICA_MIN_AGE=7200 \
PLATFORM_NODE_STORAGE_LONGHORN_STALE_REPLICA_MIN_BYTES=1073741824 \
PLATFORM_NODE_STORAGE_LONGHORN_STALE_REPLICA_SETTLE_SECONDS=30 \
make platform-node-storage-cleanup

PLATFORM_NODE_STORAGE_LONGHORN_STALE_REPLICA_RECLAIM=false \
make platform-node-storage-cleanup
```

## Root-backed installation capacity

An application cluster with no user repositories or pipelines is not an empty
storage cluster. PostgreSQL, Harbor, monitoring, Loki, MinIO, Forgejo,
Woodpecker, and the other platform services initialize PVCs, and Longhorn keeps
the configured replica copies and snapshots for those volumes. Most allocated
blocks under `/var/lib/longhorn/replicas` are therefore real platform data even
when application-level user data is still zero. A directory with no matching
Replica CR can also be stale physical data; only the guarded proof above may
classify and reclaim that special case automatically.

The bootstrap uses `/home/longhorn` automatically only when `/home` is a
different filesystem with enough free capacity. It deliberately rejects that
path when `/home` and `/var/lib/longhorn` resolve to the same filesystem. On a
host with one 200 GiB disk entirely allocated to the root logical volume, the
installer cannot manufacture independent Longhorn capacity; the safe choices
are to add a dedicated disk to each node or provision the operating-system
layout with a separate storage filesystem before production data is created.
Longhorn can rebalance replicas after pressure clears and alternate disks have
sufficient scheduler and physical headroom. The guarded cleanup never deletes
referenced PVC data to make a single disk appear empty.

If pressure remains after the bounded wait, the playbook prints the kubelet
condition, taints, filesystems, largest `/var/lib` consumers, runtime service
state, Longhorn volume allocation and orphan inventory, and deleted files still
held open before failing. This is intentionally not converted into automatic
PVC, snapshot, referenced Longhorn replica, or arbitrary volume deletion.

The focused Longhorn runtime repair also validates the v1 instance-manager data
plane on every Ready Longhorn node. If an attaching data-bearing RWO volume has
exactly one healthy registered replica and one stopped healthy-history replica
whose local disk registration was removed, the repair can recover the stale
path without deleting data. It requires one unambiguous Ready/Schedulable local
filesystem disk, makes a same-filesystem reflink copy, compares the complete
directory manifest, performs a server-side dry run, and then updates only the
replica's disk metadata. The original directory remains in place as a recovery
copy and should be removed only after backup and replica health are independently
verified.

## What This Does Not Fix

If the root filesystem is full because referenced Longhorn replica data is on
`/var/lib/longhorn`, cache pruning, cluster-wide filesystem trim, orphan
cleanup, and duplicate reclamation can recover only blocks that are genuinely
unused. Do not delete replica files to force space recovery. When no single
directory passes the strict duplicate proof, add a dedicated disk, add it as a
Longhorn disk, clear pressure with additional capacity, and let Longhorn migrate
or rebuild replicas. Review unused PVCs, old snapshots, backup retention, and
registry/MinIO retention in their application APIs before approving any data
deletion.
