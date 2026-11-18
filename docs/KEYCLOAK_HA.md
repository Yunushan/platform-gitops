# Keycloak cluster and new-realm packages

Status: optional runtime packaging, not a completed identity installation or HA
acceptance. This advances the shared identity requirement for every stack in
[the multi-runtime contract](MULTI_RUNTIME_STACKS.md). Existing profiles remain
unchanged. The renderer never contacts a server or reads a credential.

The separate [fresh workload installer](KEYCLOAK_INSTALLATION.md) now provides
explicit-target, create-only installation and original-handle resume for the
three Kubernetes runtimes. The separate [new-realm bootstrap](KEYCLOAK_REALM_BOOTSTRAP.md)
creates and reads back the MFA realm for all five package choices. Neither helper
prepares a database or completes the native/Docker workload lifecycle.
The optional [Argo CD identity handoff](KEYCLOAK_ARGOCD.md) separately creates a
new confidential client bound to that realm's original MFA flow and produces
private application configuration for review; it does not deploy Argo CD.

## Prepared inputs

Copy [the example](../examples/keycloak-ha.json) into ignored private storage.
Replace every placeholder with reviewed values. This initial adapter accepts
an exact Keycloak 26.4 patch with PostgreSQL 17, following the versioned
[database compatibility guide](https://github.com/keycloak/keycloak/blob/26.4.7/docs/guides/server/db.adoc).
This is a bounded implementation family, not an LTS designation or a claim that
an arbitrary patch is current, secure or supported. Review release provenance,
security updates and runtime compatibility before choosing an artifact. Other
Keycloak/database families need an explicit compatibility change and tests.

Provide a reviewed optimized image or native distribution built with PostgreSQL,
health and metrics enabled. Start uses `--optimized`; the renderer neither
downloads nor builds software. It requires two or three different members,
hosts, peer addresses and physical failure domains. The native release needs
a compatible Java runtime and an unprivileged `keycloak` account provisioned
separately. Native SHA-256 checks pin the operator-provided distribution file
manifest before execution. Review its completeness, canonical relative entries,
absence of symlinks and writable executable paths; the renderer does not prove
that inventory from a manifest digest alone. Exclude mutable `data/` contents.

`optimized-build.json` contains the separate preparation command arguments:
`build --db=postgres --health-enabled=true --metrics-enabled=true
--transaction-xa-enabled=false`. Run that build only while preparing the reviewed
image/distribution. Those build-time options must not be repeated on
`start --optimized`: Keycloak 26.4 rejects them. Runtime packages contain only
the runtime arguments. The recipe is not evidence that an image was built with
those settings; inspect and test the actual pinned artifact before installation.

The external HA PostgreSQL endpoint, dedicated database/role and Keycloak schema
must already be prepared for the selected release. The daemon requests schema
validation and disables empty-schema initialization. It does not perform an
upgrade or bootstrap migration. The [PostgreSQL foundation](POSTGRES_HA.md) and
its restore drill provide separate building blocks; this package does not yet
bind or verify their live receipts. Database promotion, durable sessions and
application reconnect behavior still need acceptance together.

Provision a certificate/key matching the canonical HTTPS hostname and a trusted
database CA privately. The JDBC URL requests hostname verification with
`sslmode=verify-full`; no password enters an argument or generated public file.
Review Keycloak's version-specific configuration escaping, particularly for
passwords containing configuration-expression syntax.

The optional integer `public_port` selects the canonical external HTTPS port;
it defaults to 443. For example, `public_port: 8443` advertises
`https://identity.example.com:8443` in discovery, redirects and token issuers.
It does not change the member's internal HTTPS listener (8443), management
listener (9000), Gateway or external routing. Configure that entry point and
application trust for the same origin. No HTTP fallback is supported.
This follows the versioned
[Keycloak hostname configuration](https://github.com/keycloak/keycloak/blob/26.4.7/docs/guides/server/hostname.adoc),
which distinguishes advertised URLs from server listening ports.

## Render only

```bash
python3 scripts/render_keycloak_ha.py \
  --plan private/keycloak-ha.json --runtime rke2 \
  --output-dir private/generated/keycloak-rke2
```

Other runtime choices are `k3s`, `k8s`, `docker` and `raw`. The output must be a
new directory under this repository's ignored `private/` tree. Existing output
is refused and partial new files are retained after failure. The contract binds
the plan, runtime and each generated workload/realm artifact by SHA-256.

| Runtime | Generated package | Placement and credentials |
|---|---|---|
| RKE2, K3s, Kubernetes | Deployment, HTTPS Service, disruption budget, NetworkPolicy | Explicit hostname/zone combinations; different zones; named existing Secrets |
| Docker | One Compose file per member, run on different Linux hosts | Host networking and explicit peer bind addresses; private read-only TLS/CA binds and credential file |
| Native Linux | One systemd unit per member, run on different hosts | Native release manifest checks, private environment file, separate local data directory |

Kubernetes exposes only HTTPS through the Service. Health/metrics use the
separate TLS management port. Kubelet HTTPS probes check process readiness,
not certificate validity or SSO correctness. No service account token or RBAC
grant is provided. Pod placement requires the reviewed node hostname and zone
labels; it does not invent or verify physical failure domains.

Existing private Kubernetes Secrets must provide the selected database password
key, `tls.crt`/`tls.key`, and database `ca.crt`. The namespace must already exist.
No Secret contents or bootstrap administrator credentials are generated.

Docker requires Linux host networking and Compose 2.30 or newer for raw
environment-file parsing. It does not expose the Docker socket or request
privileged mode. Each protected `database.env` contains only the reviewed
`KC_DB_PASSWORD` assignment, using the chosen runtime's file syntax. Docker
and systemd environment-file quoting differ; prepare separate files as needed.
The TLS and CA directories must already exist and be readable by UID/GID 1000.

Native hosts install the generated `native-manifest.sha256` and the separately
reviewed `native-files.sha256` under the protected private directory, plus
`database.env`, `tls/` and `db-ca/`. Both manifest checks run from `native_home`.
The immutable release must have a `data/` mount target. The unit binds its own
systemd-managed local state directory there; persistent identity/session data
belongs in PostgreSQL, not a shared Keycloak home. Packages, users, permissions,
firewall rules, service activation and rolling upgrade orchestration remain
separate installation work.

The separate [host activation helper](KEYCLOAK_HOST_ACTIVATION.md) now handles
explicit first activation on one prepared Docker/native member, original-handle
resume and verified TLS readiness tied to its kernel listener. It does not
prepare the distributions/database/users or certify fleet upgrades/recovery.

## Cluster and network boundaries

The cache uses database discovery and encrypted TCP communication as described
by the versioned [cache configuration](https://github.com/keycloak/keycloak/blob/26.4.7/quarkus/config-api/src/main/java/org/keycloak/config/CachingOptions.java).
Peers need bidirectional TCP 7800 and failure-detection port 57800. Docker/native
members bind the reviewed host addresses, avoiding unreachable container-NAT
advertisements. Kubernetes uses the actual Pod IP. This is one cluster sharing
one database, not a multi-site deployment or an external Valkey cache.

The Kubernetes policy allows HTTPS from reviewed ingress namespaces, management
from monitoring, same-namespace Keycloak peers, cluster DNS, and narrowly listed
PostgreSQL destinations. Review the effective Cilium datapath, DNS placement and
service-address translation: CIDR policy behavior can differ around NAT. SMTP,
identity brokering, federation, external backchannels and additional management
access need separate explicit policy/integration work. Docker/native hosts need
equivalent firewall boundaries, including restricted management access.
This package does not generate a public ingress or a redundant external entry.

Ordinary application logging disables the Hibernate JDBC error/batch categories:
concurrent transport-certificate bootstrap can otherwise dump SQL parameters,
including private key material. General Keycloak logs, audit events and metrics
remain available, but detailed ORM troubleshooting requires a separate private
workflow. This avoids the observed parameter dump; it is not a guarantee that
every plugin or failure path redacts all secrets. The test fixture emits only
allowlisted diagnostic classifications, never raw SQL/log bodies.

## Separate new-realm MFA definition

`new-realm-mfa.json` describes a **new** dedicated realm with registration
disabled, verified emails, brute-force controls and audit events. Its browser
forms require password and OTP, with OTP enrollment enabled. Existing SSO cookies
can continue the authenticated session. It contains no users, passwords,
application clients or client secrets. The realm fields and OTP authenticator
follow the versioned [realm representation](https://github.com/keycloak/keycloak/blob/26.4.7/core/src/main/java/org/keycloak/representations/idm/RealmRepresentation.java)
and [OTP implementation](https://github.com/keycloak/keycloak/blob/26.4.7/services/src/main/java/org/keycloak/authentication/authenticators/browser/OTPFormAuthenticator.java).

The realm artifact is deliberately absent from daemon startup/import paths.
The separate bootstrap helper can create a new realm; it refuses existing ones.
Master-realm administrator MFA, recovery identities, application OIDC clients and exact redirect URIs,
client grants, scopes, group/role mapping, SMTP and service identities need a
separate guarded configuration workflow. In particular, this browser definition
does not prove that a future client's direct grants or brokered login enforce
the intended MFA policy. Those paths must be explicitly restricted and tested.

Offline tests cover all five package choices and refusal cases. An opt-in
loopback-only Kubernetes API fixture tests resource admission and read-back
without workers, Keycloak or PostgreSQL. Neither test establishes actual schema
compatibility, successful cluster formation, MFA login/enrollment/recovery,
client integration, session survival, node loss, upgrades or off-platform restore.
All corresponding acceptance flags remain false.

Run the normal package checks with `python3 scripts/test_keycloak_ha.py`.
The optional `KEYCLOAK_TEST_COMPOSE=1` check uses a standalone `docker-compose`
CLI, an empty temporary Docker configuration, and `config --no-env-resolution`;
it needs no daemon or real credential files. The optional
`KEYCLOAK_TEST_API=1` check additionally requires Linux amd64, OpenSSL and
`ROOK_BOOTSTRAP_TEST_BIN` pointing to the exact pinned envtest binaries reviewed
in `scripts/test_rook_api_fixture.py`. It starts only fresh loopback API/etcd
processes with disposable credentials and removes them when the check ends.

The separate `KEYCLOAK_TEST_RUNTIME=1` check adds a disposable Linux amd64 runtime
fixture. It requires root only to stage and drop process
identities, OpenSSL, the exact Keycloak/Temurin archives pinned in
`scripts/test_keycloak_runtime_fixture.py`, and a private PostgreSQL 17.11
installation with TLS libraries. `KEYCLOAK_TEST_ARTIFACTS`,
`KEYCLOAK_TEST_PG_ROOT` and `KEYCLOAK_TEST_PG_LIBS` select those inputs. It performs
no downloads or system package installation. Its database, realm, PKI, accounts
and OAuth client are disposable; it never imports the operator's real realm.
The fixture checks the production runtime arguments with loopback/port overrides.
Its direct foreground JVM launch follows the pinned distribution's launcher
properties so process ownership is retained during cleanup. Two local processes
and one database cannot certify physical failure-domain or database HA.

The real fixture has passed with Keycloak 26.4.7, Temurin JRE 21.0.13+9 and
PostgreSQL 17.11. It checks strict-TLS readiness of both members, production
helper creation/read-back of a new realm, original-handle resume and refusal
to overwrite an existing realm, OTP enrollment,
authorization-code/PKCE login and userinfo, rejection of password-only and bad
OTP attempts, and refresh/userinfo through the second member after stopping the
first. It also checks captured logs for the generated credential, OTP and token
values and the observed private-key SQL parameter marker. This exercises the
server-rendered HTTP browser flow, not browser JavaScript/rendering or every
authentication/recovery method. The database is a single disposable instance;
the stop is a planned same-host process termination. Cache membership, abrupt
node loss, database promotion, production clients, master-realm MFA, recovery,
mail, upgrades and fleet/deployment acceptance remain separate requirements.

The fixture also creates its application client through the guarded Argo CD
identity helper, then checks exact callback and missing-PKCE rejection,
`unauthorized_client` responses for disabled password/service grants, full group
paths in userinfo and ID tokens, and the ID-token signature with OpenSSL plus
issuer/audience/nonce/expiry claims. Its role is provider-side protocol
acceptance with synthetic identities; no Argo CD process or application RBAC
enforcement is exercised.

The fixture disables inherited HTTP proxies, verifies that `localhost` resolves
only to loopback and checks the original processes' kernel socket handles for
loopback-only listeners. An optional new `KEYCLOAK_TEST_REPORT` file under
ignored `private/` receives aggregate results, exact artifact/tool hashes and
unchanged first-party source hashes only after process/data cleanup. No user
password, OAuth code/token, OTP seed or private certificate/key is written to
that report. This local test report is not a signed deployment attestation.
