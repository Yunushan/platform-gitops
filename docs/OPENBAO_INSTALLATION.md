# Guarded fresh OpenBao workload installation

The separate [OpenBao package](OPENBAO_HA.md) supports five runtime formats.
`scripts/deploy_openbao_ha.py` now creates fresh workloads for RKE2, K3s and
existing Kubernetes, using explicit target and original-resource checks. It
does not initialize or unseal OpenBao, generate recovery keys, install a CNI,
provision storage, rotate credentials or upgrade an existing cluster. Docker
and native hosts use the separate
[host activation helper](OPENBAO_HOST_ACTIVATION.md).

## Bind prepared infrastructure

Render the original package into ignored private storage. Copy the
[deployment-plan example](../examples/openbao-installation.json) into private
storage and replace every placeholder with reviewed current observations.

Required inputs are the exact `openbao-contract.json` file SHA-256, an explicit
kubeconfig environment-variable reference and context, the original
`kube-system` UID and restricted OpenBao namespace UID. Kubernetes credentials
are frozen for each server operation; verified HTTPS and self-contained static
client credentials are required. There is no default-context fallback.

Pin the three Linux node UIDs, hostname/zone labels and declared internal IPv4
addresses. Nodes must be Ready and schedulable, without blocking taints. Labels
and metadata alone do not prove physical failure-domain independence.

Each member requires a different immutable `kubernetes.io/tls` Secret. The
installer requests only its UID, type, immutability/deletion state and boolean
presence of `tls.crt`, `tls.key` and `ca.crt`. It never fetches the Secret as JSON
or captures its certificate/private-key values. Certificate validity, hostname
coverage and live TLS identity must be verified separately.

Pin all six existing data/audit PVC UIDs, complete PVC-spec hashes, PV names,
PV UIDs and complete PV-spec hashes. Every claim must be Bound, Filesystem,
ReadWriteOnce, sufficiently sized and free from pending resize conditions.
Claims with data-source/clone references are refused. Each retained PV must
reference that exact claim UID/name/namespace and expose a writable ext4/xfs
Ceph RBD CSI volume. Separate PV names are insufficient: CSI driver/volume-handle
pairs must also be different. Selected claims must not already be mounted by
another Pod, even an unlabelled one.

For every StorageClass, pin its UID and a canonical configuration hash. Hash
the complete object after removing `apiVersion`, `kind` and `metadata`. Its
provisioner must match the reviewed Rook Ceph RBD driver family, and reclaim
policy must be `Retain`. PVC/PV hashes cover their entire `spec` objects.
Other hashes use sorted-key compact JSON. Changed access modes, bindings,
storage-class policy or CSI handles stop the operation.

These checks bind storage identities and declarations; they do not inspect
filesystem contents, attest encryption, establish Ceph replica health or prove
an empty/new Raft database. Independently review the intended volume contents
and recovery history before activation. The daemon remains sealed and the
installer never runs `operator init`, so this is not a new-cluster ceremony.
Storage encryption/KMS and bootstrap recovery must remain usable without
depending on this same OpenBao deployment being available.

Pin the Cilium namespace, DaemonSet and ConfigMap UIDs, all agent/init image
digests, complete agent Pod-template hash and ConfigMap-data hash. The original
current rollout needs a Ready agent on every selected node, policy mode
`default` or `always` and Kubernetes NetworkPolicy support enabled. The
installer does not change Cilium. Effective traffic enforcement remains a live
datapath acceptance requirement.

## Validate and inspect concrete admission

Local validation regenerates the original package, compares every artifact and
validates the deployment scope without constructing an API client:

```bash
python3 scripts/deploy_openbao_ha.py \
  --bundle-dir private/generated/openbao-rke2 \
  --deployment-plan private/openbao-installation.json
```

Inspect the explicit server and dry-run every resource:

```bash
python3 scripts/deploy_openbao_ha.py \
  --bundle-dir private/generated/openbao-rke2 \
  --deployment-plan private/openbao-installation.json \
  --check-server --output-dir private/openbao-check-001
```

This performs Kubernetes reads and create dry-runs only. It rejects existing
target resources, matching workloads/ReplicaSets/Pods and already-used claims.
The private output contains normalized admitted material for nine objects and
the aggregate `admitted_sha256`. Review those concrete policies and place that
hash in the private deployment plan before applying. No public configuration
or Secret contents are written.

Admission may add ordinary defaults, which are committed in the aggregate hash.
Changed declarations, configuration contents, host/privileged execution,
injected commands/environment/hooks, external/headless Services and foreign
ownership are refused. The first controller-generated Deployment revision
annotation is normalized; subsequent rollout revisions are refused. This is
not a rolling-update or adoption command.

## Create and observe the original members

```bash
python3 scripts/deploy_openbao_ha.py \
  --bundle-dir private/generated/openbao-rke2 \
  --deployment-plan private/openbao-installation.json \
  --apply --output-dir private/openbao-install-001
```

Network policy, immutable configuration, Services and disruption budget precede
the three single-member Deployments. Prerequisites and admission are checked
again before every create. Operation/contract annotations, admitted material,
original UIDs and create intent are recorded privately before proceeding.
The installer never patches/deletes resources, creates Secrets/PVCs or invokes
a shell inside a Pod.

Startup observation follows each original Deployment, ReplicaSet and Pod UID
to the declared node, container ID, image ID and distinct Pod IP. Containers
must be running and have passed their startup probe. Pod readiness may remain
false while OpenBao is sealed: the successful result explicitly reports
`requires_external_ceremony: true` and phase `awaiting-external-ceremony`.
Initialization, unseal, Raft membership, verified TLS, auth policy, audit,
encryption, HA and restore flags remain false. Probe/process observations
cannot certify those controls.

Resume an interrupted operation using its original private directory:

```bash
python3 scripts/deploy_openbao_ha.py \
  --bundle-dir private/generated/openbao-rke2 \
  --deployment-plan private/openbao-installation.json \
  --resume --output-dir private/openbao-install-001
```

An uncertain create is observed by its original operation/contract handle;
it is never reissued. A missing resource with a recorded UID is not recreated.
Changed sources, cluster, inputs, original UIDs, process invocation or admitted
material refuse resume. Preserve the journal and use a separately designed
maintenance/recovery action instead of clearing handles to bypass checks.
Receipts are private integrity records, not signed operator attestations.

## Verification scope

`python3 scripts/test_openbao_installation.py` exercises offline validation,
sealed startup, dependency drift, privilege/configuration injection, aliased
volumes, claim reuse, lost acknowledgements, unknown writes, deleted/replaced
objects and changed process IDs.

`OPENBAO_INSTALL_TEST_API=1` opts into the isolated real API fixture with the
cached `ROOK_BOOTSTRAP_TEST_BIN` inputs. It uses the actual installer for
admission, creation and original-handle resume, including a lost successful
create response and a subsequently changed container ID. Node, CSI, Cilium and
Pod status are synthetic; no worker, mount, CNI or OpenBao daemon is executed
by this fixture. `OPENBAO_INSTALL_TEST_REPORT` can select a new private receipt.
The separate real three-process Raft/TLS test remains documented in the
[package runbook](OPENBAO_HA.md). Combining these tests does not establish a
complete Kubernetes installation, physical-node HA or production key custody.

The isolated API lifecycle case has passed against the current installer
source closure. It created exactly nine original resources, recovered a lost
successful create response without retrying it, resumed without new writes and
refused a subsequently changed container invocation. Its saved receipt retains
the synthetic-status limitation and keeps real daemon, initialization, unseal,
HA and full-stack acceptance false. Temporary API processes and data were
removed and checked after completion.
