# Optional TLS application gateway for Valkey

Status: packages for all five hosting formats and a real native protocol component
test. The test uses Valkey 8.1.11, authenticated Sentinel, mTLS and HAProxy 3.2.26.
**Full Harbor application compatibility, Kubernetes worker deployment, physical HA and
complete requested stacks remain unverified.** Existing profiles and live Harbor
values are unchanged.

The pinned Harbor cache and legacy-pool constructors now have a separate
[real client component test](HARBOR_CACHE_CLIENTS.md), including TLS/authentication
refusal and original-process reconnect after promotion. That evidence does not
qualify Harbor core, jobservice or registry as deployed applications.

The separate [registry daemon test](HARBOR_REGISTRY_RUNTIME.md) exercises two
actual registry processes, authenticated OCI traffic, cache promotion and
filesystem-backed metadata-cache fallback. Full Harbor integration and
independent shared-storage/physical HA still require verification.

A separate [actual Harbor service test](HARBOR_SERVICE_RUNTIME.md) now passes
release SQL migration, TLS PostgreSQL connections, authenticated project APIs
and real dry-run jobs through original core/jobservice processes across cache
promotion. It does not qualify database promotion, service replica failover,
independent storage or a full Harbor deployment.

The strict [Valkey profile](VALKEY_HA.md) requires mTLS and separate scoped server
and discovery credentials. Inspection of the pinned Harbor 2.15.1
[cache parser](https://github.com/goharbor/harbor/blob/v2.15.1/src/lib/cache/redis/util.go)
and [legacy pool builder](https://github.com/goharbor/harbor/blob/v2.15.1/src/lib/redis/pool.go)
shows different TLS/Sentinel credential handling. A chart TLS switch alone is
insufficient evidence for that strict profile. This adapter supplies a separate
direct TLS endpoint without disabling upstream client certificates or Sentinel
authentication. Real Harbor core/jobservice/registry tests are still required.

## Protocol and selection

Applications connect using verified TLS on **6381**, then authenticate using
their own backend ACL credentials. HAProxy forwards the Redis stream through
verified mTLS to the chosen server on 6380. It does not replace application
credentials with an administrator identity or provide anonymous cache access.

The read-only checker uses the original pinned mTLS identity and discovery ACL
to query all three Sentinels on 26380. It requires two original Sentinel IDs
agreeing on an approved primary and configuration epoch, matching master metadata
and usable quorum. It checks that original server's invocation/version, primary
role, approved connected replicas/lag, AOF status and no-eviction memory policy.
It never promotes a server, changes configuration or writes data.

HAProxy starts every backend down, verifies its data connection's CA and DNS name,
and requires successful current checks from three loopback-only health listeners.
Marked-down backends close existing sessions. Missing quorum, changed identities,
stale replicas, bad persistence, TLS/authentication failures or a stopped checker
fail closed. No cached successful discovery state survives a checker failure.

## Private preparation

Copy [the example](../examples/valkey-gateway.json) into ignored private storage
and replace all placeholders. Use an existing reviewed Valkey package plus its
original [runtime scope](VALKEY_RUNTIME.md), including contract hash, member
addresses, aliases, server/Sentinel IDs and TLS leaf pins. The gateway namespace
must already be authorized in the cache plan's `network.client_namespaces`.
Declare application ingress namespaces separately. Do not silently rewrite an
existing cache contract to add access.

Each backend certificate needs a distinct approved DNS SAN for `tls_names`, plus
the original observer's required name/address. The frontend certificate must
cover the application endpoint's DNS name. Independently stage:

```text
ca.pem              original observer CA
client_cert.pem     original observer mTLS certificate
client_key.pem      original observer private client key
frontend.pem        gateway certificate chain and private key
upstream.pem        upstream client certificate chain and private key
observer.env        only the declared probe/discovery credential assignments
```

Pin all five TLS file hashes. The combined upstream PEM must represent the
approved client identity. Keep keys, credentials, plans, addresses and receipts
outside public Git. The renderer reads hashes/references, not private key or
credential contents. Observer paths refer to final Linux locations: container
paths use `/etc/platform-valkey-gateway`, native paths use `private_dir`.
Windows packaging validates these as Linux paths without reading local TLS files.

```bash
python3 scripts/render_valkey_gateway.py \
  --cache-bundle-dir private/generated/valkey \
  --plan private/valkey-gateway.json --runtime k3s \
  --output-dir private/generated/valkey-gateway-k3s
```

Other choices are `rke2`, `k8s`, `docker` and `raw`. Output must be a new private
directory. The command does not apply resources, start services or certify HA.

## Hosting and unfinished integration

Kubernetes output contains an immutable code/config ConfigMap, two-replica
Deployment with mandatory failure-domain anti-affinity, TLS Service, one-available
PodDisruptionBudget and ingress/egress NetworkPolicy. Readiness checks usable
backends rather than just a listening port. Rolling updates replace one replica
at a time without requesting a surge Pod, so exactly two eligible failure
domains do not require a third domain merely to start an update. Actual rollout
scheduling and availability still need worker validation. Pre-provision the
namespace, labeled nodes, image and private Secret. The pinned image must supply compatible Python
3.12+, PyYAML 6.0.3, HAProxy, shell, GNU `sha256sum` and the declared mount paths.

The Secret contains the five TLS filenames and `probe-password` /
`discovery-password` keys. Only the checker receives credentials. Read-only
per-file `subPath` mounts preserve the observer's strict regular-file checks;
they require an explicit reviewed rollout for rotation. Egress permits only the
three reviewed addresses on 6380/26380. Real CNI policy/Service translation and
worker execution remain required; admission alone does not test enforcement.

Docker generates two separate-host Compose files with host networking, non-root
UID/GID 1000, read-only code/private binds and finite limits. Native units launch
Python and HAProxy directly as a separately provisioned `valkey-gateway` account,
with root-protected code/config and readable private files. Native proxy units
check the declared binary SHA-256 before execution. Local health ports are
19090–19093, so do not colocate the two host packages in one network namespace.

For the raw and host-network Docker packages, the
[optional Keepalived plan](VALKEY_HA.md#optional-tls-valkey-replication-and-sentinel-packages)
provides a single IPv4 entry point. Start the local checker and HAProxy on both
peers, verify that each reports ready, then start its rendered Keepalived unit.
Confirm that exactly one peer owns the VIP and that a client can authenticate
through the VIP's DNS name. On a planned gateway shutdown, check that the other
peer owns the VIP and can serve a new TLS connection before bringing the first
peer back. `nopreempt` lets the recovered peer stay on standby. See the
[Argo CD integration test](CI_GITOPS_HANDOFF.md#run-argo-cd-reconciliation-from-docker).
Runtime installation, upgrades and secret provisioning remain manual.

The existing private Harbor values renderer can select `HARBOR_REDIS_ADDR` or
`HARBOR_REDIS_HOST` / `HARBOR_REDIS_PORT` and requires TLS under its strict policy.
That is only an integration starting point. Prepare a dedicated cache, Harbor
ACL identity, trust bundle, original deployment/backup and real component tests
before changing live values. This adapter has not executed Harbor.

## Verification

```bash
PYTHONPATH=scripts python3 -B -m unittest scripts/test_valkey_gateway.py
VALKEY_GATEWAY_TEST_API=1 PYTHONPATH=scripts \
  ROOK_BOOTSTRAP_TEST_BIN=private/validation/envtest/v1.35.0/extracted/controller-tools/envtest \
  python3 -B -m unittest \
  test_valkey_gateway.GatewayTests.test_kubernetes_package_admits_without_real_cache_or_secret_reads
```

Ordinary tests model discovery disagreement, missing quorum, stale roles,
changed invocations, lag, persistence errors and foreign targets. The optional
API fixture checks admission/read-back without workers or real Secrets/cache.

The actual native fixture uses a fresh network namespace, three real servers
and Sentinels, one checker and two original HAProxy forwarders. It verifies TLS
forwarding without an application client certificate while backends still reject
clients without mTLS, plaintext refusal, surviving-forwarder operation, reconnect
after real primary/Sentinel loss and promotion, and checker-loss fail-closed
behavior. Only read-only readiness listener addresses are relocated so both
forwarders can share the isolated test namespace. The Argo CD integration
fixture separately exercises two Keepalived peers and two checkers in isolated
gateway namespaces. Physical gateway host loss remains unverified.

The source-bound component case passed in 51.008 seconds with original process
and cgroup cleanup verified. An earlier background-only run stopped without a
report and is retained as interrupted, not accepted. Application compatibility,
lossless replication, physical HA and full-stack verification remain false.
The gateway does not change Valkey's asynchronous replication guarantees.

The final Kubernetes package also passed a 14.420-second admission/read-back
case against owned Kubernetes 1.35.0 API and etcd processes, with cleanup verified.
That case had no workers, actual Secret projections, application traffic or
network-policy enforcement.

To repeat the native case, select the reviewed TLS build through
`VALKEY_TEST_ARTIFACTS`, HAProxy through `VALKEY_GATEWAY_HAPROXY`, set both
`VALKEY_TEST_RUNTIME=1` and `VALKEY_GATEWAY_TEST_RUNTIME=1`, and run the explicit
gateway test in an owned fresh network namespace under a bounded systemd cgroup.
Keep a foreground supervisor until the original unit is terminal, then inspect
the explicit test exit receipt, source hashes and original process/cgroup cleanup.
Never borrow production services or remove changed/unrelated resources to retry.
