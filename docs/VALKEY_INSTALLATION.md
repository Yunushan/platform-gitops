# Guarded fresh Valkey/Sentinel Kubernetes installation

After installation and convergence, use the separate [runtime observation
helper](VALKEY_RUNTIME.md) for pinned mTLS, replication and Sentinel checks.

`scripts/deploy_valkey_ha.py` creates a fresh, explicitly scoped deployment
from the exact [Valkey package](VALKEY_HA.md), for RKE2, K3s or existing
Kubernetes. It creates eleven resources: NetworkPolicy, three immutable seed
ConfigMaps, three headless Services, a PDB and three Recreate Deployments.
It creates no Secret, PVC, PV, namespace or Cilium resource and does not update,
delete, adopt or upgrade an existing installation.

The installer observes original resource/controller/Pod/container identities
and completed seed status. TCP readiness and reported init completion cannot
prove TLS trust, ACL correctness, replication, Sentinel quorum, application
compatibility or HA. Successful installation requires subsequent runtime and
failure verification before an application uses this group.

## Bind prepared target, credentials, storage and network

Copy [the installation-plan example](../examples/valkey-installation.json) into
ignored private storage. Replace every example identity/hash with reviewed
current observations. Pin the rendered `valkey-contract.json` file SHA-256 and
the explicit kubeconfig environment reference, context and original
`kube-system` UID. Each server operation freezes its static credential material
and verifies HTTPS; default contexts and executable credential plugins are not
used. Keep credentials out of the public plan and command arguments.

Pin the restricted namespace UID, the three Ready schedulable Linux node UIDs,
hostname/zone labels and declared internal IPs. Pin all six distinct immutable
Secret UIDs: three `kubernetes.io/tls` and three `Opaque` auth Secrets. The
installer requests only UID, type, immutability/deletion state and key-presence
booleans using a projection. It never retrieves Secret JSON or the certificate,
key, ACL or password contents.

TLS Secret keys are `tls.crt`, `tls.key`, `ca.crt`. Auth keys are
`server-auth.conf`, `server.acl`, `sentinel-auth.conf`, `sentinel.acl`. Prepare
auth with the private materializer using the selected runtime and member.
Metadata presence is not content validation: independently verify TLS and
effective role permissions using the intended client credentials.

Pin the six existing PVC UIDs and complete spec hashes, their PV names/UIDs and
complete spec hashes, and each StorageClass UID/configuration hash. Claims must
be Bound, Filesystem and ReadWriteOnce, without clone/data-source references or
pending resize conditions. Volumes must have exact claim bindings, retained
writable Ceph RBD ext4/xfs CSI storage and separate driver/volume-handle pairs.
A Pod already using a selected claim is refused even if it has unrelated labels.

Storage hashes use sorted-key compact JSON of the complete `spec`; a
StorageClass configuration hash removes only `apiVersion`, `kind`, `metadata`.
Requested capacity must cover the bound request. The installer also applies a
sizing floor of four times configured `maxmemory_mib` for each data/AOF claim
and 64 MiB for each Sentinel claim. These floors do not certify AOF rewrite
headroom, physical independence, Ceph health, encryption or restore capacity.
Review the real storage and failure domains separately.

Pin the current Cilium namespace, DaemonSet and ConfigMap UIDs, full agent
Pod-template/config-data hashes and digest-qualified agent/init images. The
current rollout must have one original Ready agent on each selected node and
Kubernetes NetworkPolicy support with policy mode `default` or `always`.
The installer changes no CNI configuration. Effective datapath isolation is a
separate live acceptance requirement.

## Inspect concrete server admission

Local mode regenerates and compares the package without constructing a client:

```bash
python3 scripts/deploy_valkey_ha.py \
  --bundle-dir private/generated/valkey-rke2 \
  --deployment-plan private/valkey-installation.json
```

Inspect the selected server and dry-run creation of every resource:

```bash
python3 scripts/deploy_valkey_ha.py \
  --bundle-dir private/generated/valkey-rke2 \
  --deployment-plan private/valkey-installation.json \
  --check-server --output-dir private/valkey-check-001
```

