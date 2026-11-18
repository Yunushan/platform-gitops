# Single-active Forgejo and Gitea on native Linux and Docker

Use the opt-in host renderer to provision one active Git service on a prepared
Linux host. Choose Forgejo or Gitea Community, then pair it with Tekton or
Woodpecker and Argo CD on the selected management cluster. The existing
Kubernetes chart paths remain available. [Stack support](MULTI_RUNTIME_STACKS.md)

## Render a private bundle

Copy the example for your runtime under `private/` and set the DNS names and
paths there. For a native release, verify the upstream distribution and record
the SHA-256 of the installed Linux executable. Set `provider`, `binary`,
`name`, `user` and `group` to match the selected forge and host. The Docker
examples pin reviewed rootless images by manifest digest.

- [Forgejo Docker plan](../examples/forgejo-host-docker.json)
- [Gitea Docker plan](../examples/gitea-host-docker.json)
- [Native Linux plan](../examples/gitea-host-native.json)

```bash
python3 scripts/render_forge_host.py \
  --plan private/forge-host.json \
  --output-dir private/rendered-forge-host
```

The renderer writes `app.ini` and either a Compose file or a systemd unit with
a binary checksum. It does no installation or network operations.

For a rendered [Patroni write endpoint](POSTGRES_HA.md#publish-a-stable-patroni-write-endpoint), also
pass `--postgres-bundle` and `--postgres-contract-sha256` using the reviewed
contract digest. Set the forge's `database.host` and `database.port` to the
contract's shared endpoint and use the declared application database and login.
The renderer checks those values against the contract and adds a startup check
for the planned PostgreSQL CA. On native Linux, install the generated
`postgres-ca.sha256` in the same input directory as `binary.sha256`.

## Prepare state, credentials and routing

1. Create the durable state directory for the selected service account. Docker
   uses UID/GID 1000. Install the rendered `app.ini` at `config/app.ini`
   under that directory. Give the service account read/write access to the
   state and use mode `0600` for the configuration. Keep the repository data and
   SSH host key in this directory for replacement or recovery.
2. Put the persistent `secret-key` and `internal-token` in the separate inputs
   directory, using the selected forge's `generate secret` command for each
   value. For an existing forge, preserve its current keys and adopt its
   repository paths through a reviewed migration. Supply mode `0600` files
   readable by the service account. For native Linux, install the rendered
   `binary.sha256` in the same inputs directory. The service checks it before
   starting. Install the native unit, binary and checksum with
   administrator ownership so the service account can read and verify them.
3. For PostgreSQL, provision the application database and unprivileged login.
   Use the stable read/write endpoint DNS name that appears in the database
   server certificate. Place `postgres-ca.crt` and an exact
   `host:port:database:user:password` entry in a mode `0600` `pgpass` file in
   the inputs directory. The forge connects with `verify-full`. SQLite is
   also available for a single instance through `{"type":"sqlite3"}`.
   [PostgreSQL passfile format and escaping](https://www.postgresql.org/docs/current/libpq-pgpass.html)
4. Terminate HTTPS at a reverse proxy on the forge host and forward the
   original path, `Host` and `X-Forwarded-Proto` to the loopback HTTP port.
   Forward the advertised SSH endpoint by TCP to the loopback SSH port.
   Only the local proxy should connect to these backend listeners. Set
   `webhook_hosts` to the hostnames or IPv4 addresses of the approved CI
   receivers. Gitea's strict egress mode requires a port on entries for
   receivers outside ports 80 and 443, for example `192.0.2.1:8443`.
   For a receiver on a non-public IP, list the fixed address.
   [Gitea v28.3.0 egress configuration](https://github.com/go-gitea/gitea/blob/v28.3.0/custom/conf/app.example.ini#L532-L555)
   With `custom_https_ca` enabled, supply `https-ca-bundle.crt` containing
   the system roots and any additional trusted HTTPS CA. [Gitea reverse proxy guidance](https://docs.gitea.com/administration/reverse-proxies/)
5. Review the package, then start it using the rendered Compose file or install
   the reviewed unit and start it through systemd. Use the forge's administrator CLI with
   the same binary, config, work directory, and service identity to create
   the first administrator. If using PostgreSQL, keep `PGPASSFILE` and
   `PGSSLROOTCERT` in the administrator CLI environment too.

```bash
docker compose -f private/rendered-forge-host/compose.yaml up -d
```

Install one active instance per dataset. Protect state and private credentials
in backups, and fence the old writer before attaching that state to a
replacement. The host package supplies one application process or container.
The selected storage, database and edge deployments govern recovery behavior.

## Back up and restore a host forge with PostgreSQL

Capture a matched set of the application database, the complete state tree
and the complete inputs tree while the forge is stopped. The state tree
contains the repositories, local application data and SSH host key. The
inputs tree includes keys required to read encrypted records after restore.
These commands cover the renderer's local storage layout. If you added object
storage or a separate queue, include it in the same backup window.
[Forgejo backup guidance](https://forgejo.org/docs/latest/admin/setup/backup/)
and [Gitea backup guidance](https://docs.gitea.com/administration/backup-and-restore/)
describe the additional data stores.

### Capture the set

Stop the exact systemd unit or Compose service from the reviewed deployment.
Confirm the process is gone, the forge login has no remaining database
sessions, and no other process is writing its database or files. Keep the
forge stopped until all three captures finish.

For Compose, set `SOURCE_PROJECT`, `SOURCE_COMPOSE_FILE` and `FORGE_SERVICE`
to the running deployment's project, rendered file and service name.

```bash
(
  set -euo pipefail
  docker compose --project-name "$SOURCE_PROJECT" -f "$SOURCE_COMPOSE_FILE" stop "$FORGE_SERVICE"
  running=$(docker compose --project-name "$SOURCE_PROJECT" -f "$SOURCE_COMPOSE_FILE" \
    ps --status running --quiet "$FORGE_SERVICE")
  test -z "$running"
)
```

Set `FORGE_STATE` and `FORGE_INPUTS` to the two rendered host paths. Set
`BACKUP_DIR` to a new directory on approved private backup storage with
capacity for the dump and both trees. Run with access to the input and state
files. Set `PG_BIN` to the absolute PostgreSQL client directory for the
database's major version. The `DB_HOST`, `DB_PORT`, `DB_NAME` and `DB_USER`
values come from the reviewed forge plan. The application login from the
renderer owns the application objects.

```bash
(
  set -euo pipefail
  umask 077
  mkdir -m 0700 -- "$BACKUP_DIR"
  cd -- "$BACKUP_DIR"
  env -i PATH=/usr/bin:/bin PGSSLMODE=verify-full PGCONNECT_TIMEOUT=5 \
    PGSSLROOTCERT="$FORGE_INPUTS/postgres-ca.crt" PGPASSFILE="$FORGE_INPUTS/pgpass" \
    "$PG_BIN/pg_dump" --no-password --format=custom --file=database.dump \
    --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" --dbname="$DB_NAME"
  tar --create --file=state.tar --directory="$FORGE_STATE" .
  tar --create --file=inputs.tar --directory="$FORGE_INPUTS" .
  sha256sum database.dump state.tar inputs.tar > SHA256SUMS
  sha256sum SHA256SUMS
)
```

Record the displayed manifest digest independently. Keep the three files and
manifest together on private, encrypted backup storage, along with the reviewed
runtime and configuration pins. Record a backup-time Git ref, repository ID
and expected application access for the drill. Release the write outage after
the backup set and its digest are retained.

### Restore to a fresh target

Use an isolated replacement with egress restricted so queued tasks and webhooks
stay inside the drill. Provision an empty PostgreSQL database with the original
name, matching encoding/locale and approved extensions. Provision the original
application role as owner, and prepare a passfile for the destination. Review
any extra database object owners and grants before restoration. Keep the
replacement forge stopped until validation and restoration finish.
Check the target lock capacity for a single-transaction restore of the
application schema. [PostgreSQL restore options](https://www.postgresql.org/docs/18/app-pgrestore.html)

Set `TARGET_STATE` and `TARGET_INPUTS` to **new** absolute paths on the
replacement. Set `FORGE_UID` and `FORGE_GID` to the source service's numeric
UID and GID (1000 and 1000 for Compose). Set `FORGE_RECEIPT` from the independently
recorded manifest digest. Use the reviewed runbook account with permission
to restore the archive's file ownership. Run the extraction against your
verified backup set.

```bash
(
  set -euo pipefail
  umask 077
  cd -- "$BACKUP_DIR"
  printf '%s  SHA256SUMS\n' "$FORGE_RECEIPT" | sha256sum --strict --check -
  sha256sum --strict --check SHA256SUMS
  mkdir -m 0700 -- "$TARGET_STATE" "$TARGET_INPUTS"
  chown -- "$FORGE_UID:$FORGE_GID" "$TARGET_STATE" "$TARGET_INPUTS"
  tar --extract --numeric-owner --same-owner --same-permissions --keep-old-files \
    --file=state.tar --directory="$TARGET_STATE"
  tar --extract --numeric-owner --same-owner --same-permissions --keep-old-files \
    --file=inputs.tar --directory="$TARGET_INPUTS"
)
```

Review the restored `config/app.ini`, including its absolute paths and
database settings. For Compose, render the replacement service with its new
host bind paths. Those paths map to the same locations inside the container.
Keep the backed-up `app.ini` and edit only settings that changed, such as a
database hostname or native host path. Preserve keys stored in the file as
well as the backed-up inputs and SSH host key. Install the approved PostgreSQL
CA and mode-0600 passfile for the destination, and match the rendered CA and
binary checksums when the deployment uses them. Set the `TARGET_DB_*` values
to the verified destination hostname, port, database name and user.

```bash
env -i PATH=/usr/bin:/bin PGSSLMODE=verify-full PGCONNECT_TIMEOUT=5 \
  PGSSLROOTCERT="$TARGET_INPUTS/postgres-ca.crt" PGPASSFILE="$TARGET_INPUTS/pgpass" \
  "$PG_BIN/pg_restore" --no-password --single-transaction --exit-on-error \
  --host="$TARGET_DB_HOST" --port="$TARGET_DB_PORT" --username="$TARGET_DB_USER" \
  --dbname="$TARGET_DB_NAME" "$BACKUP_DIR/database.dump"
```

Start the replacement using the same forge version. For Compose, use the
reviewed digest and the new project and file.

```bash
docker compose --project-name "$TARGET_PROJECT" -f "$TARGET_COMPOSE_FILE" \
  up --detach --pull never "$FORGE_SERVICE"
```

This start command expects the exact image digest in the destination engine's
local cache. Verify existing account
access, repository visibility, the backup-time Git ref over strict host-key
checking, and any representative issues, attachments and permission checks.
Write to a test repository and read it back. For a live replacement, fence the
former writer before switching client routes. Record the restore elapsed time
and latest verified source change for the operator's RTO and RPO assessment.

## Upgrade a prepared host forge

Select a release supported by the upstream upgrade guide. Review every
intervening release for required migration steps. A Forgejo LTS patch can
stay on its existing LTS series. The Kubernetes image update workflow has
separate release-channel selection. [Forgejo upgrade guide](https://forgejo.org/docs/latest/admin/upgrade/),
[Gitea installation and updates](https://docs.gitea.com/installation/install-from-binary/),
[Kubernetes image updates](PLATFORM_UPDATES.md)

Keep the original plan and a verified copy of the original artifact.
Copy the plan to a new private file. For native Linux, stage the verified
release in a separate version directory, keep the executable basename
(`forgejo` or `gitea`), and set its absolute path and SHA-256. For Docker,
set the reviewed rootless image digest and load or pull it before the
maintenance window.

```bash
python3 scripts/render_forge_host.py \
  --upgrade-from private/forge-host-current.json \
  --plan private/forge-host-target.json \
  --output-dir private/forge-host-upgrade
```

If the current deployment uses a reviewed Patroni bundle, pass that same
`--postgres-bundle` and `--postgres-contract-sha256`. The upgrade mode checks
that the two plans specify the same deployment apart from the runtime
artifact. It emits a new Compose definition or the native unit and checksum
files. The live `app.ini` stays in place, including settings generated by the
forge.

1. Put ingress into maintenance and stop the old writer. Capture a matched
   backup of the database, state and inputs as described above. Retain the
   old launch files and verify access to the saved artifacts.
2. For native Linux, install the generated unit and checksum files using the
   same ownership as the originals, then reload systemd and start the unit.
   For Compose, keep the existing project name and start the service from
   the new Compose definition with `up --detach --pull never`. Restrict
   access to the test clients while the new version applies migrations.
3. Check the running version, original authentication, a representative Git
   ref, and read-only permissions. Push and read back a commit over the
   published SSH or HTTPS endpoint before opening ingress.
4. If the upgrade fails, stop the replacement and restore the complete
   pre-upgrade backup to fresh paths and a fresh database with the former
   runtime artifact. Verify access and the backup-time ref before switching
   ingress back. Review any writes made since the backup separately.

For native releases with repository hooks that reference an absolute binary
or config path, regenerate the built-in hooks after startup and before opening
ingress. The same applies after restoring to relocated paths. Use the service
account, config, work path and PostgreSQL environment from the rendered unit.
For the PostgreSQL host layout, set `FORGE_BINARY`, `FORGE_STATE`,
`FORGE_INPUTS`, `FORGE_USER` and `FORGE_GROUP` to the running service values.

```bash
sudo -u "$FORGE_USER" -g "$FORGE_GROUP" \
  env HOME="$FORGE_STATE" PGSSLROOTCERT="$FORGE_INPUTS/postgres-ca.crt" \
  PGPASSFILE="$FORGE_INPUTS/pgpass" \
  "$FORGE_BINARY" --config "$FORGE_STATE/config/app.ini" \
  --work-path "$FORGE_STATE" --custom-path "$FORGE_STATE/custom" \
  admin regenerate hooks
```

Use the selected release's hook procedure for releases with a different hook
layout. [Forgejo 15 CLI](https://forgejo.org/docs/v15.0/admin/command-line/),
[Gitea CLI](https://docs.gitea.com/administration/command-line/)

## Connect a host forge to Woodpecker and Argo CD

Use the same published forge HTTPS origin in the Woodpecker forge plan and
the Argo CD repository configuration. It must resolve from the Woodpecker
server, its build workers, and the Argo CD repository server. If the forge
uses a private CA, add that CA to the trust stores of all three, including the
Woodpecker clone step's Git trust store.

Register a confidential OAuth application on the selected forge with
`https://<woodpecker-host>/authorize` as its callback. Store the client ID
and secret in a Kubernetes Secret in the Woodpecker server namespace. Use
`WOODPECKER_FORGEJO_CLIENT` and `WOODPECKER_FORGEJO_SECRET` for Forgejo, or
`WOODPECKER_GITEA_CLIENT` and `WOODPECKER_GITEA_SECRET` for Gitea. Pin the forge
plan's `oauth_secret` to that Secret. Configure the forge's `webhook_hosts`
to permit the published HTTPS endpoint of Woodpecker.
[Forgejo OAuth setup](https://woodpecker-ci.org/docs/administration/configuration/forges/forgejo) · [Gitea OAuth setup](https://woodpecker-ci.org/docs/administration/configuration/forges/gitea)

Copy the [forge selection plan](../examples/woodpecker-gitea-forge.json) into
`private/`, set `provider`, `url`, and the current and new OAuth Secret names,
then render against the base Woodpecker values for your cluster.

```bash
python3 scripts/render_woodpecker_forge.py \
  --plan private/woodpecker-forge.json \
  --values private/woodpecker-base.yaml > private/woodpecker-reviewed.yaml
```

Apply the reviewed chart values through the target cluster's GitOps workflow.
For a Docker or native Linux Woodpecker server, use the
[Woodpecker host package](CI_GITOPS_HANDOFF.md#run-woodpecker-on-a-docker-or-native-linux-host).
Sign in to Woodpecker and activate the repository so it registers the push
webhook. Use a separate Git writer for the deployment branch and a read-only
Git identity in Argo CD. Follow the
[Woodpecker handoff runbook](CI_GITOPS_HANDOFF.md#woodpecker-and-argo-cd)
to verify the exact successful commit and promote it. For Tekton, use the
[forge bridge runbook](TEKTON_FORGE_BRIDGE.md).

## Local validation

Forgejo 15.0.6 and Gitea Community 28.3.0 ran under the rendered native
systemd service settings and in containers based on the pinned rootless images.
Each case pushed to a private repository over built-in SSH and over HTTPS
through HAProxy. Each rejected an unregistered SSH key and retained the repo
ID, original SSH host key and exact Git ref after replacement.

Both native forges also used the rendered PostgreSQL connection settings
against a three-member disposable PostgreSQL 18.6 cluster. Each rejected a
wrong TLS hostname, an unrelated CA and an incorrect password. After planned
primary promotion behind a stable HAProxy endpoint, the same forge processes
read their original repository records and created new private repositories.

The bound Patroni configuration also passed a local test with both native
forge services, two generated HAProxy routers, and three PostgreSQL 18.4
members. Both services opened TLS sessions with their own PostgreSQL
logins and committed files to private repositories through the router pair.
The test stopped a router used by Forgejo and verified writes through the
surviving router. After the watchdog fenced the primary, both original
forge processes reconnected with the same hostname, retained their repository
IDs and existing files, and committed new files. The fenced database member
rebooted and rejoined as a standby. The test verified the rendered CA guard,
fixture and binary hashes, and cleanup of the service and VM processes.

The generated Docker Compose services passed the same fault sequence.
Forgejo 15.0.6 and Gitea Community 28.3.0 ran as UID 1000 from digest-pinned
images, with a read-only root filesystem and loopback-published ports. The
test used routed Docker bridges inside three disposable guests and stopped
the isolated image registry before starting the services. Both services
rejected a CA with the wrong checksum. With the reviewed CA, they used
their dedicated TLS database logins to write after a router outage and
after watchdog-driven PostgreSQL promotion. Their container IDs and
processes stayed the same, and the test verified the original repository
IDs and files. The former primary rejoined as a standby. Compose removed
both services and their bridge networks, and the VM processes and scratch
directory were removed.

Native and Compose restore drills used the backup sequence above for Forgejo
15.0.6 and Gitea Community 28.3.0 with PostgreSQL 18.6. Each drill stopped the
forge, confirmed its database sessions had closed, and captured the database,
state and inputs together. It then advanced the source repository and restored
the backup to fresh directories and a PostgreSQL server with a different
system identifier. Both replacements accepted the original account password,
API token and SSH key. They retained the private repository ID, issue and SSH
host key. A read-only collaborator could still read the private repository and
was denied a file write. The recovered Git ref matched the backup, and new
commits succeeded after recovery.

The Compose drill used the generated service and new project and bind paths.
It checked the exact backed-up `app.ini` before startup and used the
digest-pinned images with the fixture registry stopped. The lab supplied
synthetic hostname mappings for the source and target database listeners.
The native drill changed only the relocated paths in the backed-up application
configuration. Both drills removed their temporary services and scratch data.

Upgrade and rollback drills covered Forgejo 15.0.6 to 15.0.11 in the
15.x LTS series and Gitea Community 28.2.0 to 28.3.0, using PostgreSQL 18.6.
The isolated native test verified the signed release archives and used the
generated launch commands. The Compose test used pinned rootless images and
fixture-only hostname mappings. Each upgrade opened the existing state and
database, retained original access and Git refs, and accepted an SSH push.
The rollback used the pre-upgrade backup, the previous release and a fresh
database. It recovered the backup-time ref, excluded the commit made after
the backup, retained read-only access and accepted a new owner push.
[Forgejo 15.x releases](https://forgejo.org/releases/15.x/),
[Gitea 28.3.0 release](https://github.com/go-gitea/gitea/releases/tag/v28.3.0)

Separate Forgejo and Gitea cases used Docker Compose with the rendered
configuration, the published loopback port, and HAProxy serving trusted HTTPS.
Each delivered a signed push to Tekton and promoted the tested revision through
Argo CD. Forcing Compose to replace each container preserved the repository
ID, both Git refs, and the commit status. [CI/CD validation](TEKTON_FORGE_RESULTS.md#docker-compose-forge-with-k3s)

Both Compose forges also passed OAuth sign-in, private push, Woodpecker
pipeline execution and Argo CD deployment in the same disposable K3s cluster.
[Woodpecker validation](CI_GITOPS_HANDOFF.md#docker-compose-forge-with-woodpecker-and-k3s)

Physical host loss, production SSO, mail delivery, other upgrade paths and
off-platform recovery remain open.
