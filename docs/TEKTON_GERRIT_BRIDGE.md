# Gerrit patch sets to Tekton

This optional package adds review submission for the requested Gerrit + Tekton +
Argo CD stack. It supports native Linux, Docker, RKE2, K3s and existing Kubernetes
hosting of the submitter. Every format uses an explicit existing Kubernetes
management target for Tekton jobs; native/Docker hosting creates no hidden cluster.

The submitter discovers current open, non-WIP patch sets through authenticated
Gerrit REST polling. It can also consume one saved `patchset-created` JSON event
from Gerrit's [event stream](https://gerrit-review.googlesource.com/Documentation/cmd-stream-events.html).
An event file is a candidate, never submission authority. Gerrit's HTTPS API
must confirm the exact project, branch, change number, current patch-set number,
revision and canonical ref. Stale, closed, WIP and mismatched events are refused.

## Private preparation

Copy [the example](../examples/tekton-gerrit-bridge.json) into ignored `private/`
storage. Replace image digests, cluster context/UID, original job namespace UID,
CA byte hash and source/branch references. Example hashes identify no verified
application image or trust root. Preprovision the restricted job namespace, a
different control namespace, separate Linux worker capacity, reviewed
[Tekton installation](TEKTON.md) and application-specific network boundaries.

Prepare three files at `/run/platform/gerrit-tekton`, readable by the non-root
submitter and inaccessible to other users:

- `api-token`: a Gerrit HTTP authentication token for a narrowly scoped reader.
- `ca.crt`: the exact PEM trust anchor committed by byte SHA-256 in the plan.
- `kubeconfig`: a self-contained verified-TLS configuration for the explicit
  management target and separate submitter identity. Dynamic credential plugins
  are refused; the process freezes this configuration before server operations.

Gerrit's [REST authentication](https://gerrit-review.googlesource.com/Documentation/rest-api.html#authentication)
uses HTTP Basic authentication under `/a/`. The package sends credentials only
to the fixed HTTPS origin, refuses redirects and ignores ambient proxies. It
checks the protected JSON prefix and bounds responses. No Gerrit SSH connection
is required for polling or checkout. URLs cannot embed credentials.

The separate `read_secret` in the job namespace contains `token` and `ca.crt`
for the scoped Git checkout login. API and checkout credentials may have
independently managed values; neither belongs in a public plan. Only checkout
mounts the Git read Secret. The build has no Gerrit write credential or mounted
Kubernetes token. Submitter credentials are not mounted in job Pods.
Both steps mount the bounded worktree volume directly at
`/workspace/repository`, writable through filesystem group 1000. Git trusts
only that exact fresh directory; it does not trust arbitrary repositories.
The build command remains the reviewed argument array and needs no additional
interpreter for changing its working directory.

Review/apply generated RBAC and workload through the approved deployment path.
The renderer does not apply resources, generate credentials or install services.

## Render and run

```bash
python3 scripts/render_tekton_gerrit_bridge.py \
  --plan private/tekton-gerrit-bridge.json --runtime raw \
  --output-dir private/generated/gerrit-tekton
```

