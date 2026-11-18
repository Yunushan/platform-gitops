# Gerrit HA entry-point adapter

This opt-in adapter renders a two-primary active/passive **router**, not a
complete Gerrit installation. It does not initialize sites, deploy applications,
install plugins, create a VIP, configure identities, or certify HA. Keep the
[full stack expansion requirements](MULTI_RUNTIME_STACKS.md) in scope.

## Architecture and upstream basis

The upstream [Gerrit HA plugin documentation](https://gerrit.googlesource.com/plugins/high-availability/+/refs/heads/master/README.md)
requires shared repositories, the high-availability plugin and a load balancer.
It documents routing SSH to one primary at a time. The documentation also calls
for the global-refdb module to be present, and an implementation of global-refdb
for active-active operation. Select and test version-compatible artifacts; this
adapter does not download unreviewed plugins or claim active-active support.

Both public HTTPS and SSH pass through unchanged. The HTTPS backend checks the
SSH banner **and** a verified TLS connection to the
[Gerrit healthcheck plugin endpoint](https://gerrit.googlesource.com/plugins/healthcheck/).
The SSH backend tracks the same primary/backup health decision. An SSH-only or
HTTP-only outage therefore removes that server from both routing paths. Requests
are not round-robin distributed across primaries. TLS terminates on Gerrit, whose
certificate must cover the canonical hostname on both primaries. HAProxy checks
both the CA chain and hostname; no `verify none` exception is provided.

At startup each backend must pass two combined health checks before it can
receive traffic. HTTPS and SSH can use a healthy backup while the preferred
primary is still failing its checks. This configuration uses HAProxy's
`init-state fully-down`, verified with HAProxy 3.2.26.
[HAProxy initial server state](https://docs.haproxy.org/3.2/configuration.html#5.2-init-state)

When a backend is marked down, HAProxy closes its existing HTTPS and SSH
streams. By default, when the preferred primary returns to service, HAProxy closes
existing backup streams as new connections return to the primary. This
applies to each router's own sessions. Clients must reconnect, and a push
interrupted during failover or failback may have an uncertain result. Check
the remote ref or review revision before retrying the push.
[HAProxy server state actions](https://docs.haproxy.org/3.2/configuration.html#5.2-on-marked-down)

The checks use HAProxy's documented
[multi-port TCP-check sequences and server tracking](https://docs.haproxy.org/3.2/configuration.html).
The generated artifacts still need parser checks against the exact selected
HAProxy binary and live failure tests before deployment acceptance.

## Prepare privately

1. Copy `examples/gerrit-router.json` to an ignored private file. Set distinct,
   stable per-primary DNS addresses, the canonical host and backend ports. Do
   not point either backend at the public load balancer or a pooled Gerrit
   service. Select a reviewed digest-pinned HAProxy image; the public placeholder
   is deliberately rejected until replaced.
2. Prepare both initialized Gerrit sites, shared Git storage, independent local
   indexes/caches, matching SSH host keys, peer-forwarding/authentication and
   compatible HA/global-refdb/healthcheck artifacts. Restrict direct primary
   access so users cannot bypass the supported routing/authentication path.
3. Provision backend certificates for the canonical hostname and their trusted
   CA outside Git. No credentials or certificate contents belong in the plan.
   Run the backend preflight from the router network with the
   [review canary in place](GERRIT_SITES.md#version-2-health-configuration-guard).
4. Select a runtime and render into a **new** ignored private directory:

```bash
python3 scripts/render_gerrit_router.py \
  --plan private/gerrit-router.json \
  --runtime rke2 \
  --output-dir private/generated/gerrit-router-rke2
```

Rendering never applies resources or starts services. Existing output directories
are rejected, and a partial output directory from an I/O failure is retained
for inspection. The renderer prints an aggregate status.

## Check both backends before routing traffic

```bash
python3 scripts/check_gerrit_router_backends.py \
  --plan private/gerrit-router.json \
  --backend-ca private/gerrit-backend-ca.crt
```

Use the same CA bundle and router plan as the deployment. The command connects
directly to both backends, verifies the TLS certificate with the canonical
hostname, and checks the SSH identification line at the address used for HTTPS.
It requires distinct backend addresses, HTTP 200 and a passing result for each
required probe. A failed `querychanges` check is reported by name. The output
contains probe names and statuses and omits backend addresses and response text.
A nonzero exit means the preflight did not verify both backends. Repository
writes, SSH authentication and failover require separate acceptance tests.

## Hold the preferred peer during recovery

Set `"hold_primary": true` in a copy of the private router plan and render
a fresh bundle. Install it on **every** router before restarting the preferred
Gerrit peer. Use the direct backend preflight to check the backup and verify
traffic reaches it through both protocols on every router. For Kubernetes,
wait for the selected ConfigMap to reach all Deployment replicas and the old
pods to terminate. For Docker and native deployments, restart or replace each
router with the held configuration and confirm the previous processes have
exited. Quiesce new writes during this transition if any router could still
send traffic to the preferred peer.

Record the direct management address of every serving router in an ignored
JSON file. In Kubernetes, use each current router Pod's address from an
authorized management path. For Docker, use a private tunnel or loopback
port mapping to the management listener. For native Linux, use a protected
management interface. A VIP, pooled Service, or incomplete inventory can
hide a router with the previous configuration.

```json
[
  {"address": "192.0.2.11", "port": 8404},
  {"address": "192.0.2.12", "port": 8404}
]
```

```bash
python3 scripts/check_gerrit_router_backends.py \
  --plan private/gerrit-router-held.json \
  --router-inventory private/gerrit-router-instances.json
```

Each management endpoint must report the expected configuration ID and
backup selection for both protocols. The command exits nonzero on a mixed
configuration, mixed selection, unavailable backend, or unreachable router.
The report uses inventory positions for failed checks. The management port
uses HTTP and must stay on a trusted management network or tunnel. This
check reads HAProxy's current routing state. Confirm direct inventory
membership and test Git through both entry points during recovery.

The hold survives a router restart. HTTPS and SSH use only the backup. If the
backup becomes unhealthy, the router fails closed. A router preflight can still
reach both Gerrit sites directly.
[HAProxy server maintenance configuration](https://docs.haproxy.org/3.2/configuration.html#5.2-disabled)

Keep the hold while Gerrit completes startup index recovery or an offline
rebuild. Record the outage change numbers and their current Git revisions from
the serving peer. Put the evidence in an ignored private JSON file with this
shape. Use a Gerrit reader that can see all the named changes.
Store its HTTP token outside Git in a file readable only by the operator.

```json
{
  "username": "recovery-reader",
  "changes": [
    {"number": 123, "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
  ]
}
```

```bash
python3 scripts/check_gerrit_router_backends.py \
  --plan private/gerrit-router-held.json \
  --backend-ca private/gerrit-backend-ca.crt \
  --recovery-evidence private/gerrit-outage-changes.json \
  --reader-token-file private/gerrit-recovery-reader.token
```

The check requires matching current revisions in both direct Gerrit search
indexes as well as healthy HTTPS and SSH on two distinct backend addresses.
It prints counts and probe statuses. A nonzero exit means keep the hold.
Quiesce review writes before capturing the final revisions and keep writes
paused while applying the regular bundle with `hold_primary` set to false on all
routers. Refresh the direct inventory and repeat the router check using the
regular plan. It now requires every router to select the preferred peer.
Run the client entry check below and fetch a canary patchset through both
protocols before resuming writes.
This procedure verifies the listed revisions at check
time. It requires coordinated operator rollout and a complete outage inventory.
Pass up to 50 changes per run and check every batch before releasing the hold.
[Gerrit secondary-index search operators](https://gerrit-review.googlesource.com/Documentation/user-search.html)

For failure recovery, the external entry point and each writer still
need an independently verified fencing strategy.

### Verify the client-facing entry address

From a client network, verify the actual VIP or external entry IP after each
rollout. Use the held plan to expect the backup and the regular plan to expect
the preferred peer. The HTTPS check verifies the canonical hostname, CA and
expected peer's leaf certificate before sending the reader token. It searches
for the exact current revision. The SSH check uses Git to compare the exact
patchset ref with that revision and validates the shared SSH host key.

Use distinct peer TLS certificates covering the same canonical hostname.
Record their SHA-256 fingerprints from the trusted provisioning artifacts.
The check expects lowercase hex without separators. Refresh the pins when
certificates rotate. This check assumes TLS passthrough to the Gerrit peers.
Use a read-only Gerrit account with both an HTTP token and an authorized
SSH key usable in batch mode. Keep the key readable only by its owner.
The known-hosts file must pin the canonical client hostname,
using `[hostname]:port` for a nondefault SSH port.

```json
{
  "address": "192.0.2.50",
  "https_port": 443,
  "ssh_port": 29418,
  "peer_certificate_sha256": {
    "primary": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "backup": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "username": "recovery-reader",
  "project": "review-canary",
  "number": 123,
  "patchset": 2,
  "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

```bash
python3 scripts/check_gerrit_router_backends.py \
  --plan private/gerrit-router.json \
  --backend-ca private/gerrit-backend-ca.crt \
  --entry-evidence private/gerrit-client-entry.json \
  --reader-token-file private/gerrit-recovery-reader.token \
  --ssh-identity private/gerrit-recovery-reader.key \
  --known-hosts private/gerrit-entry-known-hosts
```

The command runs on Linux with Git and OpenSSH. It reports two read-only
observations over separate client connections. The shared SSH key identifies
the service while the HTTPS certificate identifies the peer. Also check
the direct router inventory and verify VIP ownership during a fault drill.
[Git reference checks](https://git-scm.com/docs/git-ls-remote) ·
[OpenSSH host key settings](https://man.openbsd.org/ssh_config)

### Exercise VIP movement in the isolated lab

The opt-in Gerrit VIP fixture runs the reviewed Gerrit peer pair with two
HAProxy and Keepalived processes on separate Linux network namespaces.
Keepalived reads the local router's configuration ID and combined HTTPS and
SSH route state. A router with no usable peer stops advertising the VIP.
The drill checks the same patchset over both protocols before and after it
stops the VIP owner's HAProxy. It starts that router again, verifies that the
new owner retains the VIP, and stops the preferred Gerrit peer to check
backend failover. After each failover, it uploads a new review over HTTPS
and another over SSH, checks their exact revisions on the serving peer,
and fetches each patchset over the other protocol. The target branch keeps
its original tip. When both Gerrit peers stop, the fixture requires the VIP
to be withdrawn and both client checks to fail.

The HTTPS interruption drill stops the VIP owner's HAProxy at two controlled
points. First, the fixture pauses the request before forwarding its body to
Gerrit. After the VIP moves, both peers must return 404 for the original
project, branch and Change-Id. The drill then retries the exact original
commit. In the second case, the fixture holds the completed response before
the client receives it. The client reports a failure, but after the VIP
moves the review is present and its exact patchset can be fetched. The drill
checks the existing review and skips resubmission in that case.
[Gerrit Change-Id matching](https://gerrit-review.googlesource.com/Documentation/user-changeid.html)

The SSH cases use a test-only Git pre-push hook and byte relay. One push
pauses after the remote ref advertisement, before Git sends the update.
The other forwards the update and withholds the server response while the
fixture checks Gerrit's REST API for the accepted revision. In each case
the drill stops the active HAProxy, observes the SSH connection close,
and checks the review outcome through the new VIP owner. It retries the
original commit only in the first case and fetches both verified patchsets
over HTTPS.
[Git push protocol](https://git-scm.com/docs/gitprotocol-pack)

Use the artifact inputs from the [Gerrit plugin fixture](GERRIT_SITES.md#verify-plugin-startup-on-a-disposable-site)
and the pinned archives and launch procedure from the
[gateway VIP fixture](GATEWAY_EDGE_VIP_RUNTIME.md#artifact-inputs-and-explicit-execution).
Use a unique `platform-gerrit-vip-*` transient unit, a runtime limit of up to
360 seconds, a 4 GiB memory limit, and a 512-task limit. Set
`GERRIT_VIP_TEST=1 GERRIT_PEER_INDEX_TEST=1 GERRIT_ROUTED_GIT_TEST=1` and run
`python3 -B scripts/test_gerrit_artifacts.py LivePluginTests` inside the fresh
network namespace. Supply the reviewed `GATEWAY_EDGE_TEST_HAPROXY` and
`GATEWAY_EDGE_VIP_TEST_ARTIFACTS` paths alongside the Gerrit artifact inputs.

The drill uses shared local Git data and one Linux kernel. It verifies reads
and writes after each transition and samples ownership. The HTTPS
and SSH interruption points use test-only barriers. Production VIP rollout,
storage loss, and partition fencing remain separate acceptance gates.
[Keepalived tracking and VRRP behavior](https://www.keepalived.org/documentation/keepalived-conf/)

## Runtime packaging

| Runtime | Output | Required operator provisioning |
|---|---|---|
| RKE2, K3s, existing Kubernetes | ConfigMap, two-replica Deployment, PDB and ClusterIP Service | Existing namespace, CA Secret (`ca.crt`), eligible separate nodes, network policy and redundant external TLS/SSH entry |
| Docker | Compose file for one router host | Supply readable `backend-ca.crt`; deploy on two independently managed hosts with redundant ingress/VIP |
| Native Linux (`raw`) | Unprivileged HAProxy systemd unit; optional two-host Keepalived bundle | Approved Linux packages, `haproxy` user and group, CA bundle, two entry hosts, protected management access |

The native unit validates configuration before starting. It does not use a
container as a hidden replacement for native execution. The Docker file refuses
automatic creation of missing bind sources. The Kubernetes workload does not
mount service-account credentials and uses mandatory node anti-affinity.
The immutable ConfigMap name changes with the rendered configuration, so
applying a changed bundle updates the pod template and starts a rolling
replacement. Keep the previous ConfigMap through the rollout if rollback is
needed. For a CA-only change, use a new CA Secret name in the plan or perform a
rolling restart to make the workers load it.

The pod readiness probe requests `GET /readyz` on port 8404. The same
management listener serves the read-only `GET /routez` check.
The role reported by `/routez` comes from the combined health check on the
HTTPS backend and also requires a usable SSH target. The SSH backend tracks
that same health decision.
HAProxy returns
503 until both protocols have a usable target and whenever all targets are
unavailable. Liveness still checks the HTTPS listening socket so a Gerrit
outage does not cause the router to restart. The ClusterIP Service exposes
only ports 443 and 29418. Docker publishes 8443 and 29418. For native Linux,
keep port 8404 on the management network. CA files need to be readable by
UID/GID 65532 for containers, or the native `haproxy` service user.

On a Cilium Gateway cluster, attach the canonical Gerrit HTTPS hostname to the
[TLS passthrough listener](CILIUM_GATEWAY.md#prepare-the-prerequisites). Target
the router Service's HTTPS port 443 and the Pod's port 8443 so HAProxy retains
primary selection. Expose SSH through the reviewed Gerrit router TCP path.
For the [shared Gateway VIP](GATEWAY_EDGE.md#gerrit-ssh-through-the-shared-vip),
set `ssh_node_port` in the Kubernetes router plan. The renderer adds the
SSH-only `gerrit-router-ssh` Service alongside the ClusterIP Service.
The Gateway adds ingress policy to the router Pods. Before attaching the route,
authorize legitimate direct CI and SSH clients through separate scoped ingress
rules for their actual source identities and ports. Verify those paths as well
as the Gateway's HTTPS path.

### Verify route selection with the prepared K3s pair

A disposable K3s lab ran two instances of the generated HAProxy 3.2.26
Deployment against prepared Gerrit 3.13.10 Pods on separate workers. Both
routers started with the preferred backend. The test verified each HTTPS
leaf certificate against its peer pin and uploaded a review through HAProxy
over SSH with a pinned host key. The backup advertised the exact review ref
before the preferred Pod was stopped.

Both replicas then reported `backup` through `/routez` and served the backup's
TLS certificate. A new SSH review upload succeeded against the backup, and
both routers served the exact old and new review refs. When the second Gerrit
Pod stopped, both routers returned HTTP 503 from `/routez` and rejected new
SSH connections. The regression test also exercises automatic failover and
explicit primary hold with a real HAProxy process.

The K3s test used direct port-forwards to both router Pods and a shared NFSv4
claim on a single server. Cilium Gateway, VIP and CephFS acceptance remain
separate from this drill.

### Verify HTTPS and SSH failover with Cilium

A five-VM K3s v1.35.0+k3s1 run joined the prepared Gerrit pair and both generated
HAProxy replicas to Cilium 1.19.10. The route installer applied a TLSRoute to the
router Service. Each worker's Envoy listener returned the preferred Gerrit
instance's pinned certificate and advertised the exact review ref to Git over
HTTPS. The separate SSH NodePort accepted the fixture client after a scoped
ingress rule was applied.

The test stopped the preferred Gerrit Pod, pushed a new review over SSH with
strict host-key verification, and read both exact review refs over HTTPS
through both Envoy listeners. Both listeners returned the backup instance's
pinned certificate. After both Gerrit Pods stopped, the router health checks
reported unavailable and the HTTPS and SSH entry paths refused new connections.

The run used a single NFSv4 server for shared Git storage and local site volumes.
CephFS, storage failover and a redundant external VIP remain separate acceptance
drills. The 17m 35.026s run matched 554 Python source hashes and the prepared-pair
fixture. The test unit and cgroup were removed. The terminal log SHA-256 is
`c145b7031039d36a57fe278fa7c0c4864305d041cd02b22866e8c9f8cf2944be`.

### Verify review uploads through the shared VIP

The joined K3s v1.35.0+k3s1 lab ran the two Gerrit Pods with Cilium 1.19.10,
both generated Gerrit routers, and the generated HAProxy/Keepalived edge pair.
The two edge peers ran in separate network namespaces on the lab host.
Read-only edge preflight verified the live Gerrit SSH NodePort and ready
router endpoints on both workers. The fixture verified that Cilium denied
direct client access to the NodePort before testing the edge route.

The client uploaded one review over pinned SSH through the VIP, a second
after stopping HAProxy on the VIP owner, and a third after stopping the
preferred Gerrit Pod. The surviving edge returned an SSH banner in 3.082
seconds after the first fault. Before the third push, Git over HTTPS and
authenticated SSH both read the previous refs. After the push, all three
refs resolved to their exact commits over HTTPS through the VIP with the
backup Gerrit instance's pinned certificate. The VIP withdrew after both
Gerrit Pods stopped.

The final 24m 3.348s run matched 554 Python source hashes and the private
prepared-pair fixture. The lab service, network namespaces, staged archives
and VM scope were removed. The terminal log SHA-256 is
`7636f83b052b495227d75519e51144b830b99ddf05719826e54fd45d10fa4e1a`.
The edge processes shared one kernel and the repositories used a single
NFSv4 server. Independent edge-host failure, CephFS recovery and complete
stack acceptance remain open.

### Render and install the native VIP pair

Add a `native_vip` object to the private router plan and render with
`--runtime raw`. Supply two Linux entry hosts in separate failure domains
with stable IPv4 addresses in the VIP subnet. Choose a VRID unused on that
network and allow unicast VRRP between the two entry addresses.

```json
{
  "native_vip": {
    "vip": "192.0.2.50/24",
    "vrid": 73,
    "entry_hosts": [
      {"name": "edge-a", "address": "192.0.2.10", "interface": "eth0", "failure_domain": "rack-a"},
      {"name": "edge-b", "address": "192.0.2.11", "interface": "eth0", "failure_domain": "rack-b"}
    ]
  }
}
```

### Apply one native host at a time

With the approved daemon packages and `haproxy` account installed, run the
apply command on the selected entry host. For the first installation use
`--fresh`. For an update or rollback, supply the currently installed
plan as `--previous-plan` and the desired plan as `--plan`.

```bash
sudo python3 scripts/apply_gerrit_native_vip.py \
  --host edge-a \
  --plan private/gerrit-router-held.json \
  --previous-plan private/gerrit-router.json \
  --backend-ca private/gerrit-backend-ca.crt
```

The command renders the selected plan locally and validates existing files
against the supplied previous and target plans. It stops Keepalived first,
then HAProxy. It installs the target files, runs both parsers and checks the
expected HAProxy route before starting Keepalived. Repeating a successful
invocation against the same active target reports `changed: false`.
The command records a pending transition on the host before stopping
services or replacing any configuration. If a run stops partway through,
retry with the original host, target plan, previous plan and CA. A different
transition is rejected while that record exists. The generated units skip
startup, including at boot, while configuration files are being replaced.
Once both daemon parsers pass, the command records that activation can
proceed. The record is cleared after both services start and the expected
route is available. To restore the previous release while an update is
still pending, run the original invocation with `--rollback-pending`
added. Use the same `--plan`, `--previous-plan`, host and CA as the
failed update. If the rollback itself stops partway through, repeat that
rollback invocation. For rollback after a completed rollout, exchange the
target and previous plans. Use the same VIP topology and CA for these
transitions. Pending rollback requires an existing previous release.

Apply the standby first and confirm the VIP and client traffic before
updating the former owner. Pause writes throughout a hold or release
transition and verify both routers before resuming. The host command leaves
boot enablement and the two-host rollout order under operator control.

### Manual installation and preflight

On both hosts, install the approved HAProxy and Keepalived packages and
Python 3. Install the generated `haproxy.cfg` and `gateway_edge_health.py`
under `/etc/haproxy/` and provision the trusted CA at
`/etc/haproxy/backend-ca/ca.crt`. Use root-owned configuration files and
directories. Make the HAProxy config and CA readable by the `haproxy`
service user. Install only the host's own `keepalived-<name>.conf` as
`/etc/haproxy/gerrit-router-vrrp.conf`.

Install `gerrit-router.service` and `gerrit-router-vrrp.service` under
`/etc/systemd/system/`. Run the read-only host preflight on each host
after staging the files and before starting the units:

```bash
sudo python3 scripts/check_gerrit_native_vip.py \
  --plan private/gerrit-router.json \
  --host edge-a \
  --backend-ca private/gerrit-backend-ca.crt
```

Select `edge-b` on the second host. The command checks the declared interface,
address and prefix, matches the installed files and CA to the reviewed inputs,
and invokes both daemon configuration parsers. HAProxy's parser runs as
the service account. The command reports pass/fail flags and leaves services
untouched. Confirm that the VIP and listener ports are available separately.
The packaged services use TCP 8443 and 29418 for clients and
TCP 8404 for health and management. Restrict direct entry-host access with
the host firewall so clients use the VIP. After `systemctl daemon-reload`,
start `gerrit-router.service` and verify both routes through the direct
management endpoint before starting `gerrit-router-vrrp.service`. Check one
VIP owner and run the client entry check over the VIP before enabling the
units at boot. Use a dedicated VRRP instance for this bundle.

The VRRP health command requires the configuration ID of the matching
HAProxy bundle. Apply both files from a single render when applying or
releasing `hold_primary`. Mismatched files withdraw the VIP on that host.
Keep a copy of the regular bundle for rollback. Pause review writes and
identify the current VIP owner before each rollout. On the standby host,
stop both Gerrit router units, install the selected bundle, and run host
preflight. Start HAProxy, verify the expected route identity, then start
Keepalived. Stop the units on the current VIP owner and verify that the
updated standby takes the VIP and serves the expected peer before changing
the former owner. After updating both hosts, run the router inventory and
client entry checks before resuming writes. Use the same order when
restoring the regular bundle. Two-peer VRRP has no quorum fencing for a
partition.
[Keepalived VRRP configuration](https://www.keepalived.org/documentation/keepalived-conf/)

The two-guest systemd drill installs the generated files at their native
paths, runs the host preflight and starts both units without drop-ins. It
checks CA-verified HTTPS and the SSH banner through the VIP after stopping
the owner's HAProxy, then verifies withdrawal when both routers stop and
recovery after restart. It rolls out the held bundle one guest at a time
and restores the regular bundle in the same order. The checks require the
VIP to reach the selected backend over both protocols and the full router
inventory to reject an incomplete transition. The drill also terminates the
standby apply after HAProxy's config is replaced and before Keepalived's
config is replaced. It reboots the standby with both units enabled,
verifies that systemd skips both services, then resumes the original apply
while the VIP stays on the active host.
It also checks rollback from a target with an unreachable backend. The
failed activation leaves Keepalived stopped on the standby. The original
plans restore its earlier route while the active host retains the VIP.
To rerun it, follow the
[native edge VM prerequisites](GATEWAY_EDGE_HOST_VM_RUNTIME.md#running-it)
with `GERRIT_ROUTER_VM_RUNTIME=1` and test
`HostTests.test_real_gerrit_router_systemd_lifecycle`. The backends in this
drill provide fixture TLS responses and SSH banners. The separate Gerrit
VIP drill exercises real review pushes.

## Remaining acceptance gates

- Verify exact HAProxy syntax/image provenance, plugin compatibility and shared
  filesystem semantics. Verify enabled healthcheck probes, not just HTTP 200.
- Test clone, push, review events, identities, SSH host-key continuity and TLS.
- Test SSH failure, TLS failure, healthcheck failure, primary loss, recovery,
  in-flight pushes and long-lived SSH/Zuul event streams. Clients must reconnect
  when an established connection dies; TCP forwarding cannot migrate sessions.
- Test both load-balancer nodes and redundant entry independently. Health
  decisions are local to each router; network partitions can produce different
  selections. Shared storage, HA plugin correctness and any required fencing or
  promotion coordination must be proven; routing alone does not establish a
  cluster-wide single-writer invariant.
- Test rolling maintenance, capacity, backups, restore, split-brain avoidance,
  RPO/RTO and full CI/CD integration. The router does not satisfy these gates.

Local structural tests do not prove live deployment. If Docker/HAProxy or an
isolated cluster is unavailable, report that limitation instead of accepting the
stack based on rendering alone.

## Routed Git uploads against Gerrit

The opt-in Gerrit 3.13.10 fixture uses the generated HAProxy configuration,
the reviewed Gerrit WAR and plugins, and a temporary Git project. It clones
the project and pushes a new review through each entry point. It checks that
the pushed commit is the current review revision, the branch tip stays
unchanged, and the same patchset can be fetched through the other protocol.
[Gerrit review upload and patchset refs](https://gerrit-review.googlesource.com/Documentation/user-upload.html)

Use the [plugin startup fixture command](GERRIT_SITES.md#verify-plugin-startup-on-a-disposable-site)
with `GERRIT_ROUTED_GIT_TEST=1` and `GERRIT_ROUTER_TEST_HAPROXY` pointing to the
reviewed HAProxy 3.2.26 binary. Git verifies the fixture HTTPS certificate and
uses the Gerrit site's public host keys for strict SSH verification.
Also set `GERRIT_PEER_INDEX_TEST=1` to run the two-process failover case.
The fixture verifies change-index forwarding before routing Git. It
uploads one review over each protocol, stops the original preferred Gerrit
process, and verifies the surviving peer's TLS certificate through the same
HAProxy process. It fetches both pre-outage patchsets and uploads and fetches
new reviews over both protocols. Both peers have separate indexes and share
temporary local Git storage and the fixture SSH host identity.
[Gerrit active-passive routing](https://gerrit.googlesource.com/plugins/high-availability/+/master/README.md#active_passive-configuration)

Before restarting the preferred process, the fixture runs an offline
change-index rebuild from the shared repositories. It then verifies the
preferred peer's certificate through the original router, searches for both
reviews created during the outage with their exact revisions, and fetches
those patchsets. It verifies new HTTPS and SSH uploads after failback.
[Gerrit offline reindex command](https://gerrit-review.googlesource.com/Documentation/pgm-reindex.html)

Add `GERRIT_AUTO_REINDEX_TEST=1` to the two-process case to test the HA
plugin's startup reindex. This enables `autoReindex` on the preferred peer
from its first start. The fixture blocks inbound peer forwarding before the
outage. While the preferred peer is stopped, it replaces the router with
a rendered `hold_primary: true` configuration. It restarts the preferred
peer and checks both search indexes for the outage revisions. The check
accepts the latest patchset and rejects its predecessor. The fixture then
restarts HAProxy with the held configuration to verify that it still selects
the backup with both peers healthy. It applies the regular configuration
and verifies failback, patchset fetches, and new review uploads over HTTPS
and SSH.
[Gerrit HA plugin configuration](https://gerrit.googlesource.com/plugins/high-availability/+/master/src/main/resources/Documentation/config.md)

Both recovery tests run on one host with shared local storage. The automatic
case uses two independent HAProxy processes and rejects a partial rollout
when applying the hold and when releasing it. The client entry check verifies
the HTTPS peer certificate and SSH patchset through the local router addresses
and rejects an unexpected certificate or a missing patchset. The
`GERRIT_ROUTER_TEST_HOLD=1` test also verifies refusal of both protocols when
the backup fails during the hold. CephFS outages, coordinated cross-host
fencing and pushes interrupted in flight require separate verification.

## Optional actual HAProxy protocol test

`GerritRouterTests.test_real_haproxy_combined_health_and_write_route_selection`
is an opt-in Linux test with a real, SHA-256-pinned HAProxy 3.2.26 process and
explicitly modeled HTTPS and SSH-banner backends. It verifies that HTTPS and
SSH switch to the backup after an invalid SSH banner, a TLS hostname
mismatch or an HTTP 503. For each fault, it keeps both connections open
through failover and checks that they close. After recovery, it repeats the
check for the connections to the backup as traffic returns to the primary.
The fixture holds health responses at startup to verify that HTTPS and SSH
refuse clients before any backend passes. It releases the backup first and
checks that both protocols choose it and readiness returns 200. Readiness
returns 503 again when both backends become unhealthy.

The optional Kubernetes API test verifies admission, creation and read-back
of the rendered Deployment, ConfigMap, Service and PodDisruptionBudget
against the [disposable API fixture](ROOK_BOOTSTRAP.md). It uses the same
pinned `ROOK_BOOTSTRAP_TEST_BIN` binary directory:

```bash
GERRIT_ROUTER_TEST_API=1 python3 -B scripts/test_gerrit_router.py \
  GerritRouterTests.test_kubernetes_router_admission
```

The HAProxy test keeps the generated timing, frontend/backend and TLS-check directives.
Only the backend CA file path is relocated into the private temporary scope.
It checks HTTP PUT route selection, untrusted client CA rejection and refusal
to select either backend when both are unhealthy. The process remains non-root,
capability-free and no-new-privileges throughout. Its original process is not
restarted to satisfy the fault checks. It uses only loopback in a new network
namespace and contacts no existing forge or company endpoint.

Run as an isolated root-supervised laboratory with the exact fixture binary
and a new proof path, for example from a Linux checkout:

```bash
sudo systemd-run --wait --pipe --collect --quiet \
  --unit="platform-gerrit-router-test-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')" \
  --property=RuntimeMaxSec=240 --property=MemoryHigh=768M \
  --property=MemoryMax=1G --property=MemorySwapMax=0 \
  --property=TasksMax=256 --property=NoNewPrivileges=yes \
  --property=CapabilityBoundingSet=~CAP_SYS_TIME \
  --property=KillMode=control-group --property=UMask=0077 \
  --working-directory="$PWD" \
  /usr/bin/env GERRIT_ROUTER_TEST_RUNTIME=1 \
    GERRIT_ROUTER_TEST_HAPROXY="$REVIEWED_FIXTURE_HAPROXY" \
    GERRIT_ROUTER_TEST_REPORT="$NEW_PRIVATE_PROOF" \
  /usr/bin/unshare --net -- /usr/bin/python3 -B \
    scripts/test_gerrit_router.py \
    GerritRouterTests.test_real_haproxy_combined_health_and_write_route_selection
```

The fixture requires OpenSSL, `ip`, a compatible host ABI and the binary digest
in its source; it does not download or install an arbitrary binary. A binary
digest is not publisher-signature verification. Proofs bind the source snapshot
and remain private. Retain the original handles and independently inspect
cleanup; an observation timeout is not a reason to restart this test.

These are protocol tests with modeled backend health, responses and writes.
They do not execute Gerrit, SSH Git, plugins, shared CephFS, fencing, split-brain
prevention or actual Git writes. The requested real Gerrit HA/shared-storage
integration and complete-stack acceptance remain open. The
[version 2 site guard](GERRIT_SITES.md#version-2-health-configuration-guard)
separately locks the plugin's required probe configuration before Java starts;
it also cannot establish that a live plugin executes those checks correctly.
