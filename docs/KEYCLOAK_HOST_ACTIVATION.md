# Activate one prepared Docker or native Keycloak member

`scripts/activate_keycloak_host.py` adds explicit first activation and original
attempt observation to the [Keycloak runtime packages](KEYCLOAK_HA.md). Use it
separately on each reviewed host. It creates one scoped Docker container or one
new systemd unit, starts it once, and observes TLS readiness on a listener owned
by the original member's dedicated cgroup. It does not prepare or migrate a
database, install software/users, import a realm, rotate credentials, change
firewalls, upgrade an existing member or certify a cluster's availability.

The full stack still needs fleet ordering, tested upgrades, native boot
enablement/recovery, database promotion, cache membership, node loss and restore
acceptance. This first-activation helper does not turn those requirements green.

## Review and stage the host

Copy either the [native plan](../examples/keycloak-host-native.json) or
[Docker plan](../examples/keycloak-host-docker.json) to ignored private storage.
Pin the exact rendered contract SHA-256, one member, SHA-256 of the stripped
`/etc/machine-id` value, and current boot UUID. Native service UID/GID must match
the pre-existing `native.user` account and `native.group`, including its primary
GID. The example uses a dedicated `keycloak` account; the helper never creates
an account or silently changes ownership to match a different identity.
Docker uses UID/GID 1000. The reviewed member IPv4 address must occur exactly
once on the executing host. Different package members must still run on their
separate reviewed physical failure domains.

Pin `bind_addresses.https` and `bind_addresses.management` separately. Each may
be `127.0.0.1` or the reviewed local member address; wildcard and unrelated
addresses are refused. A loopback management listener requires a local
monitoring/ingress arrangement. Peer discovery/transport still uses the
reviewed member address. Host activation materializes only these non-secret
bind environment settings in addition to the private password reference.

Stage the helper and its Python dependencies under a root-owned Linux path
without group/other writes or symlink components. Live operations require root;
local validation does not. Use the explicitly selected local runtime. The helper
does not use an inherited Docker context, remote engine, SSH destination or
default Kubernetes cluster.

Stage and hash these exact inputs under the package's `private_dir`:

| Input | Required protection |
|---|---|
| `database.env` | Root-owned, no group/other access; only the database password assignment |
| `tls/tls.key` | Root-owned, service GID, mode 0440 or 0640 |
| `tls/tls.crt` and `db-ca/ca.crt` | Root-owned, no group/other writes, readable by the service |
| `health-ca.crt` | Root-owned CA bundle for verified direct management HTTPS |

Directory ancestors must be root-owned and non-writable by group/others. The
service must be able to traverse the TLS/CA directories. Keep only `tls.crt`
and `tls.key` in `tls/`, and only `ca.crt` in `db-ca/`. No private bytes are
copied into the operation journal or printed in failure diagnostics.
For Docker, service readability applies inside each declared read-only mount;
the root-owned host ancestors can remain mode 0700. The rootful daemon performs
the bind mount. Native services still need traversal of their full host paths.

Docker's file is a raw `KC_DB_PASSWORD=` assignment. Native files support one
unquoted value without whitespace/quotes/backslashes, single-quoted values, or
double-quoted values using backslash escapes for backslash, quote, dollar and
backtick. Values must be printable ASCII, 1–1024 characters. Newlines, extra
assignments and `${...}` expression expansion are refused. These are explicit
supported input forms; the helper does not silently rewrite an existing
credential or generate a replacement password. Prepare the appropriate private
file for the selected runtime and keep its hash unchanged during resume.

For Docker, preload the reviewed digest and pin its local image ID, engine ID,
exact engine version and CLI binary SHA-256. Activation requires a local rootful
Linux engine, runc and built-in seccomp. Rootless/user-namespace remapping and
images declaring anonymous volumes are refused. Image inspection and container
environment read-back remain private in memory. The create request uses the
credential **file path**, not its value, and retains the package's host
networking, explicit peer address, non-root user, read-only root, capability
drop, resource limits and private TLS/CA binds. `--pull=never` refuses an image
that disappears after inspection. It adds bounded local logs,
operation labels and a recorded original container ID.
The policy fingerprint treats Docker's unset OOM flag and explicit `false` as
the same default and sorts the already-validated bind-mount observations.
`true` or a false-looking OOM value of another type is refused. This permits
the first start to materialize its default without adopting a policy change.
The engine must report cgroup v2 and support for memory, swap, CPU quota/period
and PID limits before a container is created. A missing capability is refused;
an engine-normalized unlimited swap value is not accepted as a no-swap limit.

