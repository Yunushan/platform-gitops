# Forge Coexistence Transition Runbook

This workflow is optional and dormant by default. Platform bootstrap, first
deployment, seed synchronization, health checks, and normal Argo CD operations
never invoke it. Use it only when GitLab or GitHub must remain the writable Git
source while Forgejo, Woodpecker, Harbor, and Argo CD become the only active
CI/CD authority.

The transition helper supports these directions:

- GitLab to Forgejo
- GitHub to Forgejo

It does not claim that arbitrary GitLab CI or GitHub Actions pipelines can be
translated losslessly. It proves the contract declared in the plan: portable
repository data, explicitly mapped CI files, variables and secret metadata,
schedules, runner labels, protections, integrations, service health, writer
policy, and authority state. Any newly discovered or unmapped required surface
fails closed.

Use [Pipeline Compatibility And Conversion](FORGE_PIPELINE.md) before shadow
preparation when a source CI file needs conversion into Woodpecker. The
converter handles a reviewed common subset and emits a redacted report; it
blocks unsupported provider semantics instead of silently dropping them. The
resulting Woodpecker file remains a reviewed Forgejo commit and must be included
in the transition plan's pipeline mapping.

Use [Forge Migration](FORGE_MIGRATION.md) for repository-only transfers. Use
[GitLab to Forgejo Cutover](FORGE_CUTOVER.md) for an immediate GitLab freeze and
handover. This runbook is for a coexistence period followed by an optional
final freeze.

## Authority Model

The durable state file records these phases:

```text
planned -> shadow -> transition -> finalized
             ^          |             |
             |          |             +-> failback -> rolled-back
             |          +-> rollback -------> rolled-back
             +---------- fallback
             +---------- automatic fallback
```

- `planned`: no transition-owned destination or relay is assumed.
- `shadow`: source Git and source CI remain active. Forgejo is mirror-writer
  only, Woodpecker deployment authority is disabled, and canaries must pass.
- `transition`: source Git remains writable, but GitLab CI or GitHub Actions is
  disabled. The relay continues to synchronize new commits. Woodpecker and
  Argo CD hold deployment authority.
- `finalized`: the source repository is archived, a final zero-drift relay has
  passed, the relay is stopped, and the approved Forgejo maintainer policy is
  active.
- `rolled-back`: Woodpecker authority is disabled, the source CI snapshot is
  restored, and the managed relay is stopped. This phase can be reached by a
  pre-finalization rollback or a verified post-finalization failback.
- `rollback-failed`: at least one authority restoration could not be verified.
  Treat this as an incident and do not enable either deployment path manually.

The transition ordering is fixed: reconcile, snapshot source CI, disable source
CI, enable destination authority, then verify. A failure after source CI is
disabled automatically restores the snapshot and disables destination
authority. `transition_control.auto_rollback` cannot be false.

Choose the recovery operation from the current phase:

| Operation | Valid from | Source CI after success | Relay after success | Intended use |
| --- | --- | --- | --- | --- |
| `fallback` | `shadow`, `transition`, `rollback-failed` | Restored | Running | Temporary return to the old CI while commits continue to mirror into Forgejo |
| `rollback` | `shadow`, `transition`, `rollback-failed` | Restored | Stopped | End coexistence before finalization and return authority to the old provider |
| `failback` | `finalized` | Restored only after reverse-sync verification | Stopped | Return post-finalization Forgejo changes to GitLab or GitHub and restore old-provider authority |

Fallback and rollback never reverse-sync Forgejo-only commits. They are valid
before finalization because Forgejo remains mirror-only during `shadow` and
`transition`. Failback is the only recovery operation that moves finalized
Forgejo history back to the old provider.

## Relay Model

The provider-neutral relay always verifies Git refs, Git LFS, wiki, and the
portable metadata selected by the migration plan.

- `external` is required for GitHub and available for GitLab. Run the relay
  command under an external service supervisor.
- `gitlab-push` manages or verifies a GitLab native push mirror for fast Git-ref
  propagation. The external reconciliation still runs because a Git push
  mirror alone does not prove LFS, wiki, or portable metadata parity.

During `shadow` and `transition`, Forgejo branch protection permits only the
declared mirror actor to push. This prevents users or CI jobs from creating a
second writable history. Finalization replaces that policy with the declared
maintainer policy.

## Prepare A Private Plan

Copy one credential-free example into the ignored `private/` tree:

```bash
cp examples/migrations/gitlab-to-forgejo.transition.example.json \
  private/migrations/forge-transition.json
```

For GitHub, use
`examples/migrations/github-to-forgejo.transition.example.json` instead.

Keep these rules in every private plan:

- Keep every `unmapped` policy set to `fail`.
- Use `managed` only when the helper owns the destination object.
- Use `mapped` when an existing object must be verified but not created.
- A `manual` or `skipped` decision requires explicit acceptance and a reason.
- Never place credentials or secret values in JSON. Plans contain only names of
  environment variables.
