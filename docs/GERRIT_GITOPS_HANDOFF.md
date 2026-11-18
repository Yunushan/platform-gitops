# Merged Gerrit CI revision handoff to Argo CD

This optional, explicitly invoked adapter connects the
[Gerrit/Tekton submitter](TEKTON_GERRIT_BRIDGE.md) and
[result reporter](TEKTON_GERRIT_RESULTS.md) to the existing
[GitOps branch handoff](CI_GITOPS_HANDOFF.md). It advances a separate deployment
branch in the same repository. It changes no Argo CD specification and has no
Kubernetes workload-write permission.

The tested revision must be the exact current patch-set commit of a **merged**
Gerrit change and the current source branch head. A positive review of an open
change is insufficient. A cherry-pick, rebase or merge commit with another SHA
is refused; the actual resulting head needs its own supported CI proof path.
This adapter currently covers merges preserving the tested commit, including
the explicitly configured fast-forward-only real fixture. It does not silently
equate ancestor/review success with testing a different merge result.

All five source hosting formats are preserved. As with the existing handoff,
Docker/native hosting still uses explicit CI/CD Kubernetes targets for the
Kubernetes-workload part. It does not update native services or Docker hosts
over SSH. Complete multi-runtime stack acceptance remains open.

## Required authority and evidence

Prepare the original submission package and separately reviewed reporter package
first. Keep the successful PipelineRun, TaskRun, Pod, worker identities and
immutable submission/publication receipts until handoff completes. The adapter
rechecks the actual CI evidence, original UIDs, checkout/ref parameters, commands,
images, exits and credential boundaries. A status string or copied vote alone
does not authorize a deployment.

It verifies the exact original reporter reservation, publication receipt,
tagged message, bot identity and detailed non-copied `Verified +1` vote against
the current evidence digest. The source read account is separately pinned by
username and numeric account ID. Its permissions must allow change/head and
inherited-label inspection, including read-only `refs/meta/config` access on
each relevant ancestor. It needs no Gerrit voting privilege.

Project label policy remains committed by the reporter's definition hash.
Site-wide copying policy is a separate deployment requirement described in the
reporter runbook. Even if other review automation copies votes, this handoff
requires its own exact CI commit, original publication proof and current merged
head; it does not authorize a different revision from a label alone. Gerrit's
own merge/submit requirements and access controls still require independent review.

Use a third, dedicated Git writer with authority limited to the selected
deployment branch, without force-push or source-branch authority. The CI job,
reporter bot and source reader must not receive this write credential. Protect
the source/deployment branches against unauthorized updates, deletion and
history rewriting using Gerrit's actual permissions.

## Prepare the private package

Copy [the example](../examples/ci-gitops-handoff-gerrit-argocd.json) into ignored
`private/` storage. Replace every placeholder, including account ID zero with
the original positive numeric reader ID. Commit the exact original submission
and reporter contract byte hashes, runtime, deployment branch and existing Argo
CD Application's cluster UID, object UID and canonical full-spec digest.

The Application must use the same HTTPS Git URL, selected deployment branch and
manifest directory, with automated synchronization enabled. Its full spec binds
the project, destination and rendering settings. Multi-source/chart applications
are refused for this path. Pruning stays disabled unless the plan explicitly
authorizes its already reviewed controller policy.

```bash
python3 scripts/render_gerrit_gitops_handoff.py \
  --bundle-dir private/generated/gerrit-tekton-reporter \
  --plan private/gerrit-gitops-handoff.json \
  --output-dir private/generated/gerrit-gitops-handoff
```

The output preserves both original contracts and adds a separate
`gerrit-handoff-contract.json`. Rendering is create-only/private and performs
no credential reads, server operation, branch update or deployment.

Prepare a separate private directory at `/run/platform/gerrit-promoter`:

- `api-token`: native Gerrit token of the pinned source reader.
- `token`: native HTTPS Git token of the dedicated deployment-branch writer.
- `ca.crt`: exact source CA bytes pinned by the submission contract.
- `ci-kubeconfig`: static, scoped read access to original CI evidence/receipts,
  namespace/cluster identities and worker Nodes. No job/receipt writes.
- `cd-kubeconfig`: static, scoped read access to the selected Application and
  CD cluster identity. No Kubernetes patch/apply/delete operations.