The optional `docker.binary` and `docker.socket` fields must be supplied
together. They select canonical absolute Linux paths for the reviewed CLI and
local Unix socket. If both are omitted, the original `/usr/bin/docker` and
`/var/run/docker.sock` paths apply. The CLI is checked against `binary_sha256`;
the root-owned, non-public socket's resolved path, device and inode are bound
with the engine ID in the private journal. Remote URLs, traversal, inherited
Docker contexts/hosts and writable or foreign executable paths are refused.
Custom socket symlinks and symlink ancestors are refused before any CLI
request; only the root-controlled conventional `/var/run` to `/run` directory
alias is accepted. Resolving an arbitrary alias is not a substitute for
checking the original path's ownership and permissions.
This permits a separately staged engine without modifying a default daemon or
borrowing Docker Desktop's client configuration.

For native Linux, prepare the optimized Keycloak release and Java distribution
separately. Pin a complete relative SHA-256 inventory for each. Place
`native-files.sha256`, the generated `native-manifest.sha256` and
`java-files.sha256` in the private directory. Manifest files must be root-owned,
non-writable by group/others; Keycloak's two manifest files must be readable by
the service because its pre-start checks run under that identity. Inventories
use `64-lowercase-hex`, two spaces and a relative path. Symlinks, hardlinks,
duplicate/traversing paths, missing/extra immutable files and unreadable files
are refused. Keycloak's `data/` target is excluded from the immutable inventory;
it must initially be an empty root-owned directory. The systemd-managed
`/var/lib/platform-keycloak-<member>` directory must not already exist. This is
a fresh activation boundary, not a way to attach an existing member's state.

Pin the systemctl binary as well. The helper checks the prepared Keycloak
version with a clean Java environment. The created unit retains the generated
hardening and schema-validation arguments and adds the reviewed account,
`JAVA_HOME`, PATH and bind addresses. Unit creation is exclusive; existing units, drop-ins and changed
fragments are refused. Native activation does not enable boot-start symlinks.

Use `native.max_runtime_seconds: null` for a persistent member. An explicit
integer from 60 to 600 selects a bounded native canary: the generated unit sets
that `RuntimeMaxSec` and disables automatic restart. This is a reviewed lifetime
choice, not a periodic restart mechanism or an HA acceptance result. Native
runtime fixtures use this bound so an interrupted runner cannot leave a test
member running indefinitely.

## Validate, inspect, activate and resume

Default execution validates local plans and the exact rendered package without
constructing a host client or reading credential/CA inputs:

```bash
python3 scripts/activate_keycloak_host.py \
  --bundle-dir private/generated/keycloak-raw \
  --host-plan private/keycloak-host.json
```

Inspect the executing host and prepared inputs without creating a service:

```bash
python3 scripts/activate_keycloak_host.py \
  --bundle-dir private/generated/keycloak-raw \
  --host-plan private/keycloak-host.json --check-host
```

Explicit first activation needs a new private operation directory:

```bash
python3 scripts/activate_keycloak_host.py \
  --bundle-dir private/generated/keycloak-raw \
  --host-plan private/keycloak-host.json \
  --activate --output-dir private/keycloak-host-a-operation
```

If any response is lost, preserve the original inputs, source and journal:

```bash
python3 scripts/activate_keycloak_host.py \
  --bundle-dir private/generated/keycloak-raw \
  --host-plan private/keycloak-host.json \
  --resume --output-dir private/keycloak-host-a-operation
```

The journal binds host/boot, package/plan, source closure, before-write intent,
original container ID or unit inode, policy hash and first observed invocation.
Create, service-manager reload and start are never reissued after their intent
has been recorded. Resume can finish later unattempted steps, but only observes
an uncertain original step. Missing, replaced, edited or restarted original
members are refused; it does not delete or recreate them. A reboot changes the
scope and requires a separate recovery procedure, not a fresh activation that
adopts an existing service.

