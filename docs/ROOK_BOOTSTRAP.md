# Optional fresh Rook and CSI bootstrap

`scripts/render_rook_bootstrap.py` and `scripts/deploy_rook_bootstrap.py`
prepare and create the Rook v1.21.2 APIs, runtime access grants, operator,
Ceph CSI operator and explicit RBD/CephFS drivers. This is the prerequisite for
the [fresh Ceph provider helper](CEPH_PROVIDER.md). It does not initialize disks
or install a CephCluster. Storage creation and client acceptance follow separately.

All five consumer runtimes (`rke2`, `k3s`, `k8s`, `docker`, `raw`) use an explicit
Kubernetes management target for the Rook provider. Docker and native clients
still need their own Ceph client configuration, restricted identities, mounts
and fencing. Selecting a runtime does not deploy a Kubernetes cluster.

## Scope and prerequisites

Copy `examples/rook-bootstrap.json` into ignored `private/` storage and replace
all placeholders. Public examples contain no company addresses or credentials.
The plan requires:

- One explicit management context, trusted HTTPS kubeconfig with static
  credentials, and original `kube-system` UID. Credential plugins are rejected.
- A pre-created namespace and its UID, explicitly labeled
  `pod-security.kubernetes.io/enforce=privileged`. Rook/CSI require host access;
  the published runtime RBAC and privileged node plugins must be reviewed.
- Kubernetes 1.32 through 1.37, the pinned Rook release's compatibility lane.
- Four or more Ready, schedulable Linux client nodes with exact UIDs and hostname
  and failure-domain labels. Two or more controller nodes span distinct zones
  or racks; tainted or cordoned nodes are refused by this adapter.
- Digest-pinned images for Rook, CSI operator, Ceph CSI plugin and every sidecar
  key, including currently disabled snapshot/addon functionality. Empty image
  values and mutable fallback tags are rejected. Obtain the reviewed images
  for this release and the actual target architecture before filling digests.
- Explicit CPU/memory requests and limits for operators, plugins and sidecars,
  and the actual kubelet root directory. Defaults for RKE2 and K3s differ from
  conventional `/var/lib/kubelet`; inspect the target's configuration.

The catalog preserves upstream API definitions and finite runtime RBAC. It
omits unused CSI NFS/NVMe service accounts and convenience/aggregate roles.
Existing CRDs, roles, bindings, service accounts or operators are never adopted
or upgraded; an existing Rook installation requires a separate lifecycle path.
Only one installation of these cluster-scoped bootstrap names is supported.

