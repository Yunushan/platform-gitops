# Optional external Gateway entry pair

This adapter adds an explicitly selected HAProxy Community and Keepalived pair
in front of the [Cilium Gateway package](CILIUM_GATEWAY.md). It renders RKE2,
K3s and existing Kubernetes workloads, separate per-host Docker Compose files,
or native Linux systemd units. Kubernetes has a guarded deployment helper.
Docker and native Linux have a separate explicit local host lifecycle helper.
The separate pair coordinator handles two-host maintenance and optional verified
SSH input distribution. Live acceptance remains an operator responsibility; rendering or
installation is not acceptance of any complete stack or of VIP failover.
The separate [isolated real VIP probe](GATEWAY_EDGE_VIP_RUNTIME.md) now checks
actual health-gated VIP movement and strict HTTPS using two private edge network
namespaces. Its scoped evidence leaves production lifecycle, physical HA and
partition-fencing acceptance open.

## Traffic and health contract

HAProxy uses SNI to pass TLS through to the selected Envoy nodes on each
hostname's listener port. Its health probes verify the issuing CA and
certificate hostname. Both terminating HTTP routes and TLS passthrough routes
require a verified read-only HTTPS readiness path and expected 2xx status.
For TLS passthrough, include the backend application's issuing CA in the
reviewed trust bundle. A gRPC route uses null health path/status and checks
TLS reachability.
Unknown or absent SNI is rejected. Only the explicit IPv4 `client_cidrs` are
accepted; `0.0.0.0/0` must be an intentional choice in the private plan.

The loopback health listener counts routes with at least one healthy backend.
`minimum_ready_routes` determines how many routes must pass before the VIP may
be advertised. Requiring every route can withdraw all entry traffic when one
application fails; allowing fewer preserves partial service. Review that choice
with the application owners. The response includes a hash of the selected plan,
which the tracked script and container probes compare exactly. An unrelated
HTTP 200 or an older plan's listener does not satisfy that check. This hash
identifies configuration; it is not authentication against a compromised host.

Both Keepalived peers start BACKUP with `nopreempt`, VRRPv3 unicast, an explicit
peer/interface, and a weight-zero `init_fail` tracked script. They share a
reviewed subnet and distinct failure domains. There is no VRRP password. Network
controls must restrict IP protocol 112 to the two peers and support the required
TTL 255, VIP address movement and gratuitous ARP. VRRP is not quorum fencing:
peer isolation can produce two owners. Switch, VLAN and partition behavior need
live tests and an operational fencing procedure before claiming availability.

HAProxy opens a new backend connection. Envoy sees the entry host as its client.
This package does not enable PROXY protocol or preserve the original client IP.
Apply source-network restrictions at the entry, and review backend policies for
the actual entry-node addresses. The optional GitLab Shell listener forwards SSH
through a separate TCP port. [Gerrit routing](GERRIT_ROUTING.md) uses a
separate write-routing policy.

### GitLab Shell

To expose GitLab SSH, add this optional object to the private edge plan:

```json
"gitlab_ssh": {"hostname": "ssh.example.org", "port": 2222, "node_port": 30222}
```

Point the hostname at the existing edge VIP. Use a public port free on both
entry hosts and a free NodePort in the default Kubernetes range. Set
`GITLAB_SSH_EDGE_PLAN_FILE` to this same private plan when running
`make platform-render-private-values` for `premium-3node-gitlab-ce`.
The GitLab values advertise the hostname and public port. The chart selects
GitLab Shell pods through the named NodePort, and the generated NetworkPolicy
allows the edge and backend-node IPv4 addresses to reach TCP 2222 on those
pods. Restrict direct NodePort access to the two edge hosts at the node
firewall or CNI host-policy layer. With `externalTrafficPolicy: Cluster`,
forwarded packets can carry the receiving node's address. Keep the same
GitLab Shell host-key Secret across replicas and during recovery.

