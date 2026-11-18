# Promote a Zuul-tested Gerrit revision

The source handoff promotes the tested revision. Use [image promotion](#promote-a-published-image)
to deploy the digest recorded by the trusted post-review publisher.

The Gerrit and Zuul stacks can hand an exact branch revision to Argo CD or
Flux through the [shared promotion workflow](CI_GITOPS_HANDOFF.md). Run the
handoff on a Linux management runner with Git, kubectl, and an explicit
Kubernetes target. The selected RKE2, Docker, native Linux, K3s, or Kubernetes
runtime describes where the application stack is hosted.

## Prepare the build and policy

Configure a trusted Zuul post-review pipeline for updates to the selected
Gerrit branch and attach the real required voting jobs in the tenant's trusted
configuration. After the pipeline finishes, select its buildset UUID and the
exact 40-character branch revision from the Zuul API. The handoff reads the
Zuul 14.3.0 buildset detail and requires one matching branch ref, a successful
buildset, and a completed final success for each required voting job. The
expected evidence shape is a branch buildset with a new revision, no patch-set
identity, and no API enqueuer. The selected Gerrit branch must still point to
that revision when the handoff checks it.
[Zuul buildset detail](https://zuul-ci.org/docs/zuul/14.3.0/rest-api.html),
[Gerrit trigger configuration](https://zuul-ci.org/docs/zuul/latest/drivers/gerrit.html)

Copy [the example](../examples/zuul-argocd-handoff.json) to
`private/zuul-handoff.json`. Fill in the selected origins, tenant, pipeline,
Gerrit project, branch, buildset UUID, tested revision, and required jobs. The
Gerrit clone URL is `https://<gerrit-origin>/a/<project>.git`. Configure the
same URL and deployment branch on the controller object. The deployment branch
must differ from the tested source branch.

Use the original cluster and controller object UIDs and spec digests. For
Flux, use the Kustomization and GitRepository fields described in
[the shared handoff policy](CI_GITOPS_HANDOFF.md). Set
`buildset.zuul_ca_sha256` to the SHA-256 of the approved CA file. After
completing the buildset section, compute the digest for
`handoff.bridge_contract_sha256` from the repository root.

```bash
PYTHONPATH=scripts python3 -B -c '
from pathlib import Path
from bounded_file import read_bounded_text
from ci_gitops_handoff import sha256
from strict_json import loads_strict_json
plan = loads_strict_json(read_bounded_text(Path("private/zuul-handoff.json"), max_bytes=128 * 1024))
print(sha256(plan["buildset"]))
'
```

Mount the reviewed credentials in a private input directory. Use a
tenant-scoped read token for `zuul-token`, the reviewed CA in `zuul-ca.crt`,
and the controller observer's kubeconfig in `cd-kubeconfig`. Use a separate
Gerrit credential in `token` with read access to the source and write access
to the deployment branch, and its HTTPS CA in `ca.crt`. The Zuul API
connection verifies the hostname and certificate and rejects redirects.

## Promote a published image

Start with the build and target settings above. Select the publisher job in
`buildset.required_jobs` and set `publisher_contract_sha256` to the reviewed
`contract_sha256` from the publisher's render status. Pass that bundle's
`playbooks/platform-static-publisher/contract.json` to the promotion command.
Use the example for the controller in your stack. [Argo CD example](../examples/zuul-argocd-artifact-promotion.json),
[Flux example](../examples/zuul-flux-artifact-promotion.json),
[publisher setup](ZUUL_STATIC_IMAGE.md)

Fill in `artifact_policy` with the original Deployment UID, container name,
digest in the reviewed source manifest, existing deployment branch revision,
the writer's Gerrit commit identity and the Kubernetes server in the observer's
kubeconfig. The observer needs `get` on the named Deployment in addition to
the controller objects. Argo CD must use a directory source and the same
cluster and namespace. Flux uses the watched GitRepository and a Kustomization
in the same cluster. Include a `kustomization.yaml` in the selected Git
directory that lists the Deployment JSON as its only resource. Flux's automatic
generation scans YAML files. [Flux manifest discovery](https://github.com/fluxcd/pkg/blob/kustomize/v1.18.0/kustomize/kustomize_generator.go#L481-L510)

Use the same private input files as the source handoff. Server preflight reads
the final voting build and its artifact in one Zuul API response, then prepares
the image change in a temporary Git tree. Apply rechecks the build, source
branch, original Deployment and controller settings. The deployment branch
advances only from its reviewed revision, and an exact replay skips the push.
Success requires the controller to report the new Git commit and the original
Deployment to reach its desired replica count with the selected image digest.

```bash
python3 -B scripts/zuul_artifact_promotion.py \
  --plan private/zuul-artifact-promotion.json \
  --publisher-contract private/zuul-static-image/playbooks/platform-static-publisher/contract.json \
  --check-server --output private/proof/zuul-artifact-preflight
python3 -B scripts/zuul_artifact_promotion.py \
  --plan private/zuul-artifact-promotion.json \
  --publisher-contract private/zuul-static-image/playbooks/platform-static-publisher/contract.json \
  --apply --output private/proof/zuul-artifact-promotion
```

The proof distinguishes Git promotion from workload readiness. If Git advances
and readiness times out, inspect the controller and then retry with the same
plan and a fresh proof directory. The artifact receipt establishes the CI
association.

The local rollout fixture ran both controller paths after one real Gerrit
ref update and an encrypted Zuul publisher job. Argo CD and Flux each advanced
a dedicated deployment branch once, used the same Harbor digest, and kept the
original Deployment UID. Kubernetes reported that digest on pods on both K3s
workers, and the node endpoints returned the exact reviewed bytes. Read-only
preflight and replay also ran through the promotion CLI. The fixture uses
native controllers and disposable KVM guests on one workstation.

To include the rollout in the existing publisher fixture, set
`ZUUL_ARTIFACT_ROLLOUT_TEST_RUNTIME=1` along with its publisher settings. Supply
the pinned K3s guest, QEMU, kubectl, Argo CD, Valkey and Flux artifacts, and a
fresh `K3S_VM_TEST_REPORT` path under `private/validation/`. Run the fixture in
its bounded `platform-k3s-vm-zuul-artifact-…` systemd unit in a fresh network
namespace. [Publisher fixture settings](ZUUL_STATIC_IMAGE.md#local-verification)

### Require release signature verification

For a signature-gated promotion, set the **outer** plan's `schema_version` to
`2` and add `signature_policy`. Keep the nested `handoff.schema_version` at `1`.
Use exactly the five `{ "path": "…", "sha256": "…" }` input references from the
[signature verifier](IMAGE_SIGNATURE_VERIFICATION.md), `cosign_binary`,
`registry_ca`, `public_key`, `trusted_root`, and `credential`. The promoter runs
as a non-root Linux user with access to the public trust material and a
repository-scoped pull credential. The release operator holds the signing key
and the registry credential that can publish the signature.

Generate the release request with the same plan that the promoter will apply.
This mode reads the live Zuul result with `zuul-token` and `zuul-ca.crt`.

```bash
umask 077
python3 -B scripts/zuul_artifact_promotion.py \
  --plan private/zuul-artifact-promotion.json \
  --publisher-contract private/zuul-static-image/playbooks/platform-static-publisher/contract.json \
  --input-dir /run/platform/promoter --prepare-signature > private/zuul-release-request.json
```

The release operator reviews the request with the source change and deployment
policy, then signs its `image` digest with Cosign and supplies every entry in
`annotations` as an individual `--annotations key=value` argument. Use the
approved transparency log and its corresponding Sigstore signing configuration.
The annotations bind the published image, Gerrit revision, final Zuul build,
publisher contract and selected deployment policy. Each controller target has
its own release binding. Refresh a pull credential by updating its pinned file
hash in the private plan. A change to the CI evidence or reviewed target
requires a fresh release signature.

Run `--check-server` and `--apply` with the signature-gated plan. A failed
signature check stops preflight before Git access. Apply verifies the signature
again just before advancing the branch. The private journal records the
verified key and annotation digests. `merged_ci_signature_verified` reports
the release-gate result. Verify Kyverno image admission separately on the
destination cluster.

Add `ZUUL_SIGNED_ARTIFACT_ROLLOUT_TEST_RUNTIME=1` to the local rollout fixture
for both signed controller paths. Supply `COSIGN_TEST_BINARY`,
`REKOR_POSIX_TEST_BINARY`, and a private `SIGSTORE_TEST_REPORT` path as
described in the [signature fixture](IMAGE_SIGNATURE_VERIFICATION.md#actual-local-protocol-acceptance).
The local signed run used a separate Harbor release robot. Both controllers
verified the signed CI and deployment binding and transparency inclusion on
preflight, apply and replay. Each deployed the same image digest to two K3s
workers after one branch update. The fixture tested refusal of a missing or
incorrect binding, a different key and a different log. This run covered
promotion verification and workload rollout.

For the local joined admission test, also set
`ZUUL_ADMITTED_ARTIFACT_ROLLOUT_TEST_RUNTIME=1`. Supply the reviewed Kyverno
release and image archives in `KYVERNO_ADMISSION_TEST_ARTIFACTS`, the pinned CLI
in `KYVERNO_TUF_TEST_BINARY`, and a private `KYVERNO_TUF_TEST_REPORT` path.
This variant runs three K3s workers and installs the pinned Kyverno v1.19.2
fail-closed admission
controllers before promoting. Each target namespace has its own policy,
release key, threshold-signed trust metadata and pull credential. The fixture
checks the Kubernetes API refusals and admits the controller's rollout for the
same digest and annotations verified by the promotion gate. The fixture
retains promotion, policy, workload and trust evidence under the private K3s
report directory. The local run verified both rollouts and 12 named refusals
per target, including altered bindings and unsigned init or ephemeral
containers. Physical failure-domain qualification requires a separate lab.

## Check and promote the source revision

Run local validation first. Select a new proof directory under `private/`
for each server invocation.

```bash
python3 -B scripts/zuul_gitops_handoff.py --plan private/zuul-handoff.json
python3 -B scripts/zuul_gitops_handoff.py --plan private/zuul-handoff.json \
  --input-dir /run/platform/promoter --check-server --output private/proof/zuul-preflight
python3 -B scripts/zuul_gitops_handoff.py --plan private/zuul-handoff.json \
  --input-dir /run/platform/promoter --apply --output private/proof/zuul-promotion
```

Server preflight reads the original objects and checks that the deployment
branch can fast-forward. Apply rechecks the Zuul evidence and Gerrit source
head, pushes the exact commit to the reviewed deployment branch, and waits for
the chosen controller to report that revision. Repeating the same successful
promotion observes the branch without pushing again. Retain the private proof
and inspect the branch and controller if an invocation stops after the push.

The local tests exercise changed-evidence rejection, preflight, promotion, and
repeat execution with simulated Git and controller interfaces for both
controllers. An isolated Gerrit and Zuul 14.3.0 run also submitted a disposable
change, verified the resulting post-review buildset through Zuul's real API,
and matched the resulting Gerrit branch revision with the non-root worker's
completion marker.

Use `ZUUL_POST_HANDOFF_TEST_RUNTIME=1` and the isolated integrated worker
fixture inputs described in [Zuul service validation](ZUUL_SERVICES.md) to run
`ZuulServicesTests.test_real_gerrit_post_buildset_for_gitops_handoff` from
`scripts/test_zuul_services.py`.

The combined Linux fixture promoted the same commit from a real Gerrit and
Zuul post-review job to dedicated Argo CD and Flux deployment branches. It
used the production handoff CLI with a scoped Gerrit writer, a read-only
Kubernetes observer, and an isolated TLS gateway to the real Zuul buildset
API. Each native controller applied the ConfigMap from the exact promoted
revision against a disposable Kubernetes API. Server preflight left each
deployment branch at its original revision, and replay performed no push.

Run `ZuulServicesTests.test_real_zuul_gerrit_to_gitops_controllers` with
`ZUUL_GITOPS_HANDOFF_TEST_RUNTIME=1` using the same integrated worker fixture.
Supply the pinned fixture inputs via `ARGOCD_TEST_ARTIFACTS`,
`ARGOCD_TEST_VALKEY`, `ROOK_BOOTSTRAP_TEST_BIN`, and
`FLUX_RECONCILIATION_TEST_ARTIFACTS`. The native Kubernetes API and Gerrit
workspaces use disk-backed `/var/tmp`. This proof covers local controller
reconciliation. Deployment and failover on the intended clusters remain
separate acceptance work.

## Local RKE2 worker deployment rehearsal

A further isolated Linux run joined native Gerrit 3.13.10 and Zuul 14.3.0
to Argo CD 3.5.0 and an offline RKE2 v1.36.4+rke2r1 cluster. The cluster used
three control-plane VMs, two dedicated agent VMs, and pinned Cilium 1.19.6
with separate Envoy processes. A real Zuul SSH worker executed the reviewed
manifest validation test before the post-review buildset authorized promotion.
The worker received only the manifest and its test, without repository
credentials or Git metadata.

The handoff CLI's preflight preserved the deployment branch. Apply promoted
the exact tested revision, and native Argo reconciled two non-root HTTP Pods
on separate agent VMs. Verification checked the pinned image digest, resource
ownership, worker identities, credential isolation, and the response through
both workers. After deleting one serving Pod, its replacement served the same
content; Deployment, Service and node identities stayed unchanged. Replaying
promotion made no second Git push. The observer's Kubernetes writes and reads
outside the application scope were denied.

This run completed in 22 minutes 26 seconds with a 12 GiB parent memory limit,
no OOM events, and verified guest and native-process cleanup. Its receipts
matched 557 public Python source hashes and three private coordinator inputs.
The fixed second-agent profile and deployment callback are opt-in fixture
helpers; the combined coordinator and cached runtime inputs remain private
acceptance tooling.

This is a hybrid, single-workstation rehearsal with native CI and Argo
processes plus real RKE2 VMs. It does not establish physical Gerrit/Ceph HA,
Argo HA, registry publication of a built artifact, or database promotion
during deployment. Those acceptance checks remain separate.

## Worker-backed Flux validation

The native Flux fixture also accepts the fixed RKE2 worker context. Its
workload service account can apply ConfigMaps, Services and Deployments in
the application namespace and read Pods and ReplicaSets for health checks.
It must be denied secret access. The promotion observer must be denied
Kubernetes writes and reads outside its declared scope.

The reviewed worker bundle includes an explicit `kustomization.yaml` selecting
`web.json`, and its CI validation rejects an empty resource selection. Keeping
the build file in Git also follows [Flux's build-validation guidance](https://fluxcd.io/flux/components/kustomize/kustomizations/#generating-a-kustomizationyaml-file).
The worker verifier requires Flux ownership labels and inventory entries,
two-worker HTTP serving, and successful Pod replacement without another Git
push.

A complete isolated run now verified this extension with native Gerrit 3.13.10,
Zuul 14.3.0 and Flux 2.6.4, three RKE2 v1.36.4+rke2r1 control-plane VMs and two
dedicated agent VMs. The actual Zuul worker validated the reviewed manifest and
its explicit resource selection before the successful post-review buildset.
The handoff CLI preserved the deployment branch during preflight, promoted the
exact tested revision once, and verified replay without another push. Flux
ownership labels and inventory covered the Deployment and Service; both agents
served the expected HTTP response before and after a serving Pod replacement.
The observer's writes and out-of-scope reads, and the Flux workload identity's
Secret reads, were denied.

The run completed in about 21 minutes under the original 12 GiB parent memory
limit and 2 GiB swap limit, without reaching the memory ceiling or recording an
OOM event. Its acceptance
audit matched all 4,578 archived public/coordinator inputs, including 560
top-level public Python files, checked original process identities, and verified
guest and native-service cleanup. The runtime used a separate frozen source
copy and read-only binds of the pinned artifact cache.

This remains a single-workstation rehearsal with native Flux source and
kustomize processes, not an HA Flux installation. Physical Gerrit/Ceph HA,
redundant Flux artifact storage, built-artifact publication, database promotion
during deployment and full-stack qualification remain separate acceptance work.