Readiness uses direct HTTPS to the reviewed management bind address with the canonical
hostname for SNI and certificate verification. It checks an UP readiness body
and uses the actual service/container cgroup, process descriptors and kernel
TCP listener inventory to avoid borrowing another service's response. Unified
cgroup v2 and root access to those kernel observations are required. No TLS
skip flag, inherited proxy, management token or redirect is used.

`--wait-seconds` bounds observation to 5–600 seconds; individual host commands
have 45-second bounds and 2 MiB output limits. Preserve the original journal
after timeout. A completed receipt describes that observation, not perpetual
monitoring or cluster/session/database availability.

## Evidence and remaining acceptance

`python3 scripts/test_keycloak_host.py` tests both activation paths with
simulated runtimes, exact Docker policy checks, drift, lost acknowledgments,
unknown creates/reloads/starts and later readiness. The separate opt-in
`KEYCLOAK_HOST_TEST_SYSTEMD=1` case has passed against a real local Linux
systemd manager. It creates one uniquely named disposable TLS service, verifies
create/reload/start/original-invocation resume, kernel listener ownership,
hostname rejection, manifest drift and non-root readability checks, then stops
and removes its own unit and files.

`python3 scripts/test_keycloak_native_runtime.py` is a separate opt-in real
distribution test. `KEYCLOAK_NATIVE_TEST_PREFLIGHT=1` selects archive/host
inspection without creating a service or contacting a database. It requires
the exact Keycloak/Java archives selected by `KEYCLOAK_TEST_ARTIFACTS` in
[the runtime guide](KEYCLOAK_HA.md). This preflight does not prove that a schema
or optimized artifact is ready for startup. It does not reserve activation
ports; its opt-in test rejects socket construction in the Python fixture.

`KEYCLOAK_NATIVE_TEST_RUNTIME=1` prepares a disposable schema and two baseline
Keycloak processes, then runs the actual host helper with a separately staged
optimized distribution and Java tree. It uses real private PKI, hashes each
immutable file, invokes real systemd create/reload/start, observes exact-bind
kernel listener ownership, checks OIDC discovery and resumes the original
invocation. The new member has a reviewed 240-second canary lifetime. The
fixture uses the existing `nobody` account for synthetic data; production
operators must select their dedicated reviewed service identity. No host or
workload prerequisite is mocked in this distribution test.

The remaining input variables are the PostgreSQL 17 runtime inputs in the
package guide. An optional `KEYCLOAK_NATIVE_TEST_ARGOCD=1` additionally runs the
Argo CD MFA and group allow/deny fixture with its pinned API/Argo CD/Valkey
inputs. The native member uses an explicit canonical HTTPS port and creates a
separate fixture MFA realm through the production bootstrap helper. Argo CD
authenticates against that activated member, checks OTP enrollment/login and
group allow/deny behavior, and verifies the same service invocation remains
healthy afterwards. This does not prove native-node issuer failover.
Reports retain source, native/Java manifest and activation-journal
commitments without credential values. Stops and cleanup apply only to the
unique fixture unit, state directory and files after checking their ownership.
The native distribution/application case has passed with a restrictive
`umask 077`, binding all 29 loaded first-party source files. Its staged public
directories receive explicit search permissions; credential files retain their
private modes. A separate real preflight with that same umask also passes
without socket construction or service creation.

The earlier mechanics fixture intentionally replaces workload prerequisites
and uses a tiny TLS server. The separate distribution fixture now verifies
actual optimized native startup, original-invocation resume and cleanup through
the production host helper. The separate Docker workload case below now also
verifies actual optimized-image startup. Cluster joins across physical nodes,
fleet availability, restart/reboot recovery and upgrades remain acceptance
requirements. These single-host fixtures do not establish those fleet properties.

