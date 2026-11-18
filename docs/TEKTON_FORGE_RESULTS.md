# Verified Tekton results and forge commit statuses

Status: optional result verifier and reporter packages for the
[Forgejo/Gitea push bridge](TEKTON_FORGE_BRIDGE.md). Offline API simulations,
runtime configuration checks and disposable Forgejo and Gitea builds on K3s
exercise reporting and the Argo CD handoff. Adversarial isolation, HA, upgrade
and restore remain open. This is one part of the full
[requested stack expansion](MULTI_RUNTIME_STACKS.md).

## Disposable Forgejo and Gitea validation on K3s

Separate lab runs used native Forgejo 15.0.6 and Gitea Community 28.3.0 with
disposable SQLite databases and trusted HTTPS. For each forge, a Git push
to a private repository triggered the packaged receiver through the forge's
own signed webhook. Tekton Pipelines 1.4.0 ran on K3s v1.35.0+k3s1 with one
control plane and two dedicated workers. An unsigned request was rejected.
Replaying the signed event kept the original PipelineRun UID.

The worker checked out the exact private commit. Its build verified non-root
execution, runtime seccomp, no effective capabilities, and the absence of both
the checkout token and the Kubernetes service-account token. The receiver and
reporter used separate RBAC identities. Both were denied access to Kubernetes
Secrets, and the reporter was denied PipelineRun creation. A separate Forgejo
identity published the success status. Read-back confirmed the commit and run
UID, and a second reporter invocation left exactly one status in forge history.
The VMs and test scope were removed after verification.

The verified commit included Kubernetes manifests tested by the Tekton job.
The packaged handoff used a separate Git writer and read-only Kubernetes
identities. The CI reader was denied PipelineRun patches and receipt creation.
Its preflight kept the deployment branch absent. The apply run promoted the
exact tested commit, and Argo CD 3.5.0 reported a successful sync of that
revision. Two replicas served the expected HTTP response on distinct K3s
workers. Repeating the handoff verified the same deployment without a second
push. Both runs removed their disposable scope after verifying the HTTP
workload.

The cases used non-root processes with fixture-managed paths. Native systemd
installation of all services, deployment of the complete stacks on RKE2 or
existing Kubernetes, and physical failover remain open.

## Docker Compose forge with K3s

Forgejo 15.0.6 and Gitea Community 28.3.0 ran from their rootless images through
Docker Compose v2.40.3 on a disposable Docker 29.9.3 engine. HAProxy routed
traffic through the Compose-published loopback HTTP port and forwarded each
forge's signed webhook. The Gitea case used strict egress with an explicit
allowlist entry for the receiver's address and port. Tekton built each exact
private commit on K3s, and the separate reporter published one success status.
Native Argo CD 3.5.0 synced each tested revision, and the deployment responded
on both K3s workers. Retrying the handoff did not push a second time.

Forced Compose replacement retained each repository ID, source and deployment
refs, and commit status. The tests used reviewed image content with local tags
for offline loading. The receiver and Argo CD ran as native processes with
disposable test credentials. A full Docker CI/CD deployment and physical HA
remain unverified.

## Explicit enablement and separate authority

The example keeps `reporting.enabled` false. Omitting `reporting` also disables
it. Rendering never starts a reporter or writes a forge status. To enable it,
review the private plan's complete reporting settings:

- `account`: a separate Kubernetes reporter service account in the controller
  namespace. It must differ from receiver and build identities.
- `secret`: a separate controller-namespace Secret for reporter inputs. The
  receiver and build jobs receive no reference to this Secret.
- `context`: the reviewed commit-status context prefix.
- `poll_seconds`: 10 through 60 seconds between bounded list pages.
- `max_runs_per_poll`: 1 through 100 PipelineRuns per page.
- `helper_images`: the complete reviewed digest references for the actual
  Tekton init helpers, including entrypoint and working-directory initialization.
  Use the resolved images from the selected controller package, including any
  approved mirror overrides. At least one pin is required when reporting is
  enabled; every observed init image must belong to this inventory.

The renderer adds reporter RBAC/workloads for Kubernetes targets, a second
Compose service for Docker, or a separate native systemd unit. The reporter
uses the same reviewed code/template contract and explicit Kubernetes job
target. Kubernetes candidates have separate placement/budget configuration;
replica counts do not certify failover. Docker/native redundancy remains an
external entry/host recovery responsibility.

Reporter RBAC permits get/list for the job namespace's PipelineRuns, get for
TaskRuns and Pods, and get/create for receipt ConfigMaps. Cluster access is
limited to two namespace identities and get for worker Node metadata. It cannot
create jobs, read Kubernetes Secrets, patch status, update objects or delete
resources. Review cluster-level metadata exposure and admission separately.

## Private inputs

Prepare these files under `/run/platform/reporter`, readable only by the
reporter identity/group:

- `status-token`: a dedicated forge token scoped to the selected repository and
  the minimum permissions needed to write commit statuses and read them back.
  Review the selected forge edition/version's actual token scopes; this package
  does not claim a universally available status-only PAT permission.
- `ca.crt`: the trusted source API CA bundle.
- `kubeconfig`: the explicit job-cluster context and reporter identity.

Source status writes use verified HTTPS and reject redirects. The reporter
uses the same private flattened kubeconfig snapshot and kube-system UID check
as the receiver. Target/token rotation requires a reviewed restart. No receiver
webhook key, receiver TLS server key or Git checkout token is needed here.

