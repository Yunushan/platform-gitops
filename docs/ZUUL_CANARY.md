# Gerrit and Zuul worker canary

Status: opt-in job configuration and playbook, not a completed live CI test.
The generator renders a candidate for the RKE2, K3s, existing Kubernetes,
Docker and native Linux packages. It never contacts Gerrit, publishes Git
configuration, triggers an event or starts a service. Hostnames, selections and
rendered files belong in ignored `private/` paths.

## What the job exercises

`platform-canary-check` is a separate independent pipeline. On a new patchset,
a restored open change, or an explicit `platform-canary recheck` comment, it
requests one approved worker for the selected project's selected branch. A
tenant semaphore limits this canary to one running job. Open/current-patchset
requirements reject closed or superseded changes. Branch filters are escaped
and anchored; ordinary check/gate pipelines are not redefined.

The final, protected base job lives in a trusted configuration repository and
allows only the selected canonical Gerrit project. Its playbook waits for SSH,
runs a fixed Python probe as the worker's unprivileged account, and checks a JSON
marker against the current build UUID. It gathers no host facts, uses no sudo,
runs no application code and makes no deployment. Ansible may create its normal
temporary module files on the worker. A root worker or malformed build UUID
fails the probe. The job timeout is 180 seconds; connection wait is 60 seconds.

Start/success/failure reporters add **comments only**, with Gerrit mail
notifications set to `NONE`. There is no Verified/Code-Review label, submit
action or deployment credential. Success messages explicitly say application
code was not tested. The job participates in this pipeline's result by default,
but project configuration can affect aggregate results. Inspect the actual job
result; neither a green pipeline nor its comment is an application review vote
or a submit gate.
These choices use Zuul's documented
[independent pipeline](https://zuul-ci.org/docs/zuul/latest/config/pipeline.html),
[trusted base-job controls](https://zuul-ci.org/docs/zuul/latest/config/job.html)
and [Gerrit trigger/report settings](https://zuul-ci.org/docs/zuul/latest/drivers/gerrit.html).

## Prepare private selections

First prepare the reviewed [Zuul service/executor](ZUUL_SERVICES.md) and
[Nodepool launcher](NODEPOOL_SERVICES.md) plans. Both must describe the same
tenant/worker restrictions. The canary generator requires:

- An explicitly enabled executor package in the Zuul plan.
- A trusted configuration project already declared in that tenant, not loaded
  by any other tenant in either trust category.
- An untrusted, disposable canary project already declared in the same tenant.
- A worker label supplied by that tenant's static Nodepool pool. Every tenant
  must have explicit label restrictions, as required by the static adapter.
- An existing simple canary branch, chosen independently of the trusted config
  branch. The renderer checks its shape, not its remote existence.

Do not select a production application repository as the infrastructure canary.
The example introduces `platform/ci-canary`; add that private selection to the
tenant's `untrusted_projects`. The stock Zuul example deliberately does not
enable executors or supply private worker capacity. Follow the
[executor contract](ZUUL_EXECUTORS.md) and the
[static worker contract](NODEPOOL_STATIC.md) before rendering this job.

Copy [the public canary example](../examples/zuul-canary.json) into the ignored
private tree and edit only the non-secret selections. No API token, password,
SSH key or custom command is accepted in this selection file.

```bash
python3 scripts/render_zuul_canary.py \
  --plan private/zuul-canary.json \
  --zuul-plan private/zuul-config.json \
  --nodepool-plan private/nodepool-static.json \
  --runtime rke2 \
  --output-dir private/rendered-zuul-canary
```

Use `docker`, `raw`, `k3s` or `k8s` for the other runtime packages. Job semantics
are the same; these values do not install a hidden Kubernetes cluster or supply
missing services. Output must be a **new directory** under `private/`. Existing
output, public locations and resolved symlink escapes are refused.

The bundle contains `zuul.yaml`, `platform-canary.yaml`, and a render status.
The status records input/content hashes and publication coordinates for review.
Hashes identify the candidate; they are not signatures or evidence of a run.
No automatic publication or successful CI/HA status is recorded.

## Review, publish and exercise

Review this candidate with the selected Zuul/Ansible releases and the tenant's
existing configuration. The generated `zuul.yaml` contains four definitions:
pipeline, semaphore, job and project. **Merge these definitions into the existing
trusted configuration layout; do not overwrite a configuration repository's
existing `zuul.yaml`.** With a `zuul.d/` layout, add a distinct configuration
fragment instead. Put the generated `platform-canary.yaml` playbook at the
configuration repository root, matching the job's `run` path. Retain unrelated
pipelines and verify that the reserved `platform-canary-*` names are not already
used. Do not publish this candidate into an untrusted project: pipeline and
base-job definitions require a trusted configuration project.

Publish through the normal review process to the `config_project` and
`config_branch` recorded in the private render status. Review the active
tenant's configuration errors before uploading a disposable change to the
selected canary branch. The comment recheck trigger accepts the command as its
own line, including Gerrit's usual patchset-message prefix. A recheck can post
another comment, so avoid using it as a loop or attaching it to a mail test.

The service identity needs event access, project read/fetch and review-comment
permissions on this canary project. This canary does not require Verified or
Submit grants. Restrict its access according to the broader integration design;
the generator does not edit Gerrit ACLs or weaken existing submit requirements.

For actual acceptance, retain a private record of the configuration commit,
Gerrit change/patchset, event time, Zuul build UUID/result and selected worker.
Inspect the probe's JSON marker in the build log and confirm it contains that
same UUID. Confirm a comment from the intended service account on that exact
patchset, plus no label/submit change. A successful render, a scheduler process,
an unrelated successful build or a comment by itself does not prove this path.
This renderer does not collect or attest that live evidence.

The base job supplies no log upload destination. Streaming logs are only
available while the executor retains them; integrate reviewed private log
retention before relying on it for operational evidence. Keep build logs and
server details out of public Git and public test output.

## Limits and next acceptance

No live Gerrit event, SSH allocation or Zuul job has been executed by the unit
tests. A remote Python probe is not a malicious-job isolation test, a substitute
for worker reset/reimage, an application test suite, a gate/shared-queue design,
or a node-loss/restore drill. Static worker reuse and cross-job secret isolation
remain unresolved for mutually untrusted workloads. Trusted administrators can
still change configuration; final/allowed-project controls do not constrain
administrators of the configuration repository.

Test event interruptions/replay, launcher/executor/worker failure, release
compatibility, retained logs, production CI jobs and explicit review/submit
policy before accepting either requested Gerrit/Zuul/CD stack. All those gates
remain open in the [full stack plan](MULTI_RUNTIME_STACKS.md).
