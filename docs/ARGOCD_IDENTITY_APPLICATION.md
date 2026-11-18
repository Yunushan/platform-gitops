# Apply the original Keycloak identity handoff to Argo CD

Status: an explicit-target operation that provisions one dedicated immutable
OIDC Secret, updates two existing Argo CD ConfigMaps and requests a guarded
rolling restart of the original server Deployment, then observes the
HTTPS server's public OIDC settings. It consumes the completed original
[Keycloak client workflow](KEYCLOAK_ARGOCD.md). It does not install Argo CD,
change passwords, alter local recovery accounts, change
AppProject roles, or certify login/authorization/HA from configuration alone.

All five Keycloak package choices can use this helper. Argo CD still requires
an existing, explicitly selected Kubernetes management cluster for its API and
controllers; Docker/native Keycloak hosting does not create a hidden cluster.

## Review the original deployment

Copy [the example](../examples/argocd-identity-application.json) to ignored
private storage. Pin the original identity plan/proof files and their SHA-256
digests. The completed provider proof must match current first-party policy,
original realm/client/mapper IDs and the unchanged private client credential.
An expired administrator access token may be renewed through its existing
environment reference; the OIDC client credential must remain unchanged.

Pin the management context and original `kube-system` namespace UUID. The
helper freezes self-contained static credentials and verifies HTTPS; default
kubeconfig discovery, credential plugins and insecure API connections are
refused. Pin the original Argo namespace, server Deployment UID, entire Pod
template digest, complete Deployment spec digest and image digest. Choose a
fresh reviewed `rollout_id` UUID, unchanged between inspection and apply.
Its existing rollout must be ready. The existing strategy must be
`RollingUpdate`, allow progress and retain serving capacity; the helper does
not change replicas, placement, image or rollout strategy. This
metadata check does not attest to a running image's version or physical HA;
the HTTPS endpoint must separately report the selected exact Argo CD version.

Review and pin both original ConfigMap UIDs and `material()` digests, including
their complete data, annotations, labels, owner references and binary data.
The helper's public `material(obj)` function defines the canonical projection;
its `fingerprint(value)` uses canonical JSON SHA-256. Resource versions and
managed-fields bookkeeping are excluded from that digest but resource versions
are tested atomically during each actual update.

Pin `projects(client, namespace)`, the sorted original AppProject name/UID/role
inventory digest. This preserves existing authorization and detects changes
during the operation. Nonempty extra `policy.*.csv` RBAC fragments are refused:
review and consolidate those separately instead of silently retaining another
grant path. The global policy change deliberately replaces the reviewed
`policy.csv` and default role; review all affected local users and project roles.

Prepare the corresponding values in the correct **private** GitOps/Helm source
before a live handoff. A controller reconciling older values can revert a live
patch; this helper detects that drift and does not suppress reconciliation.
It does not push a company repository or disable auto-sync automatically.

The application HTTPS CA file and digest are separate from the issuer CA in
the original identity plan. Both use certificate/hostname verification. The
application origin comes from that identity plan. Redirects and inherited
HTTP proxies are disabled. No login or management token is sent to the public
version/settings observation endpoints.

## Inspect admission, then apply

Default execution validates local plans, proofs and the rendered package. It
does not construct network clients or read a credential/CA file:

```bash
python3 scripts/apply_argocd_identity.py \
  --bundle-dir private/generated/keycloak-rke2 \
  --application-plan private/argocd-identity-application.json
```

Explicit server inspection requires the private provider access token, the
unchanged client credential and the selected management kubeconfig. It checks
the original live realm/client policy and credential, application version,
workload and authorization inventory, then performs real admission dry-runs:

```bash
python3 scripts/apply_argocd_identity.py \
  --bundle-dir private/generated/keycloak-rke2 \
  --application-plan private/argocd-identity-application.json \
  --check-server --output-dir private/argocd-application-review
```

Review the generated `argocd-values.yaml` and `secret-reference.json`, the
original pinned objects, and the reported `admitted_sha256`. Copy only that
reviewed hash into the private application plan. The reference JSON remains
a declaration, not a Secret manifest or a live provisioning status report.
The operation receipt is authoritative for observed Secret UID/provisioning.
No actual client credential is saved in those artifacts or the journal.

Explicit apply uses a different new private operation directory:

```bash
python3 scripts/apply_argocd_identity.py \
  --bundle-dir private/generated/keycloak-rke2 \
  --application-plan private/argocd-identity-application.json \
  --apply --output-dir private/argocd-application-operation
```

The order is the immutable dedicated credential, restricted group/default-role
RBAC, OIDC issuer/client/URL settings, then the reviewed restart UUID in the
original Deployment's Pod-template annotations. Unrelated ConfigMap keys, labels,
owner references and recovery-account settings are preserved. Admission must
retain the exact reviewed configuration and ownership. The Secret is sent
through stdin; ConfigMap and Deployment patches use protected temporary files rather than
putting private configuration in command arguments. Those files are removed.

