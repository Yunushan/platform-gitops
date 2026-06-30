# Troubleshooting

## Argo CD cannot access this repository

Check that you replaced `<THIS_REPO_URL>` at bootstrap time and configured repository credentials in Argo CD using a private secret flow.

Run the automated platform report:

```bash
make platform-status
```

It prints API VIP readiness, Argo CD pods/services, registered Argo CD Applications, ingress state, expected GUI URLs, and the next command when the GUI layer is not deployed yet.

Run the read-only production readiness gate after changes settle:

```bash
make platform-production-check
```

This chains repository validation, RKE2 verification, the platform status
report, and `make platform-app-health`. If it fails, read the first failing
section: app sync/health and pod readiness failures are GitOps/workload issues;
StorageClass and PVC failures point to Longhorn/storage provisioning or stuck
finalizers; GUI backend endpoint failures point to missing Ingress/IngressRoute
objects or Services with no ready pods; controller/client app VIP and HTTP
redirect failures usually point to MetalLB, Traefik, DNS, or client routing;
Argo CD or Woodpecker ClusterIP failures point to CNI, kube-proxy, firewalld, or
node-to-pod networking. The service-path section checks both the node host path
and short-lived diagnostic pods pinned to each RKE2 node, so Woodpecker agent
gRPC failures on only one or two nodes are reported directly.

For Argo CD repo-server/Redis timeouts, Woodpecker agent gRPC timeouts, or
node-specific ClusterIP service failures, run the explicit service-path repair
alias before rechecking health. The alias repairs CoreDNS/CNI service routing,
then refreshes Woodpecker agents and verifies the Woodpecker gRPC ClusterIP from every RKE2 node so CrashLoopBackOff agents do not wait on exponential backoff:

```bash
make platform-service-path-repair
make platform-argocd-service-repair
make platform-app-health
```

To require RKE2 node-originated app VIP self-probes as well:

```bash
PLATFORM_APP_HEALTH_NODE_INGRESS_STRICT=true make platform-app-health
```

To skip required StorageClass enforcement during a temporary non-Longhorn subset
debug run:

```bash
PLATFORM_APP_HEALTH_STORAGE_CLASSES=skip make platform-app-health
```

To skip only HTTP-to-HTTPS redirect enforcement during a temporary debug run:

```bash
PLATFORM_APP_HEALTH_HTTP_REDIRECT=false make platform-app-health
```

If a cluster intentionally deploys only part of the stack, override the app,
namespace, and GUI route lists together. For example:

```bash
PLATFORM_APP_HEALTH_REQUIRED_APPS="cert-manager trust-manager metallb traefik longhorn cloudnativepg forgejo woodpecker" \
PLATFORM_APP_HEALTH_NAMESPACES="argocd cert-manager cnpg-system forgejo woodpecker longhorn-system metallb-system traefik" \
PLATFORM_APP_HEALTH_GUI_APPS="argocd forgejo woodpecker" \
make platform-app-health
```

To bootstrap Argo CD without manually copying commands:

```bash
make platform-argocd
```

`make platform-argocd` also exposes Argo CD through a temporary bootstrap NodePort. The default browser URL is `https://<NODE_1_IP>:30443`. The NodePort probe is soft by default because some host firewalls or CNI/kube-proxy paths block direct NodePort access even though the final Traefik/MetalLB ingress will work. To expose an already-installed Argo CD instance again:

```bash
make platform-argocd-expose
```

To use a different bootstrap port:

```bash
PLATFORM_ARGOCD_BOOTSTRAP_NODEPORT_HTTPS=31443 make platform-argocd-expose
```

To require the temporary NodePort to pass from every node:

```bash
PLATFORM_ARGOCD_BOOTSTRAP_NODEPORT_VERIFY_MODE=strict make platform-argocd-expose
```

After Traefik and MetalLB provide the real ingress URL, remove the temporary NodePort exposure:

```bash
make platform-argocd-unexpose
```

If Argo CD bootstrap fails with `metadata.annotations: Too long` for `applicationsets.argoproj.io`, rerun `make platform-argocd` after updating to this version of the playbook. The bootstrap uses server-side apply so large Argo CD CRDs are not stored in the client-side `last-applied` annotation.

If the playbook is waiting at Argo CD rollout, it polls for up to 600 seconds by default and prints pod/event diagnostics plus a likely-cause summary on failure. To extend the wait for slow image pulls:

```bash
PLATFORM_ARGOCD_ROLLOUT_TIMEOUT=1200 make platform-argocd
```

After an Argo CD timeout, collect the live state again without changing the cluster:

```bash
make platform-argocd-diagnose
```

The diagnostic target prints pods, workloads, services, CRDs, images, pod events/details, recent logs, recent events, and registry reachability checks for the image registries detected in Argo CD pods.

If Argo CD applications stay `Unknown` and the controller logs show timeouts to
`argocd-repo-server:8081` or `argocd-redis:6379`, repair Argo CD's internal
service path:

```bash
make platform-argocd-service-repair
```

This creates headless internal services for the Argo CD repo-server and Redis,
points Argo CD at those services, restarts the Argo CD workloads, and refreshes
Longhorn and Forgejo. It is useful when ordinary ClusterIP routing is unhealthy
but pod-to-pod routing is still working.

If the controller then times out to a pod IP such as `10.42.x.x:8081`, the
cluster still has a pod-to-pod path problem. The repair target defaults to a
bootstrap node-local and host-network fallback for Argo CD's controller,
repo-server, and Redis so first deployment can continue while the wider CNI path
is investigated:

```bash
make platform-argocd-service-repair
```

To disable that node-local fallback and only create the headless services:

```bash
PLATFORM_ARGOCD_SERVICE_REPAIR_NODE_LOCAL=false make platform-argocd-service-repair
```

To keep node-local placement but avoid the host-network/direct-node-IP fallback:

```bash
PLATFORM_ARGOCD_SERVICE_REPAIR_HOST_NETWORK=false make platform-argocd-service-repair
```

If Forgejo is synced but stuck in `Pending` because the `longhorn-critical`
StorageClass does not exist and the Longhorn Argo CD application remains
`Unknown`, bootstrap Longhorn directly through the RKE2 Helm controller:

```bash
make platform-longhorn-bootstrap
```

This applies the premium Longhorn storage classes, installs the Longhorn Helm
chart, creates the default Longhorn data path on every node, configures a
schedulable default disk on each Longhorn node object, refreshes the Longhorn
and Forgejo Argo CD applications, and prints storage/PVC status. If the
`longhorn-critical` priority class already exists, the bootstrap adopts it for
Helm metadata instead of changing immutable fields. It is a first-deployment
recovery path for the storage chicken-and-egg case; after Argo CD and pod
networking are healthy, GitOps continues to own the desired Longhorn manifests.

If Longhorn pods show `ImagePullBackOff` for `docker.io/longhornio/*` with
`TLS handshake timeout` or `connection reset by peer`, the Longhorn chart is
installed but node image egress to Docker Hub is flaky. The bootstrap discovers
the Longhorn image set and pre-pulls it on every RKE2 node with retries before
waiting for workloads. For slow enterprise links, increase the per-image pull
timeout:

```bash
PLATFORM_LONGHORN_IMAGE_PULL_TIMEOUT=600 \
PLATFORM_LONGHORN_IMAGE_PULL_RETRIES=6 \
PLATFORM_LONGHORN_WAIT_TIMEOUT=2400 \
make platform-longhorn-bootstrap
```

To skip node pre-pulls and only rely on kubelet image pulls:

```bash
PLATFORM_LONGHORN_PREPULL_IMAGES=false make platform-longhorn-bootstrap
```

For a quick diagnostic failure instead:

```bash
PLATFORM_LONGHORN_IMAGE_PULL_FAST_FAIL=true make platform-longhorn-bootstrap
```

For production, configure a local registry mirror or preload the Longhorn images
on all RKE2 nodes.

If the Forgejo PVC is `Bound` but the Forgejo pod remains in `Init:*`, Longhorn
has provisioned storage and the next useful signal is the Forgejo pod's
init-container state, logs, PVC/PV mapping, and Longhorn volume attachment:

```bash
make platform-forgejo-diagnose
```

If the Forgejo PVC is stuck in `Terminating` during the first deployment, stop
before manually removing finalizers. The repair target diagnoses the state by
default, and only resets storage when explicitly allowed:

```bash
make platform-forgejo-storage-repair
PLATFORM_FORGEJO_RESET_STUCK_PVC=true make platform-forgejo-storage-repair
```

Use the reset flag only when Forgejo is still empty. It scales Forgejo down,
removes the stuck first-deploy PVC/PV state, optionally removes the old Longhorn
volume, and refreshes the Forgejo Argo CD application so the chart can recreate
clean storage.

If Forgejo events show `AttachVolume.Attach failed` with `node <name> not
found`, the pod cannot start because Longhorn has not registered that Kubernetes
node as a healthy Longhorn node yet, or the instance-manager/engine-image pods
for that node are still unhealthy. Check the Longhorn node objects and manager
logs:

```bash
kubectl api-resources --api-group=longhorn.io -o wide
kubectl get crd | grep 'longhorn\.io'
kubectl -n longhorn-system get nodes.longhorn.io instancemanagers.longhorn.io engineimages.longhorn.io -o wide
kubectl -n longhorn-system logs -l app=longhorn-manager --all-containers --tail=180
```

The Forgejo storage repair target also detects this attach failure, restarts the
Longhorn manager DaemonSet, waits for Longhorn node objects to match Kubernetes
nodes, and restarts the Forgejo pod so the volume attach is retried:

```bash
make platform-forgejo-storage-repair
```

For slow clusters:

```bash
PLATFORM_FORGEJO_VOLUME_ATTACH_REPAIR_TIMEOUT=900 make platform-forgejo-storage-repair
```

