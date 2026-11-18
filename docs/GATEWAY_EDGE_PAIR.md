# Coordinated entry-host maintenance and input distribution

`scripts/manage_gateway_edge_pair.py` coordinates the Docker/native Linux
[external entry hosts](GATEWAY_EDGE.md). It reserves both physical hosts before
starting host B, verifies that host and the VIP routes, then advances to A.
This adds maintenance ordering and recovery; it does not prove VRRP single
ownership, network-partition safety, failover or full-stack availability.
The [two-guest runtime probe](GATEWAY_EDGE_PAIR_RUNTIME.md) additionally accepts
ordinary real SSH staging, durable host jobs, configuration update and rollback
against its source-bound laboratory snapshot. Its additional recovery profile
accepts coordinator loss with the original worker still live, plus completed
idempotent resumes. A subsequent terminated-worker profile accepts verified
process/unit/cgroup termination and exactly one explicit replacement job in
the disposable guests. Physical/fleet acceptance remains a separate gate.

## Prepare private inputs

Copy `examples/gateway-edge-pair.json` into ignored private storage. Replace all
example addresses, fingerprints and paths. Both peer names/addresses must match
the reviewed entry contract. Set `expected_current` to `absent` for an initial
installation, or each peer's exact current contract SHA-256 for an upgrade.

Each peer needs a distinct SHA-256 of its stripped `/etc/machine-id` bytes.
Obtain that value through an already trusted administrative connection:

```bash
python3 -c 'import hashlib; from pathlib import Path; print(hashlib.sha256(Path("/etc/machine-id").read_bytes().strip()).hexdigest())'
```

This prints a fingerprint, not the raw machine ID. Never substitute guessed
values. Host-key validation and this fingerprint are independent checks against
reused addresses, copied host keys or accidentally targeting the same machine.

The two `identity_env` values name environment variables containing paths to
approved noninteractive root SSH identities. `known_hosts_env` names variables
containing paths to independently verified peer host-key files. These files
must contain only one to four exact entries for that numeric address/port:
`192.0.2.10` for port 22, or `[192.0.2.10]:2222` for a custom port. Wildcards,
hashed host entries, other hosts and certificate-authority patterns are refused.
Ed25519, RSA and the declared NIST ECDSA key types are supported. Authentication
is public-key-only; agent forwarding, password prompts, multiplexing, proxies
and caller SSH configuration are disabled. Keep all identity files outside Git.

Pin the local OpenSSH executable with `ssh_binary_sha256`. The file supplied by
`--ssh-binary` must match. The coordinator freezes approved known-host entries
into private temporary files for the operation.

Print the current first-party tool inventory without reading SSH credentials:

```bash
python3 scripts/manage_gateway_edge_pair.py --toolset-info
```

Copy its `toolset_sha256` into the private pair plan. The edge contract digest
must match the private entry package. Default mode verifies those local inputs
and does not construct SSH connections, read identities or probe the VIP:

```bash
python3 scripts/manage_gateway_edge_pair.py \
  --plan private/gateway-entry-pair.json \
  --bundle-dir private/rendered-gateway-entry \
  --gateway-bundle-dir private/rendered-cilium-gateway
```

## Distribute the reviewed inputs

The optional `--distribute-inputs` flag copies the selected inputs through the
same pinned, noninteractive SSH connection before either host is reserved or
updated. Supply the reviewed local CA with `--ca-file`; native Linux also needs
`--artifacts-dir` containing `haproxy`, `keepalived` and, when declared, the
Keepalived build metadata. Only regenerated entry/Gateway packages, the exact
reviewed Python dependency inventory, that CA and fingerprint-checked native
artifacts are sent. Unrelated local files and SSH private keys are excluded.

Choose separate immutable destination paths for `remote_root`, `paths.bundle`,
`paths.gateway`, `paths.ca` and native `paths.artifacts`. Automatic staging
permits paths below `/opt`, `/srv`, `/var/lib` or `/run`, with at least two path
components below the filesystem root; it excludes the host manager's own
`/var/lib/platform-gateway-edge` state. `/run` is volatile and is intended for
temporary validation rather than persistent recovery material. Destinations
cannot overlap or contain symlink components. Every existing ancestor must be
root-owned and unwritable by other users. For upgrades, select new paths for
changed inputs; the transfer never replaces a conflicting destination.

The standard-library-only bootstrap verifies the physical-host fingerprint
before creating transfer state. It bounds the request to 128 files, 64 MiB per
file and 160 MiB in total, rejects traversal and checks every byte against the
private request's SHA-256 inventory. Inputs are received into root-private
sibling caches under per-destination OS locks. Nothing is published until the
entire transfer passes its hashes and framing checks. Each directory is then
published atomically; publication across several roots is recoverable rather
than one filesystem transaction. No service is started by staging.