This performs reads and create dry-runs. It refuses pre-existing targets,
matching workloads and claim users, and writes normalized admitted material
to the private check directory. Review that material and pin its aggregate
`admitted_sha256` in the deployment plan. Ordinary API defaults are included;
injected executables, init sidecars, extra environment, privileges, foreign
template annotations and external/non-headless Services are rejected.

Fresh operation IDs are omitted from the policy digest so the reviewed policy
can be used by a separate apply operation. Actual objects must carry the exact
operation/contract annotations at both Deployment and Pod-template levels.
Changes after review or between create dry-run and creation fail closed.

## Create once, preserve seed authority and verify original processes

```bash
python3 scripts/deploy_valkey_ha.py \
  --bundle-dir private/generated/valkey-rke2 \
  --deployment-plan private/valkey-installation.json \
  --apply --output-dir private/operations/valkey-001 --wait-seconds 180
```

Network/configuration/Service/PDB resources precede workloads. A random original
installation ID is bound to the private journal and Pod template. The seed init
container receives it through the Downward API. On first use, it records both
the immutable seed hash and installation ID in each private runtime directory.
An ordinary replacement Pod from that same Deployment can preserve its dynamic
role/Sentinel/AOF state. A fresh installation with another ID refuses existing
runtime state, even when its initial seed hash matches. Partial seeds or legacy
markers missing an installation ID are refused. This installer supplies the
required annotation; a renderer-only manifest has no installation authority.

The installer neither deletes state nor treats a failed seed as permission to
reinitialize. Retain the original journal and investigate the exact volume
history. Original journal/resource recovery and deliberate state migration or
restore require separate workflows; do not clear markers to bypass checks.

Success requires three original Deployment/ReplicaSet/Pod chains on the pinned
nodes, Ready Pods, both server/Sentinel processes running after startup probes,
and the original completed seed container. Container IDs, image IDs, restart
counts, start/finish timestamps, Pod IPs and controller UIDs are retained.
These observations come from Kubernetes status and are not process attestation
or a Valkey protocol check.

## Resume an uncertain create without reissuing it

```bash
python3 scripts/deploy_valkey_ha.py \
  --bundle-dir private/generated/valkey-rke2 \
  --deployment-plan private/valkey-installation.json \
  --resume --output-dir private/operations/valkey-001 --wait-seconds 180
```

`valkey-install-proof.json` commits intent before each create. Resume observes
an uncertain result rather than sending that create again. Original UIDs,
annotations, admitted policy and recorded process identities must match.
Missing originals, foreign targets, a new rollout, changed inputs/source or a
restarted seed/server/Sentinel invocation cause refusal. No replacement is
adopted or recreated. Local journal writes continue during read-only API resume.

Completion ends at `awaiting-runtime-verification` with
`requires_runtime_verification: true`. TLS, authentication, replication,
quorum/failover, application compatibility, lossless writes, storage encryption,
network enforcement, off-platform recovery, physical-node loss and HA flags
remain false. This command does not prove native/Docker activation or provide
upgrades, rollout rollback, live credential rotation or independent restore.

Default CI covers state/ownership changes, lost acknowledgements, secret
metadata, init completion and refused admission. The opt-in
`VALKEY_INSTALL_TEST_API=1` fixture uses a disposable actual Kubernetes API and
the existing `ROOK_BOOTSTRAP_TEST_BIN` cache. Its nodes, CSI bindings, Cilium and
Pod statuses are synthetic. It creates no real cache process or storage volume;
keep its API lifecycle evidence separate from real TLS/Sentinel acceptance.

The disposable API lifecycle case passed with the current installer: eleven
original resources, recovery from a lost successful create response without a
duplicate create, read-only resume and rejection of a changed seed-container
identity. The operation-bound seed shell and six-process TLS/Sentinel failure
case also passed independently. These results do not establish that real
Kubernetes Pods, Cilium or Ceph storage have run this deployment.
