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

The supported surfaces are `users`, `groups`, `subgroups`, `projects`,
`repositories`, `runners`, `variables`, `ci`, and `pipelines`.

The source must explicitly select projects, groups, or users. Users require
either `source.usernames` or `surfaces.users.all_available=true`; projects
require `source.project_paths`, `source.group_paths`, or the explicit
`source.all_available_projects=true`. This prevents a typo from becoming an
instance-wide import.

## What is imported

- **Users:** selected GitLab users are created through Forgejo's administrative
  user API. The plan references one or more password environment variables;
  passwords are never put in JSON. New users are marked to change the password.
- **Groups and subgroups:** GitLab groups are represented as Forgejo
  organizations. GitLab access levels are represented by Forgejo teams. Nested
  groups are flattened into deterministic organization names; use `mappings`
  when a different name is required. Set `members_mode` to `skip`, `mapped`, or
  `manual` when users are intentionally not being imported.
- **Projects:** project metadata that Forgejo can represent is reconciled on
  the destination repository. The source project is not deleted or disabled.
- **Repositories:** Git refs, tags, LFS data when selected, and the supported
  repository metadata are delegated to `forge_migration.py`.
- **Variables:** project, group, and optional instance variables are read from
  GitLab at import time and stored as Woodpecker repository secrets. By default,
  project names are preserved; group and instance names receive `GL_GROUP_` or
  `GL_INSTANCE_` prefixes unless a mapping supplies a target name.
- **CI:** selected `.gitlab-ci.yml` or `.gitlab/ci/*` files are converted by the
  fail-closed pipeline converter and committed to the destination repository as
  `.woodpecker.yml` (or an explicit destination path). Unsupported constructs
  stop the import rather than producing a misleading workflow.
- **Runners:** GitLab runners are inventory-only unless a `managed` surface is
  selected. Managed runner import verifies that an already-running Woodpecker
  agent matches each declared label mapping. GitLab runner machines,
  registration tokens, executors, and host credentials are never copied.
- **Pipelines:** pipeline runs and history are export-only. Managed pipeline
  import recreates GitLab pipeline schedules as Woodpecker cron jobs. GitLab
  trigger tokens and historical run state require an explicit manual design.

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

Use an SSH `destination.git_url_template` or a preconfigured Git credential
helper for the CI conversion commit. The API token alone is not silently
embedded into Git remotes.

## Cutover and fallback

Workspace import does not pause GitLab CI and does not make Forgejo the source
of truth. Run the existing `forge-cutover` or `forge-transition` workflow after
the workspace import:

1. Export and review the workspace snapshot.
2. Import users/groups/projects/repositories and verify counts and refs.
3. Import variables and convert CI in a shadow destination.
4. Run `forge-cutover-verify` or `forge-transition-verify-shadow`.
5. Activate Forgejo/Woodpecker only through the cutover controller, which owns
   the source-CI freeze, checkpoint, rollback, and failback evidence.

If any workspace surface fails, the command stops and writes no claim of
success. Existing GitLab data remains untouched, so rollback is an operational
reversal through the cutover/transition controller rather than destructive
deletion. Keep the source GitLab projects and pipelines enabled until the
destination verification and recovery drill pass.

The complete selectable example is
`examples/migrations/gitlab-to-forgejo.workspace.example.json`.
