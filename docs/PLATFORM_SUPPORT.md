# Platform Support

## Admin workstations

Supported for repository management, documentation, kubectl, Helm, SSH, and Argo CD CLI workflows:

- Windows
- Windows Server
- macOS
- Linux
- BSD-family systems
- Solaris-family systems

## Cluster nodes

RKE2 Kubernetes server nodes should be Linux hosts.

## Git and CI compatibility

Included validation configs:

```text
.github/workflows/validate.yml
.gitlab-ci.yml
.gitea/workflows/validate.yml
.forgejo/workflows/validate.yml
.woodpecker/validate.yml
```
