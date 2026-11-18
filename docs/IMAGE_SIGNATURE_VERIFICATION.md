# Explicit image signature verification

This optional helper verifies one digest-pinned private Harbor image with the reviewed Cosign v3.1.4 Linux executable. It requires a release public key, a Sigstore trusted-root JSON containing approved transparency-log keys, a registry CA and a current Harbor token scoped to that repository's pull action. Every file and the tool are hash-pinned in the private plan. The tool does not hold a signing key or a registry password.

Use an ignored private JSON plan with these exact fields:

- `schema_version`: `1`.
- `image`: the canonical registry/repository URI ending in `@sha256:<64 lowercase hex>`.
- `cosign_binary`, `registry_ca`, `public_key`, `trusted_root`, `credential`: objects with an absolute `path` and its `sha256`.
- The tool hash must equal `424e2c97d0b45fd7a6b608a87f090f35633f4c23ece5f092d48bba11aaf4bbfa`.

The private credential file contains only `registry`, `repository` and `token`. The selected registry/repository and the token's audience, validity and exact pull-only access must match. It is not a Docker password or a publisher robot credential. Harbor authenticates the token; local claim parsing bounds the credential offered to the registry.

`python3 scripts/verify_image_signature.py --plan <private-plan>` checks local input bindings without contacting services. It does not establish a valid signature. On Linux, use a separate non-root verification identity and add `--verify --output <new-absolute-private-proof.json>` to perform verification. The output must be a new path under this checkout's ignored `private` directory. Failures do not produce a positive proof or retry a refused verification.

The helper snapshots the tool, public trust inputs and pull-only Docker authorization into its own temporary directory. It starts Cosign with a minimal environment and explicit registry CA/public-key/trusted-root arguments. Both transparency-log and SCT bypass switches are explicitly false. No public TUF defaults, ambient Docker configuration, registry password argument, signing-key environment or proxy setting is inherited. The reviewed Cosign output must match the image-signing predicate, selected reference and manifest digest. The signed subject binds the content digest; the selected repository is the fetch location, not an independent release-authorization claim. The temporary directory is removed after the bounded command.

A successful receipt records hashes and image-signature/transparency/TLS verification. Optional schema `2` additionally requires an `annotations` object containing 1 to 16 explicit signed key/value pairs. Keys and values are bounded printable strings without CSV separators or control characters. The helper passes each requirement to Cosign and independently checks the returned signed `optional` map. A different or absent required value refuses verification; unrelated signed annotations are allowed. Schema `1` remains the original content-only verification path. Admission, CI evidence, release authorization, HA and full-stack verification remain false. The optional [Gerrit signed promotion flow](GERRIT_ARTIFACT_PROMOTION.md) binds the signature to live merged CI evidence. The [joined K3s admission test](KYVERNO_INSTALLATION.md#optional-persistent-native-ci-watcher-join) exercised that binding from review through enforcement and recovery.

## Actual local protocol acceptance

`test_valkey_gateway.GatewayTests.test_real_harbor_image_signature_verification` is gated by `HARBOR_IMAGE_SIGNATURE_TEST_RUNTIME=1`. It uses the existing actual Harbor/pool-health/PostgreSQL/cache laboratory, official Cosign v3.1.4 and Rekor POSIX v2.4.1 artifacts, and a TLS transparency log with its own private key. The checkpoint origin matches the log URL hostname, as required by the reviewed checkpoint verifier. Signer, log and pull-only verifier use distinct disposable numeric Linux identities; no host account is created. Private signing keys must be inaccessible to the verifier and the signer cannot read the log key.

The case signs a real private-registry image, requires an actual log inclusion proof and tests unsigned, wrong-key, wrong-log, wrong signed-policy and absent required-annotation refusal. Its two fixture annotations exercise cryptographic binding; they are not real CI provenance. It retains the original log process identity, public verification inputs, exact image manifest and Sigstore bundle in ignored private evidence. It stops the log after signing and checks the original signature and embedded inclusion proof through the existing Harbor peer-loss and cache/database promotion phases. Artifact variables are `COSIGN_TEST_BINARY`, `REKOR_POSIX_TEST_BINARY` and `SIGSTORE_TEST_REPORT`, in addition to the existing laboratory inputs.

This protocol case does not establish Kubernetes admission, CI-to-signature authority, persistent log recovery, independent witnesses, gossip/consistency monitoring, log HA or complete platform qualification. The existing audit-first policies and explicit enforcement settings are unchanged. A local log is not the public Sigstore service.

## Upstream compatibility

The default deployment profile pins Kyverno 1.18.1. Its key-based verifier initializes trusted material differently from the newer bundle path. The joined admission tests used the optional Kyverno 1.19.2 installation and an explicit private TUF trust mirror. The Cosign receipt and the Kubernetes admission evidence cover these separate checks. [Optional Kyverno installation](KYVERNO_INSTALLATION.md).

Sources: [Cosign annotation verification](https://docs.sigstore.dev/cosign/verifying/verify/), [Cosign custom trust configuration](https://docs.sigstore.dev/cosign/system_config/custom_components/), [Kyverno ImageValidatingPolicy](https://kyverno.io/docs/policy-types/image-validating-policy/).

The local protocol runtime calls the verification operation under its dedicated identity. Persistent service packaging and fleet installation remain separate work. After teardown, an independent network-isolated audit verifies the retained DSSE signature and checkpoint with OpenSSL, checks the Merkle inclusion and manifest hash, and re-verifies the bundle with Cosign while rejecting a modified manifest.

Pinned verifier source reviewed for compatibility: [Kyverno 1.18.1 options](https://github.com/kyverno/kyverno/blob/ec14520a11cc25432482bfc0baa6a61d3c309524/pkg/image/verifiers/ivpol/cosign/opts.go).
