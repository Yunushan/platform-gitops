# Optional Cilium installation and owned patch lifecycle

Status: implementation in progress. This adapter installs Cilium and standard
Gateway API CRDs onto an explicitly prepared Kubernetes target. It adds patch
upgrades, explicit patch rollback and interrupted-operation recovery for its
own installations. A separate accepted five-VM case now covers live CNI,
policy positive/negative traffic, control-plane quorum recovery and verified
HTTPS serving. Real chart rendering and simulated API tests alone do not prove
these behaviors. Physical HA and restoration remain unverified. The full
[multi-runtime objective](MULTI_RUNTIME_STACKS.md) remains open.

## Topology and compatibility

The initial topology uses IPv4 cluster-pool IPAM, VXLAN, explicit MTU,
kube-proxy replacement, a separately pinned Envoy DaemonSet, Gateway API
host-network listeners and two Cilium operators across failure domains.
Resource requests/limits and an operator disruption budget are included.
Gateway and TLS-interception secret references use one package-owned namespace.
Gateway certificate sync is enabled; TLS-interception secret sync is disabled
in this initial topology. Application routes and their TLS Secrets use the
separate [Gateway route adapter](CILIUM_GATEWAY.md).

The adapter accepts two explicitly reviewed compatibility lanes:

| Cilium patches | Gateway API | Kubernetes release cores |
| --- | --- | --- |
| 1.18.x | 1.2.0 | 1.31–1.33 |
| 1.19.x | 1.4.1 | 1.32–1.35 |

