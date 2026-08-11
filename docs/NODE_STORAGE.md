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
manager caches, and system-managed temporary files:

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

## What This Does Not Fix

If the root filesystem is full because Longhorn replica data is on
`/var/lib/longhorn`, cache pruning will recover only cache space. Do not delete
replica files to force space recovery. Instead, add a dedicated disk, add it as
a Longhorn disk, then migrate or rebuild replicas through Longhorn. Review
unused PVCs, old snapshots, backup retention, and registry/MinIO retention in
their application APIs before approving any data deletion.
