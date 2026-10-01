# Optional application updates and Argo CD recovery

Updates are opt-in. Existing public defaults and vendored charts are unchanged.
This workflow edits **application image versions**, not Helm chart versions,
and never deploys, syncs, prunes, changes authentication or resets passwords.
Use the existing release-promotion and backup gates before publishing changes
to a private repository watched by Argo CD: a push there can trigger auto-sync.

## Reviewed release choices

The offline catalog is `config/platform-releases.json`, reviewed on
2026-10-01. Named channels require another review before 2026-10-29;
they are not floating container tags or an automatic production updater.

| Component | Existing default | Optional LTS | Optional stable |
| --- | --- | --- | --- |
| Forgejo | 15.0.6 | 15.0.9 | 16.0.5 |
| Woodpecker server and agent | v3.16.0 | Not offered upstream | v3.18.1 |
| Argo CD core components | v3.5.0 | Not offered upstream | v3.5.3 |

`lts-stable` is an alias for Forgejo `lts`, not the newer stable major.
Forgejo 15 LTS is supported until 2027-07-15; Forgejo 16 stable until
2026-10-29. Prefer the LTS patch update for an existing Forgejo 15 deployment.
Woodpecker has stable/next, not an LTS channel. Argo CD's current stable
patch line is 3.5; do not substitute a 3.6 release candidate.

