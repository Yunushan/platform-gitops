# Optional PostgreSQL physical backup capture

`scripts/postgres_physical_backup.py` captures a complete PostgreSQL 17/18
database cluster over certificate-authenticated TLS and performs local integrity
verification. A prepared database on RKE2, K3s, existing Kubernetes, Docker or
native Linux uses the same replication protocol. The backup runner itself is
a separately prepared non-root Linux process/container; this helper does not
install its host, provision source roles, schedule retention or restore a server.

The [HA policy and lifecycle runbook](POSTGRES_HA.md) covers database creation,
runtime observations and planned CNPG switchover. The separate
[database restore drill](POSTGRES_RESTORE.md) boots a fresh copy and verifies
backup-time canaries. Complete application/stack and off-platform recovery
acceptance remain open.

## Prepare private inputs

Copy `examples/postgres-backup.json` into ignored `private/` on the intended
backup host. Replace every placeholder with reviewed values:

- Set the source runtime and exact PostgreSQL 17/18 patch. The four client
  executables must use that same patch: `psql`, `pg_basebackup`, `pg_verifybackup`
  and `pg_waldump`. Use their direct versioned executable directory, rather than
  distribution wrapper/symlink commands. Pin each executable's SHA-256.
- Declare the source TLS hostname, explicit transport IP, port and approved
  replication username. A reviewed SSH-forward address may be used while
  retaining the real certificate hostname for `verify-full` validation.
- Bind the runner's non-root UID/GID and SHA-256 of the stripped 32-character
  `/etc/machine-id`. Declare three distinct database-host machine-ID hashes;
  the runner hash must differ from each. These are reviewed declarations, not
  live attestation of the database machines, so `off_platform_verified` stays
  false. A container runner needs the intended host identity provisioned
  read-only; an arbitrary container ID does not establish host separation.
- Provision an owned mode-0700 TLS input directory containing `ca.crt`,
  `client.crt`, and an unencrypted `client.key`. Pin the public CA/certificate
  hashes. Private keys must be owned by the runner and mode 0600. No password
  or private-key contents belong in the plan or public repository.
- Set timeout, transfer-rate, file-count and post-capture size limits. Provision
  a dedicated durable destination filesystem with quotas and capacity for both
  transport and extracted copies. Size checks are enforced after transport
  capture and during extraction; they are not a streaming filesystem quota.

All path ancestors must be trusted, without symlinks or writes by other
identities. An optional private loader directory must contain exactly the
declared, hash-pinned regular library files. Otherwise root-managed system
libraries are used. Executable hashes/versions do not replace OS/library
provenance and supply-chain review.

The source needs an approved `REPLICATION` role, certificate mapping/HBA rules,
trusted client certificate and sufficient WAL sender/slot capacity. That source
configuration is an explicit prerequisite. CNPG normally has its operator-managed
`streaming_replica` certificate identity; using it requires approval and private
credential provisioning. Native Patroni policy currently supplies SCRAM
replication rules, so a separately reviewed certificate-authenticated backup
rule/identity is required. The helper does not change HBA, roles or server policy.
Automatic source-account integration remains unfinished.

Backups contain the whole database, including account hashes and potentially
private configuration. Keep plans, evidence, transport and extracted data on
restricted private storage outside the public repository. Independent storage,
encryption, access control, retention and disaster recovery need deployment and
acceptance on the intended backup infrastructure.

## Validate and identify the source

Run from the repository root on the prepared backup runner:

```bash
python3 scripts/postgres_physical_backup.py --plan private/postgres-backup.json
```

Default mode validates only the plan schema. It does not read certificates,
inspect the host identity, execute client tools, contact the source or create
output. Explicit source checking can use `system_identifier: null`:

```bash
python3 scripts/postgres_physical_backup.py \
  --plan private/postgres-backup.json --check-source \
  --output-dir private/postgres-source-check-001
```

The source check verifies runner identity, tool hashes/versions and local TLS
material, then uses the physical replication protocol's `IDENTIFY_SYSTEM` and
fixed `SHOW` commands. The observed version, TLS availability, full-page writes
and data checksums must match the policy. The CA and hostname are verified by
libpq; inherited credentials, service settings and loader injection are removed.
The client's password-file lookup is directed to `/dev/null` and prompts are
disabled. This lane uses certificate authentication, not a stored password.

Inspect `source_before.system_identifier` in the private
`postgres-backup-proof.json`. Review it against the intended cluster (for example,
the independently obtained runtime observation), then set that exact decimal
string as the plan's `system_identifier`. Capture refuses an unpinned identity
before any runtime operation. The source check prints only summary flags;
private endpoint information and database identities stay in the private proof.

## Capture and verify