- GitHub APIs expose secret metadata, not secret values. Each managed GitHub
  secret mapping therefore requires a private `value_env` source.
- Keep `source_ci.keep_repository_writable` true for coexistence.
- Keep the shadow writer whitelist limited to exactly `mirror_actor`.
- Keep `transition_control.auto_rollback` true.
- Every managed Woodpecker deployment workflow must enforce the configured
  deployment gate marker.

Export the provider and service credentials named by the plan. Use
short-lived, least-privilege service accounts. Do not enable shell tracing while
credentials are present.

## Step 1: Validate

```bash
make forge-transition-validate \
  PLAN=private/migrations/forge-transition.json \
  PROOF=private/migrations/proof/plan.json

make forge-transition-proof-verify \
  PROOF=private/migrations/proof/plan.json
```

Validation is credential-free. It rejects unsupported directions, plaintext
credential fields, incomplete mappings, unsafe destination writer policy, a
GitHub plan using the GitLab-native driver, or disabled automatic rollback.

## Step 2: Discover

Discovery is read only:

```bash
make forge-transition-discover \
  PLAN=private/migrations/forge-transition.json \
  DISCOVERY=private/migrations/proof/discovery.json

make forge-transition-proof-verify \
  PROOF=private/migrations/proof/discovery.json
```

Review the proof in the change record. It must account for every declared
source pipeline, variable/secret metadata item, schedule, runner label,
protection rule, and integration.

## Step 3: Prepare Shadow

Preparation mutates the destination, so it requires the exact approved
discovery digest, the live-operation switch, and a change ticket:

```bash
export FORGE_TRANSITION_LIVE=1
export FORGE_TRANSITION_CHANGE_TICKET=CHG-000000
export FORGE_TRANSITION_PREPARE_CONFIRM="$(jq -r .proof_sha256 \
  private/migrations/proof/discovery.json)"

make forge-transition-prepare \
  PLAN=private/migrations/forge-transition.json \
  DISCOVERY=private/migrations/proof/discovery.json \
  STATE=private/migrations/state/transition.json \
  PREPARED=private/migrations/proof/prepared.json
```

This creates or reconciles Forgejo content, maps approved Woodpecker values,
keeps schedules disabled, keeps the deployment gate false, applies mirror-only
writer protection, and configures the selected relay. Source CI remains active.

## Step 4: Verify Shadow

```bash
make forge-transition-verify-shadow \
  PLAN=private/migrations/forge-transition.json \
  PREPARED=private/migrations/proof/prepared.json \
  STATE=private/migrations/state/transition.json \
  VERIFICATION=private/migrations/proof/shadow-verification.json \
  WORK_DIR=/var/lib/platform-forge-transition/work
```

The proof must verify repository parity, mapped CI/CD surfaces, destination
writer policy, Woodpecker agent labels and canary, Harbor access, Argo CD source
and health, and continued source-CI authority.

## Step 5: Enter Coexistence

Entering coexistence disables source CI but does not archive the source Git
repository:

```bash
export FORGE_TRANSITION_ENTER_CONFIRM="$(jq -r .proof_sha256 \
  private/migrations/proof/shadow-verification.json)"

make forge-transition-enter \
  PLAN=private/migrations/forge-transition.json \
  VERIFICATION=private/migrations/proof/shadow-verification.json \
  STATE=private/migrations/state/transition.json \
  HANDOVER=private/migrations/proof/handover.json \
  WORK_DIR=/var/lib/platform-forge-transition/work
```

For GitLab, schedules are paused, active pipelines are drained or explicitly
cancelled according to the plan, and build access is disabled. For GitHub,
active workflow runs are drained or cancelled and Actions is disabled through
the repository Actions permissions API. The source repository remains writable
in both cases.

## Step 6: Supervise The Relay

Run one reconciliation during commissioning:

```bash
make forge-transition-reconcile \
  PLAN=private/migrations/forge-transition.json \
  STATE=private/migrations/state/transition.json \
  PROOF=private/migrations/proof/reconcile.json \
  WORK_DIR=/var/lib/platform-forge-transition/work
```

Then run the long-lived relay under systemd, Kubernetes, or another process
supervisor that restarts failed processes and protects the credential
environment:

```bash
make forge-transition-relay \
  PLAN=private/migrations/forge-transition.json \
  STATE=private/migrations/state/transition.json \
  PROOF_DIR=private/migrations/proof/relay \
  INTERVAL=60
```

The state lock prevents two relay writers. Each loop releases the lock before
sleeping. Consecutive failures at the configured threshold automatically
disable destination authority, restore source CI, return the state to `shadow`,
and write proof.

## Step 7: Verify Operations

Run this from monitoring and before every release:

```bash
make forge-transition-status \
  PLAN=private/migrations/forge-transition.json \
  STATE=private/migrations/state/transition.json \
  PROOF=private/migrations/proof/status.json

make forge-transition-proof-verify \
  PROOF=private/migrations/proof/status.json
```

