# Restricted Harbor CI publishing identity

Status: **actual local robot publication case passed**: one test, zero skips,
in 145.181 seconds; its foreground supervisor finished in 148.087 seconds with
unchanged public Python hashes and removal of the owned processes/cgroup.
This fills a CI credential prerequisite and does not install Harbor,
configure a CI worker or certify a complete stack. Existing profiles and robot
accounts remain unchanged unless this helper is explicitly applied.

## Scope and preconditions

`scripts/provision_harbor_robot.py` creates one new expiring Harbor 2.15 project
robot. The operator must already have permission to inspect the selected private
project and create/list its robots. The plan pins the exact Harbor build string,
HTTPS origin, CA file/hash, presented leaf certificate hash, project ID/name,
robot username and lifetime (1–90 days). This reviewed plan uses Harbor's default
`robot$` username prefix; a different prefix is outside this helper's accepted
configuration. It contains only a management-password
environment-variable name, never a password. The handoff contains the robot
credential; it does not contain the operator credential.

The only grants are repository `pull` and `push` in that one project. They cover
**all repositories in the selected project**, not an individual repository.
There are no system, wildcard, delete or project-administration grants. Use a
dedicated private project when CI publication needs a narrower organizational
boundary. Public or proxy-cache projects are refused.

The helper checks the actual project and Harbor build before any creation. It
uses verified HTTPS with the pinned leaf, bounded requests/responses and no
redirect following. It accepts only the selected read endpoints and one exact
project-robot POST; update, delete and secret-refresh operations are absent.
Project robots are enumerated by project ID with stable counts, ordered unique
IDs and a 1,000-account bound. Harbor's `q` parser unescapes a name containing
`+` again, so a literal full-name filter can miss an existing robot; the helper
checks the exact full username locally after complete project enumeration.

## Explicit use

Copy `examples/harbor-robot.example.json` into ignored private storage and replace
its synthetic origin, project ID, version and certificate hashes with reviewed
values. Keep robot names and project scope explicit. From the checkout on the
management host, set the operator credential without putting it in the plan:

```bash
read -rsp 'Harbor project operator password: ' HARBOR_OPERATOR_PASSWORD; echo
export HARBOR_OPERATOR_PASSWORD
python3 -B scripts/provision_harbor_robot.py \
  --plan private/harbor-robot.json --mode preview
```

Preview performs server reads and writes no journal or credential files. After
reviewing the selected project and available name, choose a new private operation
directory whose parent exists:

```bash
python3 -B scripts/provision_harbor_robot.py \
  --plan private/harbor-robot.json --mode apply \
  --state-dir private/operations/harbor-ci-robot-new
```

The CLI requires ignored output beneath this checkout's `private/` directory.
It reserves the operation under a lock before the only POST, saves the one-time
creation response before interpretation, and verifies the returned robot ID,
creation timestamp, expiry, ownership marker, flags and exact permission set.
It also checks the credential against the pinned token service and requires
exact project pull/push claims. Token-shape inspection is not an independent JWT
signature verifier; the separate live publication test must exercise Harbor's
authenticated data plane. Provisioning alone leaves artifact-publication and HA
flags false.

Both `create-response.json` and `credential.json` contain the robot secret. Keep
them private and deliver only the robot credential through the CI secret store;
CI publication must not receive the operator password. Normal output contains
aggregate verification flags, not credentials or raw API responses.

## Resume and uncertain writes

```bash
python3 -B scripts/provision_harbor_robot.py \
  --plan private/harbor-robot.json --mode resume \
  --state-dir private/operations/harbor-ci-robot-new
```

Resume makes server reads only and may update the local private verification
journal. It binds the original plan/tool hashes, operation marker, robot ID,
creation/expiry metadata and retained credential. It refuses deleted/recreated
robots, expanded permissions, changed private handoffs and unrelated existing
accounts. It never re-POSTs, adopts another account, rotates a secret or deletes
an account to make a retry pass.

The pinned API generates the secret during creation; its create handler does
not use a caller-selected secret. If the response is lost before it is saved,
a matching server robot is insufficient to recover the secret. The helper
records that condition and stops for separately reviewed manual recovery. A
later verification failure with a saved response can resume using that same
response. Expiry, certificate/build changes, source changes and intentional
rotation need separate review; resume is not an upgrade or rotation mechanism.

## Local acceptance boundary

The optional case uses the [separately pinned local pool-health candidate](HARBOR_DB_POOL_HEALTH.md),
the real helper, native Harbor/PostgreSQL/Valkey processes, two core/jobservice
peers and actual OCI layer/config/manifest transfers. It must require one robot,
read-only preview, unchanged private credentials on resume, actual pull/push,
refused delete/admin/cross-project operations, and new work after each original
peer loss and cache/database promotion. Earlier robot tokens must still read
their original images. The same six real SQL audit jobs, strict TLS/quorum,
route fencing and owned process cleanup remain required.

Select `HARBOR_ROBOT_TEST_RUNTIME=1` plus the candidate/artifact environment from
the [candidate test](HARBOR_DB_POOL_HEALTH.md#explicit-runtime-selection), then run
inside the owned isolated namespace with its retained foreground supervisor:

```bash
PYTHONPATH=scripts python3 -B -m unittest \
  test_valkey_gateway.GatewayTests.test_real_harbor_robot_project_publication_after_failures_and_promotions
```

The measured case creates exactly one synthetic project robot, verifies the
same credential hash and database ID on every resume, and publishes distinct
OCI images in all five workload phases. Actual delete and project-creation
requests are refused; a cross-project token has no grants. Actual GET and upload
requests to the other project's endpoint are refused with that empty-scope
token; an upload with the selected-project token is also refused. Wrong-scope
reads of the published image are refused as well. An earlier robot token continues to read its original image
after core/worker loss and cache/database promotion. The native helper's pinned
leaf refusal and refusal to adopt an existing account are also exercised.
Ten portable robot contract tests cover malformed scope, preview, saved-secret
resume, uncertain creation, permission/identity/credential drift, pagination and
transport bounds. The related regression suite passed 120 tests with 20 opt-in
cases skipped. The first native attempt exposed the plus-name query issue;
that failed receipt remains separate from the accepted corrected case.

This does not verify actual Tekton/Zuul/Jenkins/Runner/Woodpecker worker secret
delivery, publisher-produced image deployment, Cosign/Kyverno policy, independent
storage/host failure, rotation, upgrades, backup/restore or the full five-runtime
stack matrix. A same-host helper test must not promote those gates.

For the optional daemonless static-output publisher and isolated Tekton task, see [Harbor static image publication](HARBOR_STATIC_IMAGE.md). Worker publication requires its own runtime acceptance; robot provisioning alone does not establish it.