For Linux amd64, the [image inventory](../examples/rook-v1.21.2-linux-amd64-images.json)
records the public manifest digests for Rook v1.21.2, Ceph 20.2.4,
Ceph CSI operator v1.1.0 and its image defaults. Use
`rook_bootstrap_images` as the bootstrap plan's `images` object and
`ceph_image` as the provider plan's `images.ceph`. The offline lab verified
each OCI archive against its manifest, config and layer digests. The disposable
Kubernetes 1.35.0 API also admitted and returned all 97 bootstrap objects
with these image references.
The standard Ceph CSI v3.18.1 image in this inventory requires x86-64-v3.
The guest CPU presented to every controller and node plugin must expose
that feature level. The local test with a Nehalem CPU model reached
operator readiness, then both CSI plugin binaries failed to start at glibc.
[Ceph CSI image CPU compatibility](https://github.com/ceph/ceph-csi#alternative-images-for-older-cpus)
[Rook operator release](https://raw.githubusercontent.com/rook/rook/v1.21.2/deploy/examples/operator.yaml)
· [Ceph CSI operator image defaults](https://raw.githubusercontent.com/ceph/ceph-csi-operator/v1.1.0/internal/controller/defaults.go)

An isolated K3s v1.35.0+k3s1 run checked and created all 97 objects from
the same inventory using one schedulable server and three agents. The
Rook operator, both CSI operator replicas, four driver controller
replicas and eight node-plugin Pods were Ready. The kubelet on each of
the four guests registered both RBD and CephFS. The install receipt
retained the original object UIDs and both driver handoffs, and the
fixture removed its guests and network scope after verification. This
run stopped before CephCluster creation. Disk initialization, volume
provisioning, data I/O and failure recovery remain open.

There is one active Rook operator. The CSI operator has two replicas and
explicit leader election. Each CSI driver has two controller replicas with
required domain anti-affinity, plus node plugins restricted to the reviewed
nodes. Optional CSI addons, fencing and snapshot services are disabled.
Ceph availability and mount recovery still require live acceptance.

The immutable Rook configuration disables automatic discovery, device hotplug,
loop devices and unused CRUSH-rule deletion. `watch_current_namespace_only`
must agree with the intended provider namespace. The provider helper requires
`true` for a shared operator/provider namespace and `false` for separate ones.
This selection also controls the CSI operator's `WATCH_NAMESPACE`. A global
watch uses Kubernetes' canonical empty EnvVar form, without an explicit empty
`value` field that the API would discard.

## Render, inspect admission, create

```bash
python3 scripts/render_rook_bootstrap.py --runtime rke2 \
  --plan private/rook-bootstrap.json --output-dir private/rook-package

python3 scripts/deploy_rook_bootstrap.py --runtime rke2 \
  --plan private/rook-bootstrap.json --check-server \
  --output-dir private/rook-admission
```

The default deployment-helper mode validates local inputs only. `--check-server`
performs reads and server dry-runs of CRDs, RBAC, configuration and operator
Deployments. It writes the concrete admitted objects privately and reports
`admitted_core_sha256`. Inspect the objects and copy this hash into the plan.
Operation-specific annotations are excluded from the material digest.

Custom-resource admission cannot run until the CSI APIs exist. The catalog's
explicit Driver and OperatorConfig policies are reviewed with the package;
their server dry-run occurs after all created CRDs are Established and their
names accepted. Only exact defaults from those pinned CRD schemas may be added.
An unexpected custom-resource admission change stops the operation, preserving
already created resources and their journal; there is no automatic rollback.

```bash
python3 scripts/deploy_rook_bootstrap.py --runtime rke2 \
  --plan private/rook-bootstrap.json --create \
  --output-dir private/rook-create --wait-seconds 900
```

Creates are ordered: APIs, runtime resources, then explicit driver configuration.
Every request has a durable intent record and original UID. After a timeout or
lost response, use the same plan, runtime and original directory:

```bash
python3 scripts/deploy_rook_bootstrap.py --runtime rke2 \
  --plan private/rook-bootstrap.json --resume \
  --output-dir private/rook-create --wait-seconds 900
```

Resume observes an original resource when its acknowledgement was lost. It never
repeats a request whose original outcome is uncertain and resource is absent.
Resource replacement, policy drift, source changes and foreign ownership stop
the operation. Generated CSI workload and registration UIDs/specification hashes
are retained once observed; replacement is not accepted as the same handle.

## Provider handoff and verification limits

The private `rook-bootstrap-proof.json` includes `provider_operator`: original
namespace/operator/API UIDs, the pinned Rook image, and immutable ConfigMap UIDs
and canonical data hashes. Copy that object into the provider plan's `operator`
field. The provider helper verifies these identities and discovery controls,
and permits only these exact bootstrap ConfigMaps in a fresh shared namespace.
It continues to reject unrelated old Ceph state. No Secret values are read by
this handoff.

The completed receipt also supplies `storage_access_nodes`, the reviewed
node/UID/domain inventory, and `storage_csi`, the observed RBD and CephFS
driver identities, namespaces, controller/node-plugin UIDs and complete image
inventories. These are inputs to the version 2 [storage plan](CEPH_STORAGE.md).
Copy `provider_operator` into that plan's `provider.operator`, keep its OSD
hosts in `nodes`, and review the complete client inventory in `access_nodes`.
Generated resource UIDs and material hashes are rechecked before writing the
handoff. The receipt does not provision pools or prove client I/O.

`bootstrap_resources_ready=true` means the original APIs and operators are
current, CSI controllers span reviewed domains, node plugins cover reviewed
nodes, and CSI registrations retain their expected owner and attach policy.
The namespaced Driver owns its controller/node workloads through owner references;
the cluster-scoped CSIDriver uses the pinned CSI operator's
`csi.ceph.io/ownerref` namespace/name annotation instead. Fresh generated names
must be absent before a Driver is created, and observed registration UIDs are
retained in the journal.
The annotation contains JSON `Namespace` and `Name` fields, matching
[the pinned CSI reconciler](https://github.com/ceph/ceph-csi-operator/blob/v1.1.0/internal/controller/driver_controller.go)
and its untagged `types.NamespacedName` serialization.
`provider_installed`, client mounts, data roundtrips, node-loss acceptance,
`ha_verified` and `full_stack_verified` remain false.

`scripts/test_rook_bootstrap.py` exercises phased admission, refusal of broadened
RBAC/host access, uncertain-create recovery, readiness transitions and replacement
rejection with simulated Kubernetes responses. With
`CEPH_STORAGE_TEST_SCHEMA=1`, it also validates generated CRs against the actual
vendored schemas and checks rendered fields would not be silently pruned. These
checks do not establish live operator reconciliation or storage availability.

The renderer makes standard pod, field-reference and probe defaults concrete
before pinning operator policy, and canonicalizes integral resource quantities.
See the [Kubernetes v1.35 defaulting source](https://github.com/kubernetes/kubernetes/blob/v1.35.0/pkg/apis/core/v1/defaults.go).

Pinned upstream provenance and license: [catalog notes](../schemas/rook/v1.21.2/README.md).

## Isolated server-admission fixture

An optional Linux-amd64 fixture starts its own Kubernetes 1.35.0 API server and
etcd, using fresh loopback ports, a temporary CA/client certificate and a new
temporary data directory. It never uses an existing kubeconfig. When started
as root, its server children drop to UID/GID 65534, clear supplementary groups,
set no-new-privileges, and arm a parent-death signal. Child process groups are
stopped and waited for before the temporary directory is removed.

Obtain the official `envtest-v1.35.0-linux-amd64.tar.gz` from
[controller-tools](https://github.com/kubernetes-sigs/controller-tools/releases/tag/envtest-v1.35.0).
The [official release index](https://github.com/kubernetes-sigs/controller-tools/blob/HEAD/envtest-releases.yaml)
publishes the archive SHA-512; the fixture also requires exact SHA-256 pins for
all three extracted binaries. Keep them in ignored private validation storage.
No binary is downloaded automatically by the tests.

```bash
ROOK_BOOTSTRAP_TEST_API=1 \
ROOK_BOOTSTRAP_TEST_BIN=/absolute/path/to/controller-tools/envtest \
python3 -B -m unittest discover -s scripts -p test_rook_bootstrap.py \
  -k test_optional_real_kubernetes_admission
```

The fixture creates synthetic node readiness records and the bootstrap objects
inside that API. It exercises the real dry-run/create/read-back path and checks
the retained object UIDs and admitted policy hashes. There is no scheduler,
kubelet or Rook/CSI process: operator availability must remain false, and the
bounded readiness wait must stop. API admission is distinct from controller
reconciliation, Ceph initialization, client I/O and HA acceptance.
