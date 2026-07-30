# Supply-Chain Verification

The repository separates fast source-contract tests from release-grade
supply-chain evidence. Production acceptance requires both. A generated file
or an installed scanner is not counted as proof unless its validation command
completed successfully for the exact Git revision being promoted.

## Pull Request Gate

The required GitHub `validate` job performs these checks on every push and
pull request:

- Gitleaks scans full Git history with redacted findings.
- Semgrep executes the repository security rules and fails on findings.
- Trivy scans dependencies, secrets, and infrastructure misconfiguration and
  fails on fixed HIGH or CRITICAL findings.
- A checksum-pinned Kyverno CLI `v1.18.1` compiles and behavior-tests every
  active CEL admission policy against positive, negative, and exemption fixtures,
  and compiles the stable image-signature policy without registry access.
- Syft produces an SPDX JSON SBOM.
- `scripts/verify_supply_chain_evidence.py` rejects malformed or empty SBOMs.
- Kustomize and Helm render every complete base and premium application, then
  Kubeconform validates built-in Kubernetes objects against the target schema.
- Coverage.py traces Python subprocesses and enforces the measured 81.0% branch
  coverage ratchet across the forge migration, cutover, and transition engines.
- The SBOM is retained as a workflow artifact for 30 days.

All third-party GitHub Actions are pinned to full commit SHAs. The separate
weekly OpenSSF Scorecard workflow publishes results and uploads SARIF to code
scanning.

## Migration Parser Fuzzing and Coverage

`.github/workflows/fuzz.yml` uses ClusterFuzzLite and Atheris to fuzz the
credential-free JSON plan parsers for migration, immediate cutover, coexistence,
rollback, and failback. Pull requests that change those surfaces run
`code-change` fuzzing. A weekly batch grows the corpus and is followed by corpus
pruning. The ClusterFuzzLite actions and Python builder image are pinned to an
immutable commit and digest. The pure-Python wrapper intentionally does not use
`LD_PRELOAD`. The repository `.dockerignore` excludes ignored private deployment
state, rendered evidence, local inventory/configuration, and local key material
from every Docker build context.

The target rejects inputs larger than 128 KiB and structures deeper than 64
levels. Expected fail-closed `MigrationError` results are normal corpus
outcomes; any other exception is retained as a crash. Seed inputs come from
the credential-free plans under `examples/migrations/`.

Run the deterministic fuzz contract locally with:

```bash
python scripts/test_forge_fuzz_contract.py
```

Install the pinned CI release of Coverage.py and reproduce the branch-coverage
evidence with:

```bash
python -m pip install coverage==7.15.2
bash scripts/forge-coverage.sh
```

The gate writes `rendered/coverage/forge-coverage.json` and
`rendered/coverage/forge-coverage.xml` before enforcing the 81.0% minimum.
`FORGE_COVERAGE_MIN` can raise the threshold for a branch, but lowering the
repository default requires an explicit reviewed source change. GitHub retains
the coverage directory as a workflow artifact for 30 days.

Every shipped GitHub, Forgejo, and Gitea Actions job declares a bounded timeout,
and each explicit checkout disables persisted credentials. GitHub-hosted jobs
also pin an operating-system runner instead of following a moving `*-latest`
label. GitLab validation jobs carry equivalent limits. The premium Woodpecker
server enforces a 60-minute default and a 120-minute maximum for repository
pipelines. Live forge migration acceptance is single-flight and never cancels
an in-progress evidence run.

Stable semantic-version tags run the attested release workflow. It validates
the tagged commit, creates a reproducible source archive, emits SPDX and
CycloneDX SBOMs, signs their checksum set through keyless Cosign, attaches
GitHub build and SBOM attestations, and publishes all evidence with the GitHub
release. Verification and independent environment approval run in separate
read-only jobs. Only the final downstream job receives write and OIDC
permissions, and it publishes a checksummed approved artifact without checking
out or executing repository source. Verification commands are documented in
`docs/RELEASE_GUIDE.md`. GitHub governance and release-evidence API clients
reject every redirect before a follow-up request can carry their bearer token;
workflow API endpoints must be canonical.

GitLab, Forgejo/Gitea Actions, and Woodpecker continue to run the portable
source-contract suite. Install Trivy, Gitleaks, Semgrep, Syft, Scorecard, and
Cosign on a promotion runner. Also install Kyverno CLI `v1.18.1` and run
`make policy-cel-verify`; use `make supply-chain-verify` as the provider-neutral
release gate.

## Rendered Manifest Schemas

Install Kustomize `v5.8.1`, Helm `v3.21.0`, and Kubeconform `v0.7.0`, then run:

```bash
make rendered-schema-verify
make rendered-private-schema-verify
```

