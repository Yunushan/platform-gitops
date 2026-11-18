# Optional Ceph client data-path acceptance

`scripts/render_ceph_client_check.py` and `scripts/verify_ceph_clients.py`
prepare and run fresh CSI client canaries against the original resources from
the [Ceph storage provisioner](CEPH_STORAGE.md).
For prepared Linux mounts outside CSI, use the separate
[native CephFS peer check](CEPH_NATIVE_CHECK.md).

The check creates two new 1 GiB filesystem claims and four no-retry Jobs:

- An RBD writer fsyncs a deterministic payload and directory on the first node.
  After that Job completes and its original writer attachment is released, an
  independent reader mounts the same claim read-only on the second node and
  verifies every byte.
- Two CephFS clients on those nodes each write and fsync their own payload
  before publishing a readiness marker. Each waits for the other's marker,
  verifies the other client's bytes, and publishes a durable acknowledgement. Both
  wait for the peer acknowledgement before exiting. This requires overlapping
  client mounts; independently successful writes are insufficient.

No client Job is scheduled with `nodeName`: required node affinity selects its
reviewed node while preserving scheduling for WaitForFirstConsumer claims.
The check never forces detachment, kills nodes, changes a provider, adopts a
claim, overwrites an existing canary file, or retries an uncertain create.

## Private prerequisites

Use a version 2 storage plan. Its explicit operator namespace can differ from
the provider namespace. Its `access_nodes` inventory includes both the OSD
hosts and approved client/controller workers; CSI controllers are checked
against that complete inventory and node plugins must cover it exactly.
The two canary nodes must match reviewed `access_nodes` entries, including
their UIDs and domains. Workers do not become OSD hosts or add capacity.

Complete the [Rook bootstrap](ROOK_BOOTSTRAP.md), provider creation and storage
provisioning first. Copy `examples/ceph-client-check.json` into ignored `private/`
storage and replace all placeholders. Bind:

- The exact SHA-256 of the complete canonical storage plan and the exact bytes
  of its original completed private `ceph-storage-proof.json` receipt. The
  original operator/CSI/provider/namespace/CRD identities and current Ceph
  health, pool quotas, headroom and metadata services are rechecked live.
- A dedicated, pre-created namespace, its original UID, and
  `pod-security.kubernetes.io/enforce=restricted`. Keep it separate from the
  storage provider and application namespaces.
- A new independent operation UUID and two Ready, schedulable Linux nodes in
  distinct reviewed failure domains. Node UIDs and hostname/domain labels are
  checked. CSI node plugins must cover the complete storage access inventory.
- A reviewed digest-pinned Python image supporting the standalone Linux
  program, and finite CPU/memory budgets. The program runs as UID 65532 with
  group/fsGroup 2000, no service-account token, a read-only root filesystem,
  dropped capabilities and RuntimeDefault seccomp. The fixed `/volume` mount
  must be ext4 for RBD or the kernel Ceph filesystem for CephFS.
- A payload between 64 KiB and 4 MiB and a peer timeout from 10 to 300 seconds.
  Allow time for CSI provisioning, filesystem permissions, unmount/detach and
  the reader's new mount. Clock differences over 30 seconds and receipts older
  than one hour are refused.

Plan fingerprints use sorted compact JSON encoded as UTF-8 without a trailing
newline. Receipt hashes cover the original file bytes, including its newline.
The receipt must be a private regular file with one hard link; on POSIX it must
belong to the current user with no group/other access. Tokens and Secret values
are never copied into the plan or public repository. Only CSI Secret references
on the bound PV are inspected.

Each PVC must bind to its original UID and to an original Retain PV with the
expected driver, class, access mode, provider cluster ID, pool/filesystem and
node-stage credential reference. PV specification hashes and UIDs are retained;
replacement or rebinding invalidates acceptance. Existing data sources,
prebinding and unreviewed admission changes are refused.
The original PV inventory is recorded before any creates; unbound/terminating
PVs on the selected classes cause refusal. A canary PV must have a new UID,
an operation-time creation timestamp and CSI provisioner identity. Quiesce
manual/static PV creation and rebinding on those classes during the check;
these observations are not fencing against a concurrent privileged operator.

