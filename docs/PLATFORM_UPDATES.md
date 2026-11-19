# Optional application updates and Argo CD recovery

Updates are opt-in. Existing public defaults and vendored charts are unchanged.
This workflow edits **application image versions and reviewed image digests**
in Helm values files. Deployment uses the existing GitOps promotion workflow.
Use the existing release-promotion and backup gates before publishing changes
to a private repository watched by Argo CD: a push there can trigger auto-sync.

## Reviewed release choices

The offline catalog is `config/platform-releases.json`. The choices below
reflect its 2026-10-01 review. Named channels require a fresh upstream review
after the catalog deadline of 2026-10-29. Exact versions can be selected
independently.

| Component | Existing default | LTS at review | Stable at review |
| --- | --- | --- | --- |
| Forgejo | 15.0.6 | 15.0.9 | 16.0.5 |
| Woodpecker server and agent | v3.16.0 | Not offered upstream | v3.18.1 |
| Argo CD core components | v3.5.0 | Not offered upstream | v3.5.3 |

`lts-stable` is an alias for Forgejo `lts`, not the newer stable major.
Forgejo 15 LTS is supported until 2027-07-15. Forgejo 16 support ended on
2026-10-29. Gitea supports explicit version and digest selection through
`specific`. Woodpecker uses its upstream stable release series.

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

## Premium three-node staging selection

`config/premium-3node-forgejo-staging.env` selects Forgejo 15.0.12-rootless,
Woodpecker v3.20.1 for the server and agent, and the digest-pinned
CloudNativePG PostgreSQL 18.7 operand. Load it in a private staging checkout
with its site-specific inventory and environment already configured. Export
the selections before a fresh private render, then preview the Forgejo digest
pin with the image-only updater.

```sh
set -a
. ./config/premium-3node-forgejo-staging.env
set +a
make platform-render-private-values
python3 scripts/update_platform_versions.py \
  --forgejo "$FORGEJO_UPDATE_CHANNEL" --forgejo-version "$FORGEJO_IMAGE_TAG" \
  --forgejo-digest "$FORGEJO_REVIEWED_MANIFEST_DIGEST" \
  --woodpecker "$WOODPECKER_UPDATE_CHANNEL" --woodpecker-version "$WOODPECKER_IMAGE_TAG"
```

After reviewing the plan, rerun the updater with `--write` to apply the image
pin locally. Run `make platform-profile-check` against the resulting staging
values. Woodpecker uses exact tags in its Helm values. Verify the registry
manifest digests during image reconciliation.

| Woodpecker v3.20.1 image | Reviewed registry manifest digest |
| --- | --- |
| Server | `sha256:0f31560e29d67ec4a06d6eeee4bac38d2a2697a6a741b8a0db3998180f3a3517` |
| Agent | `sha256:a7d7299a13add0b3936fa0ac9ed23507fe283a7fc7d9596c4289d7e8206541eb` |

## Plan and edit existing private values

Run inside the appropriate checkout. The default paths select the premium
profile for Forgejo, Woodpecker and Argo CD. Gitea defaults to the alternative
chart values. Each component's `--<component>-values` flag can select its
existing private values elsewhere. Omitted components stay untouched.
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

# Gitea needs the reviewed digest for the selected image variant.
python3 scripts/update_platform_versions.py \
  --gitea specific --gitea-version "$GITEA_VERSION" \
  --gitea-digest "$GITEA_DIGEST" --gitea-values "$GITEA_VALUES"
```

After a verified, restorable backup of Forgejo's database **and** repositories,
attachments/LFS and configuration, drain/pause migration writes and CI jobs,
review upstream migration notes, test in staging, and inspect the plan. Then
add `--write` to the chosen command to edit **local values only**. This is not
backup verification or approval to push/deploy. Forgejo's three healthy
Longhorn replicas are not an independent backup. A database schema migration
may prevent rollback by changing an image tag alone.

The updater preserves storage, database, cache, host, SSO and Secret settings,
including their comments. An explicit Forgejo `-rootless` suffix is retained.
Pass `--forgejo-digest` to add a reviewed pin or replace the digest for a
digest-pinned update. Use the manifest digest from the approved registry for
the selected tag and image variant. Gitea requires the digest with its target
version. A digest-pinned forge version change requires a new digest, and the
tag and digest are staged together.
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

## Maintain a single active forge during an upgrade

Confirm the rendered forge Deployment uses one replica and the Recreate
strategy. During backup, hold writes at ingress and pause any reconciliation
that could restart a scaled-down writer. Wait for the old Pod to terminate
before copying the repository volume and capturing database state. Include
the external data stores used by the private deployment in the same backup
window. Verify the backup before upgrading.

Render and review the version and digest change through the same chart.
After promotion, verify the new Pod and expected image, the original claim,
an existing login and Git ref, and read/write behavior for representative
permissions. Recover from a failed, database-changing upgrade by restoring
the pre-upgrade database and files with the old image in a fresh deployment.
Compare the restored ref to the backup-time ref before reopening ingress.
[Kubernetes Deployment strategy](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#strategy)

A disposable K3s v1.35.0 lab ran the vendored Gitea 12.7.0 chart with its
SQLite profile. The version and digest edit advanced Gitea 28.2.0 to 28.3.0
on its original claim and the new Pod accepted an SSH push. Recovery on
28.2.0 used the cold backup and a fresh claim in a new namespace on the other
worker. The original login, SSH host key, repository ID, issue, read-only
collaborator permissions and backup-time Git ref were preserved. The
recovered repository excluded the post-upgrade write and accepted a new SSH
push from the owner. The lab used local volumes and verified cached images.

A disposable K3s v1.35.0 lab started rootless Forgejo 15.0.6 on an empty local
claim with single-instance CloudNativePG and `verify-full` TLS. Forgejo
15.0.11 reused the claim and accepted an SSH push. The lab restored the
pre-upgrade database and files into a fresh CloudNativePG cluster and claim
on the other worker, then started 15.0.6. The existing login, SSH host key,
repository ID, issue, read-only collaborator permissions, and backup-time Git
ref survived recovery. The restore excluded the post-upgrade write and the
owner could push again. The lab used local volumes and verified cached images.

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
