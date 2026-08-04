# GitLab to Forgejo Cutover Runbook

The cutover orchestrator is optional and dormant by default. It is never called
by platform bootstrap, first deployment, seed synchronization, or normal health
checks. Use it only for a repository that is intentionally moving deployment
authority from GitLab CI to Forgejo, Woodpecker, Harbor, and Argo CD.

The tool extends the portable repository migration described in
[Forge Migration](FORGE_MIGRATION.md). It does not pretend that GitLab CI YAML,
runners, or provider-specific controls can be translated losslessly. Instead,
every discovered surface must be managed, mapped, or explicitly accepted as a
manual/skip decision. An unsupported or newly discovered unmapped item blocks
the cutover.

Before shadow preparation, use [Pipeline Compatibility And Conversion](FORGE_PIPELINE.md)
for each source pipeline that is intended to become a Woodpecker workflow. The
converter produces reviewed destination YAML for the supported subset and a
redacted report; it fails closed for lossy constructs. The generated file must
then be committed to Forgejo and listed in the plan mapping. A repository
transfer proof by itself never proves CI equivalence.

If GitLab or GitHub must remain writable while its CI is disabled and commits
continue to relay into Forgejo, use the optional
[Forge Coexistence Transition Runbook](FORGE_TRANSITION.md) instead. This
cutover workflow freezes GitLab during activation; it is intentionally not the
long-lived coexistence workflow.

## Safety Model

The workflow has five explicit phases:

1. `discover` performs a read-only inventory of GitLab pipeline files and
   external includes, project/group variable metadata, schedules, runner tags,
   protected branches, project hooks, and native push mirrors. It also checks
   whether the Forgejo destination exists. Destination workflow enforcement is
   deferred so an explicitly managed missing or stale repository can be created
   and mirrored during shadow preparation.
2. `prepare` performs an approved shadow migration. It mirrors repository data,
   activates the Woodpecker repository, copies only explicitly managed variable
   values directly into Woodpecker secrets, creates disabled Woodpecker cron
   entries, reconciles managed Forgejo protections, configures Harbor access,
   and leaves the deployment gate false.
3. `verify` re-reads both sides, checks Woodpecker agent labels, secrets, disabled
   schedules, mapped Forgejo workflow files and their deployment gate marker,
   Forgejo protections/hooks, Harbor evidence, and Argo CD health, then runs a
   shadow canary.
4. `activate` requires a fresh verification digest, a live-operation switch,
   and a change ticket. It snapshots rollback state, freezes every GitLab
   source, performs the final mirror, enables Woodpecker authority, and runs a
   post-cutover canary. Destination authority is never enabled while an
   unfrozen source repository remains in the batch.
5. `rollback` disables Woodpecker authority and restores the recorded GitLab CI,
   schedule, and archive state. Activation also attempts this rollback
   automatically when its final sync or canary fails.

Activation writes an atomic checkpoint before and after each authority change.
If the process or operator terminal is interrupted, use that checkpoint as
rollback evidence. Store plans, proofs, and checkpoints only in the ignored
`private/` tree or an access-controlled evidence system.

## Prepare The Plan

Copy the public, credential-free example:

```bash
cp examples/migrations/gitlab-to-forgejo.cutover.example.json \
  private/migrations/gitlab-to-forgejo.cutover.json
```

Inventory every mapping in the private plan. The important rules are:

- Every section keeps `"unmapped": "fail"`.
- `managed` means the orchestrator owns the destination object.
- `mapped` means a pre-existing destination object must verify.
- `manual` and `skipped` require both `"accepted": true` and a reason.
- `unsupported` always blocks validation.
- Credential values never belong in the plan. Only environment-variable names
  such as `GITLAB_SOURCE_TOKEN` or `WOODPECKER_API_TOKEN` are allowed.
- A managed protected or environment-scoped GitLab variable also needs the
  corresponding acknowledgement fields in its mapping.
- `variables.group_hierarchy` set to `managed` or `mapped` inventories every
  parent group in the repository namespace, in addition to explicit
  `group_ids`. Each discovered group variable still needs its own mapping.
- `variables.instance_scope` set to `managed` or `mapped` inventories GitLab
  instance variables through the administrator-only API. Environments without
  an instance-administrator token must use an explicitly accepted `manual` or
  `skipped` decision with a reason; the orchestrator never silently assumes
  that instance variables are absent.
- Every managed/mapped Woodpecker workflow must contain the configured
  `deployment_gate_marker`.

The Woodpecker deployment steps must enforce that marker. A typical pipeline
condition uses the managed secret and keeps build/test/canary work available in
shadow mode while preventing deployment:

```yaml
steps:
  deploy:
    environment:
      FORGE_CUTOVER_DEPLOYMENT_ENABLED:
        from_secret: FORGE_CUTOVER_DEPLOYMENT_ENABLED
    commands:
      - test "$${FORGE_CUTOVER_DEPLOYMENT_ENABLED}" = "true"
      - ./scripts/deploy.sh
```

