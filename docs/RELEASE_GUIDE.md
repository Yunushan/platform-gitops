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
registration, the `platform-profile-check`, `platform-app-health`, and
`platform-production-check` gates, and CI workflow coverage.

## Release checklist

- [ ] No private data.
- [ ] No plaintext secrets.
- [ ] Local files ignored.
- [ ] Docs updated.
- [ ] Profiles updated.
- [ ] `make validate` passes.
- [ ] `PLATFORM_PROFILE=premium-3node make platform-profile-check` passes for the deployment repo.
- [ ] `make platform-app-health` passes on the target cluster.
- [ ] `make platform-production-check` passes on the target cluster.
- [ ] Restore process reviewed.
- [ ] Release notes created.
