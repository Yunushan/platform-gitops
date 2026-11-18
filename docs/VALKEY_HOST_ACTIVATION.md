# Native and Docker Valkey pair activation

`scripts/activate_valkey_host.py` activates one fresh, prepared Valkey server
and colocated Sentinel on its selected Linux host. Run it once for each of the
three members in the [Valkey package](VALKEY_HA.md). Kubernetes uses the separate
[installer](VALKEY_INSTALLATION.md). This helper does not install a binary,
download an image, provision storage, create an account or change firewall rules.

## Prepare each host

Render the original raw or Docker package, then copy the applicable example into
ignored private storage:

- [Native host plan](../examples/valkey-host-native.json)
- [Docker host plan](../examples/valkey-host-docker.json)

Replace all placeholder identity pins. Each plan binds the original rendered
contract, member name, machine-ID hash, current boot ID, numeric service UID/GID,
and device/inode of its two existing empty storage directories. Pin all seven
prepared TLS/authentication files by byte SHA-256. Declare only environment
references for the existing `probe` and `discovery` credentials; neither value
belongs in public Git, a host plan, shell history or a proof.

The member's declared IPv4 address must be uniquely local and both TLS ports
(6380 and 26380) must be unused during fresh preflight. Storage directories must
be separate, empty, service-owned, mode `0700`, and under trusted root-controlled
ancestors. They remain writable for AOF and Sentinel/configuration rewrites.
The helper never resizes or replaces storage or removes existing state.

Prepare this exact private inventory under `private_dir/member`:

```text
tls/ca.crt
tls/tls.crt
tls/tls.key
auth/server.acl
auth/sentinel.acl
auth/server-auth.conf
auth/sentinel-auth.conf
```

