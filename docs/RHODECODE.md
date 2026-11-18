# RhodeCode Community deployment

The adapter renders the RhodeCode Community 5.14.0 web app, VCS server and
Celery worker for Docker Compose, native Linux, RKE2, K3s and existing
Kubernetes. The three processes form one active instance.
The container examples pin the official Linux amd64 Community image by
digest. Upstream licenses Community Edition under AGPLv3 and offers its HA
features in Enterprise Edition. [Community Edition](https://rhodecode.com/opensource/) · [Official image](https://hub.docker.com/layers/rhodecode/rhodecode-ce/5.14.0/images/sha256-b23f2f8d02c326da17610fc5631b71c7eb1694c3a2819e3acee4ea551d10e552)

The web app and VCS server share repository storage and a network namespace.
The unauthenticated VCS server binds to `127.0.0.1:10010`. Docker publishes the
web port on host loopback. The Kubernetes Service includes only the web port,
and its NetworkPolicy requires an explicit allow rule from the selected
gateway. On native Linux both processes use host loopback. [VCS server configuration](https://docs.rhodecode.com/5.x/rce/admin/system_admin/vcs-server.html)

## Prepare and render

1. Copy the [Docker](../examples/rhodecode-docker.json), [native](../examples/rhodecode-native.json)
   or [Kubernetes](../examples/rhodecode-kubernetes.json) plan to `private/`.
2. Prepare durable `data` and `repos` subdirectories on the state volume.
   Docker and Kubernetes mount them at `/var/opt/rhodecode_data` and
   `/var/opt/rhodecode_repo_store`, with write access for UID/GID 999.
3. Prepare `rhodecode.ini`, `vcsserver.ini`, `gunicorn_conf_rc.py` and
   `gunicorn_conf_vcs.py` using the pinned release defaults. They are available
   under `/etc/rhodecode/conf_build` in the image. In `[app:main]` of
   `rhodecode.ini`, set `use = egg:rhodecode-enterprise-ce`. The packaged
   template selects the Enterprise application until this setting is
   changed. Keep the configuration directory private and deploy it
   read-only. Set `vcs.server` to
   `127.0.0.1:10010`, configure repository, cache and session paths in the
   mounted state, and generate unique session and application secrets. Set
   the public base URL and configure a private service API endpoint reachable
   by the VCS process. For native Linux, use the plan's absolute state paths
   instead of the container paths. [Upstream configuration](https://docs.rhodecode.com/5.x/rcstack/configuration/configuration-overview.html)
4. Configure `celery.broker_url` and `celery.result_backend` in `rhodecode.ini`
   for a private reachable Redis-compatible service. Set `vcs.hooks.protocol.v2`
   to `celery`. Git writes depend on the worker for pre-receive hook processing.
   Use a prepared PostgreSQL database with certificate verification for an
   external database deployment. The local runtime exercise uses SQLite in
   the durable `data` directory and Valkey for Celery. Enable the Git and
   Mercurial backends for this bundle. SVN over DAV requires Apache and
   separate deployment configuration. [Upstream configuration](https://docs.rhodecode.com/5.x/rcstack/configuration/configuration-overview.html) · [External database](https://docs.rhodecode.com/5.x/rcstack/install/custom-database-use.html)
5. Initialize an empty target with the release's `rc-setup-app` as the
   runtime service account. Use `--skip-existing-db --no-public-access` and
   the prepared repository directory. Supply bootstrap credentials in a
   private session and avoid exposing them through command logs. Start the
   rendered processes after initialization. [Upstream installation](https://docs.rhodecode.com/5.x/rcstack/install/installation.html)

```sh
python3 scripts/render_rhodecode.py --runtime k3s \
  --plan private/rhodecode-kubernetes.json --output-dir private/rhodecode-k3s
```

- **Kubernetes.** Prepare the namespace, PVC and configuration Secret named
  in the plan. Populate the Secret with the four prepared files and any
  required CA. Create and remove the initialization Pod before applying the
  Deployment. Expose the Service through a TLS gateway whose policy allows
  web traffic on port `10020`.
- **Docker.** Prepare the `data` and `repos` bind mount sources and the private
  configuration directory before running `docker compose up -d`. Recreate
  all three containers together when replacing the shared network
  namespace.
- **Native Linux.** Prepare the matching Community and VCS Python packages,
  dependency executables and Gunicorn 25.3 at the absolute paths in the unit.
  The install root needs `bin/gunicorn`, `vcs_bin/gunicorn`, and `bin/celery`. Set writable
  application scratch paths under `/tmp` or the state directory. Install the
  units and enable the web unit. It depends on the VCS and worker units. Native package
  assembly and live acceptance remain open. [Upstream source setup](https://docs.rhodecode.com/5.x/rcstack/dev/dev-setup.html)

If TLS terminates at a proxy, set `trusted_proxy_ips` to its exact source
addresses and have the proxy replace forwarded scheme and client IP headers.
The renderer leaves Gunicorn's forwarded-header trust empty by default.

## Recovery and acceptance

Before an upgrade or cold backup, stop all three processes and preserve the
database, repository and data directories, and private configuration.
Restore to separate targets and verify the account, privacy and exact refs.
Follow the release's migration instructions before starting upgraded
processes. [Upstream service upgrades](https://docs.rhodecode.com/5.x/rcstack/install/upgrade.html)

The isolated Docker test used the rendered process arguments and mounts, a
SQLite database, and a local Valkey 8.1.11 broker. It created a private Git
repository, pushed through the Celery pre-receive hook, and denied anonymous
Git access. After replacing the web, VCS and worker containers, a fresh clone
returned the exact commit and file. The VCS listener bound to loopback only.
Kubernetes API admission passed for all three packages under restricted Pod
Security. Live Kubernetes and native installs, TLS gateway, external database,
Mercurial and SVN operations, upgrades and backup restore remain open.
