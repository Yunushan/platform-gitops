# Optional Gerrit artifact promotion

This explicitly invoked option connects [artifact-aware Gerrit CI](GERRIT_ARTIFACT_CI.md) to a separate deployment branch and an existing Argo CD Application. It requires the exact tested source revision to be merged and still the source branch head, and rechecks the original worker, artifact, vote and immutable receipt evidence using the existing [Gerrit handoff authorities](GERRIT_GITOPS_HANDOFF.md). An open change with a positive vote does not authorize promotion.

## Explicit policy

Start with the existing private Gerrit handoff plan. Change its `schema_version` to `2` and add:

```json
{
  "artifact_policy": {
    "manifest": "deploy/staging/workload.json",
    "namespace": "application",
    "destination_server": "https://192.0.2.2:6443",
    "deployment": "application",
    "deployment_uid": "99999999-9999-4999-8999-999999999999",
    "container": "app",
    "commit_name": "Artifact writer",
    "commit_email": "cd-writer@example.invalid",
    "expected_image": "registry.example.invalid/application@sha256:<64 lowercase hex characters>",
    "expected_deployment_revision": "<40 lowercase hex characters>"
  }
}
```

Replace every example reference with reviewed private values. Pin the original live Deployment UID, exact source-manifest image and current deployment-branch commit. The destination server must exactly match the scoped CD kubeconfig endpoint; the tool verifies that endpoint and the pinned kube-system UID. Aliases are not silently equated, and native controllers may use their explicit verified API endpoint. A new branch requires both `expected_deployment_revision: null` and `allow_create_branch: true`. Updating the expected revision for a later promotion is an explicit operator policy change. Do not automatically copy an unexpected live tip into the policy.

The selected Argo source directory must contain only this regular, single JSON `apps/v1` Deployment manifest. Its explicit namespace/name and uniquely named container must match the policy. Symlinks, extra manifest files, duplicate JSON keys, nested paths and image drift are refused. The Application must use a plain directory source, the same HTTPS repository, the selected deployment branch and the reviewed destination namespace and exact CD management endpoint. Helm, Kustomize, directory overrides, plugins, multi-source applications and destinations different from the verified CD management target are outside this option. Other resources, such as an existing Service, need their own managed configuration.

## Promotion transaction

The promoter reads the approved source tree without checking out or executing it, changes only the named container image to the original CI receipt's digest URI, and writes a deterministic Git commit. All other JSON values and source files remain as approved. The deployment commit differs from the tested source commit: its parents retain that source revision and the explicitly reviewed previous deployment revision. The resulting tree comes from the approved source; previous deployment-branch-only changes are not merged into it.

The commit message binds the source revision, original PipelineRun UID, CI evidence hash, complete plan hash, previous deployment revision and artifact receipt. Commit identity is explicitly pinned to the dedicated writer account name and registered email; its timestamp is the approved source commit time. The same policy/evidence yields the same commit, allowing an exact replay to finish without a second push.