Sync the GitLab application and check the Shell Service before applying
the edge bundle. HAProxy checks for an SSH banner on the same serving nodes
used for HTTPS. At least one SSH backend must pass before Keepalived
advertises the VIP, in addition to the HTTPS readiness threshold. A total
SSH outage can therefore withdraw the shared VIP. Verify the SSH host key
and run an authenticated clone and push through the VIP for application
acceptance.
[GitLab Shell chart configuration](https://docs.gitlab.com/charts/charts/gitlab/gitlab-shell/)

### Gerrit SSH through the shared VIP

Add a `gerrit_ssh` object to the private edge plan and set the same
`ssh_node_port` in the Kubernetes Gerrit router plan.

```json
"gerrit_ssh": {"hostname": "review.example.invalid", "port": 29418, "node_port": 30294}
```

The hostname must match the reviewed Gerrit TLSRoute. Point it at the edge VIP
and use the public hostname and port for `sshd.advertisedAddress` on both
Gerrit instances. [Gerrit SSH configuration](https://gerrit-review.googlesource.com/Documentation/config-gerrit.html#sshd.advertisedAddress)
Apply the router bundle first. It adds a separate `gerrit-router-ssh` Service
with `externalTrafficPolicy: Local`, so each backend address uses a router on
that node. Kubernetes edge deployment checks the Service selector, port
mapping and ready router Pods on at least two selected serving failure
domains. Choose serving nodes that host the router replicas.
For native and Docker entry pairs, run `deploy_gateway_edge.py --check-server`
against the referenced Gateway bundle before installing the hosts. This
checks the cluster-side prerequisites using the reviewed management target.

Before exposing the NodePort, restrict direct access to the entry hosts at the
node firewall and authorize their effective source identities to router TCP
29418 in Cilium.
For independent entry hosts, use scoped CIDRs. For Kubernetes-hosted entry
peers, use the reviewed node identities and rules supported by the installed
Cilium configuration. Verify reachability from each peer. [Cilium layer 3 policy](https://docs.cilium.io/en/stable/security/policy/layer3/)

HAProxy forwards SSH to the local router NodePort. The Gerrit router still
selects the writable instance. The VIP requires at least one healthy SSH
backend for every enabled SSH listener as well as the HTTPS route threshold.
Use distinct public and node ports when enabling both forges.

During a rolling SSH enablement on an existing edge pair, the coordinator checks
HTTPS continuity after the first peer. The peer still running the old release
can own the VIP until the second peer is updated. The coordinator requires the
configured SSH banners through the VIP once both peers have the new listeners.

## Private inputs and packaging

Copy `examples/gateway-edge.json` into ignored private storage. All example
addresses, UIDs and digests are placeholders. Replace them with reviewed values:

- The exact rendered Gateway contract SHA-256 and all its hostnames.
- Two entry hosts and at least two Gateway serving nodes, with exact addresses,
  Kubernetes Node UIDs and failure domains. Container/native entry hosts may
  use null Node name/UID when they are outside Kubernetes.
- An unused VIP, interface, VRID and distinct public/backend/health ports.
- Approved client networks, readiness paths and the route-health threshold.
- Digest-pinned daemon images, an exact stable version and SHA-256 of each executable at
  `/usr/sbin/haproxy` and `/usr/sbin/keepalived` for containers. For native Linux,
  set the paths to `/etc/platform/gateway-edge/bin/haproxy` and
  `/etc/platform/gateway-edge/bin/keepalived`; the installer retains each binary
  in its immutable release and the launcher resolves that release before exec.
- The CA file SHA-256 and, for Kubernetes, an immutable preprovisioned Secret
  containing `ca.crt`. The Secret is separate from the Gateway TLS private key.

An external native Linux or Docker entry pair can reference a Gateway package
rendered for RKE2, K3s or existing Kubernetes. Keep that original Gateway package,
runtime label, management target and digest intact; select `raw` or `docker` only
for the entry package. Kubernetes-hosted entry workloads still require their
runtime to match the Gateway runtime. A native plan can use `"images": null`
because its executable and support-file hashes define the native artifacts.
Container plans continue to require both digest-pinned daemon images.

The reviewed Linux images must contain `/usr/bin/python3`, the selected daemon
and its runtime libraries. Keepalived packages may also need their compiled
configuration-options metadata. This renderer does not build or download images.
The supported lane is HAProxy 3.2.20 or later in the 3.2 series and Keepalived
2.3.4 or later in the 2.3 series. Older HAProxy builds have a startup defect that
can select a backend before its first successful health check despite
`init-state fully-down`; the adapter rejects them. The launcher verifies the
actual version reported by the pinned executable before its parser check or
startup. Version strings alone are not artifact pins. Select maintained patch
releases and rerun the transport fixture before release.

```bash
python3 scripts/render_gateway_edge.py \
  --plan private/gateway-entry.json \
  --gateway-bundle-dir private/rendered-cilium-gateway \
  --runtime rke2 \
  --output-dir private/rendered-gateway-entry

python3 scripts/deploy_gateway_edge.py \
  --bundle-dir private/rendered-gateway-entry \
  --contract-sha256 '<REVIEWED_EDGE_CONTRACT_SHA256>' \
  --gateway-bundle-dir private/rendered-cilium-gateway
```

Rendering requires a new private directory. Review every artifact and the digest
in `render-status.json`. Default helper mode reads local files only; it constructs
no Kubernetes client, reads no kubeconfig and starts no daemon.

## Kubernetes rollout

Preprovision a dedicated namespace with the privileged Pod Security enforcement
level. Host networking and VIP capabilities need this infrastructure boundary;
the helper does not weaken a namespace policy itself. Select schedulable entry
nodes without an unmatched NoSchedule taint. These workloads do not grant a
blanket control-plane toleration. The two DaemonSets each bind to one exact Node.

The containers have separate identities and capabilities: HAProxy is non-root
with NET_BIND_SERVICE; Keepalived is root with NET_ADMIN, NET_RAW and SETGID.
The latter is needed because Keepalived resets supplementary groups before
running tracked scripts, even for root. The launcher rejects missing effective
capabilities before starting Keepalived. Neither container is privileged and
neither receives a Kubernetes service-account token. Configurations, helpers,
the Python interpreter, binaries and CA must have root-owned paths that other
users cannot write. The root health check retains script security enforcement.

Set the referenced Gateway management kubeconfig environment variable. As in
the Gateway helper, server operations freeze a private kubeconfig snapshot,
reject dynamic credential plugins and verify the selected context/cluster UID.
Append one mode and a new private proof directory to the command above:

```bash
  --check-server --output-dir private/gateway-entry-check-001
  --apply --output-dir private/gateway-entry-apply-001
  --verify --output-dir private/gateway-entry-verify-001
```

These are separate invocations. Server preflight checks programmed Gateway
routes, current Cilium topology, Node UIDs/addresses/failure domains, the
immutable CA and resource ownership. Apply performs a server dry-run, installs
immutable configuration and the disruption budget, then updates peer B before
A. Each peer must become current and ready before the next update. It never
forces field conflicts, adopts unowned objects, prunes or restarts the CNI.
Verification rechecks generated workload specifications and current controller
generations. It does not prove VIP ownership, client reachability or failover.

The private `edge-proof.json` records phases and write attempts before sending
them. A lost acknowledgment or timeout can leave partial state. Inspect that
state and retain the package/journal before a new invocation with a new proof
directory. This operation has no distributed concurrency reservation, automatic
rollback or fencing. Serialize entry maintenance externally; an invocation's
ordered rollout does not coordinate another operator's invocation. A PDB helps
with supported voluntary evictions, not arbitrary process or network failure.
CA rotation and configuration changes require a new reviewed package.

## Docker and native Linux

Run `scripts/manage_gateway_edge_host.py` on the matching entry host. Its default
mode checks local packages only and constructs no host controller. Host modes
require an explicit root Linux session and verify the selected interface/address.
First verify the Gateway using `deploy_cilium_gateway.py --verify` on its
management target. The host helper does not hold Kubernetes credentials or claim
that local artifact checks establish live Gateway programming.

For Docker, preinstall rootful Linux Docker/Compose without user-namespace
remapping and preload both exact image
digests. The helper explicitly selects the local root-owned Unix socket, rejects
rootless/non-Linux engines and never pulls images. It owns only the named
per-host Compose project. Docker Desktop does not provide this host VIP model.
For native Linux, supply executable artifacts named `haproxy` and `keepalived`
in a private input directory. Preprovision compatible OS runtime libraries and
Python/systemd. If a reproducible Keepalived build requires its configuration
metadata, add `config_opts_sha256` to that native binary's plan entry and supply
`keepalived.config-opts` alongside it. The helper hashes and retains the sidecar.
Review any build-specific system metadata dependencies before accepting startup.

```bash
python3 scripts/manage_gateway_edge_host.py \
  --bundle-dir private/rendered-gateway-entry \
  --gateway-bundle-dir private/rendered-cilium-gateway \
  --contract-sha256 '<REVIEWED_EDGE_CONTRACT_SHA256>' \
  --host edge-b
```

Append `--check-host --ca-file private/entry-ca.crt` for a non-starting host check.
Native checks also need `--artifacts-dir private/entry-binaries`. This mode uses
temporary parser inputs and does not install units, create an account, switch
the active release or start a daemon. Native parsers validate the staged CA;
Compose configuration checking alone does not establish image runtime readiness.

Append `--apply` with those same private inputs to install and start one host.
Fresh installation defaults to `--expected-current absent`; upgrades require
the exact currently installed digest instead. A root-owned exclusive lock and
receipt under `/var/lib/platform-gateway-edge` serialize local operations and
reject unrelated installations, unowned units, systemd drop-ins, changed
Compose ownership and an unowned already-assigned local VIP.

Each immutable `releases/<digest>` includes programs, generated files, the CA and,
for native Linux, pinned binaries/support metadata. A retained inventory digest
is recorded separately in the private host receipt. Native CAs also use
fingerprint-specific persistent paths under `trust/`; reboot does not depend on
recreating a temporary CA file. The `platform-edge` system account is created
if absent; an existing root identity is rejected. Only the selected host's two
units are installed. Directories remain root-owned and non-writable to other
users; public CA/config/program files are readable by the non-root service.

Activation stops the owned VIP daemon first, atomically changes the active
selector, starts HAProxy and waits for its plan-specific local route readiness,
then starts Keepalived. Verification checks current unit/container ownership,
isolation, readiness and the running executable hash. Native process arguments
must name the immutable release; rewriting a selector or unit file alone cannot
make an old process pass verification. Container commands/images/mounts and
capabilities are checked through the local engine.

`--verify --expected-current '<ACTIVE_DIGEST>'` rechecks the installed release
without restarting services. `--resume` requires the exact pending target and
the still-recorded stable digest (or `absent` for an interrupted initial install).
It accepts only the previously reserved payload and checks ownership again.
If the desired services are already verified after a lost acknowledgment, it
commits the receipt without another restart. Otherwise it resumes explicit
activation. Private `attempts/<id>/proof.json` records write attempts and partial
failures. Inspect the receipt and proofs before retrying; preserve the matching
tool revision and Gateway/bundle inputs. A changed generator may intentionally
reject a package produced by another revision.

`--rollback --expected-current '<ACTIVE_DIGEST>'` accepts only the receipt's
retained previous release, supplied with its matching original bundles. Native
artifacts and CA can be reused from that retained release. Ordinary apply refuses
a daemon downgrade; rollback still cannot bypass the supported version floor.
No failure triggers automatic rollback, pruning, account deletion or broad
Compose teardown. An interrupted operation must be resumed before starting
another change. Dependency-library restoration and cross-host rollback order
remain operational responsibilities.

Review host firewall rules, port conflicts and existing VIP owners before
activation. Install and verify B before touching A, stopping on the first failure.
The lock and receipt coordinate only one host. The separate
[staged pair coordinator](GATEWAY_EDGE_PAIR.md) adds two-host reservations,
ordered jobs, optional verified input distribution and observation/resume. Fencing
remain unfinished. Host readiness
does not prove VIP ownership, reachability from outside or failover.

## Acceptance still required

The offline suite exercises all five package formats, capability separation,
input drift, CA/Node rejection, ordered simulated rollout and partial-failure
journals. The host lifecycle fixtures additionally exercise retained binary/CA
releases, install/upgrade/rollback, immutable inventory drift, Docker inspection
guards, partial native activation and acknowledgment-loss resume. Host service
operations are simulated in those fixtures. Optional local Linux parsers validate
staged real native binaries without installing them. Transport fixtures exercise real HAProxy TLS pass-through,
unknown-SNI rejection and loss of one then both HTTP-ready backends. The HAProxy
fixture also checks rejection of a trusted certificate for the wrong hostname
and an untrusted certificate with the correct hostname. Keepalived
is run in parser-only mode, with before/after address comparison. No test
advertises a VIP or starts a VRRP daemon.

Run `python3 scripts/test_gateway_edge.py` for the ordinary offline suite.
Optional fixtures use explicitly supplied local tools:
`GATEWAY_EDGE_TEST_HAPROXY` (the corrected 3.2.26 fixture binary),
`GATEWAY_EDGE_TEST_KEEPALIVED` (2.3.4),
`GATEWAY_EDGE_TEST_SYSTEMD_ANALYZE` and `GATEWAY_EDGE_TEST_DOCKER`.
`GATEWAY_EDGE_TEST_LIBS` can point at extracted fixture libraries; it is used
only by the tests, never by the production launcher. The Keepalived parser test
requires a root test identity for its root-script security validation. These
optional tool fixtures are skipped when their explicit inputs are absent;
ordinary CI success alone is not proof they ran.

Native artifacts must also match the target host's userspace ABI. The cached
Ubuntu Keepalived 2.3.4 parser executable requires GLIBC 2.42, while the pinned
Debian 13 VM guests report GLIBC 2.41. That executable cannot establish native
guest/VIP acceptance. Official source archives are retained separately for a
compatible build: 2.3.4 SHA-256
`6afd95ddb7d3e0d3b8b8e5b3a489144131b61a01b06d29e883d0c44acc8a36bf`
and 2.4.3 SHA-256
`a0faef8e401c143487b131b526df7541c1e33d9b8814642fa9dfe8bb250a9632`.
Their published legacy MD5 values match the
[official download inventory](https://www.keepalived.org/download/); this is not
publisher-signature verification. Neither archive has been executed for this
acceptance. The 2.4 series remains outside the adapter's qualified version lane;
caching its source does not enable an upgrade or certify its configuration.

The currently accepted parent HAProxy fixture binary likewise requires GLIBC
2.42. A separate same-version Debian guest candidate,
`haproxy_3.2.26-1~bpo13+1_amd64.deb`, has archive SHA-256
`cda2d07f9a1697f5265c1a3f568db500495046e8a704bd1d4245765dc27b55b3`
and executable SHA-256
`0be1f08b8031166bfa22b39720a6a21d49ceca07cea11134ecc0395f5208ebbe`.
The [packaging team's signed Release](https://haproxy.debian.net/dists/trixie-backports-3.2/InRelease)
verified under its [HTTPS-distributed archive keyring](https://haproxy.debian.net/),
fingerprint `AEF2348766F371C689A7360095A42FE8353525F9`; that Release binds the
package index and exact archive. The fingerprint was recorded without an
independent trust-channel check. ELF inspection finds a GLIBC 2.38 requirement,
and its private version probe passes with the cached Debian runtime libraries.
The package was privately extracted, then used by the
[two-guest native host lifecycle probe](GATEWAY_EDGE_HOST_VM_RUNTIME.md).
That probe accepts guest execution, native installation, configuration update
and retained-release rollback. Coordinated pair operation and physical HA
still require acceptance. This candidate does not change the separate namespace
fixture's selected binary or any deployed version.

A separately cached Debian `keepalived_2.3.4-1+b2_amd64.deb` offers another
candidate without lowering the adapter's minimum patch version. Its
[published package checksum](https://packages.debian.org/forky/amd64/keepalived/download)
is SHA-256
`a3c960f7c25c962e91a26e73e1821e14192aba2ff41473613fedd5da4f6c5cd4`;
the extracted executable is
`885d32a9bc86e69441d13c8ce9ead7ebc86df60f379421f59477568c86d94bae`.
Read-only ELF inspection finds a highest GLIBC requirement of 2.38, but this
alone does not establish guest compatibility: its dependency closure includes
`libsnmp45` and must be supplied and tested in the disposable guest. The package
was first extracted privately, then passed actual service startup in the
two-guest lifecycle test with the complete pinned guest dependency closure.
No package was installed on the parent, and no publisher-signature claim is
made for this archive.
Its first isolated version probe returned
`Unable to read build config options file`. The
[upstream reproducible-build loader](https://github.com/acassen/keepalived/blob/v2.3.4/keepalived/core/main.c#L2469)
tries the primary configuration path, then the executable's adjacent
`.config-opts` file. Retaining the original package metadata beside the cached
binary made the actual version probe pass without changing host configuration.
That metadata has SHA-256
`0a71bf7d887fd38ef63d486c7acea36ff9dc7f45a3b20a178c50173cd64097b7`;
the native plan's existing `config_opts_sha256` field can bind it. A native
guest fixture verifies that original sidecar and dependency closure; the
passing parent version probe alone does not establish guest or VIP acceptance.
The candidate subsequently passed both real configuration-parser fixtures in
an isolated network namespace, with no skipped tests, no daemon startup and
unchanged interface addresses. The native package-root input lookup was also
corrected and checked using actual root-owned executable/metadata files,
including rejection after binary or sidecar drift. These are parser and input
checks; the separate private VIP runtime evidence is described in the
[real entry-pair probe](GATEWAY_EDGE_VIP_RUNTIME.md). The separate
[native host VM probe](GATEWAY_EDGE_HOST_VM_RUNTIME.md) subsequently passed
real guest startup, host installation, configuration update and retained
rollback in 101.674 seconds against 347 Python sources. It exposed and now
covers the unused checker/BFD PID-file failure and missing Keepalived
`AF_PACKET` permission. A subsequent
[real SSH pair run](GATEWAY_EDGE_PAIR_RUNTIME.md) passed staged installation,
configuration update and rollback through six durable jobs in 136.955 seconds
against 348 Python sources. Its runtime evidence exposed and covers the omitted
render-time helper inventory and restrictive-job-umask directory creation.
A subsequent coordinator-recovery profile passed in 181.173 seconds against
349 Python sources, preserving the original live job through coordinator loss
and a bounded uncertain resume, then completing three idempotent resumes without
extra jobs or service restarts. It exposed and now covers contradictory terminal
receipt fields after recovery, preserving the prior error under `last_failure`.
The actual terminated-worker profile subsequently passed in 197.104 seconds
against 350 Python sources, retaining the dead worker as unverified during
ordinary resume and permitting one replacement only after explicit verified
recovery. It checked six completed records plus the original terminated record,
all three idempotent resumes and original unit/process cleanup. An earlier
guest-boot stop remains unexplained; bounded startup diagnostics are now retained.
The separate [joined seven-guest case](CILIUM_NATIVE_ENTRY_RUNTIME.md) now
qualifies the actual Cilium/Gerrit/Tekton/Argo CD application through this native
pair and health-triggered peer entry failover. Its 1253.023-second run retained
351 source hashes and independently inspected original processes, contracts,
job receipts and cleanup. Peer service recovered in 6.96 seconds with verified
TLS and continued direct-access policy denials. Fleet deployment, daemon version
upgrades, physical/partition failover and full-stack acceptance remain open.

Before production acceptance, verify each hostname through the actual VIP,
certificate failures, each backend path, peer/entry/serving-node loss, network
partition, single ownership, ARP convergence, Git writes and gRPC semantics.
Measure outage/RPO/RTO under load and exercise upgrade, rollback and independent
restore. Until that evidence exists, `vip_failover_verified`,
`application_health_verified`, `ha_verified` and `full_stack_verified` stay false.

Upstream behavior references: [HAProxy configuration](https://docs.haproxy.org/3.2/configuration.html),
[HAProxy startup-state fix in the 3.2 changelog](https://www.haproxy.org/download/3.2/src/CHANGELOG),
[Keepalived configuration](https://www.keepalived.org/documentation/keepalived-conf/)
and [Keepalived script execution](https://github.com/acassen/keepalived/blob/v2.3.4/lib/notify.c).
