# OpenBao Initialization and Recovery Ceremony

OpenBao initialization creates the root encryption material for the entire
secret-management cluster. Treat it as a controlled production change, not as
another bootstrap command. The ceremony must have a named operator, an
independent approver, distinct key custodians, and an approved recovery test.

The project deliberately does not generate or retain unseal shares, recovery
shares, root tokens, HSM PINs, or KMS credentials. It validates sanitized
evidence that those materials were handled correctly. OpenBao documents that a
shared HA storage backend is initialized only once and that PGP recipients can
encrypt every generated share and the initial root token at creation time:

- <https://openbao.org/docs/commands/operator/init/>
- <https://openbao.org/docs/commands/operator/unseal/>
- <https://openbao.org/docs/configuration/seal/>

## Choose The Seal Before Initialization

For enterprise production, prefer an approved HSM or cloud KMS auto-unseal
provider. Keep its credentials outside Git and Kubernetes manifests, use
workload identity where the provider supports it, and retain at least five
PGP-encrypted recovery shares with a threshold of three.

Use `shamir-pgp` only when no approved HSM/KMS trust anchor is available. The
minimum accepted custody arrangement is:

- Five shares and a threshold of three.
- Five distinct custodians, one share per custodian.
- A separate PGP recipient for the initial root token.
- Encryption of every share and the root token during `operator init`.
- At least two offline escrow copies in separate failure domains.
- No plaintext share or token in shell history, terminal capture, CI output,
  Ansible output, tickets, chat, Git, or Kubernetes Secrets.

Do not use a static auto-unseal key unless an existing external secret system
already provides an equivalent root of trust and the security owner explicitly
accepts its rotation and revocation limitations.

## Ceremony Procedure

1. Open an approved change record and name the operator, approver, five key
   custodians, root-token recipient, recovery objectives, and seal method.
2. Ensure the selected private GitOps revision is reviewed and deployed. Record
   its commit and compute the profile-specific OpenBao application-tree digest:

   ```bash
   PLATFORM_PROFILE=premium-3node make platform-openbao-ceremony-digest
   ```

3. Verify each public PGP key out of band. Record only a SHA-256 of each full
   fingerprint in the evidence record. The five fingerprint hashes must be
   unique; the root-token recipient must be a sixth identity.
4. In a controlled, non-recorded operator session, make only the public keys
   available to the OpenBao CLI execution environment. Initialize exactly one
   server with five shares, threshold three, all five PGP public-key paths, and
   the separate root-token PGP key:

   ```bash
   bao operator init \
     -key-shares=5 \
     -key-threshold=3 \
     -pgp-keys=/secure-public/key-1.asc,/secure-public/key-2.asc,/secure-public/key-3.asc,/secure-public/key-4.asc,/secure-public/key-5.asc \
     -root-token-pgp-key=/secure-public/root-recipient.asc \
     -format=json
   ```

   Redirect the encrypted result directly into approved offline custody. Do not
   print it into an automation log. If auto-unseal is configured, use recovery
   shares and `-recovery-pgp-keys` instead.
5. Each required custodian decrypts their own share independently. Run
   `bao operator unseal` without a key argument so the CLI prompts without
   echoing or placing the share in command history. Apply the threshold to every
   sealed Raft member.
6. Use the encrypted initial root token only long enough to enable an audit
   device, configure Kubernetes authentication, establish a least-privilege
   administrative path, and verify access. Revoke the initial root token.
7. Restart one standby at a time and prove the approved unseal or auto-unseal
   recovery path. Then perform a quorum recovery exercise inside the accepted
   RTO. Do not seal all members simultaneously merely to create evidence.
8. Run `make platform-openbao-verify`. Retain the single sanitized
   `cluster_id_sha256` value, never the raw cluster identifier.
9. Complete a private copy of
   [`examples/openbao-ceremony-evidence.example.json`](../examples/openbao-ceremony-evidence.example.json)
   and obtain independent approval.

## Evidence Verification

The evidence record contains no key material, token, provider credential, raw
PGP fingerprint, or raw OpenBao cluster identifier. It remains private because
operator identities, timing, and custody metadata are operational information.

Validate it against the current profile configuration:

```bash
EVIDENCE=private/openbao-ceremony/CHG-OPENBAO.json \
PLATFORM_PROFILE=premium-3node \
make platform-openbao-ceremony-evidence-verify
```

The verifier rejects self-approval, fewer than five shares, thresholds below
three, duplicate custodians, a shared root-token recipient, plaintext retention,
fewer than two escrow copies, missing bootstrap controls, stale recovery tests,
changed OpenBao configuration, and a different live cluster identity.

Production evidence also requires the record explicitly:

```bash
PLATFORM_OPENBAO_CEREMONY_EVIDENCE_FILE=private/openbao-ceremony/CHG-OPENBAO.json \
PLATFORM_RELEASE_ID=<APPROVED_CHANGE_ID> \
PLATFORM_EVIDENCE_OPERATOR=<OPERATOR_ID> \
PLATFORM_EVIDENCE_APPROVER=<INDEPENDENT_APPROVER_ID> \
make platform-production-evidence
```

The schema-v6 production packet copies the ceremony record below the ignored
`private/production-evidence/` directory, binds it by SHA-256, verifies its
configuration digest, and matches its hashed cluster identity to the live
OpenBao readiness output. Its `sourceCommit` must also equal the exact revision
accepted by the production packet.
