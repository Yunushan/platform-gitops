# Prepared Docker and native Gerrit member activation

Status: optional per-host lifecycle tooling. Modeled failure/recovery tests pass
for both runtimes. A disposable real Docker engine accepts the actual generated
container-create arguments and passes original-object inspection. That test uses
an unstarted static probe image; **no real Gerrit/Ceph pair has been activated**.
No complete stack is qualified by this adapter.

`scripts/activate_gerrit_host.py` complements the
[Kubernetes activation adapter](GERRIT_ACTIVATION.md). Native activation installs
a systemd unit that launches Java directly. Docker creates a container through
an explicitly selected local Unix socket. Neither path installs a hidden cluster,
downloads images, initializes sites, mounts storage, creates service accounts,
upgrades daemons or imports repositories.

## Prepare privately

Use the [version 2 site profile](GERRIT_SITES.md) and separate
[initialization workflow](GERRIT_INITIALIZATION.md). Both hosts must independently
mount the actual reviewed encrypted kernel CephFS subtree. Keep local site state
separate, and provision TLS/SSH credentials and health-probe authentication
outside Git. Review separate configurations for native and container paths.

Render the runtime package into a new ignored private directory, for example:

```bash
python3 scripts/render_gerrit_sites.py render \
  --plan private/gerrit-sites-native.json --runtime raw \
  --output-dir private/generated/gerrit-sites-native
```

For Docker use `--runtime docker` and its reviewed plan. Stage the complete
generated package on each host under protected root ownership. Native startup
files must be at `/etc/gerrit`, matching their rendered paths. Docker can use
another canonical Linux path; it mounts startup files read-only under
`/opt/gerrit-startup`. Keep only the exact generated files in that directory.
They contain hashes/guard policy, not the private configuration/key contents.

Prepare a private host plan from the
[native example](../examples/gerrit-host-native.json) or
[Docker example](../examples/gerrit-host-docker.json). Replace `site_plan` with
the complete reviewed version 2 plan, then pin the selected member, actual
machine-ID SHA-256, current boot UUID, service UID/GID, private file hashes, and
the direct local health address/TLS hostname/CA file hash. Never publish this
plan or paste its credential-bearing files.

Native mode additionally pins the dedicated user/group, immutable Java home,
complete relative SHA-256 inventory and its digest, and `/usr/bin/systemctl`
hash. Inventory lines are `SHA256  relative/path`; duplicate/escaping paths,
missing/unreviewed files and symlinks are refused. Keep Java, its protected
inventory and the startup package separate from writable Gerrit data.

Docker pins the CLI path/hash, root-owned local Unix socket, original engine ID,
exact server version and preloaded image ID. The image repository digest must
match `site_plan.java_image`; anonymous image volumes are refused. It requires
UID/GID 1000, rootful `runc`, default seccomp, cgroup v2 limits and no user-namespace
remapping. The adapter never pulls an image.

The service identity must be able to traverse/read the required staged inputs.
Local site roots must be owned by that service UID, without group/other write
permission. The pinned `secure.config`, TLS keystore and private SSH host key
must be regular single-link files owned by that UID with no group/other
permissions. Ownership/permissions are checked, never repaired recursively.

## Explicit commands

Local mode checks the finite package against regenerated output, without host
access or journal writes:

```bash
python3 scripts/activate_gerrit_host.py \
  --bundle-dir /etc/gerrit --host-plan private/gerrit-host-native.json
```

Run host operations in an explicit root Linux session, with first-party Python
tooling and its local dependencies staged under protected root ownership:

```bash
python3 scripts/activate_gerrit_host.py \
  --bundle-dir /etc/gerrit --host-plan private/gerrit-host-native.json \
  --mode check-host
```

This checks the original host/boot, package, artifact hashes/stable API metadata,
private files, real CephFS scope and health configuration. Native also checks the
configured repository/HA paths directly; Docker repeats that check in its normal
startup guard before Java. Existing service/container names are refused. No
daemon starts in `check-host`.

Activate one prepared member with a **new**, private operation directory:

```bash
python3 scripts/activate_gerrit_host.py \
  --bundle-dir /etc/gerrit --host-plan private/gerrit-host-native.json \
  --mode activate --confirm ACTIVATE_PREPARED_GERRIT_MEMBER \
  --output-dir private/generated/gerrit-a-activation
```

