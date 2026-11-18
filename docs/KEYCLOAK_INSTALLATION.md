# Fresh Keycloak workload installation

Status: guarded create-only Kubernetes installation and original-handle
observation. This does not complete the [shared identity requirement](MULTI_RUNTIME_STACKS.md).
The [runtime package](KEYCLOAK_HA.md) remains available for all five runtimes;
this installer accepts RKE2, K3s and existing Kubernetes packages only. Native
and Docker installation/lifecycle orchestration remain required work. It never
creates a hidden Kubernetes cluster for those application-hosting choices.

## Prepare a concrete private scope

Render and review the original Keycloak package, then copy the
[installation example](../examples/keycloak-installation.json) into ignored
private storage. Replace every placeholder. Required observations are:

- SHA-256 of the exact `keycloak-contract.json` bytes. The installer regenerates
  the finite package and compares every file before a server operation. Changed
  workloads, realm artifacts or build recipes cannot become arbitrary manifests.
- An explicit kubeconfig environment-variable reference, context and original
  `kube-system` namespace UID. Server actions freeze self-contained static
  credentials and require a verified HTTPS API. No default kubeconfig is used.
- The original identity namespace UID with enforced restricted Pod Security,
  and each selected Linux node's UID, hostname label, reviewed zone and internal
  address. Selected nodes must be ready and schedulable. Labels are operator
  assertions, not proof of independent physical failure domains.
- Three different existing immutable Secret snapshots: server TLS, database CA
  and database password. Pin UIDs and types. The installer formats only metadata,
  immutability, deletion state and required-key presence booleans. It never
  retrieves a Secret as JSON or captures a password, certificate or private key.
  A database credential may be Opaque or an existing CNPG-compatible
  `kubernetes.io/basic-auth` snapshot with the standard `password`/`username`
  keys. This permits reusing the prepared database role Secret without copying
  its values. The declared Keycloak database username must still be independently
  reviewed against that role; no username value is read by this metadata check.
- The prepared Cilium namespace/DaemonSet/ConfigMap UIDs, agent image digest,
  complete agent Pod-template hash and ConfigMap-data hash. Hashes use canonical
  JSON with sorted keys and compact separators. Every agent/init image must be
  digest-pinned. The agent rollout must be current and ready, with one original
  ready agent on each selected host. Policy enforcement must be `default` or
  `always`, with Kubernetes NetworkPolicy support explicitly enabled. These
  settings match the reviewed Cilium 1.18.3 package; other CNI implementations
  need a separate adapter. No CNI configuration is changed by this installer.

The complete agent template must be reviewed together with its actual
configuration sources and overrides. A current ConfigMap snapshot does not
attest that an earlier agent init loaded those same values. This metadata
preflight therefore leaves runtime policy and datapath acceptance unverified;
test effective traffic rules before accepting the shared network foundation.

The optimized Keycloak artifact, correct PostgreSQL 17 schema/role and private
PKI must already be prepared and independently reviewed. This helper does not
build an image, initialize a database, import a realm, create an administrator,
provision application clients or configure SMTP. Schema validation is retained
in the workload startup command. A ready Kubernetes probe does not verify the
certificate chain/hostname, expected realm, database HA or end-user SSO.

The cluster must enforce the admission, provenance and network policies needed
for the deployment. Client-side checks do not replace the
[shared foundation](MULTI_RUNTIME_STACKS.md). In particular, the installer does
not establish effective Cilium traffic behavior or prevent a trusted cluster
administrator from changing admission while an operation is in flight.

## Validate locally and inspect admission

Local validation reads the bundle and private deployment plan, constructs no
API client and starts no service:

```bash
python3 scripts/deploy_keycloak_ha.py \
  --bundle-dir private/generated/keycloak-rke2 \
  --deployment-plan private/keycloak-installation.json
```

Explicitly inspect the live prerequisites and server-admitted resources:

```bash
python3 scripts/deploy_keycloak_ha.py \
  --bundle-dir private/generated/keycloak-rke2 \
  --deployment-plan private/keycloak-installation.json \
  --check-server --output-dir private/keycloak-check-001
```

This performs read calls and create dry-runs only. It refuses existing target
objects or matching workloads/ReplicaSets, including orphan templates that could
be adopted by a new Deployment. Private output includes normalized admitted
material for all four objects and an aggregate `admitted_sha256`. Review the
concrete policies and put that hash in the private installation plan. The
installer rejects weakened declared fields, added host/container privilege
paths, executable injections, external/headless Services and foreign ownership.
Other admitted defaults are bound by the reviewed full material hashes.

Service `clusterIP` and `clusterIPs` are excluded from admission fingerprints:
the API allocates them and can choose different addresses for dry-run and create.
The original Service UID, internal type, selectors, named port and owned Pod
endpoints are checked separately. This allocation exception does not permit
replacing an existing Service or adopting external endpoints.

## Create and observe

```bash
python3 scripts/deploy_keycloak_ha.py \
  --bundle-dir private/generated/keycloak-rke2 \
  --deployment-plan private/keycloak-installation.json \
  --apply --output-dir private/keycloak-install-001 --wait-seconds 300
```

The output directory must be new and inside this repository's ignored `private/`
tree. A private process lock, source-closure digest and durable per-object create
intents protect the operation. Creation order is NetworkPolicy, HTTPS Service,
disruption budget, then Deployment. The target/prerequisites are rechecked before
each write, and the admitted policy is checked again before creation.

Readiness requires the original four resource UIDs and admitted policies, a
current Deployment and its own current ReplicaSet, ready pinned-template Pods
spread over all selected hosts, a current healthy disruption budget, and owned
EndpointSlices routing the named HTTPS port to those exact Pod UIDs/addresses.
Container and image IDs are recorded privately. A second snapshot after live
prerequisite verification must preserve the observed Pod/container identities.

Successful `resources_ready=true` is Kubernetes resource/readiness evidence.
MFA, database HA, network datapath, application integration, session failover,
overall HA and full-stack flags stay false. The installer does not infer these
properties from Running Pods, Kubernetes probes or the separate local MFA test.

## Interrupted requests

Retain the original plan, source version and `keycloak-install-proof.json`.
Continue observation with the same directory:

```bash
python3 scripts/deploy_keycloak_ha.py \
  --bundle-dir private/generated/keycloak-rke2 \
  --deployment-plan private/keycloak-installation.json \
  --resume --output-dir private/keycloak-install-001 --wait-seconds 300
```

A create intent is synced before each request. If acknowledgment was lost, the
installer observes the object with that operation's annotations and admitted
policy, then retains its UID. An attempted request whose result remains absent
is never repeated automatically. A known original UID that disappears or changes
is refused. Later unattempted objects may be created only after the earlier
original handles have been verified. Unready workloads can be observed again;
the wait limit bounds each observation phase, not the sum of all API call timeouts.

There is no patch, delete, adoption, force apply, retry-through-new-directory,
schema migration, upgrade or automatic rollback path. Failed partial resources
and private evidence remain for deliberate recovery. Do not edit the journal,
rewrite the source digest or discard an uncertain request to bypass these guards.

## Verification scope

Simulated controller tests cover all three Kubernetes selections, prerequisite
refusals, Secret metadata-only access, admission drift, partial creates, lost
acknowledgments, deleted/replaced UIDs, unready endpoints and source changes.
An opt-in `KEYCLOAK_INSTALL_TEST_API=1` test exercises these admission fingerprints
with the pinned disposable API fixture, real server defaults, Service allocation
and read-back. It needs the same explicit `ROOK_BOOTSTRAP_TEST_BIN` reviewed in
`scripts/test_rook_api_fixture.py`. It does not run workers or Cilium/Keycloak.

Live installer execution on a prepared cluster, artifact provenance, PKI/realm
verification, actual network tests, production identity integration, upgrades,
node/database loss and independent recovery remain open acceptance requirements.