Use the [private materializer](VALKEY_HA.md) for the authentication files. Files
and mount directories must be root-owned and not writable by the service group.
Set the TLS key and all four authentication files to `0440` or `0640` with the
dedicated service GID; public certificates may be `0644`. The helper verifies
the complete mount inventory, hashes, ownership and service readability before
host writes. Prepare a node certificate valid for both server and client TLS;
the host health check uses the same node identity that participates in peer
connections. [Valkey documents TLS replication and Sentinel behavior](https://valkey.io/topics/tls/).

For native Linux, stage the pinned TLS-enabled executable, a non-root service
account and the rendered `member-binary.sha256` as `private_dir/member/binary.sha256`.
Pin the account names/IDs and `/usr/bin/systemctl` hash. The binary must report
the selected 8.1.11 release and match its package hash. Source code, interpreter
and artifact ancestors must be root-controlled; stage reviewed tooling outside
user-writable worktrees before invoking this root operation.

For Docker, preload the exact digest-selected Linux image without anonymous
volumes. Pin its image configuration ID, the Docker CLI path/hash, protected
local Unix socket, engine ID and server version. Only rootful Docker with runc,
builtin seccomp, cgroup v2 and enforced memory/swap/CPU/PID controllers is
accepted. The declared numeric service identity must be `1000:1000`.
Inherited Docker contexts, remote endpoints and untrusted socket aliases are
not used. Containers have host networking, a read-only root, dropped capabilities,
no added devices, fixed resource limits, bounded logs and three original bind
mounts. TLS/auth mounts are read-only; only the selected state mount is writable.
The approved image must provide `/bin/sh` and `/usr/local/bin/valkey-server`.
A fixed shell entrypoint sets `umask 077` and immediately executes Valkey, so
configuration rewrites keep mode `0600` rather than inheriting the engine's
default umask. The same startup rule is rendered for Kubernetes; native units
already set `UMask=0077`. Mutable configuration is never made more broadly
readable to accommodate a container default.
The helper never pulls an image or executes a command inside a container.

## Validate, preflight and activate

Default mode checks the package and host-plan structure locally. It does not
read private host files, credentials or service state:

```bash
python3 scripts/activate_valkey_host.py \
  --bundle-dir private/generated/valkey-raw \
  --host-plan private/valkey-host-native.json
```

On the selected host, supply both declared scoped credentials through protected
environment variables and run the read-only host preflight:

```bash
python3 scripts/activate_valkey_host.py \
  --bundle-dir private/generated/valkey-raw \
  --host-plan private/valkey-host-native.json --check-host
```

Activate the pair with a new private operation directory:

```bash
python3 scripts/activate_valkey_host.py \
  --bundle-dir private/generated/valkey-raw \
  --host-plan private/valkey-host-native.json \
  --activate --output-dir private/operations/cache-a --wait-seconds 120
```

For Docker, select the Docker package and host plan in the same commands.
For native services, the helper writes two original unit files, reloads systemd
as needed and starts them. It does not enable them at boot. A null
`native.max_runtime_seconds` uses the rendered persistent service policy; a finite
value is only for an explicitly bounded fixture/canary lifetime.

Each state directory is seeded once with its rendered configuration and an
operation-bound root-owned marker. The marker's original file handle is kept
in the private journal. Once started, Valkey and Sentinel may rewrite their
mutable configuration; the helper preserves that live state. Before start,
the seed must still match the package exactly.

The result proves only that both original processes are active, own their
kernel TLS listeners, match the release and answer scoped authenticated TLS
observations. Starting an isolated pair may succeed before its remote peers
are available. After all three pairs converge, use the separate
[runtime verifier](VALKEY_RUNTIME.md) to check replication links, complete
Sentinel membership/quorum and the optional expiring canary.

## Interrupted operation and recovery boundaries

Retain the original package, private inputs, host plan, operation directory and
executing tool versions. Resume the original operation with:

```bash
python3 scripts/activate_valkey_host.py \
  --bundle-dir private/generated/valkey-raw \
  --host-plan private/valkey-host-native.json \
  --resume --output-dir private/operations/cache-a --wait-seconds 120
```

The private `valkey-host-proof.json` records intents before every seed, create,
reload and start. A lost acknowledgement is observed through the original
marker/service/container handles; the action is never repeated automatically.
Absent or partial uncertain writes stop for operator investigation. The helper
does not reseed, adopt another service, recreate a missing container, restart a
stopped member, delete state or roll back an incomplete pair.

A completed resume performs host observations and journal updates without host
mutation (`read_only: true`). Replacement markers, service definitions, process
invocations, container policy, prepared files, credentials, boot or tool sources
are refused. A legitimate reboot, automatic restart, upgrade or disaster restore
requires a separate reviewed recovery procedure; a stale success receipt must
not be treated as current availability evidence.

All group-replication, Sentinel-quorum, application-compatibility, storage-
durability, physical-node, lossless-failover and full-stack HA flags remain
false. These are separate acceptance requirements. Three colocated pairs on one
test runner do not establish independent host failure domains.

## Tests

`python3 scripts/test_valkey_host.py` covers native/Docker pair custody, changed
inputs and policy, lost seed/create/reload/start acknowledgements, absent
uncertain writes, replacement/restart refusal and successful resume without
repeating writes. Default CI does not activate a host or access company services.

The explicit native fixture requires root Linux/systemd, an isolated network
namespace, OpenSSL, iproute2 and the pinned TLS test build selected by
`VALKEY_TEST_ARTIFACTS`. Set `VALKEY_TEST_HOST=1` and select
`HostTests.test_real_native_three_pairs_and_original_resume`. It stages tooling
under a root-controlled temporary directory, adds only test namespace placement
to generated units, activates three pairs, runs the production replicated
canary, resumes originals and refuses a changed invocation.

The Docker fixture uses `VALKEY_TEST_DOCKER_HOST=1` and
`HostTests.test_real_docker_three_pairs_and_original_resume`, with the same
Valkey build cache plus `DOCKER_ENGINE_TEST_ARTIFACTS` and
`VALKEY_TEST_REGISTRY_ARTIFACTS`. It builds a disposable image from the pinned
binary, host shell and recorded libraries, registers its immutable digest through a
private registry, and uses a separate finite engine. This exercises the helper
and kernel isolation; it does not establish publisher image provenance.
`VALKEY_TEST_REPORT` may select a new private aggregate receipt. Every fixture
must stop and remove only its original owned services/containers and temporary
data; it never borrows a production engine or host service.