Interrupted transfers and lost acknowledgments reuse the exact input scope on
`--resume --distribute-inputs`. Completed destinations must match all expected
files, hashes and modes; changed data, extra files, symlinks and hardlinks are
refused. Partial caches retain their manifest and can be filled again. The
coordinator journal binds the per-host staging fingerprints, so changing local
artifacts, remote paths or distribution mode cannot silently alter a retry.
Do not delete the private journal or cache claims to bypass a conflict. Caches
and lock files are retained for recovery; pruning them is an operator action.
As with reservations, root administrators can override files; the locks do not
fence privileged intervention.

Docker images, OS dependencies and the pinned PyYAML renderer still need to be
installed separately. Staging does not configure the CNI/Gateway or establish
that the complete application stack is healthy.

### Manual staging alternative

For an already provisioned fleet, manual root staging remains available. Write
a new isolated tool directory:

```bash
python3 scripts/manage_gateway_edge_pair.py \
  --plan private/gateway-entry-pair.json \
  --bundle-dir private/rendered-gateway-entry \
  --gateway-bundle-dir private/rendered-cilium-gateway \
  --prepare-tools --output-dir private/entry-pair-tools
```

Install its `scripts/` directory under each
peer's `remote_root`. Do not copy the whole repository there: the RPC bootstrap
requires an exact source inventory, without unrelated modules or bytecode caches.
Every source file is checked against the reviewed manifest before it is imported.
The directory, files and every parent must be root-owned and non-writable to
other users. The bootstrap uses the peer's `/usr/bin/python3` in isolated mode.

Stage the matching entry package, Gateway package and CA at each declared remote
path, with the same root ownership requirements. For native Linux, also stage
the pinned executable artifacts and optional Keepalived build metadata, setting
`paths.artifacts` to their directory. Docker may leave it null, but its exact
image digests must already be loaded on each local rootful Linux engine.
Python, the project's pinned PyYAML 6.0.3 renderer, and the runtime dependencies required by the host helper must be
present. Pair jobs additionally require an active Linux systemd host manager
at version 240 or later.
Local and remote YAML versions are checked before orchestration so a different
serializer cannot silently redefine a reviewed package.

First use the Gateway verifier on its management target. Root-stage only
reviewed inputs, or use the distribution flag; the pair coordinator then drives
the guarded host installation lifecycle.

## Apply, observe and resume

Apply the reviewed pair operation:

```bash
python3 scripts/manage_gateway_edge_pair.py \
  --plan private/gateway-entry-pair.json \
  --bundle-dir private/rendered-gateway-entry \
  --gateway-bundle-dir private/rendered-cilium-gateway \
  --apply \
  --distribute-inputs \
  --ssh-binary /usr/bin/ssh \
  --ca-file private/entry-ca.crt \
  --output-dir private/entry-pair-operation-001
```

For native Linux, add `--artifacts-dir private/entry-native-artifacts`. Omit
`--distribute-inputs` when inputs were staged manually. Use the same choice for
every resume of this operation. A cancellation needs no new upload: use
`--cancel` without `--distribute-inputs` and retain the original journal.

The new output directory must be under `private/`. Its `pair-proof.json` records
the exact plan/target, operation ID, remote job IDs and observed phases. Keep it
with the private plan and matching generator/tool revision in off-platform
recovery storage. The observer CA is fingerprint-checked before server work.
After each host completes, the coordinator verifies every declared hostname
through the actual VIP using that CA and SNI. HTTP routes must return their
expected readiness status; gRPC routes establish TLS reachability only. These
maintenance probes require all declared routes, even if the VIP advertisement
policy permits a lower `minimum_ready_routes` threshold.

A terminal receipt is observed as `finishing` until PID 1 and the recorded
process identities confirm the unit has drained its descendants. The
coordinator keeps polling that handle; neither another host job nor reservation
release is allowed while the previous job's cgroup remains active.

Both peers receive durable reservations before any host job starts. A private
receipt marker also makes older managers with the previous exact state schema
refuse mutation during maintenance. The current host manager accepts writes
only with the matching pair token and target. Root administrators can override
local files; this protocol is not fencing against privileged intervention.

Remote jobs use named transient systemd services with a private log, a 15-minute
runtime limit, a 512 MiB memory limit and a bounded process count. This keeps
job ownership with PID 1 instead of the SSH login session. The agent records
Linux boot ID, PID and process start ticks, followed by terminal results. An
observation timeout preserves that handle and both reservations. It does not
kill or restart the job or advance to the other host.

