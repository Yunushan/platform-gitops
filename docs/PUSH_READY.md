# Push-Ready Guide

This repository includes CI definitions for common Git hosting systems.
Each validation path runs the project structure check, production contract
check, and secret safety scan. Locally, `make validate` and
`python scripts/run_validation.py` run the same validation suite.

Actions-style workflows pin third-party actions to full commit SHAs, with the
human-readable upstream tag kept as a comment. When updating an action, resolve
the new upstream tag to its commit SHA first, update the comment, then rerun
`make validate`.

## GitHub

```bash
git remote add origin git@github.com:<OWNER>/platform-gitops.git
git push -u origin main
```

CI file:

```text
.github/workflows/validate.yml
```

## GitLab

```bash
git remote add origin git@gitlab.com:<GROUP>/platform-gitops.git
git push -u origin main
```

CI file:

```text
.gitlab-ci.yml
```

## Gitea

```bash
git remote add origin ssh://git@<GITEA_HOST>/<ORG>/platform-gitops.git
git push -u origin main
```

CI file:

```text
.gitea/workflows/validate.yml
```

## Forgejo

```bash
git remote add origin ssh://git@<FORGEJO_HOST>/<ORG>/platform-gitops.git
git push -u origin main
```

CI files:

```text
.forgejo/workflows/validate.yml
.woodpecker/validate.yml
```
