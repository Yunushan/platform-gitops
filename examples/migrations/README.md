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