Both contexts/cluster UIDs are verified, and local configuration snapshots
freeze credentials/targets for the process. Dynamic credential plugins are
refused. Native source reads use verified TLS, no ambient proxy, bounded
responses and a fixed GET-only resource scope. Credentials belong in approved
private delivery, never a public plan, image, build step or command argument.

## Optional promotion reader access package

The separate [access renderer](../scripts/render_gerrit_promotion_access.py)
prepares Kubernetes identities for the two read-only kubeconfigs above. Copy
[the access example](../examples/gerrit-promotion-access.json) into private
storage. Set the CI account's namespace to the original submission control
namespace, inventory the exact CI worker Node names, and place the CD account
in the reviewed Application namespace. Use new dedicated account names; the
renderer rejects known job, submitter and reporter identity reuse.

```bash
python3 scripts/render_gerrit_promotion_access.py \
  --bundle-dir private/generated/gerrit-gitops-handoff \
  --contract-sha256 "$REVIEWED_HANDOFF_CONTRACT_SHA256" \
  --plan private/gerrit-promotion-access.json \
  --output-dir private/generated/gerrit-promotion-access
```

This is local, create-only rendering. It loads the original handoff and both
embedded CI contracts, validates their pinned file hashes, and writes:

- `ci-reader-rbac.json`: ServiceAccount in the original control namespace;
  get-only PipelineRun, TaskRun, Pod and receipt ConfigMap access in the original
  job namespace; get-only access to the named cluster/job namespaces and
  explicitly inventoried worker Nodes.
- `cd-reader-rbac.json`: a separate ServiceAccount, get-only access to the one
  reviewed Argo CD Application, and get-only cluster identity access.
- `promotion-access-contract.json`: the original handoff byte hash, access
  policy, two original management targets and exact manifest hashes.
- `access-status.json`: prepared only; applied, credentials-created,
  authorization, controller and full-stack verification remain false.

Every object records its intended cluster UID. These annotations are review
metadata, not Kubernetes enforcement of the target cluster. The two manifests
remain separate even when CI and CD currently use the same cluster. Never
concatenate and apply them against an assumed shared context. An administrator
must first compare each explicit target's actual `kube-system` UID to the
contract, review the manifests and provision them on their respective clusters.
No Namespace, Secret, token, kubeconfig or workload is generated or applied.

Use the exact reviewed access-contract hash with the renderer module's
`load_bundle(access_directory, access_digest, handoff_directory, handoff_digest)`
before provisioning. It verifies both packages and regenerates the manifests;
rehashing a broadened Role does not make that Role valid. Keep the reviewed
hash outside the generated directory. The default handoff package remains
unchanged when this optional access package is absent.

Provision short-lived scoped tokens through an approved private channel and
deliver the resulting static kubeconfigs separately. Kubernetes permissions are
additive: creating these Roles does not remove grants an existing account
already has. Reject unexpectedly existing identities/bindings and check actual
effective access. Readers must fail Secret access, receipt/job/status mutation,
unlisted Node access, and listing or reading unrelated Applications. CI readers
need named object GETs, not list/watch; the promoter already knows the original
run and follows its retained child references. Application observation never
needs patch/apply/delete. Worker names bound RBAC scope; rendering alone does
not verify live Node UIDs, placement, health or availability. Runtime evidence
checks still verify those properties separately.

## Inspect, preflight and apply explicitly

```bash
python3 scripts/gerrit_gitops_handoff.py \
  --bundle-dir private/generated/gerrit-gitops-handoff \
  --contract-sha256 "$REVIEWED_HANDOFF_CONTRACT_SHA256" \
  --pipeline-run '<ORIGINAL_SUCCESSFUL_PIPELINE_RUN_NAME>'
```

Without a mode flag this inspects only local reviewed files. It creates no
proof directory, reads no credentials, contacts no server and pushes nothing.

Use `--check-server --output-dir private/handoff-check-001` for explicit server
reads and temporary Git fetches. Use `--apply` instead for the branch update
and reconciliation wait. Server modes require Linux and a new private output
directory. Existing journals are never overwritten.

Before a push, CI/review/merge/head evidence and Argo CD UID/spec/source/policy
are checked again. The selected manifest path must be a tree in the exact
tested commit, and the existing deployment tip must be its ancestor. Branch
creation requires explicit `allow_create_branch`; no ref is forced, pruned or
deleted. The command re-reads the deployment ref after an uncertain push.

The same Application must then report Synced/Healthy and a successful operation
for that exact commit. A changed deployment branch needs reconciliation after
the attempt began. These are recorded controller outcomes, not independent
application health, load, HA or restore acceptance. A different deployed commit
or changed controller identity/spec stops verification.

