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

RKE2 Kubernetes server nodes should be Linux hosts. See [Node OS Support](NODE_OS_SUPPORT.md) for the full support matrix.

Recommended premium node operating systems:

- Rocky Linux 10
- SUSE Linux Enterprise Server
- SLE Micro
- Red Hat Enterprise Linux
- Oracle Linux
- Ubuntu Server LTS

Compatible or lab-profile node operating systems:

- AlmaLinux
- Debian Stable
- CentOS Stream
- Fedora Server
- Arch Linux
- Gentoo Linux
- Linux Mint
- Legacy CentOS Linux

Linux Mint, Arch, Gentoo, Fedora, CentOS Stream, and legacy CentOS are better supported as operator workstations or lab nodes than premium production RKE2 server nodes.

## Git and CI compatibility

Included validation configs:

```text
.github/workflows/validate.yml
.gitlab-ci.yml
.gitea/workflows/validate.yml
.forgejo/workflows/validate.yml
.woodpecker/validate.yml
```
