# Optional Gerrit source-bound static publication

This extends [static image publication through Harbor](HARBOR_STATIC_IMAGE.md) with a fixed, exact Gerrit patch-set checkout. It is an optional manually selected task. The existing Gerrit bridge and reporter contracts, default stack profiles and deployment promotion rules remain unchanged. Rendering this task does not submit it or authorize deployment.

## Inputs and task construction

Select the current patch set from the intended Gerrit project and branch. Keep company URLs, source credentials and registry plans in ignored private input files. The publication plan's `revision` must equal this source plan's `revision`:

```json
{
  "schema_version": 1,
  "clone_url": "https://review.example.invalid/a/team/application.git",
  "read_username": "ci-read",
  "revision": "<40 lowercase hexadecimal characters for the selected commit>",
  "patchset_ref": "refs/changes/23/123/2",
  "ca_sha256": "<SHA256 of the source CA bytes>",
  "checkout_program_sha256": "<SHA256 of the reviewed UTF-8/LF checkout helper>"
}
```

`source_bound_static_image.checkout_program_sha256()` returns the reviewed helper hash. The helper is normalized to UTF-8/LF as packaged into ConfigMap data. `render_gerrit_harbor_static_task.programs()` supplies the five public programs required by the task. Put them into an immutable program ConfigMap; do not execute source repository copies of those programs.

`render_gerrit_harbor_static_task.task_spec(...)` takes the source and publication plans, three digest-pinned Python images, explicit build arguments, and distinct ConfigMap/Secret names. The checkout image must include Git. Provide immutable source input data at `source.json`, a source Secret containing only `token` and `ca.crt`, and the original Harbor robot Secret described in the publisher guide. The source token needs only access to the selected source project. Build commands run with `/workspace/output` as their working directory; the exact checkout is available at `/workspace/source` read-only. A typical build command is `python3 -I -B /workspace/source/build.py`.

Configure the enclosing TaskRun with a dedicated ServiceAccount with no API roles and `automountServiceAccountToken: false`, restricted Pod security, runAsUser/runAsGroup/fsGroup 1000, explicit worker selectors, a bounded timeout and the reviewed namespace/target. Review the complete TaskRun before submitting it. This helper does not provide a new admission controller or automatic webhook integration.

## Isolation and provenance

The task has three ordered steps:

1. Trusted checkout: only this step mounts the source credential. The existing fixed checkout helper fetches the exact Gerrit change ref over verified HTTPS, verifies that its object ID equals the selected commit and checks out that commit with hooks and redirects disabled. After success, it writes a create-only checkout witness to a separate EmptyDir volume.
2. Build: reads the source mount without write access and writes only its output/scratch volumes. Source credentials, source plan, checkout witness and Harbor robot are absent from its mounts. Set compiler/cache output paths accordingly; the source tree is deliberately immutable to the build.
3. Trusted publish: reads the checkout witness and source plan, requires the publication revision to match, then snapshots and publishes the selected output through the original restricted Harbor robot. It cannot read the source credential. Its output mount and witness mount are read-only.

The witness binds the source URL hash, exact commit/ref, CA hash, checkout helper hash and source plan hash. A failed checkout produces no witness. Altered witnesses or conflicting revisions fail before publication. The task result adds the witness and source-plan hashes to the normal image receipt.

A matching witness file is not a signed attestation and does not independently prove that a remote Git checkout happened. Trust it only after verifying the actual TaskRun/Pod identity, ordered successful steps, immutable trusted program/input maps and the mount isolation above. A caller that can fabricate all those inputs is outside this file-witness trust boundary. Image signing, reviewed CI receipts, merge approval and the existing deployment promotion gates still apply. This task does not cast a review vote, merge a change or update GitOps desired state.

## Acceptance scope

Portable tests cover invalid URLs/refs, changed source/helper/CA inputs, witness substitution, checkout failure before witness creation and three-way mount isolation. The explicit `test_gerrit_harbor_worker_fixture.py` runtime case selects a real Gerrit patch set, executes its build script on a real Tekton/KVM worker, publishes through Harbor, pulls by digest with a distinct read-only identity and verifies serving on two workers. Its negative case requests a conflicting commit for the same change ref and checks that build/publication were skipped and artifact count did not increase.

The integration is local component evidence, using the Harbor pool-health candidate and temporary local storage. Automatic bridge submission, review voting, Argo CD/Flux digest promotion, signed-image admission, Ceph failure domains and physical HA require their own acceptance. Do not treat this task or its receipt as qualification of the full stack/runtime matrix.

The runtime fixture saves failed-step logs only beside its ignored private validation report and prints sanitized code locations. Treat those private logs as potentially containing source-project output; do not add them to a public commit.

For the opt-in automated bridge/reporter pipeline and artifact-aware votes, see [artifact-aware Gerrit CI version 2](GERRIT_ARTIFACT_CI.md). The manual task remains available.