The private journal records committed plan/contract hashes, original run UID,
evidence digest, previous/promoted revisions and last phase. If Git advances
but reconciliation fails/times out, the branch stays advanced. There is no
automatic rollback, controller patch or blanket sync/prune. Retain the journal
and evidence; retry only while the same commit remains eligible. An already
promoted commit is observed idempotently and never rewound. Concurrent authors
are not frozen; ordinary Git fast-forward rules protect the branch from rewind.

## Verification scope

`python3 scripts/test_gerrit_gitops_handoff.py` checks unmerged/abandoned reviews,
changed merge/source commits, missing worker/receipt evidence, altered/copied
votes, source-reader drift, pre-push changes, Application policy/identity drift,
five formats, package tampering and local no-network inspection. Shared
Forgejo/Gitea/Flux handoff tests remain applicable to the common FF/CD mechanics.
The same test file checks promotion-access scopes, known-writer identity reuse,
distinct CI/CD targets, invalid worker inventories, tampered or rehashed broad
roles, changed source packages, and create-only private CLI rendering.

The joined [native CLI/worker acceptance](K3S_VM_WORKERS.md#native-polling-through-explicit-promotion-cli)
also provisions the generated reader roles on its isolated real cluster,
regenerates the full access package from the exact handoff, compares all ten
objects against live ServiceAccounts/Roles/bindings, and requires effective
denials for writes, Secrets and unrelated reads. Its accepted 2026-11-08 run
completed in 455.406 seconds with 340 source-file hashes, actual worker tests,
native Gerrit votes, explicit merge/promotion and Argo CD HTTP serving. Rendering
status remains prepared-only. This fixture does not certify separate physical
CI/CD clusters, production credential delivery, fleet installation or full-stack
HA acceptance.

The explicit `HandoffTests.test_real_gerrit_merged_commit_handoff` case uses
`GERRIT_HANDOFF_TEST_RUNTIME=1` and the pinned inputs from
[real Gerrit protocol acceptance](GERRIT_REVIEW_RUNTIME.md). It creates a
synthetic review, manifest tree and dedicated reader/writer grants, refuses an
unmerged promotion, merges with the exact tested SHA and checks actual HTTPS Git
preflight, deployment-branch fast-forward and idempotence. CI/controller/worker
and Argo CD status remain modeled. The receipt therefore keeps actual Argo CD
reconciliation, Tekton execution and full-stack/HA flags false.

Run that opt-in fixture in a new network namespace, using the reviewed public
artifacts already described in the protocol runbook:

```bash
sudo env \
  GERRIT_HANDOFF_TEST_RUNTIME=1 \
  GERRIT_REVIEW_TEST_ARTIFACTS=/absolute/private/cache/gerrit/3.13.10 \
  GERRIT_REVIEW_TEST_JAVA_ARCHIVE=/absolute/private/cache/OpenJDK21U-jre_x64_linux_hotspot_21.0.13_9.tar.gz \
  /usr/bin/unshare --net -- \
  python3 -B scripts/test_gerrit_gitops_handoff.py \
  HandoffTests.test_real_gerrit_merged_commit_handoff
```

It has no route to an existing deployment. Its synthetic Gerrit server and Git
credentials are disposable. An optional new `GERRIT_REVIEW_TEST_REPORT` path
under this checkout's ignored `private/` directory receives the source-bound
receipt only after server/scope cleanup. Freeze Python sources during the run.

The separate opt-in
`HandoffTests.test_real_gerrit_handoff_and_argocd_reconciliation` case adds
native Argo CD 3.5.0 repository/application-controller processes, a disposable
Kubernetes 1.35 API/etcd pair and the pinned TLS-enabled Valkey 8.1.11 fixture
binary. It is an actual reconciliation test rather than a substitute status
writer. The fixture creates one restricted AppProject, one Application and an
explicit destination with namespace-scoped ConfigMap permissions. The promoter
has only Application read access; both authorization inspection and a denied
server-dry-run patch check its Kubernetes write boundary.

```bash
sudo env \
  GERRIT_ARGOCD_TEST_RUNTIME=1 \
  GERRIT_REVIEW_TEST_ARTIFACTS=/absolute/private/cache/gerrit/3.13.10 \
  GERRIT_REVIEW_TEST_JAVA_ARCHIVE=/absolute/private/cache/OpenJDK21U-jre_x64_linux_hotspot_21.0.13_9.tar.gz \
  ARGOCD_TEST_ARTIFACTS=/absolute/private/cache/argocd/v3.5.0 \
  ARGOCD_TEST_VALKEY=/absolute/private/cache/valkey/8.1.11/tls/valkey-server \
  ROOK_BOOTSTRAP_TEST_BIN=/absolute/private/cache/envtest/v1.35.0/extracted/controller-tools/envtest \
  /usr/bin/unshare --net -- \
  python3 -B scripts/test_gerrit_gitops_handoff.py \
  HandoffTests.test_real_gerrit_handoff_and_argocd_reconciliation
```

The Argo CD binary and `install.yaml` schema hashes are committed in
`test_argocd_identity_runtime_fixture.py`; the envtest hashes are in
`test_rook_api_fixture.py`. The native repository server also needs the exact
upstream [Git verification wrapper](https://raw.githubusercontent.com/argoproj/argo-cd/v3.5.0/hack/git-verify-wrapper.sh)
and [GPG wrapper](https://raw.githubusercontent.com/argoproj/argo-cd/v3.5.0/hack/gpg-wrapper.sh)
in its artifact directory. Their hashes are committed in
`test_argocd_reconciliation_fixture.py`, and the fixture copies those original
scripts into its private executable path, matching the upstream image's
dependency inventory. It does not replace verification with a success stub.
Only schemas are installed from the upstream
manifest. Every target/credential/port is created inside this new fixture;
there is no existing kubeconfig, cluster, forge or mail relay connection.
Controller/repository communication and cache connections use verified TLS
with client certificates; Git uses the fixture CA and read identity. Runtime
logs stay in the private disposable scope and are checked for generated tokens.

A successful run must observe the exact promoted revision through the
production handoff verifier and read the controller-created ConfigMap, its
original UID, expected content and Argo CD tracking annotation. Its scoped
`actual_argocd_reconciliation_verified` flag describes this ConfigMap flow.
The Tekton records remain modeled, and no worker Pods execute; HA, physical
node failure, signed artifacts and complete stack acceptance remain false.
No hand-edited Application status can satisfy this test.

The native ConfigMap regression also passed on 2026-11-07 in 86.913 seconds
after adding support for the separate worker laboratory. Its receipt matched
337 first-party Python files and confirmed native scope/API cleanup; independent
inspection found the original service inactive with main PID zero. Its terminal
log SHA-256 is
`bedede235e223be411a4c8356f25ec36c0780d7ab19745f1bab5c666ff1d9d2b`.

The further optional
`WorkerRuntimeTests.test_real_gerrit_worker_ci_and_argocd_serving_handoff`
case is documented in [the K3s worker runbook](K3S_VM_WORKERS.md). It consumes
real Tekton worker evidence and uses the original private KVM laboratory API
for native Argo CD reconciliation and HTTP workload probes. It has no modeled
CI or Application status fallback. Its scoped deployment role adds only
Services and Deployments to the ConfigMap scope; its AppProject and resource
inclusions retain that same namespace/kind boundary. Its separate accepted run
now verifies three worker tests, exact merged-commit promotion, actual Argo CD
reconciliation and two Ready application Pods with HTTP through both workers.
It also verifies the promoter cannot patch the Application and that replay
does not push again. The worker runbook records its distinct source-bound
receipt, terminal log digest and independent native/VM cleanup inspection.
The ConfigMap receipt above does not certify those additional checks.

The further [joined native polling/promotion case](K3S_VM_WORKERS.md) now
exercises automatic native submission/voting and the actual packaged promoter
CLI against that serving workload in one laboratory. Four separate non-root
invocations verify unmerged rejection, read-only preflight, explicit promotion
and replay. The promoter has its own CI reader with denied receipt creation,
pipeline/status writes and Secret access; the controller identity remains
read-only. Private journal phases, contract hashes, ownership and modes are
checked, and native/VM cleanup is independently inspected. Its accepted receipt
matched 339 Python sources. This verifies operator-approved CLI promotion after
automatic CI; it does not implement unattended release authorization or certify
production service installation.

Continuously deployed event/CI/CD services, publication of signed build
artifacts, other merge strategies, guarded fleet workload
installation, fleet failure/load/upgrade/restore and the complete stack matrix
remain required. Native controller protocol acceptance does not establish
production controller installation or any Kubernetes distribution's readiness.
