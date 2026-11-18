# Isolated real entry-pair runtime probe

This opt-in probe exercises actual HAProxy 3.2.26 and Keepalived 2.3.4 processes
on two private Linux network namespaces. It supplies runtime evidence for the
[external entry adapter](GATEWAY_EDGE.md), alongside its configuration-parser
and host lifecycle tests. The complete stack/runtime matrix remains open.

## What runs

A fresh parent namespace initially contains only loopback and has no upstream
route. Two original namespace keeper processes receive separate veth interfaces
on a private bridge. No global named network namespace is created. All addresses
are from `192.0.2.0/24`; the VIP is `192.0.2.50`.

Each edge runs its own HAProxy and Keepalived. HAProxy uses UID/GID 65534,
no effective or bounding capabilities, and no new privileges. Keepalived has
only `SETGID`, `NET_ADMIN` and `NET_RAW`, with no new privileges, in its edge
namespace. The parent service cannot acquire `CAP_SYS_TIME`. This is a shared
kernel laboratory, not two physical hosts or a production service deployment.

The actual renderers supply the Gateway and edge contracts and configurations.
Declared laboratory relocations supply the private CA and original health
helper paths; HAProxy is limited to one thread for the probe. Generated keys
stay in a mode-0700 directory and are removed with the original scope. The
probe uses synthetic TLS backend services, without a Kubernetes cluster,
company credentials, registry access or package installation.

The live checks require:

- One sampled VIP owner and HTTPS through the VIP with the expected SNI, CA,
  certificate hostname and backend payloads; an untrusted client CA is refused.
- Movement to the other live edge when the current owner's HAProxy is stopped.
- Continued ownership by that peer after the stopped HAProxy is restarted.
- VIP withdrawal when route health falls below the threshold, followed by
  serving recovery when the synthetic health endpoints recover.
- Original namespace/process identities, capability bounds, unchanged runtime
  configurations, no upstream routes and no cgroup OOM event or kill.
- Removal of original daemons, namespace keepers, temporary keys and files
  before writing a create-only, source-bound private receipt.

Ownership is sampled during the traffic observations. Two-node VRRP has no
quorum fencing; partition safety, physical failure domains, real serving-node
loss and uninterrupted availability require separate architecture and evidence.
The gRPC route's backend check establishes TLS reachability. gRPC application
semantics, authenticated Git writes, actual Envoy deployment, the production
host/pair helpers, upgrades and independent restore are additional acceptance work.

## Artifact inputs and explicit execution

Use the original reviewed Linux amd64 HAProxy artifact and the nine Debian
package archives pinned in `scripts/test_gateway_edge_vip_fixture.py`. The
Keepalived archive goes directly under `GATEWAY_EDGE_VIP_TEST_ARTIFACTS`;
the eight library archives go under its `dependencies/` directory. The fixture
captures and verifies the original archive bytes before private extraction.
The original `keepalived.config-opts` is copied beside its executable and
verified against its own pin. Matching version text alone is insufficient.

The Linux runner needs root, systemd/cgroup v2, iproute2, network
namespace/veth/bridge support, OpenSSL, `dpkg-deb`, `setpriv` and the repository's
Python dependencies. It refuses the initial host network namespace and an
unbounded or incorrectly owned service.

From the checkout, with the private caches already prepared:

```bash
vip_probe_id=$(cat /proc/sys/kernel/random/uuid)
sudo systemd-run --wait --pipe --collect \
  --unit="platform-gateway-edge-vip-${vip_probe_id}" \
  --property=RuntimeMaxSec=240 --property=MemoryMax=512M \
  --property=MemorySwapMax=0 --property=TasksMax=128 \
  --property=PrivateTmp=yes --property=NoNewPrivileges=yes \
  --property=CapabilityBoundingSet=~CAP_SYS_TIME \
  --property=KillMode=control-group --property=UMask=0077 \
  --working-directory="$PWD" \
  /usr/bin/env GATEWAY_EDGE_VIP_TEST_RUNTIME=1 \
    GATEWAY_EDGE_VIP_TEST_ARTIFACTS=/absolute/private/cache/keepalived-2.3.4-debian \
    GATEWAY_EDGE_TEST_HAPROXY=/absolute/private/cache/haproxy-3.2.26/haproxy \
    GATEWAY_EDGE_VIP_TEST_REPORT="$PWD/private/validation/vip-${vip_probe_id}.json" \
    /usr/bin/unshare --net -- \
    python3 -B scripts/test_gateway_edge.py \
      EdgeTests.test_real_private_vip_health_failover
```

Freeze Python sources until the run is terminal and its independent audit has
checked the receipt's source hashes and original unit/process/scope cleanup.
Ordinary CI skips this method unless the explicit runtime selection is present.

## Scoped evidence

The shared Gerrit VIP variation passed on 2026-11-15 in 103.089 seconds.
After the owning HAProxy stopped, the peer served HTTPS in 3.195 seconds
and returned the synthetic Gerrit SSH banner. Both edges withdrew the VIP
when the SSH listeners stopped returning banners while HTTPS stayed healthy.
Restoring the SSH banners restored VIP service. The receipt matched 554
Python source hashes and the original service and scope were removed.
The terminal log SHA-256 is
`dcc155f85004ffb5a3aa9aa488f28a7aa890683172efa5023d02ececb83d83f2`.
The [joined Gerrit lab](GERRIT_ROUTING.md#verify-review-uploads-through-the-shared-vip)
verified authenticated review uploads through the shared VIP with real Cilium
and Gerrit, including entry-process and Gerrit Pod failure.

On 2026-11-08 the probe passed in 64.285 seconds. An independent audit matched
its 346-Python-source snapshot, checked all five daemon identity records and
the two original edge namespaces, verified resource and capability bounds,
and confirmed removal of the original unit, processes and scope. The private
log SHA-256 is
`206cc31e12dff0f01240ffe8ca6ec4be2281c4017becdf93c9866a3f5830e440`.

The HAProxy-loss experiment measured 3.192 seconds to a serving peer, including
the probe's readiness observations. The receipt separates 104 observations by
cold election, health-gated failover, below-threshold route health and recovery;
78 HTTPS observations failed across those stages. These are measured laboratory
interruptions, not a production availability SLO or RTO. No duplicate VIP owner
was observed in those samples.

Only `private_vip_health_failover_verified` and the specifically tested protocol,
identity, health and cleanup properties are accepted. Physical HA, partition
fencing, production host lifecycle, application gRPC, publisher signatures and
complete-stack flags remain false.