Rerun the original command with `--resume` and the same output directory instead
of `--apply`. A running job is polled by its existing ID. A completed job is
verified again without another host restart. A failed job gets at most one
explicit retry per resume invocation; the host helper inspects its partial
receipt and resumes the exact target. A missing launch acknowledgment reuses
the original job ID, and agent launch is idempotent. Unknown process outcomes
are polled without a new start; after the bounded wait, the coordinator stops
again. A worker killed before writing a terminal result needs the explicit
terminated-job recovery described below. Do not erase its reservation to force
an automatic retry.

### Recover a terminated worker

If the same recorded job remains `unverified`, resume the original operation
with `--recover-terminated-jobs` as well as `--resume`. Keep the original plan,
bundles, CA, output directory and distribution choice. The recovery flag is
refused for a new apply or rollback. It never stops a worker or expires a lease.

The root agent holds the launch, job and host-operation locks, verifies the
exact job request and matching durable reservation/host marker, and checks both
recorded process identities (PID, Linux start ticks and boot ID). A live
launcher, worker or held job lock prevents recovery. Missing or damaged process
identities and host receipts require restoration rather than a guessed retry.

Only then does it query PID 1 for the exact recorded unit. The unit must be
absent or inactive/failed, have zero main/control PIDs and no restart policy.
Loaded units must be transient. When a control-group path remains, it must be
the exact job group and cgroup v2 must report `populated 0`, including descendant
processes. Active, activating or stopping units, populated groups, incomplete
properties and observation failures leave the original handle unresolved. A
timeout or missing PID alone never establishes termination.

Verified termination writes a durable failed-job receipt with the evidence;
it does not mark the host healthy, change its release state or release either
reservation. The coordinator can then spend its existing single retry allowance
for that explicit resume. The host helper must inspect/resume any partial
activation and verify readiness again. A late worker sees the terminal receipt
under its job lock and refuses execution. Healthy completed jobs retain their
existing receipt and do not restart.

Reservations have no automatic expiry. If staging, connectivity or a host fails,
the other host is left untouched and private evidence is retained. A reservation
can be cancelled only by its matching operation before any job exists. The
explicit `--cancel` mode uses the original private plan and journal and changes
only reservations; it cannot cancel or stop a started job. The
coordinator does not silently transfer ownership or assume an SSH failure means
the remote command failed. Recover both hosts and the recorded jobs before
starting an unrelated pair operation.

For rollback, supply the reviewed previous entry/Gateway bundles, a new private
pair plan with the current per-host digests, and `--rollback` in a new operation
directory. Each host still accepts only its retained previous release. There is
no automatic rollback, broad service teardown or deletion of retained releases.
CA rotations need overlapping trust during maintenance when an old active VIP
owner still serves an earlier certificate.

## Evidence and remaining work

The suite simulates two hosts and tests reservation-before-write, B-before-A,
VIP probe failure, lost acknowledgments, bounded observation, failed jobs and
same-handle resume. Agent fixtures exercise durable receipts, exclusion of
direct host writes, process uncertainty and idempotent launch. Optional tests
exercise actual OpenSSH `-G` configuration parsing and the pinned Python
bootstrap through a local shell, including source-tamper rejection. The latter
uses a trusted temporary `/run` directory and performs identity reads only.
Separate root Linux staging fixtures execute the real isolated shell bootstrap,
publish and compare temporary inputs, interrupt transfer/publication and verify
that retries preserve existing data. They reject wrong machine fingerprints,
conflicting files, symlinks, hardlinks, bad modes and corrupted/trailing data.
Default fixtures do not connect to company peers, start host services or
advertise a VIP.

On a disposable root Linux system with an active systemd host manager, opt in to
the local transient-worker fixture:

```bash
GATEWAY_PAIR_TEST_SYSTEMD_LIVE=1 \
  python3 -B scripts/test_gateway_edge_systemd_job.py
```

It starts randomly named temporary worker units using the production launcher
and copied tools under `/run`, kills the launcher session, checks that the
worker survives in PID 1's separate cgroup, and tests normal and killed-worker
exit. Real kernel process identities, cgroup descendant cleanup, private log
modes, OS locks and atomic recovery receipts are checked. The fixture stops
only its own named transient unit and removes its temporary files. It does not
install or restart any entry/application service, contact company hosts or
advertise a VIP. This is local process-lifecycle evidence; it does not simulate
an SSH fleet, hypervisor failure or a network partition.

Actual SSH fleet rollout, image execution,
deliberate node/path/VRRP partition drills,
single-owner verification, restore and complete stack acceptance remain open.
`pair_services_ready` and `vip_reachable` describe observed maintenance checks;
`vip_failover_verified`, `application_health_verified`, `ha_verified` and
`full_stack_verified` remain false.

For the detached job mechanism, see the upstream
[systemd-run service behavior](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html).
