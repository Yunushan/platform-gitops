# Optional encrypted host-OSD key recovery

`scripts/ceph_key_recovery.py` captures the key material for one explicitly
selected, healthy Rook provider using encrypted host devices. It uses the same
explicit Kubernetes management target for RKE2, K3s, existing Kubernetes, and
the external provider consumed by Docker/native applications. It does not
install a hidden Kubernetes cluster or change a provider.

The multi-runtime stack planner lists this optional helper for all ten stacks
and all five runtimes when the helper, material reader, example and runbook are
present. That discovery leaves provider capture, escrow, restoration and overall
deployment readiness unverified; local fixture results cannot qualify a selected
provider automatically.

Host-device encryption must be handled separately from PVC-backed KMS keys.
[Ceph's host-OSD encryption workflow](https://docs.ceph.com/en/tentacle/ceph-volume/lvm/encryption/)
stores the dm-crypt material through the monitor. The pinned
[Rook activation program](https://github.com/rook/rook/blob/v1.21.2/pkg/operator/ceph/cluster/osd/activate-osd.sh)
uses the OSD UUID's monitor config-key and lockbox identity. Copying only
Kubernetes `dmcrypt-key` Secrets would omit that host-device recovery material.
PVC-backed and external-KMS providers are refused by this collector.

The capture includes the complete reviewed OSD ID/UUID inventory, each
`dm-crypt/osd/<UUID>/luks` value, the Ceph auth export including lockbox
identities, and the original owned monitor bootstrap Secret and endpoint
ConfigMap. It verifies the management cluster, namespace, CephCluster UID/spec,
FSID/release, original toolbox pod/container/image/start identity and node UID.
It requires two matching complete reads. Fixed Ceph commands run under an
in-container timeout; the helper never uses `get-or-create`, formats a disk,
replaces a pod, restores a cluster or imports an auth/config key.

## Private prerequisites

Copy `examples/ceph-key-recovery.json` into ignored `private/` and replace all
placeholders with independently reviewed observations. Pin the admitted
CephCluster and toolbox `.spec` hashes as SHA-256 of sorted compact UTF-8 JSON.
Pin the original runtime `imageID` separately: its platform manifest may differ
from the multi-architecture digest in the approved pod specification.
Keep the provider inventory, plan, journals and recovery identities private.
Use one approved existing toolbox containing `/usr/bin/ceph` and
`/usr/bin/timeout`; this helper does not provision that privileged access.

Pin an independently reviewed native `age` executable by its own SHA-256 and
select a native X25519 recipient. Keep the matching private identity outside
the provider and OpenBao failure domains. No plugin or SSH identity is executed
by offline verification. The helper passes plaintext to age through a bounded
pipe and never writes captured plaintext keys to a local file or to OpenBao.

Provision the existing OpenBao 2.7.2 three-voter service, CA, audit/recovery
arrangements and a KV v2 mount separately. Select one top-level mount name and
pin that mount's original UUID. Pin the cluster ID, voting member addresses
and CA content; export the restricted token only through the plan's
named environment variable. Plain HTTP, redirected requests, untrusted TLS,
root privileges and unrelated data/destructive permissions are refused.

Choose a fresh `operation_id` before issuing the token. For a plan using mount
`recovery` and prefix `ceph/provider`, the required policy is:

```hcl
path "recovery/data/ceph/provider/<OPERATION_UUID>" {
  capabilities = ["create", "read"]
}
path "recovery/metadata/ceph/provider/<OPERATION_UUID>" {
  capabilities = ["read"]
}
path "sys/storage/raft/configuration" { capabilities = ["read"] }
path "sys/mounts/recovery" { capabilities = ["read"] }
path "sys/capabilities-self" { capabilities = ["update"] }
```

The operation ID is explicit so its exact policy can be prepared in advance.
The exact mount is read through the system API and must still be the original
KV v2 engine before any write. A KV v1 engine cannot satisfy this check.
Do not grant a wildcard over other backup entries. The helper never creates
policies, tokens, mounts or unseal material itself.

## Capture and resume

Local mode validates the plan without constructing clients or reading tokens:

```bash
python3 scripts/ceph_key_recovery.py --plan private/ceph-key-recovery.json
python3 scripts/ceph_key_recovery.py --plan private/ceph-key-recovery.json \
  --mode capture --output-dir private/ceph-key-capture-001
```

The output directory must be new, canonical and inside ignored `private/`.
The local `ceph-recovery.age` file is committed before a single KV v2 write
using `cas=0`. OpenBao receives the encrypted archive and its scope, never the
plaintext keyring or OSD keys. The journal commits the original write intent
before that request. Completion requires the same version-1 ciphertext to be
read back from the original OpenBao entry. An existing entry is never replaced.
[OpenBao's KV v2 CAS semantics](https://openbao.org/docs/secrets/kv/kv-v2/)
provide the server-side create-only condition.

```bash
python3 scripts/ceph_key_recovery.py --plan private/ceph-key-recovery.json \
  --mode resume --output-dir private/ceph-key-capture-001
```

Resume preserves the original operation. After a lost write acknowledgement,
it only observes the original entry; even a 404 does not authorize another
POST. If capture stopped in the durable pre-write `encrypted` phase, resume
can submit that original first write. Changed plans or implementations cannot
resume a write. Do not delete the archive/journal, select another entry, or
start overlapping operations to clear an uncertain result.

## Recover ciphertext and verify without live services

If the encrypted file is missing but the original private journal survives,
`recover` reads the exact original OpenBao version and recreates only that
missing file. It cannot overwrite an existing archive or contact the provider.

```bash
python3 scripts/ceph_key_recovery.py --plan private/ceph-key-recovery.json \
  --mode recover --output-dir private/ceph-key-capture-001
python3 scripts/ceph_key_recovery.py --plan private/ceph-key-recovery.json \
  --mode verify-archive --output-dir private/ceph-key-capture-001 \
  --identity-file /secure/off-platform/ceph-recovery-identity
```

Offline verification requires neither Kubernetes nor OpenBao. Preserve the
original plan, journal, pinned age executable and separate private identity in
the independent bootstrap material. The helper verifies decryption, original
scope and material completeness without printing plaintext. A receipt checksum
checks file integrity; it is not an independent operator signature.

## Acceptance boundaries

Model tests cover source identity changes, key changes, restricted credentials,
CAS conflicts, lost acknowledgements, corruption and ciphertext recovery. A
pinned native age fixture additionally checks real encryption/decryption,
wrong identities and tampering. Model tests do not establish real Ceph collection.

A separate private Linux fixture used three native OpenBao 2.7.2 processes with
verified TLS and native age 1.3.2 in one isolated network namespace. A restricted
token committed the ciphertext with KV v2 CAS zero before an injected lost
acknowledgement. After the original leader stopped, resume verified the same
entry through the unchanged TLS endpoint, and recovery recreated the missing
archive byte for byte. With all OpenBao processes stopped, offline decryption
verified the original material and scope. The HMAC audit contained one original
KV write and two reads, for resume and recovery. Original processes were removed.

Ceph reads in that fixture were modeled. This is local OpenBao/age component
evidence, not real host-OSD collection, physical failure-domain availability,
off-platform recovery or provider restoration. Those acceptance checks remain
open; the fixture does not permit any broader result flags to become true.

`captured` and `escrow_verified` concern only this encrypted key archive.
`source_fencing_verified`, `monitor_database_backup_verified`,
`provider_restore_verified`, `off_platform_verified`, `ha_verified` and
`full_stack_verified` remain false. Repeated matching reads are not fencing
against another administrator. Coordinate key changes during capture and take
a new capture after key rotation or OSD inventory changes.

These keys do not replace monitor-store backups, intact LUKS headers, device
metadata, application data backups or an isolated restoration drill. Preserve
those independently and verify real provider/application recovery before
accepting any complete stack.