## Render, inspect admission and run

```bash
python3 scripts/render_ceph_client_check.py --runtime rke2 \
  --plan private/ceph-client-check.json --storage-plan private/ceph-storage.json \
  --output-dir private/ceph-client-package

python3 scripts/verify_ceph_clients.py --runtime rke2 \
  --plan private/ceph-client-check.json --storage-plan private/ceph-storage.json \
  --storage-proof private/ceph-storage-create/ceph-storage-proof.json \
  --check-server --output-dir private/ceph-client-admission
```

The default verifier mode only validates local inputs and the original receipt.
`--check-server` revalidates the provider and server-dry-runs the new objects,
writes the concrete admitted bodies privately and returns `admitted_sha256`.
Review these bodies, including mount/privilege/scheduling policy, then pin that
hash in the client plan before creation.

```bash
python3 scripts/verify_ceph_clients.py --runtime rke2 \
  --plan private/ceph-client-check.json --storage-plan private/ceph-storage.json \
  --storage-proof private/ceph-storage-create/ceph-storage-proof.json \
  --create --output-dir private/ceph-client-create --wait-seconds 900
```

If observation or a create response fails, preserve the same plan, runtime,
operation UUID, output directory and storage receipt. Resume with `--resume`
in place of `--create`. The journal records intent before every create, then
the original resource UID. A missing uncertain resource is never recreated.
Observed pod UIDs, volume bindings and receipts are re-read and revalidated;
cached success flags alone cannot complete a resumed operation.

Failed Jobs and private files are retained for diagnosis. The last stage is
recorded as `failure_phase`. There is no automatic deletion of claims, PVs,
backend data or Jobs. Review cleanup using the original UIDs in the receipt;
Retain storage classes leave backend volumes after claim deletion. A new run
uses a new UUID and fresh claims, rather than reusing incomplete data.

## What a successful receipt proves

`kubernetes_client_mounts_verified` and
`kubernetes_data_roundtrip_verified` become true only after all four original
clients pass, both original PV chains still match, and the provider remains
ready. The program's receipts bind the operation/scope/program digests,
scheduled node, filesystem, payload hashes and CephFS peer barrier.

All five consumer labels are accepted because each can use the explicit
Kubernetes-managed provider. These Jobs verify its Kubernetes CSI access path.
Docker/native Ceph installation, restricted CephX credentials, client mounts,
fencing and application bindings require their own acceptance;
`native_client_mounts_verified` stays false. Node loss, recovery, HA, performance,
power-loss durability and complete-stack verification also stay false. This
check is one part of the requested storage acceptance, not a substitute for it.

`scripts/test_ceph_clients.py` exercises actual local POSIX file operations and
the peer barrier, plus simulated Kubernetes scheduling, binding and recovery.
Those tests do not establish live Ceph I/O. The optional
`ROOK_CLIENT_TEST_API=1` test uses the isolated Kubernetes API fixture documented
in [Rook bootstrap](ROOK_BOOTSTRAP.md), with the same
`ROOK_BOOTSTRAP_TEST_BIN` binary directory. It checks admission, creation and
read-back of the original client resources and the
[recovery reader resources](CEPH_STORAGE.md#include-reads-from-the-original-client-volumes).
It also dry-runs Pods built from the admitted recovery Job templates and
checks that admission preserves the non-root policy and read-only claim
mounts. The fixture has no workers or storage backend.

Upstream references: [Rook CSI driver names and access paths](https://rook.io/docs/rook/v1.21/Storage-Configuration/Ceph-CSI/ceph-csi-drivers/),
[RBD StorageClass](https://github.com/rook/rook/blob/v1.21.2/deploy/examples/csi/rbd/storageclass.yaml),
[CephFS StorageClass](https://github.com/rook/rook/blob/v1.21.2/deploy/examples/csi/cephfs/storageclass.yaml).
