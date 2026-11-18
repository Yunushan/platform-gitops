# Forgejo and Gitea push-to-Tekton bridge

Status: opt-in receiver and application-job packages. Offline tests cover
signature validation, repository/revision binding, retry receipts and generated
credential boundaries. Real Forgejo and Gitea pushes, K3s worker builds,
commit-status read-back and Argo CD deployment passed in separate disposable
lab runs. HA, upgrade and restore remain open.
[Results runbook](TEKTON_FORGE_RESULTS.md#disposable-forgejo-and-gitea-validation-on-k3s)
The full requested
[stack expansion](MULTI_RUNTIME_STACKS.md) remains in progress.

## Contract and supported event

This adapter accepts JSON `POST /hooks/push` deliveries from exactly one
configured Forgejo or Gitea repository and branch. It verifies HMAC-SHA256 over
the original bytes, using the selected provider's signature/event headers;
duplicate signature headers, ambiguous JSON, tags and branch deletions fail.
It uses neither compatibility SHA-1 headers nor a secret inside the payload.
See the primary [Forgejo webhook documentation](https://forgejo.org/docs/latest/user/repository/webhooks/)
and [Gitea delivery contract](https://docs.gitea.com/usage/repository/webhooks/).

Before a new submission, the receiver independently reads repository identity
and branch head from the configured HTTPS API. The numeric ID, full repository
name and signed commit must agree. Clone URLs or pipeline definitions from a
payload are ignored. The configured Git URL must match the canonical API
origin/base path and repository exactly. Source redirects and insecure TLS are
refused. Events for an already superseded branch head do not start a new build.

There is no pull-request execution, merge gating, Gerrit adapter or approval
vote in this package. It runs the administrator-reviewed build command for
pushes to the selected branch. Do not attach it to untrusted repositories or
call the resulting Pod an untrusted-code sandbox.

## Fixed job and checkout identity

Copy [the example](../examples/tekton-forge-bridge.json) into ignored private
storage and replace all placeholders. Pin the bridge, checkout and build images
by digest. The bridge image must provide Linux Python 3 and a compatible
`kubectl`; the checkout image needs Git, Python 3 and `/usr/bin/env`. The build
image must support the fixed `build_argv`. Review image provenance and licenses.

### Prepare the bridge image

The receiver and reporter can share a Linux amd64 image. Select a
digest-pinned Linux amd64 base with Python 3.11 or newer. Obtain the Linux
amd64 kubectl binary and its reviewed SHA-256 checksum. Select a kubectl
release compatible with every API server in the destination cluster.
[Kubernetes version skew policy](https://kubernetes.io/releases/version-skew-policy/#kubectl)

```bash
python3 scripts/prepare_tekton_bridge_image.py \
  --base-image "$REVIEWED_PYTHON_IMAGE" \
  --kubectl "$REVIEWED_KUBECTL_BINARY" \
  --kubectl-sha256 "$REVIEWED_KUBECTL_SHA256" \
  --output private/tekton-bridge-image

docker build --platform=linux/amd64 --pull=false --network=none \
  --tag "$BRIDGE_UPLOAD_TAG" private/tekton-bridge-image
```

For an offline build, load the pinned base into the builder first and run
the build on an isolated host. The generated context contains the build
recipe, the verified binary, and their hashes. The build checks Python and
kubectl and sets the runtime user to UID and GID 1000. Publish the resulting
image to your registry and set `bridge_image` to its repository and manifest
digest. Keep the published digest with the context record.

### Job execution

The inline PipelineRun carries the reviewed task/step definitions rather than
fetching a mutable Pipeline or Task by name. Event input supplies only a
validated 40-character commit ID. The checkout step uses isolated Git settings,
an empty template, no credential helper, verified HTTPS, no redirects and an
askpass helper that reads the scoped token file. It fetches the exact commit,
checks it is a commit, checks out detached HEAD and verifies the resulting HEAD
through a verify-only Git ref transaction. An empty directory created by
Tekton's working-directory initializer is accepted; existing contents and
symlinks are refused.
The bounded shared EmptyDir mounts directly at `/workspace/repository` in
both steps so the Pod's filesystem group grants write access at that path.
The mount root can remain root-owned. Checkout trusts only this exact fresh
path through Git's command configuration; the build receives the same exact
`safe.directory` through Git configuration environment variables. No wildcard
trust, root initialization or additional build-image interpreter is required.
It does not enable Git submodules or Git LFS filters; applications requiring
those features need explicit additional implementation and acceptance.

Checkout credentials mount only in the checkout step. The build step shares the
worktree but has a separate temporary volume, no read-token mount and no
automatically mounted Kubernetes token. Both steps use non-root UID/GID 1000,
read-only roots, dropped capabilities, resource limits and runtime-default
seccomp. Job placement selects dedicated Linux workers and excludes
control-plane/master nodes. These controls do not prove tenant isolation,
network restrictions, provenance, artifact publication or adversarial safety.
Supply admission policy, quotas, network policy and workspace disposal separately.

## Private prerequisites and runtime packages

Provision [Tekton Pipelines](TEKTON.md), a separate job namespace enforced at
restricted Pod Security, a controller namespace and suitable worker capacity.
The generated RBAC grants the submitter only create/get for PipelineRuns and
receipt ConfigMaps in the job namespace, plus get for two namespace identities.
It grants the job service account no roles. PipelineRun creation still permits
job execution within that namespace: protect this identity and constrain it
through admission and quotas. Do not use an administrator kubeconfig.

Prepare the following private files under `/run/platform/bridge`, readable by
the service identity and inaccessible to other users:

- `webhook-secret`: at least 32 bytes, matching the repository webhook secret.
- `api-token`: scoped source repository-read token for identity/head lookup.
- `ca.crt`: CA bundle for verified source API HTTPS.
- `tls.crt` and `tls.key`: receiver certificate chain/key for the webhook host.
- `kubeconfig`: explicit job-cluster context, trusted API CA and the generated
  submitter identity. Provision credential lifetime/renewal separately; projected
  pod tokens are disabled and no credential is generated by this renderer. The
  receiver pins a private kubeconfig snapshot for its lifetime: target or token
  rotation requires a reviewed restart and a fresh cluster-identity check. A
  local `kubectl config view --flatten --raw` resolves CA/certificate file paths
  into this private snapshot. Use a scoped static token or client certificate;
  dynamic exec/auth-provider plugins and token-file references are refused.

The job namespace separately needs the `read_secret` Secret containing `token`
and `ca.crt` for Git HTTPS reads. Keep its token scoped to the selected repository.
The receiver has no need to read that job Secret. The bridge's private input
Secret is distinct and stays in the controller namespace.

```bash
python3 scripts/render_tekton_forge_bridge.py \
  --plan private/tekton-forge-bridge.json --runtime rke2 \
  --output-dir private/rendered-forge-bridge
```

The output must be a new directory under `private/`. Review `bridge-rbac.yaml`,
`run-template.json`, the program files and the runtime workload. Record
`contract_sha256` from `render-status.json` separately. The contract binds exact
program/template bytes; this is an integrity check, not source authentication.

- `rke2`, `k3s`, `k8s`: manifests include two receiver candidates, required host
  anti-affinity, a disruption budget, private-input Secret references and a
  ClusterIP TLS service. Namespaces, Secrets and external routing are prerequisites.
  Review and install the new RBAC/workload explicitly using the selected context;
  no deployment command runs during rendering. Provide a private redundant TLS
  entry route to port 8443; no unauthenticated public Ingress is generated.
  The HTTPS readiness endpoint reports whether the single request worker can
  answer; it does not check source or Kubernetes API availability. A long
  submission can temporarily make a receiver unready. Kubernetes HTTPS probes
  do not authenticate the receiver certificate, so they are not a TLS trust test.
  The local TLS fixture verifies that an idle receiver answers, a busy receiver
  times out, and the receiver answers again after submission completes. Run it
  on a disposable Linux host with:

  ```sh
  FORGE_TLS_FIXTURE=1 python3 -B scripts/test_tekton_forge_bridge.py BridgeTests.test_https_probe_and_busy_receiver
  ```

  A separate disposable Kubernetes API test admits the rendered receiver pod
  under restricted Pod Security and checks the HTTPS probe and disabled token
  automount. This validates admission, not an actual scheduled workload.
- `docker`: use `compose.yaml` on the selected host. Set the absolute reviewed
  bundle and private input directory through `PLATFORM_TEKTON_BRIDGE_BUNDLE`
  and `PLATFORM_TEKTON_BRIDGE_INPUTS`. Only the selected IPv4 address publishes
  port 8443. The service uses UID/GID 1000 and read-only mounts. Redundant hosts
  and an external entry route remain separate work.
- `raw`: install the reviewed program bundle read-only under
  `/etc/platform/tekton-bridge`, provision the non-root `platform-ci-bridge` user
  and private inputs, then review/install the systemd unit. It binds the chosen
  address on port 8443. Python 3, `kubectl`, CA trust and service management are
  host prerequisites. The renderer does not create users or enable services.

All runtime selections submit jobs to the explicitly bound Kubernetes target;
Docker and native hosting do not replace Tekton with local shell execution.
Service startup checks the kube-system namespace UID, verified API context and
restricted job namespace before accepting connections. The receiver serves TLS
1.2 or later, with bounded request bodies, socket timeouts and a finite queue.
Each process submits synchronously; assess throughput and forge delivery timeout
under load. A source timeout may need redelivery after the submission finishes.

## Receipts, retries and recovery

The run name derives from the reviewed contract and commit. Before creation,
the receiver reserves an immutable ConfigMap name. After observing the expected
run spec and UID, it records a separate immutable completion-of-submission
receipt. This receipt means **submitted**, not that the build passed.
The reviewed template explicitly includes the empty EmbeddedTask metadata and
null custom-task extension emitted by the supported controller, with canonical
pipeline-duration strings. It also predeclares the controller's inline-pipeline
label with the deterministic run name. Declared job fields remain bound to the
reviewed contract. This representation is shared with the Gerrit submission adapter; changing
existing job bytes requires a newly rendered, reviewed contract rather than
adopting a mutated run under the old receipt.

Retries across receiver processes reuse the deterministic run. A retained receipt
prevents recreation even after PipelineRun garbage collection. Do not prune or
delete event receipt ConfigMaps as part of ordinary job retention. Back them up
with the job cluster. Deleting receipts removes this replay protection; changing
the reviewed contract intentionally creates a different event identity.

An ambiguous reservation with no observable run stops automatic submission.
It never assumes a timeout means the server did not accept a job. Inspect the
source event, both receipt records, cluster audit history and job history before
any manual recovery. A replaced UID, foreign receipt or changed inline job fails
verification. There is no automatic deletion, credential reset or retry bypass.

An isolated Kubernetes API fixture also exercises actual scoped RBAC and the
reviewed Tekton v1.4.0 PipelineRun schema. It simulates a lost create response,
checks the original run is reused, and confirms a retained receipt prevents
recreation after the run is deleted. Source head verification is simulated;
there is no forge server, Tekton controller or worker in this fixture.

Run the copied receiver without `--serve` and with its reviewed contract hash to
check local bytes only. `--serve` explicitly starts the TLS listener and enables
authenticated job creation. Protect the program directory from service-user
writes. Updating code/plan requires a new bundle, contract review and deliberate
webhook routing change; this helper does not claim upgrade/rollback acceptance.

## Acceptance still required

Test real signed deliveries and negative signatures, current versus stale heads,
exact checkout and build results, receipt races across nodes, source/API outage,
receiver loss and receipt restoration. Verify credential denial from build
steps, worker/network isolation and quotas. Enable and verify the separate
result reporter, and implement artifacts/signing and Argo CD handoff, plus
the separate Gerrit event path.
Offline API simulations and a Compose configuration check cannot establish any
of those live full-stack outcomes.

On Linux with Git/OpenSSL available, the suite also creates a disposable local
HTTPS Git backend, requires Basic authentication through the real askpass helper,
checks the exact fetched HEAD and executes the fixture application's tests.
The fixture contacts only loopback and writes temporary files. It validates
checkout mechanics; it does not establish real forge delivery or Pod execution.
Where available, Compose and systemd tooling check their generated configurations.
