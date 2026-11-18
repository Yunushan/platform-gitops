# Actual Harbor service integration fixture

**Validation update:** the shared audit-job fixture now requests an explicit
`pull` event type so its dry run must execute SQL. The stronger
[jobservice continuity case](HARBOR_JOBSERVICE_REPLICA_RUNTIME.md#observed-database-continuity-failure)
failed on the worker's first database query after handover (`unexpected EOF`).
Earlier dry-run receipts used an empty operation filter and do not prove this
SQL-backed job-continuity requirement. Full acceptance remains pending; keep the
original process, TLS, quorum and route-fencing checks in place.

This optional local test runs pinned Harbor **2.15.1 core, jobservice and
registry-controller executables**, the real registry daemons, a three-member
TLS PostgreSQL fixture and the TLS Valkey gateway fixture. It is component
integration evidence. **It does not qualify a full Harbor HA deployment or
any complete stack/runtime combination.** Existing deployment profiles are
unchanged.

The strengthened local run passed in **80.064 seconds** (one runtime test,
zero skips). The supervisor separately verified source stability and original
process/cgroup cleanup. An earlier stricter run correctly failed on a missing
worker `HSCAN` permission; that failed receipt is retained and was not accepted.

## What it checks

- Core applies the original PostgreSQL migration SQL from the checksum-pinned
  release source archive. The resulting schema version must equal the latest
  migration, with `dirty=false`; no schema stub, skipped migration or `UTTEST`
  substitution is used.
- Core and jobservice use PostgreSQL with `sslmode=verify-full` and an explicit
  ephemeral CA. Live application database connections must have TLS enabled.
  PostgreSQL runs with synchronous `remote_apply` replication; its original
  three-member checks and read-only/configuration guards still run.
- Core serves authenticated project creation/read requests backed by real SQL
  rows. Jobservice loads configuration from actual core and runs the production
  `PURGE_AUDIT_LOG` job with `dry_run=true`, a nonzero retention period, real
  database managers, a successful terminal state and the dry-run log marker.
- Fresh projects and separate jobs must succeed before and after deliberate
  cache primary/co-located Sentinel loss. Original service PIDs/start times
  must remain unchanged. Existing project data must remain readable.
- Services run as the unprivileged fixture account with zero effective
  capabilities. Requests require verified HTTPS and the original leaf identity.
  An unrelated CA, changed leaf, plaintext transport, missing authorization and
  wrong credentials are refused. Internal API authorization uses distinct
  ephemeral core and jobservice secrets.
- The composed registry fixture still checks cross-replica OCI upload/digest
  round trips, registry process loss, cache promotion, scoped registry ACLs and
  filesystem fallback during cache gateway checker loss.
- Service identities are checked during both workload phases. Cache
  `NOPERM` errors observed in those service logs fail acceptance, including errors ignored
  by a background startup/reaper path. Explicit command grants include the
  source-required queue migration sorted-set reads and worker `HSCAN`.
- Original process/data cleanup and absence of generated credentials from
  fixture/database logs are checked. A bounded foreground supervisor retains
  source hashes, the original invocation identity and an explicit test exit.

Core session IDs lack an application prefix. The two temporary service cache
users therefore have all-key access within this isolated cache, with a finite
command allowlist. They have no cache administration, ACL, configuration,
shutdown or flush permissions. This is **not a production tenant-isolation
policy**. The separate registry user retains its reviewed key restrictions.

## Run explicitly

Prepare the exact reviewed binaries/build receipts, the original Harbor source
archive, the pinned TLS Valkey and HAProxy builds, and the PostgreSQL 18 fixture
tools/libraries. All paths below refer to ignored private artifacts:

```text
HARBOR_SERVICE_TEST_RUNTIME=1
HARBOR_SERVICE_TEST_ARTIFACTS=<CORE_JOB_SERVICE_BUILD_DIRECTORY>
HARBOR_REGISTRYCTL_TEST_ARTIFACTS=<REGISTRY_CONTROLLER_BUILD_DIRECTORY>
HARBOR_SERVICE_TEST_SOURCE_ARCHIVE=<PINNED_HARBOR_SOURCE_ARCHIVE>
HARBOR_REGISTRY_TEST_ARTIFACTS=<REGISTRY_BUILD_DIRECTORY>
VALKEY_TEST_RUNTIME=1
VALKEY_TEST_ARTIFACTS=<PINNED_TLS_VALKEY_BUILD_DIRECTORY>
VALKEY_GATEWAY_HAPROXY=<PINNED_HAPROXY_EXECUTABLE>
POSTGRES_RUNTIME_TEST_BIN=<POSTGRESQL_18_BIN_DIRECTORY>
POSTGRES_RUNTIME_TEST_LIBS=<POSTGRESQL_RUNTIME_LIBRARY_DIRECTORY>
VALKEY_TEST_REPORT=<NEW_PRIVATE_REPORT_PATH>
```

Use an owned **fresh network namespace** on a disposable root Linux host,
with a bounded cgroup and a foreground supervisor that stays attached through
cleanup. Do not reuse a company kubeconfig, external database or running local
database. The PostgreSQL fixture refuses an occupied primary port.

```bash
PYTHONPATH=scripts python3 -B -m unittest \
  test_valkey_gateway.GatewayTests.test_real_harbor_services_execute_jobs_after_cache_promotion
```

Ordinary validation runs the artifact/destination contract tests and skips
real service execution unless explicitly enabled. The fixture validates exact
executable hashes and build provenance before staging. Migration SQL comes
only from bounded regular members of the checksum-pinned original archive.
Credentials, keys, SQL data, service configuration, logs and receipts stay
private. The source/build inputs are documented in
[Harbor service builds](HARBOR_SERVICE_BUILD.md) and the separate
[registry component test](HARBOR_REGISTRY_RUNTIME.md).

## Optional planned database promotion and client reconnection

A separate local runtime case passed in **84.507 seconds** (one test, zero
skips), with source stability and original process/cgroup cleanup verified by
the supervisor. It runs the actual core, jobservice, registry-controller and
registry fixtures, including cache promotion, then promotes PostgreSQL and
requires a third fresh project and real dry-run job through the same original
application processes. Live application database connections still require TLS.

Enable these additional explicit flags with the same artifact inputs:

```text
HARBOR_DATABASE_PROMOTION_TEST_RUNTIME=1
POSTGRES_PROMOTION_TEST_RUNTIME=1
```

Inside the owned isolated namespace, run:

```bash
PYTHONPATH=scripts python3 -B -m unittest \
  test_valkey_gateway.GatewayTests.test_real_harbor_services_reconnect_after_planned_database_promotion
```

The database fixture uses three separate loopback listeners on port 5432.
It cleanly stops the exact original primary and proves that the original process
is gone. The candidate must replay the final shutdown checkpoint before it can
be promoted. Its original postmaster stays running, the timeline advances,
and the old primary rejoins as a standby from its existing data directory.
The same strict verifier must observe all three members, two direct verified-TLS
replication connections, the same database identity, the new timeline,
`remote_apply` and the required synchronous standby quorum. Real checkpoint and
restartpoint operations make the observed control-file timeline current; the
verifier is not weakened to accept stale metadata.

Before promotion, the owned local TCP backend is placed in maintenance, its
existing sessions are closed and SQL must show zero old application connections.
It stays fenced while the old primary rejoins as a standby. Only after strict
verification does the test select the new writer and reopen the unchanged
local TCP endpoint. This owned HAProxy process passes PostgreSQL TLS through;
it does not terminate TLS, select a primary or emulate an operator. A private
mode-0600 Unix socket protects administrative route control. The fixture client
exposes only its reviewed read/switch commands. State read-back must identify the original backend/server and exact
new address/port. API work is not retried by the fixture as a new mutation;
application connection handling must reconnect without restarting the services.

The original non-promotion fixture remains separately selectable. Its negative
configuration checks run after the service callback stops the applications.
The standalone promotion primitive also passed with its actual PostgreSQL tools.
Failed bootstrap/timeline-check receipts are retained privately, alongside the
successful runs.

This is a **controlled clean-shutdown promotion and application-reconnection
component test**. It does not establish crash/asymmetric-partition safety,
automatic fencing, watchdog/DCS behavior, CNPG/Patroni controller integration,
service routing HA, physical host failure or a full deployment. Those gates stay
false; the generic full database-promotion qualification is not inferred from
this local test.

The separate [bearer-token OCI workflow case](HARBOR_JWT_OCI_RUNTIME.md) now
passes actual core token issuance, project-guest permissions, image push/pull,
artifact metadata SQL and earlier-token continuity across both promotions.
It also covers core's source-required registry-state cache URL over TLS.
This is native HTTP protocol/application evidence; image-client deployment,
full SSO/MFA, replica/storage HA and supply-chain admission remain separate gates.

The [two-core replica-loss component](HARBOR_CORE_REPLICA_RUNTIME.md) additionally
passes verified HTTPS health routing, direct token/image reads on both peers,
exact-process SIGKILL and fresh work through the original survivor. Its combined
case found and corrected stale routing to the old database after it rejoined
as a standby; old sessions are now fenced before handover.

The separately pinned [pool-health candidate](HARBOR_DB_POOL_HEALTH.md) now
passes six real SQL-backed jobs across both peer losses and both promotions.
Its patch/build/runtime evidence stays separate from the unmodified release.
No existing deployment selects that local build.

## Remaining gates

The basic fixture uses single core/jobservice instances; the separate core-pair
case keeps its own process-level scope. Local shared registry
storage is not Ceph/S3 or an independent failure domain. The basic fixture does
not promote PostgreSQL; the optional case above qualifies only its controlled
local handover. No service session-continuity, cache-outage
recovery, Docker/Kubernetes image-client integration, full SSO/MFA/RBAC, scanner, signed-image
admission, service replica failover, upgrade/restore, off-platform backup,
physical host/network failure or load qualification follows from this test.
Those remain explicit gates; a successful component receipt must not set
`application_compatibility_verified`, `ha_verified` or `full_stack_verified`.
