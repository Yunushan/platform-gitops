# Optional PostgreSQL database restore drill

`scripts/restore_postgres_backup.py` boots a fresh private copy of a completed
[physical backup](POSTGRES_BACKUPS.md), replays its included WAL, checks bounded
backup-time data witnesses and stops the new database. It works with backups
captured from any of the five source runtime choices. The drill runs on a
separately prepared non-root Linux host/container using the original backup's
approved numeric UID/GID. It does not install PostgreSQL or replace an existing
CNPG/Patroni cluster, application database, volume or service endpoint.

A passing `database_restore_drill_verified` result proves this database drill
only. `restore_verified`, `application_restore_verified`, `off_platform_verified`,
`network_namespace_verified`, `ha_verified` and `full_stack_verified` remain false.
Application files/object storage, credentials, service integration, independent
backup storage, cluster reconstruction and measured RPO/RTO need further work.

## Prepare the runner and evidence

Keep all backups, plans, witness records, logs and proofs in restricted ignored
`private/` storage. Preserve the original backup's exact plan, completed receipt,
independently recorded receipt SHA-256, sealed inventory, complete data and
client/helper dependencies. Offline backup verification must pass first.
No source database connection, API token or TLS private key is needed for this
drill. The source's database files still contain private application information
and account hashes.

Copy `examples/postgres-restore.json` into `private/` and review these fields:

- `backup_plan_sha256`: the SHA-256 of `postgres_physical_backup.canonical(plan)`
  encoded as UTF-8, not the raw JSON file hash. The original plan must have a
  pinned decimal system identifier. `backup_receipt_sha256` is the independently
  retained hash of the exact completed capture receipt bytes.
- `runner_machine_id_sha256`: SHA-256 of the stripped `/etc/machine-id` on the
  intended drill host. It must differ from the backup plan's three declared
  database host hashes. These declarations do not prove off-host placement.
- `server`: SHA-256 pins for native `postgres`, `psql`, and `pg_controldata`,
  all at the original backup's exact PostgreSQL 17/18 patch. Supply the complete
  versioned installation, including its normal `bin`, `share` and library
  layout. Standalone copied executables are insufficient. Pin every required
  runtime asset tree; an optional private loader directory requires an exact
  regular-file library inventory and SHA-256 pins. Root-managed system loader
  dependencies and OS provenance still require review.
- `runtime_parent`: an existing owned mode-0700 directory beneath `/run`,
  separate from the backup and output paths. Use a short path to fit Unix socket
  limits. Runtime/tool ancestors must be trusted, without symlinks or writes
  by other identities. Data and private configuration use modes 0700/0600.
- `database_user`: an existing superuser role in the backup. The new socket uses
  an explicit local peer map from the reviewed OS account to that role. Use the
  actual CNPG bootstrap administrator or native database administrator identity.
- `limits`: startup/query/shutdown deadlines, per-process address-space limit
  and startup log bound. Provide filesystem quotas/headroom and an aggregate
  cgroup limit separately. The address-space limit is inherited by each database
  process; it is not a total memory quota. Data copying is bounded by the
  original backup's inventory/size limits.
- `witnesses`: independently retained backup-time row counts and digests, in
  one to eight named databases. Do not derive expected results from the restored
  copy itself.

For a reviewed runtime tree, the helper's canonical file-map digest can be
computed on the intended Linux runner without starting PostgreSQL:

```bash
python3 -B - <<'PY'
import json
import sys
from pathlib import Path
sys.path.insert(0, "scripts")
from restore_postgres_backup import asset_tree
paths = ("/usr/share/postgresql/18", "/usr/lib/postgresql/18/lib")
print(json.dumps({path: asset_tree(Path(path)) for path in paths}, indent=2))
PY
```

Review and retain these pins independently. They identify files relative to the
reviewed installation; they do not authenticate a software publisher.

## Establish backup-time witnesses

The operator must provision and commit a small reserved canary table before
capturing the backup. The helper never writes a witness into the source. Use an
ordinary heap table, exactly these non-generated built-in columns and no row
security or rules:

```sql
CREATE SCHEMA platform_restore_canary;
CREATE TABLE platform_restore_canary.witness (
  id BIGINT PRIMARY KEY,
  payload TEXT NOT NULL
);
```

Insert reviewed synthetic canary rows as part of the approved source operation.
Retain the committed expected rows/count/digest through an independent evidence
channel. Each database must be UTF-8 and have 1–1000 canary rows with at most
1 MiB of payload in total. This implementation checks reserved canaries, not
arbitrary application SQL, views, functions or all application records.

The canonical digest is SHA-256 of UTF-8 text: rows ordered by numeric `id`, then
`payload COLLATE "C"`; each row is `id:UTF8_payload_byte_length:payload`; rows
are joined by one newline without a final newline. The drill checks relation
metadata first and reads `FROM ONLY` that exact heap. It verifies rows using
read-only transactions and returns only counts/digests, never payload text.

