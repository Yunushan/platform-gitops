# GitLab Workspace Migration

`forge_workspace.py` is the opt-in workspace layer for the GitLab-to-Forgejo
migration. It complements the repository migrator, cutover controller, and
transition controller; it does not replace them.

The command uses a two-phase flow:

1. `export` reads the selected GitLab scope and writes a redacted snapshot.
2. `import` reads that snapshot and mutates only surfaces whose plan mode is
   `managed`.

The snapshot is an inventory and hand-off artifact, not a secret store. Values
for GitLab variables are deliberately re-read from GitLab during import and
written directly to Woodpecker secrets. Variable values, access tokens,
passwords, runner tokens, and webhook secrets are never written to the plan,
snapshot, proof, or normal output.

## Surface modes

Every surface is independently selectable:

| Mode | Export | Import |
| --- | --- | --- |
| `skip` | No inventory | No action |
| `export` | Redacted inventory | No mutation |
| `managed` | Inventory | Apply and verify |
| `mapped` | Inventory | No mutation; operator mapping/proof only |
| `manual` | Inventory | No mutation; requires `accepted: true` and a reason |

The supported surfaces are `users`, `groups`, `subgroups`, `memberships`,
`projects`, `repositories`, `permissions`, `rules`, `runners`, `variables`,
`ci`, and `pipelines`.

The source must explicitly select projects, groups, or users. Users require
either `source.usernames` or `surfaces.users.all_available=true`; projects
require `source.project_paths`, `source.group_paths`,
`source.all_available_projects=true`, or `source.all_available_groups=true`;
groups and memberships require `source.group_paths` or
`source.all_available_groups=true`. These explicit selectors prevent a typo
from becoming an instance-wide import.

## What is imported

- **Users:** selected GitLab users are created through Forgejo's administrative
  user API. The plan references one or more password environment variables;
  passwords are never put in JSON. New users are marked to change the password.
  Every managed user is read back by its mapped login before the import can
  report success.
- **Groups and subgroups:** GitLab groups are represented as Forgejo
  organizations. Nested groups are walked recursively and flattened into
  deterministic organization names; use `mappings` when a different name is
  required. The snapshot retains both direct and effective GitLab membership
  views. Direct memberships become organization-team memberships by default;
  inherited memberships are not incorrectly copied into every child
  organization.
- **Memberships:** The managed surface reconciles GitLab direct group
  memberships into deterministic Forgejo teams. The default mapping is
  Owner -> Forgejo's built-in `Owners` team, Maintainer/Developer -> `write`,
  Reporter/Planner/Security Manager -> `read`, Guest -> `read`, and
  No/Minimal Access -> no team. GitLab Owner mappings must use
  `{ "permission": "owner", "team": "Owners" }`; the importer verifies that
  Forgejo's special organization team exists and refuses to create a normal
  team as an ownership substitute. The `Owners` team is organization-wide and
  is not attached as an ordinary repository team; project-level Owner access
  is represented by a verified Forgejo repository `admin` collaborator grant.
  `role_mappings` can map numeric access levels, role names, or custom roles to
  `{ "permission": "read|write|admin|owner|none", "team": "..." }`. Custom
  GitLab roles must have an explicit mapping; the default is fail-closed.
  Expired memberships are skipped and pending memberships are skipped unless
  the plan explicitly chooses a different policy. Role downgrades remove stale
  managed team memberships and every membership is read back.
- **Projects:** project metadata that Forgejo can represent is reconciled on
  the destination repository. The source project is not deleted or disabled.
- **Repositories:** Git refs, tags, LFS data when selected, and the supported
  repository metadata are delegated to `forge_migration.py`.
