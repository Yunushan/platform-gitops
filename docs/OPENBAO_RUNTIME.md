# Read-only OpenBao operational verification

`scripts/verify_openbao_runtime.py` observes a prepared OpenBao 2.7.2 cluster
through direct TLS API endpoints. It works independently of the deployment
runtime: RKE2, K3s, existing Kubernetes, Docker and native hosts use the same API.
It does not initialize, join, unseal, restore, issue credentials, modify policies
or write a secret. The only POST is the documented read-only
[self-capability query](https://openbao.org/docs/api/system/capabilities-self/).

Copy [the example](../examples/openbao-runtime.json) into ignored private
storage and fill in the original cluster UUID, three original member IDs,
direct HTTPS origins, advertised Raft addresses and runner-local trusted CA
path and SHA-256. Use canonical endpoints with no redirects or embedded
credentials. Each member must be directly reachable by the runner; a single
load-balancer origin repeated three times is refused.

## Prepare two scoped identities and a canary

The helper consumes existing environment credentials. It never provisions them.
Use a separate observer identity with this policy shape:

```hcl
path "sys/storage/raft/configuration" { capabilities = ["read"] }
path "sys/audit" { capabilities = ["read", "sudo"] }
path "sys/capabilities-self" { capabilities = ["update"] }
```

Listing [enabled audit devices](https://openbao.org/docs/api/system/audit/)
requires `sudo` on that specific read path. This does not grant policy changes,
token creation or restore capabilities. The helper checks those dangerous
paths are denied and refuses root capability or broader rights on its observed
paths. These finite capability checks do not enumerate every possible API path.
Review the complete effective identity/group policies separately.

Prepare a non-sensitive KV-v2 canary with a string `value` field. Pin the SHA-256
of that string's exact UTF-8 bytes in `probe.value_sha256`; do not place a real
application secret or its digest in the plan. The restricted canary identity
should have only the canary read and self-capability-query permissions:

```hcl
path "platform-probe/data/allowed" { capabilities = ["read"] }
path "sys/capabilities-self" { capabilities = ["update"] }
```

Select one or more exact denied canary paths in the same private plan. With the
restricted identity, the helper queries that the allowed path has exactly read
capability, the denied paths and specified administrative paths have exactly
deny capability, and performs actual GETs expecting 403 on denied canary paths.
It makes no trial write, token creation, policy update or restore request.
It reads the allowed canary independently through every member and compares
the value in memory without printing it or placing it in proof.

## Audit configuration and membership observations

Inventory every enabled file-audit device in `audit_devices`. The live inventory
must match exactly. Each device must have the expected private file path,
`mode: "0600"`, `log_raw: "false"` and accessor HMAC enabled (including its
upstream default). Unreviewed devices, plaintext raw logging, broader file
modes and changed paths are rejected. Other device types require separate
support; the helper does not silently accept them as equivalent.

The [health API](https://openbao.org/docs/api/system/health/) must report the
same initialized, unsealed cluster and version on every direct member, with
exactly one active member. The active member must report exactly the three
declared voting IDs and Raft addresses and itself as the sole leader. Health,
voting membership, observer scope and audit configuration are repeated after
the canary checks. A leadership change during observation fails the run; an
operator can rerun against the settled cluster.

API observations do not prove private file permissions on each host, durable
audit-file writes, HMACs in actual records, off-platform log collection,
independent machines/storage or quorum failover. The real isolated acceptance
fixture separately reads actual HMAC audit records and tests leader-process
loss and recovery; those local tests cannot certify a production fleet.

## Run and retain evidence

Local mode validates only the declared structure. It reads no CA contents,
credential or API and cannot produce a live receipt:

```bash
python3 scripts/verify_openbao_runtime.py --plan private/openbao-runtime.json
```

Set the two declared environment references through approved private prompts
or a secret provider. Then observe the prepared cluster:

```bash
python3 scripts/verify_openbao_runtime.py \
  --plan private/openbao-runtime.json --verify \
  --proof private/proof/openbao-runtime.json
```

Use private proof storage and an existing parent directory. Successful proof
is atomically published with owner-only POSIX permissions; enforce equivalent
ACLs separately on Windows. A failed observation does not replace the previous
proof. Check the timestamp and plan/source hashes when evaluating older proof.
Receipt digests provide integrity checks, not signatures or proof of authorship.
Endpoints, tokens, canary values and raw API errors are omitted from output.

`live_observations_verified` covers only the checks above. Physical failure
domains, quorum failover, audit delivery, every application's identity,
unseal recovery, off-platform restore, HA and full-stack flags remain false.
Default CI runs bounded fake-transport failure cases; the opt-in actual
Raft/TLS fixture calls this same helper against real OpenBao processes.
