# SourceHut accounts and Git hosting

The renderer packages `meta.sr.ht` and `git.sr.ht` for Docker Compose, native
Alpine Linux, RKE2, K3s and existing Kubernetes. It uses one active Git writer
and a single repository volume. SourceHut publishes the services as alpha
software. The image recipe uses Alpine 3.22 AMD64 with signed SourceHut
packages for `meta.sr.ht` 0.90.2 and `git.sr.ht` 0.102.13. [SourceHut installation](https://man.sr.ht/installation.md) · [Official package repository](https://man.sr.ht/packages.md) · [Alpha status](https://sourcehut.org/alpha-details/)

## Prepare and render

Build the image from `scripts/sourcehut.Containerfile` and publish it to the
approved registry. Use the published digest in the Docker or Kubernetes
plan. The image recipe pins the Alpine base and the SourceHut service
packages. Alpine dependency revisions resolve from the signed repositories
at build time. Archive the built image and its package inventory for recovery.
On native Alpine 3.22, install the same packages plus the corresponding
OpenRC subpackages, nginx, fcgiwrap, and `git-daemon`, which contains Alpine's
`git-http-backend`. [Alpine package files](https://pkgs.alpinelinux.org/contents?branch=v3.22&name=git-daemon&arch=x86_64)

Prepare two PostgreSQL databases with separate service identities, a private
Redis-compatible endpoint, your SMTP settings, and the OpenPGP mail-signing
key pair required by the API processes. Merge the official
`config.example.ini` files into the private `/etc/sr.ht/config.ini`. Set
distinct TLS origins for accounts and Git and the correct shared cookie
domain. Configure `api-origin` as `http://127.0.0.1:5100` for accounts and
`http://127.0.0.1:5101` for Git. Restrict `internal-ipnet` to loopback for
services in this bundle. Keep the service, network and webhook signing keys
in that private file. Place the OpenPGP files alongside it and use those
absolute paths in `[mail]`. Configure `repos=/var/lib/git/` and set
`post-update-script=/usr/bin/git.sr.ht-update-hook` for the packaged image.
[Account configuration](https://git.sr.ht/~sircmpwn/meta.sr.ht/blob/master/config.example.ini) · [Git configuration](https://git.sr.ht/~sircmpwn/git.sr.ht/blob/master/config.example.ini)

Initialize each empty database by running `sourcehut-migrate meta.sr.ht init`
and `sourcehut-migrate git.sr.ht init` with the prepared config as the relevant
service account. Create the first account with `meta.sr.ht-manageuser`. Plan
the upstream maintenance tasks `meta.sr.ht-daily` and `git.sr.ht-periodic`
with the same config and data access as their services. [Accounts](https://man.sr.ht/meta.sr.ht/installation.md) · [Git hosting](https://man.sr.ht/git.sr.ht/installation.md)

Copy the [Docker](../examples/sourcehut-docker.json),
[native](../examples/sourcehut-native.json), or
[Kubernetes](../examples/sourcehut-kubernetes.json) example to `private/`.
Use the DNS names from the private configuration.

```sh
python3 scripts/render_sourcehut.py --runtime k3s \
  --plan private/sourcehut-kubernetes.json --output-dir private/sourcehut-k3s
```

- **Kubernetes.** Create the named namespace, PVC, and Secret with
  `config.ini` and the mail-signing files. Make the PVC writable by UID/GID 101. Apply the bundle and
  route both hostnames through the TLS gateway to the web Service. Supply
  the matching ingress allow policy from that gateway. The remaining
  listeners use Pod loopback.
- **Docker.** Prepare the state directory for UID/GID 101 and the private
  config directory and files as root-owned and readable by GID 101. Start the Compose
  bundle from its output directory. Make the generated `nginx.conf` readable
  by UID 103 before starting the proxy. The web proxy binds on host loopback.
  Forward both TLS hostnames to that proxy and retain the original Host.
  Recreate the services together when replacing the shared network namespace.
- **Native Alpine.** Install `nginx.conf` for a dedicated nginx process and
  install the generated API overrides in `/etc/conf.d/` under the matching
  OpenRC service names, without the `.conf` suffix. Install the generated
  `sourcehut-fcgiwrap` under `/etc/init.d/` with executable permissions. Start
  the account web, API and webhook services, Git web and API, and fcgiwrap.
  Run nginx as its unprivileged service account on the generated loopback
  listener and publish it through TLS.

The internal nginx proxy blocks the `Internal` auth scheme at public GraphQL
routes. The Git FastCGI handler receives a read-only repository mount in the
container packages. HTTPS serves public Git reads. Private Git reads and
writes use the optional SSH listener.

## Enable Git over SSH

Generate or supply a dedicated Ed25519 host key and preserve it with your
backups so clients can pin its public key across replacement. Set
`enable-dispatch=yes` under `[git.sr.ht]` and keep the `git` service account
and `git.sr.ht-shell` dispatch defaults. The dedicated OpenSSH listener
uses SourceHut's account keys and Git permission checks. It disables
password authentication, terminal allocation, forwarding and regular
authorized-key files. [SourceHut SSH dispatch](https://man.sr.ht/git.sr.ht/installation.md#ssh-dispatch) · [OpenSSH server configuration](https://man.openbsd.org/sshd_config)

- **Docker.** Add `ssh_port` and `ssh_host_key_file` to your private plan.
  Use an absolute path under `/etc` to the existing root-owned host key with
  mode `0600`. The service publishes the selected port on host loopback.
  Make the generated `sshd_config` readable by UID 0 and mount it from the
  rendered directory. Forward Git SSH to the loopback port over a TCP edge
  listener. Recreate `git-ssh` when replacing the shared proxy network
  namespace.
- **Native Alpine.** Add `ssh_port`. Install `sshd_config` as
  `/etc/sourcehut-ssh/sshd_config` and its root-owned host key as
  `/etc/sourcehut-ssh/ssh_host_ed25519_key` with mode `0600`. Install
  `sourcehut-sshd` as a separate executable OpenRC service. Prepare an
  unlocked `git` account for key-only authentication and a Git-owned
  `/var/log/git.sr.ht` directory. Point the SSH edge at the selected
  loopback port.
- **Kubernetes.** Add `ssh_host_key_secret` and provision a separate Secret
  with the `ssh_host_ed25519_key` entry. Use a dedicated namespace that
  permits the OpenSSH container to start as root with `SETUID`, `SETGID`,
  and `SYS_CHROOT`. Kubernetes Baseline allows those capabilities.
  Restricted Pod Security requires an exception for the SSH container.
  The SSH container switches to `git` for the dispatcher and shell. Route
  the Service's `2222` port through the TCP edge and allow only that path
  in NetworkPolicy. [Kubernetes Pod Security Standards](https://kubernetes.io/docs/concepts/security/pod-security-standards/)

For recovery, stop writers before coordinating repository and PostgreSQL
backups. Preserve the service configuration and signing keys. Validate
database ownership, account login, and exact repository refs from a
separate restore target. Full suite services such as builds, tickets and
mailing lists have separate installation and recovery requirements.

## Local verification

The isolated Docker lab used PostgreSQL 18.6, Valkey 8.1.11, and synthetic
configuration and signing keys. It initialized both databases, started the
web and API processes and account webhook worker, created an account with
`meta.sr.ht-manageuser`, and signed in through the account web origin. Both
public API routes rejected external internal-auth requests. The Git client
read the exact public ref through the FastCGI handler. The handler denied a
private repository with a stale Git export marker. The proxy rejected HTTP
push discovery. The runtime used a loopback-only Docker network namespace.

An opt-in Docker run also exercised the upstream SSH dispatcher with
synthetic owner, reader, and unknown keys. The owner pushed to a private
repository. The reader could fetch and was denied push access. The unknown
key and a shell command were rejected. A fresh clone after SSH container
replacement used strict host-key checking and returned the exact commit and
file.

The web-only Kubernetes packages passed API admission under Restricted Pod
Security, and the SSH variants passed under Baseline. In a two-worker K3s
v1.35.0+k3s1 lab, the rendered SSH Deployment used a local repository PVC
and PostgreSQL 18.4, Valkey 8.1.11 and HAProxy on a separate worker. The first
SSH push through HAProxy's TCP listener created a private repository. A
read-only user could fetch and was denied a push. An unknown key was
rejected. A new application Pod served the same commit and file through the
same listener, using the supplied SSH host key and the original PVC.

The K3s lab used Flannel. A second run passed the same SSH checks on RKE2
v1.36.4+rke2r1 with bundled Cilium, three control-plane guests and two
dedicated workers. HAProxy, PostgreSQL and Valkey ran on one worker, and
SourceHut ran on the other. The replacement Pod served the exact private
commit from the original PVC through the same TCP listener with the
original SSH host key. The receipt matched 584 Python source hashes, and
the test unit and guests were removed.

Both labs used one HAProxy Pod and a local volume. Native installs, a
redundant SSH entry point, TLS, mail delivery, upgrades, backup restore and
host failover remain open.
