# Troubleshooting

## Argo CD cannot access this repository

Check that you replaced `<THIS_REPO_URL>` at bootstrap time and configured repository credentials in Argo CD using a private secret flow.

## MetalLB does not assign addresses

Check that the address pool was customized from placeholders to your private network range in ignored or encrypted configuration.

## Ansible or host resolution fails

Run:

```bash
make rke2-preflight
```

This checks SSH, passwordless sudo, required VIP/domain variables, and node `/etc/hosts` entries.

If the WSL/controller machine cannot resolve `api.platform.local` or platform app names, also update the controller:

```bash
ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/preflight.yml \
  -e manage_controller_hosts=true
```

## RKE2 install appears stuck

Use the Ansible install playbook instead of a long ad-hoc shell command:

```bash
make rke2-install
```

The playbook runs the package installer asynchronously, polls progress, starts `rke2-server` without blocking Ansible output, verifies service readiness, and prints diagnostics if install or startup exceeds the timeout.

Collect current process, service, journal, disk, and memory diagnostics:

```bash
make rke2-status
```

If only one host appears stuck, limit the check to that node:

```bash
make rke2-ping HOST=node-1
make rke2-status HOST=node-1
```

If the network or image pulls are slow, extend the timeouts:

```bash
RKE2_INSTALL_TIMEOUT=1800 RKE2_START_TIMEOUT=1200 make rke2-install
```

If logs show image pull failures such as `image ... not found`, pin a known-good RKE2 version:

```bash
RKE2_VERSION='v1.35.4+rke2r1' make rke2-install
```

You can also use:

```bash
ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/install-rke2.yml \
  -e rke2_version='v1.35.4+rke2r1'
```

## CI cannot push images

Check Harbor robot account permissions. Do not commit robot account credentials.

## Secret scanner fails

Replace real values with placeholders or move them to ignored local files or encrypted secret workflows.