`python3 scripts/test_keycloak_docker_runtime.py` adds a separate opt-in engine
prerequisite check. `KEYCLOAK_DOCKER_TEST_ENGINE=1` requires a disposable root
Linux amd64 runner with systemd `DelegateSubgroup` support, a static C compiler and
`DOCKER_ENGINE_TEST_ARTIFACTS` pointing to the exact Docker 29.9.3 static archive
whose SHA-256 is pinned in `scripts/test_docker_engine_fixture.py`. The archive
comes from the [official binary distribution](https://docs.docker.com/engine/install/binaries/)
over HTTPS; the fixture does not claim independent publisher-signature
verification, install packages, select a production engine version or download
anything during execution.

The engine has a unique private socket, data/exec roots and a finite delegated
systemd unit. It disables bridge creation, firewall changes and forwarding
changes; it uses the vfs driver and a private mount namespace. A supervisor subgroup keeps daemon processes
separate from the delegated container-controller subtree. The fixture enables
CPU, memory and PID controllers in that empty, individually owned subtree
before executing the daemon, so capability discovery observes the delegated
controls. A locally compiled
static probe is imported without pulling an image. Its actual kernel report
checks UID/GID, effective capabilities, no-new-privileges, seccomp and read-only
root; the original engine invocation and container cgroup are checked too.
The probe's actual cgroup files must contain its declared memory, zero-swap,
PID and CPU-quota limits; API configuration alone is insufficient.
Real socket checks cover the conventional runtime alias and refuse custom
socket/directory symlinks, writable aliases and a world-accessible socket.
Cleanup is limited to the original labelled container, unit, processes and
unmounted private directory. `DOCKER_ENGINE_TEST_REPORT` can select a new private
receipt containing aggregate results and artifact/source commitments.

This probe is not a Keycloak image, TLS/database/session test or HA acceptance.

The separate `KEYCLOAK_DOCKER_TEST_RUNTIME=1` case in the same entrypoint adds
actual prepared-image activation. It requires the baseline PostgreSQL/Keycloak
inputs described above, `DOCKER_ENGINE_TEST_ARTIFACTS`,
`KEYCLOAK_DOCKER_TEST_IMAGE` and `KEYCLOAK_DOCKER_TEST_REGISTRY`. The image input
directory contains the exact pinned amd64 Keycloak 26.4.7 `manifest.json`,
`config.json` and compressed layer blobs; the registry directory contains the
pinned Distribution 3.1.2 archive. The fixture verifies compressed hashes and
uncompressed diff IDs before loading the image. It performs no downloads.

Image preparation runs the reviewed optimized-build arguments in a new
network-isolated container, before introducing database or application
credentials. A temporary, process-owned loopback HTTP registry provides the
prepared image's immutable repository digest; it carries only this public
fixture image and is not a Harbor/TLS/supply-chain acceptance substitute.
The actual host helper uses that preloaded digest with `--pull=never`, a private
Unix socket, TLS/private database inputs and a root-private host staging path.
The case checks original container handles, kernel-owned TLS listeners,
readiness, OIDC discovery and read-only resume.

`KEYCLOAK_DOCKER_TEST_ARGOCD=1` additionally uses the real Argo CD inputs above
to create a fresh MFA realm through the activated container and verify
application login and group allow/deny behavior. Native and Docker application
tests share the same realm/client/application acceptance code. Reports retain
the baseline two-process framework's counts, with explicit Docker workload
result flags and image/activation commitments for the additional member.
Engine image storage uses an individually owned disk directory under
`/var/lib`, not an in-memory image store. Cleanup checks its identity, live
processes and mounts before removing it. Physical failure domains, HA database
promotion, node/session failover, upgrades, restore and complete fleet
deployment remain separate acceptance requirements.

The Docker workload and optional Argo CD case have passed locally with
Keycloak 26.4.7, Docker 29.9.3, Distribution 3.1.2 and Argo CD 3.5.0. That run
verified actual MFA enrollment/login, reader access with write denial,
administrator project creation, unmapped-user denial, original-container
resume and cleanup. Its receipt binds all 30 loaded first-party source files
and the exact input/image/activation artifacts; it keeps physical-node HA,
publisher-signature and full-stack acceptance false. Receipts live in ignored
private validation storage and must match the current source before reuse.

Relevant upstream interfaces are documented in
[Docker create/start](https://docs.docker.com/reference/cli/docker/container/create/),
[systemd service units](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)
and the versioned
[Keycloak cache options](https://github.com/keycloak/keycloak/blob/26.4.7/quarkus/config-api/src/main/java/org/keycloak/config/CachingOptions.java).
