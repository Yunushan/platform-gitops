# Gogs deployment

The adapter renders one active Gogs v0.14.4 instance for RKE2, K3s, existing
Kubernetes, Docker, and native Linux on amd64. Container deployments use the
digest-pinned upstream non-root image. The native service checks the official
release binary before starting. [Gogs v0.14.4 release](https://github.com/gogs/gogs/releases/tag/v0.14.4)

## Render and prepare state

Copy the matching `examples/gogs-*.json` plan into `private/`. Set the public
HTTPS origin, SSH endpoint, and database settings for the target. Use the
Kubernetes example for `rke2`, `k3s`, and `k8s`.

```bash
python3 scripts/plan_platform_stack.py --forge gogs --runtime k3s
python3 scripts/render_gogs.py --runtime k3s \
  --plan private/gogs.json --output-dir private/gogs-rendered
```

The examples use a dedicated PostgreSQL database and `verify-full` TLS. Give
the Gogs login ownership of its database and supply the CA that signed the
database server certificate. Use `{"type": "sqlite3"}` for an embedded
database on the same state volume. Keep the database password and application
secret outside the plan and generated configuration. Generate a stable random
application secret. A random hex database password also avoids quoting
ambiguities in Gogs's PostgreSQL connection settings.

For a fresh state volume, install `app.ini` under `gogs/conf/app.ini` relative
to its root. The service account needs write access to the state. Preserve the
configuration and SSH keys when adopting existing state. Review existing data
paths before replacing settings. Startup requires a nonempty configuration file
and the configured credential variables.

- **Kubernetes.** Create the namespace, single-writer PVC, and credentials
  Secret. Set `secret-key` and, for PostgreSQL, `database-password` in the
  credentials Secret. Provide a separate Secret containing `ca.crt` for
  PostgreSQL. Apply `gogs.yaml` to the intended context. The NetworkPolicy
  starts with no ingress rules. Keep it in place during bootstrap and use an
  admin-only port-forward. Add a scoped allow policy for the selected gateway
  before publishing the service. The ClusterIP serves HTTP on 3000 and Git SSH
  on 2222.
- **Docker.** Use Docker Compose 2.30 or later. Create the state directory with
  owner UID and GID 1000. Place `GOGS_SECURITY_SECRET_KEY` and, for PostgreSQL,
  `GOGS_DATABASE_PASSWORD` in the protected environment file configured in the
  plan. Use `docker compose -f private/gogs-rendered/compose.yaml up -d`.
  The published ports bind to loopback. [Compose environment file reference](https://docs.docker.com/reference/compose-file/services/#env_file)
- **Native Linux.** Verify the official amd64 archive SHA-256 before extracting
  the binary to the configured path. Install `git`, `ssh-keygen`, and the
  dedicated service account. Install `gogs.sha256` next to the binary using
  the binary's filename with `.sha256` appended. Keep the binary, checksum,
  unit and environment file administrator-owned. Supply the same environment
  variables as Docker, then install and start `gogs.service`. Both listeners
  bind to loopback. [Release assets and checksums](https://github.com/gogs/gogs/releases/expanded_assets/v0.14.4)

## Bootstrap and recover

Start Gogs with the private configuration and wait for the first web startup
to initialize the schema. Run `gogs admin create-user` in the same service
environment to create the first administrator, using a protected shell or
execution session. The upstream command takes the password as an argument.
Use a private setup connection and avoid command history or shared process
visibility during this step. Registration and the web installer are disabled
by the rendered configuration. Publish HTTPS at the selected gateway and route
Git SSH separately. [Gogs installation and administrator setup](https://gogs.io/docs/getting-started/installation)

Stop the writer before taking a coordinated backup of the state and database.
Keep the application secret and database trust material with the recovery
inputs. Restore into an isolated environment and verify the repository refs,
permissions, and SSH host keys. Review release-specific migrations before
replacing the image or binary. For rollback across a schema change, restore the
matching pre-upgrade data and binary.

The official Linux binary passed private repository creation, authenticated
HTTP and SSH clone and push, and exact ref and host-key verification after
restart on SQLite. The pinned Docker image ran as UID 1000 with a read-only root
filesystem and preserved the private repository and exact ref after an HTTP
push and restart. All three Kubernetes packages passed API and restricted Pod
Security admission.

A disposable K3s 1.35.0+k3s1 cluster ran the rendered workload with the pinned
image imported offline. Authenticated HTTP and SSH writes through a Service
port-forward survived pod replacement on the same PVC. With the writer stopped,
a cold backup restored the SQLite database, repository data and SSH host keys
to a new PVC in another namespace on a second worker. The restored repository
remained private and returned the exact commit, file contents and SSH host key.
The test used local volumes.

The native binary also ran against three local PostgreSQL 18.6 instances with
synchronous replication and a stable HAProxy TCP endpoint. Gogs rejected a
hostname mismatch, an untrusted CA, and an incorrect password. During a planned
promotion, the lab stopped the old primary and retargeted HAProxy. The same Gogs
process reconnected over verified TLS, preserved the private repository and
exact commit, and accepted a new Git push and repository creation. This check
used a controlled database promotion with all instances on one lab host.
[PostgreSQL certificate verification](https://www.postgresql.org/docs/18/libpq-ssl.html)

A separate K3s VM run used CloudNativePG 1.30.0 and PostgreSQL 18.4 with
three instances, required synchronous replication, and failover quorum.
The rendered Gogs Deployment connected to the database write Service by DNS
with `verify-full`, a mounted CA certificate, and a password from a Kubernetes
Secret. Deleting the primary database pod triggered promotion. The same Gogs
container reconnected through the same Service, served the exact private Git
ref, and accepted a new push and repository creation. The test used local
volumes and reached Gogs through a Service port-forward.

Live RKE2 and other Kubernetes deployments, production identity, gateway
routing and physical shared-storage failover remain open.