- **Permissions:** The managed surface inventories GitLab direct and effective
  project members, including inherited access and available invited-group
  metadata. Invited groups are expanded to their current member set and each
  member is capped at the access level granted by the project invitation before
  the role mapping is applied. It reconciles Forgejo repository collaborators with verified
  `read`, `write`, or `admin` permissions and attaches the corresponding
  organization teams to group-owned repositories when the destination mapping
  permits it. Duplicate grants use the strongest effective permission.
  `group_strategy` may be `users`, `teams`, or `both`; `both` is the complete
  default because collaborators also preserve access inherited through a
  parent group or shared group that Forgejo cannot model natively.
  `teams` is intentionally fail-closed when a direct, parent-group, or
  invited-group user cannot be proven to be covered by the destination team.
  In the normal complete migration, invited-group access is materialized as
  verified user collaborators and recorded in the import proof.

  Permission reconciliation is additive by default. `reconcile: exact` is
  destructive within the explicitly selected GitLab identities and managed
  teams, and therefore requires `accepted: true` plus a non-empty `reason`.
  Unmanaged destination collaborators are never removed. Forgejo has coarser
  repository permissions than GitLab, so issue-only distinctions such as
  Planner, Security Manager, and some custom-role capabilities are represented
  by the configured coarse permission or stop with an unmapped-role error;
  they are never silently over-granted.
- **Rules:** GitLab protected-branch rules are inventoried and applied after
  destination repositories and access teams exist. The portable subset maps
  push and merge access levels to Forgejo branch protections and preserves
  administrator enforcement. GitLab rules using identity-specific access,
  code-owner approval, force pushes, or non-Maintainer unprotect access fail
  closed because Forgejo cannot represent them with equivalent guarantees.
  `reconcile: additive` preserves unrelated destination rules; the explicitly
  destructive `reconcile: exact` mode requires `accepted: true` and a reason
  and removes destination-only rules for the selected repositories.
- **Variables:** project, group, and optional instance variables are read from
  GitLab at import time and stored as Woodpecker repository secrets. By default,
  project names are preserved; group and instance names receive `GL_GROUP_` or
  `GL_INSTANCE_` prefixes unless a mapping supplies a target name. The importer
  fails before writing secrets when two environment-scoped source variables
  resolve to the same case-insensitive Woodpecker secret name. Use a full
  identity mapping such as `project:DEPLOY_TOKEN:production` with a unique
  `target_name`, or mark the mapping `manual`, `mapped`, or `skip` when the
  source scope cannot be represented safely.
- **CI:** selected `.gitlab-ci.yml` or `.gitlab/ci/*` files are converted by the
  fail-closed pipeline converter and committed to the destination repository as
  `.woodpecker.yml` (or an explicit destination path). Unsupported constructs
  stop the import rather than producing a misleading workflow. Destination
  paths are confined to the repository checkout, unchanged retries do not make
  duplicate commits, and each pushed workflow is read back from the remote and
  verified by SHA-256 digest.
- **Runners:** GitLab runners are inventory-only unless a `managed` surface is
  selected. Managed runner import verifies that an already-running Woodpecker
  agent matches each declared label mapping. GitLab runner machines,
  registration tokens, executors, and host credentials are never copied.
- **Pipelines:** pipeline runs and history are export-only. Managed pipeline
  import recreates GitLab pipeline schedules as **disabled** Woodpecker cron
  jobs. Workspace import rejects schedule activation; only the approved
  cutover controller may enable them after source-CI freeze and verification.
  GitLab trigger tokens and historical run state require an explicit manual
  design.
- **Argo CD:** Argo CD is not a GitLab CI pipeline importer. It remains the
  deployment authority and is checked by the cutover controller; this command
  does not invent Argo `Application` manifests from arbitrary CI jobs.

