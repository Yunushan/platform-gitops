# Optional Jenkins success reporting to Gerrit

`scripts/report_jenkins_gerrit.py` can publish a `Verified +1` for an open
Gerrit patch set after the existing resolver finds the successful build for a
configuration-bound Jenkins queue reservation. With explicit `--failure`, it
can instead report `Verified -1` for a definite Jenkins `FAILURE` tied to the
same queue, parameters, job configuration, and current open patch set. A
failure may occur before checkout; it does not establish checkout provenance.
Neither mode triggers builds, submits changes, or deploys.

The policy explicitly sets `trust_jenkins_controller` to `true`: the Jenkins
controller, reviewed job, workers and plugins must be trusted. The reporter
checks the current job configuration and recorded build metadata; it cannot
prove that the configuration was unchanged during the build or independently
attest the checkout. Do not interpret a vote as independent provenance or an
end-to-end HA or deployment verification.

The JSON policy has exactly these fields: `schema_version` (1), `trigger`
(the original trigger plan, including its pinned job configuration digest),
`reviewer_username`, `reviewer_account_id`, `label_definition_sha256` (the
SHA-256 of the canonical JSON representation of the effective Gerrit Verified
label), and `trust_jenkins_controller` (true). Pin the bot and label after
reviewing the project permissions and inherited label configuration. The label
must not copy votes forward or allow post-submit votes.

Use an existing private input directory with `jenkins-user`, `jenkins-token`,
`gerrit-user`, `gerrit-token`, `reviewer-user`, `reviewer-token`, and `ca.crt`.
The Gerrit read and review usernames must differ; the reviewer must match the
policy and be a separately scoped bot. Reuse the queue journal created by the
trigger and create a private, mode-0700 review journal directory under the
checkout's `private` directory. Keep all credentials and journals private.

Run first without `--apply` to check readiness:

```sh
python3 scripts/report_jenkins_gerrit.py --plan /private/path/report-policy.json \
  --queue-journal /private/path/queue.json --review-journal-dir "$PWD/private/reviews" \
  --input-dir /private/path/inputs
```

Add `--apply` to submit the single positive vote. A reservation is written
before the API call. If the call fails or the result cannot be observed, a
subsequent run checks for the exact tagged vote but does not repeat the write.
Investigate the Gerrit change and retained reservation before any manual
recovery. A new patch set invalidates the check; the non-copying label prevents
the older vote being carried forward. There is still a race between the final
check and posting, so review the actual target patch set and branch policy.

After the change is merged, use the same arguments with `--verify-merged` for
a read-only check that the recorded successful build and vote belong to the
merged revision and that revision is still the exact branch head. This does
not authorize a deployment. An experimental programmatic adapter,
`scripts/jenkins_gerrit_gitops_handoff.py`, connects the live check to guarded
Argo CD promotion. Its experimental operator entry point accepts `--policy`,
`--queue-journal`, `--review-journal-dir`, `--input-dir`, `--git-input-dir`,
and a new private `--output-dir`; it performs preflight unless `--apply` is
specified. The input directory also needs `cd-kubeconfig`, while the separate
Git directory needs `token` and `ca.crt`. The source reader, review bot and Git
writer must be distinct. The policy pins the report policy and reviewed Argo CD
target; the output directory records the handoff's progress. Real Git/Argo CD
integration testing is still needed, and simulated tests do not establish
production readiness.

If a handoff fails, retain its private `handoff-proof.json` and inspect its
phase and bounded diagnostic signals. A failure after `pre-push-verified` may
have changed the deployment branch even if reconciliation was not confirmed;
check the branch and controller before attempting another handoff. A retry
requires a new private output directory and repeats the live evidence checks.

A disposable HTTPS Git fixture verifies that the adapter's preflight leaves
the deployment branch unchanged and that an applied handoff fast-forwards it
to the tested commit. That fixture simulates Jenkins evidence and Argo CD
reconciliation; it does not verify a live controller or the full workflow.

An additional network-isolated fixture uses real Jenkins and Gerrit to build,
vote, merge, and fast-forward the Gerrit deployment branch through this
adapter. Its Argo CD observation and management-target check are simulated;
the fixture does not establish actual controller reconciliation or HA.

In its optional Argo CD mode, the same isolated fixture also runs native Argo
CD repository and application controllers against a disposable Kubernetes
API. It verified reconciliation of a synthetic ConfigMap after the Jenkins
handoff, exercising the operator entry point and its private proof journals
in the fixture process. Jenkins API reads used the CLI's HTTPS transport via
a local TLS proxy and a token generated and authenticated by the real
Jenkins instance; an invalid token was rejected. The temporary fixture user
has broad permissions, so this does not validate production RBAC. It is a
local single-instance test, not an HA or production test.
On a prepared Linux test host with the pinned artifacts, enable this mode
with `JENKINS_REAL_GERRIT_FIXTURE=1` and `JENKINS_ARGOCD_FIXTURE=1` when
running `scripts/test_jenkins_gerrit_real_fixture.py`. Set
`ARGOCD_TEST_ARTIFACTS`, `ARGOCD_TEST_VALKEY`, and `ROOK_BOOTSTRAP_TEST_BIN`
to the reviewed local artifact locations. The test requires root to create
its disposable network namespace and Kubernetes API; it is not a deployment
or a substitute for a separate multi-node HA laboratory.

