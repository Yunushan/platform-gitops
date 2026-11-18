# Original CI/CD application through a native external entry pair

The opt-in
`WorkerRuntimeTests.test_real_cilium_gerrit_native_cicd_with_external_entry`
extends the [actual Cilium/Gerrit/Tekton/Argo CD worker test](K3S_VM_WORKERS.md)
with two separate native Linux edge guests. The three control-plane guests and
two CI/serving workers remain Kubernetes nodes. The edge guests do not run K3s
or join the cluster. The Gateway contract retains its original `k3s` runtime,
management target and digest; only the external entry contract uses `raw`.

The joined seven-guest case passed in 1253.023 seconds against a snapshot of
351 Python sources. Independent inspection accepted the original source and
contract hashes, actual application/CI/CD evidence, raw pair-job receipts and
terminal process/scope cleanup. Health-triggered entry failover served the
original application through the peer in 6.96 seconds. This is scoped virtual
integration acceptance; physical HA and the complete stack/runtime matrix
remain unverified.

This test requires an explicit seven-guest profile. The harness admits only the
original five Kubernetes guests and `edge-a` at `192.0.2.10` plus `edge-b` at
`192.0.2.11`, each with 512 MiB. It checks all seven original KVM monitors,
the exact private TAP inventory and absence of an upstream route. Those two
small guest allocations are a laboratory profile, not production sizing.

The parent has a 12 GiB hard memory cap, an explicit 11.5 GiB memory high
threshold, disabled swap and a 4096-task limit. The seven-guest harness verifies
these memory controls before starting guests. At least 13 GiB must be
available before guest startup. The parent cannot acquire `CAP_SYS_TIME`.
Pinned Chrony synchronizes the seven disposable guest clocks. The original
1800-second outer deadline and 1700-second Kubernetes service limits apply.

The test first runs the original quorum failures, Cilium policies and Gateway,
real Gerrit patch-set build/tests/review feedback, native promotion journal and
actual Argo CD reconciliation. It retains the resulting application Pod UIDs,
container identities and Deployment/Service specifications. The production
[pair coordinator](GATEWAY_EDGE_PAIR.md) then stages and installs the native
entry through verified SSH identities and real PID-1-owned jobs on both edge
guests. PyYAML 6.0.3 and all guest daemon/library artifacts are captured and
hashed; none is installed on the parent host.

HTTPS through `192.0.2.50` must return the exact original application body with
strict certificate and hostname verification. An untrusted CA must fail.
Direct Service/Pod bypass probes must remain denied. The test stops only the
current owner's managed HAProxy service and requires the peer to serve the
same application within 30 seconds, with at most one sampled VIP owner. It
restarts the stopped service, waits for its real health helper and resumes the
completed pair operation. Both guest boot identities and every unaffected
daemon invocation must remain unchanged. Automatic restarts, guest OOM signals
or leftover job units/reservations prevent acceptance.

Use the complete Cilium/native CI/CD command and pinned inputs documented in
`K3S_VM_WORKERS.md`, with these explicit changes:

- Set `CILIUM_GERRIT_NATIVE_EDGE_VM_TEST_RUNTIME=1`.
- Select
  `WorkerRuntimeTests.test_real_cilium_gerrit_native_cicd_with_external_entry`.
- Set `GATEWAY_EDGE_HOST_VM_HAPROXY` to the reviewed guest-compatible
  `haproxy-3.2.26-debian` artifact directory and
  `GATEWAY_EDGE_HOST_VM_KEEPALIVED` to `keepalived-2.3.4-debian`, including their
  pinned Debian dependency archives described in
  [the native guest runbook](GATEWAY_EDGE_HOST_VM_RUNTIME.md).
- Select a new private report path and bounded transient unit for this run.

The parent API proxy's HAProxy input is separate from the guest-compatible
native HAProxy package. Do not substitute the parent executable for the guest
artifact merely because its version string matches.

