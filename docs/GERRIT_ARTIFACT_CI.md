# Optional artifact-aware Gerrit CI (plan version 2)

Version 1 continues to submit its fixed checkout/build pipeline. Version 2 adds static image publication and requires the artifact receipt before a successful Gerrit Verified vote. The control helpers can be packaged for `rke2`, `k3s`, `k8s`, `docker` and `raw`; Tekton worker execution always requires the explicitly selected Kubernetes target. Rendering a host package does not establish runtime or HA acceptance for that host option.

## Explicit configuration

Start with a reviewed Gerrit bridge plan. Set `schema_version` to `2` and add exactly one `publication` object:

```json
{
  "publisher_image": "registry.example.invalid/python@sha256:<64 lowercase hex characters>",
  "robot_secret": "harbor-ci-publisher",
  "trust_secret": "harbor-ci-trust",
  "registry": "<the static publisher plan object, with revision removed>"
}
```

The `registry` placeholder above must be an object from the [static publisher plan](HARBOR_STATIC_IMAGE.md), not a string. Keep its CA and credential paths `/publisher-input/ca.crt` and `/run/robot/credential.json`. Its base digest, repository, CA/leaf hashes, original robot credential-file hash, project/robot IDs and selected files remain pinned. The bridge supplies the discovered Gerrit revision; a static registry `revision` override is refused. Use a canonical lowercase `https://` registry origin.

The dedicated immutable robot Secret contains only the original provisioning handoff at `credential.json`. A distinct immutable trust Secret supplies `ca.crt`. Neither name may equal the source-reader or bridge-control Secret. The reporter uses a separate account and Secret and cannot reuse publication identities. Provision these inputs through the private operator workflow; the scoped bridge/reporter roles still have no Secret-read permissions.

Set the build command explicitly for this option, for example `python3 -I -B /workspace/source/build.py`. Source is mounted read-only at `/workspace/source`; build output goes to `/workspace/output`. The build must run the project's checks and produce the selected public files, including `index.html`. Existing in-place source build commands may require output/cache path changes.

Render with the existing `render_tekton_gerrit_bridge.py` and `render_tekton_gerrit_reporter.py` commands using ignored private plans/output directories. All helper files, embedded task code and the run template are bound into version-specific package inventories. Preserve the reviewed package digests. Do not commit company URLs, credentials or generated private packages to a public repository.

## Evidence and isolation

The bridge continues to verify the current Gerrit ref, reserve each run once, compare server admission against its exact template and retain the original PipelineRun UID. A replay reuses that reservation; uncertain creates are read back rather than repeated.

Version 2 runs three ordered steps: trusted checkout/witness creation, build and trusted publication. Trusted program code is embedded in the reviewed template and unpacked only into per-step scratch space. Dynamic source/publication plans contain the selected revision/ref and reviewed references/hashes, with no token values. The build cannot mount the source credential, registry robot, checkout witness or other steps' scratch volumes. The reporter checks actual Pod/TaskRun ownership, worker placement, image identities, execution order and the reviewed per-step mount boundaries. Initializers must not mount source/publication Secrets.

A positive vote also requires one `image-receipt` from both the original TaskRun and PipelineRun, with matching bytes. The decoded receipt must match the exact source revision/ref witness, trusted checkout helper, selected repository/base image and digest-addressed image. Unsupported signing/deployment/HA claims are refused. The immutable review receipt's evidence hash includes the artifact receipt. A matching file witness is not a cryptographic attestation; trust still depends on the verified task, trusted operator/controller and credential boundaries.

A failing build cannot satisfy successful-worker evidence. Its publication step must be skipped; the explicit runtime fixture also checks that artifact count does not increase. The failed CI result can receive the existing negative Verified vote. Existing stale-patchset, reservation and report replay rules continue to apply.

## Deployment boundary

The legacy source-branch GitOps handoff explicitly refuses version 2. It cannot establish that a deployment manifest selects this worker-produced digest. The separate [artifact promotion option](GERRIT_ARTIFACT_PROMOTION.md) adds an explicit merged-source, image-selection and Deployment rollout policy. Its separate combined runtime case verifies actual artifact-CI-to-Argo promotion and rollout with scoped clients; the additional native variant verifies its packaged non-root CLI in restricted transient services. A separately gated polling variant also checks automatic native bridge/reporter submission, positive reporting, process restart and replay before explicit promotion. Signed-image admission, persistent installation/recovery and full stack qualification remain separate work. Do not bypass that refusal by treating a Verified vote as deployment authorization.

Portable contracts cover v1 compatibility, all host-package renderings, bootstrap imports, helper inventories, exact receipt matching, cross-step mount refusal and replay. The opt-in `test_gerrit_artifact_worker_fixture.py` case exercises real Gerrit, scoped API identities, Tekton workers, successful and failing tests, artifact-aware votes and private digest pulls/HTTP serving on two workers. It retains successful API/evidence files inside the ignored validation report directory before teardown. This is local component evidence using the Harbor pool-health candidate and temporary local storage, not full-stack or physical HA qualification.
