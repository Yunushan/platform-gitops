# Disposable native pair-coordinator runtime probe

The explicit `PairTests.test_real_native_pair_vm_lifecycle` uses the same
[two-guest KVM laboratory](GATEWAY_EDGE_HOST_VM_RUNTIME.md) to run the actual
[entry pair coordinator](GATEWAY_EDGE_PAIR.md), SSH staging bootstrap, root
agent and PID-1-owned host jobs. It selects a separate opt-in environment flag;
ordinary validation does not connect to hosts or start guests.

The test begins with clean guest installation state. It provisions the
captured PyYAML 6.0.3 pure Python dependency into each disposable guest's normal
site-package path, where isolated Python can import it. The exact remote tool
directory remains separate and contains only its reviewed source inventory.
That inventory includes the declared runtime helper assets as well as imported
modules. The isolated-package regression test regenerates native and Docker
contracts using only the transferred tool set and the renderer dependency; it
must not fall back to files in the checkout.
The generated private SSH key is authorized for the two laboratory root
accounts. Each peer has its own verified host-key entry and independently read
machine-ID fingerprint. No production identity, SSH agent or unknown host key
is used.

Three separately journaled operations select initial installation, a reviewed
configuration update and explicit rollback. All distribute their real inputs
through the production staging path and use the pinned local SSH executable.
The production coordinator reserves both peers, starts B before A, probes every
declared route through the actual VIP and releases reservations after job
completion and process/cgroup drainage. No fake transport, host or observer is
injected. The configuration update changes the aggregate ready-route threshold;
it is not a daemon version upgrade.

The fixture retains private plans, coordinator journals and summaries of all
six original job receipts. It checks each job's operation/target, recorded
process identities, terminal unit properties and released reservation, then
checks both retained releases and cleared pending state after rollback. The
shared laboratory's original VM, source/artifact pin, strict TLS, no-upstream,
resource and cleanup checks also apply.

The real remote run exposed a directory-mode issue that direct installation
under a normal login umask did not exercise. Pair jobs use `UMask=0077`; plain
`mkdir(mode=0755)` created `0700` release paths and prevented HAProxy's separate
identity from reading its helper. New managed public directories now receive
an explicit `0755` mode and durable directory flush. Private lock, receipt and
job files retain their own `0600` modes, and existing directories are not
silently changed. An actual capability-free UID-65534 read under umask `0077`
reproduces the failure before this fix and passes afterward. Old partial
installations created by the earlier implementation still require permission
inspection before recovery; this creation fix does not rewrite existing paths.

Use the command and cache variables from the native host VM runbook, changing
the opt-in selection and test method to:

```bash
GATEWAY_EDGE_PAIR_VM_RUNTIME=1
python3 -B scripts/test_gateway_edge_pair.py \
  PairTests.test_real_native_pair_vm_lifecycle
```

Those assignments belong inside the original bounded `systemd-run`/`unshare`
command. An unbounded invocation or the host network namespace is refused.
Freeze project Python sources through terminal completion and independent
audit, and retain private journals when an operation fails. A timeout does not
authorize a new job or deletion of reservations. This probe does not establish
network partition fencing, physical HA, real Gateway/application behavior,
daemon version upgrades, publisher signatures or complete-stack acceptance.

## Scoped acceptance

On 2026-11-08 the two-guest real SSH run passed in 136.955 seconds. Independent
audit matched the original 348-Python-source snapshot and checked all three
private plans and coordinator journals against the recorded tool set, local
SSH executable, machine fingerprints and staging digests. It reconstructed all
six completed job records from their exact requests, process identities and
results and matched their canonical hashes. Each original job unit was absent,
with zero main/control PIDs and an empty control-group property. Both peers'
reservations were released, and rollback retained two releases with no pending
operation on either host.

The original parent unit, both VM processes and the exact temporary scope were
removed. Peak parent cgroup memory was 1,958,723,584 bytes with no OOM event or
kill under the 4 GiB limit. The private log SHA-256 is
`48dd4eb0a41ca4b73f496c6493c0b2a5ef26b54a0f393694c632948c75b65f16`.

This original profile accepts ordinary staged pair installation, configuration
update and retained rollback through real SSH and durable systemd jobs in
disposable guests. It does not interrupt the coordinator or kill a remote job.
The additional recovery profile below covers coordinator loss with the original
worker still alive. Physical HA, network partition fencing, a live
Cilium/application path, daemon upgrades and the complete stack/runtime matrix
remain unverified here.

## Explicit coordinator-recovery profile

`PairTests.test_real_native_pair_vm_recovery` selects an additional real-process
fault profile with `GATEWAY_EDGE_PAIR_VM_RECOVERY_RUNTIME=1` in the same bounded
VM command. It uses the production coordinator CLI as an original child process.
On the configuration update, a laboratory observer reads the original B job
record, verifies its operation/target, PID/start-ticks/boot-ID and systemd cgroup,
then sends `SIGSTOP` to that exact unit's members. It verifies stopped process
states before killing the original coordinator. A stopped worker is still live.

The profile attempts the same journal's resume while that original job is
stopped, with an explicit five-second job-observation window. A refused or
bounded unsuccessful observation must preserve both
reservations, the original job ID and the other host's service identities/job
count. It then continues the same original job and uses `--resume` again, with
no erased state, replacement job or fake transport. Successful recovery must
retain that worker's process identity and finish with exactly one job per host
for the operation.

