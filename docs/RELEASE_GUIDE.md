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

## Release checklist

- [ ] No private data.
- [ ] No plaintext secrets.
- [ ] Local files ignored.
- [ ] Docs updated.
- [ ] Profiles updated.
- [ ] Restore process reviewed.
- [ ] Release notes created.