Start from the synthetic [handoff policy](../examples/jenkins-gerrit-argocd-handoff.json)
in a private location. Replace all example identities, hashes, URLs and
object UIDs with reviewed values. After updating the `report` section, compute
its canonical digest and put it in `promotion.bridge_contract_sha256`:

```sh
PYTHONPATH=scripts python3 -c 'import json,sys; from tekton_gerrit_reporter import fingerprint; print(fingerprint(json.load(open(sys.argv[1]))["report"]))' /private/path/handoff-policy.json
```

The example is not a deployment configuration. The Argo CD spec digest must
match the reviewed live application, and its source must use the configured
deployment branch and the same Gerrit HTTPS repository.

The isolated real Jenkins/Gerrit fixture exercises the reporting core's
preflight, positive and negative publication, and deduplication. It also
checks that the failed build cannot satisfy merged deployment evidence. A separate loopback TLS fixture
exercises command-line argument and credential handling through preflight,
publication and retry against simulated services. These tests do not establish
production controller trust, worker isolation, or availability.

For bounded polling, `scripts/scan_jenkins_gerrit_results.py` discovers current
open changes, checks their existing queue reservations, and preflights every
selected result before reporting up to `max_reports`. It skips running and
missing builds. Setting `report_failures` to true opts in to negative votes
for `FAILURE`; aborted, unstable and not-built results remain unreported. Start with
the synthetic [result scan policy](../examples/jenkins-gerrit-results-scan.json)
and replace its example values privately. Pin the complete policy with
`scan_jenkins_gerrit.canonical_digest`, then supply `--policy`,
`--policy-sha256`, `--input-dir`, `--queue-dir`, and `--review-dir`. It is
read-only without `--apply`. If a write fails, earlier votes in that cycle may
already have been posted; retain the review reservations for safe inspection.
Pass `--watch --interval-seconds 60` to repeat the cycle until interrupted or
until one cycle fails. A POSIX lock prevents overlapping result pollers using
the same queue directory; it does not coordinate distinct directories or hosts
without shared lock-capable storage. Configure supervision and alerts
separately, and avoid automatic restarts until an uncertain vote is inspected.
The combined disposable Jenkins/Gerrit/Argo CD fixture exercises two
single-cycle CLI invocations, including policy hashing, authenticated HTTPS,
the lock, a vote and deduplication. It also exercises opt-in negative failure
reporting through the CLI against real disposable Jenkins and Gerrit services.
It does not test long-running supervision.

For a Linux host, `scripts/render_jenkins_pollers.py` renders two optional
systemd units from a private copy of [the example plan](../examples/jenkins-gerrit-pollers.json).
It prints unit text and does not install or start services. Set the reviewed
policy digests and provision the private directories and credentials first.
Both units use one dedicated non-root Unix account so they can share locks;
that account can therefore access both input directories. They stop on failure
instead of automatically retrying an uncertain write. Configure monitoring and
recovery procedures before installing them. The renderer uses a service root
under `/opt`, `/srv`, or `/var`; it does not configure Kubernetes workloads,
multi-host locking, or HA.

For Kubernetes, `scripts/render_jenkins_pollers_k8s.py` renders an explicitly
started single pod from [the synthetic plan](../examples/jenkins-gerrit-pollers-k8s.json).
It assumes a separately built and reviewed digest-pinned image containing the
scripts at `/opt/platform/scripts`, Python and its dependencies; it does not
build that image. Provision the PVC with `queue` and `reviews` directories
owned by UID 1000 and mode 0700, separate credential Secrets, and ConfigMaps
containing each reviewed `policy.json`. The pod uses `restartPolicy: Never`;
if one container fails, the other can continue, so monitor both and inspect
the retained reservations before replacement. Use one active pod and ensure
the PVC supports the required lock semantics. A disposable API accepted the
pod in a restricted namespace; image execution, storage recovery and HA are
not established by admission.
An init container checks that both pre-existing directories are distinct,
on the same filesystem, owned by the non-root poller UID, mode 0700, and
writable before either poller starts. This local check does not establish
distributed lock correctness, backup, or storage failover. It verifies that a
second local process cannot acquire a held temporary queue lock; cross-node
locking still requires separate testing on the selected storage.

