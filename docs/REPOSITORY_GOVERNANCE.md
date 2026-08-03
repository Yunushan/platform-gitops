# Repository Governance Verification

This runbook defines the live GitHub controls required before this public
template publishes a release. These checks are separate from the
provider-neutral private-cluster production gate. A private Forgejo or GitLab
deployment may use equivalent native controls without running this GitHub
target.

## Required GitHub Controls

The default branch must enforce:

- Strict required checks for repository validation and CodeQL.
- Pull requests are required for protected-branch changes, but routine PR
  merging does not require an approving review, CODEOWNER approval, or a
  second approval after the last push.
- An active `.github/CODEOWNERS` file with a catch-all rule and at least two
  distinct owners. CODEOWNERS routes ownership and release review; it does not
  create a mandatory PR approval gate under this policy.
- At least two review-capable collaborators and stale-review dismissal remain
  configured for auditability; the independent `production-release`
  environment reviewer is the mandatory release approval boundary.
- GitHub-verified commits.
- Administrator enforcement, linear history, and resolved conversations.
- No force pushes or branch deletion.

The repository must also have:

- An active tag ruleset covering `refs/tags/v*.*.*`.
- Creation, update, deletion, and non-fast-forward restrictions on those tags.
- A named release-authority bypass actor rather than an unrestricted bypass.
- A `production-release` environment with required reviewers and
  `prevent_self_review` enabled. At least one reviewer must be a review-capable
  non-owner user, or a two-member-or-larger review-capable team, and must not be
  the release-tag bypass actor.
- Administrators disallowed from bypassing the `production-release`
  environment protection rules.
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
metadata, administration settings, Actions permissions and workflow-run review
history, rulesets, branch protection, environments, active CODEOWNERS content,
collaborators, and reviewer team membership. It must not have content, release,
workflow, or administration write permission.

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
machine-readable control has drifted. GitHub's documented environment REST API
does not expose the administrator-bypass toggle, so an operator must also
confirm that setting in the repository UI. The tagged release workflow does not
trust that manual observation as release proof: after the environment gate
opens, it reads the workflow-run approval history and rejects an approval by the
repository owner, release authority, or anyone outside the configured reviewer
boundary. A successful static-governance run writes a sanitized report to:

```text
rendered/governance/github-governance-evidence.json
```

The tagged release workflow runs the static-governance verifier in its
read-only verification job and includes the sanitized report in `SHA256SUMS`.
A separate read-only approval job is attached to the environment gate. After
the gate opens, that job verifies the recorded reviewer, emits
`*.github-release-approval.json`, appends its digest, verifies the complete
manifest, and transfers an approved artifact. Only the downstream publication
job receives write and OIDC permissions. It does not check out source or run
repository scripts; it verifies the approved manifest, signs it with keyless
Cosign, and publishes all reports with the release bundle. The workflow also
retains `*.github-release.json`, which binds the GitHub-verified annotated tag
and signed release commit by SHA-256. `make platform-production-score` requires
all three public reports to match the private live production evidence commit.

## Plan and Configure the Release Boundary

Use a short-lived fine-grained token with repository Administration write
permission from a trusted operator workstation. The configuration token is
separate from the read-only `GOVERNANCE_AUDIT_TOKEN` and must never be stored as
a repository secret.

Start with a non-mutating plan:

```bash
export GITHUB_REPOSITORY=<OWNER>/<REPOSITORY>
export GITHUB_GOVERNANCE_REVIEWER=<INDEPENDENT_COLLABORATOR_LOGIN>
export GITHUB_RELEASE_AUTHORITY=<RELEASE_AUTHORITY_LOGIN>
read -rsp "Temporary GitHub administration token: " GITHUB_TOKEN
echo
export GITHUB_TOKEN
make github-governance-plan
```

For an organization-owned repository, teams are preferred. Use team slugs
instead of the two user variables:

```bash
export GITHUB_GOVERNANCE_REVIEWER_TEAM=<REVIEWER_TEAM_SLUG>
export GITHUB_RELEASE_AUTHORITY_TEAM=<RELEASE_AUTHORITY_TEAM_SLUG>
make github-governance-plan
```

The reviewer team must have `write`, `maintain`, or `admin` access and contain
at least two members. Reviewer and release-authority principals must be
different and their team memberships must not overlap. The release authority
must also have push-capable repository access. User and team forms are mutually
exclusive for each role.

The planner preserves unrelated ruleset rules and existing environment
reviewers. It refuses a full apply when the reviewer is the release authority,
is not review-capable, or is an undersized team.

The managed tag ruleset keeps only explicit always-on user or team bypass
actors. Broad organization-admin, repository-role, integration, or deploy-key
bypasses are removed from that ruleset during a reviewed full apply.

To enable only the scanner controls, with no tag or environment change:

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
unset GITHUB_TOKEN GITHUB_GOVERNANCE_REVIEWER GITHUB_RELEASE_AUTHORITY
unset GITHUB_GOVERNANCE_REVIEWER_TEAM GITHUB_RELEASE_AUTHORITY_TEAM
```

The configurator manages only scanner settings, the semantic-release tag
ruleset, the independent-review environment gate, and its tag-only deployment
policy. It deliberately does not create collaborators, forge a reviewer,
rewrite commits, or weaken existing branch protection.

### Manual Equivalent

In GitHub repository settings:

1. Commit an active `.github/CODEOWNERS` file with independent ownership and
   add at least two review-capable collaborators.
2. Apply the default-branch protection controls listed above.
3. Create an active tag ruleset for `refs/tags/v*.*.*` with the four mutation
   restrictions and an explicit release-authority bypass actor.
4. Create the `production-release` environment.
5. Add an independent reviewer, enable prevention of self-review, and deselect
   **Allow administrators to bypass configured protection rules**.
6. Restrict environment deployments to tags matching `v*.*.*`.
7. Enable every required security-analysis control and private vulnerability
   reporting.
8. Configure CodeQL default setup for Actions and Python with weekly analysis.
9. Add `GOVERNANCE_AUDIT_TOKEN` and rerun the verifier.

Keep reviewer identities, team IDs, token ownership, and internal approval
records outside this public repository. The release artifact exposes only
SHA-256 bindings and pass/fail controls; it does not publish the approver login
or numeric GitHub identity.

GitHub's review-history response is bound to a workflow run but does not expose
the run attempt that received approval. The verifier therefore accepts only
attempt 1. Do not rerun a failed publication job under the same tag; correct the
cause and issue a new reviewed patch release.
