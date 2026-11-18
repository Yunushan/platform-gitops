# Verified Gerrit review results from Tekton

The optional result reporter consumes the exact original
[Gerrit submission package](TEKTON_GERRIT_BRIDGE.md). It adds a separate private
reporting plan, credential and Kubernetes service account. Preparing/enabling it
does not modify the original submission contract or resubmit existing jobs.
All five submitter hosting formats have matching reporter artifacts.

The reporter writes only `Verified +1` or `Verified -1` to the exact reviewed
change/commit. It never votes Code-Review, submits/merges a change, changes
reviewers or deploys an application. The generated review request keeps drafts,
disables notification mail and avoids automatic attention-set updates. Actual
publication happens only when the separately prepared reporter is explicitly run.

## Evidence before a vote

The original restricted job namespace/cluster, deterministic run name, original
run UID, fixed inline task spec, revision/ref parameters and submission
reservation/receipt must all agree. A successful PipelineRun condition alone
cannot establish a positive vote. Before `+1`, the reporter checks the sole build
TaskRun and its owner UID, completed Pod and its owner UID, checkout/build step
commands and ordering, zero exit codes, pinned images/helper inventory, credential
mounts/security context and explicit non-control-plane worker placement.
The shared evidence inspector supports the Gerrit revision/ref pair while
preserving the Forgejo/Gitea single-revision contract. Modified ref parameters,
missing children, unreviewed helpers, secret exposure to the build step and
nonzero exits cannot produce a positive vote.

A controller-recorded terminal failure/cancellation produces `-1`, including
failure before worker startup. That vote means the CI run did not pass; it does
not claim that a successful build occurred. Pending conditions produce no vote.

The source checks bind the bot's numeric account ID and username and require an
active account. Gerrit's [review endpoint](https://gerrit-review.googlesource.com/Documentation/rest-api-changes.html#get-review)
returns labels for the current patch set, even when accessed via an older
revision. The reporter therefore re-reads the current change, number, patch set,
ref, commit, project, branch and open/non-WIP state. It repeats evidence/source
checks immediately before publication and reads the result back afterward.

## Label policy and private setup

Copy [the reporter example](../examples/tekton-gerrit-reporter.json) into ignored
`private/` storage. Set `source_contract_sha256` to the original submission
contract byte hash. Replace all example identities, account IDs and digests;
they identify no deployed credential, label or helper image.

Obtain the original effective `Verified` definition privately through
`GET /a/projects/<URL-encoded-project>/labels/?inherited`. Select exactly one
unambiguous definition with that name. `label_definition_sha256` is SHA-256 of
that whole JSON object encoded by `tekton_gerrit_reporter.fingerprint` (sorted
keys, compact separators, ASCII JSON). The bot needs read access to the relevant
`refs/meta/config` on the project and each inheritance ancestor, including
`All-Projects`, in addition to project/change visibility and the intended
Verified voting range. The reporter grants no Gerrit permission itself.

The original effective definition must support -1/0/+1, use neutral default zero,
have no approval-copy condition/legacy copy flags and disallow post-submit votes.
Its exact digest is checked before writing. Ambiguous inheritance, label drift
or project copying rules stop reporting. Site-level
`label.<name>.labelCopyEnforcement` can independently force copying and is not
exposed by the inspected label/server-info APIs. Deployment must separately
verify that no such enforcement applies to Verified on every serving primary.
The remote reporter cannot certify that site policy from project definitions.
The real fixture checks its local fresh configuration and actual no-copy
behavior; that evidence does not certify an external server's configuration.
See Gerrit's [label definition API](https://gerrit-review.googlesource.com/Documentation/rest-api-projects.html#label-definition-info).
Project submit requirements must still be reviewed separately to make CI voting
enforce the intended merge policy. The reporter does not rewrite that policy.

Prepare the following files at `/run/platform/gerrit-tekton-reporter`, readable
only by the separate non-root reporter identity/group:

- `api-token`: the dedicated bot's HTTP authentication token, never the source
  reader's or checkout step's credential.
- `ca.crt`: the exact trust bytes pinned by the original submission source.
- `kubeconfig`: a self-contained verified-TLS configuration for the original
  explicit management target and the separate reporter identity.

Reporter RBAC permits listing/reading PipelineRuns, reading TaskRuns/Pods/worker
Nodes and creating/reading immutable publication ConfigMaps. It permits neither
run creation/deletion nor Secret reads. The reporter's token never enters a
worker Pod. Provision/review its RBAC, network isolation and workload through the
approved deployment path; this package renderer does not apply resources.

## Prepare and operate

```bash
python3 scripts/render_tekton_gerrit_reporter.py \
  --bundle-dir private/generated/gerrit-tekton \
  --plan private/tekton-gerrit-reporter.json \
  --output-dir private/generated/gerrit-tekton-reporter
```

The original package determines the runtime. Output is create-only/private and
contains both original verified submission files and a separate
`gerrit-report-contract.json`. Stage the exact reporter package, its inputs and
the kubeconfig environment reference named by the original plan. Then a scoped
non-root Linux identity may run:

