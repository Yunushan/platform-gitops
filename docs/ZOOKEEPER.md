# ZooKeeper packages for the Gerrit/Zuul stacks

Status: three-member configuration and guarded runtime packages, with an
accepted optional local Java/mTLS quorum case described below. It verifies
process loss and isolated-network recovery; physical-host HA, application CI
integration and full stack acceptance remain open in the
[multi-runtime plan](MULTI_RUNTIME_STACKS.md). Existing profiles are unchanged.

## Private inputs and distribution

Copy [the example](../examples/zookeeper.json) into `private/zookeeper.json`.
Replace every placeholder. Generate an independent ensemble UUID, choose a
reviewed exact ZooKeeper 3.9 patch release at least 3.9.4, and pin the runtime
image digest. The example's version illustrates the contract's minimum feature
set, not a claim about the latest or recommended patch release.

The toolchain image must supply Python 3 and a compatible Java runtime, plus the
complete verified ZooKeeper binary distribution's jars under `/opt/zookeeper/lib`.
Native hosts need the same jars and runtimes. The renderer does not download or
build them. Do not assume an unmodified upstream container includes Python or
the required paths. Review upstream artifact provenance, notices, dependencies
and release compatibility before building that image.

List **every** distribution jar's basename and SHA-256 under `artifacts`, not
only the server jar shown in the example. Startup compares both the exact jar
set and each file's bytes, then invokes the upstream Java version entry point
before a fixed foreground `QuorumPeerMain` command. Unknown jars, symlinked jars,
changed configuration, wrong releases and ambient JVM option overrides stop
startup. These locks establish agreement with the reviewed input, not artifact
authenticity or a supported upgrade path.

The three hosts and client ports must exactly match the endpoints in the
[Zuul plan](ZUUL_CONFIGURATION.md). The [Nodepool plan](NODEPOOL_STATIC.md)
already derives its ensemble from that Zuul plan. This adapter therefore cannot
silently deploy a different ensemble behind the two clients.

Members require distinct IDs 1/2/3, names, hosts, failure domains, host publish
addresses, claims, state paths and TLS references. Host paths may not overlap.
Declared domain names and labels do not prove physical independence. Provision
three real failure domains and redundant network paths, then test them.

## TLS and identity contract

Each member references a separately provisioned private TLS directory or
Kubernetes Secret with these exact filenames:

| Filename | Purpose |
|---|---|
| client-server.pem | PEM server certificate/chain and unencrypted private key for client traffic |
| client-ca.pem | Approved CA trust for client traffic and the local status probe |
| quorum-server.pem | PEM certificate/chain and unencrypted private key for peer traffic |
| quorum-ca.pem | Dedicated approved peer CA trust |
| health-client.pem | Certificate/chain for the status-probe identity |
| health-client.key | Unencrypted private key for that probe identity |

Provision private keys with restrictive ownership/permissions; no key or password
is put in the JSON plan, ConfigMap, generated unit, public examples or stdout.
The renderer does not generate certificates, create Secrets or approve trust
roots. Startup checks bounded regular TLS input files; the actual TLS stacks
perform cryptographic validation when used.

