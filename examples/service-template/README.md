# Service Template

Copy this directory to start a new application repository.

Included CI starter files:

- `.github/workflows/ci.yml` for GitHub Actions.
- `.gitea/workflows/ci.yml` for Gitea Actions.
- `.forgejo/workflows/ci.yml` for Forgejo Actions.
- `.gitlab-ci.yml` for GitLab CI.
- `.woodpecker.yml` for Woodpecker CI.

Recommended source repository name:

```text
apps/<service-name>
```

Recommended deployment state:

```text
gitops/apps-dev/apps/<service-name>
gitops/apps-stage/apps/<service-name>
gitops/apps-prod/apps/<service-name>
```
