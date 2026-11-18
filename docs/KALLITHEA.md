# Kallithea deployment

This adapter runs one active Kallithea server on RKE2, K3s, existing Kubernetes,
Docker Compose or native Linux. It uses the GPL-3.0-or-later upstream development
revision `976a1e77932ea7de09558989eb82182c9429ed8f`, which identifies itself as
0.7.99. The published 0.7.0 release is from 2021 and constrains Waitress and
Mercurial to older versions. The adapter pins the newer source, Python 3.11
image and Python dependency versions. [Upstream revision](https://kallithea-scm.org/repos/kallithea/changeset/976a1e77932ea7de09558989eb82182c9429ed8f) · [Published release](https://pypi.org/project/Kallithea/0.7.0/)

The dependency list pairs TurboGears 2.4.3 with `tgext.routes` 0.2.1.
The next routing extension release uses a TurboGears 2.5 API, and the
pinned Kallithea source requires TurboGears below 2.5.
[Routing extension source](https://github.com/TurboGears/tgext.routes)

## Build and render

Download the exact archive and build the Linux amd64 image in a dedicated build
directory. The Containerfile verifies the archive checksum and builds assets
from the upstream npm lockfile. Publish the resulting image to your private
registry with your normal image signing and review process, then use its
registry digest in the plan. The image build resolves Debian build packages
from Bookworm and uses a version-pinned Python requirements list. Preserve
the source archive and build inputs alongside the published image for GPL
distribution obligations.

```sh
mkdir -p private/kallithea-build
cp scripts/kallithea.Containerfile scripts/kallithea-requirements.txt private/kallithea-build/
curl -fL https://kallithea-scm.org/repos/kallithea/archive/976a1e77932ea7de09558989eb82182c9429ed8f.tar.gz \
  -o private/kallithea-build/kallithea-976a1e77932e.tar.gz
docker build --platform linux/amd64 -t kallithea:976a1e77932e \
  -f private/kallithea-build/kallithea.Containerfile private/kallithea-build
```

For native Linux, create a Python 3.11 virtual environment at `/opt/kallithea`
on a host with Git, build tools, Node.js and npm. Verify the archive with the
checksum in the Containerfile. Run its pip and frontend build commands with the
same dependency versions. Keep the installed program and virtual environment
readable by the dedicated service account and writable only by the installer.
[Upstream native installation](https://kallithea.readthedocs.io/en/latest/installation.html)

Copy the [Kubernetes](../examples/kallithea-kubernetes.json),
[Docker](../examples/kallithea-docker.json) or
[native](../examples/kallithea-native.json) example to `private/`, fill in the
deployment references, and render to a new directory.

```sh
python3 scripts/render_kallithea.py --runtime k3s \
  --plan private/kallithea-kubernetes.json --output-dir private/kallithea-k3s
```

## Prepare configuration and state

Create `kallithea.ini` using the packaged `kallithea-cli config-create` command.
Use the service account and installation path from the target runtime so Git
hooks can resolve the virtual environment's Python interpreter. Set explicit
paths under the application state directory for `cache_dir`, `index_dir`,
`archive_cache_dir`, `beaker.cache.data_dir`, `beaker.cache.lock_dir`, and
`session.data_dir`. The container state directory is `/var/lib/kallithea`.
Use an absolute SQLite path there for local evaluation or a dedicated prepared
PostgreSQL database with `sslmode=verify-full` and an explicit CA. Keep the INI
private because it contains the session secret and may contain database
credentials. [Upstream setup](https://kallithea.readthedocs.io/en/latest/setup.html)

For example, run this in the target runtime with the private output directory
already prepared. Use the target's absolute state path if it differs.

```sh
umask 077
kallithea-cli config-create /path/to/private/kallithea.ini \
  debug=false trace_errors=false use_celery=false \
  cache_dir=/var/lib/kallithea/data index_dir=/var/lib/kallithea/data/index \
  archive_cache_dir=/var/lib/kallithea/data/tarballcache \
  beaker.cache.data_dir=/var/lib/kallithea/data/cache/data \
  beaker.cache.lock_dir=/var/lib/kallithea/data/cache/lock \
  session.data_dir=/var/lib/kallithea/data/sessions \
  'sqlalchemy.url=sqlite:////var/lib/kallithea/kallithea.db?timeout=60'
```

The renderer starts the PasteDeploy application with a Waitress listener
bound explicitly for the selected runtime. The listener uses one worker
thread, matching the upstream Waitress configuration, and accepts request
bodies up to the default 1 GiB limit. Keep the listener behind the approved
gateway for remote use. [Waitress settings](https://docs.pylonsproject.org/projects/waitress/en/stable/arguments.html)

Initialize the database **once**, with the service stopped and the repositories
directory writable by its service account. For an existing empty PostgreSQL
database, add `--reuse` to initialize its tables without requiring database
creation privileges. This upstream command destroys existing Kallithea data,
so use it only against a verified empty target. Let the CLI prompt for the
administrator password. For a new private installation use `--no-public-access`.

```sh
kallithea-cli db-create -c /path/to/private/kallithea.ini \
  --repos=/var/lib/kallithea/repos --no-public-access
```

- **Kubernetes**. Prepare the namespace and a PVC writable by UID and GID
  1000. Supply `kallithea.ini` and, when needed, the PostgreSQL CA in the
  named Secret. The Secret is mounted at `/run/kallithea`. Use a temporary
  pod with the same state and configuration mounts for initial setup, then
  remove it before starting the Deployment. Apply `kallithea.yaml` and use
  port-forward for local setup or configure your trusted Gateway. The default
  NetworkPolicy requires an explicit allow policy for ingress from the proxy.
- **Docker**. Prepare the state bind mount and INI so UID 1000 can read the
  configuration and write to state. Use the same bind mounts and image to
  initialize the database with `kallithea-cli`. Then start the generated
  Compose service. Its HTTP port binds to loopback.
- **Native Linux**. Prepare the state and protected INI using the paths in
  the plan. Install the generated unit under `/etc/systemd/system/`, reload
  systemd and start the service after database initialization. Its HTTP port
  binds to loopback.

Use a TLS gateway for client HTTP traffic. Upstream also supports a dedicated
OpenSSH endpoint using generated `authorized_keys` and forced commands. That
endpoint needs its own service-account, host key and proxy configuration.
[Git and Mercurial setup](https://kallithea.readthedocs.io/en/latest/administrator_guide/vcs_setup.html)

## Recovery

Stop the writer before backing up the repository directory, private
configuration and database. Preserve ownership, Git hook paths, and the pinned
release. Restore the database and directory into a separate instance and check
private Git and Mercurial refs before routing traffic to it. Use upstream
database migration commands when changing release. Live PostgreSQL promotion,
SSH, gateway, upgrades and off-platform recovery require separate validation.

## Local verification

An isolated Docker test ran as UID 1000 with a read-only root filesystem,
mounted INI and persisted SQLite and repository state. It loaded the login
page and CSS, created repositories via the API, and verified authenticated
HTTP Git and Mercurial pushes and clones. After restart, anonymous protocol
requests were denied and both clients retrieved the exact original revisions.
Kubernetes 1.35 admitted the RKE2, K3s and existing Kubernetes bundles and
their Pods under restricted Pod Security. The native unit passed systemd syntax
validation using the fixture's local Python executable. Live cluster and native
runtime checks remain open.