After every completed operation, the profile invokes `--resume` once more and
checks unchanged job IDs/counts, service invocation IDs/PIDs, release state and
released reservations. Original raw job records are retained privately alongside
the coordinator journals for independent audit. This profile does not exercise
explicit recovery of a terminated worker, and does not claim its acceptance
from ordinary-operation evidence.

The first recovery run exposed a test-driver deadline mismatch: its outer
60-second watchdog expired before the selected CLI's 90-second observation
window. The deliberate uncertainty check now requests the CLI's supported
five-second window and still requires that the original job remains live,
stopped and unchanged afterward. This is an uncertainty experiment, not a
production availability target or permission to replace a timed-out job.

A subsequent runtime pass was rejected by the stronger receipt audit: its
completed journal still contained an active `error_code`, `failed_at` and
`job_state=running`. Successful completion now retains the prior failure under
`last_failure`, clears the active error fields and records `job_state=completed`
only after the existing peer/job/VIP/reservation checks pass. The regression
reproduces the old contradictory terminal receipt and verifies preserved
failure history without a second start of the original job.

### Recovery acceptance

On 2026-11-08 the recovery profile passed in 181.173 seconds against its original
349-Python-source snapshot. It verified three completed-operation resumes with
identical job counts/IDs, service invocation IDs/PIDs, release state and released
reservations. During the update it stopped the exact original B unit, killed
the original coordinator and observed an unsuccessful five-second resume while
the worker remained live/stopped. Both reservations and that job ID survived;
A's services and job count stayed unchanged. Continuing B and resuming the
original journal completed with the same B process identity and six total
original jobs across the three operations.

The independent audit re-read all six retained raw job records, reconstructed
their requests/results, checked the three plans/journals, compared all resume
and fault snapshots, verified terminal receipt consistency and matched all
source/artifact pins. It confirmed removal of the original unit, VMs,
coordinator processes and temporary scope. Peak parent cgroup memory was
1,988,923,392 bytes with no OOM event or kill. The private log SHA-256 is
`91dbe3717340d62dbd813e5027bb6d002783a31ad71cc830a960145d4f3c0999`.

The authoritative private audit is revision 2 (`.audit-v2.json`), with its
companion metadata-correction record. Revision 1 checked unit cleanup correctly
but emitted a guest snapshot in that summary field because of a shadowed
variable. Revision 2 repeats every check and re-queries original unit cleanup;
the original audit, runtime report and source snapshot were preserved.

This accepts coordinator-loss recovery with a live original worker and completed
idempotent resume in the disposable laboratory. Explicit terminated-worker
recovery, physical HA/fencing, the live Gateway/application path, daemon upgrades,
publisher signatures and complete-stack flags remain unverified or false.

## Explicit terminated-worker profile

`PairTests.test_real_native_pair_vm_terminated_recovery` selects
`GATEWAY_EDGE_PAIR_VM_TERMINATED_RUNTIME=1` inside the same bounded VM command.
It first performs the live/stopped-job uncertainty checks, then sends `SIGKILL`
to that verified original job unit. A separate read-only observer invokes the
actual agent's process-identity and systemd termination helpers. Both recorded
process identities must be dead; the exact unit must be terminal or absent,
with no main/control PID or populated cgroup and no restart policy.

Ordinary `--resume` then observes the original `unverified` handle for five
seconds. It must not rewrite that job, create a replacement, release either
reservation or advance the other host. Only a subsequent explicit
`--resume --recover-terminated-jobs` may mark the original job failed with
verified termination evidence and spend the coordinator's single retry
allowance. The test requires exactly one new B job under the same operation,
retains the original failed record and verifies the replacement's process
identity, completion, final peer/VIP readiness and released reservations.

The three-operation sequence therefore retains six completed job records plus
one verified terminated record. Completed-operation resumes must keep those
counts and service identities unchanged. This is a disposable-worker recovery
test, not a physical machine or storage failure test; it does not establish the
remaining live application, fencing, upgrade or full-stack requirements.

### Terminated-worker acceptance

On 2026-11-08 this profile passed in 197.104 seconds against its original
350-Python-source snapshot. The independent audit checked the actual agent's
recorded-process and PID-1/cgroup termination observations, ordinary resume's
unchanged unverified record and retained reservations, and the explicit
single-retry path. It re-read six completed records plus the original verified
terminated record, checked all three plans/journals and three idempotent resumes,
and verified the replacement's different job/process identity under the same
operation. The other host's services stayed unchanged until recovery could
advance normally. Both hosts ultimately retained two releases with no pending
operation or reservation.

The original unit, guests, coordinators and exact temporary scope were removed.
Peak parent cgroup memory was 1,988,530,176 bytes with no OOM event or kill.
The private log SHA-256 is
`1b8cc5bebd77cfcf859c190e94ee3abb34c9b4ae0f6e90e1dfd267d0a925dd4b`.

An earlier attempt stopped during guest boot, before reaching pair recovery.
Its temporary QEMU logs were not retained by the then-current harness, so its
cause remains unproven. The harness now captures bounded QEMU stderr, original
process return codes and parent cgroup memory events on failure. The subsequent
passing run does not establish that the earlier startup issue was fixed.

This accepts the named terminated-worker recovery and explicit single retry in
the disposable laboratory. Physical/fleet HA, network partition fencing, the
actual Cilium/application path, daemon version upgrades, publisher signatures
and the complete stack/runtime matrix remain unverified or false.
