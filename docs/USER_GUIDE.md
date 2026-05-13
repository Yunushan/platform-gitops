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
