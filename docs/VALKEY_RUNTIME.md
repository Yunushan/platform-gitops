# Valkey runtime observations

`scripts/verify_valkey_runtime.py` checks an existing three-member Valkey
8.1.11 TLS/Sentinel group against its original rendered package and a private
runtime inventory. It accepts raw Linux, Docker, RKE2, K3s and existing
Kubernetes packages. It does not start, reconfigure or fail over servers.
See [packaging](VALKEY_HA.md) and [Kubernetes installation](VALKEY_INSTALLATION.md).
Native and Docker use the separate [pair activation helper](VALKEY_HOST_ACTIVATION.md).

## Private inventory

Copy [the runtime example](../examples/valkey-runtime.json) into ignored private
storage. Example hashes and IDs are placeholders. Supply:

- The byte SHA-256 of the original `valkey-contract.json`. Every package artifact
  is regenerated and compared byte for byte before connecting.
- Absolute runner-local CA, client certificate and client key paths, with byte
  SHA-256 hashes. The CA must validate all three server certificates; the client
  certificate must be accepted by both data servers and Sentinels.
- Separate environment variable references for the existing `probe` and
  `discovery` credentials. Values must match private auth materialization;
  they are never written into the runtime plan or proof.
- Each approved direct IPv4 address, current server `run_id`, persistent
  `SENTINEL MYID`, and SHA-256 of the DER server leaf certificate. The data server
  and colocated Sentinel use the same leaf in this package.
- Each member's advertised aliases, including its connection IP and original
  package hostname. Kubernetes usually needs both Pod IP and headless Service
  hostname. Each alias must identify exactly one member.

Establish identities from the approved host/workload inventory and an already
trusted TLS administrative session. `INFO server` exposes the server invocation
ID; `SENTINEL MYID` exposes the persistent Sentinel identity. The materialized
discovery identity now permits `INFO` for version/invocation observations.
Existing ACL files are not automatically rewritten or reloaded by this helper.

For a reviewed leaf PEM file, calculate the distinct DER fingerprint with:

```bash
openssl x509 -in private/cache-a.crt -outform DER | openssl dgst -sha256
sha256sum private/generated/valkey-raw/valkey-contract.json
```

CA/client input pins use file bytes; server leaf pins use DER bytes returned by
TLS. Certificate rotation, changed Pod IP or server restart requires a reviewed
inventory update. The verifier never automatically trusts replacement identities.

TLS files must be regular and non-hardlinked. On POSIX the client key must be
owned by root or the invoking user with mode `0400`, `0440`, `0600` or `0640`;
restrict any readable group to designated operators. Files are read without
following symlinks and hash checked. Client material is loaded from a private
temporary directory removed immediately after SSL context creation.

The runner needs direct access to all approved IPs on data TLS port 6380 and
Sentinel TLS port 26380, including NetworkPolicy permission. Connections use
the fixed direct IP and original package name for TLS hostname checking.
Discovered aliases are compared, never used as new connection targets.
There is no plaintext, proxy, redirect or certificate-verification fallback.

## Local validation and read-only observation

Default mode validates package and plan structure locally. It does not read
TLS files, load credentials, contact endpoints or create a live proof:

```bash
python3 scripts/verify_valkey_runtime.py \
  --bundle-dir private/generated/valkey-raw \
  --runtime-plan private/valkey-runtime.json
```

Supply the declared credentials through protected environment variables, then:

```bash
python3 scripts/verify_valkey_runtime.py \
  --bundle-dir private/generated/valkey-raw \
  --runtime-plan private/valkey-runtime.json \
  --verify --proof private/proof/valkey-observed.json
```

The check requires one primary and two connected, synchronized replicas with
the same replication ID, original invocations, bounded last primary activity,
AOF enabled with successful last write/rewrite status, and the declared
memory/no-eviction policy. All Sentinels must agree on primary/configuration
epoch, know the two original peers and replicas, and expose the declared quorum,
detection, failover and parallel-sync settings. Each must report three usable
Sentinels through `CKQUORUM`; this checks current quorum without an election.
See [Sentinel operation](https://valkey.io/topics/sentinel/).

Read-only denial probes check out-of-scope key/configuration reads, discovery
configuration reads, unauthenticated PING and missing client certificates.
No policy write or deliberate failover command is issued. Before/after topology,
invocations and executing-source checks must agree.

## Optional replicated canary

`--canary` explicitly selects live verification with one unique data write:

```bash
python3 scripts/verify_valkey_runtime.py \
  --bundle-dir private/generated/valkey-raw \
  --runtime-plan private/valkey-runtime.json \
  --canary --proof private/proof/valkey-canary.json
```

It writes a random `platform-probe:verify-...` key with `SET NX PX`, then requires
`WAIT 2` on that same connection. Every member must return the exact value and a
positive TTL within the declared lifetime. The key expires; there is no delete
or cleanup write. A failed run may leave that expiring key. Its TTL exceeds the
operation limit by at least ten seconds; the finite replication wait must be
shorter than the request limit. [`WAIT` observes writes on its connection but
does not guarantee lossless failover or strong consistency](https://valkey.io/commands/wait/).

## Evidence and limits

Responses, commands, nested frames, socket requests and the whole operation
are bounded. Errors suppress endpoints, credentials and response payloads.
Successful proofs contain aggregate flags and plan/contract/topology/source
hashes, without endpoint names, run IDs, canary keys or values. Proofs are
atomically written privately; failure preserves the previous complete proof.
The receipt digest detects accidental modification and is not a signature.

The helper observes roles and selected INFO/Sentinel fields, not all effective
configuration. It does not prove disabled plaintext listeners, disk durability,
application compatibility, physical failure domains, restores, lossless failover
or full-stack HA. Those broader verification flags remain false. It requires
a stable three-member topology; reconverge after failures or rebuilds first.

`python3 scripts/test_valkey_runtime.py` covers inventory/package boundaries,
read-only behavior, canary acknowledgement, changed identities, foreign
discovery/quorum refusal, safe proof failure, TLS pin ordering and bounded RESP.
The opt-in [six-process fixture](VALKEY_HA.md) executes the production verifier
over real mTLS in read-only/canary modes and after original-member rejoin. It
also verifies refusal of a stale invocation pin. Its separate process-failover
and quorum-loss cases do not establish physical-node or application HA.
