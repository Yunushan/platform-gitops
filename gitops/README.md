# GitOps

Argo CD reads from this directory to deploy platform components.

Start with:

```text
gitops/bootstrap/root-app.yaml
gitops/clusters/rke2-main/platform-apps.yaml
```

Replace `<THIS_REPO_URL>` only at runtime or through private Argo CD configuration.