A passing status proves the current phase, source authority, destination
authority, writer policy, service health, and relay age against `max_lag_seconds`.
Alert on a nonzero exit, a stale relay, `rollback-failed`, or a state integrity
error.

## Step 8: Use A Temporary Fallback

Use fallback when source CI must resume temporarily but synchronization into
Forgejo should continue. Supply a recent `enter`, `status`, `reconcile`, or
automatic-rollback proof:

```bash
export FORGE_TRANSITION_FALLBACK_CONFIRM="$(jq -r .proof_sha256 \
  private/migrations/proof/status.json)"

make forge-transition-fallback \
  PLAN=private/migrations/forge-transition.json \
  STATE=private/migrations/state/transition.json \
  EVIDENCE=private/migrations/proof/status.json \
  FALLBACK=private/migrations/proof/fallback.json
```

Fallback first disables Woodpecker deployment authority and applies the
mirror-only writer policy. It then restores the recorded source CI state and
keeps the managed relay enabled. The resulting phase is `shadow`, so the normal
shadow verification and enter steps are required before Woodpecker becomes
authoritative again.

## Step 9: Roll Back Coexistence

Use a recent `enter`, `status`, `reconcile`, or automatic-rollback proof:

```bash
export FORGE_TRANSITION_ROLLBACK_CONFIRM="$(jq -r .proof_sha256 \
  private/migrations/proof/status.json)"

make forge-transition-rollback \
  PLAN=private/migrations/forge-transition.json \
  STATE=private/migrations/state/transition.json \
  EVIDENCE=private/migrations/proof/status.json \
  ROLLBACK=private/migrations/proof/rollback.json
```

Rollback first disables Woodpecker authority and restores mirror-only access,
then restores the recorded source CI state and stops the managed relay. Verify
the resulting proof before allowing source deployments.

## Step 10: Finalize

Finalization is optional. Perform it only after the coexistence acceptance
window and a fresh healthy transition status:

```bash
export FORGE_TRANSITION_FINALIZE_CONFIRM="$(jq -r .proof_sha256 \
  private/migrations/proof/status.json)"

make forge-transition-finalize \
  PLAN=private/migrations/forge-transition.json \
  STATE=private/migrations/state/transition.json \
  EVIDENCE=private/migrations/proof/status.json \
  FINALIZATION=private/migrations/proof/finalization.json \
  WORK_DIR=/var/lib/platform-forge-transition/work
```

The command verifies transition health, archives the source, performs the final
reconciliation, disables the native relay, applies final Forgejo writer policy,
and verifies destination operations. Any failure restores the source archive
state, relay, and transition writer policy. It does not silently declare a
partial finalization successful.

## Step 11: Fail Back After Finalization

Use failback only when Forgejo has already become the final writable source and
authority must return to GitLab or GitHub. Use a fresh verified finalization or
finalized-status proof:

```bash
export FORGE_TRANSITION_FAILBACK_CONFIRM="$(jq -r .proof_sha256 \
  private/migrations/proof/status.json)"

make forge-transition-failback \
  PLAN=private/migrations/forge-transition.json \
  STATE=private/migrations/state/transition.json \
  EVIDENCE=private/migrations/proof/status.json \
  FAILBACK=private/migrations/proof/failback.json \
  WORK_DIR=/var/lib/platform-forge-transition/failback
```

The failback order is fixed and fail-closed:

1. Verify the finalized state and its retained source snapshots.
2. Disable Woodpecker deployment authority and lock Forgejo to mirror-only.
3. Unarchive the old source while keeping its CI disabled.
4. Reverse-sync and verify the declared portable contract from Forgejo to the
   old source.
5. Restore the original source CI snapshot only after verification passes.

If any step fails, the helper automatically disables the old source CI,
rearchives the old source, reapplies final Forgejo access and Woodpecker
authority, and returns to `finalized` when that recovery verifies. If automatic
recovery itself cannot be verified, the phase becomes `rollback-failed` and
neither side should be enabled manually. Failback requires a state created by a
version that retained both the original source CI snapshot and the finalization
snapshot.

## Evidence And Limits

Store the private plan, discovery, preparation, shadow verification, handover,
relay, status, fallback, rollback, finalization, failback, and automatic
recovery proofs, plus the integrity-checked state, in an access-controlled
evidence store. Local state and proof updates are owner-only and atomically
replace a complete prior file only after a durable write. Proofs are tied to
the plan digest and have a maximum approval age. State and proof tampering is
rejected.

A successful run means 100 percent of the declared, supported contract was
verified. It is not proof that every provider-specific behavior was translated.
Unsupported pipeline semantics, external SaaS integrations, organization-wide
policy, runner operating-system state, and secret values unavailable from a
source API must be explicitly supplied, mapped, or accepted as manual work.
Production acceptance still requires a credentialed rehearsal with disposable
repositories before the first real handover.
