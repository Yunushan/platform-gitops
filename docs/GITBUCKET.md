# GitBucket deployment

The adapter renders a single active GitBucket 4.48.0 service. It uses the
upstream WAR on native Linux and derives a non-root image from the official
digest-pinned image for Docker, RKE2, K3s and existing Kubernetes. Keep the application state on persistent
storage and stop the writer before moving or restoring it. [Upstream release](https://github.com/gitbucket/gitbucket/releases/tag/4.48.0)

## Render a private deployment

Copy the matching example under the ignored `private/` directory and set the
HTTPS origin and advertised SSH endpoint for the intended gateway. Use the
Kubernetes example for `rke2`, `k3s` or `k8s`.

For a container deployment, generate the build recipe first and build it in an
empty context. The recipe verifies the WAR hash and installs it with permissions
that allow UID 1000 to read it. Publish the resulting image to your approved
registry and use its resulting digest in the private plan.

```bash
mkdir -p private/gitbucket-image
python3 scripts/render_gitbucket.py --containerfile > private/gitbucket-image/Dockerfile
docker build -t gitbucket:4.48.0-nonroot private/gitbucket-image
```

```bash
python3 scripts/plan_platform_stack.py --forge gitbucket --runtime k3s
python3 scripts/render_gitbucket.py --runtime k3s \
  --plan private/gitbucket.json --output-dir private/gitbucket-rendered
```

The output directory must be new. The renderer writes the application settings
and one runtime package. Prepare the state directory or PVC before starting the
service. Copy the generated `gitbucket.conf` into the root of that state volume
on a fresh install. For an existing instance, merge the selected keys into its
configuration while the service is stopped. Preserve credentials and its existing
SSH host keys. GitBucket manages the file as part of its application state.

## Start the service

- **RKE2, K3s and existing Kubernetes.** Prepare the dedicated namespace and
  the requested single-writer PVC. Seed the configuration and ensure UID 1000
  can write the state. Review and apply `gitbucket.yaml` to the selected context.
  A ClusterIP exposes HTTP on 8080 and SSH on 29418. The included NetworkPolicy
  denies pod ingress until an allow policy is added for the intended gateway.
  Use an enforcement-capable CNI and a scoped port-forward for initial setup.
- **Docker.** Prepare the named bind mount with owner UID and GID 1000, then
  run `docker compose -f private/gitbucket-rendered/compose.yaml up -d`. The
  published HTTP and SSH ports bind to loopback. Put the approved proxy or SSH
  forwarder on the same host.
- **Native Linux.** Install Java 17 or later, the upstream WAR at the configured
  path, and the dedicated unprivileged account. Set the state directory owner
  to that account. Install the generated `gitbucket.war.sha256` as the WAR path
  plus `.sha256`. Keep the WAR, checksum, Java, and systemd unit administrator-owned
  and read-only for the service account. Install `gitbucket.service`, reload
  systemd and start the unit. Both listeners bind to loopback.

Change the upstream bootstrap administrator password through the private
setup connection before opening HTTPS or SSH to users. Terminate HTTPS at the
gateway and route only trusted gateway clients to the HTTP listener. Route
SSH separately and verify its presented host key. [GitBucket configuration](https://github.com/gitbucket/gitbucket/wiki/Basic-configurations)

## Database, upgrades and recovery

Prepare a dedicated PostgreSQL database for installations using the shared HA
database tier. Configure `database.conf` directly in the private state volume
with the JDBC URL, login and secret supplied by the site's secret delivery
workflow. GitBucket uses embedded H2 when the external database is absent.
Back up application state and the database together with the writer stopped.
Restore into a separate environment and check private repository access and
exact Git refs before making it reachable. [Upstream database configuration](https://github.com/gitbucket/gitbucket/wiki/External-database-configuration)

For upgrades, read the upstream migration and plugin notes for the target
version, take a restorable backup, then replace the image digest or WAR and
checksum during a maintenance window. Restore the matching pre-upgrade database
and state if rollback is required after a database migration.

The native 4.48.0 WAR passed authenticated HTTP and SSH clone and push,
anonymous denial for a private repository, and exact ref persistence across
restart. The derived Docker image ran as UID 1000 with a read-only root
filesystem and passed HTTP clone, push and exact ref verification across
restart. The RKE2, K3s and existing Kubernetes packages passed Kubernetes 1.35
API admission, including Pod admission under the restricted policy.

In a disposable K3s 1.35.0+k3s1 cluster, the rendered workload ran with the
derived image imported offline and a local persistent volume. A Git push through
a Service port-forward survived Pod replacement on the original PVC. After
stopping the writer, a cold state backup restored to a new PVC and namespace on
another worker. The restored repository remained private and served the exact
commit and file contents. The native, Docker and K3s runtime tests used embedded
H2. External PostgreSQL, gateway routing, production authentication and live RKE2
and other Kubernetes deployments remain open.
