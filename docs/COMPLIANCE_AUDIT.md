# Compliance and Audit Evidence

This guide defines a public-safe compliance and audit evidence model for
private platform deployments. It is not a legal compliance statement and does
not certify any framework. Keep actual owners, audit exports, screenshots,
ticket numbers, risk decisions, customer names, internal hostnames, and
regulatory mappings in the private deployment repository or governance system.

The purpose is to make production evidence repeatable. Each private deployment
should be able to show what control was expected, what evidence proves it, who
owns it, and when it was last reviewed.

## Principles

- Evidence must be current, attributable, and repeatable.
- Git is the source of truth for planned platform state.
- Live cluster checks prove runtime state, not policy intent.
- Private records hold real audit evidence; this public template holds only the
  evidence model.
- Exceptions must have an owner, expiration date, risk acceptance, and follow-up
  action.
- Do not collect more sensitive evidence than the review needs.
- Redact logs, screenshots, and exports before sharing outside the approved
  review group.

## Control Domains

Use these domains as a starter map for private compliance reviews:

| Domain | Control intent | Primary evidence |
|---|---|---|
| Source control | Production changes are reviewed, validated, and traceable | Pull requests, required reviews, CODEOWNERS, validation checks |
| Change management | Risky changes have owner, reason, rollback, and maintenance window | Change record, release checklist, Argo CD sync result, `docs/RELEASE_PROMOTION.md` |
| Access control | Users and automation have least-privilege access | Access matrix, RBAC review, robot account review |
| Secrets management | Secrets are not stored in plaintext Git | SOPS or external secret policy, secret scan output, rotation record |
| CI/CD separation | CI builds artifacts and Argo CD deploys desired state | Pipeline logs, image publish record, Argo CD Application history |
| Supply chain | Dependencies, images, charts, and CI references are reviewed and pinned | Renovate dashboard, pinned charts, pinned images, CI SHA pinning |
| Backup and recovery | Recovery is proven, not assumed | Restore drill, RPO/RTO evidence, backup target status |
| Incident response | Incidents have severity, roles, timeline, recovery proof, and review | Incident record, post-incident actions, credential rotation |
| Observability | Production signals, alerts, SLOs, and routing are tested | Alert routing test, SLO review, dashboard and alert review |
| Capacity management | Growth and saturation are measured before failure | Capacity baseline, thresholds, load tests, scale decisions |
| Data classification | Retention, disposal, and handling follow data class | Data map, retention review, disposal evidence |
| Vulnerability management | Security findings are triaged and remediated | Vulnerability report, patch record, exception review |
| Audit logging | Sensitive operations are traceable to an actor and time | Git audit logs, Argo CD history, Kubernetes events, application audit logs |
| Disaster recovery | Off-cluster copies and recovery authority are protected | Backup credential review, restore drill, recovery owner review |
| PKI and trust | Certificate, CA, and trust bundle changes are controlled | Certificate readiness, issuer review, trust-manager bundle review |

## Required Evidence Records

Keep private records for:

- Latest repository validation output from `python scripts/run_validation.py`.
- Latest secret scan output from `make no-secrets` or
  `python scripts/validate_no_secrets.py`.
- Latest `PLATFORM_PROFILE=<PROFILE> make platform-production-check`.
- Latest `make platform-app-health`.
- Latest production readiness go/no-go record from
  `docs/PRODUCTION_READINESS.md`.
- Latest restore drill from `docs/BACKUP_RESTORE.md`.
- Latest continuity exercise from `docs/BUSINESS_CONTINUITY.md`.
- Latest service catalog ownership review from `docs/SERVICE_CATALOG.md`.
- Latest architecture decision review from `docs/ARCHITECTURE_DECISIONS.md`.
- Latest operations review from `docs/OPERATIONS.md`.
- Latest incident response review from `docs/INCIDENT_RESPONSE.md`.
- Latest access review from `docs/ACCESS_CONTROL.md`.
- Latest capacity review from `docs/CAPACITY_PLANNING.md`.
- Latest alert routing and SLO review from `docs/ALERTING.md`.
- Latest data classification and retention review from
  `docs/DATA_CLASSIFICATION.md`.
- Latest threat-model review from `docs/THREAT_MODEL.md`.
- Latest security policy and vulnerability triage review from `SECURITY.md`.
- Latest dependency, image, chart, and CI reference update review.
- Latest release and environment promotion review from
  `docs/RELEASE_PROMOTION.md`.

## Audit Logging Expectations

Private deployments should retain enough audit detail to answer:

- Who changed production desired state.
- Who approved the change.
- Which validation checks passed.
- Which Argo CD Application applied the change.
- Which Kubernetes resources changed.
- Which CI pipeline built and published an artifact.
- Which registry identity pushed or pulled a release artifact.
- Which admin user or robot account changed access, secrets, policies, or
  retention.
- Which backup, restore, or incident action was performed.

Evidence sources can include Git hosting audit logs, pull request history,
Argo CD Application history, Kubernetes events and audit logs when enabled,
Harbor audit logs, Forgejo or GitLab audit events, Woodpecker build history,
Grafana and Alertmanager history, CloudNativePG backup status, Longhorn event
history, Velero records, and private ticketing systems.

## Exceptions and Risk Acceptance

An exception is any approved departure from the intended production baseline,
including a skipped health gate, temporary admin access, broad alert silence,
missing backup target, unpinned dependency, expired restore drill, or incomplete
private value.

Every exception should record:

- Owner.
- Affected component.
- Reason.
- Risk.
- Compensating control.
- Expiration date.
- Review cadence.
- Follow-up action.

Expired exceptions should block production release or require explicit
management approval in the private governance process.

## Review Cadence

Minimum review cadence:

- Per change: validation, pull request evidence, and Argo CD sync evidence.
- Weekly: app health, backup freshness, alert health, and pending exceptions.
- Monthly: access review, robot account review, alert review, capacity review,
  and vulnerability remediation status.
- Quarterly: restore drill, threat model, data classification, retention, and
  disaster recovery ownership.
- Before major release: full production gate, release checklist, dependency
  review, and open exception review.
- After incident: incident review, evidence preservation, credential rotation,
  monitoring gaps, runbook gaps, and preventive actions.

## Control Mapping Template

Use a private table like this for framework-specific mapping:

```text
Control ID:
Control owner:
Platform domain:
Evidence source:
Review cadence:
Last reviewed:
Status:
Exception reference:
Next action:
```

Keep framework names, auditor notes, control IDs, and evidence links in the
private deployment repository or governance system.

## Production Evidence

Before calling a private deployment production-ready, the private evidence
record should prove:

- Repository validation passes.
- Secret scanning passes.
- Production health gates pass.
- Git source of truth is private and production-safe.
- Production readiness is approved through `docs/PRODUCTION_READINESS.md`.
- Access reviews are current.
- Restore drills are current.
- Alert routing and SLO reviews are current.
- Capacity thresholds and load-test evidence are current.
- Data classification and retention decisions are current.
- Open exceptions are accepted, time-boxed, and owned.
- Incident response and break-glass processes have owners.
- Audit evidence can trace production changes from pull request to runtime.

Do not commit private audit exports, screenshots, ticket links, internal
hostnames, user lists, customer names, or framework-specific control mappings
to this public template.