`scripts/prepare_jenkins_poller_image.py` can prepare a minimal offline build
context using a reviewed digest-pinned Python base image and a separately
obtained PyYAML wheel with its verified SHA-256. Start from the synthetic
[image plan](../examples/jenkins-poller-image.json) and replace both digests.
Provide `--plan`, `--wheel`, and an absolute, new `--output` directory under
the checkout's `private` directory. The base must provide
`/usr/local/bin/python3`, `pip`, and `sha256sum`, and the wheel must be
compatible with that Python and target architecture. The generated context
contains only the explicit helper files and the wheel; it does not build,
publish, sign, or scan an image. Build and review it in an appropriate image
pipeline and use the resulting immutable digest in the pod plan. Container
execution and the produced image have not been verified here. A disposable
Linux test separately ran the prepared scripts as a non-root user and checked
that they could use private state while their script files remained unwritable.

## Optional signed image publication and promotion

Provision a dedicated non-root Linux Jenkins worker for the reviewed static
publisher. Restrict jobs on that worker to trusted operator-defined commands.
Use the [publisher contract example](../examples/jenkins-static-image.json)
and install the publisher and its dependencies outside the checkout. Pin the
contract with `jenkins_static_image.fingerprint` and the checkout helper with
`source_bound_static_image.checkout_program_sha256`. The private publisher input
directory contains `source/ca.crt`, `source/token`, `registry/ca.crt`, and
`registry/credential.json`. Use a scoped Gerrit read account and the scoped
[Harbor publisher robot](HARBOR_STATIC_IMAGE.md).

Set `archive_image_receipt` to `true` in the Jenkins job renderer plan. It
configures Git to wipe the dedicated workspace before checkout and archives only
`platform-image-receipt.json` after a successful build. Review the Jenkins
`config.xml` returned by the controller and pin its SHA-256 in the trigger and
report policy. Configure `build_argv` with the reviewed absolute command below.
[Jenkins ArtifactArchiver](https://javadoc.jenkins.io/hudson/tasks/ArtifactArchiver.html)

```text
/usr/bin/python3 -EsB /opt/platform/scripts/jenkins_static_image.py --contract /run/platform/jenkins-publisher/contract.json --contract-sha256 REVIEWED_CONTRACT_SHA256 --input-dir /run/platform/jenkins-publisher
```

The publisher independently fetches the exact Gerrit patch-set ref and reads
the listed static files. The Jenkins controller and dedicated worker remain
trusted for the build record and archived receipt. The existing review process
records the build result and submits the change. The release command reads the
receipt from the exact successful build number and verifies the queue
reservation, reviewed Jenkins configuration, Gerrit vote and current merged
commit before and after the artifact read.

Copy the [signed promotion plan](../examples/jenkins-argocd-artifact-promotion.json)
and supply the reviewed values. Use the existing GitOps handoff for `handoff`.
Pin the existing deployment branch tip, live Deployment UID, container, and
controller spec. The watched directory contains one regular Deployment JSON.
Give the observer `get` on this Deployment. Use the usual private operator
inputs and add `jenkins-ca.crt` and `ca.crt` for the Jenkins and Gerrit APIs
respectively. Both CA files are pinned by the publisher contract. The Jenkins
artifact endpoint must serve bytes on the reviewed HTTPS origin without redirects.

```bash
umask 077
python3 -B scripts/jenkins_artifact_promotion.py \
  --plan private/jenkins-artifact-promotion.json \
  --publisher-contract private/jenkins-static-image.json \
  --input-dir /run/platform/jenkins-promoter \
  --queue-journal /absolute/path/to/private/queue/reservation.json \
  --review-journal-dir /absolute/path/to/private/reviews \
  --prepare-signature > private/jenkins-release-request.json
```

Have the separate release signer sign the exact `image` and all `annotations`
from the request. Follow the [Cosign inputs guide](IMAGE_SIGNATURE_VERIFICATION.md)
for the verification identity. Repeat the command with `--check-server` and
then `--apply`, replacing the stdout redirect with `--git-input-dir` for the
deployment writer and a fresh private `--output` path for each invocation.
The promoter rechecks the signature, archive and merged source before advancing
the branch. It verifies reconciliation of the original Deployment at that
digest. Retain the private proof if a run stops after the Git push.

In the disposable local test, Jenkins 2.580.2 built two Gerrit 3.13.10 patch
sets and archived both receipts. The release check rejected the older receipt,
and an independent Harbor pull matched the selected patch set's static file.
Argo CD 3.5.0 applied the signed digest to the original Deployment on three
K3s workers. The test checked each running image ID and queried the NodePort
on each worker. Kyverno v1.19.2 enforced the signature policy before the Git
push and rejected 12 invalid admission requests. Promotion replay skipped
the push. This test ran with a single Jenkins controller and a single Gerrit
site in the disposable lab.