Each patch tests the original UID and current resource version in the same
JSON Patch request. No forced update, client-side apply annotation, replacement
object, automatic rollback, or deletion is used.

The restart is required because the selected Argo CD server constructs its
[session verifier HTTP transport](https://github.com/argoproj/argo-cd/blob/v3.5.0/util/session/sessionmanager.go)
once when the server object is created. Its
[listener restart loop](https://github.com/argoproj/argo-cd/blob/v3.5.0/cmd/argocd-server/commands/argocd_server.go)
reuses that object. Public settings can therefore reflect a new private OIDC
CA while session verification still uses the old trust configuration. A new
Pod process loads the intended trust; TLS verification remains enabled.

## Resume an uncertain original write

Before each write, the helper durably records its intent, original handles,
source closure, before/after hashes and credential commitment. If the response
is lost or the request times out, preserve the same inputs and journal:

```bash
python3 scripts/apply_argocd_identity.py \
  --bundle-dir private/generated/keycloak-rke2 \
  --application-plan private/argocd-identity-application.json \
  --resume --output-dir private/argocd-application-operation
```

Resume observes the original operation markers and expected contents; it never
repeats an attempted create or patch. An unchanged ConfigMap or Deployment after a lost
response is still uncertain, not permission to patch again. A deleted or
replaced observed object is refused. A conflicting edit, different source,
credential, project-role inventory or deployment identity requires operator
investigation. Do not start another operation to bypass those checks.

Once all four writes are observed, the helper waits for the original
Deployment's new generation to be fully updated and available, and for `/api/v1/settings`
over verified HTTPS to expose the expected URL, issuer, client, scopes,
requested group claim and PKCE setting. It checks the original objects/provider
again before recording success. Public settings read-back follows the
versioned [Argo CD settings service](https://github.com/argoproj/argo-cd/blob/v3.5.0/server/settings/settings.go).
It cannot prove that a human login or an authorization decision works.

`--wait-seconds` bounds each uncertain-write/settings observation phase to
5–600 seconds, not the duration of all bounded API calls. Kubernetes calls use
the shared management client's bounds; HTTPS calls use 15 seconds and 512 KiB.
Receipts describe a completed command, not perpetual monitoring. Later
authorized project or configuration changes can require fresh verification.

## Verification scope

Normal tests use simulated controllers and provider responses to check write
ordering, retained data, admission, original UIDs, privacy, drift and lost
responses. `ARGOCD_IDENTITY_TEST_API=1` additionally uses the checksum-pinned
envtest binaries described in [the Keycloak guide](KEYCLOAK_HA.md), with
`ROOK_BOOTSTRAP_TEST_BIN` selecting that exact local directory. It proves real
admission/JSON Patch/read-back and deleted-Secret refusal. Readiness and public
settings are explicitly simulated in that API-only fixture; no workers run.

The separate opt-in native runtime fixture adds real Keycloak, PostgreSQL,
Argo CD, Valkey and Kubernetes API processes with disposable identities. It
uses the official pinned Argo CD 3.5.0 multi-call binary and CRD manifest, and
a reviewed private Valkey 8.1.11 build. The official
[Argo CD image definition](https://github.com/argoproj/argo-cd/blob/v3.5.0/Dockerfile)
also dispatches server mode through that binary. This is not a production
version recommendation. Artifact and build digests are pinned in the fixture;
it performs no downloads or system package installation.

The native fixture has passed with actual browser cookies after password/OTP
authentication: reader project reads succeed, reader project writes are
denied, an administrator can create a project and an authenticated user with
no mapped group cannot read it. It also checks a new server process after the
guarded restart, resume without repeated writes, sensitive-value log absence
and process/data cleanup. These results apply to the fixture's exact source
and artifact digests, not an arbitrary deployment or version.

The native fixture's Valkey is password-protected, memory-only and bound to
loopback; its cache transport is not TLS or a failover test. Argo CD uses a
restricted namespaced Kubernetes service identity and verified HTTPS for its
browser/API and identity provider. Synthetic Deployment readiness is used only
as an API guard input. The restart hook stops the original standalone server,
starts a new process with the same inputs, and observes verified HTTPS readiness
before advancing that synthetic generation. The real standalone server supplies application behavior;
there are no worker Pods or physical failure domains. Actual deployment,
network-policy datapath, cache/database promotion, fleet upgrades and independent
restore acceptance remain separate requirements.

Run normal checks with `python3 scripts/test_argocd_identity.py`. The runtime
check is `python3 scripts/test_argocd_identity_runtime.py`, explicitly enabled
with `ARGOCD_IDENTITY_TEST_RUNTIME=1`, the Keycloak runtime input variables in
[its guide](KEYCLOAK_HA.md), `ROOK_BOOTSTRAP_TEST_BIN`,
`ARGOCD_TEST_ARTIFACTS` and `ARGOCD_TEST_VALKEY`. The first selects the private
directory containing the exact Argo CD binary and manifest; the second is the
reviewed cache binary path. Aggregate source-bound receipts contain no user
password, browser cookie, OAuth token, private key or credential value.