From the repository root on the prepared Linux KVM host, this is the complete
command. Replace the cache root with the already verified offline artifact
tree; the command does not download or install missing host dependencies.
The new UUID selects a distinct bounded unit and create-only private report.

```bash
entry_lab_artifacts=/absolute/private/validation
entry_lab_run_id=$(cat /proc/sys/kernel/random/uuid)
sudo systemd-run --wait --pipe --collect \
  --unit="platform-k3s-vm-cilium-native-edge-${entry_lab_run_id}" \
  --property=CapabilityBoundingSet=~CAP_SYS_TIME \
  --property=RuntimeMaxSec=1800 --property=MemoryHigh=11776M \
  --property=MemoryMax=12G --property=MemorySwapMax=0 \
  --property=TasksMax=4096 --property=KillMode=control-group \
  --property=UMask=0077 --working-directory="$PWD" \
  /usr/bin/env CILIUM_GERRIT_NATIVE_EDGE_VM_TEST_RUNTIME=1 \
    K3S_VM_TEST_RUNTIME=1 TEKTON_VM_TEST_RUNTIME=1 \
    GERRIT_NATIVE_CICD_VM_TEST_RUNTIME=1 \
    K3S_VM_TEST_TIME_ARTIFACTS="$entry_lab_artifacts/chrony/4.6.1-debian13" \
    K3S_VM_TEST_ARTIFACTS="$entry_lab_artifacts/k3s/v1.35.0+k3s1" \
    K3S_VM_TEST_QEMU="$entry_lab_artifacts/qemu/10.2.1" \
    K3S_VM_TEST_KUBECTL="$entry_lab_artifacts/envtest/v1.35.0/extracted/controller-tools/envtest/kubectl" \
    K3S_VM_TEST_REPORT="$PWD/private/validation/cilium-native-edge-${entry_lab_run_id}.json" \
    CILIUM_VM_TEST_ARTIFACTS="$entry_lab_artifacts/cilium/v1.19.10" \
    CILIUM_VM_TEST_HELM="$entry_lab_artifacts/cilium-lifecycle/helm" \
    K3S_QUORUM_TEST_HAPROXY="$entry_lab_artifacts/gateway-edge/haproxy-3.2.26/haproxy" \
    GATEWAY_EDGE_HOST_VM_HAPROXY="$entry_lab_artifacts/gateway-edge/haproxy-3.2.26-debian" \
    GATEWAY_EDGE_HOST_VM_KEEPALIVED="$entry_lab_artifacts/gateway-edge/keepalived-2.3.4-debian" \
    TEKTON_VM_TEST_RELEASE="$entry_lab_artifacts/tekton-v1.4.0/release.yaml" \
    TEKTON_VM_TEST_IMAGES="$entry_lab_artifacts/tekton/v1.4.0" \
    GERRIT_REVIEW_TEST_ARTIFACTS="$entry_lab_artifacts/gerrit/3.13.10" \
    GERRIT_REVIEW_TEST_JAVA_ARCHIVE="$entry_lab_artifacts/keycloak/OpenJDK21U-jre_x64_linux_hotspot_21.0.13_9.tar.gz" \
    ARGOCD_TEST_ARTIFACTS="$entry_lab_artifacts/argocd/v3.5.0" \
    ARGOCD_TEST_VALKEY="$entry_lab_artifacts/valkey/8.1.11/tls/valkey-server" \
    /usr/bin/unshare --net -- \
    python3 -B scripts/test_k3s_worker_runtime.py \
      WorkerRuntimeTests.test_real_cilium_gerrit_native_cicd_with_external_entry
```

The first seven-guest attempt was deliberately interrupted after a read-only
check in its original live network proved that the old Gerrit fixture guard
rejected the extra edge interfaces. All original processes and the temporary
scope were independently confirmed removed before changing sources. The guard
now admits only the declared seven-guest profile and rejects altered addresses,
memory declarations, extra guests and a missing three-control-plane topology.
That interrupted attempt is not runtime acceptance. The next run passed Cilium
bootstrap, policy/TLS traffic and quorum recovery, then failed after 1030.019
seconds during Gerrit group creation. Its backend proxy hit the fixed
20-second socket timeout while the daemon remained alive. The captured signals
showed no JVM OOM, index error or parent cgroup OOM; the underlying latency cause
is not established. All seven original VM processes and the temporary scope
were independently confirmed removed with all 351 Python source hashes
unchanged.

