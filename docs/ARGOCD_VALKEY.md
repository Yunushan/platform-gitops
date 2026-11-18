# Dedicated Argo CD Valkey client preparation

`scripts/argocd_valkey.py` prepares optional Argo CD 3.5.0 client fragments and
private authentication files for the separate [Valkey TLS/Sentinel package](VALKEY_HA.md).
It supports the five cache package formats. It does not apply an application
change, create Kubernetes Secrets, modify a running cache or restart a service.

The versioned [Argo CD cache implementation](https://github.com/argoproj/argo-cd/blob/v3.5.0/util/cache/cache.go)
supports Sentinel discovery, separate data/Sentinel credentials, verified TLS,
client certificates and file-mounted credentials. The prepared arguments select
all three Sentinels, the exact master group, database zero and certificate/name
verification. They do not configure a direct-primary or plaintext fallback.

## Isolation and private material

Use a dedicated three-member cache group for this Argo CD installation. Copy
[the dedicated cache example](../examples/argocd-valkey-ha.json),
[client plan](../examples/argocd-valkey-client.json) and
[credential references](../examples/argocd-valkey-credential-refs.json) into
ignored private storage. Replace artifact, host, storage, namespace, private
path and Secret reference placeholders before use. Render the chosen cache
runtime first and pin the resulting `valkey-contract.json` byte hash in the
client plan. The example client pin is shaped for the native example; every
different plan/runtime needs its own exact commitment.

The helper requires `network.client_namespaces` to contain only the application
namespace. Argo CD's cache uses multiple key families and notification channels,
so the application data identity can access all keys/channels in this dedicated
group through an explicit command list. It cannot administer ACLs/configuration,
flush databases, execute scripts or change replication. Sharing this group with
another application is outside the prepared isolation model and is refused.

The Sentinel application identity is separate from the data, probe, replication,
Sentinel peer and Sentinel control identities. It can discover topology and
subscribe to `+switch-master` and `+replica-reconf-done`, the two events used by
Argo CD's pinned go-redis client; it cannot change Sentinel configuration or force
promotion. The original generic discovery identity remains unchanged.

Credential references select seven distinct private environment inputs: the
original five cache roles plus `argocd_data` and `argocd_sentinel`. Preserve the
original five values when preparing an application-enabled variant; the helper
does not rotate them. Keep these values in approved private environment/secret
handling, never in the plan, command arguments or public repository.

Prepare separate mTLS client material for the application, trusted by the cache
CA, with client-auth usage. The CA must validate every declared cache endpoint;
member certificates must match the names/IPs advertised to the application.
The client helper references these files and does not generate certificates.

## Prepare public-shaped fragments privately

```bash
python3 scripts/render_valkey_ha.py \
  --plan private/argocd-valkey-ha.json --runtime raw \
  --output-dir private/generated/argocd-cache

sha256sum private/generated/argocd-cache/valkey-contract.json
```

After pinning that hash in the private client plan:

```bash
python3 scripts/argocd_valkey.py \
  --bundle-dir private/generated/argocd-cache \
  --application-plan private/argocd-valkey-client.json \
  --output-dir private/generated/argocd-cache-client
```

This mode reads the exact rendered package and plan, with no private credential,
TLS or network reads. It creates argument/environment JSON, a Pod-spec fragment,
a client-file mapping and a status document. All deployment and compatibility
flags remain false. The Pod fragment describes the server, repo-server and
application-controller cache options; it is not an applyable Kubernetes object
or a complete Deployment, chart override or installation.

Both `REDIS_CREDS_DIR_PATH` and `REPOSERVER_REDIS_CREDS_DIR_PATH` point at the
same private mounted directory. This supplies the server's two cache clients
without placing values in arguments or relying on ambient password variables.
The TLS and credential mounts are read-only and reference separate Secrets.
For Docker, the [Argo CD Compose renderer](CI_GITOPS_HANDOFF.md#run-argo-cd-reconciliation-from-docker)
consumes the pinned bundle and client plan directly. For native installations,
use the arguments, environment and prepared private files at the declared
paths. Review the component's complete argument list, mounts and service identity.

## Materialize an application-enabled member variant

With all seven declared private environment inputs set, prepare each member:

```bash
python3 scripts/argocd_valkey.py \
  --bundle-dir private/generated/argocd-cache \
  --application-plan private/argocd-valkey-client.json \
  --materialize --member cache-a \
  --credential-refs private/argocd-valkey-credential-refs.json \
  --output-dir private/generated/cache-a-argocd-auth
```

Repeat for the other two members into separate new directories. The four cache
authentication files preserve the existing account/outbound configuration and
append the two application accounts. The additional `argocd-*` files supply the
client-side credentials. `argocd-valkey-client-files.json` maps those filenames
to the `auth`, `auth_username`, `sentinel_auth` and `sentinel_username` keys Argo
CD expects. The generated client values are identical across member variants.

All outputs are private mode `0600` and create-only; existing output is refused.
Only the four cache auth files belong in each member's auth directory/Secret.
The additional client files belong in the application's separate credential
mount/Secret. Set ownership/group access according to the reviewed service or
Pod identity; the cache [host helper](VALKEY_HOST_ACTIVATION.md) and
[Kubernetes installer](VALKEY_INSTALLATION.md) require their original private
input and Secret commitments. Do not mix the additional client files into a
member mount whose exact inventory is four files.

Prepare these variants before fresh activation. Changing a live cache, adding
Secret keys or rolling an existing application requires its own reviewed target,
identity, admission and recovery operation. This preparation helper performs
none of those writes and does not bypass the existing installation journals.

## Verification and remaining scope

`python3 scripts/test_argocd_valkey.py` tests all five formats, dedicated-group
refusal, trust options, role separation, preservation of original control files,
private create-only output and bounded binary cache responses. Default CI uses
no company endpoint, account, credential or service.

The explicit runtime case is
`ClientTests.test_real_argocd_server_cache_survives_primary_loss`. It requires
`ARGOCD_VALKEY_TEST_RUNTIME=1`, `VALKEY_TEST_RUNTIME=1`, a root Linux runner with
an empty isolated network namespace, OpenSSL, iproute2, the reviewed TLS build in
`VALKEY_TEST_ARTIFACTS`, the Argo CD 3.5.0 binary/install schema in
`ARGOCD_TEST_ARTIFACTS`, and envtest binaries in `ROOK_BOOTSTRAP_TEST_BIN`.
`VALKEY_TEST_REPORT` may select a new private aggregate receipt.

The fixture uses the production preparation output, six real TLS cache/Sentinel
processes, a disposable real Kubernetes API and an unprivileged native Argo CD
server. It exercises native logout/token-revocation writes, compares application
cache state on replicas, kills the primary and its colocated Sentinel, and checks
new revocation writes after promotion with the same server process. A fresh
second server must read persisted revocation state and reject revoked sessions.
It also tests denied application administration reads. Argo CD 3.5.0 keeps login
failure counters in process memory; this test does not claim those counters are
replicated. See the versioned [session storage implementation](https://github.com/argoproj/argo-cd/blob/v3.5.0/util/session/state.go).
These observations concern the server's tested cache flow. Physical-node
loss, production load, lossless failover and whole-stack HA require broader
acceptance.

The real revocation scenario passed, including the fresh reader and new writes
after promotion. Its receipt sets `argocd_server_revocation_cache_verified` for
that flow. The broader `argocd_server_cache_compatibility_verified`, repo-server,
controller and application-wide compatibility flags stay false.

Separate Compose runs used Forgejo and Gitea with Woodpecker, Argo CD 3.5.0,
and two K3s workers. Both exercised Sentinel election, original member
rejoin, manifest regeneration, and API reads of the updated resource tree.
Recovery required replacing the repo server in the Forgejo run and the
application controller in the Gitea run. The API remained running in both.
The cache processes ran on one lab host. Independent cache host loss
still needs validation.

Both stacks also passed through the optional mTLS Valkey gateway. With
one gateway stopped, Sentinel promoted a replica and the original Argo CD
containers rebuilt manifests and served the updated resource tree. This
exercised the surviving gateway address. Separate Forgejo and Gitea tests
used a Keepalived VIP and stopped HAProxy on its owner before Valkey
promotion. Argo CD recovered through the same address in both cases.
Gateway endpoint takeover across independent physical hosts still needs
validation.
[Compose setup and recovery](CI_GITOPS_HANDOFF.md#run-argo-cd-reconciliation-from-docker)
