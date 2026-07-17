# Private Deployment Workspace

This directory is intentionally ignored except for this README.

Use it only for local or organization-private deployment material, such as:

```text
Rendered values for a specific organization cluster
Private Argo CD Application manifests
Temporary bootstrap files
Internal DNS/FQDN mappings
SOPS-encrypted secret manifests
```

Do not commit real organization deployment state to the public 0BSD template repo.
For production, store the real GitOps source in a private Git repository and
point Argo CD at that private repository.