To fail faster after the repair retry and print attach diagnostics sooner:

```bash
PLATFORM_FORGEJO_POD_IP_WAIT_TIMEOUT=120 make platform-forgejo-storage-repair
```

If the PV has `longhorn.io/volume-scheduling-error: precheck new replica failed:
disks are unavailable` and `nodes.longhorn.io` shows empty `Spec.Disks`, the
repair target creates the default Longhorn data path, adds a schedulable
Longhorn disk on each node at `/var/lib/longhorn`, waits for Longhorn
`status.diskStatus` to report a ready schedulable disk, and only then retries
the Forgejo attach. To use a dedicated disk path:

```bash
PLATFORM_LONGHORN_DEFAULT_DISK_PATH=/mnt/longhorn make platform-forgejo-storage-repair
```

If the Forgejo pod is `1/1 Running` but
`https://<GIT_FQDN>` returns Traefik's plain `404 page not found`, the app VIP
and Traefik are reachable but no Forgejo router matched the hostname. Publish
and verify the explicit Forgejo Traefik route:

```bash
make platform-forgejo-ingress
```

To fail faster or wait longer while debugging VIP convergence:

```bash
PLATFORM_FORGEJO_INGRESS_VERIFY_TIMEOUT=60 make platform-forgejo-ingress
PLATFORM_FORGEJO_INGRESS_VERIFY_TIMEOUT=600 make platform-forgejo-ingress
```

If Longhorn manager logs repeat `the server could not find the requested
resource` for `nodes.longhorn.io`, `engines.longhorn.io`, or
`engineimages.longhorn.io`, restore the missing CRDs and restart Longhorn:

```bash
make platform-longhorn-crd-repair
```

If `kubectl apply` reports `PriorityClass "longhorn-critical" is invalid:
value: Forbidden: may not be changed in an update`, leave the existing
PriorityClass value alone. The bootstrap now only patches Helm ownership
metadata on an existing `longhorn-critical` PriorityClass and no longer tries to
update its immutable `value`.

If only the Argo CD HA Redis pods are failing, you can continue with a simpler bootstrap control plane while investigating Redis HA separately:

```bash
make platform-argocd-core
make platform-status
```

`make platform-argocd-core` removes stale Argo CD HA Redis bootstrap resources and applies the standard Argo CD install manifest. The default `make platform-argocd` starts with the HA manifest, but automatically falls back to core mode when the known HA Redis announce-service bootstrap failure is detected. Use `make platform-argocd-ha` for strict HA-only behavior with no automatic core fallback.

To register platform applications, provide the repository URL and explicitly allow GitOps app registration:

```bash
PLATFORM_REPO_URL=<THIS_REPO_URL> PLATFORM_APPLY_GITOPS=true make platform-argocd
```

The playbook checks the selected GitOps profile for unresolved placeholders before it registers applications. This prevents Argo CD from syncing incomplete domains, storage sizes, backup targets, or secret references.

For the premium profile, the unattended renderer can clear most first-deploy
placeholders automatically:

```bash
make platform-render-private-values
make platform-app-secrets
PLATFORM_PROFILE=premium-3node make platform-profile-check
```

Set object-storage values in ignored env files or your secret manager before
running `platform-app-secrets`: `LOKI_S3_ACCESS_KEY_ID`,
`LOKI_S3_SECRET_ACCESS_KEY`, `VELERO_CLOUD_CREDENTIALS`, or the shared
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.
For production verification, set
`PLATFORM_APP_SECRET_REQUIRE_OBJECT_STORAGE=true`; `platform-app-secrets` will
then fail immediately if the Loki or Velero credential secret is still missing.

If Argo CD controller logs show timeouts to the Kubernetes API service IP or an
Argo CD Redis ClusterIP, the pod-to-service path is unhealthy. First deployment
runs `platform-dns-repair` automatically by default; for a standalone repair
run:

```bash
make platform-dns-repair
```

For first bootstrap, use skip mode to register only deployable apps and print
the incomplete apps:

```bash
PLATFORM_GITOPS_PLACEHOLDER_MODE=skip-incomplete \
PLATFORM_REPO_URL=<THIS_REPO_URL> \
PLATFORM_APPLY_GITOPS=true \
make platform-argocd
```

Use strict mode after all private values are resolved:

```bash
PLATFORM_GITOPS_PLACEHOLDER_MODE=strict \
PLATFORM_REPO_URL=<THIS_REPO_URL> \
PLATFORM_APPLY_GITOPS=true \
make platform-argocd
```

## MetalLB does not assign addresses

Check that the address pool was customized from placeholders to your private network range in ignored or encrypted configuration.

Deploy or repair the full ingress foundation from `inventory/hosts.local.ini`:

```bash
make platform-ingress
make platform-status
```

This installs MetalLB and Traefik through the RKE2 Helm controller, applies the configured app VIP, publishes Argo CD at the effective Argo CD hostname, verifies it on 443, and removes the temporary Argo CD NodePort exposure.

