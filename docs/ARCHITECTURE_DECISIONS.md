# Architecture Decision Records

This document defines a public-safe architecture decision record process for
private platform deployments. Keep real approvers, internal topology, customer
impact, ticket links, vendor cases, cost centers, threat details, private
domains, IP addresses, and private evidence in the private deployment
repository or decision system.

Use ADRs with `docs/ARCHITECTURE.md`, `docs/RELEASE_PROMOTION.md`,
`docs/PRODUCTION_READINESS.md`, `docs/THREAT_MODEL.md`,
`docs/SERVICE_CATALOG.md`, `docs/BUSINESS_CONTINUITY.md`, and
`docs/COMPLIANCE_AUDIT.md`.

## Principles

- Important platform choices should be traceable to context, options,
  decision, consequences, owner, and review date.
- ADRs should explain why a decision was made, not only what changed.
- ADRs should be written before implementation when the decision affects
  production risk, data durability, security, recovery, access, or support.
- ADRs should reference private evidence rather than copying sensitive details
  into the public template.
- Superseded decisions should remain readable and point to the replacement.

## When to Write an ADR

Create or update an ADR for changes that affect:

- RKE2 topology, Kubernetes version strategy, node operating system, or support
  tier.
- CNI, kube-proxy, CoreDNS, kube-vip, MetalLB, ingress, or VIP behavior.
- Argo CD source-of-truth, sync policy, bootstrap model, or repository
  ownership.
- Source control, CI, registry, storage, database, backup, observability, PKI,
  or policy-engine selection.
- Data classification, retention, backup, restore, failover, or failback
  behavior.
- Authentication, authorization, break-glass access, robot accounts, or
  external secret systems.
- Production readiness gates, support exceptions, release promotion, rollback,
  or operational ownership.

Small documentation fixes, formatting-only edits, and routine dependency bumps
usually do not need a new ADR unless they change one of the areas above.

## ADR Lifecycle

Use these states:

| State | Meaning |
|---|---|
| Proposed | The decision is under review and should not be treated as accepted. |
| Accepted | The decision is approved and can guide implementation. |
| Superseded | A newer ADR replaces this decision. |
| Deprecated | The decision remains historical but should not guide new work. |

Review accepted ADRs during production readiness, major upgrades, incident
reviews, support lifecycle review, and service ownership review.

## Required ADR Fields

Each private ADR should include:

| Field | Purpose |
|---|---|
| Title | Short decision name |
| Status | Proposed, Accepted, Superseded, or Deprecated |
| Date | Decision date |
| Owner | Accountable private owner or team |
| Review date | When the decision should be revisited |
| Context | Problem, constraints, and private evidence references |
| Decision drivers | Security, reliability, operability, cost, support, compliance, and recovery factors |
| Options considered | At least two realistic choices when possible |
| Decision | The selected option |
| Consequences | Benefits, tradeoffs, risks, and follow-up work |
| Validation | Repository checks, live gates, restore drills, or evidence required |
| Rollback or exit plan | How to reverse, migrate, or supersede the decision |
| Related records | Links to runbooks, issues, releases, incidents, or replacement ADRs |

Use `docs/adr/0000-template.md` as the public-safe starter.

## Decision Review Gates

Before merging a decision that affects production, confirm:

- Required owners reviewed the ADR.
- Security and compliance impacts are documented.
- Backup, restore, continuity, and data classification impacts are documented.
- Service catalog and support lifecycle impacts are documented.
- Release promotion and rollback or roll-forward plan are documented.
- Validation commands and live gates are identified.
- Open exceptions have owner, expiration, and accepting authority.

## Public-Safe Guidance

This repository should contain only the ADR process and a safe template. Private
deployment ADRs can live in a private repository, ticketing system, or
architecture repository. If a decision must be mirrored publicly, replace real
domains, IP addresses, service names, owners, incident references, and evidence
links with safe examples.

Do not commit private ADRs, internal architecture diagrams, vendor tickets,
customer impact statements, exploit details, private hostnames, IP addresses,
user lists, cost records, or approval records to this public template.

## Evidence

Private deployments should keep evidence for:

- Current ADR index and statuses.
- Accepted ADRs that affect production.
- Superseded ADR links and migration status.
- Review dates and owners.
- Validation evidence referenced by the ADR.
- Exceptions and risk acceptance linked to the ADR.
- Release or incident records that triggered the ADR.