The Docker and Kubernetes packages can use the same pinned Python and
kubectl runtime image as the Forgejo and Gitea bridge. Set `bridge_image`
to the published repository and manifest digest.
[Prepare the bridge image](TEKTON_FORGE_BRIDGE.md#prepare-the-bridge-image)

Use `docker`, `rke2`, `k3s` or `k8s` for the corresponding hosting artifacts.
Native output uses a non-root systemd service; Docker uses a constrained,
read-only container with no published listener. Kubernetes output has two
anti-affine submitters and a disruption budget. These replicas alone do not
establish source availability or completed-job HA.

Stage the exact package and private inputs. Set the explicit kubeconfig
environment variable named by the plan. A non-root Linux identity may invoke:

```bash
python3 /opt/platform/gerrit-tekton/tekton_gerrit_bridge.py \
  --bundle-dir /opt/platform/gerrit-tekton \
  --contract-sha256 "$REVIEWED_CONTRACT_SHA256" --poll-once
```

`REVIEWED_CONTRACT_SHA256` is the byte hash of the original
`gerrit-event-contract.json`. `--watch` repeats discovery at the reviewed interval;
`--event-file` selects one saved event instead. Packaged services invoke `--watch`.
Public output contains counts/flags, not events, names, revisions, URLs, tokens
or raw API diagnostics. Changing credentials/target requires a service restart.

## Source checks and retained write handles

Discovery paginates the fixed project/branch query, rejects duplicate change
numbers and refuses an incomplete inventory exceeding `max_changes`. A cycle
reads its complete bounded inventory before submitting. Polling covers current
visible open patch sets eventually; it does not audit every intermediate
revision or prove completeness of changes invisible to the reader. Concurrent
changes can stop a stale cycle; the supervisor may restart discovery.

Each identity selects a deterministic PipelineRun name scoped to the exact
package. The inline job fixes checkout/build commands and images; source code
cannot select another Pipeline, namespace, command or target. It fetches the
exact Gerrit `refs/changes/NN/change/patchset` into a fresh local ref, verifies
its object ID against the API commit before checkout, and builds that detached
commit. A changed or wrong ref cannot execute some other fetched revision.

Before reserving a job, the helper performs server-side dry-run, requires the
exact submitted spec/ownership metadata and rechecks the source. Unexpected
webhook defaults/mutations stop before reservation. Review compatibility rather
than weakening this comparison. API-only acceptance does not exercise Tekton's
mutation/validation webhooks.
The generated inline task includes the exact empty `metadata` object and null
custom-task `spec` serialized by the reviewed
[EmbeddedTask type](https://github.com/tektoncd/pipeline/blob/v1.4.0/pkg/apis/pipeline/v1/pipeline_types.go#L164).
Pipeline timeouts use canonical duration strings, including the hour boundary.
The regular executed TaskSpec is checked separately from those empty wrapper
fields; nonempty wrapper metadata or custom-task content is unsupported.
The request also predeclares `tekton.dev/pipeline` with its own deterministic
run name, matching the
[controller's inline-pipeline labeling](https://github.com/tektoncd/pipeline/blob/v1.4.0/pkg/reconciler/pipelinerun/pipelinerun.go#L1444).
The same label is therefore present before admission and after reconciliation;
unexpected metadata still fails the complete comparison.
Previously rendered packages need a new review and contract; an old contract
does not silently authorize changed helper code or job bytes.

An immutable reservation precedes the run. Only its acknowledged creator may
send the run create. Uncertain writes are observed through original handles;
an existing reservation without a run requires explicit recovery and cannot
silently create another job. An immutable receipt binds reservation UID, exact
run spec and run UID. Duplicate deliveries and garbage collection of a receipted
run do not resubmit. Replaced UIDs, changed specs and missing/changed receipts
are refused. Retain reservations/receipts for the deduplication period; deleting
them all or selecting another contract creates another submission scope.
The source may change after the last read. The job verifies the selected commit;
any eventual result reporter must independently check the current revision.

## Tests and open acceptance

```bash
python3 scripts/test_tekton_gerrit_bridge.py
```

Default checks cover identity, pagination, admission injection, uncertain writes,
duplicate delivery, garbage collection, tampering, five formats and credential
isolation. Linux additionally uses a disposable authenticated loopback HTTPS
review-API surrogate and real Git smart-HTTP backend. It fetches a real
Gerrit-shaped change ref, runs the checked-out application test and rejects a
different expected commit. This is not a Gerrit server or Tekton worker.

The optional `GerritTests.test_actual_kubernetes_api_submission_and_replay` uses
`TEKTON_GERRIT_TEST_API=1`, pinned envtest binaries in `ROOK_BOOTSTRAP_TEST_BIN`
and the upstream v1.4.0 PipelineRun schema at `TEKTON_GERRIT_TEST_CRD`. Its pinned
SHA-256 is `1f7a7d78a0fa4019cdd262aaa6d6260752fd60f8bf10e526959d2989fa79ee35`;
source: [upstream CRD](https://github.com/tektoncd/pipeline/blob/v1.4.0/config/300-crds/300-pipelinerun.yaml).
Only the original v1 structural/CEL schema is used, with fixture conversion
`None`, because no Tekton webhook runs. The actual isolated API checks scoped
RBAC, admission/create, a lost acknowledgment, original/replaced UIDs and
retained receipts. It fabricates no completed worker status.
`TEKTON_GERRIT_TEST_REPORT` may select a new ignored private aggregate receipt.

The separate optional [result reporter](TEKTON_GERRIT_RESULTS.md) now prepares
verified-child evidence checks and revision-specific Verified vote writes. It
uses another credential/identity and preserves the original submission scope.

The separate [K3s worker case](K3S_VM_WORKERS.md) has passed native Gerrit
discovery, exact-ref worker checkout, two application tests and evidenced vote
read-back using these helpers. It additionally verifies scoped Secret denials,
submission/reporting replay, stale-patch-set refusal and an actual deliberate
build failure followed by a negative native Gerrit vote. This manual helper
execution does not deploy a continuous bridge service.

Fleet Gerrit compatibility, continuously deployed submission/reporting,
guarded fleet workload installation, upgrades, restores, load and
physical-failure acceptance remain required. Those flags stay
false in preparation/submission receipts. The submitter itself performs no
Gerrit review mutation, Verified vote or CD deployment.
The separate [combined worker handoff case](K3S_VM_WORKERS.md) has now passed
actual worker tests, native voting, exact merged-commit promotion, Argo CD
reconciliation and HTTP serving on two workers. It uses manually invoked
helpers; it does not establish a continuously deployed bridge or fleet HA.
The further [native watch-service case](K3S_VM_WORKERS.md) has now passed
packaged submitter/reporter entry-point execution as actual transient systemd
services. It verifies automatic patch-set discovery, actual worker builds,
native positive/negative voting and restart/replay without duplicate runs or
reviews. Private identity/mount mappings and cleanup are checked separately.
This establishes bounded laboratory polling and restart behavior, not ordinary
fleet installation with the default static accounts or a combined automatic
deployment handoff.
The subsequent joined native case now connects those services to the packaged
promoter CLI and actual Argo CD HTTP serving. It verifies dedicated CI-read
permissions, unmerged rejection, read-only preflight, explicit promotion and
replay under a third non-root identity. The worker runbook records its separate
339-source receipt and cleanup audit. Review/merge and release promotion remain
explicit decisions; fleet installation and unattended release policy are open.