```bash
python3 /opt/platform/gerrit-tekton-reporter/tekton_gerrit_reporter.py \
  --bundle-dir /opt/platform/gerrit-tekton-reporter \
  --contract-sha256 "$REVIEWED_REPORTER_CONTRACT_SHA256" --once
```

Use `--watch` for bounded, paginated polling. Native output uses a separate
`platform-ci-reporter` service user. Docker has no published listener;
Kubernetes has separate anti-affine reporter instances, account and input
Secret. Static kubeconfig credentials are frozen for each process. Rotation or
target changes require an explicit restart. Public output contains only counts
and conservative verification flags, without source names, revisions, tokens,
review bodies or raw API diagnostics.

## Publication custody and uncertainty

Before posting, an immutable publication reservation commits the run UID,
evidence digest, exact body, bot and reporter contract. Only its acknowledged
creator may send the POST. An uncertain POST is observed via the exact tagged
message and detailed bot vote; it is never automatically repeated. The message
must belong to the original bot, patch-set number and exact result body; the
current bot vote must have the expected value and must not be copied. A final
receipt pins the reservation UID and evidence. Duplicate polling can verify
that receipt without requiring retained worker Pods.

Unrelated existing bot votes are not overwritten. Counterfeit matching messages
without an original reservation, changed votes/messages, replaced run/reservation
UIDs and changed policy are refused. Keep the publication reservations/receipts;
deleting them is not a retry mechanism. A missing result after an uncertain write
requires operator recovery. An administratively changed label policy or source
revision can race any HTTP client; the POST targets the original commit and
post-write verification must still pass. No positive result is reported as
verified for a newer revision merely because a stale revision endpoint returned
current labels.

## Tests and acceptance limits

`python3 scripts/test_tekton_gerrit_reporter.py` exercises the actual production
inspectors with explicitly simulated controller/worker records. Cases include
missing/mismatched children, checkout ordering, nonzero exits, credential mounts,
worker placement, stale reviews, policy/account drift, copied votes, uncertain
writes, deduplication and all five packaging formats. Linux additionally uses
a disposable authenticated loopback HTTPS review surrogate to test real TLS,
authentication, one synthetic review write and read-back. It contacts no company
Gerrit, account or recipient.

The optional `ReporterTests.test_actual_kubernetes_reporter_rbac_and_failure_receipt`
uses the same explicit envtest/CRD inputs described in the submission runbook,
with `TEKTON_GERRIT_TEST_API=1`. It checks a real restricted reporter token,
denied run/Secret mutations, API pagination and immutable publication receipts.
Its failed controller status and Gerrit review endpoint are simulated; no
successful worker execution or real Gerrit vote is claimed. The receipt retains
false flags for live Gerrit, worker execution, review acceptance, CD and HA.

The separate [real Gerrit acceptance case](GERRIT_REVIEW_RUNTIME.md) additionally
exercises a pinned actual distribution, native tokens, real Git checkout,
review authorization, tagged-message/vote read-back and stale revisions. Its
CI controller/worker evidence remains modeled and does not certify execution.
The additional [real K3s worker case](K3S_VM_WORKERS.md) packages real current
patch-set submission and evidence-gated result reporting with separate API
identities. Its separate source-bound execution passed with actual worker
checkout, two successful application tests, native bot vote/read-back, replay
and stale patch-set rejection. A further deliberate test failure executes on
the actual worker and produces a native negative vote without a successful
worker claim; replay does not repost it. The runbook records the limits and
terminal log digest.
Its execution inspector projects only the known empty EmbeddedTask wrapper to a regular
TaskSpec, rejects nonempty custom-task content, and retains command, ordering,
image, identity and credential checks.

The separate [merged-commit GitOps handoff](GERRIT_GITOPS_HANDOFF.md) now consumes
these receipts with read-only source credentials and a separate deployment-branch
writer. Open/unmerged reviews and a different resulting merge SHA cannot promote.
Its separate [combined K3s case](K3S_VM_WORKERS.md) now passes real worker CI,
native voting, exact merged-commit promotion and actual Argo CD HTTP workload
reconciliation with distinct restricted identities. This manual flow is
separate from the modeled CI protocol case.
The additional native watch-service case uses the actual packaged `--watch`
entry points under separate non-root systemd identities. Automatic positive
and negative reporting, forced main-process restart and preserved publication
receipts have passed with real workers. Its separate runtime receipt and cleanup
evidence are recorded in the worker runbook; it does not certify fleet service
installation or automatically invoke the deployment handoff.
The later joined case now verifies automatic reporting followed by explicit
promotion through the actual packaged CLI, using a separate read-only CI
credential and controller-read identity. It retains the original native vote
and worker receipts through merge, checks the new private journals, observes
actual Argo CD HTTP serving and audits removal of every temporary service.
This remains an operator-approved release flow, not unattended promotion.

Complete fleet Gerrit deployment/auth/label integration, fleet installation of continuous
submission/reporting services, fleet review/CI/CD, application artifacts, guarded
workload installation, load/failure tests, upgrades and restore remain required.
This result path does not certify a complete stack or physical-node HA.