The generated configuration has only a secure client listener, requires client
certificates and X.509 authentication, and enables mutually authenticated peer
TLS with hostname verification. It disables plaintext port unification, the
HTTP admin server, dynamic reconfiguration and standalone fallback. The sole
enabled four-letter status command is `srvr`. These settings use the
[ZooKeeper 3.9 administration contract](https://zookeeper.apache.org/doc/r3.9.4/zookeeperAdmin.html)
and [X.509 authentication provider](https://github.com/apache/zookeeper/blob/release-3.9.4/zookeeper-server/src/main/java/org/apache/zookeeper/server/auth/X509AuthenticationProvider.java).

Peer certificates need appropriate server/client purposes and SANs that pass
both sides' hostname checks for the actual network path. In particular, validate
pod addresses, DNS resolution and Docker NAT source addresses. Per-pod/source-IP
certificate issuance and renewal integration is not yet supplied here. Do not
turn verification off to conceal that missing integration. Client and peer CA
issuance must be restricted to intended service/member identities.

Authentication is not authorization. Review and test the Zuul/Nodepool znode ACL
policy and any chroot/namespace separation; do not share this ensemble with
unrelated applications merely because their certificates use a trusted CA.
ACL provisioning, private PKI and certificate rotation remain acceptance gates.

## Render without deployment

```bash
python3 scripts/render_zookeeper.py \
  --plan private/zookeeper.json \
  --zuul-plan private/zuul-config.json \
  --runtime rke2 \
  --output-dir private/rendered-zookeeper
```

Other targets are `k3s`, `k8s`, `docker` and `raw`. Output must be a new private
directory. Existing paths, public locations and symlink escapes are refused.
The report identifies each member contract's hash and retains false deployment,
quorum and HA flags. Rendering contacts no server and changes no storage.

For RKE2/K3s/Kubernetes, use each member's generated Service FQDN as `host`:
`<name>.<namespace>.svc.<cluster_domain>`. The package contains an immutable
ConfigMap, three single-replica member Deployments, three per-member Services,
and a budget requiring two members available for voluntary evictions. Each
Deployment mounts only its own pre-existing claim and TLS Secret. Recreate
strategy prevents that controller from surging two copies of one ID. Required
anti-affinity separates hosts; required node affinity selects each declared
hostname or zone. Namespace, storage, node labels, PKI and network policy are
provisioning prerequisites. Services publish unready endpoints for bootstrap;
that publication is not a health guarantee.

**Upgrade one member at a time.** The disruption budget does not serialize
Deployment updates. Applying a changed all-member manifest can restart several
members at once and lose quorum. The renderer supplies no automatic upgrade
or restore orchestrator. A production lifecycle must inspect serving quorum,
take usable backups, update one member, verify recovery and only then proceed.

For Docker, output includes one `<member>-compose.yaml` per host. Copy the
bundle to each selected host and run only that host's member file. The three
protocol ports publish on its explicit private `bind_address`; no host network
or privileged mode is used. Pre-provision its state path and
`private/<member>/` TLS directory beside the bundle. Bind mounts never create
missing host paths. Names must resolve through the actual inter-host DNS path;
no single-host Docker aliases or hidden load balancer are substituted. Three
members on one Docker daemon do not establish HA.

For native Linux, copy configuration and helper modules into `/etc/zookeeper`,
and that host's TLS files into `/etc/zookeeper/private`. Provision its state
directory and `zookeeper` service account, install only that member's unit, and
review Java/Python executable paths. The unit runs without capabilities and
restricts writes to that state path and temporary storage. The client listener
binds the declared private host address; peer endpoints must resolve to the
corresponding private host interfaces. Host firewall and routing policy remain
required for both host-based runtimes.

## New storage and local checks

`initialize_if_empty` defaults to false. For a reviewed new ensemble with three
new empty state locations, set it explicitly to true before rendering the
first startup package. The guard can then create only `myid` and its identity
marker using exclusive writes. It never erases, adopts or renumbers an existing
member. An otherwise empty filesystem's empty `lost+found` is allowed. Partial
initialization is retained and refused on the next attempt for inspection.

Existing managed state must match the ensemble UUID, member ID, peer hosts and
peer ports stored in its marker. Configuration/version changes do not silently
change that identity. Retain it when restoring data. Existing unmanaged state,
missing/partial identity files or a different membership stop startup. Joining,
replacing or changing ensemble membership requires a separate recovery design;
there is no force/adopt switch. Restore to empty volumes is not authorization
to create a fresh ensemble with no prior coordination state.

With the bundle provisioned at the runtime paths, check local locks without
starting Java or creating identity files:

```bash
python3 /etc/zookeeper/start_zookeeper.py \
  --member zk-1 --contract-sha256 '<HASH_FROM_PRIVATE_RENDER_STATUS>'
```

Explicit `--start` performs guarded foreground startup; the generated runtime
command already supplies this. New state additionally requires the generated
`--initialize-if-empty` flag. JVM version checking, classpath/configuration
rechecking and storage identity validation precede the launch handoff.

Explicit `--probe` sends a bounded authenticated TLS status request to this
member, verifies its canonical server name and requires `leader` or `follower`
mode. It rejects standalone/observer/non-serving replies and does not opt into
ambient TLS key logging. Kubernetes uses this for readiness, with no
quorum-dependent liveness restart loop. A member serving report does not prove
an ensemble write or application ACL behavior.

## Verification still required

The default tests parse real Docker Compose configuration, protect existing fixture
storage, exercise lock/version failures and complete real TLS protocol
handshakes over an **in-memory transport** with temporary certificates. Wrong
server names and absent client certificates are rejected. Socket status parsing
and limits are tested separately. The default tests do not start Java, validate
ZooKeeper's complete runtime configuration, prove network reachability, or
establish a live three-member quorum.

Before accepting a stack, test real authenticated sessions, writes and ACL
denials; isolated-member behavior; leader replacement; client reconnection;
disk-full and stale-member handling; upgrade/rotation; off-platform snapshot/log
backup and restore; and declared RPO/RTO under node/network loss. Export logs
and metrics through the reviewed observability layer. Autopurge is bounded
local retention, not a backup or recovery test.

## Optional real local quorum check

The opt-in `ZooKeeperTests.test_real_guarded_tls_quorum_acl_and_leader_loss`
uses an official pinned ZooKeeper 3.9.7 binary distribution, the existing pinned
Java 21 runtime and Kazoo 2.11.0. Nothing is downloaded during the test.
Stage the verified binary archive and client wheel under an ignored private
artifact directory. The archive SHA-512 and detached signature can be checked
against [Apache's release inputs](https://downloads.apache.org/zookeeper/zookeeper-3.9.7/);
the fixture independently checks its exact SHA-256 pins before extraction.

Run only in a fresh, bounded root-owned Linux systemd unit named
`platform-zookeeper-runtime-<unique>.service`, with private mount/network
namespaces, no external route and a fresh private report path. Set
`ZOOKEEPER_TEST_RUNTIME=1`, `ZOOKEEPER_TEST_ARTIFACTS`,
`ZOOKEEPER_TEST_JAVA_ARCHIVE` and `ZOOKEEPER_TEST_REPORT`, then select that one
unittest method. The ordinary suite skips it. It creates three bounded member
units (2 GiB, two CPUs, no swap), each bound to the original parent, with separate
network namespaces, data, TLS identities and non-root service identities.

The case exercises the rendered native configuration and the original packaged
launch guard, real mTLS status and Kazoo sessions, certificate/hostname/client
identity refusals, X.509 ACL denial, leader process loss, the same client session
through election, state-preserving member rejoin and an unacknowledged write
while two original voters are paused. Any ambiguous timed-out request is retained
and may commit after recovery; it is never classified as an acknowledged outage
write or proof of zero RPO. Original units/processes and temporary state must be
removed. Private logs stay in ignored evidence.

The accepted local case completed one real test without skips in 47.339 seconds.
Independent inspection checked the official detached signature, exact distribution
jar set, rendered configuration, all 419 source hashes, original member identities
and cleanup. Separate regressions ran 77 Linux tests (five explicit tool/runtime
skips) and 17 Windows tests (only the opt-in runtime case skipped, with real
Compose parsing exercised). Privacy, lint, CI parity and documented-target checks
passed. The partition write completed through a survivor in 19.6 seconds with the
same session; the expired-session branch was not exercised. The minority write
was unacknowledged during the outage and acknowledged after recovery, preserving
the timeout ambiguity. These local process and network failures do not establish
physical HA, disk-loss durability, off-platform restore, Zuul/Nodepool execution,
release upgrades or the complete runtime matrix.

Daemon loss retains the member's original private network namespace so closed
listeners refuse connections. A separate partition drops the original member's
link while its JVM remains alive, requires a write through a surviving peer,
and verifies either the same session's lease or fencing of an expired session's
ephemeral node. Restoring the link must recover all original process identities.
This distinguishes process loss from a blackholed network; neither is relabeled
as physical-host acceptance. Uncertain writes are not retried to make it pass.
