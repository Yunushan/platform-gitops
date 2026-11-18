# Harbor database pool pre-use health experiment

Status: **local patched component case passed from rebuilt artifacts**: one
actual runtime test, zero skips, in 126.700 seconds. The foreground supervisor
completed in 129.147 seconds,
with unchanged public Python sources and removal of the original processes and
cgroup. The unmodified Harbor release remains unqualified for SQL-backed job
continuity across this handover. This experiment does not deploy a service or
change an existing profile.

## Reproduced failure and isolated comparison

The original Harbor 2.15.1 jobservice failed its first real audit SQL read after
the owned database route closed its old sessions and moved to the promoted
primary. A separate read-only driver probe uses the same pinned pgx v4.18.3,
TLS-verified endpoint, database credentials and pool sizes (8 open, 2 idle).
Both probe processes perform one application read before and one after handover.

In the measured comparison, the default pgx pool returned `unexpected EOF` on
its post-handover read. The candidate `OptionResetSession` callback rejected one
closed idle connection before the application query and then completed that
read over TLS on the new writable primary. No application query was resubmitted
by the probe. Strict three-member PostgreSQL quorum and the original session
fence remained required. The surrounding unmodified Harbor case still failed,
so the comparison was not accepted as a successful application runtime.

The probe source is `scripts/fixtures/harbor_db_pool_probe.go`. It accepts only
the isolated loopback endpoint, registry fixture database and TLS verification.
Its mode and ordered Unix-socket commands are bounded; it prints neither
connection strings nor raw driver errors. The private build used the original
Harbor module lock with downloads disabled.

## Candidate patch and build boundary

`patches/harbor/2.15.1-pg-pool-pre-use-health.patch` changes only
`src/common/dao/pgsql.go`. It uses pgx's existing `OptionResetSession` callback,
with a three-second bounded ping before reusing an idle connection. On a failed
ping it returns `driver.ErrBadConn`, allowing `database/sql` to discard that
connection before any application query is sent. This is not a retry policy
for a query whose outcome is unknown. A failure after the check can still fail
the application operation; in-flight/exactly-once recovery remains unqualified.

Beego receives the checked pool through `AddAliasWthDB`; its configured open,
idle, lifetime and idle-time limits remain unchanged. The patch does not alter
TLS, credentials, migration SQL, synchronous replication, job retry behavior,
worker counts or database routing/fencing.

The build uses a separate source copy and offline original module cache.
Its audit requires exactly the reviewed file change, unchanged original and
patched source trees during the build, explicit successful core/jobservice
exits and removal of the original build process/cgroup. Generated API inputs
come from the separately audited base build. Binaries identify themselves as
`v2.15.1-poolhealth.local`, with a distinct revision label. This is a local patch,
not an upstream-supported release or a production deployment artifact.

## Rebuild from reviewed offline inputs

The optional `scripts/build_harbor_pool_health.py` command rebuilds the candidate
from the reviewed archives, rather than copying a prior generated tree. Run it
from the checkout on a Linux amd64 host with systemd and the prepared module
cache. The output parent must exist, be ignored by Git and remain beneath
`private/validation/harbor`; the selected output directory must be new.

```bash
python3 -B scripts/build_harbor_pool_health.py \
  --source-archive /absolute/private/harbor-335c05f2b9507b4c7116fe69b84e31ef9f4b91dd.tar.gz \
  --go-archive /absolute/private/go1.25.9.linux-amd64.tar.gz \
  --swagger /absolute/private/tools/swagger \
  --module-cache /absolute/private/gopath/pkg/mod \
  --output private/validation/harbor/pool-health-build-new
```

Input pins are checked before output reservation. The builder extracts only
bounded regular files/directories, refuses links, escape paths, duplicate names
and file/directory conflicts, and drops privileged archive mode bits. It checks
all 3,231 original files, verifies offline Go modules, regenerates and checks
all 970 API files, applies the exact one-file patch with zero fuzz, and checks
the complete 4,201-file patched tree both before and after compilation. It
requires the same candidate binary hashes as the original experiment.

