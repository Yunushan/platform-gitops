# Forge Migration Runbook

This project treats migration as a proof-producing operation. A migration is not
accepted because a push command succeeded; it is accepted only when the proof
report shows the selected source and destination surfaces were verified.

For a months-long GitLab or GitHub coexistence period in which source Git stays
writable but only Forgejo/Woodpecker/Argo CD executes CI/CD, continue with the
[Forge Coexistence Transition Runbook](FORGE_TRANSITION.md). For an immediate
GitLab freeze and handover, use the
[GitLab to Forgejo Cutover Runbook](FORGE_CUTOVER.md). Repository migration,
coexistence transition, and final cutover are separate commands by design.

## Supported Directions

The current migration helper supports the Git data plane, repository labels,
repository milestones, portable releases, portable issues/comments, and open or
closed same-repository pull or merge requests for
these directions:

- GitHub to Forgejo
- GitLab to Forgejo
- Forgejo to GitHub
- Forgejo to GitLab

The Git data plane includes branches, tags, Git notes, and the default branch,
with optional wiki and Git LFS handling. Wiki refs are re-read from both forges.
When Git LFS is selected and `git-lfs` is available, all objects are fetched,
checked, and compared by content-addressed object ID and manifest digest.
`lfs: "required"` fails when `git-lfs` is unavailable; `lfs: "auto"` records an
explicit accepted skip in that case.
Provider-internal refs such as GitHub
`refs/pull/*` are deliberately excluded because they are not portable repository
refs. Label migration copies and verifies the provider-common label fields:
name, color, and description. Milestone migration copies and verifies the
provider-common milestone fields: title, description, open/closed state, and
due date. Release migration copies and verifies tag name, normalized release
name (the tag when no name exists), and description/body. Issue migration copies
and verifies the provider-common portable
fields: title, body/description, open/closed state, labels, milestone title, and
comment bodies. Pull and merge request migration copies and verifies the title,
body/description, open/closed state, same-repository source and target branch,
labels, milestone title, and discussion comments. It deliberately fails closed
for merged requests, fork-originated requests, reviews, inline review comments,
reviewers, approvals, reactions, and provider-owned authors/timestamps because
recreating them would either rewrite destination Git history or claim identity
that the destination forge cannot establish. Native source authors, timestamps,
issue numbers, reactions, cross-links, and audit history are provider-owned
fields and are not rewritten through normal forge APIs. Provider metadata such
as release assets, packages, branch protection, teams, permissions, and webhooks
is modeled in the migration plan but intentionally fails closed when marked
required. Draft/prerelease state and original release timestamps are not portable
across all three providers; they are outside the verified release surface. This
prevents a partial repository mirror from being reported as a complete forge
migration.

## Plan File

Create a private JSON plan outside public Git, for example
`private/migrations/gitlab-to-forgejo.json`:

Ready-to-edit portable examples for all four directions are under
`examples/migrations/`. Copy one into the ignored `private/` tree and inventory
every intentionally skipped surface before approval.

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
        "token_env": "FORGEJO_TOKEN",
        "create": "required",
        "private": true,
        "description": "Platform application"
      },
      "wiki": "auto",
      "lfs": "auto",
      "metadata": {
        "labels": "required",
        "milestones": "required",
        "releases": "required",
        "issues": "required",
        "merge_requests": "required",
        "release_assets": "skip"
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

Set `destination.create` to `"required"` to make migration idempotently create
a missing destination repository through the provider API before pushing Git
refs. Existing repositories are reused and reported as `"existing"` in proof.
Repository creation is disabled by default so an older plan cannot unexpectedly
create remote state. For a GitLab group or subgroup, the tool resolves the
namespace by full path; set `destination.namespace_id` explicitly when the
token cannot list namespaces. Destination repositories default to private.

## Execute and Prove

Validate the plan:

```bash
python3 scripts/forge_migration.py validate-plan private/migrations/gitlab-to-forgejo.json
```

Plan validation is fail closed for credentials. Literal `token`, `password`,
`authorization`, `secret`, and related values are rejected at every nesting
level, as are credentials embedded in HTTP(S) URLs. Plans may contain only
environment-variable references such as `token_env`, `username_env`, and
`password_env`. The same invariant is shared by migration, cutover, transition,
rollback, and failback parsing.

The equivalent Make target is:

```bash
make forge-migration-validate PLAN=private/migrations/gitlab-to-forgejo.json
```

Parser robustness is continuously checked by the bounded Atheris target in
`.clusterfuzzlite/` and by the subprocess branch-coverage gate documented in
`docs/SUPPLY_CHAIN.md`. These source gates complement, but do not replace, the
live four-provider acceptance proof later in this runbook.

Run the migration and write proof:

```bash
python3 scripts/forge_migration.py migrate \
  private/migrations/gitlab-to-forgejo.json \
  --work-dir /tmp/platform-forge-migration \
  --proof private/migrations/proof/gitlab-to-forgejo.proof.json
```

Or run the same operation through Make:

```bash
make forge-migration-run \
  PLAN=private/migrations/gitlab-to-forgejo.json \
  WORK_DIR=/tmp/platform-forge-migration \
  PROOF=private/migrations/proof/gitlab-to-forgejo.proof.json
```

Re-verify later without pushing:

```bash
python3 scripts/forge_migration.py verify \
  private/migrations/gitlab-to-forgejo.json \
  --proof private/migrations/proof/gitlab-to-forgejo.verify.json
```

Check that a stored proof is both successful and unchanged:

```bash
python3 scripts/forge_migration.py verify-proof \
  private/migrations/proof/gitlab-to-forgejo.proof.json
```

Every proof contains a canonical SHA-256 integrity digest. This detects an
accidentally or casually modified artifact; it is not a cryptographic signature
of operator identity. Store proofs in an access-controlled evidence system and
sign or attest them with the organization's normal release process when
non-repudiation is required.

The proof is successful only when all selected repositories report
`"verified": true`, every branch/tag/note ref matches between source and
destination, and the default branch matches. When repository creation is managed, proof also records whether
the destination was created or already existed and confirms it remains API
readable. When labels, milestones, releases, or issues are enabled, proof also includes
created/updated counts, source/destination metadata digests, missing items,
mismatched items, and extra destination-only items. Issue proof also reports the
number of comments created and per-issue missing/extra comment counts without
printing comment bodies. Extra destination metadata is reported for review but
does not fail verification unless it shadows a source item with different
content or the destination issue is missing source comments.

Repositories in one plan are isolated batch items. A failure in one repository
does not prevent the remaining repositories from being attempted; the command
returns nonzero and writes a top-level failed proof containing each individual
outcome. Duplicate names, work-directory-safe names, or destination URLs are
rejected before any migration begins.

## Metadata Policy

For a true full-fidelity migration, inventory the non-Git surfaces first:

- Open or closed same-repository pull requests or merge requests
- Releases
- Release assets
- Packages and container registry artifacts
- Wikis
- Repository labels
- Repository milestones
- Portable issues and comments
- Webhooks
- Branch protection and rulesets
- Users, teams, permissions, and CODEOWNERS

Set supported surfaces such as `labels`, `milestones`, `releases`, `issues`, and the
source-appropriate `pull_requests` or `merge_requests` key to `"required"` when
they must be migrated and verified. Set unsupported required surfaces to
`"required"` in the plan while designing a provider-specific importer. The
helper will fail and name the missing surface. Set a surface to `"skip"` only
when the migration approval explicitly accepts that loss.

## Acceptance Evidence

For every migration batch, keep these artifacts in the private deployment repo
or evidence store:

- Migration plan JSON
- Migration proof JSON
- Successful `verify-proof` result (or an organizational signature/attestation)
- Verification proof JSON after a second read-only pass
- Source and destination forge URLs
- Operator, date, and change ticket
- Explicit approval for every metadata surface marked `skip`

Do not commit proof files from real private repositories to this public
template repository.

## Live Four-Direction Acceptance

The normal self-test uses local Git repositories and provider-shaped API
servers. Before calling the migration capability production-ready, run the
separate live acceptance suite against dedicated disposable private namespaces
on GitHub, GitLab, and Forgejo. It creates one source and one destination
repository for each supported direction, seeds branches, tags, notes, labels,
milestones, a release, a closed issue, and comments, then performs both a
migration and a read-only verification pass.

The runner requires a service-account token with private repository create,
read, write, and delete permission in each configured namespace. It does not
contact a provider during its default dry run.

```bash
export FORGE_MIGRATION_LIVE_GITHUB_NAMESPACE=platform-migration-bot
export FORGE_MIGRATION_LIVE_GITLAB_NAMESPACE=platform-migration-bot
export FORGE_MIGRATION_LIVE_FORGEJO_API_URL=https://forgejo.example.com/api/v1
export FORGE_MIGRATION_LIVE_FORGEJO_NAMESPACE=platform-migration-bot

export GITHUB_TOKEN='...'
export GITLAB_TOKEN='...'
export FORGEJO_TOKEN='...'

make forge-migration-live-plan
```

The dry-run output is redacted and lists the exact four disposable repository
pairs. The acceptance sources include both open and closed same-repository
pull or merge requests with labels, milestones, and discussion comments. To
execute it, use an ignored private evidence directory and explicitly enable
live access:

```bash
FORGE_MIGRATION_LIVE=1 \
make forge-migration-live-run \
  LIVE_DIR=private/migrations/live-acceptance-2026-07-17
```

This leaves the temporary repositories available for a human audit. To remove
only successful repositories after the proof is written, add
`LIVE_CLEANUP=1`. The runner refuses to create, reuse, or delete a repository
whose name does not begin with `platform-migration-live-`; choose a different
prefix only with `--prefix` when running the Python command directly.

### Credentialed CI Evidence

The repository also includes the manual-only GitHub Actions workflow
`live-forge-migration-acceptance`. It has no push or pull-request trigger. Add
the following secrets to the protected
`forge-migration-live-acceptance` GitHub Environment before running it:

- `FORGE_MIGRATION_LIVE_GITHUB_NAMESPACE`
- `FORGE_MIGRATION_LIVE_GITLAB_NAMESPACE`
- `FORGE_MIGRATION_LIVE_FORGEJO_API_URL`
- `FORGE_MIGRATION_LIVE_FORGEJO_NAMESPACE`
- `FORGE_MIGRATION_LIVE_GITHUB_TOKEN`
- `FORGE_MIGRATION_LIVE_GITLAB_TOKEN`
- `FORGE_MIGRATION_LIVE_FORGEJO_TOKEN`

Use service accounts limited to the disposable private namespaces and configure
environment reviewers as the approval gate. The workflow writes the same
redacted proof files to a private 90-day artifact. Its default preserves the
temporary repositories; its `cleanup` input deletes only verified repositories
with the guarded live-acceptance prefix.

The resulting `live-acceptance.proof.json` references a pair of integrity
checked migration and verification proofs for each of GitHub to Forgejo, GitLab
to Forgejo, Forgejo to GitHub, and Forgejo to GitLab. It is proof for the
portable contract described above, not a claim that provider-native objects
explicitly marked `skip` were imported.