The explicit seven-guest profile uses a 55-second Gerrit backend deadline
and a 60-second fixture API deadline, retaining the original outer deadlines
and memory limits. Other profiles keep their 20-second request deadlines.
Bounded method/resource-class timings record the next actual operation without
URLs, account names, tokens or payloads. An actual TLS regression commits one
synthetic PUT and deliberately drops its acknowledgment; the fixture must
propagate that uncertain outcome after exactly one request. No write retries
were added. A second full attempt failed after 1040.512 seconds during the first
bootstrap login, with its 55-second backend deadline reached and the daemon
still alive. Its independently retained resource samples showed substantial
memory-pressure stalls and JVM major page faults, without an OOM event. That
evidence does not isolate a single latency cause, and the longer deadline alone
did not qualify the flow. Original VM/JVM processes, the scope and all 351
source hashes were independently audited after termination.

The fixture now waits for the real anonymous
[Gerrit REST version response](https://gerrit-review.googlesource.com/Documentation/rest-api-config.html#get-version)
before attempting account bootstrap. It requires the pinned version and Gerrit
protection prefix, with the same Host/HTTPS forwarding context used by the
reverse proxy. A bare open socket, canonical redirect, error response or wrong
version cannot satisfy readiness. Retries apply only to this anonymous GET.
The first focused readiness attempt exposed a missing canonical Host context;
its live response was HTTP 301. That attempt failed and was cleaned up before
the headers were corrected.

Only the explicit seven-guest profile selects
[`-XX:ActiveProcessorCount=2`](https://docs.oracle.com/en/java/javase/21/docs/specs/man/java.html)
to bound CPU-based JVM pool sizing. The maximum Java heap remains 1 GiB, all
seven guests remain present, and the original parent hard memory cap and runtime
limits remain unchanged. The pinned JVM's reported flags and original daemon arguments
are checked. The report includes the selected profile, real REST readiness time
and bounded API timings.

A separate real pinned-Gerrit probe passed anonymous REST readiness in 11.24
seconds and completed all 23 authenticated review-setup requests, with a maximum
observed request time of 0.311 seconds. It verified the two-CPU/1-GiB-heap JVM
flags. Independent inspection matched all 351 source hashes, the exact probe
code/artifacts and terminal daemon/unit/scope cleanup. That isolated probe does
not include the five Kubernetes guests or the native edge pair. At that stage,
the complete seven-guest flow remained unqualified; the later joined acceptance
below supplies separate evidence for that scope.

A subsequent seven-guest attempt passed real Gerrit REST readiness in 13.105
seconds, all 28 observed setup/review requests (maximum 0.475 seconds), and an
actual worker build with an evidence-checked vote. It then failed after
1057.955 seconds at the native Argo CD repository listener's existing 30-second
readiness deadline. The process remained alive, its log was empty, and the
captured fixed error categories were absent. Independent inspection confirmed
original process/unit/scope cleanup and all 351 source hashes. That result
does not establish why the repository listener did not open.

Startup observations now retain each original Argo CD/Valkey PID, start time,
executable inode match, UID, privilege state, memory and page-fault counters,
with no command arguments, environment values or paths. Successful observation
also requires the same non-root, capability-free processes after reconciliation.
Failure output includes these bounded observations and log sizes.

The new observations passed a separate real native Gerrit/Argo CD/Valkey
reconciliation test in 85.877 seconds. Its cache, repository and controller TCP
listeners opened in 1.004, 1.003 and 1.256 seconds respectively. It then verified
actual ConfigMap reconciliation, repository/cache mTLS, denied promoter writes
and the same non-root, capability-free, no-new-privileges processes before and
after reconciliation. Independent inspection matched all 351 source hashes and
original process/unit/scope cleanup. This case uses a disposable API and modeled
CI worker records; it does not qualify the seven-guest flow or identify the
cause of its previous repository startup delay.

The earlier seven-guest attempts used an 11 GiB soft threshold inside the same
12 GiB hard cap. The revised profile tests 11.5 GiB, leaving 512 MiB before the
hard cap, while retaining all guests, the 1 GiB JVM heap, service readiness
deadlines and outer deadlines. The
[kernel distinguishes memory-high throttling from the hard cap](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-interface-files).
Observed high events and stalls motivated this controlled profile change;
they do not prove it resolves the Argo CD startup failure. Other fixture
profiles retain their existing thresholds. The later joined acceptance below
uses this revised profile without establishing a single cause for earlier failures.

The next seven-guest attempt failed after 1106.993 seconds at the packaged
native promotion step, before the application entry check. Its original cache,
repository and controller listeners opened in 1.504, 1.505 and 2.507 seconds.
All three original processes were still alive, non-root, capability-free and
using the expected executables when promotion failed. The private journal
recorded a generic validation/transport failure at `local-validated`; that
does not identify the underlying cause or prove the soft threshold fixed the
previous startup failure. Independent inspection confirmed all 351 source
hashes and original seven-guest, Gerrit, Argo CD and scope cleanup.

Packaged promotion failures now retain a bounded exception-type and code-location
diagnostic in the private journal. Kubernetes failures retain the existing fixed
signal categories. Exception text, command arguments, environment values,
URLs and local variables are excluded. Regression checks exercise private
exception messages and filenames and verify their exclusion. The diagnostics
preserve failure behavior and do not establish successful promotion by themselves.

The subsequent joined run passed all original quorum, Cilium, Gerrit, Tekton,
native promotion and Argo CD checks before installing the native pair through
the production coordinator. Its report retained 78 original/extended positive
checks, two actual serving application Pods and both external guest identities.
Strict TLS through the VIP returned the original application body; an untrusted
CA failed, and direct Service/Pod bypasses remained denied. The intentional
HAProxy stop caused peer service within 6.96 seconds with at most one sampled
VIP owner. The original guest boot identities and unaffected daemon invocations
remained unchanged, completed pair jobs were reused, and reservations/job units
were removed. These samples do not establish absence of every possible brief
dual-owner interval or continuous zero-loss availability.

The parent peaked at 12351795200 bytes under the same 12 GiB hard cap, with
23711 soft-threshold events and no hard-limit, OOM or OOM-kill event. Gerrit
REST readiness took 12.569 seconds; the maximum observed fixture API request
was 0.4 seconds. The original native cache, repository and controller listeners
opened in 1.255, 1.511 and 2.757 seconds, retaining their verified executable,
PID/start time, non-root identity and privilege properties after reconciliation.
The preceding transport failure did not recur; its cause remains unproven.

An independent audit matched all 351 launch/report/current Python hashes, the
original seven VM identities and allocations, original Gateway/entry contract
bytes, raw pair receipts, actual failover observations and complete original
process/unit/scope cleanup. Its retained report SHA-256 is
`c5c733268a9d92bfd30d064e3ad6ebdfe7a1ebf06170aad465f2c57e30818d21`.
A supplemental read-only capture retained 20 original native promotion journal
phases and independently verified the unmerged-review rejection at its fixed
code location, preflight without a push, promotion with actual controller
verification, and the repeat without another push. Runtime journal mode `0600`
remained a required check. Retained copies are on the Windows workspace and use
Windows ACLs; WSL's reported `0777` for those copies is not a POSIX-permission
claim. Private proofs and inventories remain outside public Git.

Retain the private source-bound report, launch/process observations, raw pair
job receipts and terminal cleanup audit. A successful launcher exit without a
complete report does not establish acceptance. This scope also leaves physical
partitions/fencing, persistent storage, signed artifact publication, daemon
version upgrades, off-platform restore and the full fifty-combination matrix
unverified.
