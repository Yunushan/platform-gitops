# Ansible

Optional playbooks for node preparation and RKE2 bootstrap. Use `inventory/hosts.local.ini`, never `hosts.example.ini`, for real deployments.

`playbooks/prepare-nodes.yml` includes package preparation for RHEL-compatible, Debian-compatible, SUSE, Arch, and Gentoo families. See `docs/NODE_OS_SUPPORT.md` before using non-enterprise-validated operating systems for production cluster nodes.

Use the easier RKE2 bootstrap flow:

```bash
make rke2-preflight
make rke2-prepare
make rke2-install
```

`playbooks/preflight.yml` checks SSH, passwordless sudo, required VIP/domain inventory variables, and writes the managed `platform-gitops` block into `/etc/hosts` on each cluster node.

The playbooks pre-create `/root/.ansible/tmp` with root-only permissions before normal module execution. This avoids Ansible's `remote_tmp ... did not exist` warning during privileged RKE2 tasks.

To also update the controller's `/etc/hosts`:

```bash
ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/preflight.yml \
  -e manage_controller_hosts=true
```

Pin an exact RKE2 release when needed:

```bash
RKE2_VERSION='v1.35.4+rke2r1' make rke2-install
```

Or pass it directly to Ansible:

```bash
ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/install-rke2.yml \
  -e rke2_version='v1.35.4+rke2r1'
```

If no token is supplied in `inventory/hosts.local.ini` or the `RKE2_TOKEN` environment variable, the install playbook generates and reuses one at `~/.config/platform-gitops/rke2-token`.

The install playbook runs the package installer asynchronously, polls it, and fails with diagnostics if it exceeds the timeout. Defaults are 1200 seconds for package install and 900 seconds for service startup. Override them only when your network or image pulls are slow:

```bash
RKE2_INSTALL_TIMEOUT=1800 RKE2_START_TIMEOUT=1200 make rke2-install
```

If an installation appears stuck, collect process, service, journal, disk, and memory diagnostics:

```bash
make rke2-status
```

If an Ansible run was interrupted, stale `/tmp/install-rke2.sh` or package-manager processes can keep running on a node. Clean them before starting another install:

```bash
make rke2-cleanup-installers HOST=node-1
```

Limit diagnostics or connectivity checks to one node when one host is slow or unreachable:

```bash
make rke2-status HOST=node-1
make rke2-ping HOST=node-1
```
