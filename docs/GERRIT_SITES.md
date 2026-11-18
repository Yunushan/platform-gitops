# Gerrit primary site runtime packages

Status: opt-in packaging and integrity checks for **already initialized** sites.
This is not yet a turnkey HA installer or an accepted complete stack. The
[stack expansion plan](MULTI_RUNTIME_STACKS.md) remains the completion contract.
Pair these artifacts with the [Gerrit entry-point adapter](GERRIT_ROUTING.md).

## What is implemented

`scripts/render_gerrit_sites.py` renders two distinct primary workloads for RKE2,
K3s, existing Kubernetes, Docker, or native Linux. All paths use the Gerrit
[daemon command](https://gerrit-review.googlesource.com/Documentation/pgm-daemon.html)
directly, with no demo-image entrypoint, `--init`, or automatic schema migration.
Startup checks pinned SHA-256 locks before Java runs. The locks cover:

- Gerrit WAR: `bin/gerrit.war`.
- HA plugin: `plugins/high-availability.jar`.
- Healthcheck plugin: `plugins/healthcheck.jar`.
- Global-refdb library: `lib/global-refdb.jar`.
- Each site's separately reviewed Gerrit and HA configuration files.
- Version 2 plans also lock the private `etc/healthcheck.config` on both sites.
- The public Ed25519 host-key file, with the same digest for both sites.

The global-refdb library requirement follows the
[upstream HA plugin instructions](https://gerrit.googlesource.com/plugins/high-availability/+/refs/heads/master/README.md).
The declared Gerrit version is a reviewed plan label; hashes establish file
integrity, **not** release identity, artifact provenance, plugin/API compatibility
or a correct HA configuration. Those reviews and live gates are still required.
Other configured plugins/modules also need explicit provenance review; the four
locked artifacts are a minimum, not a complete inventory or supply-chain audit.

## Version 2 health configuration guard

The public site example now selects schema version 2. This requires the
reviewed CephFS scope and a separate `etc/healthcheck.config` SHA-256 for each
primary. The initializer copies that private file and checks it before target
creation, after copying and after initialization. It refuses initialization
changes to this credential-bearing file. The same policy check runs before
Java at startup in all five runtime packages. Changing its hash changes the
immutable startup package identity. Version 1 plans retain their existing
behavior and report `healthcheck_configuration_guarded=false`; they do not
satisfy this guarded profile.

The [upstream healthcheck configuration](https://gerrit.googlesource.com/plugins/healthcheck/+/refs/heads/stable-3.13/src/main/resources/Documentation/config.md)
allows individual checks to be disabled. A successful HTTP status therefore
does not alone establish which probes ran. The guard requires one explicit true
`enabled` setting for each of `querychanges`, `jgit`, `projectslist`, `auth`,
`activeworkers`, `httpactiveworkers`, `deadlock`, `blockedthreads`, `changesindex`
and `gitspace`. It uses Git's actual parser with includes and ambient overrides
disabled, preserving duplicate settings for rejection. Probe timeouts must be
positive and at most 5000 milliseconds; absent values retain the documented
500-millisecond default. Git free-space thresholds must be 1 through 99 percent
(default 10). Authentication checks require a single nonempty username and
password in the private plugin configuration. No credential value is printed
or included in a generated public file. Container preparation references and
stages this file through each primary's existing private bootstrap Secret.

Before admitting either primary to the router, seed a dedicated healthcheck
project with an open canary review. For the tested 3.13.10 build, configure the
`[healthcheck "querychanges"]` section on both primaries with a query scoped
to that project and a `limit` of 1. Keep one matching review visible to the
healthcheck. The plugin treats `limit` as the minimum number of results for
this probe, so closing or hiding the last matching review can cause both
primaries to return HTTP 500 and drop out of routing.

Review these required probe names and configuration semantics against the exact
pinned plugin build before deployment or upgrade. This guard checks policy and
file integrity, not whether a credential authenticates, a plugin loads, its
HTTP response is correct, storage survives loss, or the pair is HA. Those live
checks remain mandatory. The guard is a startup check, not continuous monitoring
of later privileged configuration edits.

## Exact stable artifact identity

Version 2 also checks the embedded server release and plugin API identities
before initialization and before Java starts. The SHA-256 locks remain required:
metadata inspection supplements them, not replaces them. The guard reads the
WAR's nested `com/google/gerrit/common/Version` resource and requires the exact
stable version declared by the plan. All three HA/healthcheck/global-refdb JARs
must declare that same exact `Gerrit-ApiVersion`; snapshot, different-patch and
different-minor declarations are refused by this conservative profile. Required
plugin names, module declarations and entry-class presence are checked too.
This does not assert that every earlier patch API is incompatible; it requires
a reviewed matching build instead of inferring compatibility from a branch name.

The self-contained `gerrit_artifacts.py` helper uses bounded ZIP metadata and
entry reads. Ambiguous manifest headers, duplicate ZIP names, excessive central
directories, ZIP64 layouts, missing version resources and invalid class entries
are refused. Manifest continuation lines are supported. No Java class is loaded
or executed by this inspection. The helper is included in the startup hash lock
and in container preparation's closed first-party code package. File/manifest
values and private paths are not included in its error messages.

An inspection of immutable Gerrit CI stable-3.13 build 1 artifacts found
`Gerrit-ApiVersion: 3.13.11-SNAPSHOT` for all three components, despite the selected
server being 3.13.10. The actual stable WAR passed the new version check, and
all three actual snapshot-API JARs were rejected. Do not select a plugin solely
because its CI job or Git branch contains the desired minor version.

A separate bounded, non-root source build now produces matching 3.13.10 API
JARs from the canonical Gerrit tag `v3.13.10` at
`f9e70cda0327381db8d739f017ccb05dd90ee681`, using pinned Bazel 7.7.1 and Temurin
21.0.13+9 and the
[canonical in-tree build procedure](https://gerrit.googlesource.com/plugins/high-availability/+/0da3ec437ff169bdc370ef4c64df9d56ebd23637/src/main/resources/Documentation/build.md).
The component revisions are:

| Component | Canonical source revision |
|---|---|
| high-availability | `0da3ec437ff169bdc370ef4c64df9d56ebd23637` |
| healthcheck | `52ba0d27ff869e3a0d0ac09529b8e12b81b75c85` |
| global-refdb | `9d182b4c9c13cba0f579ee1a547dbe5c4ad3d2eb` |

The source licenses were checked as Apache 2.0. The final incremental build
completed in 49.276 seconds after explicit setup failures exposed missing tools,
DNS isolation and the non-root cache path. Its upstream Java sources remained
unchanged; the prescribed external-dependency file links to the HA plugin's
declaration. Exact distribution zip/unzip tools were exposed only inside a
private mount namespace, and the compiler could not access the Windows drive.
Independent inspection verified the original non-root/capability-free build
process, source revisions, stable WAR/API/entry identities, artifact hashes,
terminal unit/cgroup cleanup and absence of host zip/unzip installation. Build
sources/caches and the private audit are retained for further qualification.

### Verify plugin startup on a disposable site

The opt-in runtime fixture loads these exact artifacts in Gerrit 3.13.10
with Temurin 21.0.13+9. It checks that Gerrit lists both plugins as enabled
at the reviewed revisions, then sends the same unauthenticated health request
used by the router. It expects HTTP 500 when there is no matching review
and HTTP 200 with all ten required probes passing after it creates one.
[Gerrit plugin REST API](https://gerrit-review.googlesource.com/Documentation/rest-api-plugins.html)

```bash
sudo unshare --net -- env GERRIT_PLUGIN_TEST_RUNTIME=1 \
  GERRIT_REVIEW_TEST_ARTIFACTS="$REVIEWED_GERRIT_WAR_DIRECTORY" \
  GERRIT_REVIEW_TEST_JAVA_ARCHIVE="$REVIEWED_TEMURIN_ARCHIVE" \
  GERRIT_PLUGIN_TEST_ARTIFACTS="$REVIEWED_PLUGIN_DIRECTORY" \
  python3 -B scripts/test_gerrit_artifacts.py LivePluginTests
```

The fixture uses temporary local storage and synthetic credentials and
starts Gerrit as a non-root process.

### Verify change-index forwarding between two processes

Use the same fixture command with `GERRIT_PEER_INDEX_TEST=1` in place of
`GERRIT_PLUGIN_TEST_RUNTIME=1`. This runs two Gerrit 3.13.10 processes with
one temporary Git directory, one `gerrit.serverId`, different instance IDs,
and separate Lucene indexes. The fixture uses a dedicated synthetic Basic
credential on the loopback-only forwarding endpoints. It disables periodic
index synchronization, creates a new review through each process, and
requires both a successful HA plugin POST for that review and a matching
search result from the receiving process.

Live shared CephFS, the global-refdb enforcement path, routed writes during
failover, upgrades and restoration require the pair laboratory.

## Shared CephFS identity at preparation and startup

The public example enables `shared_storage`, with an exact reviewed Ceph FSID,
filesystem and remote subtree. For the requested Ceph architecture, retain
this scope and use a reviewed Java image that also contains Python 3.12 or
newer, Git CLI, `/bin/sh` and `sha256sum`. The storage checker and its bundled
bounded subprocess helper use the Python standard library. Native hosts need
the same tools separately.

Both fresh initialization and runtime startup check the actual process mount
namespace. The shared path must be one exact writable kernel CephFS mount,
with encrypted `ms_mode=secure`, matching cluster/filesystem/subtree and no
stacked mounts or nested replacement mounts. Modern `client@fsid.filesystem`
sources are supported. Legacy CSI monitor-address sources require explicit
`fsid` and filesystem mount options; missing identities are refused.
The guard accounts for a bind mount's filesystem root when checking its
effective remote subtree. It reads neither Ceph keys nor Secret objects.

The guarded launcher also parses the site's actual configuration using Git
with includes disabled, bounded output and no inherited Git configuration
overrides. `gerrit.basePath` must be exactly `<shared-mount>/git`, and
`main.sharedDirectory` in `etc/high-availability.config` must be exactly the
shared mount. This follows the HA plugin's
[shared-directory configuration](https://gerrit.review.googlesource.com/plugins/high-availability/).
Relative/default local paths, duplicate target settings, configuration includes,
private `gerrit.*` overrides and a redirected `git` directory are refused.
Configuration values and private parser diagnostics are not printed. This is
a check of the storage targets, not a full Gerrit or plugin configuration audit.

The guard program, bounded helper and policy have their own SHA-256 lock. Kubernetes packages
include them in the immutable startup ConfigMap, whose generated name changes
with the policy/program. Docker uses read-only startup binds without creating
missing host paths. Native units retain `RequiresMountsFor` and add a shared
mount-point condition. Initialization checks storage before creating target
directories, checks both configured storage targets in the original and copied
sites, then rechecks mount identity before reserving shared Git data.

Observe the actual shared mount and compare it with the original provider and
claim before filling this scope. A CSI-created subtree is not inferred from a
PVC name. Separate per-runtime plans may need different reviewed subtree
values. The [new Ceph storage classes](CEPH_STORAGE.md) explicitly request
encrypted transport and the provider FSID. Existing classes/claims are not
rewritten by this change; remounts or data migration require their own reviewed
maintenance and recovery plan.

Legacy site plans without `shared_storage` remain renderable for existing
workflows, and report `shared_storage_guarded=false`. They are not evidence
for the requested Ceph-backed stack. Version 1 `verify-site` checks file integrity;
version 2 additionally checks embedded artifact identities. Neither establishes
mounted storage, Ceph permissions, live plugin compatibility, failover or HA.
Likewise, the guard prevents startup on an incorrect mount but is not a
continuous filesystem/fencing monitor. Live shared-write, locking, loss,
recovery and application acceptance remain required.

## Prepare and inspect privately

1. Copy `examples/gerrit-sites.json` into ignored `private/` storage. Replace all
   placeholders. The public example intentionally fails validation until edited.
2. Provision two initialized sites on separate serving nodes. Keep indexes,
   caches and other site-local writable state separate; repositories and HA
   shared state need the independently provisioned shared filesystem.
3. Install the exact reviewed WAR/plugins/library under the locked filenames.
   Confirm build provenance, licensing, Gerrit/Java/plugin compatibility and
   upgrades against the selected stable release. No artifact is downloaded by
   these commands and no version is resolved from a floating channel.
4. Configure canonical HTTPS URL, one shared `gerrit.serverId`, unique
   instance IDs, peer forwarding,
   shared Git path, identity integration and TLS deliberately. Container sites
   use `/var/gerrit` and shared mount `/shared`; native sites use the explicit
   host paths from the plan. Use separate reviewed plans/configuration hashes
   when those configurations differ across runtimes. The generated service
   ports require HTTPS on 8443 and SSH on 29418.
5. Provision `etc/secure.config`, TLS `etc/keystore` and private/public SSH host
   keys outside Git. Both public host-key files must match the plan's digest.
   File equality alone does not verify that each private key corresponds to it;
   confirm the presented SSH fingerprint in live tests. The renderer does not
   read or copy secret values. TLS/identity/peer authentication are prerequisites,
   not configured implicitly by packaging.
6. Calculate approved file digests locally, then run the read-only check on a
   host with access to the initialized site (not against a remote API):

```bash
python3 scripts/render_gerrit_sites.py verify-site \
  --plan private/gerrit-sites.json \
  --site-name gerrit-a --site-root /srv/gerrit-a
```

Run it separately for `gerrit-b`. A successful result says `integrity_verified`,
with `compatibility_verified` and `ha_verified` still false. This check reads only
locked files, not secure.config, private SSH keys or keystore contents. It rejects
nonregular files, mismatched hashes and locked paths escaping the chosen site.

## Render, without applying

```bash
python3 scripts/render_gerrit_sites.py render \
  --plan private/gerrit-sites.json --runtime rke2 \
  --output-dir private/generated/gerrit-sites-rke2
```

Other runtime choices are `k3s`, `k8s`, `docker`, and `raw`. Output must be a new
directory under this repository's ignored `private/` tree. Existing files cannot
be replaced by rerunning the command. I/O failures retain partial new output for
inspection. Only aggregate status is printed; no company endpoint or secret is
echoed. No command in this renderer applies resources or starts/stops services.

### Kubernetes

Each primary gets its own one-replica Recreate Deployment and stable per-primary
ClusterIP service. Mandatory node anti-affinity separates the primary pods.
Distinct pre-existing site PVCs and one shared PVC are referenced, not created.
The shared claim must provide correctly configured RWX storage (for example,
the requested CephFS architecture). The renderer cannot establish filesystem
locking, mount health, replicas or failure-domain availability from a claim name.

Pre-populate the sites and artifacts deliberately. The pod runs as UID/GID 1000;
prepare ownership and private credentials accordingly. The digest-pinned runtime
image must provide a compatible Java, POSIX shell and GNU `sha256sum`. The
read-only root filesystem and private `/tmp` do not make an incompatible image
usable. The startup script checks locks, required private files and both
All-Projects/All-Users repository directories before launching Gerrit.

Content-addressed immutable ConfigMaps hold startup scripts and locks. A changed
lock creates a new name and pod-template reference rather than attempting to
modify an immutable ConfigMap. Upgrade coordination is still manual: stage
artifacts, back up, test compatibility and sequence primary restarts. Recreate
avoids two pods sharing one local site during rollout; it does not implement an
HA-aware upgrade or promotion controller. Historical ConfigMaps require explicit
operator lifecycle management.

TCP startup/readiness probes test the listening process, not SSO, Git, plugin
health or durability. The router's combined application checks and live
acceptance remain necessary. NetworkPolicies, redundant ingress, monitoring and
the shared foundation are separate uncompleted integration work.

### Docker and native Linux

Two separate per-primary Compose files are produced for **separate hosts**.
They expose the same 8443/29418 ports and must not be started on the same host.
Bind sources must already exist; Compose must not invent empty directories on
a missing mount. Mount the shared filesystem on both hosts before starting.
The same minimum runtime tools are required as for Kubernetes.

For native Linux, use the generated systemd units with an independently reviewed
compatible Java package, a `gerrit` user/group, and the configured directories.
Install `start.sh`, both site `.sha256` files and, for scoped plans,
`check_storage.py`, `bounded_subprocess.py`, `shared-storage.json` and
`shared-storage.sha256` under protected `/etc/gerrit/`, readable by the service
user. For version 2 also install the generated, hash-locked
`gerrit_artifacts.py` alongside those files. Units require the explicit
site/shared mount paths and restrict
writes to them plus a private temporary directory. They launch native Java, not
a hidden Docker backend. No packages, users, mounts or services are changed by
rendering. Separate-host placement and storage mount correctness still need
operator verification.

## Required remaining work

The optional [Kubernetes activation lifecycle](GERRIT_ACTIVATION.md) now adds
explicit target/node/claim identities, server admission review, per-node site
check Jobs and journaled fresh daemon creation/resume. Its real API admission
and modeled recovery checks pass; real Gerrit startup on Ceph and integrated
HA acceptance remain unverified. Docker/native activation remains separate.

The [per-host Docker/native activation lifecycle](GERRIT_HOST_ACTIVATION.md)
now adds explicit host/boot and private-file commitments, original systemd or
container ownership, uncertain-write resume and TLS/SSH readiness observations.
Its recovery tests model the service; its real Docker CLI create/inspect test
uses an unstarted synthetic image. Actual Gerrit startup on Ceph and the pair's
shared-write/peer/failover behavior remain unverified.

The separate [fresh native initialization helper](GERRIT_INITIALIZATION.md) now
provides explicit new-store preparation with read-only preflight. Preparation
runs as a separate operator action before daemon startup.
The [container preparation workloads](GERRIT_CONTAINER_PREPARATION.md) provide
the matching optional `site` claim subdirectory layout. Disposable labs
verified fresh-site initialization on local bind mounts and local claims.
The K3s lab also started the rendered pair on separate workers with local
site claims and shared NFSv4 Git storage. It verified the exact review refs
through the surviving Pod after stopping the first. See the
[handoff drill](GERRIT_CONTAINER_PREPARATION.md#handoff-and-remaining-acceptance).
Private secret provisioning, full plugin/API and
configuration semantic verification, shared-storage acceptance, SSO and peer
integration, coordinated upgrade/rollback, CI event wiring and live HA acceptance
are **not yet implemented** by these packages. Do not accept two Running pods as
proof of a working Gerrit cluster or of any full Gerrit/Zuul/Tekton/Jenkins stack.
