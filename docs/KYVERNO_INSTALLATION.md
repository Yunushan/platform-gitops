# Optional private-trust Kyverno installation

This explicit installer prepares a separate Kyverno 1.19.2 installation from a reviewed official release. It does not upgrade the current default Kyverno 1.18.1 audit-first profile automatically. Do not run it against an existing unmanaged installation: pre-existing resources must carry this exact package ownership identity. The default operation checks local inputs only.

The release manifest must match SHA256 `4f785937dc7fe49a0731108b15def1538923c73d9236255bc69d94d498037969`. Five controller/init images are pinned to Linux amd64 manifest digests. Image graph integrity and official download checksums do not establish upstream image-signature verification.

A private JSON plan contains exactly:

```json
{
  "schema_version": 1,
  "runtime": "k3s",
  "management_target": {
    "kubeconfig_env": "PLATFORM_MANAGEMENT_KUBECONFIG",
    "context": "disposable",
    "cluster_uid": "11111111-1111-4111-8111-111111111111"
  },
  "namespace_uid": "22222222-2222-4222-8222-222222222222",
  "trust_configmap": {
    "name": "private-ca",
    "uid": "33333333-3333-4333-8333-333333333333",
    "ca_sha256": "<reviewed-public-ca-byte-sha256>"
  }
}
```

The namespace must already exist as `kyverno`. Pin its actual original UID and the UID/hash of an immutable ConfigMap containing exactly the public `ca.crt` data key. No SMTP settings, company endpoints, private signing keys or credentials belong in the public plan/example. The runtime selector accepts `rke2`, `k3s`, `k8s`, `docker`, or `raw`; each explicitly targets Kubernetes for admission, including a management cluster for native or Docker workloads. This selector alone does not qualify those runtimes.

```bash
python3 scripts/render_kyverno_installation.py --plan private/kyverno-plan.json --release private/downloads/kyverno-install.yaml --output-dir private/generated/kyverno
python3 scripts/deploy_kyverno_installation.py --bundle-dir private/generated/kyverno --contract-sha256 <reviewed-contract-sha256>
```

The renderer writes a new ignored private bundle. Review it and use its status receipt's `contract_sha256`. Add `--check-server` for verified HTTPS/context/cluster-UID checks, namespace and immutable trust checks, ownership preflight and server-side dry-run. Only `--apply` performs writes. It uses server-side apply without force-conflicts, avoiding the client-side last-applied annotation size limit for large CRDs. An uncertain write is not retried automatically.

The profile retains three admission replicas and two each of the background, cleanup and reports controllers. Required anti-affinity separates each role by both hostname and zone. Provide three schedulable Linux workers with explicit distinct zone labels; missing capacity leaves Pods Pending. Admission has a minimum-available disruption budget of two, other roles one. Memory requests/limits remain 256/768 MiB per admission, background and reports replica, and 128/512 MiB per cleanup replica; CPU requests are 100m except cleanup 50m. Upstream init resources and container security settings remain intact. Every controller receives the reviewed public private CA; no TLS bypass is installed.

Controller readiness reports installation progress only. It does not establish working signature policies, enforcement, failure recovery or HA. Create policies with `Deny`/`Fail` and explicit private trust and pull-only registry credentials using the [private TUF workflow](PRIVATE_SIGSTORE_TUF.md). An ImageValidatingPolicy needs its explicitly referenced registry Secret in the Kyverno namespace; a workload pull Secret alone does not authorize the policy verifier.

The opt-in `test_valkey_gateway.GatewayTests.test_real_kyverno_three_replica_image_admission` case provisions one dedicated control VM and three 2 GiB worker VMs, real Kyverno controller Pods, private Harbor, Cosign, a TLS Rekor log and a threshold-signed TLS TUF mirror. It pins the original policy UID/generation/specification, checks the nested compiled-ready status, requires named policy denials for unsigned/wrong-key/wrong-binding regular images and unsigned init/ephemeral images through the Kubernetes API, and runs an actual signed-image workload across all three workers. The single-host lab and single TUF mirror do not establish physical HA, trust-service HA, or the complete Gerrit/CI/Argo path. Runtime proofs and logs remain under ignored `private/validation` paths. Full stack qualification remains false.

