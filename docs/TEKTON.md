# Tekton Pipelines packages and manual worker canary

Status: opt-in controller packages, guarded installation and an explicitly
started infrastructure canary. This advances the Gerrit, Forgejo and Gitea
Tekton combinations in the [multi-runtime expansion](MULTI_RUNTIME_STACKS.md).
A separate disposable KVM/K3s case has passed real controller installation,
active fail-closed admission and one explicitly requested infrastructure job.
SCM event integration with real workers, application CI, failover, upgrade and
restore remain unaccepted. This result does not certify a complete stack.

## Explicit runtime and release selection

Tekton controllers and jobs run on Kubernetes. RKE2, K3s or existing Kubernetes
can provide that target. For Docker/native application hosting, select a
separate Kubernetes management target explicitly. This adapter does not create
a hidden cluster or implement Docker/SSH application deployment. The runtime
selection records application hosting and preserves that remaining integration.

Copy [the placeholder plan](../examples/tekton.json) into `private/tekton.json`.
Record the exact reviewed stable Pipelines 1.x release, release-manifest SHA-256,
management context and **kube-system namespace UID**. Prepare a private
kubeconfig whose API uses verified HTTPS. A context name alone is insufficient
to identify the installation target.

Obtain an exact release manifest through the documented
[upstream installation mechanism](https://tekton.dev/docs/installation/pipelines/),
for example the version-specific
[v1.4.0 release](https://infra.tekton.dev/tekton-releases/pipeline/previous/v1.4.0/release.yaml).
That version is a compatibility-test fixture, not a recommendation about the
latest supported release. Review release provenance, signatures, licenses,
security notices and Kubernetes compatibility before selecting production
artifacts. Hash equality binds bytes; it does not establish their authenticity.
Upstream recommends its Operator for production lifecycle management. This raw
manifest adapter does not implement the Operator's full lifecycle contract.

The manifest must contain the supported four-controller layout: pipeline,
events, webhook and remote-resolvers deployments in their original two
namespaces. Unknown layout, nonempty Secret content, mismatched declared version
or missing required APIs stop generation. Controller and injected helper images
must already have full SHA-256 references. Optional `image_overrides` maps exact
original digest references to reviewed mirror digest references; unused keys
are rejected. Review every helper, including unused platform-specific images.
The step image must provide Linux `python3` and support UID/GID 1000.

## Controller configuration and prerequisites

Each deployment gets two or more candidates, required failure-domain
anti-affinity, explicit Linux node selection and a disruption budget retaining
`replicas - 1` candidates. Rollouts use zero surge and one unavailable replica.
Preprovision enough eligible failure domains and capacity for all selected
replicas, including the webhook autoscaler maximum. A scheduling constraint
does not establish actual node redundancy or failover.

Bucket election configuration is explicit for pipeline, events and resolver
controllers; the webhook keeps one election bucket. The webhook HPA minimum
matches the selected replica floor. Provide the metrics service needed for
autoscaling. The release's supported HA behavior is retained; explicit
disable-HA flags are refused. See the
[upstream HA model](https://tekton.dev/docs/pipelines/enabling-ha/).

The package disables automatic credential initialization, enables injected
security contexts and read-only roots, requires SSH host verification, and
sets a deliberately unprovisioned default task service account. Workloads must
select an execution identity explicitly. The example uses stable API fields
and disables all remote resolvers. Enabling any resolver also requires explicit
`api_fields: "beta"` approval; the
[HTTP resolver requirements](https://tekton.dev/docs/pipelines/http-resolver/)
describe the feature gate. Resolve external tasks only under a separately
reviewed source, content-integrity, network and credential policy. This package
does not certify those remote resolution paths.

Preprovision the dedicated canary namespace with
`pod-security.kubernetes.io/enforce: restricted`, dedicated Linux worker capacity,
admission-compatible policies and controller-to-API/webhook connectivity. The
canary service account receives no role bindings and disables automatic API
token mounts. Controller RBAC from the release remains cluster-scoped; restrict
who can submit tasks or edit controller configuration. Supply network isolation,
quotas, image policy and tenant boundaries separately. The canary is not an
untrusted-code sandbox or evidence of those controls.

## Render, inspect and install

```bash
python3 scripts/render_tekton.py \
  --plan private/tekton.json --bundle private/tekton-release.yaml \
  --runtime rke2 --output-dir private/rendered-tekton
```

Other application-hosting selections are `k3s`, `k8s`, `docker` and `raw`.
Rendering contacts no service and requires a new output directory under
`private/`. The generated contract binds exact component, canary and run-template
bytes. Review the files and retain the separately recorded `contract_sha256`
from `render-status.json`.

```bash
python3 scripts/deploy_tekton.py \
  --bundle-dir private/rendered-tekton \
  --contract-sha256 '<REVIEWED_CONTRACT_SHA256>'
```

The default mode checks only local hashes and inventory. To contact the selected
cluster, set the environment variable named by `management_target.kubeconfig_env`
to the private kubeconfig path and add exactly one server mode:

- `--check-server`: inspect cluster identity, canary namespace and component
  ownership, then server-dry-run the component manifest. Custom canary resources
  are deferred until their CRDs and webhooks are ready. When controller namespaces
  are missing, dry-run only the cluster-scoped bootstrap, return their names in
  `namespace_bootstrap_required` and leave `server_checked: false`. Preview mode
  creates no namespace and does not claim full admission validation.
- `--apply`: perform those checks, install components using server-side apply,
  wait for CRD establishment and all deployment rollouts, require active
  fail-closed webhook registrations with rules and CA trust, then dry-run and
  apply the canary service account, Task and Pipeline. Missing controller
  namespaces are applied from the reviewed manifest after the bootstrap dry-run
  and ownership checks, before full component admission validation. A later
  failure can therefore retain those namespaces for diagnosis and retry.

The installer refuses foreign-owned existing component resources, forces no
field conflicts and performs no pruning or deletion. Existing Operator-managed
Tekton requires a separate ownership migration. The canary namespace is a
pre-existing prerequisite. The two upstream component namespace manifests are
included in the apply, and their metadata is exempt from the ownership check;
review those namespace changes explicitly. Only an empty bootstrap `webhook-certs` Secret
is allowed in the input, and generated private key contents are never captured
by the ownership check. Admission remains fail-closed; the installer never
disables validation to make the canary pass.

Failed operations retain partial resources for diagnosis and an explicitly
reviewed retry. Upgrades require release-specific CRD, webhook, RBAC, image,
state, backup and rollback review. Applying a new contract is not proof of a
safe downgrade or an upgrade/restore procedure.

## Run and verify the manual canary

Add `--run-canary` to an explicitly authorized `--apply` invocation to create
one fresh generated-name PipelineRun. Installation without that flag starts no
job. Repeating the flag intentionally creates another run. The fixed probe
does not clone repositories, execute application code, report review votes,
modify forge accounts, deploy workloads or send notifications.

The probe writes a JSON result bound to its actual TaskRun UID and rejects UID
0. Its step uses a digest-pinned image, read-only root, non-root identity, dropped
capabilities and runtime-default seccomp. Placement requires the declared Linux
worker selector and excludes control-plane/master nodes. No source token or
deployment credential is provisioned for this job.
PipelineRun success can precede the kubelet's final Pod-phase update. The
installer therefore allows a bounded 60-second finalization wait for the
original owned Pod, pins its UID, rechecks the TaskRun UID and then repeats
ownership, phase, image, credential and placement checks. A replacement Pod or
TaskRun cannot satisfy the wait by reusing the original name. Read-only direct
verification does not wait or accept a still-running Pod.

After success, verification correlates the created PipelineRun UID, selected
pipeline and service account, its sole probe TaskRun, the TaskRun result UID,
the completed worker Pod and that Pod's node labels. Unexpected child tasks,
credentials mounted into the Pod, image changes or control-plane placement fail
verification. `canary_completed` can become true only on this live apply path;
event integration, application CI and HA flags remain false independently.

Offline tests exercise installation ordering and negative cases through a fake
API client and execute the fixed probe with a simulated non-root UID. With
`TEKTON_TEST_EXPORT` pointing to a reviewed release, they also transform its
actual manifest for all five runtime selections and compare generated custom
resource field names with that release's CRDs. This is not full schema,
admission or Kubernetes execution validation.

## Remaining stack acceptance

Failed Kubernetes commands now expose only a return code and fixed diagnostic
booleans for timeout, transport/TLS, admission, conflict, discovery and other
failure classes. The shared exception remains a `ValueError`; its message and
diagnostic fields do not include command arguments, request bodies or raw
server output. No automatic retry is added for uncertain writes. The worker
fixture records the original failed operation's categories before cleanup and
can recheck both the component and canary payloads with server dry-run only.
These signals guide investigation; they do not prove a root cause or make a
failed admission acceptable.

Tekton 1.4.0 pins Knative revision `ed3e2158b8839a02c3381e463c965a4c87a53c94`;
its [certificate template](https://github.com/knative/pkg/blob/ed3e2158b8839a02c3381e463c965a4c87a53c94/webhook/certificates/resources/certs.go#L67)
starts validity at creation time. A paused VM's clock is therefore a candidate
when different API servers inconsistently reject a newly issued webhook
certificate. The fixture now observes only the public admission CA and each
original guest's clock offset, round-trip uncertainty and CA validity checks;
it reads no private certificate key and disables no TLS check. Signals for
certificate timing, trust/name errors and handshake timeout are distinct. A
`timeout` query parameter in a webhook URL alone is not a timeout failure.

The focused experiment reproduced this failure on 2026-11-08: two paused
control-plane clocks lagged the parent by approximately 27 and 14 seconds, and
both initially preceded the new webhook CA's validity window. Independent
observations and failure diagnostics bound the same public CA SHA-256; the
failed admission reported a certificate-time error with no trust, hostname or
timeout error. This identifies missing lab clock synchronization, rather than
permission or resource-schema rejection. That failed 516.898-second experiment
is diagnostic evidence, not accepted installation. The guests and parent unit
were removed. Synchronization and a fresh full-flow rerun were required;
certificate backdating or disabled TLS verification is not used.

The subsequent time-enabled five-guest Flannel diagnostic passed in 585.671
seconds on 2026-11-08. It recovered the original three voters after VM pauses,
forced one original K3s process failure and verified its supervised restart,
then installed actual Tekton controllers/admission and completed the explicit
PipelineRun canary. All 116 worker HTTP samples during process recovery passed;
the measured 30.184-second recovery includes the stability check and is not a
production RTO claim. A separate observation found all five guest clocks
inside the original public webhook CA's validity window. An independent audit
matched all 345 Python sources, checked clock evidence and original voter/Node
identities, and confirmed process/unit/private-scope cleanup. Its private run
log SHA-256 is
`ee843784eccd522cc46231e865c3f407fb6f7e320f787b4571c9689d9c07c40f`.
This accepts the focused clock/recovery/admission case; the complete
Cilium/native CI/CD rerun and all full-stack requirements remain separate.

The subsequent combined run passed the formerly failing admission/canary step
under Cilium, but stopped after 1101.839 seconds because the native reporter
exited unexpectedly and systemd restarted it once beyond the planned restart
test. The final original-invocation audit correctly rejected that result.
Public-CA observations still placed all five clocks inside the validity
window; no OOM kill occurred. All original services, processes and private
scopes were removed. This is a failed combined case, not full-flow acceptance.
The reporter now emits only exception class, bounded source-code locations and
fixed Kubernetes failure categories. The fixture captures those safe records
before deleting private logs. Credentials, response bodies and command
arguments remain suppressed, and the original-invocation guard remains intact.

The next combined run passed in 1166.732 seconds with exactly the planned
native watch restarts. Its independently audited 345-source snapshot, real
quorum/process recovery, admission/canary, Gerrit worker tests, promotion,
Argo CD serving and application-Gateway evidence are recorded in the
[joined worker case](K3S_VM_WORKERS.md#optional-cilium-and-native-cicd-application-case).
The previous reporter exit did not recur; this passing run does not establish
its cause or replace broader failure/load and full-stack acceptance.

The opt-in `TEKTON_QUORUM_VM_TEST_RUNTIME=1` method
`WorkerRuntimeTests.test_real_tekton_after_quorum_and_process_recovery` isolates
actual Tekton admission after the five-guest quorum and process-failure checks.
Use the bounded five-VM command from the worker runbook, the pinned Tekton
release/image inputs, a 12 GiB hard limit and a fresh private receipt. It uses
the worker runbook's pinned Chrony input and parent `CAP_SYS_TIME` exclusion,
the ordinary fixture's Flannel networking, finite 1100-second guest lifetimes
and 1200-second parent deadline. This diagnostic does not replace acceptance of
the complete Cilium/native CI/CD case.

The separate [KVM-backed K3s worker fixture](K3S_VM_WORKERS.md) provides an
explicit real scheduler/kubelet/containerd probe target with two worker VMs.
It remains separate from the API-only admission fixtures. Its completed worker
probe Pods alone do not authorize a Tekton execution claim. The additional
Tekton case now imports nine content-verified OCI archives, installs the actual
controllers, checks that preview creates no namespace and installation starts
no job, then explicitly creates and verifies the manual canary's real
PipelineRun/TaskRun/Pod/worker/result chain. Its source-bound receipt is emitted
only after all guests and temporary disks are removed. This is K3s laboratory
infrastructure acceptance, not event-driven application CI or complete-stack HA.

The optional [Forgejo/Gitea push bridge](TEKTON_FORGE_BRIDGE.md) now packages
authenticated push submission, a fixed inline pipeline and exact-commit
checkout/build steps. It has offline retry and credential-boundary tests;
live event execution, reporting and full stack acceptance remain open.

Implement and test Gerrit events or authenticated Forgejo/Gitea webhooks,
replay protection, exact repository/revision binding, isolated application
workers, scoped checkout credentials, artifacts, result reporting and Argo CD
handoff. Separately prove controller/webhook disruption behavior, in-flight job
recovery, policy denial, resource limits, upgrades and off-platform restore.
Controller replica counts and a completed infrastructure probe cannot substitute
for that full stack evidence.
