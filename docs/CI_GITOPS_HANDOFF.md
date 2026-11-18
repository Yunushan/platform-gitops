# Verified CI revision handoff to Argo CD and Flux

Status: an optional, explicitly invoked deployment-branch promotion command.
Local tests exercise CI/controller checks and real HTTPS Git fast-forwards.
Disposable Forgejo and Gitea lab runs also verified exact-commit promotion
through Argo CD and an HTTP workload on two K3s workers. Failure recovery,
upgrades, restore and the full
[multi-runtime stacks](MULTI_RUNTIME_STACKS.md) remain open.
[Lab results](TEKTON_FORGE_RESULTS.md#disposable-forgejo-and-gitea-validation-on-k3s)

## Current integration contract

The command consumes a reviewed [Forgejo/Gitea Tekton package](TEKTON_FORGE_BRIDGE.md)
and its [reported successful outcome](TEKTON_FORGE_RESULTS.md). It promotes the
exact tested commit to a distinct deployment branch in **that same repository**.
An existing Argo CD Application or Flux GitRepository/Kustomization must already
watch that branch and the selected manifest directory. Git remains the desired
state; the command grants no workload write permissions and changes no controller
specification, health customization or reconciliation policy.

This path is appropriate when the tested repository also contains the reviewed
Kubernetes manifests. It does not yet implement image build/publication,
signed-artifact promotion into a separate GitOps repository, production approval
workflows, or the GitLab proof adapter. Gerrit's separate
[merged-commit adapter](GERRIT_GITOPS_HANDOFF.md) preserves its review/report
contracts and requires the exact tested merged head. The administrator must ensure the
reviewed CI command covers the intended tests and manifest/policy validation.
A zero exit status cannot establish application-specific test coverage by itself.

All five application-host runtime labels are preserved. CI and CD Kubernetes
targets remain explicit. For Docker/native hosting this only advances the
Kubernetes-workload part; it does not deploy containers over SSH or update native
application services. `full_stack_verified` remains false.

## Inputs and authority separation

Copy either [the Argo CD example](../examples/ci-gitops-handoff-argocd.json) or
[the Flux example](../examples/ci-gitops-handoff-flux.json) under `private/`.
Bind the exact CI event-contract hash, application-host runtime, deployment
branch, CD cluster UID, object UIDs and canonical full-spec SHA-256 digests.
The full spec includes destination, project and rendering/reconciliation policy;
review those values before recording its digest.

The deployment branch must differ from the CI source branch. Protect both
branches against unauthorized updates, deletion and history rewrites. Configure
the source forge to give the dedicated Git writer only the intended branch
authority, subject to the selected edition's actual controls. This helper never
changes forge permissions or required CI contexts.
The handoff checks that the deployment update is a fast-forward and uses an
explicit lease for the branch value observed during preflight. A concurrent
deletion or replacement therefore cannot silently turn an existing-branch
update into an unauthorized branch creation.
The disposable HTTPS Git fixture deletes the branch between the final read and
push and confirms the server rejects recreation. This is transport-level
evidence, not live forge or full-stack acceptance.

Prepare a separate private directory at `/run/platform/promoter`:

- `token`: scoped HTTPS Git credential for deployment-branch writes.
- `api-token`: read-only forge credential for repository/head/status verification.
- `ca.crt`: trusted source HTTPS CA bundle.
- `ci-kubeconfig`: scoped read access to the CI namespace's PipelineRuns,
  TaskRuns, Pods and receipt ConfigMaps, plus kube-system and worker Node identity
  reads. No job creation or receipt mutation is needed.
- `cd-kubeconfig`: read access to the named Argo CD Application or Flux source
  and Kustomization, plus kube-system identity. No Kubernetes patch/apply/delete
  permissions are required.

Both contexts and kube-system UIDs are verified. Credential/CA paths are flattened
into private configuration snapshots. Only static scoped tokens or client
certificates are accepted; dynamic credential plugins are refused. None of these
credentials belongs in the source code, a public plan, a build step or an image.

Executing helper source files must match the reviewed package too. Running a
newer working-tree verifier against an old copied bundle is refused. Regenerate
and review a coherent package when updating its programs rather than assuming
that unchanged copied files establish the integrity of different running code.

## Controller prerequisites

For Argo CD, use one Git source matching the tested repository, selected
deployment branch and manifest path, with automated synchronization enabled.
The command rejects multi-source applications for this handoff. Its reviewed
spec digest binds the destination and other settings. A different in-flight
operation blocks a new promotion; an already-promoted revision may continue
waiting for its existing reconciliation. See the
[upstream automated-sync behavior](https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/).
The active-operation check is repeated immediately before the push. It is not
an atomic lock on Argo CD; an operation can still begin after that observation.

For Flux, bind both the Kustomization and GitRepository by UID/spec digest. The
source must select only the deployment branch. The Kustomization must reference
that source, use a scoped service account, enable `wait`, and remain unsuspended
with force replacement disabled. See the
[Flux health-check contract](https://fluxcd.io/flux/components/kustomize/kustomizations/#health-checks).

Pruning must be disabled unless `allow_prune` explicitly authorizes the already
reviewed controller policy. Changing a watched Git branch may cause the
controller to apply or delete workloads under that policy. The handoff itself
does not perform blanket sync/prune operations or bypass policy checks.

## Inspect and explicitly perform the handoff

```bash
python3 scripts/ci_gitops_handoff.py \
  --plan private/ci-gitops-handoff.json \
  --bundle-dir private/rendered-forge-bridge \
  --pipeline-run '<COMPLETED_PIPELINE_RUN_NAME>'
```

Without a mode flag, this validates local inputs only. It reads no credential,
contacts no server, creates no proof directory and pushes no branch.

Use `--check-server --output-dir private/handoff-check-001` to authorize server
reads and local temporary Git fetches. Use `--apply` instead to authorize the
deployment-branch update and reconciliation wait. Server modes run on Linux
and require a new private proof directory. A journal is durable before the first
network operation and records the last verified phase after partial failure.

The explicit operation performs these checks in order:

1. Recheck the event receipt, exact successful PipelineRun/TaskRun/Pod identities,
   worker placement, commands, images and completion evidence. Keep child objects
   until promotion completes. The recorded report digest and current forge status
   must agree; a status string alone does not authorize deployment.
2. Verify the current source repository/head still matches the tested commit.
   Verify the CD object UID, spec hash, source, path and reconciliation policy.
3. Fetch Git objects into a temporary bare repository. Check that the selected
   manifest directory is a tree in the tested commit and the existing deployment
   tip is its ancestor. Branch creation is refused unless `allow_create_branch`
   explicitly permits it. No application checkout or repository hook runs here.
4. Recheck CI, source head and CD configuration immediately before pushing one
   exact refspec. Never force-push, prune refs, delete tags or rewrite history.
   Read the remote branch back even if the push acknowledgment was lost.
5. Wait for the same commit in the selected controller, while rechecking its UID,
   spec and the remote branch. A different promotion superseding it stops the
   verification rather than being mistaken for this operation's success.

Argo CD must report Synced, Healthy and a successful operation for the exact
commit; a changed branch also requires reconciliation after the attempt began.
Flux must have current-generation Ready source/Kustomization conditions with the
matching source artifact and last-applied revision. Stalled/reconciling states
do not pass. These are recorded controller outcomes, not independent continuous
application health or availability probes. An unchanged already-promoted commit
can reuse its recorded outcome; complete live workload probes remain required.

Source checks and branch writes are separate protocol operations. They do not
freeze concurrent source authors. The pre-push check establishes the observed
head at that time; Git's normal fast-forward checks prevent a rewind. Promotion
still requires protected branches, controlled writers and live concurrency tests.

## Failure evidence and recovery

The private `handoff-proof.json` records plan/contract hashes, run UID, tested
revision, observed previous/promoted revisions and the last completed phase.
It contains no credential or raw API/Git diagnostic. Existing proof directories
are never overwritten by a new invocation. Operational errors retain the journal.

If a push succeeds but the controller times out, the branch remains promoted.
The command reports failure and does not silently roll back Git or delete
resources. Inspect the journal and controller diagnostics before retrying. A
retry of the same still-authorized commit is idempotent; a newer deployment tip
must never be rewound. Recovery may need an explicitly reviewed forward fix or
the platform's separate restore procedure.

The controller wait is bounded by `timeout_seconds`. Git commands default to
180 seconds each; `PLATFORM_HANDOFF_GIT_TIMEOUT_SECONDS` can set a measured,
finite longer bound under the shared subprocess policy. Captured Git output is
also bounded and private diagnostics are suppressed in public output.

The disposable Forgejo and Gitea cases verified live event, build, status and
deployment correlation, scoped permission denials, and HTTP workload checks.
Live Flux promotion, signed artifact promotion, node/controller loss,
concurrency, upgrade and restore drills remain open for this integration.

## Optional signed Forgejo or Gitea Tekton image promotion

Render the [artifact bridge plan](../examples/tekton-forge-artifact-bridge.json)
with `scripts/render_tekton_forge_bridge.py`. Select `forgejo` or `gitea`, pin
the publisher's image manifest digest for the worker architecture, and protect
the source and deployment branches before enabling the push hook. The exact
local or remote CI Kubernetes target remains explicit in all
[five hosting modes](TEKTON_FORGE_BRIDGE.md).

For the RKE2, K3s, and existing Kubernetes workload packages, use
`kubectl apply --server-side --field-manager=platform-tekton-bridge` on
the reviewed RBAC and workload files. The embedded code makes the bundle
ConfigMap larger than the 256 KiB annotation limit that client-side apply
uses for its saved configuration.
[Kubernetes server-side apply](https://kubernetes.io/docs/reference/using-api/server-side-apply/),
[annotation limits](https://kubernetes.io/docs/concepts/overview/working-with-objects/annotations/).

The fixed PipelineRun has three steps. Checkout fetches the exact branch tip
using the read-only source credential and records a witness. The build reads
`/workspace/source` and writes the declared files under `/workspace/output`.
Publication reads that output and the checkout witness, then verifies and
publishes the image using a [scoped Harbor robot](HARBOR_STATIC_IMAGE.md).
Give the build step a fixed argument vector. Keep the source, receiver,
reporter, registry trust, and robot inputs separate.

- Put the exact Forge CA bytes in `ca.crt` for the checkout, receiver,
  reporter, and promoter. Pin those bytes with `source.ca_sha256`.
- In the job namespace, provision `publication.trust_secret` with `ca.crt`
  and `publication.robot_secret` with `credential.json`. Pin the Harbor
  trust, leaf, and credential hashes in the plan. The listed mount paths in
  the example identify the inputs inside the publisher container.
- Use the separate reporter from the rendered package. It validates the
  original PipelineRun, TaskRun, worker Pod, and matching artifact results
  before recording the verified status. Retain these objects and both
  status receipts until release verification completes.

Copy the [promotion plan](../examples/tekton-forge-argocd-artifact-promotion.json).
Set its PipelineRun name, source revision, artifact policy, and Argo CD
Application identity. Give the CI observer `get` access to PipelineRuns,
TaskRuns, Pods, receipt ConfigMaps, the cluster identity, and the worker Node.
Give the CD observer `get` access to the reviewed Application, cluster identity,
and original Deployment. The Git writer needs branch-specific write access.
Place their separate credentials in the [promoter input directory](#inputs-and-authority-separation).

```bash
umask 077
python3 -B scripts/tekton_forge_artifact_promotion.py \
  --plan private/tekton-artifact-promotion.json \
  --bundle-dir private/tekton-artifact-bridge \
  --input-dir /run/platform/promoter \
  --prepare-signature > private/tekton-release-request.json
```

Have the release signer approve the digest and every annotation using the
[Cosign inputs](IMAGE_SIGNATURE_VERIFICATION.md). Reuse the same plan and
bundle with `--check-server --output` and then `--apply --output`, each with
a new private proof directory. The command rechecks the original CI evidence
and verified signature before the Git lease, then waits for the same
Deployment to run the approved digest.

Separate disposable runs with Forgejo 15.0.6 and Gitea 28.3.0 used Tekton
1.4.0 and Argo CD 3.5.0. Each verified the protected push, checked the
original result and Harbor bytes, and served the approved image on three K3s
workers. Replaying the release made no Git push. Kyverno refused all 12
invalid admission cases per forge. The Kubernetes bundle also passed
repeat server-side apply and restricted Pod admission on a disposable API server.

A separate hybrid Forgejo rehearsal passed on RKE2 v1.36.4+rke2r1 with
three control-plane guests and two dedicated workers. Forgejo, the receiver,
the reporter, and Argo CD ran as native processes in the isolated lab.
The protected push triggered the exact-revision Tekton build and Harbor
publication. Both workers served the signed image, three Kyverno admission
replicas enforced all 12 denial cases, and promotion replay made no extra
push. The outer Harbor checks also verified the original token through core
peer loss and database/cache promotion.

The longer lab sets a finite one-hour Harbor token lifetime through
`CONFIG_OVERWRITE_JSON` and checks the issued JWT lifetime before booting
the guests. Admission audits retry classified read-transport failures within
a 90-second deadline. The ephemeral-container denial uses a server dry-run
JSON patch bound to the original Pod UID and container specification.
The source hashes, retained receipts, and original unit and guest cleanup
passed independent inspection. Full-stack lifecycle and physical HA
qualification remain open.

## Woodpecker and Argo CD

Use [the Woodpecker plan](../examples/woodpecker-argocd-handoff.json) for a
protected Gitea or Forgejo branch built by Woodpecker. Review the exact pipeline
number, revision, forge and repository IDs, and required workflow step names.
The verifier reads the forge branch and Woodpecker's pipeline records. It
requires a successful original push pipeline and successful executions of every
named step. Include the clone step and the checks required by your release policy.

Bind the deployment branch to a single Argo CD Application tracking the same
repository over HTTPS. Use the cluster's `kube-system` UID, Application UID, and
reviewed Application `spec`. Calculate `bridge_contract_sha256` over the
`pipeline` object and `spec_sha256` over the Application `spec` with
`ci_gitops_handoff.sha256`. Keep the deployment branch restricted to the
promotion writer.

Store credentials in a private input directory with the following files.

- `woodpecker-token` reads the reviewed Woodpecker repository and pipeline.
- `api-token` reads the forge repository and branch protection metadata.
- `token` authenticates Git as the restricted promotion writer.
- `ca.crt` contains the trust bundle for both HTTPS endpoints.
- `cd-kubeconfig` identifies the management cluster and can read the reviewed
  Application and the cluster identity.

Run local validation with `--plan` only. For a read-only check against the
services, pass `--check-server`. For a promotion, use `--apply` with a fresh
proof directory below the repository's ignored `private/` directory.

```bash
python3 scripts/woodpecker_gitops_handoff.py \
  --plan private/woodpecker-handoff.json \
  --input-dir /run/platform/promoter \
  --check-server --output-dir private/woodpecker-handoff-check
```

The promotion advances the deployment branch with a Git lease and waits for
Argo CD to report the same revision as Synced, Healthy, and Succeeded. A rerun
can verify an already-promoted revision without rewriting its branch.

### Optional signed Woodpecker image promotion

Use the [publisher contract](../examples/woodpecker-static-image.json) for one
Forgejo or Gitea repository, protected source branch, workflow, and plugin step.
The plugin independently fetches the exact branch tip over trusted HTTPS and
publishes the listed static files through the [scoped Harbor robot](HARBOR_STATIC_IMAGE.md).
Review and pin a base image with Python 3.11 or later, Git, and `/bin/sh`.
Prepare a build context in a new private directory, then build and publish
the image to the registry used by your Woodpecker agents.

```bash
python3 -B scripts/prepare_woodpecker_publisher_image.py \
  --base-image "$REVIEWED_BASE_IMAGE" \
  --output private/woodpecker-publisher
docker build --network=none -t "$PUBLISHER_UPLOAD_TAG" private/woodpecker-publisher
docker push "$PUBLISHER_UPLOAD_TAG"
```

The build context records the reviewed publisher sources and copies them
into a UID/GID 1000 image with a fixed entry point. Use the pushed manifest
digest for the plugin image and its secret restriction. Keep the contract
and credentials in operator-managed inputs.

Encode the contract JSON as base64 and calculate its canonical SHA-256 with
`woodpecker_static_image.fingerprint`. Generate a separate random 32-byte
receipt key for this repository. Pin its SHA-256 in the contract. Store the
private input bundle as a repository secret restricted to the exact publisher
image digest and push events. The bundle is base64-encoded JSON with base64
values for `source_ca`, `source_token`, `registry_ca`, `registry_credential`,
and `receipt_key`. Pass the exact Forge CA and original Harbor credential
bytes. [Woodpecker secret restrictions](https://woodpecker-ci.org/docs/3.16/usage/secrets)

```yaml
steps:
  - name: publish-static-image
    image: registry.example.invalid/ci/static-publisher@sha256:REVIEWED_IMAGE_DIGEST
    depends_on: [verify-checkout]
    when:
      - event: push
        branch: main
    settings:
      contract_b64: REVIEWED_BASE64_CONTRACT
      contract_sha256: REVIEWED_CONTRACT_SHA256
      private_inputs_b64:
        from_secret: static-publisher-inputs
```

On the Kubernetes backend, configure a restricted worker namespace and a
workflow workspace StorageClass. Use a job ServiceAccount with token
automount disabled. Apply the following backend options to the publisher
and apply the same restrictions to the workflow's clone and build steps.
Limit Secret reads and execution into job Pods to trusted CI identities.
Woodpecker 3.16 references a generated step Secret from the publisher
container's environment.
[Woodpecker Kubernetes security context](https://woodpecker-ci.org/docs/3.16/administration/configuration/backends/kubernetes#security-context)

```yaml
backend_options:
  kubernetes:
    securityContext:
      runAsNonRoot: true
      runAsUser: 1000
      runAsGroup: 1000
      fsGroup: 1000
      allowPrivilegeEscalation: false
      seccompProfile:
        type: RuntimeDefault
      capabilities:
        drop: [ALL]
```

For a dedicated trusted local agent, install an operator-owned executable that
invokes the same entry point. Use its registered executable name as the step
image and allow it in the repository secret filter. The local backend gives
pipeline processes the agent's host access. [Woodpecker local backend](https://woodpecker-ci.org/docs/administration/backends/local)

Copy the [signed promotion plan](../examples/woodpecker-argocd-artifact-promotion.json).
On a dedicated Docker agent, set `WOODPECKER_BACKEND=docker` and point
`WOODPECKER_BACKEND_DOCKER_HOST` at that agent's Unix socket. Give the agent
account access to the socket and keep it out of the job containers. Route
the workflow to the Docker backend with `labels: {backend: docker}`. Use
the same pinned publisher image and image-restricted repository secret, and
set `backend_options: {docker: {user: "1000:1000"}}` on the publisher step.
Allow the job network to reach the forge HTTPS origin and Harbor. The
publisher receives the reviewed CA bundles in its step input. Configure the
clone image's forge trust independently.
[Woodpecker Docker backend](https://woodpecker-ci.org/docs/administration/configuration/backends/docker)

Set the required workflow steps to include the reviewed publisher. Pin the
existing deployment branch tip and the original Deployment UID, destination,
and container. The Argo CD source directory contains the reviewed Deployment
JSON. Give the observer `get` access to the Deployment. In the private operator
directory, add the original binary `receipt-key` and the `woodpecker-ca.crt`
bundle. `ca.crt` contains the reviewed Forge CA. Repeat the selected workflow
and repository identities in the publisher and promotion plans.

```bash
umask 077
python3 -B scripts/woodpecker_artifact_promotion.py \
  --plan private/woodpecker-artifact-promotion.json \
  --publisher-contract private/woodpecker-static-image.json \
  --input-dir /run/platform/promoter \
  --prepare-signature > private/woodpecker-release-request.json
```

The command authenticates the receipt from the exact successful Woodpecker
step log and returns the digest and release annotations. It allows a brief
wait for the exact step's log to become available after pipeline success.
Have a separate signer sign the `image` with every annotation, using the
[Cosign inputs](IMAGE_SIGNATURE_VERIFICATION.md).
Run the same command with `--check-server --output` and then `--apply --output`,
each with a new private proof directory. Apply rechecks the pipeline, receipt,
and signature before the Git lease. It observes the original Deployment at
the selected digest. Retain the private proof if a run stops after pushing.
[Woodpecker pipeline log API](https://woodpecker-ci.org/api)

Separate disposable runs with Forgejo 15.0.6 and Gitea 28.3.0 used Woodpecker
3.16.0's local backend and native Argo CD 3.5.0. Each compared the exact job
receipt with the Harbor manifest and served bytes, then promoted one signed
digest. Three K3s workers ran that digest and served the expected page. An
apply replay made no Git push. Kyverno rejected all 12 negative admission
cases in each run, covering unsigned regular, init and ephemeral containers,
an untrusted signing key and mismatches in each required annotation.

Separate Forgejo and Gitea runs used the same digest-pinned publisher image
on Woodpecker 3.16.0's Kubernetes backend. The Helm-managed agent ran the
publisher on a restricted K3s worker as UID 1000 and passed the publication
inputs through its generated step Secret. Each run verified the exact
receipt and Harbor bytes, deployed the signed digest across three workers,
replayed without a Git push, and passed all 12 admission rejection cases.
The generated Dockerfile built against the pinned Linux AMD64 base in an
isolated Docker engine. Forgejo and Gitea also passed the publication stage
on a Docker agent using the reviewed publisher files in a digest-pinned OCI
image. The publisher ran as UID 1000. The clone and checkout containers
received no publisher input, and Woodpecker removed the per-job containers,
network, and workspace volume.

Separate Forgejo and Gitea Docker runs verified the Cosign signature and
transparency proof, promoted each artifact once, and confirmed the pinned
digest was ready and serving on three K3s workers. Each run rejected
twelve invalid-image fixtures through Kyverno. Physical failure recovery
and complete stack qualification remain open.

### Local Woodpecker and Argo CD validation

Isolated Linux tests used Gitea 28.3.0 and Forgejo 15.0.6 with Woodpecker
3.16.0's local backend and Argo CD 3.5.0 against a disposable Kubernetes API.
For each forge, the test rejected an absent required step, promoted the exact
successful private-repository commit, and verified that Argo CD applied the
ConfigMap from that revision. Repeating the handoff left the branch at the
same commit.

A separate Forgejo 15.0.6 run exercised Woodpecker 3.16.0's Kubernetes
backend on a K3s v1.35.0+k3s1 VM worker. The agent used a short-lived
credential from the Helm-rendered Role and ServiceAccount. The private push
created a ReadWriteOnce workspace claim and ran the clone and verification
steps under restricted Pod Security. The verification step ran as UID 1000
without a service account token and wrote the tested revision to the shared
workspace. Forgejo received a successful commit status.
The handoff then rejected a plan with a missing required step, promoted the
same worker-tested commit, and verified that Argo CD applied its ConfigMap
to a separate disposable Kubernetes API. A repeated invocation verified the
same revision without another push.

Separate Forgejo 15.0.6 and Gitea 28.3.0 runs deployed the Helm-rendered
Woodpecker agent StatefulSet inside K3s using the pinned 3.16.0 agent image.
In each run, the replacement agent Pod became Ready with a projected token for
the chart's ServiceAccount and executed a new private-repository pipeline.
Argo CD 3.5.0 applied the exact worker-tested revision's ConfigMap in a
separate disposable Kubernetes API. Repeating the promotion left the deployment
branch at the same commit.

The worker test used a disposable local volume, a preloaded clone image
containing the fixture CA, and the namespace's default ServiceAccount with
token automount disabled. For restricted namespaces, set non-root execution,
`RuntimeDefault` seccomp, disabled privilege escalation, and dropped
capabilities on each pipeline step, including the clone step. The Helm
agent's security context applies to the agent itself. The in-cluster test used
a single agent and a single Woodpecker server outside K3s.

### Docker Compose forge with Woodpecker and K3s

The [host forge setup](FORGE_HOST.md#connect-a-host-forge-to-woodpecker-and-argo-cd)
uses the forge's published HTTPS URL for browser login, worker clones, and
Argo CD reads. Keep the webhook receiver's address in the forge allowlist and
register Woodpecker's published HTTPS callback in the forge's OAuth application.

Separate Forgejo 15.0.6 and Gitea Community 28.3.0 runs used the rendered rootless
Compose packages, HAProxy TLS, Woodpecker 3.16.0, and native Argo CD 3.5.0.
Each verified Woodpecker OAuth sign-in, private repository activation, and
push-triggered clone and verification steps on a K3s worker after the
Helm-rendered agent Pod was replaced. The handoff
rejected a missing required step and used separate Git identities to read the
source, push the deployment branch, and fetch it for Argo CD. Argo CD synced the
tested commit onto the same K3s cluster, and the deployment served HTTP from
both worker nodes. Repeating promotion left the branch at the same commit.
Replacing each forge container preserved the repository ID, both Git refs and
the Woodpecker commit status. Gitea used strict egress with the Woodpecker
endpoint's explicit IP and port.

The forges used disposable SQLite databases. These initial runs used a native
Woodpecker server and an agent running in K3s.

Follow-up runs for both Forgejo and Gitea used the rendered forge and Woodpecker
Compose packages. The Woodpecker server used file-backed OAuth and agent
credentials and published HTTP and gRPC on loopback. HAProxy routed HTTPS to
both services and carried agent gRPC on the isolated test network. Replacing
each Woodpecker container preserved the completed pipeline and authenticated
API access. Forge replacement preserved the repository ID, source and
deployment refs, and commit status. Argo CD ran natively. The runs did not
exercise agent gRPC over TLS, PostgreSQL promotion, or physical HA.

### Run Woodpecker on a Docker or native Linux host

The host renderer packages a single Woodpecker scheduler for Forgejo or Gitea.
Use the [Docker plan](../examples/woodpecker-server-docker.json) or the
[systemd plan](../examples/woodpecker-server-native.json) as a private template.
The Docker example pins the Linux AMD64 server image. For systemd, supply the
path and SHA-256 of the installed server executable and the dedicated service
account.

```bash
python3 scripts/render_woodpecker_host.py \
  --plan private/woodpecker-host.json \
  --output-dir private/rendered-woodpecker
```

1. Create the durable state directory, writable by the service account. The
   Docker package runs as UID/GID 1000. The inputs directory must be separate
   and contain mode `0600` files readable by the server account.
2. Register a confidential OAuth application on the forge for the Woodpecker
   HTTPS origin and its `/authorize` callback. Put the resulting credentials
   in `oauth-client-id` and `oauth-client-secret`. Put the shared agent
   secret in `agent-secret` and provision the same value through the agent's
   private credentials. With closed registration, list each initial Woodpecker
   administrator in `admins`. [Woodpecker forge settings](https://woodpecker-ci.org/docs/administration/configuration/forges/overview)
3. Select SQLite for local persistent state or PostgreSQL for an external
   database. For PostgreSQL, put the connection URL in `database-datasource`
   and configure `sslmode=verify-full` with the appropriate PostgreSQL CA.
   If the forge requires private HTTPS trust, enable `custom_https_ca` and
   install `https-ca-bundle.crt` with the full required trust chain. Supply
   the same forge trust to the clone step. If provisioning a custom trusted
   clone image, select it by digest. [Woodpecker server configuration](https://woodpecker-ci.org/docs/administration/configuration/server)
4. Route the public HTTPS origin through the reverse proxy to the generated
   loopback HTTP listener. Forward the original host, scheme and path.
   Keep the gRPC backend on loopback and publish a separate private endpoint
   for agents. For networked agents, terminate TLS with HTTP/2 at the proxy
   and forward HTTP/2 to the loopback gRPC backend. Set `WOODPECKER_SERVER`
   to the private proxy hostname and port. Launch the agent with
   `agent --grpc-secure=true --grpc-skip-insecure=false` so it verifies the
   server certificate. Woodpecker 3.16 maps the older
   `WOODPECKER_GRPC_VERIFY` variable to the skip-verification flag.
   Allow the Woodpecker HTTPS endpoint in the forge's webhook settings.
   [Woodpecker 3.16 agent flags](https://github.com/woodpecker-ci/woodpecker/blob/v3.16.0/cmd/agent/core/flags.go)
5. Start the reviewed service with Docker Compose or install the rendered
   systemd unit and `binary.sha256` in their designated locations. Connect
   separately deployed Woodpecker agents to the private gRPC endpoint. Activate
   the private repository through Woodpecker, push a test commit, and use the
   [handoff](#woodpecker-and-argo-cd) to verify and promote the tested revision.

#### Package a separately hosted agent

Copy the [Compose agent plan](../examples/woodpecker-agent-docker.json)
or [native agent plan](../examples/woodpecker-agent-native.json) to
`private/woodpecker-agent.json`. Both packages use a dedicated Docker
executor and a certificate-verified gRPC connection. The native package
runs the host agent executable through systemd.

```bash
python3 scripts/render_woodpecker_agent.py \
  --plan private/woodpecker-agent.json \
  --output-dir private/rendered-woodpecker-agent
```

Give each agent its own state directory. Put its private registration
token in `agent-secret` and, when using a private CA, its trust bundle in
`https-ca-bundle.crt` in the separate inputs directory. Make those files
readable by the service account. For Compose, the agent runs as UID/GID
1000 and receives `socket_gid` as a supplementary group. For systemd,
install the pinned executable and put the rendered `binary.sha256` in
the inputs directory. Use `socket_group` for the dedicated engine's Unix
socket group. Limit access to that socket to the agent and engine
operators, since it controls the executor host. Keep the agent token
and socket out of workflow volumes.

Start the rendered Compose service or install and start the rendered
systemd unit. Confirm that the expected agent is online in the Woodpecker
administration page before pushing a job labeled `backend: docker`.

The Woodpecker 3.16.0 TLS test exercised the rendered Compose service and
the native launch command. Both reached a trusted HTTP/2 endpoint and
rejected a certificate for the wrong hostname and a certificate signed
by an untrusted authority. Both registered their Docker backend through
a TLS-terminating HAProxy endpoint and reported a fresh heartbeat with
the same server-side agent ID after restart.

### Run Argo CD reconciliation from Docker

The [Argo CD host plan](../examples/argocd-host-docker.json) runs the
repository server and one application controller against a prepared
Kubernetes management cluster. It pins the Linux AMD64 image and uses the
Compose network for repository RPC. The Kubernetes API remains the source
of Application, AppProject, repository and destination configuration.

```bash
python3 scripts/render_argocd_host.py \
  --plan private/argocd-host.json \
  --output-dir private/rendered-argocd
```

Install the matching Application and AppProject CRDs, Argo CD settings,
and namespace-scoped controller permissions on the management cluster.
Check its TLS endpoint, kubeconfig context and `kube-system` namespace UID
against `management-target.json` before starting Compose. The controller
requires a private, self-contained `kubeconfig` with the selected context
and least-privilege credentials. Use a separate workload credential in the
Argo CD cluster Secret and restrict each AppProject to its reviewed repos,
destination namespaces, and resource kinds.
[Argo CD declarative setup](https://argo-cd.readthedocs.io/en/stable/operator-manual/declarative-setup/)

Use this pair as the active reconciler for the selected namespace. Keep a
single controller managing those Applications during installation and recovery.
Set `resync_seconds` to match the operational polling interval. The same
setting controls Git revision-cache expiry, which bounds how long a new
deployment branch can be hidden by a previously cached ref list.

Use separate directories readable by UID/GID 999 for the two containers.

- Both need `cache/ca.crt`, `cache/tls.crt`, `cache/tls.key`, and
  `cache/credentials/auth` for a dedicated mTLS Valkey endpoint. For a named
  ACL user, include `cache/credentials/auth_username`.
- The repository server needs its client CA at `repo-ca.crt` and its
  serving certificate and key at `config/reposerver/tls/tls.crt` and
  `config/reposerver/tls/tls.key`. The serving certificate must cover
  `argocd-repo-server`. Place private Git CAs in `git-trust/` using
  the Git server hostname as the filename.
- The controller needs the serving CA at `repo-ca.crt`, a trusted client
  certificate and key at `repo-client.crt` and `repo-client.key`, and
  the management `kubeconfig`.
  [Argo CD repo-server mTLS](https://argo-cd.readthedocs.io/en/stable/operator-manual/mtls/)

For a replicated cache, use a dedicated
[Valkey/Sentinel package](ARGOCD_VALKEY.md) prepared for the Argo CD
namespace. Remove `cache_host` and `cache_port` from the private host plan.
Pin the cache contract in the client plan and set
`application.private_dir` to `/run/platform/argocd/cache`. Render with both
cache inputs.

```bash
python3 scripts/render_argocd_host.py \
  --plan private/argocd-host.json \
  --cache-bundle-dir private/generated/argocd-cache \
  --cache-client-plan private/argocd-valkey-client.json \
  --output-dir private/rendered-argocd
```

In each repository, controller, and optional API input directory, prepare
`cache/tls/{ca.crt,tls.crt,tls.key}` and the four
`cache/credentials/` files named in the client-file mapping. The renderer
uses the pinned Sentinel endpoints for the API's two cache clients as well
as the controller and repo server. Every advertised primary and Sentinel
address must be reachable from Compose and protected by a matching TLS
certificate. This includes promoted members. Kubernetes-only service DNS
requires routing and DNS from the Compose host before it can be used.
[Argo CD Sentinel client implementation](https://github.com/argoproj/argo-cd/blob/v3.5.0/util/cache/cache.go)

In both the Forgejo and Gitea Woodpecker/K3s runs, Sentinel promoted a
replica after the primary and its local Sentinel stopped. The existing API
returned the resource tree written to the promoted member. Forgejo needed
a repo-server replacement to restore manifest generation. Gitea needed an
application-controller replacement to update the resource tree. The former
Valkey primary rejoined as a replica in both runs.

Verify Sentinel agreement and the new primary's writability first.
If authenticated manifest requests keep timing out, replace the repo server.

```bash
docker compose -f private/rendered-argocd/compose.yaml \
  up -d --no-deps --force-recreate argocd-repo-server
```

If manifests are available but the resource tree stays stale after an
Application refresh, replace the application controller.

```bash
docker compose -f private/rendered-argocd/compose.yaml \
  up -d --no-deps --force-recreate argocd-application-controller
```

Confirm manifest and resource-tree reads, the expected synced revision,
and application health after recovery.

You can also point `cache_host` and `cache_port` at the
[Valkey application gateway](VALKEY_HA.md) on port 6381. Use its reviewed
DNS identity, a dedicated Valkey group for Argo CD, and
`frontend.require_client_certificate: true` in the gateway plan. Put the
gateway's serving CA and the application client certificate/key in each
cache client's direct-mode input paths. Put the Valkey data password and
username in `cache/credentials/auth` and `cache/credentials/auth_username`.
The gateway checks the Sentinel majority and routes to the approved
writable primary. The client still uses the Valkey data ACL. For independent
gateway hosts, provide a redundant entry point to the gateway pair.

The Forgejo and Gitea Woodpecker/K3s tests stopped one gateway process,
then stopped the Valkey primary and its local Sentinel. Through the
surviving gateway, the original Argo CD repo server regenerated manifests,
the original controller updated the resource tree, and the original API
served that tree. The gateway also rejected a client with valid Valkey
credentials and no client certificate. Both tests used the surviving
gateway as their endpoint in a single-host lab.

For a stable entry point, enable the gateway's
[optional Keepalived VIP](VALKEY_HA.md#optional-tls-valkey-replication-and-sentinel-packages)
and point the cache DNS name used by Argo CD at the VIP. In the Forgejo
and Gitea K3s labs, two isolated gateway network namespaces shared the
address. The standby took over after stopping HAProxy on the owner.
Takeover took 3.4 seconds with Forgejo and 4.2 seconds with Gitea.
Each test then stopped the Valkey primary and its local Sentinel. The
original Argo CD containers rebuilt manifests and read the updated tree
using the same VIP after promotion. Both tests used one physical host.
Physical gateway or cache host loss still needs validation.

From the host with those directories mounted, start the reviewed package.

```bash
docker compose -f private/rendered-argocd/compose.yaml up -d
```

For the API and web UI, use the [API-enabled plan](../examples/argocd-host-docker-ui.json).
The optional `api_server` section selects a dedicated inputs directory and
a host loopback port. Prepare the same input filenames as for the controller,
using a separate API service account and client key. Set `argocd-cm.data.url`
to the browser-facing HTTPS origin. Install its certificate and key in the
`argocd-server-tls` Secret and use TLS passthrough to the host port. Configure
Argo CD RBAC and authentication on the management cluster. The API container
uses its own Kubernetes permissions and honors Argo CD application RBAC.
Pre-create an empty `default` AppProject if the API service account has
read-only access to projects. Put authorized applications in dedicated projects.
[Argo CD TLS configuration](https://argo-cd.readthedocs.io/en/stable/operator-manual/tls/)

The same example enables the ApplicationSet controller. Remove
`application_set` from the private plan to omit that service. Install the
matching ApplicationSet CRD and use a dedicated namespace-scoped service
account. Prepare `kubeconfig`, `repo-ca.crt`, `repo-client.crt`, and
`repo-client.key` in its separate inputs directory. Set that kubeconfig's
`current-context` to the plan's selected context because the ApplicationSet
manager uses it to establish its Kubernetes client. The controller can create
and update generated Applications. Its configured policy disables deletion
by reconciliation and per-set policy overrides. Limit the service account
to Application creation and updates, ApplicationSet status and finalizer
updates, and the reads it needs for settings and projects. Leave application
deletion out of that role. Set `preserveResourcesOnDeletion: true` on sets
whose generated workloads must survive deletion of the parent.
[ApplicationSet resource modification](https://argo-cd.readthedocs.io/en/stable/operator-manual/applicationset/Controlling-Resource-Modification/)

The ApplicationSet controller's SCM and pull request generators are disabled.
Its metrics, probes, and webhook listen on container loopback.

The example also enables the notifications controller. Omit `notifications`
to skip it. Run one notifications controller per management namespace.
Give it a separate inputs directory containing `kubeconfig`, `repo-ca.crt`,
`repo-client.crt`, `repo-client.key`, and `notification-ca-bundle.crt`. The last
file contains the CA roots trusted for outgoing HTTPS connections. Use a
different service account from the application controller. Scope it to the
management namespace. Grant list and watch on ConfigMaps and Secrets for
its informers. These permissions allow reading Secret contents across the
namespace. Grant get on `argocd-notifications-cm` and
`argocd-notifications-secret`, and get, list, and watch on Applications
and AppProjects. Grant patch and update only on Applications whose
notification state it manages.

Store triggers, templates, and endpoints in the management namespace's
`argocd-notifications-cm`. Put credentials in
`argocd-notifications-secret` and reference them from the ConfigMap by key.
Subscribe Applications or AppProjects to the intended trigger. The Compose
service has no published ports, and self-service configuration in other
namespaces is disabled.
[Argo CD webhook notifications](https://argo-cd.readthedocs.io/en/stable/operator-manual/notifications/services/webhook/)
[Argo CD notification subscriptions](https://argo-cd.readthedocs.io/en/stable/operator-manual/notifications/subscriptions/)

To replace only the notifications container, use the same Compose file and
project with the existing private inputs and management namespace.

```bash
docker compose -f private/rendered-argocd/compose.yaml \
  up -d --no-deps --force-recreate argocd-notifications
```

Check the receiver for delivery and keep the Application's
`notified.notifications.argoproj.io` annotation, which records delivery
state. With a revision-based `oncePer` trigger, a recovered controller can
send an eligible notification for the current Application status and use
the recorded state to suppress a repeat. A transient status must still be
observable when the controller evaluates the trigger. Use idempotent
receivers for retries and for interruption between delivery and recording.
[Argo CD notification triggers](https://argo-cd.readthedocs.io/en/stable/operator-manual/notifications/triggers/)

Separate Forgejo and Gitea runs used the rendered forge, Woodpecker server,
and both Argo CD containers with a native mTLS Valkey endpoint. For each
forge, Argo CD synced the exact Woodpecker-tested revision to K3s and the
service responded through both workers. The isolated Docker engine disables
Docker's DNS redirect, so the test resolved `argocd-repo-server` via a fixed
fixture address and used the same hostname in mutual TLS. Container image
content was verified before loading under offline tags.

Separate Forgejo and Gitea runs with the API enabled served the web UI over
verified HTTPS. In each case the application API denied anonymous access and
a bad password. A project-scoped observer signed in and read the healthy
Application at the exact deployed revision. The API denied the observer's
sync request.

With ApplicationSet enabled, both forges also passed the List generator
test. The controller created the Application from the template and updated
it in place before promotion. The test verified the parent owner reference
and checked that the generated Application had no resource-deletion
finalizer. Kubernetes RBAC denied the ApplicationSet identity permission to
delete Applications, patch Secrets, or create an Application outside
`argocd`. The generated Application deployed the exact CI-tested commit
and responded over HTTP through both K3s workers.

Both forges passed the notifications test as well. After the generated
Application reached Synced and Healthy at the Woodpecker-tested revision,
the controller delivered the Application name and exact commit to a
private HTTPS receiver. The receiver checked the bearer credential from
the notifications Secret. Kubernetes denied Secret updates, Application
updates outside the namespace, and updates to a different Application in
the same namespace. In a separate recovery test with each forge, the
notifications container was stopped before promotion. The Application
became Healthy and Synced while the container was down. A replacement
container delivered the notification and recorded it on the Application.
A second replacement delivered a new trigger after an ApplicationSet
update and did not resend the recorded deployment notification.
This covers single-host container recovery for a persistent trigger.
SMTP and cross-host failover still need live checks.

### Woodpecker server scheduling and recovery

A local test ran two Woodpecker 3.16.0 servers against PostgreSQL 18.6. A
Forgejo push reached one server while the only agent polled the other. Both
servers could read the same pipeline, but only the webhook server queued the
job. The agent's server still showed an empty queue after the webhook server
was killed. Restarting the surviving server recovered the pending job from
PostgreSQL, and the same pipeline finished without a rerun and posted success
to Forgejo. The test used the local agent backend.

A separate local test killed the single active Woodpecker 3.16.0 server
after an agent had started a workflow. After server restart the pipeline
still read as Running in PostgreSQL, but the server had no running task and
the original step had not completed. The test canceled the stale pipeline
record, stopped the original agent and its local job process group, and
registered a replacement agent. An explicit rerun of the same commit then
completed and reported success to Forgejo. Only that attempt reached the
fixture's completion marker.

Separate Forgejo 15.0.6 and Gitea 28.3.0 runs on disposable K3s v1.35.0+k3s1
reproduced the same stale Running status with a Helm-managed Woodpecker
3.16.0 agent and a Kubernetes job. In both runs, after the
PostgreSQL-backed Woodpecker server restarted, the old job container was
still running. Canceling the pipeline changed its CI status while the job Pod
remained. The test stopped the agent, deleted the exact job Pod, verified
its container had stopped in containerd, and removed the job's workspace
claim. The underlying volume used the Retain reclaim policy. A replacement
agent ran the original commit on a fresh workspace, and the forge received the
successful commit status.

A separate Forgejo test used two K3s workers with Woodpecker 3.16.0. The test
paused the worker VM that hosted the agent and the running job, and waited
for Kubernetes to mark that node unavailable. The Woodpecker server and
second worker stayed up. After confirming the first VM was still paused, the
test verified the interrupted pipeline had reached a terminal state, removed the old Pod records and
workspace claim, and preserved the original local volume under its Retain
policy. An agent on the second worker ran an explicit retry of the same
commit on a fresh workspace and posted success to Forgejo. The original VM
remained paused through completion.

The live handoff verifier rejected the successful manual rerun while its
commit was still the protected branch head. A subsequent reviewed commit
triggered a fresh push pipeline, and Argo CD 3.5.0 applied that exact
revision's ConfigMap. Repeating promotion left the deployment branch at
the same commit. Use a fresh reviewed push when the original push pipeline
was interrupted and its manual rerun is the only successful attempt.

Run one active server with independently scaled agents. On an existing
multi-replica deployment, pause new submissions and review running and queued
pipelines before setting `WOODPECKER_SERVER_REPLICAS=1` in the private
deployment environment. Keep the existing PostgreSQL database and Secrets.
Render and sync the private values, then verify the remaining server, agent
connectivity, any pending pipelines, and a new push-to-success pipeline.
When a job was running at the time of a crash, compare the pipeline and
workflow status with actual worker execution before rerunning. On Kubernetes,
use the workflow's numeric ID to identify its job Pods in the configured
build namespace.

```bash
kubectl --context "$CI_CONTEXT" -n "$CI_JOB_NAMESPACE" get pods \
  -l "woodpecker-ci.org/task-uuid=$WORKFLOW_ID" -o wide
```

Verify the workflow ID, cluster, namespace, Pod UID, and affected claims.
Stop any orphaned execution and verify external writes from the interrupted
attempt before restarting it. If the node is unreachable, verify it is
fenced before retrying. Preserve the old workspace when needed for
investigation or recovery. Force-deleting a StatefulSet Pod on an unreachable
node releases its identity in the API before the kubelet confirms the
process stopped. The VM test used a hypervisor fence before removing any
records. Automatic rescheduling, failed-worker rejoin, and shared-storage
reattachment still need acceptance. See the
[Kubernetes StatefulSet force-deletion guidance](https://kubernetes.io/docs/tasks/run-application/force-delete-stateful-set-pod/).

The queue is local to the server process and loads persisted tasks at startup.
See the upstream
[server setup](https://github.com/woodpecker-ci/woodpecker/blob/v3.16.0/cmd/server/setup.go#L102-L106)
and [queue persistence](https://github.com/woodpecker-ci/woodpecker/blob/v3.16.0/server/queue/persistent.go#L28-L35).
The same in-memory queue backend appears in the
[3.18.1 server setup](https://github.com/woodpecker-ci/woodpecker/blob/v3.18.1/cmd/server/setup.go#L102-L106)
and [3.19.0 server setup](https://github.com/woodpecker-ci/woodpecker/blob/v3.19.0/cmd/server/setup.go#L102-L106).
Multi-server scheduling on those versions would need its own runtime test.
