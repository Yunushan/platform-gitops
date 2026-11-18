# Optional static image publication through Harbor

`publish_harbor_static_image.py` is a deliberately narrow Linux publisher for static web build output. It creates an OCI image from an explicitly pinned Linux/amd64 base containing `/bin/httpd`, adds the selected public files under `/www`, sets UID/GID 1000 and port 8080, and publishes an immutable manifest digest. It requires no Docker socket, privileged builder or Dockerfile interpreter. Existing stack profiles do not enable it automatically.

The publisher uses only the private handoff produced by [Harbor CI robot provisioning](HARBOR_CI_ROBOT.md). Operator credentials belong in the provisioning workflow and must never be mounted into a build or publish pod. The robot can pull/push repositories throughout its selected Harbor project; it is not a per-repository identity. Use a dedicated private project where that scope is required.

## Reviewed inputs

Create an ignored private JSON plan with exactly these fields:

```json
{
  "schema_version": 1,
  "origin": "https://registry.example.invalid",
  "ca_file": "/publisher-input/ca.crt",
  "ca_sha256": "<64 lowercase hex characters>",
  "leaf_sha256": "<64 lowercase hex characters>",
  "credential_file": "/run/robot/credential.json",
  "credential_sha256": "<SHA256 of the original private credential file bytes>",
  "project_id": 2,
  "robot_id": 3,
  "repository": "fixture/ha",
  "base_digest": "sha256:<64 lowercase hex characters>",
  "revision": "<40 lowercase hex characters for the selected Git commit>",
  "files": ["index.html"]
}
```

Place the base manifest and all its blobs in that same repository before publishing. Mutable tags, foreign-layer download URLs, other origins, redirected uploads, expired or substituted robot handoffs and changed CA/leaf identities are refused. The base must have a matching config, platform, layer digests and rootfs diff IDs. Limits are eight base layers, 16 MiB per fetched blob, 64 MiB compressed base content, 64 MiB expansion per compressed layer, 128 selected output files, 4 MiB per file and 8 MiB total output. Only selected regular, singly linked files are read; symlink traversal and dot-prefixed path components are refused.

Run as a trusted publishing process after the build has finished:

```sh
python3 -B scripts/publish_harbor_static_image.py   --plan private/publication-plan.json   --output /absolute/path/to/static-output
```

The example mount paths describe the CI task. A direct CLI invocation must use the corresponding absolute local paths in its private plan. Keep trust and credentials outside the output directory. Hash-pinned trust/credential inputs support Kubernetes AtomicWriter links; the strict no-follow policy applies to build output. The publisher snapshots output before its first network operation and refuses the robot secret if found in those files. That check is not a general secret scanner; the caller must review which files are public.

Successful stdout is a receipt containing the image URI by digest, base/config/output-layer digests, output tree hash and supplied source revision. Errors provide only a bounded exception category and public code locations; private API details are suppressed. Only disposable read-only service readiness checks use bounded polling. There are no automatic mutation retries: an uncertain upload or manifest write fails. Content-addressed blobs can be reused only after verified readback. A later invocation may complete an interrupted publication, but callers must preserve and review its original inputs.

## Separate Tekton steps

`render_harbor_static_task.task_spec(...)` returns an optional inline Tekton task with explicit digest-pinned build/publisher images and literal build arguments. The caller provides immutable ConfigMaps for the trusted publisher program and plan/CA, plus a Secret containing only `credential.json` from the original handoff. Include `publish_harbor_static_image.py`, `bounded_file.py` and `strict_json.py` in the program map. The publisher image must contain Python 3 and its TLS trust support.

Only the publishing step mounts the robot and publisher input/program maps. The build step mounts its output and scratch directory; the publisher receives output read-only. Both steps run without privilege escalation or capabilities, with a read-only root filesystem and bounded compute resources. Configure a dedicated ServiceAccount with `automountServiceAccountToken: false`, no API roles and restricted Pod security; set runAsUser/runAsGroup/fsGroup 1000 in the TaskRun pod template. The task returns `image-receipt` only after digest readback succeeds. It does not provide a forge checkout, webhook handling, run admission, network policies or application deployment by itself.

Bind the supplied revision and output to an actual reviewed checkout and successful build before accepting a receipt for deployment. Supplying a revision label alone does not prove that source was built. A separate application pull-only identity should authenticate kubelet pulls; do not distribute the publishing robot to deployed application containers. Install the registry CA into the selected runtime's trust configuration; do not use plaintext, skip verification, or preload the new image as a substitute for a private registry pull.

## Acceptance boundary

Portable tests cover malformed plans, robot substitution, unsafe file inputs, base corruption, upload URL confinement and task mount isolation. `test_harbor_worker_fixture.py` is an explicit disposable Linux/KVM integration fixture combining real Harbor services, Tekton controllers, worker TaskRun execution, private image pull and HTTP serving. Detailed worker UIDs, image digests and resource samples stay in a private receipt; the outer native fixture returns booleans and that receipt's SHA256. Its results remain component evidence: it uses synthetic build source and local registry storage, and the Harbor database pool-health build is a local candidate rather than an unchanged upstream binary.

Image signing/admission, real SCM-to-build provenance, Argo CD/Flux digest handoff, independent storage failure domains, physical host outages and the full stack/runtime matrix require additional acceptance. Receipts retain false flags for untested signature, deployment and full-stack properties. Do not bypass a production signed-image admission policy to run this component test.

For an optional exact Gerrit checkout with a separate provenance witness and read-only build source, see [Gerrit source-bound static publication](GERRIT_HARBOR_STATIC_IMAGE.md).
