# Optional native CephFS peer check

`scripts/render_ceph_native_check.py` packages two manual, non-root CephFS
clients for `raw` Linux or Docker bind mounts. It also verifies both receipts
against the original [storage provisioner](CEPH_STORAGE.md) operation and live
provider readiness. This complements the [Kubernetes CSI check](CEPH_CLIENT_CHECK.md).

This helper operates on already prepared mounts. It does not install Ceph
packages, mount a filesystem, issue/reconcile client credentials, change access
rights, restart applications, unmount storage or delete canary data.
The separate [mount preparation helper](CEPH_NATIVE_MOUNT.md) supplies an
explicit mount operation after binary, kernel and credential provisioning.

## Prepared hosts and scope

Copy `examples/ceph-native-check.json` into ignored `private/`. Review:

- The exact canonical version 2 storage-plan hash and the original completed
  storage receipt's byte hash. The verifier uses that plan's explicit TLS
  Kubernetes management target. No default kubeconfig is selected.
  Set `storage_runtime` to the runtime recorded in that original receipt; it
  can differ from the native client's `--runtime`. An RKE2-created provider
  can therefore serve native/Docker clients without rewriting its evidence.
- Two real, independent Linux machines and physical failure domains, their
  distinct machine-ID hashes and current boot UUIDs. Hash the stripped
  `/etc/machine-id` text; do not include its trailing newline. A label alone
  does not establish physical independence.
- A dedicated, existing CephFS subtree and separate client identities. Provision
  their permissions, keys and a writable directory for the same non-root
  UID/GID on both hosts separately. The checker does not certify CephX caps.
  Path-based MDS restrictions alone do not isolate underlying RADOS data;
  review namespace isolation before using this design for untrusted tenants.
- Both explicit mount and package paths. Keep packages outside the mount.
  Prepare direct mounts using Ceph's current
  `client@fsid.filesystem=/subtree` device syntax, with `rw,nosuid,nodev,noexec`
  and `ms_mode=secure`. Keep credentials in private host files, outside the
  package. Root filesystem mounts, older ambiguous device strings,
  fallback encryption, `recover_session=clean`, nested/stacked mounts and
  read-only mounts are refused by this check.
- A fresh independent operation UUID, 64 KiB to 4 MiB payload and 10–300 second
  peer deadline. Retain original files after an interruption. An existing
  payload is never overwritten; repeated writer execution is refused.
  A failed run needs inspection before a new operation is prepared.

