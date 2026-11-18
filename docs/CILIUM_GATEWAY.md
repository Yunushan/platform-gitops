# Optional Cilium Gateway API routes

Status: implementation in progress. This package adds an explicit Gateway API
route installation and verification path to the [multi-runtime expansion](MULTI_RUNTIME_STACKS.md).
Offline checks and disposable-cluster coverage are recorded separately from
complete ingress/HA acceptance. Private company deployments remain outside
these laboratory checks.

## Architecture and current boundary

One dedicated GatewayClass and a shared Gateway expose exact hostnames for HTTP,
gRPC and TLS passthrough. Cilium's separate Envoy DaemonSet serves the
host-network listeners on explicitly selected nodes. The preflight requires two ready serving nodes in
two declared failure domains, with a current pinned Cilium and Envoy pod on
each. Two current Cilium operators must also span nodes and failure domains.

The compatibility lanes are Cilium **1.18 patches / Gateway API 1.2.0** and
**1.19 patches / Gateway API 1.4.1**. Tests retain the legacy source/schema and
installation checks and add actual 1.19.10 chart rendering with the 1.4.1
standard export. Version pairs are explicit; this adapter refuses a mixed
pair. The [1.19 prerequisites](https://github.com/cilium/cilium/blob/v1.19.10/Documentation/network/servicemesh/gateway-api/installation.rst)
and the pinned
[Cilium Gateway API documentation](https://github.com/cilium/cilium/blob/v1.18.3/Documentation/network/servicemesh/gateway-api/gateway-api.rst)
and [installation prerequisites](https://github.com/cilium/cilium/blob/v1.18.3/Documentation/network/servicemesh/gateway-api/installation.rst)
describe the respective upstream contracts. Patch selection still requires
deployment-specific provenance and security review.

All five application-host runtime labels are accepted: RKE2, K3s, existing
Kubernetes, Docker and native Linux. Gateway API remains on the explicitly
selected Kubernetes target. Docker/native hosting does not become a Kubernetes
networking installation. Those modes still need application/backend integration
with that target before the complete requested stack can be accepted.

The output includes a Cilium Helm **values fragment** with the required flags,
image overrides, serving selector and operator placement. It is input to a
separately reviewed CNI installation or upgrade, not a complete CNI values file.
The [owned CNI lifecycle adapter](CILIUM_INSTALLATION.md) now supplies an opt-in
fresh install and patch transition path. Existing distribution-managed CNI
migration remains separate required work.
The deployment command applies its GatewayClass, Gateway, HTTPRoutes, GRPCRoutes,
TLSRoutes and backend CiliumNetworkPolicies. Distribution-aware CNI migration,
independent bootstrap, minor upgrades and live upgrade/rollback acceptance
remain requirements for the full foundation.

## Prepare the prerequisites

Copy [the generic example](../examples/cilium-gateway.json) into ignored private
storage. Replace its illustrative image hashes, context, cluster UID, node
selector, namespace, hostnames and backend declarations. The plan contains no
credential values. Keep real hosts, inventories, TLS material and evidence in
private storage.

Provision the reviewed Cilium release with Gateway API, L7 proxy, kube-proxy
replacement, separate Envoy, host-network listeners, ALPN and backend
application-protocol support enabled. Proxy protocol must remain disabled for
this package. The runtime preflight checks the matching ConfigMap settings,
current DaemonSet/Deployment generations, pinned images, pod owners/readiness
and failure-domain placement. It does not prove that every agent has loaded
the configuration into its effective datapath; live probes remain required.

Preinstall the five standard Gateway API CRDs from the reviewed 1.2.0 bundle,
or the six standard CRDs from 1.4.1 (including BackendTLSPolicy v1),
and Cilium's CiliumNetworkPolicy CRD. For passthrough, also install the matching
release's experimental TLSRoute CRD serving `v1alpha2` before Cilium starts.
The command checks Established conditions,
served versions and Gateway API release annotations. It does not install,
replace, adopt or delete cluster networking CRDs.

Prepare a dedicated Gateway namespace. For HTTP and gRPC routes, provision its
`kubernetes.io/tls` Secret with a trusted certificate covering all terminating
hostnames. Preflight reads the Secret's type. For passthrough, provision the
certificate on the application TLS endpoint. Verify client trust and SNI with
live HTTPS requests for both listener types.

Label each permitted application namespace with
`platform.gitops/gateway-access=<GATEWAY_NAME>`. Only labeled namespaces may
attach routes. Backends must be existing selector-based Services with the
exact declared Service port, numeric Pod target port and ready EndpointSlice.
EndpointSlice ownership must match the current Service UID; a matching label on
an unrelated slice does not pass.
The command changes neither Services nor namespace labels.

`protocol: http` creates an HTTPRoute to an existing plaintext HTTP backend.
`protocol: h2c` creates a GRPCRoute and requires the Service port's
`appProtocol: kubernetes.io/h2c`. Set `https_port` and `tls_secret` in the plan
when either terminating protocol is in use. Check the actual serving mode
before routing an Argo CD Service to a plaintext listener.

`protocol: tls` creates a TLSRoute. Set `tls_passthrough_port` to an unprivileged
host port. Use distinct ports when both listeners are present. A passthrough-only
plan omits `https_port` and `tls_secret`. This Gerrit example belongs in the
plan's `routes` array alongside the reviewed port and cluster settings.

```json
{
  "name": "review",
  "namespace": "gerrit",
  "hostname": "review.example.invalid",
  "protocol": "tls",
  "service": "gerrit-router",
  "port": 443,
  "target_port": 8443,
  "pod_selector": {"app.kubernetes.io/name": "gerrit-router"}
}
```

Envoy matches the TLS ClientHello SNI and forwards the original encrypted stream
to the Gerrit HAProxy Service. Gerrit serves the peer certificate through both
proxies. Route SSH through the separate [Gerrit router](GERRIT_ROUTING.md).
See Cilium's [TLSRoute prerequisites](https://github.com/cilium/cilium/blob/v1.19.10/Documentation/network/servicemesh/gateway-api/installation.rst).

The backend policy permits Cilium's `ingress` identity only to the declared
selected Pods and TCP target port. It adds no egress rule or namespace-wide
allow. Cilium policies are additive: other policies can grant additional
access. Preserve the existing namespace isolation and verify both allowed and
denied traffic. Client-to-ingress restrictions and external firewall rules need
their own explicit policy; this package does not create a cluster-wide default
deny on the shared ingress identity.

### Configure bundled Cilium on a fresh RKE2 cluster

For RKE2's bundled Cilium, add `--rke2-bundled` to the render command
below. The additional `rke2-cilium-config.yaml` is a HelmChartConfig for
the distribution chart in `kube-system`. It uses the plan's pinned
images, serving-node selector and operator placement and points Cilium at
RKE2's local API listener for bootstrap. This fresh-cluster profile uses
Cilium's eBPF service and HostPort handling with CNI chaining disabled.

Choose Cilium 1.19.6 or later in the reviewed 1.19 lane and Gateway API
1.4.1. In the disposable RKE2 v1.36.2+rke2r1 test, bundled Cilium 1.19.4
accepted the route but reported `AddressNotAssigned` for the host-network
Gateway. RKE2 v1.36.4+rke2r1 includes the Cilium 1.19.6 fix.
[Cilium backport](https://github.com/cilium/cilium/discussions/47261) ·
[RKE2 package versions](https://github.com/rancher/rke2/releases/tag/v1.36.4%2Brke2r1)

Before the first server starts, prepare the same network settings on
all servers with `cni: cilium` and `disable-kube-proxy: true`. Stage the
reviewed HelmChartConfig in the first server's
`/var/lib/rancher/rke2/server/manifests/`. Apply the reviewed Gateway API
CRDs as soon as the API becomes available. Make the exact digest-pinned
images available on every node that runs Cilium, including Envoy and the
operator. Label the serving nodes before deploying the route package.
[RKE2 Cilium settings](https://docs.rke2.io/networking/basic_network_options) ·
[Cilium 1.19.6 Gateway prerequisites](https://github.com/cilium/cilium/blob/v1.19.6/Documentation/network/servicemesh/gateway-api/installation.rst)

Keep the HelmChartConfig with the private cluster bootstrap inputs.
Changing a running cluster's CNI or kube-proxy mode requires its own
migration procedure.

An offline four-VM lab applied this configuration on RKE2 v1.36.4+rke2r1
with bundled Cilium 1.19.6, Gateway API 1.4.1 and separate Envoy. The route
installer verified two serving nodes, two operator placements and current
Gateway and listener programming. Both Envoy nodes served the exact backend
response using the fixture CA and hostname. A single HAProxy entry endpoint
continued serving verified HTTPS while one Envoy VM was paused and after it
resumed. The route installer verified the same package again after recovery.

A subsequent six-VM drill installed the [native entry pair](GATEWAY_EDGE_PAIR.md)
on two separate HAProxy/Keepalived guests in front of the same RKE2 Gateway
profile. Pausing the entry VM that owned the VIP moved it to the peer, which
returned the expected TLS-verified response after 3.651 seconds. Both entry
VMs resumed service with their original daemon instances. A separate Envoy VM
pause caused one failed request among 14 samples. The final 12 requests
succeeded consecutively starting 9.187 seconds after the pause. After the
Envoy VM returned, both listeners and the original route passed verification.
The experiment used an isolated virtual network. Physical network partitions
and application failback still require their own drills.

## Redundant external entry

Provide an independently resilient entry point with at least two healthy
serving-node destinations, TCP/TLS pass-through to the selected listener port,
and protocol-aware health checks. TLS ends at Envoy for HTTP and gRPC routes
and at the application for TLSRoute. Preserve SNI and
HTTP/2; do not add a PROXY header when Cilium is configured to reject it.
Firewall and routing must allow those entry nodes to reach the selected
serving nodes and backends over the reviewed paths.

No single LoadBalancer address, DNS record, two ready Pods or programmed
Gateway condition establishes external-entry HA. The adapter currently does
not create the redundant external VIP/routers. Node-loss and network-path
drills must verify that architecture. Git SSH and application-specific Git
write routing remain separate [HAProxy/Gerrit requirements](GERRIT_ROUTING.md).

The shared Gateway owns the host-network ports declared in the plan. Another
Gateway declaring any of those ports blocks preflight, which requires Gateway
list access across namespaces. Namespace and workload permissions must prevent
unreviewed routes from attaching to the selected Gateway.

## Render, inspect and apply

```bash
python3 scripts/render_cilium_gateway.py \
  --plan private/cilium-gateway.json \
  --runtime rke2 \
  --output-dir private/rendered-cilium-gateway

python3 scripts/deploy_cilium_gateway.py \
  --bundle-dir private/rendered-cilium-gateway \
  --contract-sha256 '<REVIEWED_CONTRACT_SHA256>'
```

Rendering requires a new output directory under `private/`. Review every
manifest and the contract digest in `render-status.json`. Default deployment
mode checks local inputs only, reads no kubeconfig and constructs no client.
The loader regenerates the finite resource set from the validated plan and
compares it to the captured package; a modified hash cannot turn unrelated
resources into approved route content.

Set the plan's kubeconfig environment variable to the private configuration for
the reviewed context. Before server operations, the helper flattens it into a
private temporary snapshot and rejects dynamic credential plugins and remaining
certificate file references. All checks and writes use that same snapshot, so
rotating the original file cannot redirect an already reviewed operation.
Only static scoped tokens or embedded client certificates are accepted.
`--check-server --output-dir private/gateway-check-001`
authorizes prerequisite reads and server dry-run. `--apply` authorizes the
resource writes and a bounded programming wait. `--verify` checks current
resources without apply or dry-run. Each server invocation requires a new
private proof directory, created before contacting the cluster.

Existing route resources must carry this package's ownership label. The helper
does not adopt unowned resources, force field conflicts, restart the CNI,
blanket-sync applications, change the Kubernetes API Service or prune resources.
Apply uses verified in-memory manifest bytes even if files change during a
server check. Gateway programming requires current-generation Accepted and
Programmed conditions, listener reference resolution, the expected attached
route count, and current Accepted/ResolvedRefs conditions from the selected
controller and parent for every route. Live resource specs must still match
the generated routes and policies; stale green conditions do not hide drift.

## Recovery and acceptance

Backend Services may use a numeric target port or a Kubernetes named target
port. The route plan still declares the reviewed numeric `target_port` used
by its backend policy. For a named target, preflight binds each ready
EndpointSlice to the original Service UID and selected Service-port name, then
reads each referenced original ready Pod and resolves that container-port name.
This follows the pinned Kubernetes 1.35
[ServicePort target contract](https://github.com/kubernetes/api/blob/v0.35.0/core/v1/types.go#L5826-L5835)
and [EndpointPort name contract](https://github.com/kubernetes/api/blob/v0.35.0/discovery/v1/types.go#L164-L182).
The Pod UID, namespace, selector and numeric container port must agree. Mixed
ready endpoint ports, replaced Pods and unresolved references are refused before
route writes; a matching endpoint in one slice cannot hide a different port in
another ready slice.

The optional [HAProxy/Keepalived external entry pair](GATEWAY_EDGE.md) binds a
separate reviewed package to this Gateway contract. It adds TLS pass-through,
per-hostname backend checks, client CIDRs and guarded Kubernetes peer rollout.
It does not establish actual VIP failover or replace the live acceptance below.

The private `gateway-proof.json` records contract/runtime, operation phases,
serving-node UIDs and failure-domain counts. No Secret contents or raw server
diagnostics are printed. A failed apply or programming wait leaves the journal
and any applied resources for inspection. It does not claim rollback succeeded
or automatically remove working routes.
The journal records an apply attempt before sending it. A lost acknowledgment
retains an unverified write outcome rather than claiming that nothing changed
or retrying the write blindly. Server-side apply across the resource inventory
is not a transaction; partial application must be inspected explicitly.

For a partial failure, inspect the last phase and current controller diagnostics,
then render a separately reviewed package or retry with a new proof directory.
Changing a plan removes no old resources automatically. Route retirement and
ownership transfer need an explicit reviewed operation. Preserve the previous
package and CNI recovery material outside the platform before upgrades.

A separate accepted [five-VM case](K3S_VM_WORKERS.md#optional-five-node-cilium-datapath-and-gateway-case)
now exercises the actual route installer, HTTP through verified TLS/SNI on both
serving workers and backend isolation positive/negative traffic. The original
Pods remain healthy, and policy denials are rechecked after Gateway access.
That case uses synthetic failure-domain labels and one API proxy; it does not
prove physical HA or redundant external entry. A further accepted joined case
uses actual native Gerrit polling/voting, real Tekton worker tests and native
Argo CD reconciliation before routing the original Git-tested application.
Its named Service target port resolves against original ready EndpointSlice/Pod
identities. HTTPS on both workers and denied direct Service/Pod traffic pass
without changing the original workload specs, Pod UIDs or container IDs. The
1166.732-second receipt binds a 345-Python-source snapshot, includes actual
guest clock synchronization and forced control-process recovery, and has an independent
unit/credential/process/scope cleanup audit; see the
[joined runtime case](K3S_VM_WORKERS.md#optional-cilium-and-native-cicd-application-case).
The [mixed-listener case](K3S_VM_WORKERS.md#optional-five-node-cilium-datapath-and-gateway-case)
verified backend certificate preservation through both Cilium 1.19.10 serving
workers. A separate API admission check applied and read back a passthrough-only
plan against the pinned Gateway API 1.2.0 CRDs. The
[joined Gerrit test](GERRIT_ROUTING.md#verify-https-and-ssh-failover-with-cilium)
verified both Envoy listeners returned the selected Gerrit certificate and exact
Git review refs before and after stopping the preferred Pod.
Acceptance still needs gRPC,
external-entry traffic, detailed agent/Envoy inspection, serving-node/path/operator
loss, CNI/CRD and application upgrades, load, backup and restoration.
`gateway_programmed` records
controller evidence only; `ha_verified` and `full_stack_verified` remain false.
