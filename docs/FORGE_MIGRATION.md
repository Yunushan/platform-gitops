# Forge Migration Runbook

This project treats migration as a proof-producing operation. A migration is not
accepted because a push command succeeded; it is accepted only when the proof
report shows the selected source and destination surfaces were verified.

## Supported Directions

The current migration helper supports the Git data plane for these directions:

- GitHub to Forgejo
- GitLab to Forgejo
- Forgejo to GitHub
- Forgejo to GitLab

The Git data plane includes branches and tags, with optional wiki and Git LFS
handling. Provider metadata such as issues, pull requests, merge requests,
releases, packages, branch protection, teams, permissions, and webhooks is
modeled in the migration plan but intentionally fails closed when marked
required. This prevents a partial repository mirror from being reported as a
complete forge migration.

## Plan File

Create a private JSON plan outside public Git, for example
`private/migrations/gitlab-to-forgejo.json`:

```json
{
  "direction": "gitlab-to-forgejo",
  "repositories": [
    {
      "name": "platform-app",
      "source_url": "https://gitlab.example.com/group/platform-app.git",
      "destination_url": "https://gitops.example.com/group/platform-app.git",
      "wiki": "auto",
      "lfs": "auto",
      "metadata": {
        "issues": "skip",
        "merge_requests": "skip",
        "releases": "skip"
      }
    }
  ]
}
```

Keep credentials in Git credential helpers, CI secrets, or temporary
environment-scoped helpers. Do not put tokens in the plan. If a URL still
contains credentials, proof output redacts the user-info section before writing
the report.

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
destination.

## Metadata Policy

For a true full-fidelity migration, inventory the non-Git surfaces first:

- Issues and comments
- Pull requests or merge requests
- Releases and release assets
- Packages and container registry artifacts
- Wikis
- Webhooks
- Branch protection and rulesets
- Users, teams, permissions, and CODEOWNERS

Set unsupported required surfaces to `"required"` in the plan while designing a
provider-specific importer. The helper will fail and name the missing surface.
Set a surface to `"skip"` only when the migration approval explicitly accepts
that loss.

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
