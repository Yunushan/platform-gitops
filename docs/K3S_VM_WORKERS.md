# Disposable KVM-backed K3s worker acceptance

This opt-in fixture supplies actual scheduler, kubelet, containerd and container
execution for the [multi-runtime expansion](MULTI_RUNTIME_STACKS.md). It creates
one agentless K3s control server and two dedicated worker VMs by default. A
separate quorum selection creates three full control servers and two workers.
It does not borrow
an existing cluster, kubeconfig, Docker socket or company service.

The baseline worker probe is a test prerequisite for real Tekton execution. It
does not submit a PipelineRun, test application code, publish a Gerrit vote or
promote a deployment. Those integration requirements remain open until their
own complete live flows pass. A completed Pod is not a completed platform.

## Isolation and scope

The Linux amd64 runner needs real KVM access, root permission to create a fresh
network namespace and TAP devices, cgroup v2, systemd, OpenSSH, OpenSSL and
`dpkg-deb`. QEMU runs as UID/GID 65534 with the KVM device group, no new privileges
and its syscall sandbox. Its KVM monitor must confirm hardware acceleration;
there is no automatic software-emulation fallback.
Before guest launch, a read-only readiness check waits for `/dev/kvm` to have
its expected root ownership, non-root `kvm` group and mode 0660. This avoids
capturing a temporary device group during cold-start udev setup; the fixture
does not change device ownership or permissions.

The test requires its original `platform-k3s-vm-*.service` cgroup with finite
memory of at most 12 GiB and at most 4096 tasks. The enclosing service also needs
a finite lifetime and control-group cleanup. All guests, disks, cloud-init ISO
images, credentials, monitors and logs are private disposable resources.
The fixture uses `/var/tmp` on an explicitly checked ext4, XFS or Btrfs
filesystem with at least 16 GiB free. RAM-backed temporary storage is refused:
guest disk growth must not consume the cgroup budget reserved for guest RAM.

The namespace must initially contain only loopback. A new bridge and three
owned TAP devices form a laboratory LAN using documentation-reserved addresses.
The optional quorum selection uses five owned TAP devices on that same LAN.
Guests have a default route to that bridge so K3s can select their interfaces;
the namespace has no upstream route or external interface. No image is pulled
from a registry while the guests run. Only checksum-reviewed cached artifacts
are supplied through a read-only ISO.

Each guest has a fresh sealed NoCloud seed, unique SSH host key, private overlay
disk and an administrator authorized by an ephemeral client key. SSH checks
that original host key and uses no ambient user configuration, agent, password
or known-hosts database. Workers receive the original CA-bound K3s join token.
The manager uses verified HTTPS and the actual newly created kube-system UID.
kubectl's home and discovery cache stay inside the original disposable scope;
the fixture does not reuse a host user's discovery cache.

Kubelet kernel tuning occurs inside the guest kernels. The test records and
rechecks the host's six kubelet-related kernel settings; it never changes those
host settings. The fixture uses the `pc` machine with SMM disabled and defaults
to the Nehalem guest CPU model. A workload can explicitly select the host KVM
CPU model when its binary requires features outside the Nehalem profile.
The Allura fixture uses it for MongoDB's AVX requirement. Reports record the
selected model.

## Reviewed artifact inputs

Prepare the following ignored private cache inputs. Every file must match the
hashes committed in `scripts/test_k3s_vm_fixture.py`:

- `K3S_VM_TEST_ARTIFACTS`: K3s `v1.35.0+k3s1`, its matching
  `k3s-airgap-images-amd64.tar.zst`, and the standalone
  `debian-13-genericcloud-amd64.qcow2` whose SHA-512 is committed in the fixture.
- `K3S_VM_TEST_QEMU`: `packages.json` and the exact eighteen Ubuntu package
  archives it inventories. Their metadata manifest is itself pinned. The
  fixture verifies each archive and extracts QEMU 10.2.1, firmware, ISO tooling
  and required libraries into its private scope; it installs no host packages.
- `K3S_VM_TEST_KUBECTL`: the exact envtest Kubernetes 1.35.0 `kubectl` binary.