Before a push, the tool rechecks the original CI/vote/merged-head evidence, Application spec and live Deployment UID. The target contains the previous deployment commit as an ancestor. A Git lease additionally compares the exact reviewed previous tip, so a concurrent update is refused rather than overwritten. This guard does not grant server-side history-rewrite permission: keep the writer restricted to ordinary updates of the selected deployment branch. Because the generated commit can have two parents, the writer also needs the release-appropriate `Push Merge Commit` permission alongside ordinary `Push`. The tested Gerrit 3.13.10 WAR bundles documentation requiring the exact prefixed ref `refs/for/refs/heads/<deployment-branch>` even for direct merge pushes. Newer Gerrit documentation describes direct merge grants on `refs/heads/<deployment-branch>`; use the installed release's documentation and verify effective permissions. Review inherited broad `refs/for/` merge grants as well: an additional narrow allow does not remove an inherited grant. Keep creation separately scoped if authorized; do not grant force, deletion, source-branch writes, or identity-forging rights to work around a rejection. Use a commit email registered to that writer account. These distinctions follow [Gerrit's access controls](https://gerrit-review.googlesource.com/Documentation/access-control.html#category_push_merge). There is one push attempt; uncertain writes are read back, with no mutation retry or automatic rollback. Retain the private journal to investigate a partial result.

Success requires that the same branch still selects the generated deployment commit, the original Argo Application reports that exact revision as Synced and Healthy with a successful operation, and the original Deployment observes its current generation with the exact image and all desired replicas updated, ready and available. Zero replicas, stale generations, replacement/deleting workloads and ReplicaFailure are refused. This rollout check does not independently establish kubelet image identities, signed-image admission, service reachability or HA.

## Private packaging and invocation

Render using the existing version 2 CI reporter package:

```bash
python3 scripts/render_gerrit_artifact_promotion.py \
  --bundle-dir private/generated/gerrit-tekton-reporter \
  --plan private/gerrit-artifact-promotion.json \
  --output-dir private/generated/gerrit-artifact-promotion
```

The source hosting selection can be `rke2`, `k3s`, `k8s`, `docker` or `raw`; CI and CD still use explicitly pinned Kubernetes targets. Rendering does not access credentials or servers and writes only a new ignored private package. Preserve the resulting `gerrit-artifact-promotion-contract.json` byte hash. The legacy handoff continues to refuse version 2 CI plans.

`render_gerrit_promotion_access.py` accepts this new package with its existing separate CI/CD access plan. It adds only `get` on the exact named Deployment in the workload namespace to the CD reader. It does not grant workload, Secret or Application write privileges. Review and apply the two target-specific RBAC manifests through the existing operator workflow. Access rendering alone does not install them or prove effective authorization.

Keep the separate read token, deployment-branch writer token, original CA and static scoped CI/CD kubeconfigs at `/run/platform/gerrit-promoter` as described in the existing handoff runbook. Run the packaged helper on the selected Linux control host:

```bash
python3 /opt/platform/gerrit-promoter/gerrit_artifact_promotion.py \
  --bundle-dir /opt/platform/gerrit-promoter \
  --contract-sha256 <reviewed-package-sha256> \
  --pipeline-run <original-run-name>
```

The default mode checks only local package inputs. Add `--check-server --output-dir <new-private-proof-directory>` for remote read-only preflight, including local commit preparation. An explicit `--apply` instead of `--check-server` authorizes the branch update and waits for controller/rollout verification. Preview includes the prepared commit and image; review it before apply. Preflight writes local temporary Git objects and the private journal, but makes no remote mutation.

Contract tests cover authority and evidence refusals, all five package formats, exact scoped RBAC, rollout gates, CLI privacy and replay. Actual offline Git tests cover tree/object creation, ancestry, exact replay, create-once branches, unsafe manifest refusal and a genuinely raced push refused by the lease. CI/API/controller objects in those portable tests are simulated; these tests do not qualify a live Argo promotion, native service installation, full stack or physical HA. A separately supervised isolated Gerrit 3.13.10 HTTPS component check also verifies the generated commit, denial before the exact prefixed merge grant, scoped push acceptance, source-branch/history-rewrite refusals and exact replay. That check uses a synthetic artifact input, with CI and controller acceptance explicitly false. The separate combined worker case described below now exercises the real artifact-CI-to-Argo path; complete production and HA qualification remain open.


## Combined component runtime acceptance

The opt-in `test_valkey_gateway.GatewayTests.test_real_gerrit_artifact_ci_to_argocd` case, gated by `GERRIT_ARTIFACT_PROMOTION_WORKER_TEST_RUNTIME=1`, runs real Gerrit discovery, the scoped submitter/reporter, three ordered Tekton worker steps, restricted-robot Harbor publication, a positive vote, explicit merged-source promotion and the original native Argo CD repository/controller processes. The build runs three actual tests, including the exact reviewed Deployment input. The fixture refuses an unmerged promotion, proves preflight makes no remote update, observes the generated commit and exact digest rollout, and verifies replay makes no second push.

It retains original PipelineRun/TaskRun/Pod and vote evidence, artifact receipt, promotion policy/contract/journal, original Application and Deployment, ReplicaSets, Pods and Service snapshots in the ignored validation directory before teardown. The API snapshots show Argo Synced/Healthy at the generated deployment commit, the original Deployment UID, and ready pods on two actual workers with the produced manifest digest as their image identity. HTTP checks on both workers verify image-contained output; no content ConfigMap supplies that output. The application pull identity is project Guest and distinct from the publisher robot. Separate CI/CD reader identities are checked for forbidden writes, Secret access and unrelated reads.

Use the prepared [bounded KVM worker laboratory](K3S_VM_WORKERS.md) and existing Harbor/Gerrit/Tekton cached artifacts; this case is not enabled by ordinary validation. It additionally requires `ARGOCD_TEST_ARTIFACTS` pointing to the checksum-verified native Argo files and `ARGOCD_TEST_VALKEY` to the pinned TLS cache binary. Keep the foreground supervisor, finite resource/deadline limits, source hash snapshot and original process/cgroup identity through terminal cleanup. Rendering the package or setting only the new test flag does not provision those prerequisites.

This is a local protocol/component acceptance case using temporary local storage and the Harbor pool-health candidate. The direct variant invokes the real promotion functions with scoped live clients. The additional packaged native CLI variant below runs four restricted transient services. Persistent service installation and reboot recovery remain separate work. The additional continuous polling variant below checks native watch coordination. The Service and pull Secret are separately prepared operator resources, with their original identities checked; the GitOps source manages only the Deployment. Failed-build/skipped-publication checks remain in the separate artifact CI case. Signed-image admission, Ceph/failure-domain recovery, physical HA and all stack/runtime combinations remain unqualified.


## Packaged non-root promoter CLI variant

The separately gated `test_valkey_gateway.GatewayTests.test_real_gerrit_artifact_native_cli_to_argocd` case uses `GERRIT_ARTIFACT_NATIVE_PROMOTION_WORKER_TEST_RUNTIME=1` with the same prepared bounded laboratory and artifact prerequisites. It retains the direct and legacy variants; ordinary validation does not start the laboratory.

This variant runs the actual packaged `gerrit_artifact_promotion.py` entry point in four owned systemd services: unmerged refusal, server preflight, apply and replay. Each service uses a dedicated dynamic non-root user, `LoadCredential` inputs, read-only helper and credential binds, a private temporary directory, a protected host filesystem, no capabilities or privilege escalation, and the original isolated network namespace. Finite memory/task/deadline limits remain explicit. The native CLI freezes its scoped CI/CD kubeconfigs itself; the harness does not replace the CLI's input paths or verification functions.

A concurrent read-only observer records the original Python PID, boot/start identity, systemd invocation and cgroup, UID, privilege flags and network namespace. Journals must be private and owned by that actual non-root identity. The fixture checks unmerged failure at the source-state gate, read-only preflight, a distinct generated deployment commit, actual Argo/Deployment reconciliation and no-push replay. Every original native process/cgroup is removed before the invocation returns. The combined acceptance supervisor then verifies guest/native-scope cleanup and unchanged first-party Python hashes.

`promotion-native-runs.json` retains all four original identities, results and journals beside the other ignored runtime evidence. This establishes the packaged CLI and restricted transient-service path for the tested local component, with the exact same CI-produced digest served on both workers. It does not establish persistent installation, watch-service coordination, restart recovery, signed-image admission, physical HA or a complete stack/runtime qualification. Use the separate watch variant for bounded native polling and restart checks.

## Native polling, restart and artifact promotion variant

The independent `test_valkey_gateway.GatewayTests.test_real_gerrit_artifact_native_watch_to_argocd` case is gated by `GERRIT_ARTIFACT_NATIVE_WATCH_WORKER_TEST_RUNTIME=1`. It requires the same prepared laboratory and artifacts as the packaged CLI case. Ordinary validation does not start services or virtual machines.

The fixture starts the actual rendered bridge and reporter `--watch` entry points as distinct restricted non-root systemd services. It waits for automatic submission of the current open change, actual worker publication and the reporter's positive vote. Before merging, it kills each original watcher process and verifies one automatic restart, a new process/invocation, the original PipelineRun reservation and UID, duplicate submission refusal and already-reported vote handling. Checking replay before merge preserves the bridge's open-change discovery policy.

The explicit promotion then runs the four packaged native promoter modes while both restarted watchers keep their original identities. The promoter cannot share either watch identity. The same CI receipt, generated deployment commit, original Argo Application/Deployment and two-worker digest/HTTP checks remain required. A merged change can subsequently be rejected by the reporter's open-change policy; that is not evidence of a failed build or a negative CI vote.

`native-watch-runs.json` retains the initial and restarted Python PID/start/boot, systemd invocation/cgroup, UID, privilege/network checks and poll summary counts. It binds those records to the original application PipelineRun and CI evidence hash. Original process identities and both watch cgroups must be absent at cleanup; a reused PID alone is not treated as a surviving original process. Credentials and raw watcher logs are not copied into this proof.

The watchdog remains finite and the service memory/task and privilege boundaries remain unchanged. These are transient local polling/restart checks, not persistent host installation, reboot/restore recovery, webhook delivery, missed-event recovery, HA, signed-image admission or complete stack/runtime qualification. The separate artifact CI case retains failed-build/skipped-publication checks; this merged positive path does not claim those checks.


## Optional signed CI evidence gate

Promotion schema `3` keeps the existing schema `2` artifact policy and adds a required `signature_policy`. That object contains exactly `cosign_binary`, `registry_ca`, `public_key`, `trusted_root` and `credential`; each has an absolute file `path` and reviewed `sha256`, using the [signature verifier contract](IMAGE_SIGNATURE_VERIFICATION.md). The approved release public key is distinct from the CI publisher robot and from the deployment writer. The promoter receives no signing key or registry write credential. Version `2` packages keep their original helper list and do not require Cosign.

After checking the live original CI run, published positive review, exact merged revision and current source head, the promoter derives eight mandatory signed annotations:

- `platform.gitops/source-identity-sha256`: canonical source project/branch/API/clone identity hash.
- `platform.gitops/source-revision`: exact tested and merged source commit.
- `platform.gitops/pipeline-run-uid`: original PipelineRun UID.
- `platform.gitops/ci-evidence-sha256`: canonical verified live CI evidence hash.
- `platform.gitops/bridge-contract-sha256`: reviewed source/CI contract hash.
- `platform.gitops/reporter-contract-sha256`: reviewed CI reporting contract hash.
- `platform.gitops/promotion-policy-sha256`: canonical promotion plan excluding `signature_policy`.
- `platform.gitops/image-sha256`: SHA256 of the exact full image reference bytes.

Project names and URLs are hashed before annotation creation. Refreshing a pull token does not change the signed release-policy identity. Each particular verification operation still pins that credential file and the selected public trust inputs. The image digest in the signature, image-reference hash and source/CI/policy annotations must all match; signing the same content for a different CI run or deployment policy is insufficient.

An independent release authority must obtain and review these values from the same live `verified_ci` operation, then sign the exact image with all eight annotations and its separately protected release key. `signature_annotations` derives the values from that operation's `data` and `evidence`; it is not a substitute for verifying those inputs. The promoter only verifies an existing signature. It never creates one, resets credentials or accepts a caller-provided success receipt as authorization.

Signature verification is required before any temporary Git preparation. Apply rechecks live CI/source/CD state and performs fresh cryptographic verification before the single deployment-branch push. Missing/untrusted signatures, changed evidence, changed public trust, refused transparency verification or unavailable registry access stop before that write. The journal retains the verification hashes; the result reports `merged_ci_signature_verified`. `signed_image_admission_verified` and `full_stack_verified` remain false.

Portable tests cover the versioned CI evidence reader with simulated API objects, package integrity, annotation derivation, changed run/source/image/policy, pre-Git refusal, pre-push re-verification and partial-proof refusal. The Harbor/Cosign/Rekor protocol test uses a fixture image to exercise correct, wrong and missing signature bindings.


## Actual signed native promotion variant

`test_valkey_gateway.GatewayTests.test_real_gerrit_signed_artifact_native_cli_to_argocd` is gated by `GERRIT_SIGNED_ARTIFACT_PROMOTION_TEST_RUNTIME=1`, with the existing actual Gerrit/Tekton/Harbor/Argo/KVM artifacts and the signature tool variables described in the [signature protocol runbook](IMAGE_SIGNATURE_VERIFICATION.md). It keeps the original native promoter's privilege, memory/task, namespace and identity boundaries.

This variant requires the original CI image receipt and positive vote, merges the reviewed exact patch set, reads the live merged CI evidence, and creates a release signature containing the eight derived bindings. Signing, log and standalone verification use separate numeric identities. The native promoter receives only public trust and the repository's pull-only token, uses an additional distinct DynamicUser identity, and performs the real Cosign verification during read-only preflight, apply and replay. The original unsigned pre-merge invocation remains a separate refusal check. The CI publisher's original `publisher_signature_verified` stays false: the separate release authority signs after merge.

The runtime checks that Argo rolls out the same signed CI digest on two workers, each serving the built content. Later Harbor peer-loss and cache/database promotion phases verify the original signature and embedded log proof with the log stopped. `signed-ci-binding.json` retains the live evidence/annotation/promotion binding privately; native journals retain cryptographic verification hashes. The acceptance gate requires all original native processes/cgroups and the signing-log process to be removed.

An isolated RKE2 v1.36.4+rke2r1 run with three control-plane guests and two
dedicated workers passed the infrastructure PipelineRun, then checked out the
actual Gerrit patch set, built and published its image to Harbor, and verified
the CI artifact receipt before posting the successful review vote. After merge,
the release authority signed the image with all eight bindings. The native
promoter verified the signature and transparency-log proof, pushed the
deployment branch once, and replayed without another push. Argo CD rolled out
the exact digest, and both worker replicas served the built content. The
retained pipeline, task, pod, vote, deployment and cleanup evidence passed
independent verification.

The two dedicated workers import the verified OCI archives by canonical
digest reference. The bundled containerd's `--digests` import option created
synthetic aliases that prevented the Tekton Pods from starting. The corrected
import passed CRI lookups by both reference and image ID for all nine cached
Tekton images. The rehearsal provisions Harbor's private CA through RKE2
registry configuration before the agents start. [RKE2 private registry configuration](https://docs.rke2.io/install/private_registry).

### RKE2 signature admission

A second isolated RKE2 run used three Kyverno admission replicas on separate
nodes and zones. Kyverno enforced fail-closed policies before promotion. It
denied 12 server-side dry runs covering an unsigned image, a wrong signing key,
each of the eight changed signature bindings, and unsigned init and ephemeral
containers. Argo CD rolled out the signed CI digest on the two original
application workers. The retained evidence verified the image IDs, a single
deployment-branch push across apply and replay, and cleanup of the isolated
lab. The longer rehearsal uses the existing finite 30-minute test credentials
to cover image staging, policy checks, and promotion.

Production credential provisioning, fleet signing, reboot and restore, physical
failure testing, and Ceph-backed Gerrit HA remain open.

After a confirmed Git push, controller and rollout observations may retry bounded
API transport timeouts, connection failures and temporary service unavailability
within the original deadline. Each observation rechecks the original controller
identity, source scope and deployment revision. The Git push is never repeated
by this observation loop. Authorization, TLS verification, identity, invalid-data,
signature and rollout failures remain failures; an expired deadline retains the
promoted ref and does not trigger automatic rollback.

Post-push read observation distinguishes a TLS negotiation timeout from peer
certificate or protocol failures. Only an exact timeout with no other TLS,
certificate, authentication, authorization or validation signal can use the
existing bounded read retry. Mixed failures remain immediate errors. TLS
verification, the original deadline, single Git push and no-rollback behavior
remain unchanged.
