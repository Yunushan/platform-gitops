# Policies

Optional policy examples for teams that use admission control. They are not applied by default.

All examples are intentionally set to audit/starter posture. Copy them into a
private policy repo, replace placeholders such as `<NAMESPACE>`, test with real
traffic, then promote enforcement one namespace at a time.

Included examples:

- `kyverno/no-plaintext-secrets.example.yaml` requires Secret manifests to
  declare their private secret workflow before they are promoted.
- `kyverno/require-workload-baseline.example.yaml` audits resource requests and
  basic pod security intent.
- `kyverno/verify-signed-images.example.yaml` audits Cosign/Sigstore image
  signatures and digest mutation with Kyverno's stable
  `policies.kyverno.io/v1` `ImageValidatingPolicy` API.
- `network/default-deny.example.yaml` starts a namespace default-deny baseline.
- `network/allow-platform-dns-and-ingress.example.yaml` allows DNS plus Traefik
  ingress traffic for a namespace after default-deny is enabled.

Run `python scripts/test_policy_examples.py` before publishing changes to keep
the example set documented and safe-by-default.

For dependency drift, `renovate.json` enables Renovate's recommended preset,
dependency dashboard, grouped Helm updates, and Docker digest pinning. Use it
with the Cosign policy example after CI signs images. Replace
`<COSIGN_PUBLIC_KEY>` with the approved PEM public key, begin with `Audit`, and
promote to `Deny` only after a signed admission canary passes and an invalid
digest is rejected. The premium profile automates this private rendering and
live proof through `PLATFORM_IMAGE_INTEGRITY_MODE` and
`PLATFORM_IMAGE_INTEGRITY_CANARY_IMAGE`; see `docs/SUPPLY_CHAIN.md`.
