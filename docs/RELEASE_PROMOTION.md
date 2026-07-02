# Release and Environment Promotion

This runbook defines a public-safe release and environment promotion model for
private platform deployments. Keep real environment names, owners, branch
rules, ticket numbers, release approvals, customer impact notes, and production
change records in the private deployment repository or release system.

Promotion is the process of proving that a change is safe enough to move from
one environment to the next. A successful merge is not enough by itself; each
promotion should leave evidence that the artifact, desired state, runtime
health, rollback plan, and approvals were checked.

## Principles

- Promote the same reviewed artifact or GitOps change forward; do not rebuild
  a different artifact for each environment unless the private release process
  explicitly records why.
- CI builds, tests, scans, signs, and publishes artifacts.
- Argo CD applies desired state from Git.
- Production changes use pull requests, required reviews, validation checks,
  and private release evidence.
- Every production promotion has an owner, reason, rollback plan, and expected
  verification command.
- Emergency hotfixes are reconciled back into the normal promotion path after
  service is stabilized.
- Release decisions should be evidence-based, not based only on a green
  repository validation run.

## Environment Model

The template recommends separate desired-state repositories or directories for:

| Environment | Purpose | Promotion expectation |
|---|---|---|
| Development | Fast feedback, integration, and app-owner validation | May accept frequent changes after CI passes |
| Staging | Production-like validation and release rehearsal | Must use production-like values without real customer data |
| Production | Customer or internal service runtime | Requires approval, health gates, rollback, and evidence |

The public examples use:

```text
gitops/apps-dev
gitops/apps-stage
gitops/apps-prod
```

Private deployments can use different names, branches, or repositories. Keep
the mapping in private documentation and make Argo CD Applications point only
at the intended private source.

## Source and Artifact Flow

Use this default flow:

```text
application repository
  -> pull request review
  -> CI test, scan, sign, and publish
  -> immutable image tag or digest
  -> GitOps pull request updates desired state
  -> Argo CD syncs the target environment
  -> health gate and evidence record
```

The platform repository should not store application source code, live
production secrets, customer data, or private incident evidence. Application
repositories own source code. GitOps repositories own desired state. The
registry owns immutable artifacts and retention policy.

## Promotion Gates

Before promoting to the next environment, collect evidence for:

- Pull request approval and required reviewers.
- Repository validation with `python scripts/run_validation.py`.
- Secret/privacy scan with `make no-secrets`.
- Selected profile validation with
  `PLATFORM_PROFILE=<PROFILE> make platform-profile-check`.
- Artifact provenance, image tag or digest, scan result, and signature or
  attestation state when enabled.
- Argo CD Application sync and health in the source environment.
- Relevant smoke, integration, or user acceptance tests.
- No open critical or expired exceptions from `docs/COMPLIANCE_AUDIT.md`.
- Backup and restore prerequisites for stateful changes.
- Capacity impact review from `docs/CAPACITY_PLANNING.md` and alerting impact
  review for high-load or noisy changes.

Before promoting to production, also run:

```bash
make platform-status
make platform-app-health
PLATFORM_PROFILE=<PROFILE> make platform-production-check
```

Use `docs/PRODUCTION_READINESS.md` for the final go/no-go decision before
declaring the promoted environment production-ready.
Use `docs/BUSINESS_CONTINUITY.md` when a release changes the minimum viable
platform, dependency recovery order, failover/failback behavior, or continuity
exercise expectations.
Use `docs/ARCHITECTURE_DECISIONS.md` when a release changes a significant
platform decision, support posture, recovery model, security boundary, or
component choice.

## Change Windows and Freezes

Use a maintenance window when a promotion touches:

- RKE2, Cilium, kube-proxy, CoreDNS, MetalLB, kube-vip, or ingress.
- Argo CD controller, repo-server, Redis, projects, or repository credentials.
- Forgejo, Woodpecker, Harbor, CloudNativePG, Longhorn, Velero, Loki,
  Prometheus, Grafana, cert-manager, trust-manager, or step-ca.
- Storage classes, PVC expansion, database schema, backup target, registry
  retention, certificate issuer, trust bundle, or object storage settings.

Use a change freeze when:

- An incident is active.
- The error budget is exhausted.
- Restore drill, backup target, or production health gate is failing.
- A critical dependency or supply-chain issue is under review.
- A previous release is still being validated or rolled back.

Record who approved any freeze override and when it expires.

## Rollback and Roll-Forward

Every production promotion should define:

- Previous known-good Git revision.
- Previous known-good image tag or digest.
- Database and storage rollback constraints.
- Whether rollback requires restore, schema downgrade, or forward fix.
- Argo CD sync action or revert commit.
- Health gates that prove recovery.

Prefer a Git revert or follow-up commit so Argo CD can reconcile the platform
back to the intended state. Avoid leaving production dependent on manual
`kubectl patch`, manual Secret edits, or untracked Helm changes.

Rollback is not always safe for databases, storage, or queues. When rollback
would risk data loss, use a roll-forward fix with incident commander or change
owner approval and record the decision in private evidence.

## Hotfix Flow

Use hotfixes only for urgent service, security, or data-protection issues.

Minimum hotfix flow:

- Declare the incident or urgent change owner.
- Freeze unrelated promotions.
- Create the smallest safe Git change.
- Run focused validation and the narrow health gate for the affected layer.
- Promote through staging when time allows.
- Apply to production with explicit approval.
- Run `make platform-status` and the relevant health gate.
- Open the normal follow-up pull request if any manual action was required.
- Record the timeline, risk acceptance, and post-hotfix review.

After the hotfix, restore the normal promotion path and update any runbook,
test, alert, or capacity threshold that failed to catch the issue earlier.

## Versioning and Tags

Use immutable references for release evidence:

- Git commit SHA for desired-state changes.
- Image digest or stable release tag for container artifacts.
- Pinned Helm chart version.
- Pinned CI Action commit SHA for Actions-style workflows.
- Release note or changelog entry for user-visible behavior.

Do not use mutable tags such as `latest`, `next`, `nightly`, `dev`, or branch
names as production release proof.

## Argo CD Promotion Modes

Private deployments can use one of these patterns:

| Pattern | Use when | Evidence |
|---|---|---|
| Directory promotion | Environments live in separate directories in one private GitOps repo | Pull request diff and Argo CD sync per directory |
| Branch promotion | Environments track protected branches | Merge history, branch protection, and Argo CD source revision |
| Repository promotion | Environments live in separate GitOps repos | Cross-repo pull request or mirror evidence |
| ApplicationSet promotion | Many apps share a controlled generator | Generator change review and rendered Application diff |

Whichever pattern is used, production Argo CD Applications should point to the
intended private source.

Do not rely on temporary seed Git or insecure repository URLs for production
promotion.

## Production Evidence

Keep private evidence for:

- Change owner, approver, and release window.
- Source and target environment.
- Git commit SHA, image digest or release tag, and chart version.
- Pull request review and required validation checks.
- Secret scan result.
- Argo CD sync and health result.
- `make platform-status`.
- `make platform-app-health`.
- `PLATFORM_PROFILE=<PROFILE> make platform-production-check`.
- Go/no-go decision from `docs/PRODUCTION_READINESS.md`.
- Rollback or roll-forward plan.
- Open exceptions and risk acceptance from `docs/COMPLIANCE_AUDIT.md`.
- Post-release validation and follow-up actions.

Do not commit private release records, production ticket links, customer impact
notes, internal environment names, user lists, or screenshots to this public
template.