Use Woodpecker agent labels for runner capability mappings. The orchestrator
verifies matching schedulable agents; it does not copy GitLab runner machines,
registration tokens, or executors.

Validate the plan without contacting either provider:

```bash
make forge-cutover-validate \
  PLAN=private/migrations/gitlab-to-forgejo.cutover.json \
  PROOF=private/migrations/proof/cutover-plan.json
```

## Discover And Prepare Shadow State

Export the credentials named by the plan. Use narrowly scoped service-account
tokens and a temporary shell or secret injector. GitLab requires project,
group-variable, schedule, runner, protection, hook, mirror, pipeline, archive,
and CI settings access for the selected projects. Selecting managed or mapped
instance-variable inventory additionally requires a GitLab administrator token.
Forgejo requires repository and branch-protection access. Woodpecker, Harbor,
and Argo CD require their API credentials.

Run read-only discovery:

```bash
make forge-cutover-discover \
  PLAN=private/migrations/gitlab-to-forgejo.cutover.json \
  DISCOVERY=private/migrations/proof/discovery.json
```

Inspect every `unaccounted`, `missing`, `stale_mappings`, and `verified` field.
Update the plan and rediscover until the proof is accepted. Variable values are
never written to proof; only keys, scopes, policy flags, mappings, and a
configured/not-configured boolean are recorded.

Approve that exact discovery digest, then prepare shadow state:

```bash
export FORGE_CUTOVER_PREPARE_CONFIRM="$(jq -r .proof_sha256 private/migrations/proof/discovery.json)"

make forge-cutover-prepare \
  PLAN=private/migrations/gitlab-to-forgejo.cutover.json \
  DISCOVERY=private/migrations/proof/discovery.json \
  PREPARED=private/migrations/proof/prepared.json
```

Preparation does not archive GitLab, disable GitLab CI, enable destination
deployments, or enable Woodpecker schedules. It is safe to rerun after changing
the mapped Woodpecker pipeline or destination controls.

## Verify Shadow Operation

```bash
make forge-cutover-verify \
  PLAN=private/migrations/gitlab-to-forgejo.cutover.json \
  PREPARED=private/migrations/proof/prepared.json \
  VERIFICATION=private/migrations/proof/verification.json
```

The canary pipeline must prove build/test behavior without deploying. Configure
the plan's Harbor canary when that pipeline publishes an image, and list every
Argo CD application that must remain `Synced` and `Healthy`. Do not activate
from a failed, modified, stale, or different-plan proof.

## Activate

Activation is the only command that changes source authority. Obtain the change
approval first, then bind the invocation to the exact verification digest:

```bash
export FORGE_CUTOVER_LIVE=1
export FORGE_CUTOVER_CONFIRM="$(jq -r .proof_sha256 private/migrations/proof/verification.json)"
export FORGE_CUTOVER_CHANGE_TICKET=CHG-000000

make forge-cutover-activate \
  PLAN=private/migrations/gitlab-to-forgejo.cutover.json \
  VERIFICATION=private/migrations/proof/verification.json \
  ACTIVATION=private/migrations/proof/activation.json \
  CHECKPOINT=private/migrations/proof/activation-checkpoint.json \
  WORK_DIR=/tmp/platform-forge-cutover
```

By default, an active GitLab pipeline blocks activation. Set
`activation.cancel_active_pipelines` in the private plan only when the approved
change procedure permits the orchestrator to cancel active pipelines. A
successful activation leaves GitLab archived with CI disabled and schedules
paused; Forgejo remains the repository, Woodpecker becomes CI authority, Harbor
holds the verified canary artifact, and Argo CD remains deployment authority.

## Roll Back

Use the successful activation proof for a planned rollback, or the checkpoint
after an interrupted activation. Confirm the exact evidence digest:

```bash
export FORGE_CUTOVER_LIVE=1
export FORGE_CUTOVER_ROLLBACK_CONFIRM="$(jq -r .proof_sha256 private/migrations/proof/activation.json)"
export FORGE_CUTOVER_CHANGE_TICKET=CHG-000000

make forge-cutover-rollback \
  PLAN=private/migrations/gitlab-to-forgejo.cutover.json \
  EVIDENCE=private/migrations/proof/activation.json \
  ROLLBACK=private/migrations/proof/rollback.json
```

For checkpoint recovery, point `EVIDENCE` at
`activation-checkpoint.json` and confirm that file's current digest instead.
Rollback does not erase Forgejo history or Harbor artifacts; it removes
destination deployment authority and restores the recorded GitLab operating
state.

Verify any successful proof remains intact:

```bash
make forge-cutover-proof-verify \
  PROOF=private/migrations/proof/activation.json
```

The digest proves file integrity and command chaining, not operator identity.
Sign or attest final evidence through the organization's normal change-control
system when non-repudiation is required.