For Docker, supply the separate host directory through
`PLATFORM_TEKTON_REPORTER_INPUTS`; the service exposes no listening port.
For native Linux, provision the distinct `platform-ci-reporter` user/group,
install the reviewed bundle under `/etc/platform/tekton-bridge`, and review the
generated `platform-tekton-reporter.service`. Native permissions must prevent
either service identity from reading the other identity's input directory.

## What establishes a publishable outcome

The reporter selects only runs labeled with this reviewed contract. It checks
the deterministic run name, full contract annotation, exact revision parameter,
submitted spec and UID against the immutable event-submission receipt.
Pending or unreceipted jobs cannot publish success.

A terminal failure with a completion timestamp can publish `failure`, including
validation failures that occurred before any worker started. This reports the
pipeline outcome and does not claim the application tests executed.

Success requires stronger evidence:

The submitted EmbeddedTask wrapper and executed regular TaskSpec are separate
API representations. Only the known empty wrapper metadata and null custom-task
extension are projected out for TaskRun comparison. Nonempty wrapper metadata
or custom-task content is rejected; this does not discard command, parameter,
image, ordering or credential checks.

- Exactly one owned build TaskRun, with the expected inline TaskSpec, revision,
  execution service account and successful completion.
- Its actual completed worker Pod, linked by controller owner UIDs.
- The two expected digest-pinned checkout/build images and commands, including
  the supported Tekton entrypoint wrapping and build-after-checkout ordering.
- Reviewed init-image identities, successful init/step exits and read-only
  entrypoint binary mounts. Both TaskRun and Pod step states must record zero
  exit codes; skipped or changed execution cannot substitute for a test pass.
- Non-root security configuration, expected mounts, no unexpected secret/env
  credentials in the build step, and no host/process namespace sharing.
- A real worker Node identity with the selected labels, no control-plane role,
  and a creation timestamp predating this Pod's execution.

The entrypoint/order checks were compared with the pinned upstream
[v1.4.0 entrypoint implementation](https://github.com/tektoncd/pipeline/blob/v1.4.0/pkg/pod/entrypoint.go)
and [Pod builder](https://github.com/tektoncd/pipeline/blob/v1.4.0/pkg/pod/pod.go).
A release changing that execution contract requires compatibility review; an
unknown layout fails verification. This is controller evidence under a trusted
cluster, not an independent signed attestation against a compromised control
plane. Tenant/network isolation, artifacts, signatures and application-specific
test coverage remain separate acceptance requirements.

## Status publication and retries

The final context is `context/<first-12-characters-of-contract-sha256>`. This
keeps different reviewed pipeline versions from overwriting each other's
outcomes for the same commit. Review the exact required context in branch
protection during promotion; the reporter never changes repository permissions,
merge rules, approval votes or protected-branch settings.

The fixed status description identifies the PipelineRun UID. The status is
written for the immutable commit, even if the branch has since advanced. No
job dashboard link is invented and no build log or private error text is sent.
The reporter rechecks controller evidence immediately before a new publication,
then reads the selected context through the forge's combined-status API.
The create request sends the outcome in `state`. Read-back compares it with
the individual commit-status object's `status` field. Isolated Forgejo 15.0.6
and Gitea 28.3.0 instances verified status creation and read-back for the exact
commit over trusted HTTPS. Both denied the same write with a read-only token.
The implementation follows the documented
[Gitea status endpoints](https://github.com/go-gitea/gitea/blob/v1.24.6/routers/api/v1/repo/status.go);
verify the actual selected Forgejo/Gitea API release in live acceptance.

An immutable report reservation precedes a new status append. If an API response
is lost, read-back can recover the matching outcome. An unresolved reservation
stops automatic re-appending; it never assumes a timeout proves failure.
Concurrent candidates share the same reservation. A separate immutable receipt
records the verified payload/evidence digests after publication. A previously
reported context that drifts is rejected rather than overwritten.

Retain event receipts, report reservations and report receipts with cluster
backups. Keep TaskRuns and Pods until the first report is verified. Once a report
receipt exists, later polls can check its bound outcome against the forge even
after child Pod cleanup. If evidence disappears before receipt completion,
inspect cluster audit/job history and the forge status before manual recovery.
Deleting receipts removes retry protection; this helper performs no cleanup.

The optional [GitOps handoff](CI_GITOPS_HANDOFF.md) rechecks the complete worker
evidence before promotion. Retain TaskRuns and Pods until that handoff is also
verified, rather than cleaning them immediately after status publication.

## Run locally or explicitly report

```bash
python3 scripts/tekton_forge_reporter.py \
  --bundle-dir private/rendered-forge-bridge \
  --contract-sha256 '<REVIEWED_CONTRACT_SHA256>'
```

The default reads only the local reviewed package. Add `--report-once` to
authorize one bounded page of cluster reads and possible forge status writes.
It reports whether more pages exist. Add `--watch` instead for the generated
long-running service. Output contains counts, not repository names, payloads,
tokens or logs. Rejected evidence must be investigated; pending and successful
submission are not interchangeable with completed application CI.

No report command was run against company services during this implementation.
Complete live source delivery, build execution, result read-back, replay/race,
credential-denial, outage, upgrade and restore drills before accepting the
integration. The optional deployment-branch handoff now has its own guarded
command and checks; live Argo CD/Flux integration and the separate Gerrit
event/reporting path still require implementation or acceptance work.