Traefik is configured to redirect HTTP traffic on the app VIP to HTTPS. The ingress playbook also publishes a Traefik Middleware/IngressRoute that redirects direct app VIP browser requests such as `https://<APP_VIP>/` to the canonical Argo CD hostname. By default the target is the effective Argo CD hostname: `platform_argocd_host` when set, otherwise `argocd.<PLATFORM_DOMAIN>`. To override only the direct-IP redirect target:

```bash
PLATFORM_IP_REDIRECT_TARGET_HOST=<ARGOCD_FQDN> make platform-ingress
```

The target hostname must also have a real Argo CD route. If you change service FQDNs later, update the Argo CD ingress host and the redirect target together. Browsers may still show a certificate warning before redirecting from `https://<APP_VIP>/`, because production certificates normally cover DNS names, not private IP addresses.

To disable the direct app VIP to hostname redirect:

```bash
PLATFORM_IP_REDIRECT_ENABLED=false make platform-ingress
```

To shorten a MetalLB or Traefik wait while testing:

```bash
PLATFORM_INGRESS_ROLLOUT_TIMEOUT=180 make platform-ingress
```

To wait longer on slow chart/image pulls:

```bash
PLATFORM_INGRESS_ROLLOUT_TIMEOUT=1200 make platform-ingress
```

Traefik has its own rollout/VIP wait. If the playbook is waiting at `Wait for Traefik deployment`, use this instead of changing every ingress phase:

```bash
PLATFORM_TRAEFIK_ROLLOUT_TIMEOUT=180 make platform-ingress
PLATFORM_TRAEFIK_ROLLOUT_TIMEOUT=1200 make platform-ingress
```

If Traefik still times out, the final failure message includes a compact status summary with HelmChart/job state, pods, waiting reasons, images, and recent events. Use that summary first; the longer diagnostics printed above it contain full pod descriptions and logs.

The Traefik rollout poll is an instant readiness check, so the retry counter maps closely to `PLATFORM_TRAEFIK_ROLLOUT_TIMEOUT` and `PLATFORM_INGRESS_POLL_INTERVAL` without an extra hidden `kubectl rollout status` wait on every attempt.

If that summary shows `helm-install-platform-traefik` in `BackOff` or repeatedly `Running` with no Traefik deployment created, the Helm install job is failing before Traefik starts. Rerun `make platform-ingress` with the current playbook so stale Helm jobs are cleaned, schema-compatible chart values are applied, and pod DNS is verified against both MetalLB and Traefik chart repositories. If it still fails, read the `Helm install pod log tail` in the final failure summary; it normally shows the exact rejected value, DNS lookup error, or chart download problem.

To run only the Traefik chart-repository DNS repair:

```bash
make platform-dns-repair-traefik
```

Before creating the Traefik HelmChart, `platform-ingress` also runs a per-node chart repository check. It creates one short-lived pod per Kubernetes node so a node-specific failure such as `<POD_IP> -> <CLUSTER_DNS_SERVICE_IP>:53 i/o timeout` cannot slip through. Each pinned pod prints the Kubernetes DNS service IP probe, the live CoreDNS endpoint IP probes, CoreDNS endpoint placement by node, explicit `PLATFORM_NODE_DNS_SERVICE_OK` / `PLATFORM_NODE_COREDNS_ENDPOINT_OK` markers, and then retries Helm repository add/update before failing. The controller waits for all node checks to finish before printing diagnostics. If the check still fails, the playbook repairs the host CNI service path on every RKE2 node, including per-interface reverse-path filtering, RKE2 node-peer firewalld trust, and direct pod/CNI firewalld ACCEPT rules, refreshes kube-proxy/Cilium, ensures CoreDNS has HA placement with local service routing, and retries once before starting the Helm install job.

The checker treats Helm output such as `Unable to get an update` as unhealthy even when Helm exits with status `0`, because that usually means the repo path is still flaky and a later Helm install job may fail on the same node. If the DNS service IP probe fails but CoreDNS endpoint probes work, focus on kube-proxy or service NAT rules. If both service and endpoint probes fail from the same node, focus on Cilium pod routing, host firewall zones, VXLAN/Geneve, or node egress. If Cilium health shows host connectivity OK but remote endpoint HTTP timeouts, the node-to-node underlay is reachable but pod-to-pod L4 forwarding is still blocked or filtered. If the post-repair retry still fails, the final message includes failed-node Kubernetes diagnostics plus host network/firewalld/Cilium/kube-proxy, iptables, nft, and conntrack diagnostics for the affected node.

If the normal host service-path repair and retry still fail on a specific node, `platform-ingress` restarts `rke2-server` only on the failed node, waits for it to report Ready, waits for Cilium and kube-proxy on that node, and performs one final per-node DNS retry. This is enabled by default for HA clusters because one server restart is tolerated by the other two control-plane nodes. To disable that heavier recovery step:

```bash
PLATFORM_TRAEFIK_DNS_FAILED_NODE_RESTART=false make platform-ingress
```

To adjust how long the playbook waits for the restarted node:

```bash
PLATFORM_TRAEFIK_DNS_FAILED_NODE_RESTART_TIMEOUT=300 make platform-ingress
```

For a three-node HA control plane, the repair path targets three CoreDNS replicas with topology spread and preferred anti-affinity so every node can get a local DNS endpoint when the scheduler can place one. It also patches the CoreDNS service with `internalTrafficPolicy: Local`, so a pod uses the CoreDNS endpoint on its own node instead of randomly hitting a remote CoreDNS endpoint when cross-node pod DNS is flaky. RKE2's CoreDNS autoscaler can reconcile the Deployment back to two replicas, so the repair path temporarily scales that autoscaler to zero before enforcing the fixed HA CoreDNS placement. To override the Traefik per-node repair target replica count:

```bash
PLATFORM_TRAEFIK_DNS_COREDNS_REPLICAS=3 make platform-ingress
```

The default per-node check timeout is 300 seconds. To change it:

```bash
PLATFORM_TRAEFIK_DNS_CHECK_TIMEOUT=300 make platform-ingress
```

The post-repair per-node retry uses the same timeout by default. To make only the retry fail faster:

```bash
PLATFORM_TRAEFIK_DNS_RETRY_TIMEOUT=120 make platform-ingress
```

To tolerate short intermittent CoreDNS or chart-repository lookup failures during the per-node check:

```bash
PLATFORM_TRAEFIK_DNS_HELM_ATTEMPTS=5 PLATFORM_TRAEFIK_DNS_HELM_TIMEOUT=60 make platform-ingress
```

To disable the host service-path repair pass while collecting diagnostics:

```bash
PLATFORM_TRAEFIK_DNS_SERVICE_PATH_REPAIR=false make platform-ingress
```

If applying the app VIP fails with `failed calling webhook` or `context deadline exceeded` for `metallb-webhook-service`, the Kubernetes API server could not reach MetalLB's validating webhook yet. The ingress playbook now waits for the webhook service endpoints and runs a server-side dry-run of the MetalLB pool before creating the real resources. To wait longer for that webhook phase:

```bash
PLATFORM_METALLB_WEBHOOK_TIMEOUT=1200 make platform-ingress
```

Each webhook service-path and admission probe is bounded separately so a bad webhook path does not make every retry wait on Kubernetes' default admission timeout. The default is 5 seconds per probe:

```bash
PLATFORM_METALLB_WEBHOOK_PROBE_TIMEOUT=3 PLATFORM_METALLB_WEBHOOK_TIMEOUT=120 make platform-ingress
```

When the webhook check still fails, `platform-ingress` automatically restarts the MetalLB controller, refreshes kube-proxy, restarts Cilium, and retries the webhook dry-run before stopping. Disable that recovery path only when you want diagnostics without component restarts:

```bash
PLATFORM_METALLB_WEBHOOK_REPAIR=false make platform-ingress
```

If `helm-install-platform-metallb` or `helm-install-platform-traefik` stays `Running` for many minutes with restarts, rerun:

```bash
make platform-ingress
```

The ingress playbook cleans stale platform Helm install jobs before retrying by default and prints HelmChart/job/pod logs if CRDs still do not appear. To disable cleanup while debugging:

```bash
PLATFORM_INGRESS_CLEANUP_HELM_JOBS=false make platform-ingress
```

After Traefik receives the app VIP, `platform-ingress` verifies Argo CD through the effective Argo CD hostname using the app VIP. Before that check, the playbook enforces Traefik `externalTrafficPolicy: Local` and `internalTrafficPolicy: Local`, then verifies every RKE2 node has a ready local Traefik endpoint. It publishes Argo CD with both a standard Kubernetes Ingress and, when Traefik CRDs are available, a native Traefik `IngressRoute`. Node-originated VIP probes still run and print diagnostics, but they are advisory by default because MetalLB L2 VIP self-probes from Kubernetes nodes can fail even when real clients can reach the VIP. The hard gate is the Ansible controller/client path to the app VIP. If that check returns `http_code=404`, the VIP reached Traefik but no Argo CD router matched; review the printed Ingress and IngressRoute diagnostics. If it times out with `curl: (28)` or HTTP code `000`, the playbook automatically refreshes MetalLB speaker announcements, flushes the app VIP neighbor cache on the RKE2 nodes and Ansible controller, attempts a Windows host ARP flush when running from WSL, and retries before failing. A remaining timeout normally means app VIP L2/ARP, host firewall/routing, or client-to-VIP path rather than Argo CD itself. To shorten just this final verification while debugging:

```bash
PLATFORM_ARGOCD_INGRESS_VERIFY_TIMEOUT=120 make platform-ingress
```

To classify the live path without redeploying anything:

```bash
make platform-ingress-diagnose
```