The build runs in a fresh systemd unit with `PrivateNetwork=yes`, a cold private
compiler cache, `GOPROXY=off`, a local pinned Go toolchain, read-only module locks,
explicit time/memory/task bounds and bounded private stage logs. The caller
retains its foreground supervisor until the original unit is terminal. Initial
or later observation failures do not trigger a new build. Exact invocation,
PID/start ticks, boot ID, cgroup and public Python hashes are retained; no
accepted receipt is published until process/cgroup cleanup and source stability
are verified. Only the worker's newly reserved temporary workspace is removed.

A v2 build receipt binds archive/compiler/generator identities and complete-tree
digests instead of depending on one timestamped private base receipt. The old
receipt path still requires its original exact base hash. Both paths retain
exact patch and executable pins, explicit stage success and cleanup requirements.
Build output, generated sources, logs and receipts remain private. A build
receipt sets `runtime_verified` to false; runtime evidence is separate. For v2,
`original_sources_unchanged` covers the immutable input archives and verified
original files before patching; the working tree is intentionally changed by
the one reviewed patch, whose complete final tree digest is checked separately.

Two same-host clean-archive, cold-cache runs completed in 159.574 and 161.725
seconds. Both reproduced the original candidate core/jobservice hashes exactly,
verified module and API generation inputs, and removed their owned processes,
cgroups and temporary work. This is local reproduction, not independent
publisher-signature or cross-host build verification. The 11 build-contract
checks are included in normal validation; 109 related tests passed with 19
opt-in cases skipped. Current edited files pass LF, syntax and lint checks.
The freshly rebuilt artifacts then passed the same complete worker/core/SQL/
cache/database runtime case in 126.700 seconds, with the new receipt path and
current public source hashes checked. Original process/cgroup cleanup remained
required; no diagnostic probes or query retries were added.

The cross-OS Git ignore preflight uses a read-only command scoped to this exact
checkout's `safe.directory`; it does not write global Git configuration. Existing
output directories are never replaced. If observation cannot complete, retain
the original reservation and inspect that unit rather than launching another.

## Explicit runtime selection

Prepare the artifacts required by the [service fixture](HARBOR_SERVICE_RUNTIME.md),
plus the separate candidate build and its `build-inputs.json`. In an owned fresh
Linux network namespace with a bounded cgroup and retained foreground supervisor:

```text
HARBOR_POOL_HEALTH_TEST_RUNTIME=1
HARBOR_POOL_HEALTH_TEST_ARTIFACTS=/absolute/private/candidate-build
POSTGRES_PROMOTION_TEST_RUNTIME=1
```

```bash
PYTHONPATH=scripts python3 -B -m unittest \
  test_valkey_gateway.GatewayTests.test_real_local_harbor_pool_health_candidate_after_peer_losses_and_promotions
```

The dedicated fixture verifies exact candidate binary hashes, patch hash,
source revision/archive, Go archive, module-lock hashes, base-generation receipt,
changed-file hashes and explicit build exits. It refuses drift, missing build
cleanup or implicit selection. The normal service fixture retains its original
release pins even when the candidate environment flag is present. Runtime
reports carry the hashes of the binaries actually staged.

The candidate composes the full [worker/core continuity case](HARBOR_JOBSERVICE_REPLICA_RUNTIME.md):
six distinct SQL-backed dry-run jobs, exact original worker/core peer losses,
retained worker identity and authenticated shared log reads, real token/image
work, cache promotion, route fencing, strict PostgreSQL promotion and new work
through the same survivors. Diagnostic probes do not run in this acceptance
case. The original release's failed receipts must remain separate.

## Acceptance still required

The measured candidate run required all six real SQL-backed jobs, the original
surviving worker pool/start time, core/worker process identities, authenticated
shared logs, token/image round trips, verified database TLS, strict three-member
quorum and route fencing. It used the separate candidate binaries without the
diagnostic probes. The first candidate run was rejected because its patch hash
used the wrong report schema; that failed receipt remains separate. Portable
regressions passed 109 tests with 19 opt-in cases skipped; the service-contract
suite contains 18 checks and the builder suite adds 11 checks for provenance,
archive safety, output ownership, bounded failure and original-job observation.

This native component success must keep unmodified-release continuity,
upstream support, independent-host HA, in-flight job rescue, shared Ceph/S3
failure domains, partition safety, upgrades, load and full-stack qualification
false. Patch review and a maintained release/build path would be needed before
considering production use. No chart, image tag or deployment switches to this
candidate automatically.
