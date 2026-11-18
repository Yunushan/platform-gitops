# Zuul configuration contract

Status: private configuration generation and local-input checks. The separate
[service packages](ZUUL_SERVICES.md) now provide core runtime artifacts and a
guarded native executor path. Execution and integrated CI/HA acceptance remain
unverified; complete runtime installation is still in progress.
Keep the [full stack requirements](MULTI_RUNTIME_STACKS.md) in scope.
The optional [ZooKeeper dependency package](ZOOKEEPER.md) renders an ensemble
with matching client endpoints. Its private PKI, ACLs and live quorum acceptance
remain separate prerequisites.

## Upstream basis

The [Zuul configuration reference](https://zuul-ci.org/docs/zuul/latest/configuration.html)
defines a shared INI configuration, mandatory ZooKeeper TLS, database settings,
keystore encryption and `ZUUL_` environment interpolation. The
[Gerrit driver](https://zuul-ci.org/docs/zuul/latest/drivers/gerrit.html) uses events,
repository fetches and result reporting, each requiring appropriate access.
[Tenant configuration](https://zuul-ci.org/docs/zuul/latest/tenants.html) separates
trusted configuration repositories from untrusted projects. The
[installation guide](https://zuul-ci.org/docs/zuul/latest/installation.html) describes
node supply, executor isolation requirements and preinstalled Ansible support.

This adapter implements a bounded initial contract around those interfaces. It
does not infer that a chosen image implements a particular release merely
because a plan declares its version. Image provenance, matching dependency/API
versions and runtime validation remain mandatory.

## Render privately

Copy `examples/zuul-config.json` into ignored private storage. Replace the version
and image-digest placeholders and specify the actual private endpoints and
reviewed tenant/project lists. The example intentionally cannot run unchanged.
Use the canonical Gerrit **routing** hostname, not one HA primary's address.

```bash
python3 scripts/render_zuul_config.py \
  --plan private/zuul-config.json --runtime rke2 \
  --output-dir private/generated/zuul-configuration
```

The output contains `zuul.conf`, `tenants.yaml`, `secret-contract.json` and an
explicitly incomplete `render-status.json`. Supported selection labels are
`rke2`, `k3s`, `k8s`, `docker` and `raw`; **at this milestone these labels describe
configuration targets, not installed services**. No cluster, container, systemd
service, pipeline, worker, user, permission or Secret is created or started.
Existing output directories are rejected. Public Git must not contain generated
company hosts, tenant names, inventories or runtime credentials.

The initial plan accepts three distinct ZooKeeper hostnames and explicit ports.
Distinct names are not proof of three independent healthy servers or quorum.
Only a root HTTPS web URL is supported by this initial contract; a sub-URL needs
an explicitly built and tested web frontend. Tenant inputs currently use bounded
simple project/branch names. Unsupported values fail rather than being renamed.

Every tenant must declare its trusted configuration repositories and their
load branch. `main` is an example, not an inferred default. No project can appear
in both trust categories in one tenant. These choices are privileged policy
inputs: do not classify arbitrary application repositories as trusted just to
make a configuration load successfully.

## Private credentials and trust material

Provide these values through the private runtime environment, not the public
plan or a committed configuration file:

- `ZUUL_DATABASE_URI`: PostgreSQL connection URI with `sslmode=verify-full` and
  `sslrootcert=/etc/zuul/private/postgres-ca.pem` explicitly selected.
- `ZUUL_KEYSTORE_PASSWORD`: independently protected, recoverable encryption
  password for the chosen Zuul instance. Do not silently regenerate or rotate it.
- `ZUUL_GERRIT_HTTP_PASSWORD`: the scoped Gerrit service account's HTTP credential.
- `REQUESTS_CA_BUNDLE=/etc/zuul/private/gerrit-ca.pem`: trust bundle for Gerrit HTTP.

Provision the seven private files named in `secret-contract.json` under
`/etc/zuul/private`: ZooKeeper client certificate/key/CA, PostgreSQL and Gerrit CA
bundles, Gerrit SSH key and approved `known_hosts`. Executors additionally need
their separate build-node SSH key. Keep all keys, database credentials and
keystore recovery material outside public Git; use the private secret backend.
Provision account permissions deliberately, including the required event-stream
access and only the review/report/submit capabilities actually needed. Nothing
in this renderer grants service-account permissions.

The generated Gerrit connection enables HTTPS verification and supplies both
SSH and HTTP interfaces. CA file presence is not evidence of valid certificates,
correct hostname matching or a valid SSH identity. Host-key enforcement in the
exact selected Zuul driver must be tested against the pinned release. The
[launch pin guard and optional real transport case](ZUUL_SERVICES.md#startup-contract)
now verify endpoint-specific pins and matching/changed/missing-key behavior
against Zuul 14.3.0 and actual Gerrit 3.13.10. SSH event-stream reconnection,
HTTP reporting, other selected releases and full CI integration remain required.
Do not use an accept-new/ignore-host-key workaround as a substitute for them.

## Read-only local preflight

After configuration and private environment provisioning on a candidate runtime:

```bash
python3 scripts/zuul_runtime_preflight.py --role scheduler
```

Roles are `scheduler`, `merger`, `web` and `executor`. This reads environment
settings and file metadata, without reading certificate/private-key contents,
connecting to a network service, changing files or launching any daemon.
Projected Kubernetes Secret paths are allowed. PostgreSQL TLS downgrades and
duplicate conflicting TLS parameters are rejected. A successful local check
does **not** establish CA validity, database authentication, schema readiness,
ZooKeeper quorum, Gerrit permissions or executor sandboxing. Its report keeps
all connection, sandbox, CI and HA verification flags false.

Do not run executors simply because input files pass. The configuration sets
`manage_ansible=false` to avoid implicit installations at daemon startup;
compatible Ansible environments must be prepared and validated separately.
Bubblewrap isolation, namespaces, resource boundaries, worker credentials and
network reachability require explicit implementation and negative tests. No
privileged container or host Docker-socket shortcut is emitted here.

## Required remaining work

- Runtime packaging acceptance (including the optional [container executors](ZUUL_EXECUTORS.md)), resource/state
  sizing and coordinated rolling maintenance verified for all Zuul roles.
- ZooKeeper ensemble provisioning/TLS, database roles/schema/backup and
  recoverable project-key/keystore backups.
- Isolated executors, compatible preinstalled Ansible, and Nodepool or another
  supported node-supply design with tested worker lifecycle and log streaming.
- Trusted configuration repositories, pipelines, jobs, credential scoping and
  real Gerrit trigger/fetch/report tests. Rendering a tenant list does not create
  these repositories or pipeline definitions.
- A durable event-loss/replay design. SSH events are not assumed recoverable
  through an outage of all schedulers; the Gerrit driver documentation discusses
  durable event alternatives. Long-lived stream failover must be tested with
  the selected Gerrit routing topology.
- Keycloak/OIDC, web ingress and authentication/authorization integration.
- Live node-loss, job recovery, upgrade, restore and complete-stack acceptance.

Configuration generation and fixture tests must not be reported as successful
Gerrit/Zuul/Argo CD or Gerrit/Zuul/Flux deployment.

## Gerrit HTTPS ports

The optional `gerrit.https_port` selects a validated HTTPS port independently
of the SSH `port`. Omitting it, or selecting 443, preserves the existing URL.
Other ports remain HTTPS and use the same approved CA and hostname checks.

Explicit daemon launch escapes literal percent signs in its three private
environment inputs at the upstream ConfigParser boundary. This preserves URI
percent-encoding and literal credential text; it does not expand embedded
configuration references in passwords. Metadata-only preflight uses the original
values and does not stage secrets or start services.

Gerrit TLS verification uses the reviewed upstream boolean default. Do not add
`verify_ssl=true` to the INI file: the pinned driver retains that string and
Requests interprets it as a filename. Explicit launch rejects such overrides
and checks the selected driver policy offline. `REQUESTS_CA_BUNDLE` supplies the
approved HTTP CA, while the launcher also pins Git's `http.sslVerify`,
`http.sslCAInfo` and `GIT_SSL_CAINFO` for real merger clones. Inherited Git
verification overrides are refused before any trust or state writes.
