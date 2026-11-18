# OpenBao snapshot capture and isolated recovery acceptance

`scripts/openbao_snapshot.py` captures a private Raft snapshot from an explicitly
reviewed active OpenBao 2.7.2 cluster and verifies its local integrity offline.
It works through the same HTTPS API for RKE2, K3s, existing Kubernetes, Docker
and native deployments. It does not initialize, unseal, restore, rotate keys or
change a source policy. Storage must use the reviewed three-voter Raft topology.

This uses the versioned
[Raft snapshot API](https://openbao.org/docs/api/system/storage/raft/),
[self-capability API](https://openbao.org/docs/api/system/capabilities-self/)
and [snapshot archive format](https://github.com/openbao/openbao/blob/v2.7.2/internal/physical/raft/snapshot/archive.go).
The helper does not claim other software families are compatible.

## Prepare a private scope and credential

Copy [the example](../examples/openbao-snapshot.json) into ignored private
storage. Replace the HTTPS origin, original cluster UUID, exact advertised
Raft node IDs/addresses, runner-local trusted CA file and its byte SHA-256.
Use an explicit active member or an entry point routing to the active member.
Standby/sealed/uninitialized nodes and redirects are refused; the helper never
forwards a bearer credential to a redirected origin. No proxy or default
kubeconfig is used.

The source must report the expected version and cluster identity and exactly
the three original voters with one leader. These observations are repeated
after transfer. They do not prove independent physical failure domains or
storage replication health.

Provide a dedicated backup credential using the declared environment-variable
reference. This is a suitable policy shape for the helper:

```hcl
path "sys/storage/raft/configuration" { capabilities = ["read"] }
path "sys/storage/raft/snapshot" { capabilities = ["read"] }
path "sys/capabilities-self" { capabilities = ["update"] }
```

Review the effective identity/group policies too. The helper requires exact
read capability on the two Raft paths and deny on force restore, token creation
and its reserved ACL-write test path. Root credentials and broader permissions
on those paths are refused. This capability query is read-only despite its
HTTP POST method. The helper does not create the policy or issue a token.
Snapshot read access covers the cluster's encrypted state, so restrict custody
and retention of the credential and backup artifacts appropriately.

## Capture a new owner-private backup

Local validation constructs no network client and reads no credential or CA:

```bash
python3 scripts/openbao_snapshot.py --plan private/openbao-snapshot.json
```

Create a private backup parent first. Set the scoped credential in its declared
environment variable using a private prompt or approved secret provider; never
put it in the plan or a command argument. Then capture into a new directory:

```bash
python3 scripts/openbao_snapshot.py \
  --plan private/openbao-snapshot.json \
  --capture --output-dir private/backups/openbao-001
```

An output directory must not already exist. A caller-selected absolute path
outside the repository is also supported when its parent exists. Use an
independent backup system and storage location for production; choosing a
different local path is not off-platform evidence. POSIX directories/files use
0700/0600; separately enforce private ACLs when using a Windows runner.

The HTTP request lifetime, transfer lifetime and compressed/uncompressed byte
bounds are explicit. The snapshot is streamed to `snapshot.part`, flushed and
synced, checked, then published without replacing an existing final file.
The final receipt is atomically committed only after all checks. A failed run
retains its directory and partial artifact with `captured: false`; it never
deletes an earlier backup or retries a different source automatically.

The checker drains gzip through its trailer to detect CRC/truncation failures,
rejects unknown/duplicated/link archive entries, verifies both internal
SHA-256 checksums, and binds Raft metadata membership, size, index and term.
It never extracts snapshot paths. The sealed checksum file must be present,
but the checker cannot authenticate it without the original seal arrangement.
`seal_authenticity_verified` therefore remains false.

## Verify without a running source

Keep the original private plan, `snapshot.tar.gz` and
`openbao-snapshot-proof.json` together. The plan contains only references and
identity observations; no bearer credential is stored in the receipt.

```bash
python3 scripts/openbao_snapshot.py \
  --plan private/openbao-snapshot.json \
  --verify --output-dir private/backups/openbao-001
```

Offline verification contacts no API and needs no environment credential or
CA file contents. It checks the original receipt digest, plan binding, snapshot
digest and complete archive integrity again. The receipt records the creating
source closure. Historical receipts can still be verified; a separate
`creator_source_matches_current` flag indicates whether that closure equals
the current checker. These hashes are integrity commitments, not signatures of
an operator or proof against an attacker replacing both file and receipt.

Preserve the approved Shamir shares or KMS/HSM recovery dependencies separately
from the snapshots and platform. Retain independently retrievable bootstrap
software, plans, public trust material and recovery instructions. Snapshot
capture alone cannot prove that those inputs are sufficient to recover.
Restore, off-platform, HA and full-stack flags stay false in this command.

## Recovery acceptance scope

`python3 scripts/test_openbao_snapshot.py` tests bounded parsing, corruption,
truncation, unsafe archive members, wrong cluster/member identity, credential
scope, partial preservation and offline verification. It runs in default CI.

The opt-in real [Raft/TLS fixture](OPENBAO_HA.md) now captures using the actual
helper with a dedicated restricted token. Its recovery case stops every source
process, prepares a different empty Raft directory and a separately initialized
single-node target, then exercises restore there. The target has no retry-join
configuration pointing to source nodes. Normal restore with a different seal
is expected to fail before the fixture uses the force API on only that new
disposable target. The original source unseal shares, cluster UUID, data witness,
token restrictions and root revocation must survive recovery. The restored
configuration must identify the new target as its sole voting leader and must
commit and read back a new write. Source-independent offline backup verification
is repeated while the source processes remain down. These checks passed against
the pinned real 2.7.2 executable in the isolated Linux fixture; they are not
inferred from archive parsing or a mocked restore response.

This is single-host isolated acceptance, not a general production restore
command. The fixture never applies force restore to a caller-provided existing
cluster. Physical-node/storage loss, off-platform custody/retrieval, KMS/HSM
recovery, Kubernetes/Docker restore orchestration, upgrades, RPO/RTO and full
platform disaster recovery remain separate requirements.
