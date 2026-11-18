# Keycloak client and Argo CD identity handoff

Status: guarded creation and read-back of a new OIDC client in the original
[MFA realm](KEYCLOAK_REALM_BOOTSTRAP.md), plus private, **unapplied** Argo CD
settings. This is an application identity building block for all five
[Keycloak runtime packages](KEYCLOAK_HA.md). It does not install or configure a
live Argo CD server, prove authorization there, or certify the full stack.

This opt-in workflow is separate from the existing private-platform renderer's
legacy SSO templates. It does not overwrite that realm, translate its realm
roles into groups, adopt existing clients, or change deployed authentication.
An existing deployment needs a reviewed transition and application acceptance.

## Inputs and boundaries

Copy [the example](../examples/keycloak-argocd.json) to ignored private storage.
Supply the exact nested original realm bootstrap plan and the SHA-256/path of
its completed original proof. That proof must bind the current source closure,
original master/realm UUIDs, rendered contract and actual MFA flow UUIDs. A
name-only realm assertion or a proof from another target is refused.

The nested server URL must use the package's advertised HTTPS port
(`public_port`, default 443). A direct management URL on another port is not
an application issuer: a mismatch is refused locally before credentials or API
access. An explicit `:443` is normalized out of the rendered issuer, matching
Keycloak's fixed advertised origin. Configure DNS, routing and certificate
trust so Argo CD and browsers reach that same origin.

The current implementation accepts the reviewed Keycloak 26.4 family and exact
Argo CD 3.5 patch versions. These are bounded compatibility families, not a
claim that any patch is current or supported. Review release provenance and
security updates separately. The application version in this plan selects
configuration policy; it does not attest to a live Argo CD image/version.

Provide a separate, generated, URL-safe client credential of 32–256 characters
through the named environment variable. Preserve it privately across retries;
do not regenerate it between apply and resume. The admin bearer token uses its
own environment reference and may be renewed through the approved administrator
login. Neither credential belongs in the plan, public Git or command arguments.

The helper creates a confidential authorization-code client with one exact
HTTPS callback, `/auth/callback`, and S256 PKCE. Browser authentication is bound
to the original verified MFA flow. Implicit, direct-password, client-credentials,
device, CIBA and standard token-exchange grants are disabled. There are no
optional client scopes, wildcard callbacks, browser CORS origins, or automatic
localhost CLI callbacks. Only profile/email default scopes and the explicit
full-path group claim mapper are configured. Existing scopes and users are
not edited.