## Validate, then run explicitly

From the repository root on the prepared drill runner:

```bash
python3 scripts/restore_postgres_backup.py \
  --backup-plan private/postgres-backup.json \
  --restore-plan private/postgres-restore.json \
  --backup-dir private/postgres-backup-001
```

Default mode validates the plans and exact completed receipt/tooling pins. It
does not inspect the runner, read the full backup, start processes or connect to
any database. It reports `local_inputs_verified`, with drill success false.

Start the explicitly requested drill in a new directory:

```bash
python3 scripts/restore_postgres_backup.py \
  --backup-plan private/postgres-backup.json \
  --restore-plan private/postgres-restore.json \
  --backup-dir private/postgres-backup-001 \
  --run-drill --output-dir private/postgres-restore-001
```

The helper verifies the original backup, copies every sealed file without links
or overwrites and checks the full copied inventory. It quarantines original
configuration, automatic settings and recovery/standby signals only in the new
copy. A fresh configuration disables TCP listening, source replication,
archiving/recovery shell hooks, preload libraries and logical replication
workers. One private Unix socket accepts only the reviewed local peer identity.
This is not a kernel network sandbox; run with separately provisioned egress
isolation where required. The source backup must come from a trusted database.

The foreground postmaster runs with no-new-privileges, disabled core dumps,
per-process address-space limit and a Linux parent-death signal. The helper
records its original PID/start-time/boot/process-group identity before waiting
for readiness. It verifies the private socket/data-directory identity, system
identifier, exact version, UTF-8, checksums, read-only transactions, completed
recovery and replay at least through the backup manifest's required WAL end.
It then checks every expected witness and shuts down that same instance.
No single-user PostgreSQL mode substitutes for normal recovery/client behavior.

Success requires a zero-exit clean shutdown, no surviving observed drill
processes, `pg_controldata` in the expected clean state, unchanged original
backup, unchanged reviewed runtime assets and unchanged helper source pins.
The private `postgres-restore-proof.json` records the original backup receipt,
restore plan/source pins, operation/process identity, phases, witness summaries
and scoped results. Raw PostgreSQL diagnostics remain in private `postgres.log`.
The original capture receipt and data remain unchanged. The stopped restored
copy is retained for review; it is not automatically put into service.

## Failure and recovery

Existing output directories are refused. There is no automatic adoption,
restart, deletion, retry, promotion or reconnection to a source. An exception
attempts to stop the recorded original process and records a failed drill;
uncertain cleanup cannot set `server_stopped` or drill success true. Parent
death kills the guarded postmaster, but does not establish that every child
was observed stopped after a host failure. Preserve the private proof, data,
runtime directory and original process handle until reviewed cleanup confirms
shutdown. Do not signal an unrelated/reused PID or infer shutdown from a stale
file. Use a fresh output directory for an explicitly reviewed new attempt.

Passing integrity checks plus a successful canary drill is still insufficient
for a production restore/cutover. Rebuilding HA topology, application data and
files, off-platform failure recovery and independent bootstrap acceptance remain
required for the complete stacks.

## Validation scope

The default suite covers receipt pins, unsafe scopes, preserved original data,
fresh configuration, mismatched witnesses, uncertain cleanup, modified tooling
and failed proof publication. POSIX orchestration cases skip on Windows.

On a disposable root Linux host with `setpriv`, OpenSSL and a working C compiler:

```bash
POSTGRES_RESTORE_TEST_LIVE=1 \
POSTGRES_RUNTIME_TEST_BIN=/opt/validation/usr/lib/postgresql/18/bin \
POSTGRES_RUNTIME_TEST_LIBS=/opt/validation/usr/lib/x86_64-linux-gnu \
  python3 -B scripts/test_postgres_restore.py
```

The opt-in fixture creates temporary three-process PostgreSQL 18 TLS replication,
commits synthetic Unicode canaries, captures a non-root physical backup and
removes capture credentials before restoration. It runs a successful real WAL
recovery drill and a wrong-witness refusal, verifies shutdown and original-backup
integrity, and separately exercises the Linux parent-death guard with an idle
compiled process. Its databases use UID/GID 65534 and temporary `/run` state;
an occupied loopback port 5432 is refused. Fixture PostgreSQL results do not
prove CNPG/Patroni operation, off-host storage, application restore or HA.

Primary references: [PostgreSQL server process](https://www.postgresql.org/docs/18/app-postgres.html),
[physical recovery and WAL](https://www.postgresql.org/docs/18/continuous-archiving.html),
and [backup manifest format](https://www.postgresql.org/docs/18/backup-manifest-format.html).
