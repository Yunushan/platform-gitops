# SCM-Manager deployment

The optional SCM-Manager adapter renders one active instance on RKE2, K3s,
existing Kubernetes, Docker Compose or native Linux with systemd. SCM-Manager
3.12.2 is licensed under [AGPL-3.0-only](https://github.com/scm-manager/scm-manager/blob/3.12.2/LICENSE.txt).
The adapter pins the
[3.12.2 release](https://scm-manager.org/download/3.12.2/) by Debian image digest or
native archive SHA-256. The container artifact targets Linux amd64.

Use the [Kubernetes](../examples/scm-manager-kubernetes.json),
[Docker](../examples/scm-manager-docker.json) or
[native](../examples/scm-manager-native.json) example as a starting point.
Copy the plan under `private/`, set the deployment references and run the
renderer with the matching runtime. Use a new output directory for each render.

```sh
python3 scripts/render_scm_manager.py --runtime k3s \
  --plan private/scm-manager-kubernetes.json \
  --output-dir private/scm-manager-k3s
```

## Install and initialize

- **Kubernetes**. Prepare a dedicated namespace and a bound PVC with state
  writable by UID and GID 1000. Use `ReadWriteOncePod` if supported by the CSI
  driver. Apply `scm-manager.yaml`. The ClusterIP Service listens on port 8080.
  The NetworkPolicy blocks ingress from other pods until an explicit allow
  policy selects your trusted gateway. Use `kubectl port-forward` to initialize
  the instance locally.
- **Docker**. Prepare the state directory with owner `1000:1000`. Run
  `docker compose -f private/<bundle>/compose.yaml up -d` with Compose 2.30 or
  later if using the raw environment file option. The host port binds to
  loopback.
- **Native Linux**. Prepare a dedicated system account and state directory,
  owned by that account, with a writable `work` subdirectory on a filesystem
  that allows SQLite to load its bundled native library. Install Java 21 at
  the plan's `java_home`. Download
  the [upstream Linux archive](https://packages.scm-manager.org/repository/releases/sonia/scm/packaging/unix/3.12.2/unix-3.12.2.tar.gz),
  save it as `scm-manager-3.12.2.tar.gz`, and run
  `sha256sum -c scm-manager.sha256` from the bundle directory. Extract the
  verified package so `install_dir/bin/scm-server` exists. Keep the installation
  root-owned and the state separate. Install the generated `.service` file
  under `/etc/systemd/system/`, reload systemd and start the service.

Open `/scm/` through local access and use the initial setup token from the
restricted service logs. For unattended bootstrap, reference the `initial-password`
key of a private Kubernetes Secret or set `SCM_WEBAPP_INITIALPASSWORD` in a
root-owned `0600` host environment file. With 3.12.2, the password variable
bypasses the plugin wizard while present. After removing the bootstrap reference
and restarting, complete the plugin wizard through local access if it appears.
Verify that the administrator can sign in again. Treat the setup token and
password as credentials. [First startup](https://scm-manager.org/docs/3.12.x/en/first-startup/) · [3.12.2 wizard implementation](https://github.com/scm-manager/scm-manager/blob/3.12.2/scm-webapp/src/main/java/sonia/scm/lifecycle/PluginWizardStartupAction.java)

For remote clients, configure a TLS endpoint on your trusted gateway, retain
the `/scm` context, and set the canonical URL in SCM-Manager. Keep the backend
private. Enable forwarded-header processing only where the proxy controls the
headers and the backend accepts traffic from that proxy. [Server configuration](https://scm-manager.org/docs/3.12.x/en/administration/scm-server/)

With the project's [Cilium Gateway adapter](../scripts/render_cilium_gateway.py),
add this entry to the gateway plan's `routes` array. Match the Service name,
namespace and instance label in your SCM-Manager plan. Replace the example DNS
name with the hostname covered by the gateway listener's certificate.

```json
{
  "name": "scm-manager",
  "namespace": "scm-manager",
  "hostname": "scm.example.com",
  "protocol": "http",
  "service": "scm-manager",
  "port": 8080,
  "target_port": 8080,
  "pod_selector": {
    "app.kubernetes.io/name": "scm-manager",
    "app.kubernetes.io/instance": "scm-manager"
  }
}
```

The Gateway adapter adds the backend policy for Cilium's `ingress` identity.
Label the SCM-Manager namespace with `platform.gitops/gateway-access` set to
the gateway plan's `name`. After the route reports `Accepted` and `ResolvedRefs`,
check the certificate, repository URLs, authenticated clone and push through
HTTPS, and denial from an unapproved pod before opening client access.
[Cilium Gateway network policy](https://docs.cilium.io/en/v1.19/network/servicemesh/gateway-api/gateway-api/#cilium-s-ingress-config-and-ciliumnetworkpolicy)

## Back up, upgrade and roll back

The SCM home includes repositories, configuration, plugins, accounts and
embedded stores. Keep one writer even with redundant storage. Store backups
privately because they include account and repository data. [SCM home layout](https://scm-manager.org/docs/3.12.x/en/administration/basedirectory/)

1. Stop the instance and verify that it exited before copying the full SCM home.
   Scale the Deployment to zero and wait for its Pod to terminate on Kubernetes.
   Use `docker compose stop` on Docker or `systemctl stop` on native Linux.
   Preserve ownership and permissions and retain the old release and its settings.
2. Restore the backup to a separate claim or directory and check administrator
   login and a private Git clone with the old release. Check the candidate on a
   copy of the restored state using the new verified artifact. Verify the original
   commit, a new push, and repository privacy after restart before switching the
   live instance. Check plugin compatibility for the release in use.
3. If rollback is required, stop the new instance. Preserve any writes made
   since the upgrade for reconciliation. Start the old verified release with
   the pre-upgrade backup restored to a separate claim or directory, then check
   the account, repository privacy and expected refs before restoring traffic.

Native 3.12.1 to 3.12.2 acceptance used checksum-verified archives and synthetic
data. It preserved the original private commit, persisted a new push across a
restart, and recovered the pre-upgrade commit from the saved backup under
3.12.1. [3.12.1 archive](https://scm-manager.org/download/3.12.1/)

## Acceptance

The upstream native archive passed the local first-run setup wizard, private
repository creation, authenticated HTTP Git push and clone, anonymous access
denial and exact commit verification after restart. The pinned non-root Docker
image passed the Git checks with a read-only root filesystem. The three
Kubernetes packages passed API admission under restricted Pod Security in a
disposable Kubernetes 1.35 fixture. A K3s v1.35.0+k3s1 VM lab ran the rendered
workload with the pinned image imported offline. The workload completed the
first-run wizard and accepted a private Git push. Pod replacement on the
original local PVC preserved the exact commit. A cold backup restored to a
different worker and PVC preserved the original administrator login, repository
privacy, file contents and commit. Live RKE2 and existing Kubernetes deployments,
gateway/TLS, the optional SSH plugin, CI/CD and container upgrade acceptance
remain open.
