# Backup and Restore

Backups are not production evidence until a restore drill proves that the
platform can recover useful service, data, and GitOps control.

## Required Backups

| Area | Required protection |
|---|---|
| RKE2 control plane | Etcd snapshots and [verified offline RKE2 install artifacts](#stage-the-rke2-release-for-offline-recovery) copied off the cluster |
| Kubernetes objects | Velero with an available BackupStorageLocation |
| PostgreSQL | CloudNativePG backup plus WAL archive |
| Volumes | Longhorn backup target or the selected storage backend backup |
| Object storage | Provider replication plus off-cluster copy |
| Git repositories | Forgejo, Gitea, or GitLab repository backup/export |
| GitOps bootstrap source | Verified [GitOps branch bundle](MULTI_RUNTIME_STACKS.md#keep-a-gitops-source-copy-outside-the-platform) and the offline manifest and image cache for [Argo CD](MULTI_RUNTIME_STACKS.md#assemble-and-verify-the-argo-cd-recovery-directory) or [Flux](FLUX.md#prepare-an-offline-controller-cache), stored off-platform |
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
- The RKE2 server token from `/var/lib/rancher/rke2/server/token`. RKE2
  needs the original token to decrypt bootstrap data when restoring an
  etcd snapshot onto replacement servers. [RKE2 snapshot restore](https://docs.rke2.io/datastore/backup_restore#restoring-to-new-hosts)

### Stage the RKE2 release for offline recovery

On a connected machine, obtain `install.sh` from the selected release tag
and the binary tarball, base image archive and checksum list from that
release. Use files for the target architecture. For example, Linux amd64
with Cilium needs the following cache entries.
[RKE2 air-gap installation](https://docs.rke2.io/install/airgap)

- `install.sh`
- `sha256sum-amd64.txt`
- `rke2.linux-amd64.tar.gz`
- `rke2-images.linux-amd64.tar.zst`
- `rke2-images-cilium.linux-amd64.tar.zst`

Record the installer and checksum-list SHA-256 values independently of
the copied cache. Put the cache at an absolute path on each target node.
From the controller, use the normal installer playbook after host
preparation. The target nodes need Python 3 and any OS and SELinux
prerequisites already provisioned from a local source.

```bash
export RKE2_OFFLINE_CACHE=/private/recovery/rke2-release
export RKE2_OFFLINE_CHECKSUMS_SHA256="$REVIEWED_CHECKSUM_LIST_SHA256"
export RKE2_INSTALL_SCRIPT_SHA256="$REVIEWED_INSTALLER_SHA256"
export RKE2_VERSION="$REVIEWED_RKE2_VERSION"
ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/install-rke2.yml
```

The two manual server bootstrap scripts also accept these environment
variables. The offline installer checks every selected tarball, stages
only those assets, and runs the pinned installer in tar mode without
downloading it or its release artifacts. This tar path is for fresh
recovery hosts or installations already using tar. Keep matching
registry mirrors or locally loaded images for other services such as
kube-vip and GitOps-managed applications.

An isolated Linux test installed the pinned v1.36.2+rke2r1 Linux amd64 tar
release and staged its base and Cilium image archives without an upstream
network interface. The generated service unit and installed binary
version were checked. The replacement-host drill below tested a local
snapshot restore using that same RKE2 release.

### Recover the first RKE2 server from a local snapshot

Use a replacement host in the recovery network. Stop the source control
plane or isolate it before making the restored cluster reachable to clients.
Install the reviewed RKE2 release, restore the cluster's network and
critical server configuration, and leave the service stopped. The first
replacement server starts without a join URL. Restore the original server
token as a root-only file at `server/token` under the selected RKE2 data
directory. Keep any token settings in the server config consistent with
that saved token. Verify the snapshot digest against the recovery record
before restoring. [RKE2 restore procedure](https://docs.rke2.io/datastore/backup_restore#restoring-to-new-hosts)

```bash
sudo install -d -m 0700 /var/lib/rancher/rke2/server
sudo install -o root -g root -m 0600 "$SAVED_RKE2_TOKEN_FILE" /var/lib/rancher/rke2/server/token
printf '%s  %s\n' "$REVIEWED_SNAPSHOT_SHA256" "$RECOVERY_SNAPSHOT" | sha256sum --check --strict
sudo "$RKE2_BIN" server --cluster-reset --cluster-reset-restore-path="$RECOVERY_SNAPSHOT" --etcd-s3=false
sudo systemctl start rke2-server
```

Confirm the API is ready, that it returns the expected cluster identity and
a known object from the snapshot, and that a write succeeds in the recovery
environment. On each stopped peer, move the old `server/db` directory
aside before rejoining with the restored server's registration endpoint
and the saved token. Start peers one at a time and check that each
becomes a voting member before continuing. Compare the member list and
endpoint status across all three servers. Review old member and Node
identities before reconnecting workloads.
[RKE2 multi-server restore](https://docs.rke2.io/datastore/backup_restore#snapshot-restore-steps) · [RKE2 etcd checks](https://github.com/rancher/rancher-docs/blob/main/docs/troubleshooting/kubernetes-components/troubleshooting-etcd-nodes.md)

The isolated Linux amd64 drill saved a live embedded-etcd snapshot on a
two-server RKE2 v1.36.2+rke2r1 source using Canal. It copied the snapshot
outside the source guests, made a post-snapshot change, and stopped both
source servers. A third guest restored from that copy and the original
server token. The recovered API returned the original cluster UID and
pre-snapshot ConfigMap and Secret data, excluded the post-snapshot object,
and accepted a new write.

The two original servers then rejoined the restored server with their
previous databases set aside. All three reported the same three etcd
voters. With the elected leader VM paused, the other two elected a new
leader and committed an API write that both surviving servers returned.
After resuming, the original leader returned the same object's UID.

The same drill preloaded kube-vip v0.8.9 on all three servers and deployed
the rendered DaemonSet with `imagePullPolicy: Never`. After the VIP owner
was paused, a surviving server acquired the Lease and bound the VIP. An
authenticated client wrote a ConfigMap through the same TLS-verified API
address on TCP 6443. While the original owner was still paused, a fourth
VM registered as a fresh RKE2 agent through that VIP on TCP 9345 using a
secure token and reached `Ready`. The resumed server returned the same
ConfigMap and worker Node UIDs, and the VIP converged on a single owner.
The drill passed on an isolated virtual network with Canal and, in a
separate run using the same release, with bundled Cilium. In the Cilium
run all four nodes became Ready. Two worker pods initially reached a
ClusterIP service on another node. An ingress NetworkPolicy preserved
access for the labelled client and blocked the other. Removing the
policy restored its access. Application service failback still needs
separate acceptance.

To deploy kube-vip during offline recovery, preload the reviewed
`registry/repository@sha256:…` image under that exact reference on all
three servers. For a verified OCI archive whose named image already uses
the full digest reference, import it directly into RKE2's `k8s.io` image
store with `ctr images import` and omit `--digests`. The latter generates
additional aliases that can interfere with container startup.
[containerd import flags](https://github.com/containerd/containerd/blob/main/cmd/ctr/commands/images/import.go)

```bash
sudo /var/lib/rancher/rke2/bin/ctr \
  --address /run/k3s/containerd/containerd.sock --namespace k8s.io \
  images import "$VERIFIED_KUBE_VIP_OCI"
```

Include the VIP in each API server's TLS SANs. Set
`KUBE_VIP_IMAGE` to the digest reference and run
`KUBE_VIP_PRELOADED=true make rke2-api-vip` with the recovery inventory.
The playbook checks the image in each local container runtime and
sets `imagePullPolicy: Never` in the DaemonSet. Confirm the Lease holder,
the VIP on that server's interface, and authenticated readiness through
the VIP. [RKE2 fixed registration address](https://docs.rke2.io/install/ha) · [kube-vip DaemonSet](https://kube-vip.io/docs/installation/daemonset/)

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

For the premium profiles, strict private rendering places at least three
CloudNativePG instances on separate Kubernetes hosts. The live gate checks the
Ready database pods and their node placement. The default daily base backup
runs at 02:00 using CloudNativePG's six-field schedule, `0 0 2 * * *`. The
renderer adds a leading zero to existing five-field settings. The gate checks
the controller's last and next scheduled execution and a recent completed
backup of `platform-postgres` on the configured S3 endpoint and path.

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