The creation/read-back endpoints follow Keycloak's versioned
[client resource](https://github.com/keycloak/keycloak/blob/26.4.7/services/src/main/java/org/keycloak/services/resources/admin/ClientsResource.java)
and [client administration](https://github.com/keycloak/keycloak/blob/26.4.7/services/src/main/java/org/keycloak/services/resources/admin/ClientResource.java).
The mapper uses the versioned
[group-membership implementation](https://github.com/keycloak/keycloak/blob/26.4.7/services/src/main/java/org/keycloak/protocol/oidc/mappers/GroupMembershipMapper.java).
No remote secret rotation endpoint is called. The existing credential is read
privately for a constant-time comparison, never printed or saved in artifacts.
A SHA-256 commitment in the private journal detects changed retry inputs; treat
that journal as private too, and use a high-entropy generated credential.
Read-back accounts for the versioned provider's
[nullable disabled authorization-services flag](https://github.com/keycloak/keycloak/blob/26.4.7/server-spi-private/src/main/java/org/keycloak/models/utils/ModelToRepresentation.java):
absence/null is normalized to disabled; enabled services or a false-looking
value of another type are still refused. Other declared boolean fields retain
strict value/type comparison.

## Review, inspect and create

Local default validation reads the package and proof, without constructing an
API client or reading credentials/CA material:

```bash
python3 scripts/configure_keycloak_argocd.py \
  --bundle-dir private/generated/keycloak-rke2 \
  --identity-plan private/keycloak-argocd.json
```

Render a private application configuration for review. This explicit action
reads and validates the pinned **public CA certificates**, but no bearer token
or client credential. It creates a new output directory:

```bash
python3 scripts/configure_keycloak_argocd.py \
  --bundle-dir private/generated/keycloak-rke2 \
  --identity-plan private/keycloak-argocd.json \
  --render --output-dir private/argocd-identity-review
```

Inspect the original realm/flow and confirm that the selected client does not
exist. This is read-only; it cannot prove that a later create will be authorized:

```bash
python3 scripts/configure_keycloak_argocd.py \
  --bundle-dir private/generated/keycloak-rke2 \
  --identity-plan private/keycloak-argocd.json \
  --check-server --output-dir private/argocd-identity-check
```

After reviewing the inputs, explicitly create the new client:

```bash
python3 scripts/configure_keycloak_argocd.py \
  --bundle-dir private/generated/keycloak-rke2 \
  --identity-plan private/keycloak-argocd.json \
  --apply --output-dir private/argocd-identity-operation
```

The helper durably records write intent, original client/mapper UUIDs and source
closure before one POST. If its outcome is uncertain, preserve the credential,
plan, bundle, realm proof and original journal, then observe the same handle:

```bash
python3 scripts/configure_keycloak_argocd.py \
  --bundle-dir private/generated/keycloak-rke2 \
  --identity-plan private/keycloak-argocd.json \
  --resume --output-dir private/argocd-identity-operation
```

It never repeats an attempted create, recreates a deleted original client,
adopts a foreign UUID, updates client settings, changes group membership or
rotates a credential. A rejected/absent create needs operator investigation,
not an automatic new POST. One local exclusive journal lock prevents concurrent
use of the same operation; remote client-ID uniqueness handles competing fresh
operations. HTTPS uses the pinned CA, hostname verification, no redirects and
no inherited proxies. Calls are bounded to 15 seconds/2 MiB; the configurable
5–300 second wait bounds uncertain-create observation, not the entire action.

## Application handoff and authorization

`argocd-values.yaml` contains an OIDC configuration with PKCE, verified issuer
CA, exact client ID and an existing Kubernetes Secret **reference**. Its
`secret-reference.json` describes the required namespace, key and
`app.kubernetes.io/part-of: argocd` label. Neither file contains the credential.
Provision that Secret separately through the approved private secrets workflow
using the **same** credential. Do not apply the reference JSON as a Secret.
The separate [guarded application helper](ARGOCD_IDENTITY_APPLICATION.md)
can provision that dedicated immutable Secret, update the original ConfigMaps
and perform the reviewed rolling server restart on an explicit management
cluster. Integrate the same values into private GitOps before using it.

The settings follow Argo CD's versioned
[OIDC/PKCE and Secret-reference configuration](https://github.com/argoproj/argo-cd/blob/v3.5.0/docs/operator-manual/user-management/index.md)
and [RBAC model](https://github.com/argoproj/argo-cd/blob/v3.5.0/docs/operator-manual/rbac.md).
Only exact reviewed full group paths map to built-in `admin`/`readonly` roles.
The default role has no generated permission rules; unassigned authenticated
users do not receive a default readonly grant from this overlay. Group
membership must be provisioned and verified separately. Review all other RBAC
fragments, AppProject roles and local-account bindings before assessing actual
authorization. The overlay does not disable the recovery administrator or
rewrite local accounts before SSO acceptance.

Inspect and integrate the overlay into the correct private GitOps/Helm source,
provision its private Secret and trusted server-to-issuer network path, then
verify browser login, group allow/deny cases and recovery on the actual Argo CD
deployment. This helper does not patch that deployment or synchronize Argo CD.
Its `client_policy_verified` flag describes Keycloak read-back only; Argo CD
configuration, login/authorization, broader MFA, application integration,
recovery and HA flags remain false.

Run `python3 scripts/test_keycloak_argocd.py` for policy, privacy and uncertain
write tests. The separate opt-in runtime fixture in [the package guide](KEYCLOAK_HA.md)
uses the real helper against disposable Keycloak processes and PostgreSQL,
then checks actual MFA, callback/PKCE rejection, disabled password/service
grants, full group claims, signed ID-token claims and peer refresh. It does not
run an Argo CD process or prove its RBAC enforcement.
