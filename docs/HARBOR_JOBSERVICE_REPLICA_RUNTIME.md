# Harbor jobservice replica-loss component

Status: **not accepted**. The SQL-backed combined run failed after controlled
PostgreSQL handover: the original surviving worker's first audit query returned
`unexpected EOF`. The owned processes/cgroup were removed and public Python
sources stayed unchanged. Existing deployment profiles and pinned Harbor binaries
are unchanged. This composes the [two-core case](HARBOR_CORE_REPLICA_RUNTIME.md),
[token/OCI workflow](HARBOR_JWT_OCI_RUNTIME.md) and strict database/cache fixtures.

## Required worker and storage evidence

Two original jobservice processes use the same Redis/Valkey worker namespace,
verified TLS cache/database, private internal credentials and private file-log
directory. Their API listeners use distinct native ports behind an unchanged
TLS-passthrough endpoint. The vendored Harbor chart supports jobservice replicas
and mounts the job log volume into each replica. This local shared directory
models that shared log path; it is not evidence for Ceph or independent storage.

The first process must register exactly one healthy worker-pool ID before its
peer starts. A production `PURGE_AUDIT_LOG` dry-run job must complete then, with
a real SQL-backed handler and retained log. The second process must introduce
one distinct fresh pool ID; both original IDs/start times, concurrency and
registered job type must be visible through the real statistics APIs. Each
peer must return the first job's authenticated log with the same SHA-256.
Unauthenticated log access must be refused.

The route uses verified HTTPS `/api/v1/stats` health checks, CA/hostname checks
and minimum TLS 1.2. Runtime state must name only the original backend members
and ports. A 200 status alone does not establish two healthy worker pools; the
fixture separately checks their IDs, start times and heartbeat freshness.

After initial work and core peer loss, the exact first jobservice process is
killed using its owned process handle. Health routing must remove that peer.
The old pool must no longer be healthy, and the original surviving pool must
remain healthy with its original start time. The survivor must read the
primary-only job record and identical log, then execute a new job and complete
fresh API/token/image work through the unchanged endpoint. The same surviving
worker identity is required after cache and route-fenced PostgreSQL promotion.

Acceptance requires six distinct completed jobs: the primary-only witness and
one in each of `initial`, `core-peer-loss`, `job-peer-loss`, `promoted` and
`database-promoted`. All are read-only audit dry runs; no mock job handler,
password reset, mail delivery or company endpoint is involved. Unexpected exits,
changed process identities, stale/unexpected pools or missing logs fail the test.

## Observed database continuity failure

The strengthened combined run executed one test with zero skips in 137.038
seconds; the supervisor finished in 139.975 seconds and did not accept the case.
It reached both peer-loss phases and cache promotion. After the database route
was fenced, PostgreSQL was promoted and strict three-member quorum was restored,
the new SQL-backed audit job failed in `pkg/audit/dao/dao.go` with `unexpected EOF`.
This is a failed application-continuity gate, not evidence of an unsafe database
promotion or a proven root cause inside the driver.

The earlier dry-run parameter set omitted `include_event_types`. In the pinned
Harbor source, that leaves an empty operation filter and the audit DAO can return
without executing SQL. The shared fixture now supplies `include_event_types:
"pull"`, requires the actual job to succeed, retains at least two observed TLS
application connections and rejects any observed plaintext application
connection. Terminal unsuccessful jobs fail immediately; neither the fixture
nor the non-retryable upstream purge job retries the mutation as a new job.
Earlier receipts remain historical evidence for their original API/cache/token
checks; they do not establish SQL-backed worker continuity after handover. The
separate core-pair recheck also failed on a terminal unsuccessful SQL-backed
job after handover in 71.646 seconds (one test, zero skips), confirming that
this gate is not specific to the new two-worker topology.

Do not work around this failure by disabling TLS, changing synchronous quorum,
reducing connection pools, restarting the survivor, removing session fencing,
retrying a failed job or replacing the SQL handler with a mock. The combined
case remains unaccepted until application connection recovery is fixed and the
same assertions pass. Shared same-host logs and completed-job reads alone are
insufficient to qualify jobservice HA.

A separately pinned [pool pre-use health experiment](HARBOR_DB_POOL_HEALTH.md)
now passes this combined local workflow with a reviewed one-file candidate
patch, including all six SQL-backed jobs. It does not replace these failed
original-release checks or qualify the deployed version.

## Run explicitly

Prepare the same private artifacts and environment as the
[service fixture](HARBOR_SERVICE_RUNTIME.md#run-explicitly), then select:

```text
HARBOR_JOB_REPLICA_TEST_RUNTIME=1
POSTGRES_PROMOTION_TEST_RUNTIME=1
```

In an owned fresh Linux network namespace with a bounded cgroup, retain the
foreground supervisor through the original invocation's terminal result:

```bash
PYTHONPATH=scripts python3 -B -m unittest \
  test_valkey_gateway.GatewayTests.test_real_harbor_jobservice_peer_loss_and_original_worker_after_promotions
```

The ordinary suite skips actual services unless explicitly enabled. Passwords,
tokens, keys, database state, job logs and execution receipts remain private.
Bounded scrubbed worker/core errors are retained privately on failure.

## Still required

This is a native same-host process/queue/log component. It does not qualify
independent jobservice hosts, Ceph log-volume failure/recovery, in-flight job
rescue or exactly-once side effects, scheduler/lease partition safety, complete
Harbor HA, load, upgrade/restore, external backup or the full five-runtime stack
matrix. Completed-job read-back and new work after a process loss must not be
reported as proof of arbitrary in-flight task recovery. Those gates stay false.