The default production run is strict: unresolved values, an empty render,
Helm or Kustomize failure, and Kubernetes schema failures all block promotion.
The public pull-request workflow checks both `base` and `premium-3node`; it may
skip only applications that still contain documented public-template
placeholders and retains the exact skip list plus rendered reports for 30
days. A second CI gate renders every premium application through the real
private-values renderer using non-secret `.example.test` fixtures, rejects all
skips, and schema-validates the resulting complete profile. A private production
profile must still render without that allowance against its exact release commit.
Successful raw manifests and render stdout are temporary because charts can
generate ephemeral Secret material. Retained artifacts contain manifest hashes,
sanitized render metadata, Kubeconform reports, and the aggregate summary only.

Kubeconform validates built-in Kubernetes resources strictly and reports but
does not fail on missing third-party CRD schemas. The live server-side checks
in `platform-production-check` remain required because OpenAPI validation does
not exercise admission webhooks or controller behavior.

## Local Scans

Run the repository scanners without generating release evidence:

```bash
make security-scan
```

The Trivy filesystem gate scans maintained, deployable source. It excludes
vendored Helm chart trees, generated `rendered/` evidence, negative test
fixtures, language examples, and incomplete Kustomize patch fragments. Vendored
chart versions and images remain pin-checked, assembled profiles are validated
with Kubeconform and policy contracts, and Gitleaks still provides the repository
secret boundary. The build-only ClusterFuzzLite Dockerfile is also excluded from
the runtime `USER` rule because that image is never shipped.

Generate an SPDX SBOM and any available Scorecard or Cosign evidence:

```bash
make supply-chain-posture
```

The non-strict posture command reports missing optional tools. It must not be
used as production acceptance.

## Strict Production Evidence

Strict mode requires:

- Trivy, Gitleaks, and Semgrep gates to pass.
- A non-empty SPDX SBOM generated by Syft.
- An OpenSSF Scorecard report at or above `SUPPLY_CHAIN_MIN_SCORE`, default
  `7.0`.
- At least one successful Cosign verification for a digest-pinned image.

Create an ignored private inventory with one image and public-key path per
line. The image must use an immutable SHA-256 digest. Relative key paths are
resolved from the repository root.

```text
registry.example.test/platform/api@sha256:<64_HEX_CHARACTERS>|private/supply-chain/release.pub
```

Then run:

```bash
COSIGN_IMAGES_FILE=private/supply-chain/cosign-images.txt \
make supply-chain-verify
```

The inventory should cover every organization-owned image in the release.
Public verification keys are not secrets, but private registry names and the
approved image inventory can be deployment-sensitive and should remain in the
ignored private evidence area. Inventory and reconciliation outputs are written
atomically with owner-only mode `0600`; a failed write leaves the prior complete
artifact intact.

`platform-production-check` invokes this strict gate. A missing scanner,
missing Scorecard report, score below threshold, tag-only image, absent key,
failed Cosign verification, or empty SBOM blocks production acceptance.

## Exact Rendered and Runtime Inventory

The Cosign inventory is not accepted as complete merely because every row in it
verifies. Production runs `make platform-image-inventory-verify`, which depends
on strict schema rendering and strict supply-chain verification, then:

1. captures every regular, init, and ephemeral container from all live Pods;
2. retains only namespace, Pod, node, container, declared image, and runtime
   image ID, never Pod environment values or mounted Secret data;
3. requires each runtime image ID to resolve to an immutable SHA-256 digest;
4. extracts image references from every successfully rendered application in
   the exact selected profile, with no skipped applications;
5. resolves rendered tags through the observed live digest or an explicitly
   approved dormant-image exception;
6. requires every private-registry digest in the Cosign report and inside the
   live Kyverno admission scope; and
7. requires every image outside that admission scope to have a current,
   independently approved exception bound to a Trivy JSON report by SHA-256.

Copy `examples/image-inventory-exceptions.example.json` to an ignored private
path and populate it only for genuine upstream or dormant-image gaps:

```bash
mkdir -p private/supply-chain
cp examples/image-inventory-exceptions.example.json \
  private/supply-chain/image-exceptions.json

PLATFORM_IMAGE_REGISTRY=registry.example.test \
PLATFORM_IMAGE_INVENTORY_EXCEPTIONS_FILE=private/supply-chain/image-exceptions.json \
COSIGN_IMAGES_FILE=private/supply-chain/cosign-images.txt \
make platform-image-inventory-verify
```

Exceptions are limited to 90 days, require different owner and approver
identities, a ticket and reason, an exact digest, and an intact vulnerability
report below `private/supply-chain`. They cannot authorize an unsigned image in
the private registry: those images must be signed because the production
admission policy applies there. An outside-registry image needs an exception
even when its release signature verifies, because the platform admission policy
does not intercept that registry.

## Admission-Time Image Integrity