```bash
python3 scripts/postgres_physical_backup.py \
  --plan private/postgres-backup.json --capture \
  --output-dir private/postgres-backup-001
```

The directory must be new. The client requests a full tar-format backup with
streamed WAL and SHA-256 file checksums. A fast checkpoint and replication backup
connections can add source load. Tar transport keeps server tablespace paths
from directing writes to paths on the backup host. External tablespaces,
archive links, special files and traversal paths are currently refused. A
private partial transport is retained if capture or extraction fails.

Archive transport, sealing and restore-copy reads use explicit streaming byte
budgets. Large backups do not require raising the whole-file memory limit.
Growth beyond a sealed file size or the reviewed archive budget is refused
before returning excess bytes; symlinks and special input files are rejected.

The helper extracts only a supported base/WAL pair into fresh private paths,
checks the manifest system identifier, PostgreSQL major version and single
timeline, and runs `pg_verifybackup` with required WAL parsing enabled. The source
identity/timeline and relevant settings are checked again after capture. A
history change causes refusal rather than publication of success. Captured data
files and directories are synced before the completed proof is recorded.

The private output contains:

- `transport/`: original `base.tar`, `pg_wal.tar`, and `backup_manifest`.
- `data/`: extracted plain backup and the original manifest, suitable for
  offline integrity verification and the separately guarded database restore drill.
- `sealed-inventory.json`: SHA-256/size records for every extracted file and an
  exact directory inventory, including files PostgreSQL's verifier normally
  ignores and WAL outside the required parse range.
- `postgres-backup-proof.json`: plan/tooling pins, source observations, manifest
  and inventory hashes, phases and scoped result flags. Raw command diagnostics
  and private credential values are not stored in this evidence.

Stdout returns a `receipt_sha256` on success. Retain this expected hash through
an independent trusted evidence channel. Checksums detect changes relative to
that expected receipt; they are not an operator signature or an independently
authenticated storage service.

Later, verify without contacting the database or reading capture credentials:

```bash
python3 scripts/postgres_physical_backup.py \
  --plan private/postgres-backup.json --verify \
  --output-dir private/postgres-backup-001 \
  --receipt-sha256 REVIEWED_COMPLETED_CAPTURE_RECEIPT_SHA256
```

Offline verification requires the original pinned plan, completed receipt,
complete sealed data, original helper/dependencies and reviewed client tools.
It compares the whole tree before and after `pg_verifybackup`, and leaves the
original capture proof unchanged. This can run on another prepared Linux host
using the approved numeric UID/GID and private files; the capture-host identity
and TLS inputs are not required for offline verification. Preserve the helper,
dependencies and executable/library artifacts as independent bootstrap material.
An incomplete/failed capture cannot become accepted by merely rerunning verify.
There is no automatic capture retry, adoption, deletion or retention pruning.

`captured` and `backup_integrity_verified` report narrowly scoped results.
`restore_verified`, `off_platform_verified`, continuous WAL archive, HA and
complete-stack flags remain false. Use the separate drill to start a restored
copy and check canaries; checking full application data, testing independent
storage recovery and meeting RPO/RTO are still required. A streamed base backup
is not a continuous WAL archive.

## Validation scope

Default tests cover all five plan runtime choices, source-history changes,
unreviewed tools/credentials, unsafe archives, external tablespaces, private
extraction, whole-tree seals, offline verification and immutable evidence pins.
POSIX filesystem cases skip on Windows; the runner is a Linux component.

On a disposable root Linux test host, opt in to a real three-process PostgreSQL
fixture and non-root certificate-only backup capture:

```bash
POSTGRES_BACKUP_TEST_LIVE=1 \
POSTGRES_RUNTIME_TEST_BIN=/opt/validation/postgres18/bin \
POSTGRES_RUNTIME_TEST_LIBS=/opt/validation/postgres18/lib \
  python3 -B scripts/test_postgres_physical_backup.py
```

The fixture creates only temporary `/run` data, refuses an occupied loopback
port 5432, runs databases/capture as UID/GID 65534, and stops its own processes
before cleanup. It uses actual TLS replication and `pg_basebackup`/
`pg_verifybackup`, removes capture credentials before offline verification, and
checks ignored-file and control-file corruption. It does not prove off-host
placement, a restored running server, CNPG/Patroni behavior or storage failure
recovery.

The implementation follows the PostgreSQL primary documentation for
[physical capture](https://www.postgresql.org/docs/18/app-pgbasebackup.html),
[backup verification and its limits](https://www.postgresql.org/docs/18/app-pgverifybackup.html),
[replication identity commands](https://www.postgresql.org/docs/18/protocol-replication.html),
and [manifest format](https://www.postgresql.org/docs/18/backup-manifest-format.html).