The mount choices follow the upstream
[Ceph mount helper](https://docs.ceph.com/en/tentacle/man/8/mount.ceph/).
Review [CephFS client capabilities](https://docs.ceph.com/en/tentacle/cephfs/client-auth/)
separately when preparing restricted identities.

## Package and run the peers

Render a new private package locally; this performs no host or provider calls:

```bash
python3 scripts/render_ceph_native_check.py --runtime raw \
  --plan private/ceph-native-check.json --storage-plan private/ceph-storage.json \
  --output-dir private/ceph-native-package-001
```

Use `--runtime docker` for the Docker launchers. Copy the identical reviewed
package to each host's declared `package_path`, owned/readable by the reviewed
UID/GID. The source files and complete scope are hash-bound. Keep this package
private because it contains deployment identities, even though it contains no
Ceph keys or other credentials. Python 3.12 or newer is required.

The launchers perform local scope validation by default. For raw Linux, after checking that
the dedicated mounts and peer identities match the plan, run the two roles
concurrently on their respective hosts as the declared UID/GID:

```bash
umask 077
# On client-1:
sh /opt/platform/ceph-canary/run-cephfs-left.sh --execute > left-receipt.json
# On client-2, while client-1 is running:
sh /opt/platform/ceph-canary/run-cephfs-right.sh --execute > right-receipt.json
```

For Docker execution, use the managed operation below. The simple Docker
launcher remains useful for local validation, but its agent receipt alone is
insufficient for Docker acceptance.

Each peer verifies its host/boot identity and the exact kernel mount. It pins
the mount ID, device and open directory descriptor before writing; file I/O
uses a duplicate of that descriptor instead of resolving the path again. Each
peer writes and syncs its own new payload before publishing a readiness
marker. It waits for the other peer's marker, verifies the bytes and waits
for the durable acknowledgement. Mount, source and host identities
are rechecked before a success receipt is emitted. Existing files, symlinks
and incomplete peers fail the check. Data remains in the operation's private
`.platform-canary-UUID` directory for inspection.

## Managed Docker execution

Add `docker_hosts` to the private native plan before rendering its package.
`examples/ceph-native-docker-hosts.json` supplies the field shape to merge;
it is not a standalone native plan. For each declared host pin the SHA-256
of its root-owned `/usr/bin/docker`, the independently reviewed engine ID,
exact stable server version and local immutable image ID (`sha256:...`). The
image must also advertise the original plan's registry digest, match the
host's Linux architecture and declare no anonymous volumes. Preload it
separately; the helper never pulls images or consults registry credentials.

`scripts/manage_ceph_native_docker.py` uses the local root-owned, non-public
Unix socket, a clean CLI environment, rootful Linux/runc and built-in seccomp.
Its normal CLI cannot select a remote daemon. Run on each approved host with
an explicit root Linux session; the container itself uses the declared
non-root UID/GID. A default invocation validates local inputs only.

Check both hosts before executing either peer:

```bash
python3 scripts/manage_ceph_native_docker.py --check-host --role cephfs-left \
  --plan private/ceph-native-check.json --storage-plan private/ceph-storage.json
```

Use `--role cephfs-right` on the other host. Then execute the two roles
concurrently, each with its own new private attempt directory:

```bash
python3 scripts/manage_ceph_native_docker.py --create --role cephfs-left \
  --plan private/ceph-native-check.json --storage-plan private/ceph-storage.json \
  --output-dir private/docker-peer-left-001 --wait-seconds 300
```

The manager records a create intent before creating a retained, unstarted
container with unique attempt labels. It verifies the original ID, complete
inspected policy, reviewed image, exact four bind mounts, non-root identity,
resource limits, isolation and command before recording a separate start
intent. There are no network/device/extra-volume grants or mount privileges.
The exited container must have zero restarts, no OOM/failure and exactly one
valid peer receipt. Host, package, mount, socket, engine and image identities
are rechecked before writing `agent-receipt.json` and `docker-peer-proof.json`.

Keep the original journal and container. After an interruption, use the same
arguments and output directory with `--resume`. Lost create acknowledgements
can be resolved only through the original name, attempt labels and matching
policy. Lost start acknowledgements observe the already running/exited
container. An absent uncertain create or an uncertain start still in Created
state is refused rather than repeated. Failure/timeout never automatically
stops, restarts or removes a container or deletes canary data. Review original
handles before manual cleanup. A completed proof describes that execution;
it is not a continuing remote-host health check.

The lifecycle uses Docker's separate
[create](https://docs.docker.com/reference/cli/docker/container/create/),
[start](https://docs.docker.com/reference/cli/docker/container/start/)
and [inspect](https://docs.docker.com/reference/cli/docker/container/inspect/)
operations.

## Verify against the provider

Copy both receipts into private storage on the management runner, with mode
0600 and ownership matching its user. Run:

```bash
python3 scripts/render_ceph_native_check.py --runtime raw --verify \
  --plan private/ceph-native-check.json --storage-plan private/ceph-storage.json \
  --storage-proof private/storage-create-001/ceph-storage-proof.json \
  --left-receipt private/left-receipt.json --right-receipt private/right-receipt.json \
  --output-dir private/ceph-native-verification-001
```

Use the same runtime as packaging. Verification checks original storage UIDs,
configuration/API identities, CSI/provider readiness and Ceph pool headroom.
Receipts must bind the exact hosts, boots, scope and source hashes, verify both
payloads/barriers, overlap in time and be no more than one hour old. It writes
a new private `ceph-native-proof.json`; it submits no resource writes.

For Docker use `--runtime docker`, supply both managed `agent-receipt.json`
files and add `--left-docker-proof private/left-docker-peer-proof.json` and
`--right-docker-proof private/right-docker-peer-proof.json`. Both execution
proofs must match the reviewed Docker pins, original container IDs, current
helper hashes and the exact peer receipts. Container timestamps must contain
each peer's exchange. Unmanaged Docker receipts are refused.

Only `native_cephfs_mounts_verified` and
`native_cephfs_data_roundtrip_verified` become true. For a Docker pair,
`docker_runtime_verified` additionally requires both managed execution proofs.
Native RBD, credential/cap verification, node loss, fencing, recovery, HA and
full-stack acceptance remain false. These receipts require trusted hosts and
operators; they are not remote hardware attestations.

## Local validation

`python3 -B scripts/test_ceph_native.py` checks scope/mount/refusal policy and
original-provider binding. Linux tests exercise actual descriptor-based
payload exchange and the acknowledgement barrier on disposable local
directories. Mount identity and provider responses in those tests are
simulated; they do not establish live Ceph or Docker acceptance.
`scripts/test_ceph_native_docker.py` adds simulated create/start recovery,
policy injection, proof/collector checks and a real private Unix-socket/package
metadata fixture on root Linux. It does not run a Docker daemon or Ceph backend.
