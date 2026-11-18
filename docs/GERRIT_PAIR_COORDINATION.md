# Prepared Gerrit host-pair coordination

Status: optional fresh Docker/native pair orchestration. Coordinator and agent
recovery tests use modeled hosts. A disposable real systemd test verifies the
original job survives its launcher and cannot report termination while its
cgroup is live. Its Gerrit actions are modeled. **Real SSH delivery of this new
agent, prepared Gerrit startup, shared CephFS writes, peer forwarding and failover
remain unverified.** This tooling does not qualify a complete stack.

`scripts/manage_gerrit_pair.py` coordinates the
[per-host activation lifecycle](GERRIT_HOST_ACTIVATION.md). It requires two
independently prepared hosts with distinct reviewed machine identities, direct
addresses and declared failure domains. Both use the same version 2 site/storage
plan, release/API artifact locks and common SSH host-key commitment. This is not
a replacement for the separate [Kubernetes adapter](GERRIT_ACTIVATION.md).

## Private preparation

Complete the site initialization, actual Ceph mounts, private configurations,
TLS/key provisioning, runtime installation and package review described in the
host guide. This coordinator does not copy credentials, initialize data, mount
Ceph, provision accounts or install Docker/Java. Its root SSH credential is for
operating the prepared hosts, separate from Gerrit application credentials.

Export the closed first-party agent source set to a **new** private directory:

```bash
python3 scripts/manage_gerrit_pair.py \
  --export-tools-dir private/generated/gerrit-pair-tools
```

The command prints `toolset_sha256` and creates `scripts/` plus
`tools-manifest.json`. On each host, stage the exact exported `scripts/` directory
under the reviewed `remote_root`, with root-owned protected ancestors and files.
Keep that directory closed: no unrelated Python files, caches or symlinks.
Python 3.12 or newer and PyYAML 6.0.3 must be installed independently. The isolated bootstrap
verifies source hashes/ownership before loading the agent. Root-owned private
control directories and journals are retained under `remote_root/private/`.

Create a private plan from [the example](../examples/gerrit-host-pair.json).
Replace the complete pair site plan and both complete per-host plans, preserving
their separately pinned machine/boot/runtime/private-file/trust values. Pin the
exported toolset digest and local SSH executable SHA-256. Each peer specifies
its prepared startup bundle, root agent directory, direct address/SSH port,
explicit key-file and known-hosts environment variables, and declared domain.

Each known-hosts file must contain only that peer's exact approved host-key
entries. SSH uses strict host-key checking, one connection attempt, explicit
identity files and no agent/password authentication, forwarding, local commands,
proxies, connection multiplexing or inherited user SSH configuration. Keep
private plans, keys, paths, addresses and deployment receipts outside public Git.
Domain labels are operator declarations; the tool does not prove physical rack
or power separation and keeps `physical_failure_domains_verified=false`.

## Local validation and remote checks

```bash
python3 scripts/manage_gerrit_pair.py --plan private/gerrit-host-pair.json
```

Local mode makes no connection or journal. An explicit remote check is:

```bash
python3 scripts/manage_gerrit_pair.py \
  --plan private/gerrit-host-pair.json --mode check-peers \
  --ssh-binary /usr/bin/ssh \
  --output-dir private/generated/gerrit-pair-preflight
```

This verifies both actual machine/boot identities, exact regenerated packages and
host prerequisites before any member start. It may establish private agent
control directories/lock files and writes a private coordinator receipt; it does
not create/start a Gerrit unit or container. Staging remains explicit.

## Activate both prepared hosts

```bash
python3 scripts/manage_gerrit_pair.py \
  --plan private/gerrit-host-pair.json --mode activate \
  --confirm ACTIVATE_PREPARED_GERRIT_PAIR --ssh-binary /usr/bin/ssh \
  --output-dir private/generated/gerrit-pair-activation
```

Both hosts preflight before either starts. The coordinator reserves both members,
then activates them in reverse plan order, one at a time. Each member operation
runs as an original bounded systemd transient job owned by PID 1. SSH loss does
not kill the job or authorize a second member start. The launcher provides a
dedicated cgroup, no automatic restart and finite time/memory/process limits.
Transient job names retain the existing `platform-entry-job-<id>.service` prefix
from the shared launcher; the pair journal records their exact random IDs.

After both activation jobs finish, separate observation jobs resume each original
host activation receipt and recheck its implemented TLS/SSH readiness and original
invocation. The coordinator requires the original worker/launcher processes and
job cgroup to be terminal before accepting a completed result. A completed JSON
record while the worker/cgroup is still live is `finishing`, not terminal proof.

The reservation and receipts remain after coordination completes. Logical
`release` records completion; it does not delete ownership or authorize a fresh
pair operation to adopt running members. No automatic router change, promotion,
upgrade, restart, rollback or cleanup is performed.

## Resume original jobs

```bash
python3 scripts/manage_gerrit_pair.py \
  --plan private/gerrit-host-pair.json --mode resume \
  --confirm ACTIVATE_PREPARED_GERRIT_PAIR --ssh-binary /usr/bin/ssh \
  --output-dir private/generated/gerrit-pair-activation
```

Use the original directory with unchanged plan, tools, boots and packages.
Each job ID is durably recorded before its request. A lost acknowledgment is
resolved by observing that same job; an absent, failed or unverified job stops
for review. The coordinator does not reissue missing launches or retry failed
workers. Retain both remote and local receipts for diagnosis; recovery of a
terminated/failed worker still requires separate reviewed intervention.

Completed receipts are historical. Resume waits for any previous observation
jobs to be terminal, retains their handles, then requests new observations of
both original members. It does not reuse old readiness as fresh availability
evidence, including after a lost final acknowledgment. Another operation,
different source/host/boot, changed unit/container policy or restarted observed
invocation is refused.

Success reports `pair_members_observed_ready` for these implemented observation
gates. It keeps peer forwarding, Git writes, failover, physical failure domains,
HA and full-stack verification **false**. The separate lab is still required for
real shared-write/locking/recovery, application/CI/CD integration, authenticated
Git operations, protocol-correct routing and member-loss acceptance.

## Tests

```bash
PYTHONPATH=scripts python3 -B -m unittest scripts/test_gerrit_pair.py
```

Ordinary tests model transport/application responses. They cover two-host
preflight/order, original job acknowledgment loss, unknown/live/failed jobs,
fresh resume observations, source/host mismatches, duplicate launch refusal and
the late-launcher/fast-worker race. The optional actual PID-1 test is:

```bash
GERRIT_PAIR_TEST_SYSTEMD=1 PYTHONPATH=scripts \
  python3 -B -m unittest test_gerrit_pair.SystemdJobTests
```

It creates a fresh private control tree and job, lets the original short-lived
launcher exit, checks that a completed worker record is still `finishing` while
the worker lives, then verifies the original processes/cgroup terminate. It
does not borrow production services, run SSH, start Gerrit or use a fake mount
as Ceph evidence. Cleanup is confined to its original verified temporary tree.
The same optional suite exports the actual closed agent source set, verifies its
protected ownership and hashes under isolated Python, and regenerates matching
runtime package hashes from those staged sources. This passes without claiming
the new agent's real SSH or application deployment has been tested.
