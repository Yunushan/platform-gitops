# Apache Allura deployment

The renderer packages the Apache Allura 1.22.1 web app and task worker for
RKE2, K3s, existing Kubernetes, Docker Compose, and native Linux. They require
a prepared MongoDB deployment and a Solr core for Allura. The web app includes
smart Git over HTTPS using Allura bearer tokens. It checks the project and tool
ACLs before invoking Git's HTTP backend as the unprivileged Allura user.
[Release notes](https://allura.apache.org/posts/2026-allura-1.22.1.html) · [Upstream SCM hosting](https://forge-allura.apache.org/docs/getting_started/scm_host.html)

## Build and render

The Containerfile checks the Apache release archive against its published
SHA-512. It uses pinned Python base images, the release's requirements and
compiled frontend assets, and installs the packaged Git plugin. Subversion
requires a separate `pysvn` build and SCM service. If you distribute the
image privately, retain the release checksum, signature and build recipe and
record the image digest in the deployment plan.
[Apache release checks and signature](https://allura.apache.org/download.html)

```sh
mkdir -p private/allura-build
cp scripts/allura_git_http.py scripts/bounded_subprocess.py scripts/strict_json.py private/allura-build/
curl -fL https://downloads.apache.org/allura/allura-1.22.1.tar.gz \
  -o private/allura-build/allura-1.22.1.tar.gz
docker build --platform linux/amd64 -t allura:1.22.1 \
  -f scripts/allura.Containerfile private/allura-build
```

The release archive's Python distribution metadata reports `0.1.dev0`.
The image label and verified archive identify the actual Apache release.
For native Linux, use Python 3.14, Git, curl, libmagic and zip. Unpack the
verified release to `/opt/allura/source` and reproduce the Containerfile's
virtual environment and plugin installation under `/opt/allura/venv`.
Install `scripts/allura_git_http.py`, `scripts/bounded_subprocess.py` and
`scripts/strict_json.py` in
`/opt/allura/source/Allura`.
Keep both directories writable only by the installer.

Copy the [Kubernetes](../examples/allura-kubernetes.json),
[Docker](../examples/allura-docker.json), or
[native](../examples/allura-native.json) plan to `private/` and fill in the
deployment references. For example:

```sh
python3 scripts/render_allura.py --runtime k3s \
  --plan private/allura-kubernetes.json --output-dir private/allura-k3s
```

## Prepare dependencies and private configuration

- Prepare isolated MongoDB databases for Allura's main, project and task
  data, activity stream and encryption vault. Use TLS with verified peer
  identity and distinct deployment credentials. MongoDB uses SSPL.
  [Upstream installation](https://forge-allura.apache.org/docs/getting_started/installation.html) · [MongoDB license](https://www.mongodb.com/legal/licensing/server-side-public-license)
- Prepare a private Solr endpoint with the Allura schema for the Solr
  release you tested. For a fresh Solr 9.11 index, the following command
  copies the release's core and updates legacy cache and spatial field
  classes. It also removes the legacy remote-streaming option.
  Install the output as the core's instance directory with ownership
  appropriate for the Solr service. Keep the Solr admin interface on the
  private network. Rebuild the index from MongoDB and repositories when
  moving from an older core.
  [Solr upgrade notes](https://solr.apache.org/guide/solr/latest/upgrade-notes/solr-upgrade-notes.html)

  ```sh
  python3 scripts/prepare_allura_solr.py \
    --source-archive private/allura-build/allura-1.22.1.tar.gz \
    --output-dir private/allura-solr-core
  ```

- Create a private `allura.ini` based on the bundled production example.
  Point `use` in `[app:main]` at
  `config:/opt/allura/source/Allura/development.ini#main` and set
  `set debug = false`. Keep the `[app:task]` section with `use = main`
  and `override_root = task`. Configure the four MongoDB URIs
  (`ming.main.uri`, `ming.project.uri`, `ming.task.uri`, and
  `activitystream.master`), `solr.server`, the public base URL and domain,
  a new session JWT secret, and new MongoDB field encryption key material.
  Set `session.secure = true` for HTTPS. Preserve the production logging
  sections and direct any file-based log handlers into the state directory
  or use stream handlers. Configure the refresh hook URL for the private
  web endpoint.
  [Allura configuration](https://forge-allura.apache.org/docs/getting_started/installation.html#change-default-configuration)

Set `scm.repos.root` to the `scm` subdirectory of your state directory.
The container mounts state at `/var/lib/allura`. Prepare it for UID and GID
1000 and give the web app and worker access to the same repository files.
For native Linux, use the service account and state path in the plan.
Keep database credentials, JWT secret, encryption keys, and CA files in
the private configuration directory or Kubernetes Secret.

Initialize **only empty, dedicated databases and index** with all permanent
services stopped. The upstream setup drops application data and clears the
search index. Run the following as the service account with the state and
configuration mounts that the rendered service uses. Enter the administrator
name and password at the prompt.

```sh
ALLURA_TEST_DATA=false /opt/allura/venv/bin/paster setup-app /path/to/allura.ini
```

For Kubernetes, prepare the namespace, state PVC and named Secret before
applying `allura.yaml`. Use a temporary pod with the same state and
configuration mounts for setup. The Deployment co-locates the web app and
task worker, with one active Pod and a Recreate rollout. Add an ingress
NetworkPolicy for the approved proxy. With Docker Compose, only the web
port is published, bound to loopback. Native Linux uses separate web and
worker systemd units with the web listener on loopback. Protect the web
listener behind the approved TLS gateway.

## Git over HTTPS

Set `scm.clonechoices.git` to include HTTPS and set `scm.host.https.git`
to `https://forge.example.com/git$path` using the deployment hostname.
The Git endpoint recognizes smart HTTP discovery, fetch, and push. It requires
an active user's Allura bearer token for each request, including reads. Use
a bearer token from the Allura OAuth account page and configure the Git client
to send it as an `Authorization: Bearer` header. Protect its storage and rotate
it through Allura. The token retains its normal Allura API permissions.
[Allura bearer authentication](https://forge-allura.apache.org/rest-api-docs/#oauth-with-bearer-tokens) · [Git HTTP backend](https://git-scm.com/docs/git-http-backend)

For TLS termination at a reverse proxy, include that proxy's source IPs or
CIDRs in the optional `trusted_proxy_cidrs` plan field. Restrict the web
listener to the proxy and have the proxy replace forwarded scheme and client
IP headers before forwarding. The listener trusts no forwarded scheme
headers by default. Configure the repository refresh hook to reach the web
listener at a private URL and supply its CA through the service's trust
store when the endpoint uses TLS. Git requests have a 1 GiB body limit
and are spooled to temporary storage before Git processes them.
[Gunicorn proxy trust](https://gunicorn.org/reference/settings/#forwarded_allow_ips)

## Recovery

Stop the web app, worker and Git writer before taking a coordinated backup.
Preserve the MongoDB databases and field encryption vault and keys, the
private configuration, and the repository tree. Restore into a separate
environment and verify the account, project access and exact refs. Rebuild
the search index using Allura's commands as needed. Each release's upgrade
notes specify database migrations, including field encryption changes.
[Upstream maintenance](https://forge-allura.apache.org/docs/getting_started/administration.html) · [1.22.1 migration notes](https://allura.apache.org/posts/2026-allura-1.22.1.html)

## Local verification

The isolated Docker fixture initialized Allura against MongoDB 8.0 and the
adapted Solr 9.11 core. It rendered the login page and initialized a private
project's Git repository through the Allura task worker. An owner cloned
and pushed over HTTPS with certificate verification and a bearer token.
A Member could read the private ref and received a denial at both push
endpoints. Anonymous, invalid-token, revoked-token, and unrelated-account
requests were blocked. Cloning after restarting both services returned the
exact commit. All three Kubernetes packages passed API admission under
restricted Pod Security.

A disposable K3s v1.35.0+k3s1 cluster ran the rendered Allura Deployment on
one worker and MongoDB, Solr and HAProxy on another. An owner cloned and
pushed through the HAProxy TLS listener with certificate verification. A
Member could read the exact private ref and was denied push discovery.
Anonymous Git discovery required authentication. After replacing the Allura
Pod, a fresh clone through the same listener returned the exact commit and
file from the original PVC. The run exercised streaming Git HTTP responses
under Gunicorn. The fixture used Flannel and kept Allura on the same worker
with a node-local PVC.
Live native and RKE2 deployment, Cilium Gateway, Git SSH, upgrades, backup
restore and host failover remain open.
