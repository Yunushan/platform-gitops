# Forge Migration Runbook

This project treats migration as a proof-producing operation. A migration is not
accepted because a push command succeeded; it is accepted only when the proof
report shows the selected source and destination surfaces were verified.

## Supported Directions

The current migration helper supports the Git data plane, repository labels,
repository milestones, and portable issues/comments for these directions:

- GitHub to Forgejo
- GitLab to Forgejo
- Forgejo to GitHub
- Forgejo to GitLab

The Git data plane includes branches and tags, with optional wiki and Git LFS
handling. Label migration copies and verifies the provider-common label fields:
name, color, and description. Milestone migration copies and verifies the
provider-common milestone fields: title, description, open/closed state, and
due date. Issue migration copies and verifies the provider-common portable
fields: title, body/description, open/closed state, labels, milestone title, and
comment bodies. Native source authors, timestamps, issue numbers, reactions,
cross-links, and audit history are provider-owned fields and are not rewritten
through normal forge APIs. Provider metadata such as pull requests, merge
requests, releases, packages, branch protection, teams, permissions, and
webhooks is modeled in the migration plan but intentionally fails closed when
marked required. This prevents a partial repository mirror from being reported
as a complete forge migration.

## Plan File

Create a private JSON plan outside public Git, for example
`private/migrations/gitlab-to-forgejo.json`:

```json
{
  "direction": "gitlab-to-forgejo",
  "repositories": [
    {
      "name": "platform-app",
      "source": {
        "url": "https://gitlab.example.com/group/platform-app.git",
        "api_url": "https://gitlab.example.com/api/v4",
        "api_repository": "group/platform-app",
        "token_env": "GITLAB_TOKEN"
      },
      "destination": {
        "url": "https://gitops.example.com/group/platform-app.git",
        "api_url": "https://gitops.example.com/api/v1",
        "api_repository": "group/platform-app",
        "token_env": "FORGEJO_TOKEN"
      },
      "wiki": "auto",
      "lfs": "auto",
      "metadata": {
        "labels": "required",
        "milestones": "required",
        "issues": "required",
        "merge_requests": "skip",
        "releases": "skip"
      }
    }
  ]
}
```

Keep Git credentials in Git credential helpers, CI secrets, or temporary
environment-scoped helpers. Keep API credentials in environment variables named
by `source.token_env` and `destination.token_env`. Do not put tokens in the
plan. If a URL still contains credentials, proof output redacts the user-info
section before writing the report.

`api_url` and `api_repository` are optional when they can be inferred from a
standard public forge URL, but explicit values are recommended for self-hosted
GitHub Enterprise, GitLab, and Forgejo installs. Use `owner/repo` for GitHub and
Forgejo. Use the project path or numeric project ID for GitLab.

## Execute and Prove

Validate the plan:

```bash
python3 scripts/forge_migration.py validate-plan private/migrations/gitlab-to-forgejo.json
```

Run the migration and write proof:

```bash
python3 scripts/forge_migration.py migrate \
  private/migrations/gitlab-to-forgejo.json \
  --work-dir /tmp/platform-forge-migration \
  --proof private/migrations/proof/gitlab-to-forgejo.proof.json
```

Re-verify later without pushing:

```bash
python3 scripts/forge_migration.py verify \
  private/migrations/gitlab-to-forgejo.json \
  --proof private/migrations/proof/gitlab-to-forgejo.verify.json
```

The proof is successful only when all selected repositories report
`"verified": true` and every branch/tag ref matches between source and
destination. When labels, milestones, or issues are enabled, proof also includes
created/updated counts, source/destination metadata digests, missing items,
mismatched items, and extra destination-only items. Issue proof also reports the
number of comments created and per-issue missing/extra comment counts without
printing comment bodies. Extra destination metadata is reported for review but
does not fail verification unless it shadows a source item with different
content or the destination issue is missing source comments.

## Metadata Policy

For a true full-fidelity migration, inventory the non-Git surfaces first:

- Pull requests or merge requests
- Releases and release assets
- Packages and container registry artifacts
- Wikis
- Repository labels
- Repository milestones
- Portable issues and comments
- Webhooks
- Branch protection and rulesets
- Users, teams, permissions, and CODEOWNERS

Set supported surfaces such as `labels`, `milestones`, and `issues` to
`"required"` when they must be migrated and verified. Set unsupported required surfaces to
`"required"` in the plan while designing a provider-specific importer. The
helper will fail and name the missing surface. Set a surface to `"skip"` only
when the migration approval explicitly accepts that loss.

## Acceptance Evidence

For every migration batch, keep these artifacts in the private deployment repo
or evidence store:

- Migration plan JSON
- Migration proof JSON
- Verification proof JSON after a second read-only pass
- Source and destination forge URLs
- Operator, date, and change ticket
- Explicit approval for every metadata surface marked `skip`

Do not commit proof files from real private repositories to this public
template repository.