Sources: [Forgejo releases](https://forgejo.org/releases/),
[Forgejo upgrade requirements](https://forgejo.org/docs/latest/admin/upgrade/),
[Woodpecker version policy](https://woodpecker-ci.org/versions),
[Woodpecker v3.18.1](https://github.com/woodpecker-ci/woodpecker/releases/tag/v3.18.1),
[Argo CD releases](https://github.com/argoproj/argo-cd/releases),
[Argo CD upgrading](https://argo-cd.readthedocs.io/en/stable/operator-manual/upgrading/overview/).

Review this catalog against official upstream releases regularly. Record the
new exact versions and review dates in a reviewed PR, run validation and stage
them. Do not just extend the review date. Renovate already proposes dependency
PRs in this project with automerge disabled; it is not a live deployment
updater and does not certify database/chart compatibility or restore readiness.

## Plan and edit existing private values

Run inside the appropriate checkout. The default paths select the premium
profile; `--forgejo-values`, `--woodpecker-values`, and `--argocd-values` can
select existing private values elsewhere. Omitted components stay untouched.
The CLI uses explicit arguments, not ambient version environment variables.

```sh
# Catalog only; no changes.
make platform-update-plan

# Plan only. Safe even in a public template checkout.
python3 scripts/update_platform_versions.py \
  --forgejo lts --woodpecker stable --argocd stable

# Exact releases, also plan-only. Both Woodpecker images use the same version.
python3 scripts/update_platform_versions.py \
  --forgejo specific --forgejo-version 15.0.9 \
  --woodpecker specific --woodpecker-version 3.18.1 \
  --argocd specific --argocd-version 3.5.3
```

After a verified, restorable backup of Forgejo's database **and** repositories,
attachments/LFS and configuration, drain/pause migration writes and CI jobs,
review upstream migration notes, test in staging, and inspect the plan. Then
add `--write` to the chosen command to edit **local values only**. This is not
backup verification or approval to push/deploy. Forgejo's three healthy
Longhorn replicas are not an independent backup. A database schema migration
may prevent rollback by changing an image tag alone.

The updater preserves unrelated values and comments: existing storage sizes,
storage classes, database/Redis settings, hostnames, SSO and secret references
are not re-rendered. An explicit Forgejo `-rootless` suffix is retained.
Argo CD uses `global.image.tag`; Redis and Dex are not upgraded with the core
image. Existing Argo core component overrides and digest pins require separate
review. Major upgrades additionally require `--allow-major-upgrade`; image
selection alone is not evidence that the current chart supports that major.
Upgrade charts separately through the vendored-chart review workflow when
required. Do not modify vendored sources in place.

`--write` refuses unresolved deployment placeholders, downgrades, mutable tags,
prereleases, and ambiguous values. All selected files are validated before
writes; replacements are atomic per file, not a multi-file transaction.
Inspect local changes after an I/O interruption before retrying.

## Keep subsequent private renders consistent

Persist the chosen policy in the **ignored private deployment env file** used
by the renderer. Remove conflicting old `*_IMAGE_TAG` entries when selecting
`lts` or `stable` (example env files explicitly pin Woodpecker 3.16.0).

```sh
FORGEJO_UPDATE_CHANNEL=lts
WOODPECKER_UPDATE_CHANNEL=stable
ARGOCD_UPDATE_CHANNEL=stable
```

Or use `specific` with exact `FORGEJO_IMAGE_TAG`, `WOODPECKER_IMAGE_TAG`, and
`ARGOCD_IMAGE_TAG` values. `pinned` is the default and retains the existing
renderer defaults unless an explicit image tag is set. Exact versions are
syntax-checked, not automatically certified as supported or available.
Persist exact approved tags for repeatable later renders; named channels
intentionally stop after the catalog's review deadline. A focused Forgejo
release-pin refresh no longer downgrades an already newer image.

Do **not** run the full first-deploy renderer solely to upgrade an existing
stateful deployment: without all private settings it can replace unrelated
storage/database choices. Use the image-only updater above instead. Run the
strict profile/schema checks and normal validation before publishing to the
private GitOps source. Do not push private rendered values to public origin.

## Diagnose Argo CD before repairing

On a control-plane node, the new read-only command reads Application objects
using the node-local RKE2 kubeconfig. It prints only status counts and
allowlisted failure categories, not raw messages, URLs, credentials or specs.

```sh
make platform-app-diagnose

python3 scripts/diagnose_argocd_applications.py \
  --application harbor --application loki --application monitoring \
  --application openbao --application keycloak
```

Exit codes: `0` selected apps are Healthy/Synced with no error conditions;
`2` attention is needed; `1` status could not be established. Signals are
candidates, not a complete diagnosis. Use `--expected-revision` with the full
reviewed commit to confirm the cluster actually consumed the intended source.
For an already secured, private JSON capture, `--input private/apps.json`
does not contact the cluster. The report is diagnostic, not production proof.

Health, sync and last-operation are separate:

- **Degraded:** a workload is unhealthy; inspect its pod waiting reasons,
  events, probes, private logs, storage and dependencies.
- **OutOfSync:** live/desired differ. Review the specific diff and ownership
  before syncing; controller-generated fields need precise treatment only
  when proven harmless. Do not ignore all drift.
- **Progressing:** normal during rollout, not normal indefinitely. Check jobs,
  scheduling and readiness. Diagnose failed sync hooks before terminating them.
- **Unknown last-operation:** may mean no recorded operation; it is not
  Unknown application health. An actual Unknown health/sync needs investigation.

The inspected deployment had unresolved placeholder hosts in Harbor, Loki
and monitoring; Harbor and OpenBao also had rejected immutable StatefulSet
changes. Several workloads exceeded rollout deadlines. These are real private
configuration/runtime problems, not proof of an Argo CD version bug.

Recovery order:

1. Inspect the affected application's Conditions and Diff, including resources
   marked for pruning. Keep manifests/logs private; never display Secret values.
2. In the **actual private source watched by Argo CD**, restore the missing
   hostname, object-storage secret references and other private settings. Check
   `make platform-profile-check` there, without `--allow-placeholders`. Existing
   bootstrap `skip-incomplete` is not a fix for already registered broken apps.
3. For immutable StatefulSet/PVC drift, identify the exact field. Preserve the
   live storage identity where appropriate; otherwise plan an app-specific,
   backed-up migration. Do not use Force/Replace or delete PVCs to clear a badge.
4. Diagnose stalled pods and hooks from pod events locally. Confirm database,
   storage, secret references, TLS, network and resource readiness before a
   narrowly scoped repair. OpenBao initialization/unsealing requires its
   existing private ceremony, never fabricated keys or reinitialization.
5. Publish reviewed private fixes and sync only affected resources after diff
   review. Do not blanket-sync or enable pruning: an old database StatefulSet
   or Grafana PVC absent from desired Git can still contain needed data.
6. Verify Healthy/Synced, ingress, PVC capacity, replica health, application
   functionality and the reviewed revision; then run production checks.

Do not mask health, relax admission/network protections, or increase rollout
timeouts just to make the dashboard green. The read-only diagnostic and
version updater deliberately perform none of those actions.
