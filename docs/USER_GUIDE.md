# User Guide

## Day-to-day flow

```text
Developer pushes code
  -> Forgejo/Gitea/GitLab receives commit
  -> Woodpecker/GitLab Runner/Gitea Actions runs CI
  -> Image is built and pushed to Harbor
  -> GitOps repo is updated
  -> Argo CD syncs desired state to Kubernetes
```

## Application repositories

Use:

```text
apps/<service-name>
```

Do not put production deployment state in application repositories. Put desired state in GitOps repositories.

## GitOps repositories

Recommended:

```text
gitops/apps-dev
gitops/apps-stage
gitops/apps-prod
```

Production updates should use pull requests and approval.
Use `docs/RELEASE_PROMOTION.md` for the detailed promotion flow between
development, staging, and production GitOps state.

## Day-2 operations

Use `docs/OPERATIONS.md` for the production operating model: ownership,
routine checks, pull-request change management, maintenance windows, upgrades,
break-glass access, incident response, drift management, credential rotation,
capacity tracking, and production evidence.
Use `docs/INCIDENT_RESPONSE.md` for incident severity declaration, roles,
communications, recovery validation, and post-incident review.
Use `docs/ACCESS_CONTROL.md` for RBAC, admin roles, robot accounts,
break-glass access, and access-review evidence.
Use `docs/CAPACITY_PLANNING.md` for capacity domains, saturation signals, load
tests, scaling decisions, and private evidence before growth becomes an
incident.
Use `docs/COMPLIANCE_AUDIT.md` for control mapping, audit evidence, exception
tracking, and private review cadence.
Use `docs/RELEASE_PROMOTION.md` for environment promotion gates, rollback,
hotfix, freeze, and release evidence.