For Docker substitute its bundle directory and Docker host plan. Run separately
on each reviewed host; remote pair placement/failure-domain verification remains
separate integration work. Native exclusively creates the original unit, reloads
systemd once and starts once. Docker exclusively creates and starts the original
container with explicit resource limits, reviewed binds, no capabilities and a
read-only root filesystem. Docker uses host networking for direct listener
ownership checks; network isolation/policy is separate required work. Both modes
disable automatic restart during this initial acceptance lifecycle.

Gerrit must bind the selected direct IPv4 address at HTTPS 8443 and SSH 29418.
Wildcard listener ownership is refused. Readiness requires both sockets in the
original dedicated cgroup, verified CA/hostname TLS, and healthcheck JSON with
every required probe reporting `result: passed`. Empty HTTP 200, disabled,
not-run, timeout and failed checks are insufficient. The Ed25519 key presented
to `ssh-keyscan` must match the pinned public file. This is host-key observation,
not a successful authenticated user SSH/Git operation.

## Resume without repeating uncertain writes

Retain the original journal, unit/container and data on failure. Use the same
unchanged plan, package, tooling, host boot and operation directory:

```bash
python3 scripts/activate_gerrit_host.py \
  --bundle-dir /etc/gerrit --host-plan private/gerrit-host-native.json \
  --mode resume --confirm ACTIVATE_PREPARED_GERRIT_MEMBER \
  --output-dir private/generated/gerrit-a-activation
```

Create/reload/start intentions are recorded before each request. Lost responses
authorize observation of that original operation, not another write. Missing
objects, changed unit inodes/content/drop-ins, container policy/mount changes,
changed host facts or restarted observed invocations stop for review. A failed
readiness wait never authorizes deletion, restart, init or automatic rollback.

Success reports `original_member_active`, `tls_health_observed` and
`ssh_host_key_observed`. `peer_forwarding_verified`, `git_writes_verified`,
`failover_verified`, `ha_verified` and `full_stack_verified` remain false. SSO,
real plugin compatibility, peer/index/cache forwarding, shared writes,
pair-wide orchestration, controlled recovery/upgrades, reboot activation,
router isolation and Ceph/Gerrit failure/recovery acceptance remain open.
A retained completed receipt alone does not prove current availability.

## Repeatable checks and evidence

The optional [pair coordinator](GERRIT_PAIR_COORDINATION.md) now adds two-host
preflight, pinned SSH delivery, retained reservations and PID-1-owned activation
and observation jobs. Coordinator/agent recovery is tested with modeled hosts;
real systemd verifies the job/cgroup lifecycle. Real SSH delivery of this agent
and the shared Ceph/Gerrit pair remain unverified.

```bash
PYTHONPATH=scripts python3 -B -m unittest scripts/test_gerrit_host.py
```

These tests model host responses for both lifecycle branches, lost acknowledgments,
uncertain missing objects, source/host drift, timeouts, invocation changes,
package substitution, container policy and health JSON. They do not run Gerrit.
The optional `GERRIT_HOST_TEST_SYSTEMD_SYNTAX=1` check also passes the generated
native unit through the actual systemd parser without installing or starting it.

The optional real test uses the existing
[disposable Docker fixture](KEYCLOAK_HOST_ACTIVATION.md), private engine/socket,
static probe image and original-engine/process/mount cleanup checks. It creates
an additional **unstarted** container with the actual CLI argument builder and
verifies inspection policy. The synthetic probe image is not a valid full
Gerrit activation input. Set `DOCKER_ENGINE_TEST_ARTIFACTS` to the reviewed Docker
archive cache before explicitly enabling it:

```bash
GERRIT_HOST_TEST_DOCKER_POLICY=1 PYTHONPATH=scripts \
  python3 -B -m unittest \
  test_gerrit_host.HostTests.test_actual_docker_cli_create_and_original_inspection_without_starting_gerrit
```

The real Docker 29.9.3 case passes and caught the now-tested nullable empty
environment inventory. It establishes CLI/policy behavior, not Gerrit startup
or HA. Full Ceph/Gerrit testing still awaits a suitable separate lab.
