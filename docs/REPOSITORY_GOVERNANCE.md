# Repository Governance Verification

This runbook defines the live GitHub controls required before this public
template publishes a release. These checks are separate from the
provider-neutral private-cluster production gate. A private Forgejo or GitLab
deployment may use equivalent native controls without running this GitHub
target.

## Required GitHub Controls

The default branch must enforce:

- Strict required checks for repository validation and CodeQL.
- At least one approving review.
- CODEOWNER review, stale-review dismissal, and approval after the last push.
- GitHub-verified commits.
- Administrator enforcement, linear history, and resolved conversations.
- No force pushes or branch deletion.

The repository must also have:

- An active tag ruleset covering `refs/tags/v*.*.*`.
- Creation, update, deletion, and non-fast-forward restrictions on those tags.
- A named release-authority bypass actor rather than an unrestricted bypass.
- A `production-release` environment with required reviewers and
  `prevent_self_review` enabled.
- A custom environment deployment policy allowing only `v*.*.*` tags.
- Read-only default workflow token permissions and no Actions PR approval.
- Required full-SHA pinning for Actions.
- Dependabot security updates, secret scanning, push protection,
  non-provider patterns, and validity checks enabled.
- Private vulnerability reporting enabled.
- CodeQL default setup configured for Actions and Python with a supported query
  suite, remote threat model, and weekly scheduled analysis.
- A GitHub-verified commit at the current default-branch tip.

GitHub currently limits non-provider patterns and validity checks to
organization-owned repositories on GitHub Team with Secret Protection. The
strict verifier does not call a user-owned repository 100/100 when those
controls are unavailable. Keep the repository's Gitleaks and no-secrets gates
enabled as compensating controls, then transfer the release repository to an
eligible organization before claiming the strict GitHub governance score.

## Audit Credential

Create a repository secret named `GOVERNANCE_AUDIT_TOKEN`. Prefer a GitHub App
installation token or a fine-grained token with read-only access to repository
metadata, administration settings, Actions permissions, rulesets, branch
protection, and environments. It must not have content, release, workflow, or
administration write permission.

Do not store the token in Git, an environment example, workflow output, or a
production evidence file.

## Verify Live Settings

From a trusted operator workstation:

```bash
export GITHUB_REPOSITORY=<OWNER>/<REPOSITORY>
read -rsp "Read-only GitHub governance token: " GITHUB_TOKEN
echo
export GITHUB_TOKEN
make github-governance-verify
unset GITHUB_TOKEN
```

The command fails closed when an API endpoint is inaccessible or any required
control has drifted. A successful run writes a sanitized report to:

```text
rendered/governance/github-governance-evidence.json
```

The tagged release workflow runs the same verifier before receiving write or
OIDC permissions. It includes the sanitized report in `SHA256SUMS`; the release
job verifies those checksums, signs them with keyless Cosign, and publishes the
report with the release bundle. The workflow also retains
`*.github-release.json`, which binds the GitHub-verified annotated tag and
signed release commit by SHA-256. `make platform-production-score` requires
both reports to match the private live production evidence commit.

## Plan and Configure the Release Boundary

Use a short-lived fine-grained token with repository Administration write
permission from a trusted operator workstation. The configuration token is
separate from the read-only `GOVERNANCE_AUDIT_TOKEN` and must never be stored as
a repository secret.

Start with a non-mutating plan:

```bash
export GITHUB_REPOSITORY=<OWNER>/<REPOSITORY>
export GITHUB_GOVERNANCE_REVIEWER=<INDEPENDENT_COLLABORATOR_LOGIN>
read -rsp "Temporary GitHub administration token: " GITHUB_TOKEN
echo
export GITHUB_TOKEN
make github-governance-plan
```

The planner preserves unrelated ruleset rules and existing environment
reviewers. It refuses a full apply when the reviewer is the release authority
or is not a repository collaborator. To enable only the scanner controls, with
no tag or environment change:

```bash
make github-governance-security-apply
```

After reviewing `rendered/governance/github-governance-plan.json`, apply the
release boundary and immediately audit it:

```bash
make github-governance-apply
unset GITHUB_TOKEN

read -rsp "Read-only GitHub governance token: " GITHUB_TOKEN
echo
export GITHUB_TOKEN
make github-governance-verify
unset GITHUB_TOKEN GITHUB_GOVERNANCE_REVIEWER
```

The configurator manages only scanner settings, the semantic-release tag
ruleset, the independent-review environment gate, and its tag-only deployment
policy. It deliberately does not create collaborators, forge a reviewer,
rewrite commits, or weaken existing branch protection.

### Manual Equivalent

In GitHub repository settings:

1. Apply the default-branch protection controls listed above.
2. Create an active tag ruleset for `refs/tags/v*.*.*` with the four mutation
   restrictions and an explicit release-authority bypass actor.
3. Create the `production-release` environment.
4. Add an independent reviewer and enable prevention of self-review.
5. Restrict environment deployments to tags matching `v*.*.*`.
6. Enable every required security-analysis control and private vulnerability
   reporting.
7. Configure CodeQL default setup for Actions and Python with weekly analysis.
8. Add `GOVERNANCE_AUDIT_TOKEN` and rerun the verifier.

Keep reviewer identities, team IDs, token ownership, and internal approval
records outside this public repository.
