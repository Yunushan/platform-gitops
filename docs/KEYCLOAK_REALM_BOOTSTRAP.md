# Fresh Keycloak MFA realm bootstrap

Status: a separate create-only administration workflow for a **new** dedicated
realm. It consumes the original [Keycloak package](KEYCLOAK_HA.md) for any of
the five runtime choices. It is not a replacement for workload installation,
database/schema preparation, master administrator protection, application SSO,
or identity recovery acceptance.
The separate [Argo CD identity handoff](KEYCLOAK_ARGOCD.md) consumes the completed
original realm proof to create one MFA-bound client and private application
settings; applying and verifying those settings remains a separate step.

The helper uses the exact rendered `new-realm-mfa.json` policy. It never imports
an arbitrary operator-supplied realm export, creates users or clients, changes
passwords, configures mail, updates an existing realm, deletes anything, or
starts a server. An existing realm is refused even if its name and settings
appear compatible. Use a separate recovery/configuration workflow for it.

## Reviewed inputs and authority

Copy [the example](../examples/keycloak-realm-bootstrap.json) to ignored private
storage. Replace each placeholder after reviewing the original installation:

- SHA-256 of the original rendered `keycloak-contract.json`. Every generated
  package file must still match current first-party rendering policy byte for
  byte, including the new-realm definition.
- The direct canonical HTTPS origin, matching the package hostname, with an
  optional explicit port. Paths, redirects, embedded credentials, query strings
  and fragments are refused. This initial workflow requires deployment at `/`.
- Original UUID of the server's **master** realm. Read it privately through the
  authenticated `GET /admin/realms/master` endpoint on the reviewed server.
  Pinning this UUID and the exact server version detects accidental target
  replacement; it does not attest to a container image or database cluster.
- Absolute CA file and its SHA-256. HTTPS certificate and hostname verification
  are mandatory. The client disables environment proxies and redirects.
- An environment-variable name holding an already issued, short-lived admin
  bearer **access token**, with create-realm and read-back permissions. The
  helper neither obtains a password nor refreshes or persists the token.
  If it expires, obtain another token through the approved administrator login
  and use the same environment-variable name when resuming.

The operator must separately provision initial administrator access and the
dedicated database/schema. This helper does not automate temporary bootstrap
accounts or weaken an existing master realm's MFA. Its optional runtime test
uses a disposable administrative service identity solely inside an isolated
temporary database.

The versioned [realm creation API](https://github.com/keycloak/keycloak/blob/26.4.7/services/src/main/java/org/keycloak/services/resources/admin/RealmsAdminResource.java)
accepts a full new realm representation. The helper supplies a fresh UUID and
operation/contract attributes, and later verifies them through authenticated
read-back. The versioned [authentication administration API](https://github.com/keycloak/keycloak/blob/26.4.7/services/src/main/java/org/keycloak/services/resources/admin/AuthenticationManagementResource.java)
provides the flow, nested execution and required-action representations used
for independent policy checks.

## Validate, inspect and create

Default invocation validates local inputs without constructing an API client,
reading the CA/token, creating a journal, or contacting a server:

```bash
python3 scripts/bootstrap_keycloak_realm.py \
  --bundle-dir private/generated/keycloak-rke2 \
  --realm-plan private/keycloak-realm-bootstrap.json
```

Explicit read-only inspection checks the version, original master realm and
absence of the requested dedicated realm. It saves a private operation receipt
in a new directory. There is no server-side realm dry-run or create-permission
guarantee: a later import can still be rejected.

```bash
python3 scripts/bootstrap_keycloak_realm.py \
  --bundle-dir private/generated/keycloak-rke2 \
  --realm-plan private/keycloak-realm-bootstrap.json \
  --check-server --output-dir private/keycloak-realm-check
```

After reviewing those inputs, explicitly create the realm with a **different
new** directory:

```bash
python3 scripts/bootstrap_keycloak_realm.py \
  --bundle-dir private/generated/keycloak-rke2 \
  --realm-plan private/keycloak-realm-bootstrap.json \
  --apply --output-dir private/keycloak-realm-operation
```

The helper durably records the original UUID and write intent before its single
`POST /admin/realms`. It does not retry a timed-out, rejected or interrupted
POST. Resume the **same** journal to observe the original UUID:

```bash
python3 scripts/bootstrap_keycloak_realm.py \
  --bundle-dir private/generated/keycloak-rke2 \
  --realm-plan private/keycloak-realm-bootstrap.json \
  --resume --output-dir private/keycloak-realm-operation
```

An absent attempted realm is an uncertain outcome, not permission to create
again. A foreign UUID or altered operation marker is refused. A deleted
previously observed realm is never recreated. Original source closure, plan,
contract and flow UUIDs remain bound to the operation. Keep the journal after
failure; an operator must investigate any genuinely absent/rejected create.

Each HTTPS call is bounded to 15 seconds and 2 MiB. `--wait-seconds` bounds
observation of an uncertain create to 5–300 seconds, not the total duration of
all API calls. A local exclusive lock prevents simultaneous use of the same
journal. Remote uniqueness plus the original random UUID prevent adoption by
separate competing operations; there is no distributed administrator lock.

## What verification proves

Successful read-back checks the realm's configured policy, exact custom browser
flow and forms subflow, their UUIDs, execution ordering/levels/priorities,
required password and OTP steps, cookie alternative, and default enabled OTP
enrollment action. A second observation detects changes during final checking.
The receipt contains hashes and original handles, not the token, CA contents,
user passwords, or raw API responses.

`realm_policy_verified` means the configured browser policy was observed. It
does **not** prove enrollment, login, recovery, direct-grant/client/broker
restrictions, master-realm MFA, session survival or application authorization.
Built-in flows and clients created by Keycloak are not rewritten by this helper.
Application clients must explicitly restrict unsupported authentication paths
and have their own integration/authorization acceptance. All broader MFA,
application, recovery, database HA and full-stack acceptance flags stay false.

Run `python3 scripts/test_keycloak_realm.py` for isolated recovery and policy
tests. The separate opt-in runtime fixture described in
[the package guide](KEYCLOAK_HA.md) exercises this helper over verified TLS,
then prepares only disposable browser test identities and checks actual OTP
enrollment/login and peer refresh. Two processes and one database on one host
remain insufficient to prove physical failure-domain or full-stack HA.
