# Ansible

Optional playbooks for node preparation and RKE2 bootstrap. Use `inventory/hosts.local.ini`, never `hosts.example.ini`, for real deployments.

`playbooks/prepare-nodes.yml` includes package preparation for RHEL-compatible, Debian-compatible, SUSE, Arch, and Gentoo families. See `docs/NODE_OS_SUPPORT.md` before using non-enterprise-validated operating systems for production cluster nodes.

Use the easier RKE2 bootstrap flow:

```bash
make rke2-install
```

`playbooks/preflight.yml` checks SSH, passwordless sudo, required VIP/domain inventory variables, and writes the managed `platform-gitops` block into `/etc/hosts` on each cluster node.

The playbooks pre-create `/root/.ansible/tmp` with root-only permissions before normal module execution. This avoids Ansible's `remote_tmp ... did not exist` warning during privileged RKE2 tasks.

`playbooks/prepare-nodes.yml` opens the required RKE2 ports in firewalld when firewalld is active. `make rke2-network-check` verifies that joining nodes can reach the first server on the RKE2 supervisor/API ports after the first server is listening.

`playbooks/rke2-registry-check.yml` verifies node egress to the default RKE2 online image pull path before bootstrap. Run it directly when logs show Docker Hub or TLS handshake timeouts:

```bash
make rke2-registry-check
```

To retest one node after a firewall, proxy, DNS, or NAT change:

```bash
make rke2-registry-check HOST=node-2
```

If you use a private registry mirror or airgap image flow, set `rke2_registry_check_urls` to your mirror endpoints, or disable the check only after the mirror is configured:

```bash
RKE2_REGISTRY_CHECK_ENABLED=false make rke2-install
```

If nodes require a proxy for internet access, set `rke2_http_proxy`, `rke2_https_proxy`, and `rke2_no_proxy` in ignored local inventory, or export `RKE2_HTTP_PROXY`, `RKE2_HTTPS_PROXY`, and `RKE2_NO_PROXY`. The install playbook uses these values for installer downloads, registry checks, package installation, and `/etc/default/rke2-server`.

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

If the API VIP is not online yet, join node-2 and node-3 through node-1 while still keeping the API DNS/VIP in TLS SANs:

```bash
RKE2_JOIN_ENDPOINT=<NODE_1_IP> make rke2-install
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

For interrupted or partially bootstrapped clusters, use the safe automated recovery flow. It reuses the existing first-server token, opens firewalld ports, rewrites consistent configs, restarts node-1 first, verifies supervisor reachability, starts joining servers, and waits for all three nodes to become Ready:

```bash
make rke2-recover
```

Recovery uses shorter operator-grade defaults: 300 seconds for service/API stages and 600 seconds for node readiness. It prints diagnostics on failure instead of waiting silently. Override only for slow lab hardware:

```bash
RKE2_START_TIMEOUT=900 RKE2_NODE_READY_TIMEOUT=1200 make rke2-recover
```

Run verification independently when needed:

```bash
make rke2-verify
```

Collect focused diagnostics for a failed node:

```bash
make rke2-diagnose HOST=node-1
```

If the cluster never reached a healthy state and embedded etcd is stuck, use the guarded destructive reset. This removes failed RKE2 cluster state from all selected nodes but leaves packages installed:

```bash
CONFIRM_RKE2_RESET=YES_I_UNDERSTAND make rke2-reset
make rke2-prepare
RKE2_JOIN_ENDPOINT=<NODE_1_IP> make rke2-install
```

To also force a fresh controller-side token:

```bash
CONFIRM_RKE2_RESET=YES_I_UNDERSTAND RKE2_RESET_CONTROLLER_TOKEN=true make rke2-reset
```

Limit diagnostics or connectivity checks to one node when one host is slow or unreachable:

```bash
make rke2-status HOST=node-1
make rke2-ping HOST=node-1
```
