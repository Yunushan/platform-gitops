# Explicit fresh Gerrit site initialization

The native Linux preparation helper has been exercised with Gerrit 3.13.10
on disposable local storage. See the [site runtime packages](GERRIT_SITES.md),
[HA routing](GERRIT_ROUTING.md) and [full expansion requirements](MULTI_RUNTIME_STACKS.md)
for deployment and topology requirements.

## Boundaries

`scripts/initialize_gerrit_sites.py` can prepare a **new pair** of native sites
using previously reviewed inputs and independently mounted, **empty** shared
storage. It never initializes an existing site or upgrades an existing Git store.
The default mode performs read-only preflight checks. Initialization requires
`--apply`, a literal confirmation, a Linux preparation host and a new private
draft-output directory.

The upstream [Gerrit init command](https://gerrit-review.googlesource.com/Documentation/pgm-init.html)
can upgrade existing state. To avoid that implicit operation, this helper:

- Refuses either target if it already exists, including empty directories.
- Requires shared storage to exist, have no symlinked ancestors and be empty.
- Reserves both new site directories before initializing shared Git.
- Creates an exclusive persistent initialization marker in shared storage.
- Runs init only on the first new primary, with `--batch`, `--no-auto-start`
  and `--skip-all-downloads`.
- Copies its newly initialized local state to the second prepared primary,
  retaining the second primary's separately reviewed configuration and inputs.
  It does not run init a second time against the shared Git store.
- Retains all new state after a failure. There is no automated deletion, retry,
  reset, migration or rollback over existing data.

The helper never starts the Gerrit daemon. Review the exact release's init
behavior and all supplied plugin initialization hooks before using it: disabling
automatic downloads does not isolate plugin code from the network or certify
its behavior. An approved compatible Java package and Git CLI must exist.

## Private input layout

Use the [site plan](../examples/gerrit-sites.json), with the desired *new* native
site paths, shared mount and approved artifact/configuration hashes. Under an
ignored private input directory, prepare one tree per site name:

```text
private/gerrit-bootstrap/
  gerrit-a/
    bin/gerrit.war
    plugins/high-availability.jar
    plugins/healthcheck.jar
    lib/global-refdb.jar
    etc/gerrit.config
    etc/high-availability.config
    etc/healthcheck.config  (required by schema version 2)
    etc/secure.config
    etc/keystore
    etc/ssh_host_ed25519_key
    etc/ssh_host_ed25519_key.pub
  gerrit-b/
    (same required filenames, with separately reviewed site configuration)
```

The source trees are never modified. Public/private SSH key files must represent
the same host identity on both primaries; the helper checks the pinned public
file digests and equality of the private key files, not cryptographic key-pair
correspondence. TLS keystores, secrets and host keys must be provisioned privately
before initialization. They are copied into new sites with private permissions,
not printed or included in the generated draft plan.

Set one nonempty `auth.registerEmailPrivateKey` in each site's private
`etc/secure.config`. For a new pair, supply the same signing key from your
secret store to both sites. Gerrit 3.13.10 generates this value from 26 random
bytes encoded in standard Base64 when it is absent. Supplying it in advance
keeps `init` from changing `secure.config` after creating shared repositories.
Preflight checks for a single configured value before any site or shared Git
writes. Keep the value in private bootstrap inputs and backups.

Preflight uses Git's configuration parser with includes disabled, and rejects
configuration includes. It requires a unique `gerrit.instanceId` matching each
site name and `gerrit.basePath` equal to the selected shared mount's `git`
subdirectory. Ceph-scoped pairs must supply the same explicit
`gerrit.serverId` on both sites. This is the identity used by their shared
NoteDb. Use the original ID when bringing a prepared site back into service.
The helper checks equality before touching the destination sites and verifies
that Gerrit initialization preserved the ID before preparing the second site.
[Gerrit HA plugin configuration](https://gerrit.googlesource.com/plugins/high-availability/+/master/src/main/resources/Documentation/config.md)

Private configuration cannot introduce includes, Gerrit target
overrides or an alternate authentication mode. Only configuration key names,
not private field values, are read for that override check.
Development become-any-account authentication is rejected. These
checks also bind Ceph-scoped plans to `main.sharedDirectory` equal to the
guarded mount in `etc/high-availability.config`. Both repository and HA target
settings must be unique and absolute; included configurations and private
repository-target overrides are refused. The copied sites are checked again
before initialization and before the draft is emitted.
These checks are **not** a full semantic audit: TLS, external databases, SSO, service
credentials, peer authentication, plugin hooks and every additional configured
module still require independent review. Any external SQL/database destinations
must be approved empty initialization targets; this helper cannot prove that
from a configuration string or protect an unrelated database from plugin code.

For schema version 2 the initializer additionally verifies the pinned private
healthcheck configuration with the [site health guard](GERRIT_SITES.md#version-2-health-configuration-guard).
Disabled/missing required probes, duplicate settings, includes, unbounded
timeouts and missing authentication-check inputs stop before creating targets
or shared initialization state, even if that file's SHA-256 matches the plan.
The copied and initialized sites are checked again, and initialization must
preserve the credential-bearing health configuration. Health policy validation
does not establish successful authentication or live plugin/HA behavior.

The version 2 preflight also inspects locked artifacts' embedded release/API
identities. Matching SHA-256 values cannot authorize a snapshot-API plugin or
a WAR from a different declared release. This is read-only parsing, with no
Java execution; refusal occurs before any target or shared initialization write.
See the [exact stable artifact policy](GERRIT_SITES.md#exact-stable-artifact-identity).

## Read-only preflight

Run on a preparation host that has the reviewed inputs and final storage paths:

```bash
python3 scripts/initialize_gerrit_sites.py \
  --plan private/gerrit-sites.json \
  --inputs private/gerrit-bootstrap
```

No targets or reports are created in this mode. Success means prerequisites
passed the implemented checks, not that Java, Gerrit or HA was validated.
Do not run preflight on the old production Git store hoping to migrate it.

## Explicit initialization

After reviewing inputs, target paths, external services, Java, plugin provenance
and storage mounts, a separate operator action is:

```bash
python3 scripts/initialize_gerrit_sites.py \
  --plan private/gerrit-sites.json \
  --inputs private/gerrit-bootstrap \
  --apply --confirm INITIALIZE_NEW_GERRIT_SITES \
  --output-dir private/generated/gerrit-initialization-draft
```

Run as the intended non-root Gerrit service owner with suitable permissions, not as a
generic cluster-wide installer. No user, package, filesystem mount or service is
provisioned by this command. It does not certify that an existing empty directory
is a healthy shared mount; independently verify mount identity and CephFS locking
and recovery behavior first.

Java output is captured with byte and time limits and is not echoed. Failures
may leave logs/configuration/private keys in the new site directories, so keep
those directories private. If initialization changes private bootstrap files,
the helper stops for review rather than silently propagating newly generated
secrets. The persistent marker is **not** evidence of a running process or a
successful init. Never delete it automatically to retry a failed operation.

On successful preparation, the helper writes a new private **draft** plan with
the resulting configuration digests. Originally approved WAR/plugin and public
SSH-key hashes must still match; altered artifacts are not accepted merely
because init exited zero. The source plan is unchanged. Review the draft and
resulting configuration before rendering runtime packages or starting daemons.

## Local verification

The disposable native Linux lab ran the preparation CLI as a non-root user
with the exact Gerrit 3.13.10 WAR and matching high-availability, healthcheck
and global-refdb artifacts. Preflight left the local site paths and shared
directory untouched. An apply with the signing key omitted also stopped before
writes. With complete inputs, the initializer created bare `All-Projects.git`
and `All-Users.git` repositories with their initial configuration refs and
prepared both local sites. It preserved the supplied private files and shared
server ID, and refused a second apply. Both prepared daemons concurrently served
the version endpoint, loaded the HA plugin and exposed the healthcheck endpoint.
This drill used a local directory shared by two processes on one host and a
schema version 1 plan. The CephFS mount guard and health probe success were
outside this drill.

## Remaining integration work

The [container preparation adapter](GERRIT_CONTAINER_PREPARATION.md) now renders
explicit Kubernetes Jobs and one-shot Docker services around this helper. The
Docker service and both the K3s and RKE2 Jobs passed disposable preparation drills with
Gerrit 3.13.10, using local mounts and claims. Complete configuration semantics,
secret-backend provisioning, shared-storage
verification, coordinated failover, SSO/CI/CD wiring and live acceptance remain
uncompleted. Native preparation does not imply that container mount paths or
configuration can be reused unchanged across runtimes. No production deployment,
upgrade, import or original-password migration is performed by this helper.
