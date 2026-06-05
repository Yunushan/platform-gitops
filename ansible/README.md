# Ansible

Optional playbooks for node preparation and RKE2 bootstrap. Use `inventory/hosts.local.ini`, never `hosts.example.ini`, for real deployments.

`playbooks/prepare-nodes.yml` includes package preparation for RHEL-compatible, Debian-compatible, SUSE, Arch, and Gentoo families. See `docs/NODE_OS_SUPPORT.md` before using non-enterprise-validated operating systems for production cluster nodes.
