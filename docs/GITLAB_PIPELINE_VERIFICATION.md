# GitLab pipeline verification

`scripts/verify_gitlab_pipeline.py` provides an optional, read-only check of a
GitLab push pipeline. It requires the specified project identity, a protected
branch at the exact 40-character commit, a successful push pipeline for that
commit, and successful non-optional jobs with the requested names. It checks
the current jobs and source state again before returning. A missing pagination
header, ambiguous required jobs, or a changing source state fails the check.

Create a private JSON plan with these fields, replacing the example values:

```json
{"schema_version":1,"api_url":"https://gitlab.example.org/api/v4",
 "project_id":123,"project_path":"group/project","branch":"main",
 "pipeline_id":456,"revision":"0123456789abcdef0123456789abcdef01234567",
 "required_jobs":["build","test"]}
```

Place a GitLab API token with sufficient read access in a private directory as
`api-token`, then run `python3 scripts/verify_gitlab_pipeline.py --plan
/private/path/plan.json --input-dir /private/path/token-directory`. Keep both
files outside version control. The endpoint must use HTTPS with a trusted CA.
The endpoint must supply the `X-Next-Page` pagination header.

This checks the GitLab API at a point in time. It does not establish an atomic
authorization, validate child pipelines or artifacts, modify GitOps, or deploy.
The caller must protect against changes between verification and promotion.
The verifier also passed against a live single-node GitLab CE instance in the
local test described below.

## Optional GitOps handoff

`scripts/gitlab_gitops_handoff.py` connects this check to the reviewed
Argo CD or Flux handoff described in [CI GitOps handoff](CI_GITOPS_HANDOFF.md).
It accepts a JSON object with `schema_version: 1`, `runtime`, `pipeline`
(the verifier plan above), and `handoff` (the existing handoff policy). Set
the policy's `bridge_contract_sha256` to the SHA-256 of the canonical pipeline
plan, computed with `ci_gitops_handoff.sha256`. The Git clone URL is derived
from the API origin and project path, so this adapter requires Git and API on
the same origin. Review the controller UID, spec digests, destination branch,
and branch permissions as described in the handoff documentation.

Run with `--plan /private/path/plan.json` for local validation; `--check-server
--output /path/under/private/new-proof-directory` checks the live services
without pushing; `--apply` explicitly enables the push and controller check.
The latter two require Linux and private `api-token`, Git `token`, and
`cd-kubeconfig`, and `ca.crt` inputs in `/run/platform/promoter`. The CA bundle
must trust the GitLab HTTPS endpoint. A failed run may already
have pushed; inspect the private proof, branch, and controller before retrying.

The adapter rechecks CI and the source ref immediately before the push, but
these independent checks and the push are not atomic. Protect the source and
deployment branches and restrict who can update them. Both controller paths
have authenticated Git HTTPS fixture coverage and combined K3s runtime checks.

### Publish and promote a signed image

