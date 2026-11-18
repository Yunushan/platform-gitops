# Gerrit container preparation workloads

This adapter packages the [guarded initializer](GERRIT_INITIALIZATION.md) for
RKE2, K3s, existing Kubernetes and Docker. Disposable RKE2, K3s and Docker labs
exercised preparation with Gerrit 3.13.10. See the
[full stack expansion](MULTI_RUNTIME_STACKS.md) for deployment and HA verification.

**Rendering does nothing to a cluster. Applying or running these workloads
initializes new sites.** Review targets, artifact provenance, private inputs and
external services before that separate operator action. Never use these
workloads as an upgrade or retry tool for existing Gerrit data.

## Explicit toolchain and storage layout

Copy the [site plan example](../examples/gerrit-sites.json) into ignored private
storage and edit the private copy. Container preparation requires:

```json
{
  "shared_host_path": "/shared",
  "sites": [
    {
      "name": "gerrit-a",
      "host_path": "/volumes/gerrit-a/site",
      "claim_subpath": "site"
    },
    {
      "name": "gerrit-b",
      "host_path": "/volumes/gerrit-b/site",
      "claim_subpath": "site"
    }
  ]
}
```

This is a layout excerpt, **not** a complete plan. Retain all required artifact
locks, configuration hashes, claim names, namespace, release and image fields
from the site plan. Configurations must use `/shared/git` as the Git base path
and unique instance IDs matching the two site names. Ceph-scoped plans also
require `main.sharedDirectory = /shared` in each HA configuration; neither this
path nor the Git target may fall back to local state. Review their canonical URL,
TLS, SSO and peer settings independently. No mount paths or hashes are silently
rewritten by the renderer.

The daemon packages mount the `site` subdirectory at `/var/gerrit` when
`claim_subpath: site` is selected. Plans without that optional field retain the
previous PVC-root layout; the preparation renderer refuses them rather than
overwriting that root. See Kubernetes' [subPath volume model](https://kubernetes.io/docs/concepts/storage/volumes/#using-subpath).
**Do not create the daemon Deployments before preparing
and reviewing the sites.** A kubelet can create a missing subPath directory;
the initializer then deliberately refuses it as an existing target.

Provide a reviewed digest-pinned preparation image with compatible Java,
Python 3.12 or newer, PyYAML, Git CLI, `ssh-keygen`, POSIX shell and `cp`. The
renderer embeds the current first-party helper modules in its private output.
Provision registry pull authorization outside these templates if the image is
private.

### Build the Linux amd64 toolchain for Gerrit 3.13.10

The [container recipe](../scripts/gerrit-preparation.Containerfile) uses a
digest-pinned Python 3.13 Bookworm base with Git and OpenSSH tools. Put these
two public release files in a dedicated, otherwise empty build context:

- `OpenJDK21U-jre_x64_linux_hotspot_21.0.13_9.tar.gz`.
  [Eclipse Temurin release](https://github.com/adoptium/temurin21-binaries/releases/tag/jdk-21.0.13%2B9)
- `pyyaml-6.0.3-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl`.
  [PyYAML release files](https://pypi.org/project/PyYAML/6.0.3/#files)

The recipe checks both SHA-256 values before extraction or installation. It
runs as UID/GID 1000, uses `/tmp` as the writable Java home, and expects no
private bootstrap inputs in the build context. Pull or preload the pinned
base before building. For example, from the repository root:

```bash
docker build --platform linux/amd64 --network none \
  --file scripts/gerrit-preparation.Containerfile \
  --tag "$PREPARATION_IMAGE_TAG" private/gerrit-toolchain-build
```

Publish the reviewed image to the selected registry and pass its manifest
digest in `--preparation-image`. The WAR and plugins come from separate
release-artifact mounts and are checked against the site plan at preparation.
Check the Gerrit release's Java requirement before using another version.

## Kubernetes preparation

Provision, without starting Gerrit:

- An existing namespace.
- Two distinct writable local-site PVCs, each with no `site` subdirectory yet.
- A separate empty shared Git PVC with appropriate RWX/locking semantics.
- A separate read-only release-artifact PVC with the four approved files under
  `bin/`, `plugins/` and `lib/` as specified in the plan.
- A separate writable report PVC. Its `initialization-draft` directory must not
  exist. This retains the resulting draft independently of the Job Pod.
- Two private bootstrap Secrets named `<site-name>-bootstrap`, each containing
  `gerrit.config`, `high-availability.config`, `secure.config`, `keystore`,
  `ssh_host_ed25519_key` and `ssh_host_ed25519_key.pub`.
  Schema version 2 additionally requires private `healthcheck.config` on each
  primary, including its approved authentication-check inputs. Its separately
  pinned hash is part of the site plan; file contents are not put in ConfigMaps.

No Secret data or PVC is created by the renderer. Secret volumes expose only
those six keys for version 1, or seven keys for version 2. The artifact, report,
shared and site PVCs must all be distinct.
Supply the same nonempty Gerrit email signing key in the private `secure.config`
for both new sites. The native helper checks it during preflight.
[Private bootstrap requirements](GERRIT_INITIALIZATION.md#private-input-layout)
They must also be attachable to the same preparation Pod; incompatible zone or
local-disk constraints can leave it Pending. The selected node needs 1 CPU and
2 GiB available for the Job's requests after existing Pods' requests. Plan
this staging step before spreading the daemon primaries across serving nodes.

Render a private bundle, replacing the toolchain placeholder with an approved
digest-pinned image reference:

```bash
python3 scripts/render_gerrit_preparation.py \
  --plan private/gerrit-sites-container.json --runtime rke2 \
  --preparation-image '<REVIEWED_PREPARATION_IMAGE_WITH_SHA256_DIGEST>' \
  --artifact-claim gerrit-release-artifacts \
  --report-claim gerrit-preparation-reports \
  --output-dir private/generated/gerrit-preparation-rke2
```

Use `k3s` or `k8s` for those deployment targets. The bundle contains an immutable,
content-addressed ConfigMap and a Job with `backoffLimit: 0`, `restartPolicy:
Never`, an execution deadline and no automatic TTL cleanup. Changes to embedded
code, the plan, image or claims change the workload name. A new name does **not**
authorize a retry: the initializer still rejects nonempty storage or existing
site targets. Read and retain failures; never delete storage or initialization
markers to force the Job to pass.

The Job runs as UID/GID 1000 with no API token, no elevated capabilities and a
read-only root filesystem. It stages inputs privately, checks their locks, then
uses the same explicit initialization helper as native preparation. Prepare
filesystem ownership, capacity and storage access for that UID/GID. The report
PVC is mounted at `/opt/platform/private`; the draft will be under
`initialization-draft/gerrit-sites-draft.json` after successful execution.
Keep the projected code at `/opt/platform/scripts`. The initializer locates
the report mount next to `scripts` even when Kubernetes projects the script
through a per-revision symlink.
As noted in the [Kubernetes Job documentation](https://kubernetes.io/docs/concepts/workloads/controllers/job/),
controller behavior can cause duplicate execution even for a single-completion
Job. The helper's exclusive new-site reservations and persistent marker remain
necessary; a Job retry setting is not an exactly-once guarantee.
No Service, Gerrit daemon, identity provider or GitOps application is started.

Apply only after review, with an explicitly selected context/kubeconfig and
server-side apply so embedded code does not become a large last-applied
annotation. Applying the Job is a write action and must be an intentional
operator decision; this runbook and renderer do not perform it automatically.

A disposable K3s v1.35.0 lab preloaded the verified toolchain image on the
storage worker and ran the rendered Job under restricted Pod Security. The
Job staged both bootstrap Secrets and the pinned release files, initialized
the shared repositories, and wrote the draft to the report claim. A read-only
Pod verified both initial Git configuration commits. The local site files
matched the draft and private input checksums. A second Job refused the
existing sites and preserved the original draft. The lab used five separate
local PVs on one worker.

An offline RKE2 v1.36.4+rke2r1 lab repeated the drill with one server and one
dedicated worker. Bundled Cilium was Ready on both nodes. The rendered Job
used its configured CPU and memory requests and ran under restricted Pod
Security with the toolchain preloaded on the worker. It produced both sites
and the persistent draft, and the read-only Pod verified the initial Git
commits. A second Job refused the existing sites and preserved the draft.
The claims used local PVs. Independent existing-Kubernetes and CephFS
preparation remain unverified. [RKE2 offline installation](https://docs.rke2.io/install/airgap)

## Docker preparation

On a Linux preparation host, provision:

- The complete two-site private input trees from the native initializer guide,
  including reviewed artifacts, configs, keystores and SSH keys.
- Empty mounted shared storage at `/shared`.
- Existing writable site mount parents `/volumes/<site-name>` with no `site`
  child directory.
- A separate private report directory with no `initialization-draft` child.

All bind sources must already exist; automatic host-path creation is disabled.
Bootstrap inputs and embedded code are mounted read-only. Mount sources must not
overlap. The rendered bundle and bootstrap inputs must be readable by UID/GID
1000, and the output and storage paths must be writable by that identity. Render:

```bash
python3 scripts/render_gerrit_preparation.py \
  --plan private/gerrit-sites-container.json --runtime docker \
  --preparation-image '<REVIEWED_PREPARATION_IMAGE_WITH_SHA256_DIGEST>' \
  --bootstrap-host-path /srv/gerrit-bootstrap \
  --report-host-path /srv/gerrit-preparation-reports \
  --output-dir private/generated/gerrit-preparation-docker
```

The Docker lab built the recipe offline and ran the rendered Compose service
with a verified locally loaded image and disposable bind sources. The service
ran as UID/GID 1000 with a read-only root filesystem and no Linux capabilities.
It created the initial shared repository refs and both local site directories,
preserved the private bootstrap files and wrote the draft to its report mount.
A second execution refused the prepared sites and left the draft intact. This
run used local directories on one host.

The Compose service has no public ports, no restart policy and no inherited
image CMD, following Docker's [Compose command override semantics](https://docs.docker.com/reference/compose-file/services/#command).
Running it is an explicit initialization action, not a preview. The
renderer does not invoke Docker. Later, place the prepared daemon sites on their
separate serving hosts through an approved private transfer/storage process;
this one-shot staging service does not provide cross-host replication or secret
distribution.

## Handoff and remaining acceptance

Review the persisted draft, resulting site configuration, initialization logs,
private bootstrap state and file ownership. Use the reviewed draft to render
daemon workloads with their resulting configuration hashes.

If the preparation Job used two local PVs on one worker, wait for the Job to
finish and confirm its Pod has terminated. Stage the second site's `site`
directory on a fresh claim on its serving worker using an authenticated private
transfer. Preserve file ownership and modes, and verify the resulting site
against the draft on the destination worker. Update the second site's `claim`
in your reviewed daemon plan and retain `claim_subpath: site`. Both daemons
need access to the shared Git claim. Their pod anti-affinity requires distinct
serving nodes. [Site verification and daemon rendering](GERRIT_SITES.md#render-without-applying)

Use the selected storage, release and plugin artifacts for the deployment
checks. Those checks cover shared writes, SSH/TLS, SSO, review events and
coordinated failover.

The K3s lab also exercised this handoff with Gerrit 3.13.10. It staged the
stopped second site on a fresh local claim and rendered both Deployments from
the preparation draft with the updated claim reference. Both Pods started
on separate workers against the same NFSv4 Git claim. A fixture account
enrolled its SSH key over certificate-verified HTTPS. The lab used Gerrit's
`commit-msg` hook and pinned SSH host keys to upload a review to the first
site, read its exact commit from the second, stop the first Pod, and upload
another review to the second. Both commits remained readable there. The test
used direct port-forwards and a single NFS server. CephFS, peer index
synchronization, ingress failover and physical-node HA remain outside this
test. [Gerrit commit-msg hook](https://gerrit-review.googlesource.com/Documentation/cmd-hook-commit-msg.html)
The [K3s router drill](GERRIT_ROUTING.md#verify-route-selection-with-the-prepared-k3s-pair)
exercises the same preparation workflow through two HAProxy replicas.
