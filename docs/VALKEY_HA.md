# Optional TLS Valkey replication and Sentinel packages

The optional [TLS application gateway](VALKEY_APPLICATION_GATEWAY.md) now provides
a separate endpoint for clients that cannot directly satisfy the strict upstream
mTLS/discovery setup. Its native protocol/failover component test passes with real
Valkey and HAProxy; Harbor compatibility and complete-stack HA remain unverified.
It does not change the cache profile's TLS/authentication requirements.
Set `frontend.require_client_certificate: true` in a private gateway plan
to require client certificates at the gateway as well. The gateway uses its
reviewed `ca.pem` to verify clients and retains backend mTLS and Valkey ACL
authentication. Use a CA whose issuance policy covers the dedicated
application clients and backend certificates. Existing plans default to
server-authenticated TLS on the frontend.

For two Linux gateway hosts on the same IPv4 subnet, the optional
`vrrp` section renders a host-native Keepalived service per peer for
the raw and Docker gateway packages.

```json
{
  "vip": "192.0.2.70/24",
  "vrid": 79,
  "interfaces": {"gateway-a": "eth0", "gateway-b": "eth0"},
  "binary": "/usr/sbin/keepalived",
  "binary_sha256": "<REVIEWED_KEEPALIVED_SHA256>"
}
```

