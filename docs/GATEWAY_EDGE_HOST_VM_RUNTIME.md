# Disposable native entry-host lifecycle probe

The opt-in `HostTests.test_real_native_host_vm_lifecycle` runs the production
`manage_gateway_edge_host.py` CLI inside two disposable Debian 13 KVM guests.
It exercises real `/etc`, `/var/lib`, systemd, service-account and process
boundaries. It complements the [private VRRP protocol probe](GATEWAY_EDGE_VIP_RUNTIME.md)
and the simulated interruption/ownership tests in `test_gateway_edge_host.py`.

## Execution and scope

The guests have separate kernels and disks, two vCPUs and 1024 MiB each. They
use the existing pinned Debian cloud image and the 18-package QEMU 10.2.1
inventory from the K3s VM fixture, without starting K3s or borrowing a cluster.
Original QEMU processes run as UID/GID 65534 with access to the existing KVM
device group. Host kernel tunables and KVM permissions are checked, not changed.

The parent requires an empty private network namespace and a transient unit
named `platform-k3s-vm-edge-<unique-id>`, with a maximum ten-minute deadline,
4 GiB memory, 512 tasks, zero swap, `NoNewPrivileges`, `KillMode=control-group`
and no `CAP_SYS_TIME`. The shared bridge and guest addresses use `192.0.2.0/24`.
Neither guest nor parent has a default route. SSH requires the generated
original host keys; it never accepts unknown host keys or uses an SSH agent.

Package archives and extracted binary/build-metadata pins are explicit in
`scripts/test_gateway_edge_host_vm_fixture.py`. The HAProxy 3.2.26 Debian
candidate differs from the parent-only HAProxy artifact used by the protocol
probe. The original Keepalived 2.3.4 sidecar is retained. Dependency libraries
are extracted into the private scope and made available through a guest-only
loader configuration. Nothing is installed on the parent. The fixture captures
the installed PyYAML 6.0.3 pure Python sources and records their file hashes;
it does not copy a C extension compiled for the parent's Python version.
The first guest dependency probe identified `libsensors.so.5` as absent from
the base image. Its additional Debian archive and checksum are pinned alongside
the other guest libraries; parent version probes alone missed that dependency.

A read-only tools ISO contains the generated packages, unchanged project
Python sources, synthetic public CA and runtime dependencies. TLS private
keys stay in the temporary parent scope, outside that ISO. A test-only Python
tracer wraps the unchanged host CLI to capture bounded exception diagnostics;
it does not replace host methods, service control, checks or command results.
These diagnostics come only from synthetic guests, never production endpoints.

The test requests host preflight, initial installation and running-process
verification on each guest. It then checks strict HTTPS through the real VIP,
changes the route-health threshold through a second immutable release, verifies
it, rolls back explicitly and checks both retained releases and cleared pending
state. The production renderers supply all units, helper files and contracts;
the test does not relocate privileged production paths or relax TLS checks.

The live activation test also exposed an entry wrapper issue: Keepalived's
default checker/BFD PID-file paths are outside the hardened unit's writable
directory. The [upstream PID-file code](https://github.com/acassen/keepalived/blob/v2.3.4/keepalived/core/pidfile.c#L148)
can report failure to create one as an already-running daemon. The wrapper now
selects `--vrrp --no_bfd`, supported by the pinned reviewed build, to run this
adapter's VRRP subsystem with its explicit parent/VRRP PID files. The service
keeps the same filesystem restrictions and capability bounds. Other daemon
builds need those options as well as the declared version and binary digest.
Once those unused modes were disabled, actual VRRP startup exposed a second
unit issue: gratuitous ARP requires `AF_PACKET`. The generated native Keepalived
unit now permits that socket family; the HAProxy unit still excludes it. The
capability sets, filesystem restrictions and dedicated runtime directory remain
unchanged. The guest execution gate covers both startup failures.

This is a configuration update and retained-release rollback test. It does not
prove a daemon version upgrade, pair-coordinator operation, physical HA,
partition fencing, Git writes, application gRPC, a live Gateway cluster or the
complete stack. The corresponding receipt flags remain false.

## Running it

Prepare the caches without changing the pinned artifacts, then run from the
checkout on the dedicated Linux laboratory runner:

```bash
edge_vm_id=$(cat /proc/sys/kernel/random/uuid)
sudo systemd-run --wait --pipe --collect \
  --unit="platform-k3s-vm-edge-${edge_vm_id}" \
  --property=RuntimeMaxSec=600 --property=MemoryMax=4G \
  --property=MemorySwapMax=0 --property=TasksMax=512 \
  --property=PrivateTmp=yes --property=NoNewPrivileges=yes \
  --property=CapabilityBoundingSet=~CAP_SYS_TIME \
  --property=KillMode=control-group --property=UMask=0077 \
  --working-directory="$PWD" /usr/bin/env \
    GATEWAY_EDGE_HOST_VM_RUNTIME=1 \
    GATEWAY_EDGE_HOST_VM_IMAGE=/absolute/private/cache/debian-13-genericcloud-amd64.qcow2 \
    GATEWAY_EDGE_HOST_VM_QEMU=/absolute/private/cache/qemu/10.2.1 \
    GATEWAY_EDGE_HOST_VM_HAPROXY=/absolute/private/cache/haproxy-3.2.26-debian \
    GATEWAY_EDGE_HOST_VM_KEEPALIVED=/absolute/private/cache/keepalived-2.3.4-debian \
    GATEWAY_EDGE_HOST_VM_REPORT="$PWD/private/validation/edge-vm-${edge_vm_id}.json" \
    /usr/bin/unshare --net -- \
    python3 -B scripts/test_gateway_edge_host.py \
      HostTests.test_real_native_host_vm_lifecycle
```

Freeze all project Python sources through terminal completion and independent
receipt/cleanup verification. A passing report is create-only and private;
the source snapshot must match and the original guests, unit and temporary
scope must be gone before acceptance. Ordinary CI skips the runtime method.

## Scoped acceptance

On 2026-11-08 the original two-guest run passed in 101.674 seconds. Independent
audit matched all 347 project Python source hashes, the guest image/QEMU and
15 daemon/library archive pins, the copied renderer dependency sources and
both guests' retained-release records. Each guest returned to its initial
contract, retained two releases and cleared pending state. Strict HTTPS through
the guest VIP passed before and after the update/rollback sequence.

The original unit was absent/inactive with `MainPID=0`; both original VM
processes and the exact temporary scope were removed. Parent peak cgroup memory
was 1,804,742,656 bytes, below the 4 GiB bound, with no OOM event or kill. The
private log SHA-256 is
`12b4c8931ff7601f3725afe1d35c5979c8bcf1c32d38cec11a522a553af8595c`.

This accepts the named native host installation, configuration-update and
retained-rollback properties in disposable guests. The pair coordinator,
daemon version upgrade, live Gateway cluster, physical-host HA, publisher
signatures and complete stack remain unverified by this test.
