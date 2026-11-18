# Optional native CephFS mount preparation

`scripts/mount_cephfs_client.py` supplies a separate explicit mount operation
for the prepared Linux hosts used by the [native peer check](CEPH_NATIVE_CHECK.md).
It supports that check's `raw` and `docker` consumer selections. Docker clients
use a bind mount from the host; containers receive no mount capability or key.

This step does not install packages, provision clients/keys, create Ceph
directories, change application permissions, edit fstab, start boot services,
remount/unmount filesystems or delete data. Those lifecycle steps and native
RBD remain separate work. A successful mount is not a data or HA acceptance test.

## Private prerequisites

Complete the original provider/storage operation and review the full native
client plan first. Add Docker engine pins before this step if they are needed;
the mount plan binds the complete native plan's canonical SHA-256. Copy
`examples/ceph-native-mount.json` into ignored `private/` storage and replace
its examples/placeholders with independently reviewed observations:

- A separate new mount-operation UUID, the native-plan hash and both host names.
  Native plan fields already bind filesystem/FSID/subtree, host/boot identities,
  client names and explicit mount paths. Keep original plans immutable.
- Three distinct numeric monitor addresses and explicit ports. IPv4 and
  bracketed IPv6 are supported; DNS discovery is not used. Confirm these
  endpoints belong to the reviewed provider and are reachable from each host.
- The digest of each trusted `/usr/sbin/mount.ceph`, exact kernel release and
  an environment-variable reference to its private key file. Install/review the
  Ceph client package and load its kernel module separately. The module must
  expose enabled `/sys/module/ceph/parameters/mount_syntax_v2`; the helper and
  its ancestor paths must be root-owned and not writable by other users.
- A direct, pre-created empty mountpoint, owned by root with mode 0700, in the
  host mount namespace. Keep both package and operation journal outside it.
- A dedicated client secret file, root-owned mode 0600, with one hard link and
  no symlink in any path component. Use its canonical absolute path. Its
  parent paths must be root-owned and not writable by others.
  Use the separately provisioned restricted client's single base64 CephX key.
  The helper does not establish that its server-side capabilities are minimal.

Set only the local file reference in the root host session, for example:

```bash
export CEPH_CLIENT_1_SECRET_FILE=/etc/platform/ceph/client-1.secret
```

The key is never placed in the plan, command text or proof. The manager opens
and validates the file, holds its original descriptor during execution and
passes `secretfile=/proc/PARENT_PID/fd/FD`. The child reads through that reference
without receiving key bytes in its arguments or environment. File metadata is
rechecked; secret contents and hashes are not written into evidence.

The command uses explicit source/monitor details, `conf=/dev/null`,
`ms_mode=secure`, `noshare`, `recover_session=no`, `nofallback` and
`rw,nosuid,nodev,noexec`. `-n` prevents mtab writes. See the upstream
[Ceph mount manual](https://docs.ceph.com/en/tentacle/man/8/mount.ceph/) and
[pinned helper implementation](https://github.com/ceph/ceph/blob/v20.2.0/src/mount/mount.ceph.c).
The v2 kernel feature check is needed because that implementation may fall
back to old device syntax when the feature is not advertised.

## Validate, inspect and mount

A default invocation validates local plan data only; it does not read host
credentials or call a mount helper:

```bash
python3 scripts/mount_cephfs_client.py --runtime raw --host client-1 \
  --plan private/ceph-native-mount.json --native-plan private/ceph-native-check.json \
  --storage-plan private/ceph-storage.json
```

Use `--runtime docker` for Docker consumers. On each approved host, use an
explicit root Linux session and add `--check-host` to inspect the helper,
host/kernel/namespace, key-file metadata and target without mounting. An
existing exact matching mount is reported for review, but a new create
operation will not adopt it.

For an unmounted prepared target, request one mount:

```bash
python3 scripts/mount_cephfs_client.py --runtime raw --host client-1 --create \
  --plan private/ceph-native-mount.json --native-plan private/ceph-native-check.json \
  --storage-plan private/ceph-storage.json --output-dir private/ceph-mount-left-001
```

Use the corresponding host and key-file reference on the second client.
The manager takes a root-owned volatile per-target lock under
`/run/platform-ceph-mount-locks` before re-reading the mount state, so separate
attempt directories cannot concurrently issue requests for the same target.
The private locked journal retains the original empty directory, mount
namespace, credential-file handles, helper sources and initial mount IDs.
It syncs a mount intent before invoking the helper. Completion requires a new
matching kernel mount, its checked open-directory device/inode and unchanged
host/source evidence. It writes `cephfs-mount-proof.json` and reports only
`cephfs_mount_prepared=true`; client I/O, permissions, failover, recovery and
full-stack verification remain false.

## Interrupted requests

Preserve the original plan, helper revision and private directory. Replace
`--create` with `--resume` using the same arguments. Resume observes matching
new mount state after an interrupted acknowledgement. An uncertain request
whose mount remains absent is never repeated, and a removed/replaced observed
mount is refused. Changed boot/kernel/namespace, sources or credential-file
handles require inspection; no automatic remount or cleanup is attempted.

Kernel mounts cannot carry the operation labels used for Kubernetes/Docker
resources. Observation is scoped by the private intent, trusted host operators,
initial mount IDs and exact source/options. It is not attribution against a
competing privileged administrator. A timeout alone does not prove that a
kernel mount attempt stopped; inspect the host before manual recovery.
The volatile lock serializes live manager sessions; it does not prove that
an orphaned helper terminated after its manager was killed. Preserve and
resume the original attempt rather than starting another one for that target.

After both mounts are prepared, run the native/Docker peer exchange and the
management-runner verification in [the client runbook](CEPH_NATIVE_CHECK.md).
That separate step rechecks the original storage/provider chain and actual
client data, rather than accepting this mount receipt as storage readiness.

## Local verification limits

`python3 -B scripts/test_ceph_mount.py` exercises intent/resume, lost
acknowledgements, changed identities, numeric monitor parsing and fixed helper
arguments using simulated mount effects. Root Linux tests use real private
files/descriptors, a child reading the original descriptor, and the real mount
inventory. They perform no Ceph mount, key creation or host mount change.
