# Backup and Restore

## Required backups

| Area | Method |
|---|---|
| Kubernetes objects | Velero |
| PostgreSQL | CloudNativePG backup and WAL archive |
| Volumes | Longhorn or Rook/Ceph backup process |
| Object storage | Replication plus off-cluster copy |
| Git repositories | Forgejo/Gitea/GitLab backup process |
| Registry | Harbor backup process plus object storage backup |

## Off-cluster requirement

A backup stored only inside the same 3-node cluster is not disaster recovery. Always keep an off-cluster copy.

## Restore drill

Run a restore drill before production use and repeat regularly.