The diagnose target checks the Traefik LoadBalancer service, MetalLB pool and L2Advertisement, Traefik and Argo CD endpoints, app VIP TCP reachability, direct Traefik NodePort reachability from the Ansible controller, and Windows/WSL ARP state when those tools are available. If direct NodePort works but `<APP_VIP>:443` times out, focus on MetalLB L2/ARP, same-VLAN reachability, duplicate VIP ownership, host firewall forwarding, or virtualization switch security such as MAC address changes and forged transmits. If one node's direct Traefik NodePort returns HTTP but other nodes accept TCP and then time out, the playbook treats that as a node-local backend path problem and repairs Argo CD server placement plus Traefik native Kubernetes service load balancing before retrying.

To disable the automatic MetalLB speaker and neighbor-cache repair pass:

```bash
PLATFORM_ARGOCD_INGRESS_VIP_REPAIR=false make platform-ingress
```

To make node-originated VIP probes a strict deployment gate:

```bash
PLATFORM_ARGOCD_INGRESS_NODE_STRICT=true make platform-ingress
```

If Helm logs show `lookup ... on ...:53: i/o timeout`, pod DNS cannot resolve external chart repositories through CoreDNS. The `platform-ingress` target runs DNS repair first. To run that step directly:

```bash
make platform-dns-repair
```

The repair excludes Kubernetes DNS service IPs from CoreDNS upstream candidates. If a node resolver points back to the cluster DNS service, forwarding CoreDNS to that address creates a DNS loop and pod lookups will time out. The playbook also tests discovered upstream candidates from inside a pod and configures CoreDNS only with candidates that resolve the chart repository from the cluster network.

If direct upstream DNS works from pods but Kubernetes DNS service lookups still time out, the problem is the Kubernetes DNS service path rather than the upstream resolver. The repair now applies the CNI service-path host prerequisites on all nodes, including reverse-path-filter sysctls, active-interface reverse-path filtering, Cilium VXLAN/Geneve firewalld ports, trusted pod CIDR and node IP firewalld sources, trusted Cilium/firewalld interfaces, and direct pod/CNI ACCEPT rules, then restarts kube-proxy when present, Cilium, and CoreDNS. To disable that bootstrap repair step:

```bash
PLATFORM_DNS_SERVICE_PATH_REPAIR=false make platform-ingress
```

The service-path repair is split into visible kube-proxy, Cilium, and CoreDNS tasks. If kube-proxy is delivered as static RKE2 pods instead of a DaemonSet, the playbook deletes those pods and waits for all three replacements to become Running before retrying DNS. Each rollout waits up to 120 seconds by default and polls every 5 seconds. To shorten that while troubleshooting:

```bash
PLATFORM_DNS_SERVICE_PATH_ROLLOUT_TIMEOUT=45 \
PLATFORM_DNS_SERVICE_PATH_POLL_INTERVAL=5 \
make platform-ingress
```

After those component restarts, the playbook enforces CoreDNS HA placement, patches the CoreDNS service with `internalTrafficPolicy: Local`, re-detects the current CoreDNS endpoint IPs, and reruns the service-path DNS probe before printing the final classification. This avoids diagnosing stale CoreDNS pod IPs after a rollout and avoids kube-proxy load-balancing DNS requests to remote CoreDNS endpoints when every node has a local CoreDNS pod. To override the generic DNS repair target replica count:

```bash
PLATFORM_DNS_COREDNS_REPLICAS=3 make platform-ingress
```

The static kube-proxy delete request is non-blocking and uses a 30-second Kubernetes API request timeout by default. To make that fail faster:

```bash
PLATFORM_DNS_KUBE_PROXY_DELETE_TIMEOUT=10 make platform-ingress
```

If the playbook says direct upstream DNS works but direct CoreDNS endpoint DNS fails, pod-to-pod overlay traffic is still broken. Rerun node preparation so firewalld trusts the pod CIDR, RKE2 node IPs, and Cilium interfaces on every node, and so active-interface reverse-path filtering is disabled:

```bash
make rke2-prepare
make platform-ingress
```

For non-default RKE2 pod CIDRs, override the trusted CIDR:

```bash
PLATFORM_DNS_POD_CIDRS="<RKE2_POD_CIDR>" make platform-ingress
```

To force explicit CoreDNS upstreams:

```bash
PLATFORM_DNS_UPSTREAMS="DNS_SERVER_1 DNS_SERVER_2" make platform-ingress
```

To shorten or extend the DNS test window:

```bash
PLATFORM_DNS_CHECK_TIMEOUT=60 make platform-ingress
```

To make each in-pod DNS/HTTPS probe fail faster while keeping the outer check window:

```bash
PLATFORM_DNS_PROBE_TIMEOUT=10 PLATFORM_DNS_CHECK_TIMEOUT=60 make platform-ingress
```

If a previous interrupted run left a stale DNS check Job and Kubernetes is slow to delete it, the playbook waits up to 30 seconds before recreating the Job. To fail faster while debugging:

```bash
PLATFORM_DNS_JOB_CLEANUP_TIMEOUT=10 make platform-ingress
```

