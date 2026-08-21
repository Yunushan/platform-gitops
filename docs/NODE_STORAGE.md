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
filesystem.

When `longhorn_default_path_shares_root=true`, the cluster is storing Longhorn
replicas on the operating-system disk. The durable fix is to attach a separate
SSD or block device, format and mount it consistently on every node, and point
Longhorn at that filesystem. The bootstrap task refuses to treat a directory
on the same filesystem as an extra disk, by design.

## Safe Cleanup

The guarded cleanup target removes only unused RKE2 container images, package
manager caches, and system-managed temporary files. On mounted Longhorn XFS or
EXT4 filesystems it also issues a bounded `fstrim`, which releases blocks that
the guest filesystem has already marked unused without deleting files:

```bash
make platform-node-storage-cleanup
```

It never deletes Longhorn data, PVCs, snapshots, volume heads, or mounted
application files. Docker cleanup and journal deletion require explicit
retention choices:

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
automatically enable this guarded cache cleanup only on nodes where Kubernetes
reports `DiskPressure=True`, wait for pressure to clear, and then continue the
focused repair. Under pressure they also run `crictl rmi --prune` against every
responsive RKE2 or standalone containerd CRI socket. CRI retains images used by
existing containers; an unused image may need to be pulled again later. Active
runner volumes, ordinary Docker volumes, Longhorn data, and PVC data are never
selected.

The Longhorn profile schedules a weekly `filesystem-trim` recurring job for
volumes in the default recurring-job group. It explicitly keeps
`removeSnapshotsDuringFilesystemTrim=false`, so trim never marks valid user
snapshots for removal. Longhorn 1.12 orphan cleanup uses the supported
`orphanResourceAutoDeletion` setting with a five-minute grace period; only
resources that Longhorn itself classifies as orphaned are eligible, and
resources on down or unknown nodes remain protected.

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

If the root filesystem is full because Longhorn replica data is on
`/var/lib/longhorn`, cache pruning will recover only cache space. Do not delete
replica files to force space recovery. Instead, add a dedicated disk, add it as
a Longhorn disk, then migrate or rebuild replicas through Longhorn. Review
unused PVCs, old snapshots, backup retention, and registry/MinIO retention in
their application APIs before approving any data deletion.
