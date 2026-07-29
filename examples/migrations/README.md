# Forge Migration Plan Examples

These plans exercise the same verified portable contract in four directions:

- GitHub to Forgejo
- GitLab to Forgejo
- Forgejo to GitHub
- Forgejo to GitLab

Copy a plan into the ignored `private/` directory, replace the example URLs,
and export the named token variables. Each plan can contain multiple repository
objects for a batch migration.

The examples require and prove branches, tags, Git notes, the default branch,
labels, milestones, portable releases, and portable issues/comments. Wiki and
Git LFS use explicit `auto` policy. Provider-owned pull/merge request history,
release assets, packages, projects, identities, permissions, protection rules,
and webhooks are explicitly skipped and therefore are not part of a successful
portable proof.

Run a plan with:

```bash
make forge-migration-validate PLAN=private/migrations/plan.json
make forge-migration-run \
  PLAN=private/migrations/plan.json \
  WORK_DIR=/tmp/platform-forge-migration \
  PROOF=private/migrations/proof.json
make forge-migration-proof-verify PROOF=private/migrations/proof.json
```

See `docs/FORGE_MIGRATION.md` for the acceptance and evidence policy.

## Optional CI/CD Coexistence

The transition examples are separate from the portable repository-only plans:

- `gitlab-to-forgejo.transition.example.json`
- `github-to-forgejo.transition.example.json`

They model a shadow period followed by a state in which source Git remains
writable, source CI is disabled, commits continue to relay into Forgejo, and
Woodpecker/Argo CD are the only deployment authority. They are never used by
platform bootstrap. Copy one to `private/`, keep all credentials in the named
environment variables, and follow `docs/FORGE_TRANSITION.md`. The workflow
includes a relay-preserving temporary fallback, a full pre-finalization
rollback, and a verified Forgejo-to-source failback after finalization.