Use a dedicated protected Runner and deploy the reviewed publisher scripts on
that Runner. Store the [publisher contract](../examples/gitlab-static-image.json)
and private inputs outside the job checkout. Pin the contract with the SHA-256
of its canonical JSON using `gitlab_static_image.fingerprint`. The input
directory contains `source/ca.crt`, `source/token` for the Git checkout, and
`registry/ca.crt`, `registry/credential.json` for the scoped Harbor publisher.
The publisher independently fetches the protected branch from GitLab and
checks its exact commit, then publishes the listed static files over verified
TLS. Use a fresh writable job directory and run the publisher as a non-root
user. GitLab Runner provides the project, pipeline and job identity. [GitLab predefined variables](https://docs.gitlab.com/ci/variables/predefined_variables/)

Add this job to the reviewed pipeline after the required application tests.
Set the Runner tag and protected `PLATFORM_PUBLISHER_CONTRACT_SHA256` variable
to the values configured for your publisher.

```yaml
platform-publish-static-image:
  stage: deploy
  tags: [trusted-static-publisher]
  allow_failure: false
  dependencies: []
  variables:
    GIT_STRATEGY: empty
  rules:
    - if: '$CI_PIPELINE_SOURCE == "push" && $CI_COMMIT_REF_PROTECTED == "true" && $CI_COMMIT_BRANCH == "main"'
  script:
    - >-
      python3 -EsB /opt/platform/scripts/gitlab_static_image.py
      --contract /run/platform/gitlab-publisher/contract.json
      --contract-sha256 "$PLATFORM_PUBLISHER_CONTRACT_SHA256"
      --input-dir /run/platform/gitlab-publisher
  artifacts:
    access: maintainer
    paths: [platform-image-receipt.json]
```

Prepare the [signed Argo CD plan](../examples/gitlab-argocd-artifact-promotion.json)
with the exact pipeline ID and source revision. For Flux, use the GitRepository
and Kustomization fields shown in the [Flux artifact policy example](../examples/zuul-flux-artifact-promotion.json).
Both targets use an existing deployment branch and the original Deployment UID.
The watched directory contains one Deployment JSON. For Flux, include a
`kustomization.yaml` listing that JSON file as its only resource. Flux's
automatic generation scans YAML files. [Flux manifest discovery](https://github.com/fluxcd/pkg/blob/kustomize/v1.18.0/kustomize/kustomize_generator.go#L481-L510)
Install the observer's Deployment `get` permission in addition to the normal controller
read permissions. Use the canonical hash of `pipeline` for
`handoff.bridge_contract_sha256` and the publisher contract hash for
`publisher_contract_sha256`.

The promoter downloads `platform-image-receipt.json` by the verified required
job ID. Its GitLab API account needs permission to read that artifact. Configure
GitLab to serve this endpoint on the same trusted origin as the API. The
promoter refuses redirects. Use the exact GitLab CA file pinned in the
publisher contract and the usual private handoff files. [GitLab job artifacts API](https://docs.gitlab.com/api/job_artifacts/#download-a-single-artifact-file-by-job-id)

```bash
umask 077
python3 -B scripts/gitlab_artifact_promotion.py \
  --plan private/gitlab-artifact-promotion.json \
  --publisher-contract private/gitlab-static-image.json \
  --prepare-signature > private/gitlab-release-request.json
```

The release signer reviews the request and signs its `image` with every
`annotations` key/value pair. Use a separately protected release key, scoped
registry write credential and approved transparency log. Configure the
promoter with the [Cosign verification inputs](IMAGE_SIGNATURE_VERIFICATION.md)
and a repository-scoped pull credential. Run the same plan with `--check-server`
and then `--apply`, using a fresh private `--output` proof directory each time.
Apply rechecks the receipt, pipeline and signature before advancing the branch.
Success requires the original Deployment to roll out the selected digest.
Verify Kubernetes admission separately using the policy installed on the
destination cluster.

A disposable GitLab CE 19.1.1 instance and Runner 19.1.1 shell executor ran
this publisher on a protected push. The exact-ID artifact download and the
independent Harbor layer readback matched the reviewed source bytes. Both
signed promotion preflights verified the live job receipt with Cosign and an
isolated Rekor log. A separate HTTPS fixture exercised actual Git pushes and
replay for both target configurations with modeled controller status.

The joined local test promoted to workloads managed by native Argo CD and Flux
controllers. Each promotion advanced a separate protected GitLab deployment
branch, and the controller reconciled the original Deployment at the signed
Harbor digest on three K3s workers. The test verified every Pod's image ID
and queried the NodePort on each worker. Kyverno v1.19.2 enforced
the release signature policy before either promotion and rejected 12 invalid
requests per controller, covering unsigned images, an untrusted key, and
mismatched release annotations. Replaying either promotion skipped the Git push.
This signed publication test used the shell executor and single-node GitLab CE.

### Local GitLab CE and Runner verification

An isolated Docker container ran GitLab CE 19.1.1, the application release
used by Helm chart 10.1.1. GitLab Runner 19.1.1 used the shell executor as an
unprivileged user in the same isolated network namespace. The test registered
a project runner, pushed to a protected branch of a private repository over
trusted HTTPS, and ran actual push pipelines.

The verifier rejected queued and failed pipelines, a superseded source
revision, and an invalid API token. It accepted the successful required job
after checking the exact commit. The live jobs endpoint supplied the required
pagination header. The handoff adapter's latest-pipeline discovery and wait
check also passed against this instance.

This test covered single-node GitLab, its bundled Gitaly, and the verifier.
It did not run a deployment branch promotion or a GitOps controller against
this GitLab instance.

### Local GitLab CE, Kubernetes Runner, and Flux integration

One disposable lab ran GitLab CE 19.1.1 with bundled Gitaly in Docker,
GitLab Runner 19.1.1 using the Kubernetes executor, and Flux v2.6.4 on
K3s v1.35.0+k3s1 with two workers. GitLab served the private repository over
verified HTTPS. Flux read it with a project deploy token scoped to
`read_repository`. The verifier and promoter used separate GitLab tokens
with `read_api` and `write_repository` scopes respectively. Both `main` and
`deploy/staging` were protected.

Flux first applied the baseline from `deploy/staging`. A queued push pipeline
and a job that deliberately failed on a K3s worker both left the branch and
the applied ConfigMap at that baseline. The successful job checked its
detached HEAD and manifest, using the chart's job service account with token
automount disabled. The handoff advanced `deploy/staging` to the tested
commit, verified the exact GitRepository and Kustomization revisions, and
confirmed the live ConfigMap had the tested value. Repeating the handoff
verified the same state without another push.

The GitLab container and guests were disposable. The test used a single NFS
artifact store and a single GitLab instance. A separate run exercised
[scheduled promotion](#optional-kubernetes-scheduler) against the same
topology. The [shared-database failover drill](GITLAB_REPOSITORY_HA.md#flux-handoff-after-shared-database-failover)
also verified this handoff after automatic PostgreSQL promotion with one
Gitaly replica offline. Cilium NetworkPolicy enforcement in that combined
drill and production HA acceptance remain open.

### Local GitLab CE, Kubernetes Runner, and Argo CD integration

A separate offline K3s lab ran GitLab CE and Runner 19.1.1 with Argo CD 3.5.0.
The repo-server used a GitLab deploy token scoped to `read_repository` and
verified GitLab's TLS certificate. An AppProject restricted the source
repository and permitted only ConfigMaps in the dedicated destination namespace.
Argo CD applied the baseline from the protected `deploy/staging` branch.

A queued pipeline and a deliberately failing Kubernetes Runner job left the
deployment branch and live ConfigMap at the baseline. The successful Runner
job checked the detached commit and manifest. The handoff then advanced the
protected branch to that exact commit. It observed a successful Argo CD sync
of the same revision and checked the new ConfigMap value. Repeating the handoff
verified the applied revision without another push.

The lab used a Docker-hosted single GitLab instance, its bundled Gitaly,
and a single Argo CD application controller on K3s. A second run exercised
the [Kubernetes scheduler](#optional-kubernetes-scheduler). The
[shared-database failover drill](GITLAB_REPOSITORY_HA.md#argo-cd-handoff-after-shared-database-failover)
verified this exact-commit handoff after automatic PostgreSQL promotion with
one Gitaly replica offline. Reconciliation of the GitLab installation chart
and production HA acceptance remain open.

### Local Runner and Flux fixture

A disposable K3s v1.35.0+k3s1 lab ran GitLab Runner 19.1.1 with the
Kubernetes executor and Flux v2.6.4. A synthetic GitLab API offered the job
and derived its success status from the real Runner update. Runner's helper
checked out the private repository over HTTPS using a job token. The build
container checked the detached commit and the expected deployment manifest.
The job Pod used the chart's job service account with token automount disabled.

The handoff rejected a pending pipeline and a simulated failed required job
without pushing or advancing the deployment branch. After verifying the
successful job, it advanced `deploy/staging` once and observed the same
revision in the live GitRepository and Kustomization. The ConfigMap value
matched that commit. Repeating the handoff verified the applied revision
without a second push.

This fixture used a synthetic coordinator, a real Git HTTPS backend, and a
single NFS artifact store.

### Reusable policy for later pipelines

For repeated promotions, use outer `schema_version: 2` and replace `pipeline`
with `pipeline_policy`: the same verifier fields except `pipeline_id` and
`revision`. Set `handoff.bridge_contract_sha256` to the canonical SHA-256 of
that policy, and review the whole handoff configuration before using it. For
example, after preparing the private plan file, calculate the policy digest:

```sh
PYTHONPATH=scripts python3 -c 'import json,sys; from ci_gitops_handoff import sha256; print(sha256(json.load(open(sys.argv[1]))["pipeline_policy"]))' /private/path/plan.json
```

Set the resulting digest in the plan. Each server invocation then supplies
`--pipeline-id 123 --revision <40-character-commit>` alongside
`--check-server` or `--apply`. A scheduler can invoke it after the GitLab
pipeline has finished: a job inside that same still-running pipeline cannot
pass the successful-pipeline check. The API, project, protected branch and
required jobs remain pinned in the reviewed policy; the supplied run identity
is checked against GitLab and the current Git branch. The private proof records
both the reviewed plan digest and the bound per-run plan digest.

An external scheduler can add `--wait-seconds 900` to a server invocation to
poll the selected pipeline for up to 15 minutes. The wait accepts only the
specified project, push source, branch, commit and pipeline ID; failed,
canceled, manual, unknown, or timed-out states stop the handoff. After success,
the full branch, pipeline and required-job verification still runs before any
push. The wait is bounded to 3600 seconds, plus an in-flight API request's
network timeout. This option does not install a scheduler or run automatically
on its own.

Alternatively, `--latest` on a server invocation with a version 2 policy
discovers the current protected branch head and GitLab's latest pipeline for
that ref. It requires that pipeline to be a push for exactly that commit and
checks it remains latest during both CI checks before promotion. A latest
pipeline that is failed, manual, or from another source is not replaced by an
older successful pipeline. `--latest` cannot be combined with explicit
`--pipeline-id` or `--revision`. GitLab's
[latest-pipeline API](https://docs.gitlab.com/api/pipelines/#retrieve-the-latest-pipeline)
must be available to the scoped read token. The checks and push remain non-atomic.

### Optional host scheduler

`scripts/gitlab_gitops_schedule.py` runs one latest-pipeline check for a
version 2 policy. It verifies the SHA-256 of the entire reviewed plan, freezes
a private copy for the invocation, takes a nonblocking per-plan lock, and
creates a unique private proof. Its default is read-only; `--apply` enables
promotion. Calculate the whole-plan digest with
`PYTHONPATH=scripts python3 -c 'import json,sys; from ci_gitops_handoff import sha256; print(sha256(json.load(open(sys.argv[1]))))' /etc/platform-gitops/gitlab-handoff.json`.

The files [service example](../examples/gitlab-gitops-handoff.service.example)
and [timer example](../examples/gitlab-gitops-handoff.timer.example) show an
opt-in systemd schedule; neither installs nor enables a unit. Adapt paths,
provision a dedicated service account and its private inputs, substitute the
reviewed digest, and test the default read-only mode before enabling promotion.
The account needs access to `/run/platform/promoter` and write access to the
checkout's `private` directory. Arrange monitoring and retention for proofs,
which accumulate on every attempt. The lock coordinates runs of the same plan
on one host only; it is not a distributed lock across hosts or different plans.
The systemd example is host-local and has not been validated as an HA scheduler.

### Optional Kubernetes scheduler

For RKE2, K3s, or Kubernetes, edit a private copy of
[the CronJob plan](../examples/gitlab-gitops-cronjob.json) and run
`python3 scripts/render_gitlab_gitops_cronjob.py --plan /private/path/cronjob.json`.
This only renders YAML. It creates a service account with token automount
disabled and a restricted CronJob with `concurrencyPolicy: Forbid`; the plan
starts suspended and in read-only mode. Unsuspending or enabling `apply` is an
explicit deployment decision. `Forbid` applies to Jobs created by one CronJob.
The scheduler's advisory lock coordinates the same plan digest on the same
filesystem when the storage supports locks across clients.

#### Optional Cilium egress policy

For a scheduler namespace managed by Cilium, change the CronJob plan to
`"schema_version": 2` and add `egress` with the site's reviewed addresses
and TCP ports:

```json
{
  "gitlab_addresses": ["192.0.2.10"],
  "gitlab_port": 443,
  "api": {"kind": "in-cluster", "ports": [443, 6443]}
}
```

The renderer places a CiliumNetworkPolicy before the CronJob. It selects Pods
by both the scheduler label and service account. It permits GitLab HTTPS to
the listed IPs, DNS on TCP and UDP port 53 to the `kube-dns` Pods in
`kube-system`, and the in-cluster Kubernetes API on the listed ports. For an
external API, use `{"kind": "external", "addresses": ["198.51.100.20"],
"ports": [6443]}` inside `egress.api`. IPs become single-address CIDRs. If an
endpoint's addresses change, update the reviewed allowlist before restarting
the job. Select the ports seen by Cilium for the cluster's Service routing.
The address mode is for endpoints outside the local cluster and its nodes.
Node-local DNS or a locally hosted GitLab with Pod endpoints needs a policy
matching the actual DNS and GitLab identities. Check for other policies that
select the scheduler because allow rules are additive. Version 1 plans still
render the ServiceAccount and CronJob and can use a site-managed policy.
[Cilium policy reference](https://docs.cilium.io/en/stable/security/policy/layer3/)

#### Credentials and verification

Provide a reviewed digest-pinned Linux image with this checkout at
`/opt/platform/platform-gitops` and working `/usr/bin/python3`, PyYAML, Git,
and `kubectl` binaries. An offline image context can be prepared as described
below. Provision the dedicated namespace, the reviewed plan ConfigMap with
key `plan.json`, the Secret with `api-token`, `token`, `ca.crt`, and
`cd-kubeconfig`, and a private persistent PVC before applying the YAML. The PVC
must support the scheduler's POSIX advisory locks and be writable by UID/GID
65532. For Jobs that can move between workers, provision a `ReadWriteMany`
filesystem PVC and verify that its locking works from both workers. Keep
proofs and locks in a directory dedicated to this scheduler. Retain the volume
and include it in backups if the proofs must survive claim replacement.
Restrict egress to the GitLab and reviewed Kubernetes API endpoints.
The kubeconfig identity needs `get` on the `kube-system` Namespace for cluster
identity verification. For Argo CD it needs `get` on the reviewed Application.
For Flux it needs `get` on the reviewed Kustomization and GitRepository in
their respective namespaces. Render those bindings from the same reviewed
version 2 plan and full-plan digest used by the scheduler:

```sh
python3 scripts/render_gitlab_gitops_access.py --plan /private/path/plan.json \
  --plan-sha256 "$reviewed_plan_sha256" --namespace platform-automation \
  --account gitlab-promotion-reader > /private/path/gitlab-reader.yaml
```

Use a dedicated reader account. Verify the selected context and `kube-system`
UID against the plan before installing the rendered roles on the CD cluster.
Create the reader namespace on that cluster first. Supply a scoped credential
in `cd-kubeconfig` and rotate it before expiry. The scheduler's workload
ServiceAccount remains separate with token automount disabled.
When the reviewed observation targets change, retire the old reader bindings
after switching the scheduler to the new ones.

Set the job deadline to cover the pipeline wait, the handoff policy's controller
timeout, and network overhead. The ServiceAccount, CronJob, and restricted Pod
template passed admission against a disposable Kubernetes 1.35 API fixture.

In the K3s lab, the rendered CronJob scheduled a read-only preflight against
the latest successful GitLab pipeline. The protected branch stayed at its
baseline. The production scheduler image ran as UID 65532 and retained its
private proof on a local PVC. A scoped Kubernetes reader could observe the
reviewed Application and was denied Secret reads and Application patches.
After the CronJob was suspended and promotion was explicitly enabled in its
template, two Jobs created from that template verified the exact Argo CD
revision and the applied ConfigMap. The first Job advanced the branch and
the second verified it without pushing.

Separate Argo CD and Flux runs verified that the CronJob controller created
both the preflight and, after explicit enablement, the promotion Job. The lab
checked the owner UID and Kubernetes' scheduled timestamp on both Jobs.
Each promotion advanced the protected branch to the exact tested commit and
verified the live ConfigMap. The Argo CD run used the reader renderer above
and verified a successful Application sync at that commit. The Flux run
verified the matching GitRepository artifact and Kustomization revision.
In both runs, a subsequent Job verified the same revision without a second
push. The scoped readers were denied Secret reads and controller object
patches. A separate Kubernetes API test verified that the Flux reader's
bindings also work across two namespaces.

A separate Cilium 1.19.10 run used three K3s control-plane guests and two
workers. After the control plane recovered from the quorum drill, a baseline
Pod reached a test listener on the GitLab address. An otherwise equivalent
Pod selected by the generated egress policy could reach only the reviewed
GitLab port and timed out on the test listener's unapproved port. With the
policy active, the CronJob completed scheduled preflight and promotion against
GitLab CE, and Argo CD applied the exact tested commit. Replay made no second
push. The test exercised the in-cluster API mode.

A separate Argo CD run used a root-squashed NFSv4.1 `ReadWriteMany` proof
claim. While the first worker held the plan lock, a second worker's
write-enabled scheduler Job reported `already-running` and left the
deployment branch at the baseline. After lock release, scheduled promotion
and Argo CD reconciliation succeeded. The first worker was cordoned, and
the second read the original mode-0600 proofs with matching hashes. The
replay Job on that worker wrote a third proof without another push.

The proof server ran on a single disposable guest. The run verified
worker reassignment and cross-client locking on one shared claim. CephFS,
storage-server loss, external API egress, and combined hybrid HA remain
unverified.

### Preparing the scheduler image context

Choose a reviewed, digest-pinned Linux base image for the target architecture
that already includes `/usr/bin/python3`, PyYAML, Git, and a POSIX shell. Obtain
and independently verify the matching Linux `kubectl` binary. Copy
[the image plan](../examples/gitlab-gitops-image.json) to a private path and
replace the example base digest and kubectl SHA-256. Then run:

```sh
python3 scripts/prepare_gitlab_gitops_image.py --plan /private/image-plan.json \
  --kubectl /private/verified/kubectl --output /private/new-image-context
docker build --network=none --pull=false -t local/gitlab-handoff:reviewed /private/new-image-context
```

Load the pinned base into the selected BuildKit builder before an offline
build. Context preparation copies the fixed source allowlist and the checked
kubectl artifact and writes a file-hash manifest. The image sets the bundled
scripts' permissions before running its startup checks as UID/GID 65532.
A Linux AMD64 image built from a digest-pinned Alpine base with BuildKit
passed those checks and ran the CLI on a read-only filesystem in a
network-disabled test container. Inspect and sign the final image digest
before using it in the CronJob plan. Context preparation ends before building,
publishing, or installing the image.

For an isolated authenticated TLS API fixture with OpenSSL and loopback access,
run `GITLAB_PIPELINE_HTTPS_FIXTURE=1 python3 -B
scripts/test_gitlab_pipeline_https_fixture.py`. It uses synthetic data and also
checks that redirects are rejected; it is not a live GitLab integration test.
On Linux, `GITLAB_GITOPS_HTTPS_FIXTURE=1 python3 -B
scripts/test_gitlab_gitops_https_fixture.py` additionally exercises authenticated
Git fetch and push, a failing job, retry, and both simulated controllers.