These boundaries reflect the different provider models: GitLab exposes users,
groups, projects, runners, variables, schedules, and pipelines through separate
APIs, while Forgejo uses organizations, teams, repositories, and administrator
operations. See the [GitLab Groups API](https://docs.gitlab.com/api/groups/),
[GitLab Users API](https://docs.gitlab.com/api/users/),
[GitLab Runners API](https://docs.gitlab.com/api/runners/), and
[Forgejo token scopes](https://forgejo.org/docs/latest/user/authentication/token-scope/).

## Commands

Copy the example into an ignored private directory and edit selectors and
modes. Do not put tokens or passwords in the file.

```bash
make forge-workspace-validate \
  PLAN=private/migrations/gitlab-to-forgejo.workspace.json

make forge-workspace-export \
  PLAN=private/migrations/gitlab-to-forgejo.workspace.json \
  SNAPSHOT=private/migrations/proof/workspace-snapshot.json \
  PROOF=private/migrations/proof/workspace-export.json

export GITLAB_MIGRATION_TOKEN='...'
export FORGEJO_ADMIN_TOKEN='...'
export FORGEJO_IMPORTED_USER_PASSWORD='...'
export WOODPECKER_ADMIN_TOKEN='...'

make forge-workspace-import \
  PLAN=private/migrations/gitlab-to-forgejo.workspace.json \
  SNAPSHOT=private/migrations/proof/workspace-snapshot.json \
  WORK_DIR=private/migrations/workspace \
  PROOF=private/migrations/proof/workspace-import.json
```

For a complete users/groups/permissions/rules transfer, enable the
`memberships`, `permissions`, and `rules` surfaces in the plan and set
`surfaces.users.include_members=true` so users referenced only through
inherited project or group access are also created or checked. Configure
`surfaces.rules.gitlab_maintainer_team` when a GitLab protected branch grants access to
Maintainers; that Forgejo team must exist in the mapped repository
organization. Review the redacted snapshot before import. GitLab
passwords, personal access tokens, runner registration tokens, 2FA state, SSO
bindings, and webhook secrets are not transferable and must be provisioned
through their destination-specific controls.

Use an SSH `destination.git_url_template` or a preconfigured Git credential
helper for the CI conversion commit. The API token alone is not silently
embedded into Git remotes.

## Cutover and fallback

Workspace import does not pause GitLab CI and does not make Forgejo the source
of truth. Run the existing `forge-cutover` or `forge-transition` workflow after
the workspace import:

1. Export and review the workspace snapshot.
2. Import users, organizations, and direct memberships so destination teams
   exist.
3. Import repositories and verify counts and refs.
4. Import project permissions and protected-branch rules.
5. Import variables and convert CI in a shadow destination.
6. Run `forge-cutover-verify` or `forge-transition-verify-shadow`.
7. Activate Forgejo/Woodpecker only through the cutover controller, which owns
   the source-CI freeze, checkpoint, rollback, and failback evidence.

If any workspace surface fails, the command stops and writes no claim of
success. Existing GitLab data remains untouched, so rollback is an operational
reversal through the cutover/transition controller rather than destructive
deletion. Keep the source GitLab projects and pipelines enabled until the
destination verification and recovery drill pass.

The complete selectable example is
`examples/migrations/gitlab-to-forgejo.workspace.example.json`.

For an instance-wide, token-visible users/groups/projects authorization pass,
start from
`examples/migrations/gitlab-to-forgejo-all-users-access.example.json`. It sets
`source.all_available_groups=true`, `source.all_available_projects=true`, and
`surfaces.users.all_available=true`; GitLab's API only exposes objects visible
to the migration token. The example deliberately keeps exact reconciliation
off and leaves bots included so the export is truly broad; review the redacted
snapshot and change `skip_bots` only when that is your intended account policy.
The GitLab token must be permitted to enumerate users, groups, group members,
project members, invited groups, and protected branches. A Forgejo
administrator token is required for user creation and organization/team
reconciliation.

Before destination changes begin, import validates that every selected managed
surface is present in the snapshot and that users, groups, projects, and
permissions are non-empty. A truncated or hand-edited export therefore stops
before creating partial users, teams, or repository grants.
