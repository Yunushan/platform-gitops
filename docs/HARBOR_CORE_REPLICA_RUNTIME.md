# Harbor core replica-loss component test

**Validation update:** the shared audit-job fixture now requests an explicit
`pull` event type so its dry run must execute SQL. The stronger
[jobservice continuity case](HARBOR_JOBSERVICE_REPLICA_RUNTIME.md#observed-database-continuity-failure)
failed on the worker's first database query after handover (`unexpected EOF`).
Earlier dry-run receipts used an empty operation filter and do not prove this
SQL-backed job-continuity requirement. The strengthened core-pair recheck also failed this job-continuity gate in
71.646 seconds (one test, zero skips); source hashes stayed unchanged and the
original processes/cgroup were removed. Full acceptance remains pending; keep
the original process, TLS, quorum and route-fencing checks in place.

Status: the optional local case passed in **112.921 seconds** (one test, zero
skips). The supervisor verified source stability and original process/cgroup
cleanup in 115.598 seconds. It extends the [token/OCI workflow](HARBOR_JWT_OCI_RUNTIME.md)
and [planned database handover](HARBOR_SERVICE_RUNTIME.md) without changing the
existing deployment profiles or Harbor source/binaries.

## Topology and required behavior

Two original pinned core executables use separate loopback listeners and
per-process Beego configuration. They share the private token-signing key,
encryption key, internal credentials, verified-TLS database/cache and registry
backend. The vendored Harbor chart's core Deployment supports replicas and
mounts shared core Secrets; these local inputs reproduce that arrangement.
No Secret values or company configuration are part of the public fixture.

The stable client endpoint uses a pinned HAProxy process with TCP/TLS passthrough.
Each backend has an actual HTTPS `/api/v2.0/ping` check, required CA/hostname
verification and minimum TLS 1.2. The public TLS identity remains pinned end to
end at the clients. Each original peer must also accept an authenticated OCI
root request directly, then read the same image with the original guest token.
The proxy state read-back must identify exactly the two reviewed IPs/ports,
with no unexpected administrative state.

After initial work completes, the fixture sends SIGKILL only to the exact first
core process handle it created. It records that intentional loss, retains all
other original process/start identities and waits for automatic health-based
removal of the dead core. The core route is not manually switched. The original
survivor must immediately perform another fresh authenticated project creation,
real dry-run job and token/image push/pull through the unchanged endpoint.
The later cache and controlled PostgreSQL promotions require more fresh work
and earlier-token/image continuity through that same survivor.

The exact expected set is four distinct service/job/token/image phases:
`initial`, `core-peer-loss`, `promoted`, `database-promoted`. Unexpected service
exits are still failures. A deliberate stopped process remains tracked for
cleanup; it is not silently replaced or forgotten.

## Database-route fencing

The combined case exposed an error in the old local endpoint model: moving a
backend address alone did not prevent application reconnections to the old
primary while it rejoined as a standby. A write was correctly rejected with
SQLSTATE `25006` (read-only transaction). That failed run was not accepted.

The database fixture now places its owned backend in maintenance, shuts down
its existing sessions and observes zero old application database connections
before beginning promotion. Its old-source shutdown checkpoint and unchanged
strict three-member TLS/quorum checks still have to pass. The endpoint can be
reopened only after the exact new primary address is selected and read back as
ready. No fixture API mutation is retried to hide a failure. Pool settings and
synchronous durability requirements are unchanged.

These commands act only on the owned disposable database proxy and exact
reviewed server name. They model controlled endpoint handover, not automated
CNPG/Patroni selection, watchdog fencing or physical failure-domain protection.

## Run explicitly

Use the private artifacts/environment from the
[service fixture](HARBOR_SERVICE_RUNTIME.md#run-explicitly) and select:

```text
HARBOR_CORE_REPLICA_TEST_RUNTIME=1
POSTGRES_PROMOTION_TEST_RUNTIME=1
```

Run within an owned fresh Linux network namespace and bounded cgroup, retaining
the foreground supervisor until the original invocation is terminal:

```bash
PYTHONPATH=scripts python3 -B -m unittest \
  test_valkey_gateway.GatewayTests.test_real_harbor_core_peer_loss_and_survivor_after_promotions
```

Passwords, keys, tokens, temporary database/storage and execution receipts stay
private. Bounded scrubbed core errors are retained privately on failure. The
ordinary suite skips actual service execution unless explicitly enabled.

## Remaining qualification

This is a same-host native-process failure component. Independent core hosts,
load-balancer/VIP failure domains, browser-session/CSRF/SSO/MFA continuity,
jobservice replica failover, independent Ceph/S3 storage, automated database
failover, partition/in-flight-write behavior, upgrades, off-platform recovery,
load and full runtime installations still require evidence. A successful local
case must keep generic application, physical HA and full-stack gates false.
