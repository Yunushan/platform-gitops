# OneDev Community deployment

The adapter packages OneDev Community 17.2.0 as one active server on RKE2,
K3s, existing Kubernetes, Docker Compose or native Linux with systemd.
OneDev reserves application clustering for Enterprise Edition. The Community
server can use a replicated PostgreSQL service and durable storage with a
single writer. [Release](https://github.com/theonedev/onedev/releases/tag/v17.2.0) · [MIT license](https://github.com/theonedev/onedev/blob/v17.2.0/license.txt) · [Edition boundary](https://docs.onedev.io/administration-guide/high-availabilty-scalabilty)

Copy the matching example to `private/`, configure the references and render to
a new directory. The plans use `rke2`, `k3s`, `k8s`, `docker` or `raw` for the
runtime. The [planner](../scripts/plan_platform_stack.py) lists this adapter
under `--forge onedev`.

```sh
python3 scripts/render_onedev.py --runtime k3s \
  --plan private/onedev-kubernetes.json --output-dir private/onedev-k3s
```

## Install

- **Kubernetes**. Start with the [Kubernetes plan](../examples/onedev-kubernetes.json).
  Prepare a dedicated namespace and PVC writable by UID and GID 1000. Use
  `ReadWriteOncePod` if supported. Apply `onedev.yaml` and use a local
  port-forward for setup. The Service exposes HTTP on 6610 and SSH on 6611
  inside the cluster. The included ingress policy requires a separate allow
  policy for the approved gateway and SSH proxy. Startup and readiness use
  OneDev's HTTP health endpoints. [Upstream health probes](https://github.com/theonedev/onedev/blob/v17.2.0/server-product/helm/values.yaml)
- **Docker**. Start with the [Docker plan](../examples/onedev-docker.json).
  Prepare a dedicated directory owned by UID and GID 1000 and run
  `docker compose -f private/onedev-docker/compose.yaml up -d` after rendering.
  Both published ports bind to host loopback. The server uses the pinned
  upstream image with a non-root launcher and dedicated state bind mount.
- **Native Linux**. Start with the [native plan](../examples/onedev-native.json).
  Prepare a dedicated system user, Java 17 or later, Git, curl, and the matching
  17.2.0 Linux distribution. Verify the release artifact and unpack it so
  `state_path/bin/server.sh` exists. The application account needs write
  access to its installation directory. Install the rendered unit in
  `/etc/systemd/system/`, reload systemd and start it. HTTP binds to loopback.
  OneDev's SSH listener binds on the host, so restrict the configured backend
  port to the intended client networks or HAProxy before starting it.
  [Native dependencies](https://docs.onedev.io/installation-guide/run-on-bare-metal)

For the native installation, a separate packaging host can also export the
release's application files from the pinned image. This command creates a
stopped container, so the wrapper never starts. Transfer the archive and its
checksum to the native host over your trusted artifact path. Verify the checksum
before extraction and confirm `version=17.2.0` in `release.properties`.

```sh
image=docker.io/1dev/server@sha256:beab585e0a3efd74f35052c46732d876181313f692340e498f7eab754b0c3615
docker pull --platform linux/amd64 "$image"
container=$(docker create --platform linux/amd64 --network none "$image")
docker cp "$container:/app/." - > onedev-17.2.0-linux.tar
docker rm "$container"
sha256sum onedev-17.2.0-linux.tar > onedev-17.2.0-linux.tar.sha256
```

The upstream container publishes a multi-architecture digest for Linux amd64
and arm64. The renderer runs its packaged wrapper directly under UID 1000.
The server has no host Docker socket or Kubernetes API credentials. Configure
and verify a suitable job executor before enabling builds for repositories.

## Database, credentials and endpoints

Use the embedded HSQLDB option for a disposable evaluation. For a durable
installation, configure an empty PostgreSQL database and its stable write
endpoint before first startup. The generated JDBC URL uses `verify-full` and
the explicit CA. Use a hostname present in the certificate on every eligible
PostgreSQL primary. Grant the OneDev database user the privileges needed to
manage its schema. Moving existing data between backends requires the
OneDev backup and restore procedure. [Switch database](https://docs.onedev.io/administration-guide/switch-database)

The Kubernetes Secret or protected host credential directory supplies `ca.crt`
and `database-password` for PostgreSQL. For automated first-run setup, set
`bootstrap` to the administrator login and email and add the
`initial-password` file to the same Secret or directory. Use a password that
meets OneDev's configured password policy. Rendered bundles
refer to files and contain no password values. Restrict the host files to
the OneDev account. Remove the bootstrap settings and password file after
verifying sign-in. With `bootstrap: null`, finish the setup in the UI.

Set the canonical HTTPS and SSH endpoints in OneDev's system settings if
configuring interactively or changing an existing installation. With a Cilium
Gateway, route the HTTPS hostname to port 6610 and permit the ingress identity
on that port. Give the SSH entry point its own HAProxy TCP route to port 6611
and allow traffic from those proxies. Verify both advertised clone URLs and
test access with a private repository. [Reverse proxy setup](https://docs.onedev.io/administration-guide/reverse-proxy-setup)

## Recovery and acceptance

Keep the server at one replica. Coordinate backups of the stopped application
directory and external database so repository and database state correspond.
Restore to isolated storage first and verify administrator access, privacy,
Git refs and file contents. Retain the exact pre-upgrade release with its
backup for rollback. [Backup and restore](https://docs.onedev.io/administration-guide/backup-restore)

For a native upgrade, unpack the verified candidate release into a separate
directory. With the target stopped and its database reachable, run the
candidate's `bin/upgrade.sh <state_path>` as the OneDev service user with the
same database environment as the service. Test against a copy of the state
and a separate database before upgrading the live instance. Docker and
Kubernetes run that same upstream upgrade command when the image's release
differs from the installation on the mounted volume. Keep the old image or
native release with a matching cold backup for rollback. [Native upgrade procedure](https://docs.onedev.io/upgrade-guide/bare-metal)

The native 17.1.1 to 17.2.0 upgrade used the embedded database and release files
exported from digest-pinned images. It retained the original private commit,
persisted a new push across restart, and recovered the original commit and
administrator access from a cold pre-upgrade backup using 17.1.1.

The local PostgreSQL recovery test stopped OneDev before copying its complete
state directory and taking a logical database backup. It restored the directory
and database under new names, then verified the existing administrator, both
project records, anonymous Git access denial, and the exact post-promotion
commit and file contents. The test used a separate database on the same
disposable PostgreSQL cluster.

The native Linux fixture used the release files exported from the pinned amd64
image. It accepted authenticated HTTP and SSH Git pushes, denied anonymous
access, and served the exact commit after restart with the bootstrap password
file removed and the same SSH host key. The
pinned Docker image passed private HTTP push and exact clone after restart as
UID 1000 with a read-only root filesystem. Both runtime checks used embedded
HSQLDB. A separate native OneDev instance connected through HAProxy to three
PostgreSQL 18.6 instances using verified TLS. It rejected a mismatched hostname,
untrusted CA and wrong password. After a controlled promotion, the same OneDev
process kept the original private Git ref and accepted new Git and database
writes through the same database endpoint. [pgJDBC certificate verification](https://jdbc.postgresql.org/documentation/ssl/)

All three Kubernetes variants passed Kubernetes 1.35 API admission under
restricted Pod Security. The K3s VM lab ran OneDev with a local PVC, the pinned
image imported offline, and CloudNativePG 1.30.0 managing three PostgreSQL 18.4
instances. The original OneDev container connected to the database write Service
using the mounted CA and password, kept its exact private Git ref after the
primary pod was deleted and a replica promoted, then accepted a new Git push
and project creation through the same Service. [CloudNativePG failover](https://cloudnative-pg.io/docs/1.30/failover/)

Live RKE2 and existing Kubernetes deployments, Gateway and SSH routing,
integrated CI, PostgreSQL-backed and container version upgrades, and
off-platform recovery remain open.