Helm repository add/update uses a separate retry and timeout because chart repository access can be slower or briefly flakier than DNS probes. The default is 3 attempts and 90 seconds per Helm command:

```bash
PLATFORM_DNS_HELM_ATTEMPTS=5 PLATFORM_DNS_HELM_TIMEOUT=60 make platform-ingress
```

If you only want to increase the per-command wait without adding attempts:

```bash
PLATFORM_DNS_HELM_TIMEOUT=180 make platform-ingress
```

If the DNS check resolves a public IPv6 address and then fails with `network is unreachable`, keep the default IPv4-only DNS repair mode enabled. The repair suppresses external AAAA answers through CoreDNS so in-cluster Helm jobs use IPv4. Disable it only on networks with working IPv6 egress:

```bash
PLATFORM_DNS_IPV4_ONLY=false make platform-ingress
```

If resolution succeeds but Helm repository add/update times out, the problem is pod egress rather than CoreDNS, or the Helm timeout is too short for your network path. Check firewall, NAT/masquerade, proxy policy, TLS inspection, or use an internal chart mirror reachable from pods. The direct repository HTTPS index probe is diagnostic only; the playbook no longer fails before the Helm repository check just because `curl` or `wget` behaves differently from the pod resolver.

When using internal chart mirrors, override the platform chart repos:

```bash
PLATFORM_METALLB_CHART_REPO="https://<INTERNAL_HELM_MIRROR>/metallb" \
PLATFORM_TRAEFIK_CHART_REPO="https://<INTERNAL_HELM_MIRROR>/traefik" \
make platform-ingress
```

## API VIP or API DNS does not answer

If all RKE2 nodes are `Ready` but the VIP or API DNS fails:

```bash
curl -k https://<VIP_ADDRESS>:6443/readyz
curl -k https://<VIP_DNS_NAME>:6443/readyz
```

deploy kube-vip and write controller host resolution:

```bash
make rke2-api-vip
make rke2-controller-hosts
```

Then retest the same `curl` commands. `make rke2-api-vip` deploys kube-vip as a control-plane DaemonSet in ARP mode. The default image is pulled from `ghcr.io`, so include that endpoint in registry/proxy/mirror rules.

If plain `curl` returns `401 Unauthorized`, the VIP is already reaching the Kubernetes API server. Use an authenticated kubeconfig check to verify readiness:

```bash
kubectl --kubeconfig <PATH_TO_PRIVATE_KUBECONFIG> --server=https://<VIP_ADDRESS>:6443 get --raw=/readyz
```

If kube-vip pods enter `CrashLoopBackOff` while the image is already present, check the pod logs. On SELinux-enforcing enterprise Linux nodes, kube-vip may need IPVS modules loaded on the host before the container starts. `make rke2-api-vip` loads and persists `ip_vs` and `ip_vs_rr` for this reason.

If logs show an invalid CIDR like `invalid CIDR address: <VIP>32`, use the default `kube_vip_subnet=/32` value. The slash is required by kube-vip when building the VIP CIDR.

## Ansible or host resolution fails

Run:

```bash
make rke2-preflight
```

This checks SSH, passwordless sudo, required VIP/domain variables, and node `/etc/hosts` entries.

If the WSL/controller machine cannot resolve `api.platform.local` or platform app names, also update the controller:

```bash
ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/preflight.yml \
  -e manage_controller_hosts=true
```

## RKE2 install appears stuck

Use the Ansible install playbook instead of a long ad-hoc shell command:

```bash
make rke2-install
```

The playbook runs the package installer asynchronously, polls progress, starts `rke2-server` without blocking Ansible output, verifies service readiness, and prints diagnostics if install or startup exceeds the timeout.
The `rke2-install` target also runs preflight and node preparation first, including Rocky/RHEL 10 `kernel-modules-extra`, kernel modules, swap disablement, CNI sysctls, active-interface reverse-path filtering, Cilium overlay firewalld ports, trusted pod CIDR/node IP/Cilium firewalld handling, direct pod/CNI firewalld ACCEPT rules, and NetworkManager CNI handling.

Collect current process, service, journal, disk, and memory diagnostics:

```bash
make rke2-status
```

If only one host appears stuck, limit the check to that node:

```bash
make rke2-ping HOST=node-1
make rke2-status HOST=node-1
```

If you interrupted `make rke2-install`, clean stale installer processes before rerunning it:

```bash
make rke2-cleanup-installers HOST=node-1
```

If logs show `no route to host` for `:9345`, run node preparation again to open firewalld ports, then test node-to-node reachability:

```bash
make rke2-prepare
make rke2-network-check
```

If node-1 repeatedly logs `Pod for etcd not synced (pod sandbox not found)` and `127.0.0.1:2379: connect: connection refused`, the first server did not get embedded etcd running. First rerun the prepared install path:

```bash
RKE2_JOIN_ENDPOINT=<NODE_1_IP> make rke2-install
```