The K3s artifacts follow the official
[air-gap installation mechanism](https://docs.k3s.io/installation/airgap) and
[version-specific release](https://github.com/k3s-io/k3s/releases/tag/v1.35.0%2Bk3s1).
The guest was acquired from Debian's
[official cloud image distribution](https://cloud.debian.org/images/cloud/trixie/)
with its published SHA-512. The `latest` URL is mutable; retrieve the exact
historical bytes rather than changing a checksum to accept a different image.
The [NoCloud specification](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html)
defines the private seed format. Hash checks establish byte identity; publisher
signature verification and production version/security acceptance are separate
requirements.

The bundled BusyBox probe uses the original image index and Linux amd64
manifest digests from the reviewed air-gap archive. An explicit local alias
makes that immutable digest available to each worker without registry access.

## Guest clocks during outage tests

The quorum fixture requires `K3S_VM_TEST_TIME_ARTIFACTS` pointing to a private
cache containing Debian's `chrony_4.6.1-3+deb13u2_amd64.deb` and its extracted
`extracted/usr/sbin/chronyd` and `extracted/usr/bin/chronyc`. The package and both
binaries must match the SHA-256 pins in `scripts/test_k3s_time_fixture.py`.
Extraction does not install a host package. The default three-guest worker
case can also select this input explicitly.

Add `--property=CapabilityBoundingSet=~CAP_SYS_TIME` to the enclosing disposable
service whenever this input is selected. A private NTP relay binds only the
isolated laboratory bridge and runs with Chrony's `-x` option. The fixture
checks that its effective, permitted, bounding, inheritable and ambient
capability sets cannot change the parent clock. Existing host time services
are not changed. Guest-only clients use the relay and may step their disposable
guest clocks. There is no upstream NTP route.

Guests must synchronize before K3s creates certificates, after paused controls
resume, and at the final health check. Readback requires the original relay,
normal leap status, the expected stratum, settled correction and guest wall
times bounded by the parent observation interval. The receipt binds the
artifact hashes and initial/final clock observations. Passing a relay-only
probe does not establish guest synchronization or the complete CI/CD flow;
those runtime acceptance checks remain separate.

This requirement addresses a reproduced certificate-time failure: a paused
control guest retained a stale wall clock while the webhook serving CA was
created on another guest. TLS certificate validation stays enabled. See the
[Chrony daemon options](https://chrony-project.org/doc/4.6/chronyd.html) and
[tracking/readiness commands](https://chrony-project.org/doc/4.6/chronyc.html).

On 2026-11-08 the explicitly time-enabled three-guest worker case passed in
99.171 seconds. A separate audit matched all 345 Python source hashes,
validated all three initial and final clock observations and parent capability
sets, and confirmed removal of the original unit, guest/relay processes and
private scopes. Its private run log SHA-256 is
`a46d4c71199c5c091388dc887d05ccbd42696d665fe5014a1b7297d7d7c61b07`.
This proves the clock-enabled worker prerequisite. The later post-outage
Tekton and combined Cilium/native CI/CD acceptances are described separately.
The probe never uses a floating tag for execution.
Node readiness alone is insufficient: the fixture separately waits for the
bundled BusyBox and sandbox images to appear in each worker's containerd before
creating that alias and running probes.

## Execute explicitly

Normal validation discovers the test but skips VM startup. To run the hardware
fixture on a prepared disposable Linux runner:

```bash
sudo systemd-run --wait --pipe --collect \
  --unit="platform-k3s-vm-$(cat /proc/sys/kernel/random/uuid)" \
  --property=RuntimeMaxSec=1200 --property=MemoryMax=8G \
  --property=TasksMax=4096 --property=KillMode=control-group \
  --property=UMask=0077 \
  --working-directory="$PWD" \
  /usr/bin/env \
    K3S_VM_TEST_RUNTIME=1 \
    K3S_VM_TEST_ARTIFACTS=/absolute/private/cache/k3s/v1.35.0+k3s1 \
    K3S_VM_TEST_QEMU=/absolute/private/cache/qemu/10.2.1 \
    K3S_VM_TEST_KUBECTL=/absolute/private/cache/envtest/v1.35.0/extracted/controller-tools/envtest/kubectl \
    /usr/bin/unshare --net -- \
    python3 -B scripts/test_k3s_worker_runtime.py \
      WorkerRuntimeTests.test_real_k3s_virtual_workers
```

`K3S_VM_TEST_REPORT` optionally selects a **new** ignored `private/` file within
the checkout. A receipt is written only after guests and temporary disks are
removed and the first-party Python source hashes still match. Freeze Python
sources during acceptance. Failed operations produce no success receipt.

## Actual Tekton controller and manual pipeline case

An additional opt-in case uses those same disposable workers to install the
reviewed Tekton Pipelines 1.4.0 release through `deploy_tekton.py`. It first
checks admission and installs without starting a job, then explicitly requests
one infrastructure PipelineRun. Its TaskRun, Pod, immutable step image, worker
placement and result must be verified through the live API. The fixture never
writes controller status or substitutes modeled workers.

Additional ignored cache inputs are:

- `TEKTON_VM_TEST_RELEASE`: the original `release.yaml`, matching the committed
  SHA-256 in `scripts/test_tekton_worker_runtime_fixture.py`.
- `TEKTON_VM_TEST_IMAGES`: eight original Linux controller/helper OCI archives
  in `images/<original-root-digest>.tar`, plus `python-image/python.tar` for the
  committed Python image digest. The fixture verifies the original root,
  Linux amd64 manifest, configuration and every reachable layer. Corrupt,
  missing, extra, linked or excessive archive members are rejected before
  import. It does not treat a mutable local download receipt as image identity.

Use the command above with the exact test selection
`WorkerRuntimeTests.test_real_tekton_pipeline_on_virtual_workers`, add
`TEKTON_VM_TEST_RUNTIME=1` and those two absolute cache paths. For this larger
case, replace `MemoryMax=8G` with `MemoryHigh=10G`, `MemoryMax=12G` and
`MemorySwapMax=0`. Provide sufficient otherwise-unused host memory. Keep the
existing task, lifetime and cleanup limits. An observed 8 GiB cgroup OOM during
image import produced no acceptance receipt; it was not a pipeline success.
On runners with a RAM-backed `/tmp`, moving the original disposable scope to
the checked disk workspace is required as well as retaining finite memory limits.

The reviewed shell helper override selects the immutable bundled BusyBox;
other controller/helper images remain the originals from the pinned release.
Verified archives are copied over pinned SSH connections and imported into each
worker's containerd. No registry access is required. The source hash receipt
and cleanup rules are identical to the baseline case.

This case exercises the manual infrastructure canary, not a source-triggered
application build. Publisher signatures, review feedback, artifact publication,
deployment handoff and independent failure-domain HA require their own evidence.

## Accepted laboratory execution

The explicit Tekton case passed on 2026-11-07 with the pinned K3s 1.35.0+k3s1,
Tekton 1.4.0 and image inputs above. It observed two dedicated real worker
Nodes and two completed restricted probe Pods, imported nine verified OCI
archives into each worker and completed one manual infrastructure PipelineRun
through the production installer and live controllers. Namespace preview was
read-only, controller installation created no implicit job, all deployments
became ready and webhook registrations had active fail-closed rules and CA
trust before canary creation.

The retained private receipt matched all 335 first-party Python source files
at acceptance and was emitted only after cleanup. An independent terminal
check found the original service inactive with no main process, no QEMU guests
and no remaining fixture scope in either temporary directory. Host kernel
settings remained unchanged. The terminal test log SHA-256 is
`f15a8bb6da32f1862cd5ddbd9c674d1ff8b5f182458e09178b1051bec0ecfa95`.

This evidence does not raise application CI, Cilium integration, physical-node
HA or full-stack readiness flags. The protocol-only Gerrit/Argo CD fixture
still uses modeled Tekton records. The separate cases below provide their own
actual worker and serving-handoff evidence; this infrastructure receipt does
not establish those results or continuously deployed CI/CD services.

## Real Gerrit patch-set worker case

The further opt-in selection
`WorkerRuntimeTests.test_real_gerrit_patchset_build_and_review_on_virtual_workers`
adds `GERRIT_TEKTON_VM_TEST_RUNTIME=1` to the Tekton runner environment. It also
requires `GERRIT_REVIEW_TEST_ARTIFACTS` (the pinned Gerrit 3.13.10 distribution)
and `GERRIT_REVIEW_TEST_JAVA_ARCHIVE` (the pinned Java archive) documented in
[the Gerrit runtime runbook](GERRIT_REVIEW_RUNTIME.md). Select only that case;
the receipt path must still be new. Retain the same finite service limits and
freeze all first-party Python sources during execution.

This case creates a synthetic native Gerrit server on the exact owned VM
laboratory bridge. It rejects extra interfaces or an upstream route, checks
the original KVM monitors, and uses a fresh TLS certificate for the laboratory
address. Its backend remains on loopback. Gerrit data uses the checked
disk-backed temporary filesystem and is removed after its original process
stops.

The case packages the actual fixed Gerrit submission and reporting helpers,
creates their reviewed RBAC, and uses separate TokenRequest credentials for
their API calls. The source reader and result bot are separate native Gerrit
accounts. Only checkout mounts the read Secret. The build service account has
no automatic API token; application tests check that neither checkout nor API
credentials are available in the build container. Submitter and reporter Secret
reads must be denied by the real API.
The access-review check posts to the native authorization endpoint and requires
an explicit negative result without an evaluation error. It does not grant
cluster-wide CRD listing to satisfy kubectl's generic validation fallback for
that virtual resource.

Submission uses real current-patch-set discovery, immutable reservation/run
receipts, the production server dry-run, original UIDs and duplicate detection.
Actual Tekton controllers must execute exact-ref checkout and two application
tests. Reporting must correlate the live TaskRun, Pod, node and step evidence
before publishing and reading back the original bot's Verified vote. Repeating
the result must not post again. Publishing a new untested patch set must make
the old result ineligible and must not copy its positive vote.
The same case then publishes a deliberately failing test on another patch
set. Checkout must exit zero, build must exit one, and the original TaskRun
and Pod must be terminal failures. Three tests must execute with one deliberate
failure. The reporter must publish/read back `Verified -1`, refuse to claim
successful worker execution, and leave the negative result unchanged on replay.

The helpers execute natively with scoped API credentials; this case does not
deploy their long-running service or container workloads, prove continuous
event delivery, publish application artifacts or perform a deployment handoff.
Its source package records native helper hosting with an explicit K3s target.
The pinned Python worker image is not evidence for a separately deployable
bridge container with kubectl.

This extended case passed again on 2026-11-07 in 365.273 seconds after adding
the separate combined handoff case. Its receipt matched all 337 first-party
Python files. Actual checkout and build steps exited zero,
and the build log confirmed two executed application tests. The production
reporter correlated original run, TaskRun, Pod and worker UIDs before publishing
and reading back the native Gerrit bot's vote. Submission and reporting replay,
denied Secret reads, stale-result refusal and absent approval copying passed.
The subsequent deliberate build failure produced the expected negative vote,
with separate original run/TaskRun/Pod/worker handles retained in the receipt
and no successful-worker claim for that failed run. Negative-result replay
did not post again.
The shared bounded volume mounts directly at the repository path, retaining
filesystem-group write access and Git trust restricted to that exact directory.

The receipt was emitted after Gerrit and guest cleanup. Independent inspection
confirmed the original service inactive with main PID zero, no QEMU processes
and no remaining VM scope in either temporary directory. Host kernel tunables
remained unchanged. The terminal log SHA-256 is
`3363c9bf56a08e104c138e7f7cdfaff622f74b427ad0427c108e3d349f8df823`.

This proves the manually invoked native helper path for this synthetic patch
set and its deliberate failure successor. A deployed continuous bridge, application artifact
publication, continuous CI/CD, independent failure domains and the
fifty complete stack/runtime combinations still require acceptance. The receipt
keeps broad application-CI, continuous-event, physical-HA, Cilium, publisher
signature and full-stack flags false.

## Combined real-worker CI and serving Argo CD case

The separate opt-in selection
`WorkerRuntimeTests.test_real_gerrit_worker_ci_and_argocd_serving_handoff`
uses `GERRIT_TEKTON_ARGOCD_VM_TEST_RUNTIME=1` with the same explicit KVM/Tekton
inputs. It additionally requires `ARGOCD_TEST_ARTIFACTS` and
`ARGOCD_TEST_VALKEY`, the pinned native Argo CD 3.5.0 distribution/wrappers and
TLS-enabled Valkey binary described in
[the Gerrit handoff runbook](GERRIT_GITOPS_HANDOFF.md). Select only this case
and a fresh private receipt path. Keep the original service resource limits
and freeze Python sources throughout execution.

This case prepares a fresh Gerrit project with a deployment branch and separate
source-reader, result-bot and deployment-writer accounts. Its tested patch set
contains a pinned, restricted two-replica HTTP Deployment, content ConfigMaps
and a Service. Three actual worker tests include a check of that source manifest.
The native reporter must publish/read back the actual successful worker result
before the deployment handoff can proceed. An unmerged change must be refused;
after exact fast-forward merge, preflight must leave the deployment branch
unchanged and explicit promotion must use a normal fast-forward push.

Native repository/application-controller and mTLS cache processes use their
own private scope, the original laboratory API, namespace-scoped deployment
permissions and a promoter identity that cannot patch the Application. The
test never substitutes fabricated Application health or completed worker
records. Argo CD must reconcile the promoted revision before actual deployment,
ReplicaSet, Pod and worker UIDs, readiness, image identity, credential absence
and Service EndpointSlices are checked. HTTP requests through both private
worker entry points must return the source-controlled response.

This combined case passed on 2026-11-07 in 421.022 seconds. Its separate receipt
matched all 337 first-party Python files. Three worker application tests passed,
the native bot result was read back, an unmerged promotion was refused, and
read-only preflight left the deployment branch unchanged. Explicit promotion
fast-forwarded the exact tested/merged revision; replay performed no additional
push. Native Argo CD observed that revision with Synced/Healthy and a successful
operation. Both actual application Pods were Ready on distinct worker Nodes,
and their original Deployment/ReplicaSet/Pod chain, pinned image, credential
absence and ready Service endpoints passed inspection. Requests through both
private worker NodePorts returned the expected source-controlled response.
The promoter's Application patch was denied by the real API.

Independent inspection confirmed the original service inactive with main PID
zero, no QEMU/Argo CD/Valkey processes and no retained VM, Gerrit, native Argo CD
or API fixture scope. Host kernel tunables remained unchanged. The terminal
log SHA-256 is
`a173a5eebfc742c590e18f755950debc32365bf56e979d0d0fa577f3f7b055da`.

The Argo CD helper does not create or remove a separate API in this case; the
outer VM fixture owns the API and records its eventual guest/scope cleanup.
The receipt therefore distinguishes native-process scope removal from the
standalone API fixture's cleanup flag. This manual helper flow does not
establish a continuously deployed event bridge,
image build/registry publication, physical-node HA, Cilium, the requested shared
foundation or any complete stack/runtime combination.

## Native Gerrit watch-service case

The separate opt-in case
`WorkerRuntimeTests.test_real_gerrit_native_watch_services_and_worker_replay`
adds `GERRIT_NATIVE_WATCH_VM_TEST_RUNTIME=1` to the explicit KVM/Tekton/Gerrit
inputs above. Select only this case and a new private receipt. It does not
require Argo CD artifacts and does not run a deployment handoff.

The laboratory starts two actual transient systemd services from the rendered
native commands and hardening settings. Each has a separate ephemeral identity,
private read-only program and credential mounts, the original laboratory network
namespace, a 512 MiB memory limit, a 64-task limit and a finite runtime. The test
uses systemd's [ephemeral identity and credential facilities](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)
to map private inputs to the unchanged package paths without persistent host
accounts or unit enablement. Those identity/mount mappings are fixture-specific;
they do not establish production installation with the default static accounts.

Actual packaged `--watch` entry points must discover and submit the source
patch set and publish the evidenced native Gerrit result. The test does not
create the run or post its initial vote through an in-process substitute.
After forcibly terminating each original main process, the rendered restart
policy must create a new invocation and PID. Subsequent polling must preserve
the original run/receipts and report duplicates without another vote. A new
deliberately failing patch set must be discovered and executed automatically,
then receive a native negative vote. Original UIDs, read-only mounts, non-root
credentials, no-new-privileges, empty capabilities and unchanged unit fragments
are checked while both services run. All native units and private scopes must
be gone before aggregate acceptance is recorded.

This case passed on 2026-11-07 in 456.458 seconds with local systemd 259. Its
separate receipt matched all 338 first-party Python files. The packaged native
entry points submitted and reported both patch sets. Two tests passed on the
first worker build; the second executed three tests with the deliberate failure
and received the native bot's negative vote. Each service underwent one forced
main-process termination and a verified systemd restart with a new PID and
invocation. Later polling returned the existing run and published result without
creating duplicates. The two services retained distinct non-root UIDs, their
original unit fragments, fixed commands, read-only mounts and hardening checks.

Independent inspection found the original parent and both transient services
not loaded, inactive and without main PIDs. Their credential directories and
transient unit files were absent, no watch units remained, and no QEMU/native
runtime processes or fixture scopes remained. Host kernel tunables were
unchanged. The terminal log SHA-256 is
`2fbc213cd17be10cb23f69e176f1a0d37f09ef8686a0ad73eb824331ad2042e8`.

The inspector explicitly requests empty systemd properties, so an empty
capability bounding set is verified rather than inferred from an omitted field.
The earlier inspection failure produced no accepted receipt and was cleaned up
before this separate run.

This receipt establishes bounded native polling and process-restart behavior.
The separate manual serving receipt does not combine those services with an
automatic deployment handoff. Polling/restart acceptance also
does not certify fleet deployment, physical HA, push-event delivery, signed
artifact publication or the fifty complete stack/runtime combinations.

## Native polling through explicit promotion CLI

The further opt-in selection
`WorkerRuntimeTests.test_real_gerrit_native_watch_and_promotion_cli_serving_flow`
uses `GERRIT_NATIVE_CICD_VM_TEST_RUNTIME=1` with the same KVM/Tekton/Gerrit inputs
and the Argo CD/Valkey artifacts from the combined serving case. It joins the
packaged native polling/reporting services, real worker execution, human review
and exact merge, the packaged promoter CLI and actual Argo CD HTTP serving in
one private laboratory. Select only this case and a new receipt path.

The services must submit and report the tested patch set automatically, then
survive main-process termination and replay without another run or vote. A
third ephemeral non-root identity invokes the exact packaged promoter command
with separate source-read, deployment-write, CI-read and controller-read inputs.
Its dedicated CI reader can get the required evidence, named namespace
identities and the two original worker Nodes. Actual authorization reviews
must deny receipt creation, pipeline/status patching and Secret reads; denied
receipt creation and Secret-get calls check that boundary through the API.
An unmerged invocation must fail without advancing Git. After explicit review
and exact merge, `--check-server` must leave Git unchanged, `--apply` must
fast-forward and observe actual reconciliation, and another apply must observe
the existing promotion without pushing again. Each invocation writes a new
private journal whose phase, contract, ownership and permissions are checked.

The promoter sees read-only code/credentials and one writable journal mount
inside the original private fixture scope. Every invocation receives a separate
view of its own output directory; retained journals cannot be accessed through
another invocation's mount. Transient services and credential stores must be
removed before aggregate acceptance. The ordinary serving checks still require
two Ready worker Pods, original ownership chains, exact images, credential
absence, ready Service endpoints and the expected HTTP response through both
workers.

This joined case passed on 2026-11-08 in 455.406 seconds with dedicated CI and CD
readers provisioned from the optional production access renderer. Its separate
receipt matched all 340
first-party Python files. Three application tests executed on the actual worker
before the native bot published its result. Both watch services passed forced
restart/replay checks. The packaged promoter CLI then ran four times under
non-root identities distinct from the running watch services: rejected unmerged
apply, read-only preflight, successful apply and an already-promoted apply with
no further push. All four journals had the original contract, expected phase,
non-root ownership and private permissions. CI receipt writes, pipeline/status
mutation and Secret reads were denied; the real write/Secret requests failed
as required. The controller-read identity also failed its Application patch.
The fully prepared access package regenerated exactly, and its ten rendered
ServiceAccount/Role/binding objects matched the actual objects on the original
cluster before promotion. Additional actual authorization reviews denied CI
listing, reads outside the job namespace and unlisted Nodes; CD observation
denied unrelated Applications, Application listing, Secrets and Nodes. The
package creates neither token nor kubeconfig, and its rendered status alone
continues to report live authorization and deployment as unverified.

Argo CD observed the exact promoted revision, and both application Pods passed
their actual ownership, image, readiness, credential and HTTP serving checks.
Independent inspection confirmed the original parent, two watch units and four
promoter units not loaded, inactive and without main PIDs; their credential
directories and transient files were absent. No native/VM processes or fixture
scopes remained, and host kernel tunables were unchanged. The terminal log
SHA-256 is
`95867598fb3d8c6c58cc557a03a4964d1ac2b12ab5e045a7f8ec645e3dc54e82`.

The separate polling/manual-serving receipts cannot substitute for this joined
case's evidence. Merge and promotion remain explicit decisions; this case does not claim
unattended release authorization, artifact build/publication, fleet installation,
physical HA, Cilium or completion of any full stack/runtime combination.

## Three control-plane quorum and fixed API entry

The optional
`WorkerRuntimeTests.test_real_three_control_plane_quorum_and_api_entry` case
uses `K3S_QUORUM_VM_TEST_RUNTIME=1`. It creates five original KVM guests: three
full K3s servers with embedded etcd and two separate worker agents. Server
taints keep ordinary application Pods off the control plane. The initial server
uses `--cluster-init`; the other servers and workers join through a fixed API
address included in each server's TLS SANs. Critical networking/encryption flags
are identical across servers, following the upstream
[embedded-etcd procedure](https://docs.k3s.io/datastore/ha-embedded).

Supply the same K3s/QEMU/kubectl caches plus `K3S_QUORUM_TEST_HAPROXY`, pointing
to the reviewed local HAProxy 3.2.26 binary with SHA-256
`3137dc28d1fa8095ce2610e5736d67cbbdec3a3b15af49bb3ba8631ebe5904ed`.
That binary is a pinned local fixture build, not a publisher-signature claim or
production upgrade recommendation. Use a new bounded
`platform-k3s-vm-*.service`, a new network namespace, `MemoryMax=12G`,
`MemorySwapMax=0`, `TasksMax=4096`, a finite lifetime, `KillMode=control-group`,
`UMask=0077`, and a new private `K3S_VM_TEST_REPORT` path. Select only this test;
do not run another VM fixture concurrently against that resource budget.

```bash
sudo systemd-run --wait --pipe --collect \
  --unit="platform-k3s-vm-quorum-$(cat /proc/sys/kernel/random/uuid)" \
  --property=CapabilityBoundingSet=~CAP_SYS_TIME \
  --property=RuntimeMaxSec=1200 --property=MemoryHigh=10G \
  --property=MemoryMax=12G --property=MemorySwapMax=0 \
  --property=TasksMax=4096 --property=KillMode=control-group \
  --property=UMask=0077 --working-directory="$PWD" \
  /usr/bin/env \
    K3S_QUORUM_VM_TEST_RUNTIME=1 \
    K3S_VM_TEST_TIME_ARTIFACTS=/absolute/private/cache/chrony/4.6.1-debian13 \
    K3S_QUORUM_TEST_HAPROXY=/absolute/private/cache/haproxy-3.2.26/haproxy \
    K3S_VM_TEST_ARTIFACTS=/absolute/private/cache/k3s/v1.35.0+k3s1 \
    K3S_VM_TEST_QEMU=/absolute/private/cache/qemu/10.2.1 \
    K3S_VM_TEST_KUBECTL=/absolute/private/cache/envtest/v1.35.0/extracted/controller-tools/envtest/kubectl \
    K3S_VM_TEST_REPORT="$PWD/private/validation/quorum-$(cat /proc/sys/kernel/random/uuid).json" \
    /usr/bin/unshare --net -- \
    python3 -B scripts/test_k3s_worker_runtime.py \
      WorkerRuntimeTests.test_real_three_control_plane_quorum_and_api_entry
```

HAProxy runs as the dropped fixture identity with a private configuration and
TCP checks against the three original servers. It forwards TLS without
terminating the client's verified API session. No host proxy service or package
is installed. Code-directory traversal is explicit under the restrictive umask;
the enclosing private scope remains mode 0700. The lab uses one proxy process
to test a stable API address; redundant proxy hosts/VIP failover are a separate
unverified requirement, as described by the upstream
[load-balancer design](https://docs.k3s.io/datastore/cluster-loadbalancer).

The fixture enables the embedded etcd JSON gateway on K3s's existing loopback
HTTPS listener at port 2382. Port 2379 remains the gRPC listener, as specified
by the [pinned K3s implementation](https://github.com/k3s-io/k3s/blob/v1.35.0%2Bk3s1/pkg/etcd/etcd.go#L966-L1002).
Only fixed member-list and maintenance-status reads are made, from inside the
original guests using their original dedicated client certificate and CA;
no etcd key is exported and TLS verification stays enabled. Three distinct
non-learner member IDs must map exactly to the
original peer/client addresses, and each status must identify the expected
member and common leader. This corroborates actual membership rather than
inferring a quorum from Kubernetes Node labels.

The test pauses the original leader's entire VM through its private QMP
monitor. The two remaining voters must elect another leader and acknowledge a
new ConfigMap through the unchanged verified API endpoint. It then pauses the
new leader, requires the lone remaining voter to report no leader, and requires
a fresh write to receive no acknowledgment. Two existing worker HTTP endpoints
must continue to serve during both outages. A timeout does not prove that a
write can never commit: the uncertain write is attempted once and its actual
presence is observed after recovery, without retrying it.

Both paused VMs resume in a `finally` block. The original three member IDs and
five Node UIDs must remain, new writes must succeed, the acknowledged earlier
objects must retain their UIDs, and the original serving Pods must still be
Ready with HTTP responses through both workers. Existing guest, namespace,
process, private-scope, source-hash and unchanged host-kernel checks still apply.
Acceptance is only recorded after the complete case passes. This is a virtual
control-plane outage test, not physical failure-domain, redundant API-entry,
Cilium, Ceph, upgrade, snapshot-restore or full-stack acceptance.

This case passed on 2026-11-08 in 209.040 seconds. Its receipt matched all 341
first-party Python files. The original leader VM and then the newly elected
leader VM were paused; the remaining majority acknowledged a write through the
same verified endpoint, while the lone voter acknowledged none. Both original
worker HTTP Pods continued serving, and the original three voter IDs and five
Node UIDs survived recovery. The uncertain write was absent after recovery in
this run; this observation is not a general guarantee about timed-out writes.
Independent inspection confirmed the original unit not loaded/inactive with
MainPID zero, no remaining guest/proxy processes, and no private VM/proxy scopes.
Host kernel settings stayed unchanged. The terminal log SHA-256 is
`12420e08fdd2a03b3d8ebe53101f5056b019896b2eb6977219bd00ec2fb568d7`.
Earlier failed startup/probe attempts produced no accepted receipt and were
cleaned up before this separate complete run.
The original one-control-server/two-worker case also passed afterward in
93.844 seconds against the same 341-file source snapshot. Its separate cleanup
audit found the original unit, VM processes and private scope removed. That
regression log SHA-256 is
`2997356c655e435aa6ee2def3defddae3b1c5c77e0c496eeed535df9a413ced0`.

## Optional five-node Cilium datapath and Gateway case

`WorkerRuntimeTests.test_real_cilium_five_node_network_policy_and_gateway`
uses three full embedded-etcd control-plane guests and two separate workers.
It selects `--flannel-backend=none --disable-kube-proxy` at bootstrap on all
servers and installs the reviewed package through the production Cilium
installer. It never migrates an existing Flannel cluster. All five original
guests receive digest-checked Cilium agent, operator and Envoy OCI archives
from the private cache, with no upstream route or registry access during the
test. Before containerd starts, a guest-local K3s `config-v3.toml.tmpl`
extends the upstream base template with explicit CNI binary and configuration
directories. All five generated containerd configurations must match the
installer plan. Flannel-disabled bootstrap does not establish that the
Flannel-specific data paths remain containerd's defaults; the pinned
[K3s template](https://github.com/k3s-io/k3s/blob/v1.35.0%2Bk3s1/pkg/agent/templates/templates.go)
only emits those paths when configured. No host containerd setting is changed.

Run it from the checkout with the reviewed private caches:

```bash
sudo systemd-run --wait --pipe --collect \
  --unit="platform-k3s-vm-cilium-$(cat /proc/sys/kernel/random/uuid)" \
  --property=CapabilityBoundingSet=~CAP_SYS_TIME \
  --property=RuntimeMaxSec=1200 --property=MemoryHigh=10G \
  --property=MemoryMax=12G --property=MemorySwapMax=0 \
  --property=TasksMax=4096 --property=KillMode=control-group \
  --property=UMask=0077 --working-directory="$PWD" \
  /usr/bin/env \
    CILIUM_VM_TEST_RUNTIME=1 \
    K3S_VM_TEST_TIME_ARTIFACTS=/absolute/private/cache/chrony/4.6.1-debian13 \
    CILIUM_VM_TEST_ARTIFACTS=/absolute/private/cache/cilium/v1.19.10 \
    CILIUM_VM_TEST_HELM=/absolute/private/cache/cilium-lifecycle/helm \
    K3S_QUORUM_TEST_HAPROXY=/absolute/private/cache/haproxy-3.2.26/haproxy \
    K3S_VM_TEST_ARTIFACTS=/absolute/private/cache/k3s/v1.35.0+k3s1 \
    K3S_VM_TEST_QEMU=/absolute/private/cache/qemu/10.2.1 \
    K3S_VM_TEST_KUBECTL=/absolute/private/cache/envtest/v1.35.0/extracted/controller-tools/envtest/kubectl \
    K3S_VM_TEST_REPORT="$PWD/private/validation/cilium-$(cat /proc/sys/kernel/random/uuid).json" \
    /usr/bin/unshare --net -- \
    python3 -B scripts/test_k3s_worker_runtime.py \
      WorkerRuntimeTests.test_real_cilium_five_node_network_policy_and_gateway
```

The Cilium artifacts are the actual 1.19.10 chart, the complete Gateway
API 1.4.1 standard export and the three immutable images selected by the fixture.
Checksums and OCI graph validation establish cached content identity; publisher
signature verification remains a separate requirement. Do not run this case
concurrently with another VM test.

The case requires a read-only missing-Namespace preview, explicit owned
Namespace bootstrap with UID recorded in the lifecycle ledger, CNI coverage on
all five nodes, and subsequent read-only installer verification. It also runs
the quorum outage/recovery checks above with Cilium providing the network.
Six original restricted application Pods then exercise direct cross-worker
Pod IPs and a ClusterIP before and after an ingress NetworkPolicy. Allowed
clients must keep serving while denied clients receive no response; successful
exec markers and healthy original backends distinguish policy denial from a
failed test command. From each allowed Pod, the case also resolves the full
Service name through the default `kube-dns` resolver, matches the returned A
record against the Service's ClusterIP, and fetches the expected response by
name. It repeats those checks after restarting the disposable CoreDNS
Deployment, with new ready DNS endpoint Pod UIDs and the original DNS Service
UID and IP. A real Gateway and HTTPRoute must be accepted, and HTTPS
through each original serving worker must return the expected backend body
with the fresh CA and hostname verified. Policy denials are checked again after
Gateway access is enabled.

For the mixed-listener variation, cache the matching
[1.4.1 TLSRoute CRD](https://raw.githubusercontent.com/kubernetes-sigs/gateway-api/v1.4.1/config/crd/experimental/gateway.networking.k8s.io_tlsroutes.yaml)
as `gateway-api-v1.4.1-tlsroute.yaml` alongside the standard export. Use the
same bounded command with `CILIUM_VM_TEST_PASSTHROUGH=1` in place of
`CILIUM_VM_TEST_RUNTIME` and select
`WorkerRuntimeTests.test_real_cilium_mixed_gateway_preserves_backend_certificate`.
The fixture checks the CRD digest, installs it through the owned CNI package,
and serves a second HTTPS backend with its own CA. It compares the exact peer
certificate and HTTP body from the passthrough listener on each serving node
and keeps the terminating HTTP route and policy checks.

The mixed-listener run passed on 2026-11-15 in 807.662 seconds. The receipt
matched all 554 first-party Python sources. Both serving workers returned the
backend's exact certificate and response. The original terminating route and
network-policy tests passed as well. The VM service and its cgroup were removed.
The terminal log SHA-256 is
`9bfd7e97e501fbb1277166f7272577961af9b15a48b7366c4d4706dfa906d2a5`.

The probe CA explicitly declares critical CA constraints and certificate-signing
key usage; the leaf declares server authentication, hostname SAN and critical
leaf/key-usage constraints. A separate local HTTPS regression uses the same
certificate generator and client with strict X.509 verification, verifies the
correct hostname, and rejects both a wrong hostname and an untrusted CA.
Verification flags are not relaxed to accommodate malformed test certificates.

This is an opt-in acceptance case, not a claim that it has passed merely
because it exists. A successful complete receipt and independent guest/unit
cleanup audit are required. Even a passing virtual case does not prove
redundant external entry, independent physical failure domains, Cilium patch
upgrades, Ceph, backup restoration or complete-stack acceptance.

The complete case passed on 2026-11-08 in 574.023 seconds. Its accepted receipt
matched all 342 first-party Python files. It covered the production installer,
five original Node identities, three original etcd voter identities, quorum
outages/recovery with worker HTTP continuity, six original application Pods,
policy positive/negative traffic and verified TLS through both serving workers.
Independent inspection confirmed the original unit not loaded/inactive with
MainPID zero, no remaining guest/proxy processes and no private VM/proxy scopes.
Host kubelet kernel settings remained unchanged, and the isolated lab had no
upstream route. The terminal log SHA-256 is
`81e78db5867a9dd75ea387ab050a3bc44799f0483276b70fd2ce355ea8b26fa4`.
Earlier failed diagnostic attempts produced no accepted receipt. This result
establishes the scoped virtual CNI/policy/HTTPS case; physical HA, redundant API
entry and the complete stack/runtime combinations remain unverified. The
legacy broad foundation-integration flag also remains false; the receipt uses
separate `real_cilium_*` fields for the capabilities actually exercised here.
The default one-control-server/two-worker Flannel case also passed afterward
in 89.699 seconds against the same 342-file snapshot. Its independent audit
confirmed removal of the original unit, VM processes and private scopes. That
regression log SHA-256 is
`cf36ee63898c471e8bda05ffdbc9bf80bdc12682264493791a1afb9a35df1a69`.

## Optional Cilium and native CI/CD application case

`WorkerRuntimeTests.test_real_cilium_gerrit_native_cicd_and_application_gateway`
composes the five-node Cilium case with the native Gerrit polling/reporting,
real Tekton worker jobs, explicit promotion CLI and actual Argo CD reconciliation
case above. It then places the original Argo-managed application behind a second
Gateway with a separately verified hostname/CA. The Deployment, Service, Pod
specs, original UIDs and running container IDs must remain unchanged. Its named
Service target port stays named; the production Gateway preflight must resolve
it against the original endpoint Pods. Direct application traffic from both
test clients must work before the Gateway policy and be denied afterward,
while HTTPS through both serving workers keeps returning the tested body.

This case passed scoped runtime acceptance on 2026-11-08 in 1055.592 seconds.
The private receipt matched all 343 first-party Python sources. An independent
audit confirmed all required Cilium, quorum, native polling/voting, production
promotion CLI, Argo CD and original-application Gateway checks, then verified
removal of all seven original transient units, credential stores, native/VM
processes and private runtime scopes. The retained log SHA-256 is
`334659708c2cb2b6db6b0cde6fd2f5a63e3120855565859532e08e3cdaed7bd9`.
Both workers served the actual Git-tested application body over verified TLS;
direct Service/Pod bypass traffic was denied with the original application
Pods, container IDs and Deployment/Service specs preserved. The parent recorded
no OOM event or kill. All three control services passed direct API and stable
PID checks after recovery and at completion. Their restart counts stayed zero
in this historical run; it did not exercise forced process failure/restart.
The later combined receipt below covers that additional case. Physical HA,
registry publication and complete-stack claims remain
false. This case remains opt-in.
A subsequent default three-VM regression passed in 91.857 seconds against the
same 343-source set, with its own independent source/unit/process/scope audit.
Its retained log SHA-256 is
`a771c187b0e8f728e298fdc77a6f6ad8c2f48bf14318cf12e2dd6f10af1efb00`.
Those receipts predate the additional forced-process recovery check. A focused
five-VM Flannel quorum case accepted that scenario in 275.593 seconds against
343 Python sources, with an independent source/identity/cleanup audit. Its
retained log SHA-256 is
`c326315c813fe42c0dbde2539c045e93405349a6f450949c39a2dc6be04665d9`.
The selected original service restarted exactly once. All 113 paired worker
HTTP samples succeeded; the 28.811-second recovery check includes the required
15-second stability window and is not a physical-platform RTO claim. This
focused result alone does not verify the failure scenario under Cilium/native
CI/CD.

The time-enabled combined case subsequently passed in 1166.732 seconds on
2026-11-08. Its receipt binds a 345-Python-source snapshot; an independent audit
matched those sources before further edits and verified all seven original
units, credential stores, native/VM processes and private scopes were removed.
The log SHA-256 is
`401d3b31d21c30061feb840ac9128c1d87595adbb0d2502d9619ff068cb2765b`.
It adds actual guest clock synchronization before certificate creation and
after paused-VM recovery, one forced original K3s process failure and one
supervised restart. All 132 paired worker HTTP samples succeeded. The
36.263-second recovery check includes the stability window and is not a
production RTO claim. Both native watch services retained exactly their one
planned restart, and the complete scoped Cilium/Gerrit/Tekton/promotion/Argo CD
application-Gateway flow passed with strict TLS and policy denials intact.
An earlier extra reporter restart did not recur in this run; its underlying
cause remains unproven and safe diagnostics are retained. Physical HA,
redundant entry, signed artifact publication, upgrade/restore and complete-stack
acceptance remain open.
The current quorum fixture pins the chosen leader process with a
Linux pidfd, rechecks the original service PID/invocation inside that guest,
and sends one SIGKILL without retrying an uncertain response. It requires one
supervised restart with a new invocation, stable direct APIs, unchanged other
control processes/voter identities, preserved committed data and worker HTTP
samples throughout recovery. A full traffic-sampler buffer prevents acceptance.
See Python's [pidfd opening](https://docs.python.org/3.13/library/os.html#os.pidfd_open)
and [pidfd signaling](https://docs.python.org/3.13/library/signal.html#signal.pidfd_send_signal)
interfaces. This does not certify the complete stack.
It requires all artifact inputs from the native promotion case and the Cilium
case, with no artifact download or upstream route during execution. Use a new
bounded `platform-k3s-vm-*.service`, `RuntimeMaxSec=1800`, `MemoryHigh=11G`,
`MemoryMax=12G`, `MemorySwapMax=0`, `TasksMax=4096`, `KillMode=control-group`,
`UMask=0077`, and a new private report path. Select only this method with
`CILIUM_GERRIT_NATIVE_CICD_VM_TEST_RUNTIME=1`. Its explicit extended profile
gives the five guest services finite 1700-second lifetimes; ordinary cases
retain their 1100-second limits. Native polling and promotion services keep
their separate existing limits and original-unit cleanup checks.

The parent receipt records cgroup memory usage, peak, limits and event counters;
an OOM-killed child prevents acceptance. On failure, the fixture emits those
counters before teardown. Native Gerrit failures also report the daemon exit
code, proxy exception classes and fixed error-signal booleans from a bounded
private log tail. Raw daemon logs and credentials are not printed. An uncertain
mutating Gerrit request is not automatically retried.

The quorum servers now use bounded on-failure service restarts because a resumed
K3s process can exit after losing its controller lease during a VM pause. This
follows K3s's supervised-service approach, with stricter laboratory restart
limits: three starts per 120 seconds and a two-second delay. Recovery requires
every original service to have a live PID and its own directly addressed,
TLS-verified API to return ready for 15 seconds with stable PIDs/restart counts.
Every original etcd member must report the original voter inventory. The direct
service/API stability check is repeated after the application workload; cached
Node Ready conditions or one healthy load-balancer backend are insufficient.
The parent deadline and VM cleanup still bound the entire run. See the pinned
[K3s service template](https://github.com/k3s-io/k3s/blob/v1.35.0%2Bk3s1/install.sh)
and [systemd restart semantics](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html#Restart=).

On cold Linux starts the fixture waits for udev to settle before checking the
KVM device's existing owner/group/mode. Each QEMU launcher also opens the device
under its dropped UID and supplementary KVM group before exec. Access failure
stops the case; it never grants host device permissions or substitutes software
emulation for KVM.

```bash
sudo systemd-run --wait --pipe --collect \
  --unit="platform-k3s-vm-cilium-cicd-$(cat /proc/sys/kernel/random/uuid)" \
  --property=CapabilityBoundingSet=~CAP_SYS_TIME \
  --property=RuntimeMaxSec=1800 --property=MemoryHigh=11G \
  --property=MemoryMax=12G --property=MemorySwapMax=0 \
  --property=TasksMax=4096 --property=KillMode=control-group \
  --property=UMask=0077 --working-directory="$PWD" \
  /usr/bin/env CILIUM_GERRIT_NATIVE_CICD_VM_TEST_RUNTIME=1 \
    K3S_VM_TEST_TIME_ARTIFACTS=/absolute/private/cache/chrony/4.6.1-debian13 \
    K3S_VM_TEST_ARTIFACTS=/absolute/private/cache/k3s/v1.35.0+k3s1 \
    K3S_VM_TEST_QEMU=/absolute/private/cache/qemu/10.2.1 \
    K3S_VM_TEST_KUBECTL=/absolute/private/cache/envtest/v1.35.0/extracted/controller-tools/envtest/kubectl \
    K3S_VM_TEST_REPORT="$PWD/private/validation/cilium-cicd-$(cat /proc/sys/kernel/random/uuid).json" \
    CILIUM_VM_TEST_ARTIFACTS=/absolute/private/cache/cilium/v1.19.10 \
    CILIUM_VM_TEST_HELM=/absolute/private/cache/cilium-lifecycle/helm \
    K3S_QUORUM_TEST_HAPROXY=/absolute/private/cache/haproxy-3.2.26/haproxy \
    TEKTON_VM_TEST_RELEASE=/absolute/private/cache/tekton-v1.4.0/release.yaml \
    TEKTON_VM_TEST_IMAGES=/absolute/private/cache/tekton/v1.4.0 \
    GERRIT_REVIEW_TEST_ARTIFACTS=/absolute/private/cache/gerrit/3.13.10 \
    GERRIT_REVIEW_TEST_JAVA_ARCHIVE=/absolute/private/cache/OpenJDK21U-jre_x64_linux_hotspot_21.0.13_9.tar.gz \
    ARGOCD_TEST_ARTIFACTS=/absolute/private/cache/argocd/v3.5.0 \
    ARGOCD_TEST_VALKEY=/absolute/private/cache/valkey/8.1.11/tls/valkey-server \
    /usr/bin/unshare --net -- \
    python3 -B scripts/test_k3s_worker_runtime.py \
      WorkerRuntimeTests.test_real_cilium_gerrit_native_cicd_and_application_gateway
```

The exact three- or five-guest inventory is validated before admitting native
services to the lab network: expected names/addresses, all original KVM monitors,
the exact TAP count, the original private bridge and absence of an upstream
route. An arbitrary extra interface, control-plane guest or address is refused.
The separate [seven-guest external entry test](CILIUM_NATIVE_ENTRY_RUNTIME.md)
admits only its two explicitly declared native edge hosts in addition to the
original five Kubernetes guests. It preserves the Kubernetes node inventory.
That joined case passed in 1253.023 seconds against 351 Python sources with
independent original-process, contract, receipt and cleanup inspection. It
verified the actual application through native entry TLS and health-triggered
peer failover in 6.96 seconds. This scoped virtual case does not establish
physical HA or a complete stack/runtime combination.
Rendering or programming this application Gateway does not certify application
HA, registry publication, Ceph, physical failure domains, redundant external
entry, upgrades, restore or any complete stack/runtime combination.

## What the probe must prove

Both original worker Nodes must become Ready and keep the dedicated worker
label, real K3s kubelet version and absence of control-plane/master roles. The
real scheduler selects each worker through its node selector. Each original
Pod UID must complete under containerd using the selected immutable image.

The container itself checks UID 1000, no automatic service-account token,
read-only root mount, no-new-privileges, seccomp filtering, zero effective
capabilities and its actual 64 MiB cgroup memory limit. Its result binds the
actual Pod UID and assigned Node. The verifier correlates the original Pod,
container ID, exit status, image identity and unmodified worker placement.

The default worker test uses K3s's bundled Flannel host-gateway networking solely
for that case. It does not certify the requested Cilium/Gateway foundation,
three control-plane nodes, independent physical failure domains, persistent
storage, signed application images, upgrades, restores or any of the fifty
complete stack/runtime combinations. Those flags remain false separately.
