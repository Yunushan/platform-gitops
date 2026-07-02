# Release Guide

## Versioning

Use semantic versions for this platform template:

```text
v0.1.0
v0.2.0
v1.0.0
```

## Before release

```bash
make validate
make no-secrets
```

`make validate` includes the production contract check for platform app
registration, shell syntax, privacy scanning, renderer self-tests, and CI
workflow coverage. It needs Python 3 and Bash. It is a repository-only gate; it
does not contact a live cluster. If `make` is not installed, run
`python scripts/run_validation.py`. Use
`PLATFORM_PROFILE=premium-3node make platform-production-check` for the live
RKE2, Argo CD, storage, ingress, and service-path proof.
Use `docs/PRODUCTION_READINESS.md` as the final release go/no-go checklist for
live gates, private evidence, exceptions, and post-launch validation.
Use `docs/RELEASE_PROMOTION.md` for the environment promotion gates, rollback
or roll-forward plan, hotfix path, freezes, and release evidence expected
before production promotion.

Actions-style CI entries must remain pinned to full commit SHAs rather than
moving tags such as `v4` or `v5`. Keep the upstream version tag as a YAML
comment for readability, but release evidence should show the immutable SHA.

## Release checklist

- [ ] No private data.
- [ ] No plaintext secrets.
- [ ] `SECURITY.md` reporting, secret exposure, and supported-version policy still matches the release.
- [ ] Local files ignored.
- [ ] SOPS/age recipient policy is reviewed for private deployment repositories.
- [ ] Docs updated.
- [ ] Profiles updated.
- [ ] Third-party CI actions are pinned by full commit SHA.
- [ ] `make validate` passes.
- [ ] `make no-secrets` passes.
- [ ] Or `python scripts/run_validation.py` passes when `make` is unavailable.
- [ ] `PLATFORM_PROFILE=premium-3node make platform-profile-check` passes for the deployment repo.
- [ ] `make platform-app-health` passes on the target cluster.
- [ ] `make platform-production-check` passes on the target cluster.
- [ ] Production readiness go/no-go record captured using `docs/PRODUCTION_READINESS.md`.
- [ ] Business continuity and disaster recovery evidence captured using `docs/BUSINESS_CONTINUITY.md`.
- [ ] Platform support tier, lifecycle, and deprecation evidence captured using `docs/PLATFORM_SUPPORT.md`.
- [ ] Restore drill evidence captured using `docs/BACKUP_RESTORE.md`.
- [ ] Operations owner, access review, and maintenance-window evidence captured using `docs/OPERATIONS.md`.
- [ ] Incident response owner, escalation path, and post-incident review process captured using `docs/INCIDENT_RESPONSE.md`.
- [ ] Access-control owner, RBAC review, robot-account review, and break-glass evidence captured using `docs/ACCESS_CONTROL.md`.
- [ ] Capacity baseline, saturation thresholds, load-test notes, and scale-decision evidence captured using `docs/CAPACITY_PLANNING.md`.
- [ ] Compliance and audit evidence, open exceptions, and control review captured using `docs/COMPLIANCE_AUDIT.md`.
- [ ] Release promotion evidence, rollback or roll-forward plan, and hotfix/freeze decisions captured using `docs/RELEASE_PROMOTION.md`.
- [ ] Alert routing, receiver test, and SLO/error budget evidence captured using `docs/ALERTING.md`.
- [ ] Release notes created.
