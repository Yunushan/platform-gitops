# Node OS Support

This project supports a broad Linux estate, but node operating systems are tiered because RKE2, Longhorn, and enterprise support contracts are tiered upstream.

## Support meaning

| Level | Meaning |
|---|---|
| Enterprise validated | Suitable for premium production profiles when paired with the matching RKE2 and Longhorn versions. |
| Compatible / best effort | Project includes package guidance and preparation hooks, but upstream vendor validation or enterprise support may be limited. |
| Workstation only | Supported for Git, SSH, kubectl, Helm, docs, and operations work, not recommended as RKE2 server nodes. |

## Recommended cluster node operating systems

| OS family | Project status | Production note |
|---|---|---|
| SUSE Linux Enterprise Server | Enterprise validated | Best alignment with SUSE Rancher/RKE2. |
| SLE Micro | Enterprise validated | Good immutable/appliance-style node OS. |
| Red Hat Enterprise Linux | Enterprise validated | Best choice for Red Hat-standard companies. |
| Rocky Linux 10 | Enterprise validated when matching RHEL 10-tested releases | Best zero-subscription RHEL-style option; install `kernel-modules-extra` for RKE2 prerequisites. |
| Oracle Linux | Enterprise validated when using RHEL-compatible kernel behavior | Prefer RHCK-style compatibility for Kubernetes nodes. |
| Ubuntu Server LTS | Enterprise validated | Best practical/common Linux choice. |
| AlmaLinux | Compatible / best effort | RHEL-compatible, but verify against the current RKE2 support matrix before production. |
| Debian Stable | Compatible / best effort | Usually workable for RKE2, but not the preferred premium profile target. |
| CentOS Stream | Compatible / lab | Upstream of RHEL; use for validation/lab, not premium production. |
| Fedora Server | Compatible / lab | Fast-moving lifecycle; use for lab only. |
| Arch Linux | Compatible / lab | Rolling release; not recommended for production RKE2 servers. |
| Gentoo Linux | Compatible / lab | Requires systemd and careful kernel/package ownership. |
| Linux Mint | Workstation only / lab | Good operator desktop, not recommended for RKE2 server nodes. |
| CentOS Linux | Legacy only | Do not use for new premium clusters. Prefer RHEL, Rocky, Alma, Oracle, SLES, or Ubuntu LTS. |

## Operator workstation support

All requested operating systems are supported as operator/admin workstations when they can run Git, SSH, kubectl, Helm, and basic shell tooling:

- Ubuntu
- RHEL
- Rocky Linux
- AlmaLinux
- Oracle Linux
- Debian
- SUSE/openSUSE
- Arch Linux
- Gentoo Linux
- Linux Mint
- CentOS and CentOS Stream
- Fedora

## Required node capabilities

Every RKE2 server node should provide:

- `systemd`
- `curl`
- `tar`
- `iptables` or nftables compatibility
- `open-iscsi` or distribution equivalent
- NFS client utilities
- `chrony` or equivalent time sync
- `cryptsetup` when Longhorn encrypted volumes are used
- Dedicated SSD/NVMe for OS, RKE2, and etcd
- Dedicated SSD/NVMe for Longhorn data
- Swap disabled

## Package names by family

| Family | Packages |
|---|---|
| RHEL, Rocky, Alma, Oracle, CentOS, Fedora | `curl`, `tar`, `iptables`, `iscsi-initiator-utils`, `nfs-utils`, `chrony`, `cryptsetup` |
| RHEL 10 derivatives | Also install `kernel-modules-extra` for `nf_conntrack` support. |
| Ubuntu, Debian, Linux Mint | `curl`, `tar`, `iptables`, `open-iscsi`, `nfs-common`, `chrony`, `cryptsetup` |
| SUSE/openSUSE | `curl`, `tar`, `iptables`, `open-iscsi`, `nfs-client`, `chrony`, `cryptsetup` |
| Arch Linux | `curl`, `tar`, `iptables-nft`, `open-iscsi`, `nfs-utils`, `chrony`, `cryptsetup` |
| Gentoo Linux | `net-misc/curl`, `app-arch/tar`, `net-firewall/iptables`, `sys-block/open-iscsi`, `net-fs/nfs-utils`, `net-misc/chrony`, `sys-fs/cryptsetup` |

## Premium recommendation

For the premium 3-node profile, prefer one of:

1. Rocky Linux 10 or the currently validated Rocky/RHEL-compatible release.
2. SUSE Linux Enterprise Server 15 SP7.
3. RHEL 10 or the currently validated RHEL release.
4. Ubuntu Server 24.04 LTS.

Use the same distribution, major version, minor version, kernel track, and patch level across all three nodes.

Run `make rke2-install` from the controller to apply the project preparation automatically before installation. The preparation disables swap, persists and loads required kernel modules, applies Kubernetes/CNI sysctls, opens RKE2 and Cilium overlay firewalld ports, trusts the RKE2 pod CIDR and Cilium interfaces in firewalld, and configures NetworkManager to ignore Kubernetes CNI interfaces.

## Validation sources

- RKE2 requirements: https://docs.rke2.io/install/requirements
- RKE2 support matrix: https://www.suse.com/suse-rke2/support-matrix/all-supported-versions/rke2-v1-36/
- Longhorn installation requirements: https://longhorn.io/docs/latest/deploy/install/
- Longhorn best practices: https://longhorn.io/docs/latest/best-practices/