Tests render the actual 1.18.3/1.18.4 charts and the 1.19.10 chart with its
complete six-CRD Gateway API 1.4.1 standard export. The new lane follows the
[pinned Kubernetes matrix](https://github.com/cilium/cilium/blob/v1.19.10/Documentation/network/kubernetes/compatibility.rst)
and [Gateway prerequisites](https://github.com/cilium/cilium/blob/v1.19.10/Documentation/network/servicemesh/gateway-api/installation.rst).
Use the explicit `gateway_api_version: "1.4.1"` field for that lane. Existing
plans without this field retain the legacy 1.2.0 interpretation; a 1.19 plan
cannot silently inherit it. Mixed version pairs and out-of-lane Kubernetes
versions are refused. These are pinned compatibility inputs; review provenance,
security notices and deployment-specific compatibility before choosing releases.
Minor-version migrations remain a separate implementation requirement.

All five application-host runtime labels preserve an explicit Kubernetes
target: RKE2, K3s, existing Kubernetes, Docker and native Linux. This installs
their cluster networking dependency; it does not install a hidden cluster on
Docker/native application hosts or replace their application deployment adapters.
The plan declares the CNI binary/configuration host directories explicitly.
They must match the prepared container runtime on every node, including any
distribution-specific data-directory configuration; upstream chart defaults are
not assumed to fit every RKE2 or K3s target. These paths are immutable in patch
transitions.

## Bootstrap prerequisites

Provision three control-plane nodes, a stable API endpoint reachable without
cluster DNS/Service routing, and at least two separate Linux workers. Select
gateway nodes in two failure domains. Choose the pod pool, per-node mask and
MTU from the actual network design. Replace the documentation CIDR and fake
image digests in the example with reviewed private inputs.

This command begins after Kubernetes API/node bootstrap. For this standalone
lane, RKE2 must be prepared without a distribution-managed CNI or kube-proxy;
K3s similarly needs its default CNI, network-policy controller and kube-proxy
disabled before installation. The helper does not change host bootstrap or
migrate live distribution networking. Retain independent bootstrap material
and follow the distribution-specific procedure. See the upstream
[K3s prerequisites](https://docs.cilium.io/en/stable/installation/k3s/) and
[Cilium Helm installation contract](https://docs.cilium.io/en/stable/installation/k8s-install-helm/).

Preflight checks bind the context, verified HTTPS API, kube-system UID and
rendered Kubernetes release core. They check Linux architecture/kernel baseline,
control/worker counts, gateway failure domains, pod-pool capacity and overlap
with node/API Service addresses. Fresh installs allow nodes awaiting their first
CNI; completed installs and ordinary upgrades require node readiness. Node
status does not verify host modules, firewall rules, underlay routes or physical
network redundancy; those need independent checks before deployment.

After controller rollout, the installer waits up to 180 seconds for the
original Node inventory to report Ready. Host-network Cilium Pods can become
ready before kubelets observe the new CNI configuration. Node names and UIDs
are checked again after this bounded wait; a timeout or replacement leaves the
operation pending and cannot produce a verified installation.

Resource read-back preserves the reviewed fields while accounting for three
typed API representations observed with the pinned chart: omitted false mount
flags, omitted zero probe delay and integer resource quantities returned as
strings. Expected and observed defaults are both filled before comparison;
changing a false flag to true, a zero delay to nonzero or a resource amount
still fails verification. The real API admission test checks this independently
of scheduler, kubelet and Cilium readiness.

An existing detected CNI, Cilium Helm release, RKE2 `rke2-cilium` HelmChart or
leftover CiliumNode CRD state blocks fresh installation. The helper never removes
another CNI, disables a live kube-proxy, flushes packet filters or changes the
Kubernetes API Service. Existing distribution-managed CNI migration remains a
requirement for the full requested runtime support.

## Render a pinned package

Copy [the generic plan](../examples/cilium-installation.json) under `private/`.
The [1.19 example](../examples/cilium-installation-v119.json) pins the reviewed
1.19.10 images/chart and 1.4.1 CRDs for the newer lane. Replace all illustrative
target/network values and choose the actual CNI host directories for the
distribution before use. Installing the BackendTLSPolicy CRD from that complete
standard export does not add BackendTLSPolicy management to the route adapter.
Pin the local chart archive, Helm executable and Gateway API standard export by
SHA-256, plus every agent/operator/Envoy image digest. Use the independently
reachable bootstrap API. Company hosts, network values and credentials remain
private.

For a fresh installation that will serve TLSRoute, set `"tls_passthrough": true`
in the private plan and append the matching release's experimental TLSRoute
CRD to the standard export as a separate YAML document. Set
`gateway_crds_sha256` to the digest of that combined file. The installer
establishes all selected CRDs before starting Cilium. Patch upgrades preserve
the selected CRD inventory. See the [route prerequisites](CILIUM_GATEWAY.md#prepare-the-prerequisites).

```bash
python3 scripts/render_cilium_installation.py \
  --plan private/cilium-installation.json \
  --chart private/artifacts/cilium-1.18.3.tgz \
  --gateway-crds private/artifacts/gateway-api-standard-install.yaml \
  --helm private/tools/helm \
  --runtime rke2 \
  --output-dir private/cilium-1.18.3
```

The renderer verifies bounded archive contents and release metadata, then
captures chart/values bytes before local `helm template`. It performs no
dependency download, cluster lookup or live Helm release operation. Every
workload image must match the plan. Unexpected kinds, hooks, namespaces,
workload inventories and incomplete Gateway CRDs are refused.

Review privileged workloads, host mounts, RBAC, configuration, resource limits,
placement and CRDs before accepting the contract digest. Hashes bind bytes;
they do not replace signature, provenance or license review. Installation uses
server-side apply of captured manifests and creates no Helm release. Preserve
the complete previous package for lifecycle operations.

## Check, install and verify

```bash
python3 scripts/deploy_cilium_installation.py \
  --bundle-dir private/cilium-1.18.3 \
  --contract-sha256 '<REVIEWED_CONTRACT_SHA256>'
```

Default mode validates local inputs without constructing a client. Set the
plan's kubeconfig environment variable to the private configuration. Server
operations use a flattened temporary snapshot with static credentials so file
rotation cannot redirect later writes; dynamic credential plugins are refused.

Add `--check-server --output-dir private/cilium-check-001` for reads and server
dry-run. Add `--apply --output-dir private/cilium-install-001` for explicit
installation. Every server invocation requires a new private proof directory.
For a missing package-owned secrets Namespace, preview reports
`namespace_bootstrap_required` and checks the cluster/existing-namespace
resources while leaving `server_checked` false. A dry-run Namespace cannot
make later namespaced resources admissible. Explicit installation first
reserves its operation, creates the reviewed Namespace, resolves any lost
acknowledgment against that exact attempt, records its UID in the lifecycle
ledger, and then performs complete admission checks. Unexpected existing
Namespaces are refused. A replaced captured Namespace cannot become a verified
installation through an updated content hash.
The operation reserves its lifecycle record before applying resources,
establishes Gateway CRDs, applies Cilium and waits for rollouts. Read-back checks
verify pinned agents on every node, Envoy readiness on gateway nodes, operator
placement, resource identities and submitted resource fields.

The installed digest and resource UIDs are stored in an owned ConfigMap named
`<PACKAGE_NAME>-installation`. No unowned Namespace, Gateway CRD, role or CNI
workload is adopted. There is no force-conflicts or blanket pruning. This first
lane manages its own Gateway CRDs; adoption/migration of externally owned shared
Gateway CRDs is not implemented.

Use `--verify --output-dir private/cilium-verify-001` for read-only live checks.
Applying the same installed digest also verifies it without additional writes.
Bootstrap readiness is controller/node evidence, not proof of application
traffic, policy enforcement or availability.

## Patch upgrades and rollback

Render and review the next exact patch package. Bind the currently installed
package and its digest:

```bash
python3 scripts/deploy_cilium_installation.py \
  --bundle-dir private/cilium-next-patch \
  --contract-sha256 '<TARGET_CONTRACT_SHA256>' \
  --previous-bundle-dir private/cilium-current-patch \
  --previous-contract-sha256 '<CURRENT_CONTRACT_SHA256>' \
  --check-server \
  --output-dir private/cilium-upgrade-check-001
```

After review, replace `--check-server` with `--apply` and choose a new proof
directory. The previous digest and resource identities must match live state.
Patch operations cannot change runtime/target, bootstrap API, network pool/mask,
MTU, CNI host directories, placement, Gateway artifact or resource inventory. Ordinary upgrades require
the current configuration and pinned controller topology to match the previous
package. No implicit `--reuse-values` or IPAM migration occurs.

The target chart's image/CRD preflight workloads must have current-generation
readiness before core update. Shared Namespace/ConfigMap content is excluded
from temporary preflight so it cannot replace running configuration early.
Only package-owned temporary workloads/RBAC are removed, using API UID and
resourceVersion deletion preconditions, before core rollout. See the
[pinned upstream preflight procedure](https://github.com/cilium/cilium/blob/v1.18.3/Documentation/operations/upgrade.rst).

An intentional downgrade within this patch lane requires `--rollback` and both
package inputs. Inspect it locally or with `--check-server` before `--apply`.
No timeout triggers automatic rollback. A controlled manifest rollback does not
prove recovery of etcd, CRDs, IPAM allocations, BPF state or application data.
Keep independently restorable backups and bootstrap/PKI material outside the
platform and test restoration. Minor-version transitions require separate
release-specific preparation and implementation; this adapter refuses them.

## Interrupted operations

The private `cilium-proof.json` journal is durable before network activity and
records write attempts before sending them. The persistent operation record uses
resourceVersion comparison and an attempt identity. Lost reservation
acknowledgments are resolved by reading that exact attempt. Failed manifest
writes leave a pending operation and an unverified outcome; errors do not mean
that nothing changed.

Inspect the journal, operation record and live state. Confirm the original
process has stopped before recovery; the record does not kill or fence remote
processes. Pending operations may only resume the identical desired digest and
previous package with explicit `--resume`. Inspect with `--resume --check-server`,
then use `--resume --apply` and a new proof directory. Conflicting target digests
are refused. If the record already reached the stable target, use normal
verification or idempotent apply instead of pending-operation resume.

The helper never deletes the operation record to bypass an error, force-deletes
Pods or silently steals another manager's state. An unrecoverable pending
operation may require independent restoration; changing the desired digest is
not a replacement for recovery.

## Remaining acceptance

Tests cover local artifacts, real Helm exports, simulated install/upgrade state,
owner/UID guards, stale preflight rejection, lost acknowledgments, explicit resume
and patch rollback. A separate actual API case checks all resources from the
pinned 1.19.10 chart plus the 1.4.1 CRD export, then changes a mount flag and
requires read-back verification to reject it. It uses fresh loopback-only
API/etcd processes with explicit privileged admission for the CNI template;
the fixture's default admission mode is unchanged. There are no kubelets or
Cilium processes in that case, and zero scheduled/ready Cilium Pods is required.
Run it in a fresh bounded process with the pinned envtest binary cache:

```bash
sudo systemd-run --wait --pipe --collect \
  --unit="platform-cilium-admission-$(cat /proc/sys/kernel/random/uuid)" \
  --property=RuntimeMaxSec=240 --property=MemoryMax=2G \
  --property=TasksMax=512 --property=KillMode=control-group \
  --property=UMask=0077 --working-directory="$PWD" \
  /usr/bin/env CILIUM_INSTALL_TEST_ADMISSION=1 \
    ROOK_BOOTSTRAP_TEST_BIN=/absolute/private/cache/envtest/v1.35.0/extracted/controller-tools/envtest \
    CILIUM_INSTALL_TEST_V119_DIR=/absolute/private/cache/cilium/v1.19.10 \
    CILIUM_INSTALL_TEST_HELM=/absolute/private/cache/cilium-lifecycle/helm \
    python3 -B scripts/test_cilium_installation.py \
      CiliumInstallationTests.test_actual_v119_chart_resource_fields_survive_real_api_admission
```

The [five-VM Cilium case](K3S_VM_WORKERS.md#optional-five-node-cilium-datapath-and-gateway-case)
passed separately with actual workers, production installer verification,
cross-worker Pod/Service traffic, allowed/denied policies, control-plane quorum
outages/recovery and verified HTTPS serving on both workers. Its source-bound
receipt and independent cleanup audit cover that virtual case.
A separate run of the same fixture verified application Service DNS on both
workers after quorum recovery and again after CoreDNS replacement. Both Pods
resolved the original Service ClusterIP through `kube-dns` and fetched the
expected body by name. A separate audit matched all 550 Python source hashes
and confirmed removal of the original VM processes, unit and scratch scope.
The further [joined native CI/CD case](K3S_VM_WORKERS.md#optional-cilium-and-native-cicd-application-case)
also passes production CNI read-back after real Gerrit/Tekton/Argo CD activity
and original-application Gateway/policy checks. Its 1166.732-second acceptance
receipt binds a 345-Python-source snapshot and includes guest clock synchronization
and forced control-process recovery; the independent audit verifies original
unit, credential, native process and VM-scope cleanup. It adds workload
integration evidence for the local lab. Fleet deployment,
detailed BPF/Envoy inspection, redundant external entry,
node/network-path loss, load, independent restore and version-specific upgrade
drills remain required.
No complete requested stack/runtime combination is certified by this adapter.