The premium profile includes a separate `platform-image-integrity` Argo CD
Application with a stable Kyverno `policies.kyverno.io/v1`
`ImageValidatingPolicy`. Keeping it separate allows a controlled migration:
the public template starts disabled, private deployments begin in `Audit`, and
production promotion uses `Deny`. The policy applies only to the configured
private registry, fails closed on webhook errors, resolves tags to digests,
requires verification, and verifies the admitted digest against the approved
Cosign public key and Rekor transparency log.

Configure an ignored private environment file:

```bash
PLATFORM_IMAGE_INTEGRITY_MODE=Audit
PLATFORM_IMAGE_REGISTRY=registry.example.test
PLATFORM_COSIGN_PUBLIC_KEY_FILE=/secure/path/platform-cosign.pub
PLATFORM_COSIGN_REKOR_URL=https://rekor.sigstore.dev
```

`PLATFORM_IMAGE_REGISTRY` defaults to the configured Harbor host when present.
The key file must contain exactly one ASCII PEM `PUBLIC KEY`; private keys are
rejected. Render and sync the private profile, then run:

```bash
make platform-render-private-values
make platform-seed-git-sync
PLATFORM_IMAGE_INTEGRITY_MODE=Audit make platform-policy-readiness
```

Remediate audit findings and sign every organization-owned image before
promotion. Production proof requires both a genuinely signed digest and a
derived invalid digest. The API server must admit the signed Pod manifest and
Kyverno must reject the invalid one:

```bash
PLATFORM_IMAGE_INTEGRITY_MODE=Enforce \
PLATFORM_IMAGE_INTEGRITY_REQUIRED=true \
PLATFORM_IMAGE_INTEGRITY_CANARY_IMAGE=registry.example.test/platform/canary@sha256:<64_HEX_CHARACTERS> \
PLATFORM_IMAGE_INTEGRITY_CANARY_NAMESPACE=kyverno \
make platform-policy-readiness
```

For a private registry, pre-create a pull Secret in the canary namespace and
set `PLATFORM_IMAGE_INTEGRITY_CANARY_PULL_SECRET`. Kyverno admission and
background controllers receive only `get` access to Secrets so they can resolve
Pod `imagePullSecrets`; they do not receive `list`, `watch`, or write access.

Key rotation is a release event. Sign the complete inventory with the new key,
temporarily return the policy to `Audit`, render and sync the new public key,
prove the new signed canary, then restore `Enforce`. Do not remove the old
signatures or key approval evidence until rollback and restore windows expire.

Setting `PLATFORM_IMAGE_INTEGRITY_MODE=disabled` restores public placeholders
instead of leaving stale private key or registry data in the worktree. The
image-integrity app then becomes incomplete and is omitted from a
`skip-incomplete` registration. If it was previously active, review and approve
the guarded Argo CD prune; the live readiness gate does not treat disabled as
safe while the policy remains installed.

## Evidence Files

The default ignored evidence directory is `rendered/supply-chain/`:

- `platform-gitops.spdx.json`: source SBOM.
- `scorecard.json`: OpenSSF Scorecard report.
- `cosign-verification.json`: digest-bound images successfully verified by
  Cosign.
- `live-image-inventory.json`: sanitized live Pod image identities and immutable
  runtime digests.
- `image-inventory-evidence.json`: commit-bound reconciliation of exact rendered
  and live images against Cosign and admission scope.

`scripts/verify_supply_chain_evidence.py` supports independent verification:

```bash
python scripts/verify_supply_chain_evidence.py \
  --sbom rendered/supply-chain/platform-gitops.spdx.json \
  --scorecard rendered/supply-chain/scorecard.json \
  --signature-report rendered/supply-chain/cosign-verification.json \
  --strict
```

Supply-chain reports and evidence are read through the shared local-input
bound before JSON parsing or hashing. The default is 64 MiB; any measured
override through `PLATFORM_FILE_INPUT_MAX_BYTES` must remain within the 512 MiB
hard ceiling.

Those JSON documents also use the shared strict decoder. Duplicate object keys,
non-standard `NaN`/`Infinity` constants, and numeric overflow are rejected so
the producer, verifier, and retained evidence cannot assign different meanings
to the same bytes. Structures deeper than 128 containers or larger than
1,000,000 total nodes are also rejected before evidence evaluation.

The schema-v6 production evidence generator copies the exact image inventory
report into its private packet and binds it by SHA-256. Retain that packet. Do not
commit private registry inventories, internal identities, credentials, or
private operational reports to this public repository.

## Limitations

Runtime reconciliation proves the images present at the capture time. A dormant
CronJob, install hook, or operator-generated image that has never appeared live
cannot be resolved from a tag by observation and therefore blocks acceptance
unless its exact digest is covered by a current exception. Re-run the gate after
each chart, operator, node-image, or workload release; prior reports do not
authorize a changed runtime.
