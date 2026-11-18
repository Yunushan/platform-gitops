# Single-active Jenkins controller

`scripts/render_jenkins_controller.py` renders a dedicated ServiceAccount,
ClusterIP Service and one-replica Deployment. It does not install them. Use a
private copy of [the example plan](../examples/jenkins-controller.json), replace
the image with a reviewed digest, and render with:

```sh
python3 scripts/render_jenkins_controller.py --plan /private/jenkins-controller.json
```

The controller uses a pre-existing PVC mounted at `/var/jenkins_home`, UID and
GID 1000, a writable temporary volume, and a `Recreate` deployment strategy.
The selected image must be compatible with these restrictions. Provision the
dedicated namespace, network policy, PVC and recovery process separately; the
PVC must be writable by the selected UID and accessible to the replacement
node. Jenkins documents the [persistent home requirement](https://www.jenkins.io/doc/book/installing/kubernetes/).

This is a single-active controller with downtime during replacement, not an
active-active Jenkins cluster. `Recreate` limits rollout overlap within this
Deployment but is not a fencing mechanism for other controllers or stale
writers. Do not point another controller at the same home. Test backup and
restore of controller configuration, build data, plugins and the separately
protected encryption material before using it for important workloads.

No bootstrap credentials, plugins, Gerrit events, worker provisioning, external
TLS, or GitOps promotion are configured here. Those require separately
reviewed integration and authentication. The default Jenkins setup process is
not disabled. The rendered ServiceAccount, Service, Deployment and equivalent
Pod have passed a disposable Kubernetes 1.35 API/restricted-admission fixture;
the controller image, storage behavior, plugins, failover and real pipelines
have not been run by that fixture. A separate local test started the published
Jenkins 2.580.2 LTS WAR with Java 21 in an isolated network namespace and ran a
synthetic freestyle job successfully. That test used a temporary home and
disabled the setup wizard inside the isolated fixture; it did not test the
controller image, persistent storage, worker isolation, or Gerrit integration.
It also queried a real parameterized build through Jenkins' API and confirmed
that the comparison tool rejects the build when Git SCM evidence is absent.
With a pinned public Git-plugin dependency set, the same isolated Jenkins
instance cloned a disposable local HTTPS repository, ran the job, and exposed
an SCM record that the comparison tool matched to the synthetic Gerrit data.
Another isolated test started real Jenkins 2.580.2 and Gerrit 3.13.10,
created an open synthetic change, and used the explicit trigger to queue a
Jenkins job. Jenkins cloned the exact patch-set ref over authenticated HTTPS
and ran the job; a duplicate trigger was refused. A fixture administrator
then approved and merged the change, and the comparison matched Jenkins' SCM
record with the real merged revision and branch head, and matched the build's
queue ID to the trigger reservation. The test disabled Jenkins' setup wizard
and used in-process API adapters and a session crumb;
it did not verify an automatic event receiver, Jenkins API-token authentication,
independent workers, or GitOps promotion. The separate optional
[result reporter](JENKINS_GERRIT_REPORTER.md) was also exercised with this
fixture using a dedicated Gerrit bot; it relies on explicit controller trust.

### Parameterized Jenkins job

`scripts/render_jenkins_gerrit_job.py` renders Jenkins freestyle-job XML from a
private copy of [the job plan](../examples/jenkins-gerrit-job.json). The job
uses the scoped Gerrit credential ID and worker label, fetches the supplied
`GERRIT_REFSPEC`, verifies `GIT_COMMIT` against the requested revision, and
runs the fixed argument vector. The renderer only writes XML to stdout; it does
not create or update a Jenkins job. Install and pin the reviewed Git plugin
and dependencies separately. After an administrator imports the job, read and
review the server's `config.xml` and use its raw SHA-256 in the trigger policy;
Jenkins may serialize imported XML differently.

Restrict permission to build or reconfigure this job to the reviewed trigger
and administrators. Git SCM checkout happens before the shell check, and
untrusted users who can supply build parameters could choose other refspecs.
The selected agent must isolate untrusted code, secrets, and other builds; the
renderer does not provision or attest that isolation. An isolated test imported
the generated job into real Jenkins and built two successive real Gerrit patch
sets with the same job configuration. The fixture used the built-in node only;
it does not establish production worker isolation or historical job provenance.
Docker and native-host activation are not implemented by this renderer.

## Raw-host and Docker artifacts

`scripts/render_jenkins_host.py` renders optional single-active host artifacts
from either [the raw plan](../examples/jenkins-host-raw.json) or
[the Docker plan](../examples/jenkins-host-docker.json). It does not create
users, directories, services, containers, or firewall rules. Both expose HTTP
only on loopback; provide an authenticated TLS entry point separately.

The raw plan produces a systemd unit and a checksum file for the selected WAR.
Install the exact rendered checksum file at the reviewed `checksum_file` path;
the unit checks it before starting. The Java binary, WAR, checksum file and
their parent directories must be root-controlled and not writable by the
Jenkins account. Provision a writable `JENKINS_HOME`, non-root account, and a
supported Java runtime separately. The unit does not independently attest the
Java runtime or its dependencies. The Docker plan produces a Compose service
using a digest-pinned image, a pre-existing bind mount, UID/GID 1000, a read-only
container root and no Docker socket. The image and home must be compatible.

On Linux, `python3 scripts/check_jenkins_host.py --plan /private/host-plan.json`
performs a read-only raw-host preflight. It checks the named account and group,
private home ownership and access, root-controlled regular artifact paths,
the WAR digest and the exact rendered checksum file. Use direct regular-file
paths, not symlinks. It does not execute Java, check its version, reserve the
port, start Jenkins, or verify backup and recovery. Its file and permission
checks passed against disposable staged files in WSL.

Both designs are single-active and need an external fencing and recovery
procedure before failover. They do not establish HA, bootstrap authentication,
or configure Gerrit, agents or GitOps. Renderer tests passed, and the raw unit
passed `systemd-analyze verify` with a disposable local executable path. Neither
host artifact has been deployed or recovery-tested on a real host.

## Read-only Jenkins and Gerrit comparison

`scripts/verify_jenkins_gerrit.py` optionally compares one successful Jenkins
build with a Gerrit change. Its default checks a merged change and the current
branch head. `--change-state NEW` checks an open current patch set and requires
`--journal` or `--queue-id`; it checks that the target branch remains stable
during the read but does not require the unmerged revision to be the branch
head. Use a private
copy of [the verification plan](../examples/jenkins-gerrit-verification.json)
with the reviewed Jenkins job configuration's raw SHA-256, job path and build
number, plus the exact Gerrit change, patch set, and revision. The job must
expose the three string parameters `GERRIT_CHANGE_NUMBER`,
`GERRIT_PATCHSET_NUMBER`, and `GERRIT_PATCHSET_REVISION`; its Jenkins Git plugin
API record must report exactly the specified revision and canonical Gerrit
HTTPS clone URL. Jenkins' [Remote Access API](https://www.jenkins.io/doc/book/using/remote-access-api/)
and Gerrit's [change API](https://gerrit-review.googlesource.com/Documentation/rest-api-changes.html)
must be accessible to scoped read accounts.

Place `jenkins-user`, `jenkins-token`, `gerrit-user`, `gerrit-token`, and a
CA bundle `ca.crt` in a private input directory, then run
`python3 scripts/verify_jenkins_gerrit.py --plan /private/plan.json --input-dir /private/inputs`.
The CA bundle must trust both HTTPS endpoints. The command only reads APIs and
does not print credentials or job configuration.

To link the build to a previous trigger, add `--journal` with the corresponding
private queued reservation file. The tool verifies that the reservation has
the same job and patch-set identity and that Jenkins reports the same queue ID
on the build. Alternatively, `--queue-id` accepts an independently obtained
queue ID. The result's `queue_identity_verified` is false when neither is
supplied. This comparison depends on the integrity of the private reservation
and the Jenkins controller; it does not prove checkout provenance.
New reservations also bind the reviewed job-configuration digest. The result
sets `job_config_reservation_verified` only when a matching new-format journal
is supplied; older reservations remain readable but do not establish that
binding. This checks the configuration observed at submission and comparison,
not whether it changed and was restored during the build.
The open-change mode also returns `review_vote_authorized: false`: it is a
read-only input to a future review reporter, not authorization to cast a vote.
Both open and merged comparisons passed against the disposable real Jenkins
and Gerrit fixture, using its in-process API adapters.

`scripts/resolve_jenkins_gerrit_build.py` can find the build number from a
queued reservation and run the comparison without supplying the build number
manually. Pass the original trigger plan as `--plan`, the queued record as
`--journal`, and the private credentials as `--input-dir`. It searches at most
1,000 Jenkins build records. A missing record is reported as
`not-found-in-bounded-history`; it may still be queued, pruned, or outside the
window. Running and terminal non-success results are reported without a vote.
For success, `build_verified` means the configured comparison passed; it does
not mean checkout provenance, review authorization, or deployment is proven.
The lookup and comparison passed against the disposable real Jenkins/Gerrit
fixture, using in-process API adapters.

The check compares current records twice; it does not prove the current job
configuration was the configuration used by the historical build, that the
reported checkout was actually tested, or that the data cannot change after
the check. It does not trigger Jenkins, vote in Gerrit, or authorize GitOps
promotion. A local loopback TLS fixture exercised the CLI with separate
synthetic credentials, Gerrit's response prefix and redirect rejection; the
servers were test doubles. A separate disposable test connected actual Jenkins
and Gerrit instances using synthetic credentials, as described above. Live
deployment, review-event triggering and end-to-end promotion remain unverified.

## Explicit Jenkins queue trigger

`scripts/trigger_jenkins_gerrit.py` checks a reviewed Jenkins job configuration
and an exact current, open Gerrit patch set. With `--apply`, it reserves the
identity in a private journal before sending one Jenkins `buildWithParameters`
request containing the change, patch set, revision, and `GERRIT_REFSPEC`. Use a
private copy of [the trigger plan](../examples/jenkins-gerrit-trigger.json),
the same private credentials described above (the Jenkins token must have
permission to build the selected job), and an existing mode-0700 journal
directory under this checkout's `private` directory:

```sh
python3 scripts/trigger_jenkins_gerrit.py --plan /private/trigger.json \
  --input-dir /private/inputs --journal-dir "$PWD/private/jenkins-trigger"
```

The command above performs preflight only. Add `--apply` to queue the job.
Configure and independently review the Jenkins job to declare and use all
four parameters, fetch the exact ref, and run trusted checks; a queue
acknowledgement does not establish that the job ran or tested the revision.
The code refuses a second submission for the same identity in the same journal
directory. A timeout, crash, or invalid queue acknowledgement leaves a
`reserved` record: inspect Jenkins and the original record before any manual
recovery. The reservation is host/storage-local and does not coordinate other
hosts or journal directories. Queue submission and source recheck are not an
atomic transaction. This is an explicitly invoked operation, not yet an
automatic Gerrit event receiver or a review vote.

Local tests cover the HTTPS request and duplicate/uncertain-write handling.
A network-isolated Jenkins instance also accepted a real queued build through
the trigger using an actual open Gerrit patch set. The production authenticated
Jenkins API-token path, automatic event delivery and multi-host behavior remain
unverified.

## Bounded scheduled discovery

`scripts/scan_jenkins_gerrit.py` can run one discovery cycle from a reviewed
[scan policy](../examples/jenkins-gerrit-scan.json). It reads the bounded
inventory of open, non-work-in-progress changes before any submissions,
preflights each pending patch set, and applies the per-cycle submission
limit. Existing recorded queue acknowledgements are skipped without rechecking
the current Jenkins queue; an unresolved reservation stops the cycle for
inspection. Calculate the policy digest with:

```sh
PYTHONPATH=scripts python3 -c 'import json,sys; from scan_jenkins_gerrit import canonical_digest; print(canonical_digest(json.load(open(sys.argv[1]))))' /private/scan.json
```

Use that digest with `--policy-sha256`, plus `--policy`, `--input-dir`, and
`--journal-dir` as for the explicit trigger. The default is read-only;
`--apply` queues pending work. A scheduler may invoke one cycle repeatedly,
but no scheduler or webhook receiver is installed by this command. A failure
after earlier submissions may leave partial progress; inspect the private
reservations and Jenkins queue. Do not use multiple journal directories for
the same source and job, and do not assume host-local reservations provide
cross-host coordination.

On Linux, `--watch --interval-seconds 60` repeats discovery until interrupted
or a cycle fails. The trigger and result scanner commands share a queue-cycle
lock, so a result scan does not read a trigger reservation while that command
is still submitting it. Their separate poller locks allow one of each to run;
all instances must use the same lock-capable queue directory. Manual individual
trigger commands do not take the shared cycle lock, and a stopped or uncertain
reservation still requires inspection. Restart a poller to reload its policy
and credentials.

A disposable real Gerrit/Jenkins fixture verified discovery, preflight, queued
patch-set builds and duplicate suppression. Its optional Argo CD mode also
exercises the trigger and result commands over HTTPS with Jenkins' native API
token authentication, followed by a real Git handoff and local Argo CD
reconciliation. The fixture account is broadly privileged; production RBAC,
worker isolation and multi-node operation remain unverified.