If this is still a failed partial bootstrap and there is no production cluster data yet, reset the failed bootstrap state and reinstall:

```bash
CONFIRM_RKE2_RESET=YES_I_UNDERSTAND RKE2_RESET_CONTROLLER_TOKEN=true make rke2-reset
RKE2_JOIN_ENDPOINT=<NODE_1_IP> make rke2-install
```

The install and recovery playbooks print kernel module, swap, sysctl, `kernel-modules-extra`, CRI, containerd, listener, process, disk, and memory diagnostics for this failure pattern. You can collect the same diagnostics directly:

```bash
make rke2-diagnose HOST=node-1
```

If diagnostics show `net/http: TLS handshake timeout` while pulling images such as `rancher/hardened-etcd`, `rancher/hardened-kubernetes`, or `rancher/rke2-cloud-provider`, the first server is blocked by registry egress, not local etcd configuration. Check the node-to-registry path:

```bash
make rke2-registry-check
```

When only one node fails after a network change, retest that node directly:

```bash
make rke2-registry-check HOST=node-2
make rke2-registry-check HOST=node-3
```

Fix firewall, proxy, DNS, MTU, TLS inspection, or internet egress from all three nodes to Docker Hub. For enterprise environments, prefer an internal registry mirror or airgap image flow, then set `rke2_registry_check_urls` to the mirror endpoints. Disable the check only after the mirror is configured:

```bash
RKE2_REGISTRY_CHECK_ENABLED=false make rke2-install
```

If nodes are registered but remain `NotReady` and Cilium pods show `Init:ImagePullBackOff`, check the Cilium pod events and image names. Depending on the chart image settings, the required registry may include `quay.io` as well as Docker Hub:

```bash
ansible -i inventory/hosts.local.ini node-1 -b -m shell -a '
K=/var/lib/rancher/rke2/bin/kubectl
C=/etc/rancher/rke2/rke2.yaml
$K --kubeconfig "$C" -n kube-system get ds cilium -o jsonpath="{range .spec.template.spec.initContainers[*]}init:{.name}={.image}{\"\\n\"}{end}{range .spec.template.spec.containers[*]}container:{.name}={.image}{\"\\n\"}{end}"
$K --kubeconfig "$C" -n kube-system describe pod -l k8s-app=cilium | sed -n "/Events:/,\$p"
'
```

If the nodes must use an HTTP proxy for internet access, provide proxy settings through ignored local inventory or private environment variables:

```bash
RKE2_HTTP_PROXY=http://proxy.example.com:8080 \
RKE2_HTTPS_PROXY=http://proxy.example.com:8080 \
RKE2_NO_PROXY=<LOOPBACK>,localhost,<RFC1918_CIDRS>,<NODE_1_IP>,<NODE_2_IP>,<NODE_3_IP>,<API_VIP>,api.platform.local \
make rke2-registry-check
```

When install runs with these variables, the playbook writes `/etc/default/rke2-server` so RKE2, embedded containerd, kubelet, control-plane pods, etcd, and kube-proxy receive the proxy configuration.

For interrupted bootstrap, token mismatch, stale process, or node join recovery, use the automated safe recovery flow:

```bash
make rke2-recover
```

This does not delete `/var/lib/rancher/rke2` cluster data. It reuses the existing first-server token, repairs config, opens firewalld ports, trusts the pod CIDR, node IPs, and Cilium interfaces, restarts services in the correct order, and waits for all three nodes to report Ready.

Recovery defaults are intentionally short: 300 seconds for service/API stages and 600 seconds for node readiness. On failure, the playbook prints service status, RKE2 journals, listeners, process state, resources, nodes, pods, and events for the failed stage.

Verify the cluster after recovery:

```bash
make rke2-verify
```

Collect focused diagnostics for a failed node:

```bash
make rke2-diagnose HOST=node-1
```

If the first server never became healthy and diagnostics show embedded etcd stuck in authentication handshake failures, use the guarded destructive reset for a failed bootstrap:

```bash
CONFIRM_RKE2_RESET=YES_I_UNDERSTAND make rke2-reset
make rke2-prepare
RKE2_JOIN_ENDPOINT=<NODE_1_IP> make rke2-install
```

This deletes RKE2 cluster state on the selected nodes. Use it only before production data exists or after restoring from backup.

If the network or image pulls are slow, extend the timeouts:

```bash
RKE2_INSTALL_TIMEOUT=1800 RKE2_START_TIMEOUT=1200 make rke2-install
```

If logs show image pull failures such as `image ... not found`, pin a known-good RKE2 version:

```bash
RKE2_VERSION='v1.35.4+rke2r1' make rke2-install
```

You can also use:

```bash
ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/install-rke2.yml \
  -e rke2_version='v1.35.4+rke2r1'
```

## CI cannot push images

Check Harbor robot account permissions. Do not commit robot account credentials.

## Secret scanner fails

Replace real values with placeholders or move them to ignored local files or encrypted secret workflows.
