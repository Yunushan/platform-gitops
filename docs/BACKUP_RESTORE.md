# Backup and Restore

Backups are not production evidence until a restore drill proves that the
platform can recover useful service, data, and GitOps control.

## Required Backups

| Area | Required protection |
|---|---|
| RKE2 control plane | Etcd snapshots copied off the cluster |
| Kubernetes objects | Velero with an available BackupStorageLocation |
| PostgreSQL | CloudNativePG backup plus WAL archive |
| Volumes | Longhorn backup target or the selected storage backend backup |
| Object storage | Provider replication plus off-cluster copy |
| Git repositories | Forgejo, Gitea, or GitLab repository backup/export |
| Registry | Harbor registry object storage backup plus metadata database backup |
| Secrets | SOPS, External Secrets, Sealed Secrets, Vault/OpenBao, or equivalent private secret recovery |
| PKI | cert-manager issuer state, trust-manager bundles, and any step-ca persistent data or CA material |

## Off-Cluster Requirement

A backup stored only inside the same 3-node cluster is not disaster recovery.
Use `docs/BUSINESS_CONTINUITY.md` to connect restore proof to the minimum
viable platform, dependency recovery order, failover/failback expectations, and
continuity exercise cadence.
Keep a copy in a separate failure domain. For small private deployments this
usually means a separate object-storage account, another data center, or a
controlled offline copy.

Keep the following outside the cluster and outside plaintext Git:

- Backup storage credentials.
- SOPS age private key material.
- Object storage recovery credentials.
- CA private keys and step-ca passwords when step-ca is enabled.
- RKE2 etcd snapshot decryption material if snapshot encryption is enabled.

## RPO/RTO Targets

Set explicit recovery targets before production use:

| Target | Minimum evidence |
|---|---|
| RPO target | Latest successful backup timestamp for every required area is inside the accepted data-loss window. |
| RTO target | Timed restore drill finishes inside the accepted service recovery window. |
| Integrity target | Restored Git, database, registry, and PVC data pass checksum or application-level verification. |

Record these targets in the private deployment repository, ticket, or runbook
system. Do not commit customer data, passwords, internal IPs, or private
hostnames into this public template repository.

## Evidence Before Production

Before calling a cluster production-ready, collect a production acceptance
record with:

- `make platform-production-check` output from the target cluster.
- `make platform-app-health` output showing Velero, CloudNativePG, Longhorn,
  Harbor, Loki, monitoring, generated app secrets, Argo CD runtime, and GUI
  routes are ready for the selected profile.
- Etcd snapshot list and the latest off-cluster copy location.
- Velero BackupStorageLocation phase and latest backup name.
- CloudNativePG backup and WAL archive status.
- Longhorn backup target status and at least one successful volume backup.
- Forgejo repository backup/export proof.
- Harbor registry metadata backup and object storage backup proof.
- SOPS or external secret recovery proof, including the holder of the age
  private key or equivalent decrypt authority.
- Restore drill evidence with operator, date, DRILL_ID, elapsed restore time,
  and pass/fail result.

## Restore Drill Scope

Run the drill in an isolated test cluster, isolated recovery namespace, or
disposable lab environment. Do not restore over a live production namespace
unless you are executing an approved incident response plan.

The drill should prove all of these:

1. **Control plane recovery**: restore or validate an RKE2 etcd snapshot and
   confirm the Kubernetes API becomes healthy.
2. **Kubernetes object recovery**: restore a small Velero backup and confirm
   owned resources are present.
3. **Database recovery**: restore a CloudNativePG cluster from backup and WAL
   archive, then run a read query against known test data.
4. **Volume recovery**: restore a Longhorn backup into a scratch PVC and verify
   file contents or checksums.
5. **Git recovery**: restore or import Forgejo data, then run `git clone` and
   `git fsck` against a restored test repository.
6. **Registry recovery**: restore Harbor metadata and object storage, then run
   `docker pull` or `crane digest` for a restored test image.
7. **GitOps recovery**: point Argo CD at the restored GitOps repository and
   confirm it can compare and sync a harmless test application.
8. **Ingress recovery**: prove the restored service answers through the app
   VIP and expected FQDN.
9. **Secret recovery**: decrypt one SOPS-encrypted test Secret or retrieve one
   test secret from the selected external secret system.
10. **Certificate recovery**: confirm required cert-manager Certificates,
    trust-manager Bundles, and step-ca health checks are ready when those
    components are enabled.

## Drill Cadence

Run a restore drill:

- Before first production use.
- Quarterly.
- Before each production upgrade that changes RKE2, storage, database,
  backup, registry, or GitOps components.
- After changing object storage, backup credentials, SOPS recipients, CA
  material, or disaster recovery ownership.

## Acceptance Template

Use a private record like this:

```text
DRILL_ID:
Date:
Operator:
Cluster/profile:
RPO target:
RTO target:
Latest etcd snapshot:
Latest Velero backup:
Latest CloudNativePG backup/WAL point:
Latest Longhorn volume backup:
Forgejo repository verified:
Harbor image verified:
SOPS or external secret recovery verified:
Argo CD sync verified:
Ingress/FQDN verified:
Elapsed recovery time:
Result:
Follow-up actions:
```

## Failure Handling

If any restore step fails, do not mark the platform production-ready. Fix the
backup target, credential, retention, restore command, or component-specific
configuration, then rerun the drill from a clean recovery environment.

The common failure pattern is a green backup job with missing restore
credentials, incomplete WAL archive, inaccessible object storage, or an
unverified registry/Git data path. Treat restore evidence as the source of
truth.