Sources: [Kyverno high availability](https://kyverno.io/docs/guides/high-availability/), [ImageValidatingPolicy credentials](https://kyverno.io/docs/policy-types/image-validating-policy/), [reviewed release](https://github.com/kyverno/kyverno/releases/tag/v1.19.2).

## Explicit controller-failure test

The separately gated `test_valkey_gateway.GatewayTests.test_real_kyverno_worker_loss_and_fail_closed_recovery` case extends the original three-worker admission setup. It pauses one original worker VM, waits for the two original surviving admission endpoints, verifies signed admission and unsigned refusal, and requires the disruption budget to refuse a server-dry-run eviction of a second peer. Recovery must retain original VM processes, Node/Service/Deployment/Pod/policy UIDs, specifications and specifications without replica/resource reductions. Containers on surviving workers must retain their identities. A container on an intentionally failed worker may qualify only through a bounded, linked graceful transition: the old container ID must match its recorded clean exit, the new instance must be ready, and previous logs must verify leadership fencing. Unexplained exits, OOMs, unlinked identities and surviving-worker restarts fail the test.

It then pauses all three original worker VMs and requires the still-running control API to refuse a valid signed Pod through the named `Fail` image webhook. EndpointSlice readiness may remain cached during total loss: a global fail-closed metadata webhook can also block controller updates. Actual original QMP-paused VM identities establish unavailable backends; the test does not count cached ready endpoints as serving. All paused VMs resume in `finally`, after which signed admission, unsigned refusal and application HTTP serving must recover. Runtime inputs and full API evidence remain private.

The one-peer test measures convergence before asserting surviving service. It does not claim zero failed requests during the transition, physical-host HA, trust-mirror HA, independent control-plane HA or the complete CI-to-Argo path. The test is optional and does not pause any company node or use an existing cluster.

Failure qualification additionally requires explicit boolean EndpointSlice readiness, a current successfully processed disruption budget, and actual renewal and API resource-version advancement of every original worker Lease beyond a post-resume observation fence (without comparing worker clocks to the host clock). All four controller roles must return to full readiness. The final identity/resource snapshot follows the admission and HTTP recovery probes and includes the original application Deployment, Service and Pods. Cleanup tracks uncertain pause operations and attempts every original guest even when another resume fails; bounded outer-scope cleanup remains the fallback. Partial fault snapshots and API responses are retained privately on failure so an incomplete run cannot qualify from stale cached health.

The failure lab uses the existing checksum-pinned guest Chrony service and a read-only parent NTP relay with `CAP_SYS_TIME` removed. It verifies guest clock synchronization after resume; worker recovery still requires actual Lease renewal beyond the post-resume fence. Host time is never adjusted.

Restart allowances are anchored to each fault's preceding observed container state and cannot accumulate across faults. Recovery and final snapshots require all four Deployment roles and all nine active controller Pods to be fully ready, including containers whose identities did not change.

The explicit `GERRIT_ADMITTED_PROMOTION_TEST_RUNTIME=1` gate adds a joined local
Gerrit ÃƒÂ¢Ã¢â‚¬Â Ã¢â‚¬â„¢ Tekton ÃƒÂ¢Ã¢â‚¬Â Ã¢â‚¬â„¢ merged image ÃƒÂ¢Ã¢â‚¬Â Ã¢â‚¬â„¢ Cosign ÃƒÂ¢Ã¢â‚¬Â Ã¢â‚¬â„¢ native promoter ÃƒÂ¢Ã¢â‚¬Â Ã¢â‚¬â„¢ Argo rollout test.
It uses one dedicated API VM and three original worker VMs, preserving the
three admission replicas and both replicas of each other Kyverno controller.
The policy is ready in the original Argo destination namespace before the
promoter's Git push. The actual CI image must satisfy all eight signed bindings;
separate server admission probes refuse each incorrect binding, the wrong key,
and unsigned regular, init and ephemeral images. Original controller, policy,
Application, Deployment and Service identities are retained in ignored private
evidence. The HTTPS trust mirror stays alive through the original rollout and
is removed by the enclosing workflow, including on failure.

This gate does not run in ordinary validation and is not physical HA or full
stack qualification. Its preparation alone is not a successful runtime result.
Existing two-worker fixtures and the default audit-first installation remain
unchanged. The shared admission helpers also retain the separate worker-loss
and fail-closed recovery fixture; the joined gate does not imply those faults
were exercised in the same run.

The separate `GERRIT_ADMITTED_FAILURE_TEST_RUNTIME=1` gate runs worker-loss,
disruption-budget, total admission outage and recovery probes against that
original signed CI deployment. Workload names, namespace UID, Deployment and
Service UIDs, Pod UIDs and the enforcing policy UID come from its original API
evidence. A different synthetic workload cannot satisfy this gate. Original
Argo Application identity, source specification and promoted Git revision must
remain unchanged. An identity-bound hard refresh must produce a new Argo
reconciliation after the recovery fence; cached Healthy status cannot qualify.
All three original application Pods and HTTP serving must also verify before
success is reported.

This explicit fault profile permits a 1700-second guest lifetime within the
original finite parent service. Default fixtures retain 1100 seconds, and all
CPU, memory, replica and failure-domain settings remain unchanged. The explicit
1700-second profile applies that lifetime to the agentless control plane as well
as the workers, and requests finite 30-minute Git and Kubernetes test identities
so the original credentials cover the full fault/recovery window; ordinary
fixtures retain ten-minute identities. Credentials are not rotated on failure,
and authentication failures remain failures. Failure
evidence distinguishes cached EndpointSlice readiness from actual original VM
pause state; it requires fresh post-resume worker Leases and bounded, evidenced
graceful controller fencing. This local test does not establish physical HA,
continuous zero-error service, or full stack qualification.

Fault tests attach bounded log streams before pausing each original controller
container. Pod UID and container ID must match before and after attachment.
A successful previous-container logs API read remains authoritative: an earlier
follow stream can end before shutdown records are written, even with exit code
zero. When that API is unavailable, only a completed, successfully captured
original stream can supply the required fencing evidence. Neither source bypasses
the fencing checks, and a favorable stream cannot replace a successful API read.
The original container termination and exact leadership-loss checks still apply.
Raw API log errors and stream lifecycle records stay in ignored private proofs;
all stream processes have finite lifetimes and explicit cleanup verification.


## Optional persistent native CI watcher join

`GERRIT_ADMITTED_WATCH_FAILURE_TEST_RUNTIME=1` selects
`test_valkey_gateway.GatewayTests.test_real_native_watched_signed_ci_admission_failure_recovery`.
It additionally requests the original native Gerrit bridge/reporter services,
their existing controlled restart/replay checks and the signed-admission worker
failure profile. Existing cases keep their defaults.

The explicit 1700-second guest profile gives each watcher a finite 1700-second
service limit and binds it to the original active parent unit. Ordinary watcher
fixtures retain 300 seconds. Memory/task limits, privilege boundaries, original
invocation checks and cleanup requirements remain unchanged.

The joined check requires the same restarted watcher processes before and after
the faults, two fresh complete poll summaries per service after recovery, and a
fresh exact match of the original merged CI data and signed evidence. Merged
changes can produce reporter rejections under its open-change rule.
Credential-scanned bounded watcher logs stay in ignored proofs for independent
audit.

The isolated K3s run passed with Gerrit, Tekton, Harbor and Argo CD. Kyverno
rejected 12 invalid requests, refused admission while all its original
backends were down, and recovered under the same policy. The original
signed revision served on three workers after recovery. Both restarted
watchers recorded fresh polls, and the lab removed their original processes,
credentials and guest scopes. Post-recovery event delivery, persistent-host
reboot and restore, physical HA and full stack qualification remain open.


Survivor readiness includes the actual admission transport, not only the cached
ready EndpointSlice set. The signed server-dry-run permits at most three attempts
for an exact EOF from the original named policy webhook, inside the original
100-second post-pause convergence budget. Every failed attempt is retained in
the probe ledger, and the report separates endpoint and admission convergence
times, EOF count and total request count. Policy denials, certificates,
authorization, unknown failures and late successes cannot use this retry; all
negative and total-outage probes remain single requests. The original endpoint
identity set must still match before every attempt. This measures bounded
recovery and explicitly does not establish zero-error availability.

The post-recovery watcher barrier derives its finite deadline from each original
hashed contract: three configured poll intervals for two complete fresh poll
records. With the fixture's unchanged intervals this is 90 seconds for the bridge
(30-second polls) and 30 seconds for the reporter (10-second polls). The offset,
contract bytes/hash, deadline, observed records and completion state are retained;
late success fails. A failed barrier keeps its observations in the ignored
diagnostic proof. Poll frequency, required fresh records, process identities and
service lifetimes are not changed to make the check pass.