Supply the plan's actual peer names, interfaces, unused VIP and VRID.
Install the rendered Keepalived helper, configuration and digest file in
the root-owned gateway inputs directory, and install only this host's unit
on each peer. Allow VRRP IP protocol 112 between those peers on the shared
subnet. Enable `net.ipv4.ip_nonlocal_bind=1` in the gateway network namespace
before starting HAProxy so it can bind the VIP while on standby.
The health command checks that local HAProxy has exactly one healthy
primary backend. Keepalived withdraws the address when that check fails.
Use a frontend certificate that covers the cache DNS name pointing to
the VIP, or the VIP as an IP SAN when clients use the address directly.
[Linux nonlocal bind setting](https://docs.kernel.org/networking/ip-sysctl.html#ip-nonlocal-bind-boolean)

`scripts/render_valkey_ha.py` generates a separate Valkey 8.1.11 package for
RKE2, K3s, existing Kubernetes, Docker or native Linux. Each package has three
data members and one Sentinel per member, with an initial primary and two
replicas. Three independently placed Sentinels use quorum two and majority
election. A separate [guarded Kubernetes installer](VALKEY_INSTALLATION.md)
provides original-resource startup observation. Native/Docker activation,
upgrades, application integration and full-stack HA remain incomplete.

The implementation follows the versioned
[server configuration](https://github.com/valkey-io/valkey/blob/8.1.11/valkey.conf),
[Sentinel configuration](https://github.com/valkey-io/valkey/blob/8.1.11/sentinel.conf),
[TLS documentation](https://valkey.io/topics/tls/) and
[Sentinel architecture](https://valkey.io/topics/sentinel/).
Container commands use the executable location from the
[official 8.1 image build](https://github.com/valkey-io/valkey-container/blob/mainline/8.1/debian/Dockerfile).
Version 8.1.11 is the tested configuration family, not a claim about a latest
release, LTS policy or compatibility with every Redis client.

## Scope an independent replication group

Copy [the example](../examples/valkey-ha.json) into ignored private storage.
Pin reviewed workload and seed-tool image digests, the native TLS-enabled
executable hash, original primary, three member IDs/IPs, hosts/failure domains,
six separate persistent state claims and each member's TLS/auth Secret names.
Keep native executable, credentials and state directories disjoint.

Native and host-network Docker use the declared member IPs. Kubernetes uses
stable per-member headless Service names, DNS resolution and advertised
hostnames. Certificates must cover those exact endpoints and allow server/client
authentication. Provide an approved CA and distinct private node credentials.
The package disables plaintext TCP, enables TLS replication and requires client
certificates on both the data and Sentinel listeners. CA trust alone is not a
certification of peer hostname identity or application authentication.

Server configuration enables AOF with once-per-second fsync, replica read-only
mode, refusal of stale replica data and bounded memory with `noeviction`.
At least one sufficiently current replica is required for primary writes.
The generated server configuration explicitly sends replication PINGs every
second. The upstream ten-second default is longer than the strict freshness
limits supported by this profile and can make an idle replica fail observation.
The native acceptance case checks a quiet interval without weakening that limit.
Applications must handle temporary write refusal and promotion/reconnection.
Replication is asynchronous: successful replication/witness tests, `WAIT` and
write-admission checks do not prove lossless failover. Review the application's
acceptable loss window and fencing requirements before selecting this topology.

## Materialize private scoped credentials

`scripts/materialize_valkey_auth.py` consumes the private plan and five distinct
environment-variable references from
[this example](../examples/valkey-credential-refs.json). Each role's credential
must remain consistent across this replication group's members; role values
must differ from one another. Supply independently generated high-entropy
URL-safe values of 32–128 characters through an approved secret provider or
private shell prompts. No credential belongs in the JSON plan or public Git.

```bash
python3 scripts/materialize_valkey_auth.py \
  --plan private/valkey-ha.json --runtime raw --member cache-a \
  --credential-refs private/valkey-credential-refs.json \
  --output-dir private/generated/cache-a-auth
```

The new private output has `server.acl`, `sentinel.acl`, `server-auth.conf` and
`sentinel-auth.conf`. Existing output is refused. ACL passwords are SHA-256
commitments; replication and Sentinel outbound authentication require plaintext
values in owner-private runtime files. Keep all four artifacts private, including
rewritten runtime configuration and backups. Do not print their contents or put
credentials in process arguments. POSIX files are mode 0600; enforce equivalent
private ACLs separately on Windows.

The data server disables the default user and separates replication from
Sentinel's control identity. The latter has the commands needed to inspect and
reconfigure replication and access its dedicated Sentinel gossip channel.
The canary data identity is limited to `platform-probe:*` and selected data/read
diagnostic commands. It is not an application-wide ACL definition.

Sentinels disable their default user too. A separate privileged peer credential
controls Sentinel-to-Sentinel coordination; keep it out of application clients.
The discovery identity can inspect/discover topology and quorum but cannot
initiate a failover or modify Sentinel configuration. Validate the chosen
application client's separate Sentinel/data authentication, TLS, certificate,
discovery and retry support. A working canary does not certify Forgejo, Gitea,
GitLab, Harbor, Argo CD, Keycloak or any other application's semantics. No
application is automatically switched by rendering these files.

## Render and prepare persistent mutable configuration

```bash
python3 scripts/render_valkey_ha.py \
  --plan private/valkey-ha.json --runtime rke2 \
  --output-dir private/generated/valkey-rke2
```

All five formats bind artifacts to `valkey-contract.json`. Rendering creates
only a new private bundle and leaves deployment/compatibility/HA flags false.
Image signatures, native build provenance and TLS capability require separate
review. A binary compiled with `BUILD_TLS=no` cannot satisfy these configurations.

Sentinel rewrites its configuration to persist discovery/election state. It
also asks servers to rewrite role configuration during promotion/rejoin.
Retain the writable original configuration and AOF state across restarts.
Recopying the initial primary/replica seed after a failover is unsafe.

For native hosts, prepare a non-root `valkey` account, private TLS/auth material
under `<private_dir>/<member>`, separate writable `server_dir`/`sentinel_dir`
and the generated native binary checksum file. Keep both state directories empty
for the [activation helper](VALKEY_HOST_ACTIVATION.md); it seeds `server.conf`
and `sentinel.conf` under an original-operation marker before creating services.
Use owner-private files/directories; native units set umask 0077, restrict
writable paths, disable swap and bound memory/tasks. No service is activated or
enabled for boot by the renderer.

Docker uses the same prepared mutable directories with host networking, numeric
UID/GID 1000, a read-only root, dropped capabilities, no privilege escalation,
bounded resources and temporary storage. TLS/auth bind mounts are read-only and
missing host source paths are refused. Precreate appropriately owned private
directories and review the pinned image. The renderer does not run Compose;
the separate activation helper creates the two pinned containers directly.

Kubernetes requires prepared namespaces, six existing data/Sentinel PVCs and
private member TLS/auth Secrets. Auth Secret keys match the four materialized
filenames; TLS keys are `tls.crt`, `tls.key`, `ca.crt`. Secrets and PVCs are never
generated by this renderer. Use separately replicated/encrypted storage with
independent recovery dependencies.

Each member's Recreate Deployment has server and Sentinel containers, node/zone
placement, host anti-affinity and no service-account token. The init container
checks immutable seed hashes and creates a new private `runtime` subdirectory
on each state claim. It records the seed commitment, preserves subsequent role
rewrites and rejects a partial/mismatched seed rather than overwriting it.
The seed records the original installer's operation ID as well as its seed
hash. A replacement Pod from the same installation preserves role changes;
a fresh operation cannot adopt runtime state stamped by an older operation.
Use the [guarded installer](VALKEY_INSTALLATION.md) to supply this authority.
Marker checks do not establish physical storage isolation or live data health.

NetworkPolicy allows declared client namespaces, same-group peers and DNS;
the PDB requests two available Pods. TCP startup/liveness/readiness probes
observe listening sockets only, so they do not prove authentication,
replication, current primary identity or Sentinel quorum. Separate live
verification and failure acceptance are required.

The [runtime observation helper](VALKEY_RUNTIME.md) checks the original package,
approved direct member identities, mTLS, replication links and complete Sentinel
membership/quorum across all five package formats. Read-only verification and an
optional unique expiring replicated canary are separate explicit modes. Neither
mode certifies application compatibility, lossless writes or full-stack HA.

## Acceptance boundaries

The optional [Argo CD client preparation helper](ARGOCD_VALKEY.md) adds explicit
Sentinel/mTLS arguments and separate application identities for a dedicated cache
group. It preserves the five original control/probe roles and does not apply a
running application's configuration. Its component compatibility acceptance is
separate from the generic cache and physical failure-domain checks.

Native and Docker packages now have a separate [pair activation
helper](VALKEY_HOST_ACTIVATION.md) with original storage/service custody,
authenticated TLS startup observations and uncertain-write resume handling.
Its startup result requires later group/runtime and application acceptance.
Docker and Kubernetes startup explicitly set `umask 077` before executing
Valkey, preserving private mode `0600` when mutable configuration is rewritten.
Their approved image must contain `/bin/sh` as well as the pinned Valkey binary.

`python3 scripts/test_valkey_ha.py` covers configuration contracts, topology
refusal, private credential output and mutable-seed preservation. The POSIX
seed case executes the generated shell in temporary directories. Default CI
does not start a cache or supply credentials to a company endpoint.

The opt-in real fixture requires `VALKEY_TEST_RUNTIME=1`, a root Linux runner,
an empty network namespace, a pinned TLS-enabled test binary, OpenSSL and
iproute2. `VALKEY_TEST_ARTIFACTS` selects its private binary/build-input cache;
`VALKEY_TEST_REPORT` may select a new private aggregate receipt. The selected
case starts six unprivileged processes, uses synthetic mTLS/ACL inputs and
tests data replication, restricted access, primary/Sentinel process loss,
majority-loss refusal and recovery. It does not establish physical-node loss,
production application compatibility, publisher signatures, lossless writes,
restore, production load capacity or full-stack HA.

The real six-process TLS case passed with the pinned TLS build. Before injecting
a fault, it waits for every Sentinel to discover both healthy replicas as well
as its peer Sentinels. It verifies an acknowledged replicated witness after
promotion, original-state rejoin, no promotion with only one Sentinel, recovery
after restoring a majority and `NOREPLICAS` write refusal when the new primary
has no replica. Certificate-name failure, a missing client certificate,
unauthenticated access and out-of-scope keys/administration are rejected.
The generated mutable-seed shell also passed real POSIX execution, and rendered
Kubernetes resources passed a disposable real API admission/schema check.
That API case has no kubelets, real storage, CNI or running Valkey Pods.
