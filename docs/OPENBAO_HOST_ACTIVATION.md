# Fresh native and Docker OpenBao host activation

`scripts/activate_openbao_host.py` activates one prepared native Linux or Docker
member from the exact [OpenBao package](OPENBAO_HA.md). It creates one systemd
unit or container, starts it once and observes the original process serving
verified TLS in the uninitialized, sealed state. Initialization, unseal, Raft
joining, recovery-key custody, authentication and restore remain external.
There is no bootstrap token, development mode or insecure TLS override.

Repeat the reviewed operation on the three independently prepared hosts. One
successful member does not establish a voting cluster or HA. After external
ceremony, use the [runtime verifier](OPENBAO_RUNTIME.md), failure acceptance and
[snapshot/recovery checks](OPENBAO_SNAPSHOTS.md).

## Prepare the pinned host and inputs

Render a `raw` or `docker` package into ignored private storage. Copy the
[native](../examples/openbao-host-native.json) or
[Docker](../examples/openbao-host-docker.json) plan into private storage.
Replace every placeholder and sample storage handle with current reviewed
observations. Pin the contract file SHA-256, member name, SHA-256 of stripped
`/etc/machine-id`, current boot UUID and dedicated non-root UID/GID. Native
named user/group must resolve to those exact identities; Docker uses 1000:1000.

Run on the selected root Linux host with root-controlled source and Python.
Root-owned, non-writable ancestors/files are checked. Stage the reviewed
first-party scripts under a root-controlled location; a user-writable home
checkout is refused for root execution. The selected member IP must already
be uniquely local, and ports 8200/8201 unused before activation. The helper
does not assign IPs or change routes/firewalls.

Prepare `<private_dir>/<member>/config.json` byte-for-byte equal to generated
`<member>.json`. Prepare a root-owned `tls/` containing only `tls.crt`,
`tls.key`, `ca.crt`, and pin each file's byte SHA-256. The private key must be
root-owned, service-group-owned and mode 0440 or 0640. Files must be readable
by the service; native ancestors must be searchable. Docker mount access is
checked from the mounted file/directory boundary. The CA must parse, and the
live HTTPS endpoint must pass chain and member-IP certificate verification.

Prepare separate, initially empty data/audit directories at the generated
paths: service UID/GID ownership, mode 0700 and root-controlled ancestors.
Pin their `st_dev`/`st_ino`; identical handles are refused. Freshness is checked
again before start. No storage is emptied, copied, chmodded or chowned by the
helper. Directory checks do not attest encryption, physical disks or replicas.

For native Linux, prepare the executable at its pinned path and
`<private_dir>/<member>/binary.sha256` equal to the generated checksum file.
The executable hash/reported version and pinned `/usr/bin/systemctl` are
checked. The generated unit enforces its service identity, empty capabilities,
strict filesystem protection, private temporary storage/umask, 2 GiB memory,
disabled swap and bounded tasks. `max_runtime_seconds: null` means persistent
operation; 60–600 selects a finite canary lifetime without automatic restart.

For Docker, pin the CLI path/hash, protected Unix socket, daemon ID, exact
engine version and preloaded image ID. The generated digest must appear in
`RepoDigests`. Rootful Linux Docker/runc, built-in seccomp, unified cgroups and
working memory/swap/CPU/PID controls are required. No default context,
`DOCKER_HOST`, image pull/build or registry credential is used. Anonymous image
volumes are refused. Review image provenance/signatures independently.

## Validate and activate

Local mode regenerates every artifact and validates scope without accessing
the host, private TLS contents or runtime:

```bash
python3 scripts/activate_openbao_host.py \
  --bundle-dir private/generated/openbao-raw \
  --host-plan private/openbao-host.json
```

Add `--check-host` to inspect prepared inputs without creating a service or
journal. Then activate into a new member-specific private directory:

```bash
python3 scripts/activate_openbao_host.py \
  --bundle-dir private/generated/openbao-raw \
  --host-plan private/openbao-host.json --activate \
  --output-dir private/operations/openbao-a --wait-seconds 120
```

Use the Docker bundle for Docker. Containers use the reviewed digest and
explicit `/usr/bin/bao server -config=...`, read-only root, no capabilities or
privilege escalation, resource limits, bounded logs, four exact prepared bind
mounts and one bounded temporary tmpfs. No bind source is created automatically.
Existing service/container names are refused.

This does not enable a systemd unit for boot, install Docker, mount storage,
provision accounts or update an existing member. Boot activation and upgrade
orchestration remain separate work. Docker's restart policy alone does not
establish recovery after node loss or make unseal inputs available.

## Resume by observing original handles

The private `openbao-host-proof.json` journals intent before each create,
daemon reload and start. It binds plan/source hashes, prepared inputs,
machine/boot, storage handles and original runtime identity. Resume exactly
that operation after an uncertain response:

```bash
python3 scripts/activate_openbao_host.py \
  --bundle-dir private/generated/openbao-raw \
  --host-plan private/openbao-host.json --resume \
  --output-dir private/operations/openbao-a --wait-seconds 120
```

Unknown writes are observed, never reissued. Missing/partial resources or
changed unit inode, container ID, runtime policy, invocation, prepared files,
storage, boot or executing source fail closed. A replacement is never adopted
or restarted. Docker environment, mounts and isolation are reread and bound.
The original cgroup must own the TLS listening socket, preventing an unrelated
responder from satisfying activation health.

Success ends at `awaiting-external-ceremony` with
`tls_serving_uninitialized_verified: true` and
`requires_external_ceremony: true`. HTTP 501, `initialized: false`,
`sealed: true` and the pinned version are required. An initialized endpoint
cannot satisfy this fresh-activation check. After external ceremony or reboot,
use the appropriate subsequent workflow; this is not general reconciliation.

Default CI tests uncertain writes, replacement refusal and policy/health
boundaries. Actual native systemd activation and original resume passed in an
isolated Linux namespace; the fixture adds only a namespace-placement unit
property. Actual Docker activation/resume also passed using the real pinned
binary in a locally constructed disposable image, registry and engine. That
test does not certify the publisher's OCI image. Both fixtures leave
initialization, unseal, HA and full-stack claims false and remove owned test
services/containers and data.
