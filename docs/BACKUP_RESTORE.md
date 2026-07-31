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
- The `longhorn-system/longhorn-crypto` volume-encryption key and its recovery
  metadata. `platform-app-secrets` maintains the Git-ignored controller copy at
  `LONGHORN_ENCRYPTION_RECOVERY_FILE`; replicate it into a separate encrypted
  failure domain.
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
- An isolated restore proving the externally escrowed Longhorn encryption key
  can unlock a restored encrypted volume without exposing the key in evidence.
- Forgejo repository backup/export proof.
- A current Forgejo active-passive recovery drill proving a new pod UID, the
  same service/PVC/PV/image identities, healthy attached Longhorn storage, and
  service recovery inside its accepted RTO.
- Harbor registry metadata backup and object storage backup proof.
- SOPS or external secret recovery proof, including the holder of the age
  private key or equivalent decrypt authority.
- Restore drill evidence with operator, date, DRILL_ID, elapsed restore time,
  and pass/fail result.

Keep the machine-readable acceptance record outside public Git. Start with the
schema-v2 `examples/restore-evidence.example.json`, replace every example value
with real retained proof, and set these values in the ignored deployment env
file. Schema v1 records are historical only and fail the production gate:

```bash
PLATFORM_RESTORE_EVIDENCE_FILE=private/restore-evidence.json
PLATFORM_RESTORE_DRILL_MAX_AGE_DAYS=92
PLATFORM_FORGEJO_RECOVERY_EVIDENCE_FILE=private/forgejo-recovery-evidence.json
PLATFORM_FORGEJO_RECOVERY_MAX_AGE_DAYS=92
PLATFORM_FORGEJO_RECOVERY_MAX_RTO_SECONDS=300
PLATFORM_DATA_PROTECTION_MAX_ETCD_AGE_HOURS=8
PLATFORM_DATA_PROTECTION_MAX_BACKUP_AGE_HOURS=26
```

The operator and approver must be different people. Every required check must
be `passed` and contain an approved evidence URI, SHA-256 digest, and timestamp.
The restore record's `sourceCommit` must exactly match the Git revision under
production acceptance; evidence from an older or different deployment is
rejected even when it is otherwise fresh. The schema-v7 production packet
atomically retains and hash-binds the exact restore and Forgejo recovery records
that passed so the final score can revalidate them independently.
The `longhornEncryptionKey` check must point to proof that recovery personnel
retrieved the escrowed key and mounted restored encrypted data; the evidence
must never contain the key itself.
The gate derives actual RPO/RTO from backup, recovery-start, and completion
timestamps; requires an isolated cluster or disposable lab in another failure
domain; and requires successful failover and reconciled failback proof.
Validate the record and live backup state together:

```bash
make platform-data-protection
```

This gate rejects an in-cluster MinIO endpoint as disaster-recovery storage,
stale or missing etcd/Velero/CloudNativePG/Longhorn backups, unhealthy WAL
archiving, missing volume-data movement, stale or incomplete restore proof, and
Forgejo recovery evidence that is stale, belongs to another profile or commit,
exceeds RTO, reuses the old pod or node, changes persistent identities, lacks
encrypted Longhorn CSI key references, or leaves the source node cordoned.

Create the separate availability record only during an approved maintenance
window. Start with `examples/forgejo-recovery-evidence.example.json` for the
schema, but let the drill produce the real record:

```bash
PLATFORM_FORGEJO_RECOVERY_OPERATOR="${OPERATOR_ID:?set OPERATOR_ID}" \
PLATFORM_FORGEJO_RECOVERY_APPROVER="${INDEPENDENT_APPROVER_ID:?set INDEPENDENT_APPROVER_ID}" \
PLATFORM_FORGEJO_RECOVERY_CONFIRMATION=FAILOVER_FORGEJO_SINGLETON \
make platform-forgejo-recovery-drill
```

The drill requires at least two Ready, schedulable nodes. It cordons the current
Forgejo node, deletes only the managed singleton pod, requires the replacement
to become healthy on another preflight-eligible node within the RTO, and always
uncordons the source node. A cleanup failure prevents passing evidence.

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
Source Git commit:
Operator:
Approver:
Profile:
Backup completed at:
Recovery started at:
Recovery completed at:
Recovery target and failure domain:
RPO target:
RTO target:
Latest etcd snapshot:
Latest Velero backup:
Latest CloudNativePG backup/WAL point:
Latest Longhorn volume backup:
Object storage verified:
Forgejo repository verified:
Harbor image verified:
SOPS or external secret recovery verified:
Argo CD sync verified:
Ingress/FQDN verified:
Certificate/trust verified:
Failover DNS/VIP/TLS and consistency verified:
Failback backup and reconciliation verified:
Evidence URI, SHA-256, and verification time for every item:
Elapsed recovery time:
Result:
Follow-up actions:
```

The text template is useful during execution; the JSON record consumed by the
production gate is the authoritative machine-readable result.

## Failure Handling

If any restore step fails, do not mark the platform production-ready. Fix the
backup target, credential, retention, restore command, or component-specific
configuration, then rerun the drill from a clean recovery environment.

The common failure pattern is a green backup job with missing restore
credentials, incomplete WAL archive, inaccessible object storage, or an
unverified registry/Git data path. Treat restore evidence as the source of
truth.
