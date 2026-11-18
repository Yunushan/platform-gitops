# Explicit activation of a prepared Gerrit pair

Status: optional Kubernetes lifecycle tooling for `rke2`, `k3s` and `k8s`.
It has passed modeled recovery tests and a disposable real Kubernetes API
admission/create/read-back test. No real Gerrit/Ceph pair has been activated by
this adapter yet. It does not qualify any complete stack or runtime as ready.

`scripts/deploy_gerrit_sites.py` activates an already initialized pair using
the [version 2 site profile](GERRIT_SITES.md). Initialization remains a separate
[explicit preparation operation](GERRIT_INITIALIZATION.md). Existing sites,
claims, mounts, secrets, providers and namespaces are prerequisites, not objects
this command creates or repairs. Docker and native Linux use the separate
[per-host activation lifecycle](GERRIT_HOST_ACTIVATION.md); this command does not
substitute Kubernetes for those runtimes.

## Private activation plan

Copy [the example](../examples/gerrit-activation.json) into ignored `private/`
storage. Replace `site_plan` with the complete, reviewed version 2 site plan.
Keep that plan's two separately approved configuration hashes and artifact locks.
The runtime image must include the tools documented in the site profile.

Pin an explicit management context and the actual `kube-system` namespace UID,
the Gerrit namespace UID, two Linux node names/UIDs in separate failure domains,
and all three existing claim/PV identities. For each claim, `csi_sha256` is the
SHA-256 of its PV's complete `spec.csi` object serialized as UTF-8 JSON with
sorted keys, separators `(',', ':')` and no trailing newline. CSI entries contain
credential **references**, not the referenced Secret values. Keep this plan and
admission receipts private. Do not publish deployment addresses or credentials.

Local site claims must be filesystem `ReadWriteOnce` or `ReadWriteOncePod`
volumes; shared Git must be a filesystem `ReadWriteMany` CephFS CSI volume.
Original claimRef UIDs, bindings, CSI hashes and node labels are rechecked.
The program refuses claims used by unrelated Pods, including retained preparation
Pods. Release completed preparation workloads deliberately before activation;
the adapter never deletes them. Other controllers must not concurrently create
consumers or replace the selected claims/nodes. These checks are not distributed
fencing or continuous storage monitoring.

## Local validation and admission review

Local mode constructs no Kubernetes client and writes no output:

```bash
python3 scripts/deploy_gerrit_sites.py \
  --plan private/gerrit-activation.json --runtime k3s
```

Set the kubeconfig environment variable named in the plan, then request an
explicit server check with a **new** private output directory:

```bash
python3 scripts/deploy_gerrit_sites.py \
  --plan private/gerrit-activation.json --runtime k3s \
  --mode check-server --output-dir private/generated/gerrit-admission
```

This reads the selected target and submits server-side `create --dry-run=server`
requests; it does not persist workload resources. Admission webhooks can be
invoked by Kubernetes. The new private directory retains the admitted material
for review. Unexpected runtime containers, environment, host access or mounts
are refused. Review the complete admitted objects and set the plan's
`admitted_sha256` to the printed aggregate before creation. Fresh operation names
must be absent; existing resources are never adopted, patched or pruned.

## Activate and resume

After preparing the sites, reviewing the target and admission, activation is an
explicit operation:

```bash
python3 scripts/deploy_gerrit_sites.py \
  --plan private/gerrit-activation.json --runtime k3s \
  --mode create --confirm ACTIVATE_PREPARED_GERRIT_PAIR \
  --output-dir private/generated/gerrit-activation
```

The adapter creates immutable startup ConfigMaps and one bounded verification
Job on each selected node. Jobs have `restartPolicy: Never`, no automatic retry,
no service-account token and no Java daemon or init invocation. They verify the
reviewed hashes, embedded stable release/API metadata, CephFS mount identity,
repository/HA storage paths, health-probe configuration, required private files
and the two system repository directories. Site mounts are read-only for these
checks. **The shared Ceph mount remains writable** so the existing storage guard
can verify its required writable mount policy. The check program does not write;
this is not a sandbox proving that all mounted data is inaccessible for writes.
Activation does not set `fsGroup` or recursively repair ownership.

Only after both original Jobs and their single original Pods exit successfully
does the adapter create the stable per-primary Services and one-replica Recreate
Deployments. Placement binds each primary to its reviewed node/failure-domain
labels, retaining node anti-affinity. Startup repeats the site guards before
Java. The adapter waits for current Deployment/ReplicaSet/Pod ownership,
readiness, placement and original zero-restart container identities.

A timeout or lost API response retains the journal and resources. Resume with
the **same unchanged plan, tooling, runtime and original operation directory**:

```bash
python3 scripts/deploy_gerrit_sites.py \
  --plan private/gerrit-activation.json --runtime k3s \
  --mode resume --confirm ACTIVATE_PREPARED_GERRIT_PAIR \
  --output-dir private/generated/gerrit-activation
```

Before each request, the journal reserves that object identity. Resume observes
the original object and verifies its operation/contract annotations, UID and
admitted policy. It never reissues an uncertain create whose object is absent.
Failed or replaced check Pods are not rerun; replaced/restarted observed daemon
processes stop initial acceptance. Resources and logs remain for review. No
automatic cleanup, delete, rollback, site reinitialization, upgrade or HA promotion
is performed. A private journal is an operational input, not a signed attestation.

## Meaning of success

`site_checks_completed` and `daemons_ready` are separate from application
acceptance. TCP readiness does not establish successful SSO, plugin health,
TLS identity, peer forwarding, Git durability or redundant entry-point behavior.
`live_plugin_compatibility_verified`, `peer_forwarding_verified`,
`git_writes_verified`, `failover_verified`, `ha_verified` and `full_stack_verified`
remain **false**, including on resume. A completed journal alone is not current
live verification; rerunning resume rechecks its implemented gates.

Real shared CephFS locking/write/recovery tests, application TLS/SSH and health
checks, peer/cache/index forwarding, protocol-correct Git writes under member
loss, router isolation, identity/CI/CD integration and recovery qualification
remain required. The current local lab does not have the resources for the
full four-storage-host provider profile; no NFS/local-disk substitute is accepted
as that requested test.

## Repeatable checks

The ordinary suite runs modeled API failures, lost create responses, missing or
replaced handles, changed nodes/claims/CSI volumes, unrelated consumers, failed
checks, Pod injection and process replacement. It does not execute Gerrit.

```bash
PYTHONPATH=scripts python3 -B -m unittest scripts/test_gerrit_activation.py
```

The optional API test uses a fresh loopback API/etcd with reviewed binaries,
static private credentials and automatic original-process cleanup. It has no
workers, storage provider or Gerrit process. As documented in the
[Rook bootstrap fixture](ROOK_BOOTSTRAP.md), set `ROOK_BOOTSTRAP_TEST_BIN` to the
reviewed envtest binary directory before enabling it:

```bash
GERRIT_ACTIVATION_TEST_API=1 PYTHONPATH=scripts \
  python3 -B -m unittest test_gerrit_activation.ActualAdmissionTests
```

The admission normalization accounts for Kubernetes-generated Job controller
UID labels/selectors and allocated Service IP fields. Original object UIDs and
the remaining admitted material remain bound to the operation. See Kubernetes'
[Job lifecycle](https://kubernetes.io/docs/concepts/workloads/controllers/job/)
and [volume access modes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#access-modes)
for the controller and storage semantics; these API properties do not prove HA.
