# Guarded Zuul service packages

Status: runtime artifacts and launch guards, not an accepted complete CI stack.
Use the [configuration contract](ZUUL_CONFIGURATION.md) and keep all
[multi-runtime stack requirements](MULTI_RUNTIME_STACKS.md) in scope.

## Implemented artifacts

`scripts/render_zuul_services.py` generates:

| Runtime | Generated services | Executor status |
|---|---|---|
| RKE2, K3s, existing Kubernetes | Scheduler, merger and web Deployments, two replicas each, PDBs and internal web Service | Optional guarded executor Deployment; dedicated workers required |
| Docker | Scheduler, merger and web Compose services | Optional guarded executors with separate state; no implicit multi-host HA |
| Native Linux (`raw`) | Scheduler, merger, web and guarded executor systemd units | Requires a fresh local isolation/Ansible check before every launch |

See [container executor selection](ZUUL_EXECUTORS.md) before enabling them.
This is a milestone toward full support, **not** evidence that those executors
have passed live isolation tests. Node supply, configuration
repositories/jobs, coordination services, identity, routing, durability and live
acceptance are still required. No Secret, database, ZooKeeper server, worker
node, registry account or pipeline is created here.

## Render privately

Use the same private plan as the configuration contract. The image must contain
the exact reviewed Zuul release, its role CLI tools and Python 3. The runtime
does not download or install dependencies at launch.

```bash
python3 scripts/render_zuul_services.py \
  --plan private/zuul-config.json --runtime rke2 \
  --output-dir private/generated/zuul-services-rke2
```

Other choices are `k3s`, `k8s`, `docker` and `raw`. Existing output directories are
refused. Rendering never applies manifests, runs containers or starts units.
However, **running the generated packages starts Zuul** and may process events,
report results and manage its database schema. Review permissions, backups,
upgrade compatibility and the target environment before that separate action.

## Startup contract

`start_zuul.py` checks local inputs and, only with explicit `--start` and an
expected stable version, launches a fixed role command in the foreground. The
[Zuul operations guide](https://zuul-ci.org/docs/zuul/latest/operation.html)
documents foreground operation and graceful component management.

The wrapper checks the actual CLI's version output against the declared release.
Unrecognized or ambiguous formats fail closed; the selected release must be
tested rather than bypassing the check. This is not proof of image provenance or
dependency compatibility. All launch paths require non-root Linux operation.

Required environment/TLS/key-file checks run before launch. The approved public
host-key file is staged into the dedicated service home's `.ssh/known_hosts`;
existing different, symlinked or group/other-writable POSIX trust files are not
silently replaced. Before staging and again before daemon exec, the launcher
requires a parseable pin for every configured Gerrit SSH endpoint. Validation
uses the selected Zuul CLI's own explicit Python interpreter and actual Paramiko
parser/lookup semantics, so native virtualenvs need no second system Paramiko
installation. The CLI must be a reviewed Python console script with an absolute
interpreter shebang; ambiguous shell/environment wrappers are refused.

The endpoint selector follows `ssh_server` when explicitly configured, otherwise
`server`, plus the actual SSH port (29418 by default). Nonstandard ports require
`[host]:port` entries. Exact aliases and OpenSSH hashed hostnames are supported;
wildcard-only entries do not satisfy Paramiko's lookup. Certificate/revocation
markers require separate supported policy handling and are refused here; do not
discard an existing revocation policy to bypass this requirement. There is no
key scan, remote trust adoption, or unknown-key fallback. Read-only preflight
still inspects only local input metadata; these public-key content checks occur
during explicit launch. OpenSSH Git operations also retain strict host-key
checking and the fixed private key.

The selected upstream 14.3.0 Gerrit driver uses a warning policy for unknown
keys, so supplying a merely nonempty trust file is insufficient. The launch
checks prevent that unknown-endpoint path. Its real SSH transport is covered by
the optional case below; event streams, actual scheduler jobs and fleet
installation still require acceptance.
Credentials are not passed in command-line arguments or printed by the wrapper.
Treat upstream application logs as private; this wrapper cannot certify every
upstream error log's redaction.

Without `--start`, the wrapper is a read-only local-input checker and does not
stage files, inspect daemon versions or probe namespaces. Local input success
is not connection/CI/HA acceptance.

## Reviewed ZooKeeper TLS client candidate

Explicit launch now also requires the configured ZooKeeper certificate, key and
CA paths and a reviewed client implementation in the selected CLI interpreter.
Unreviewed releases fail closed. Rendering and metadata-only preflight still
work without installing Zuul; neither applies or starts services.

The official Zuul 14.3.0 client supplies CA material but does not explicitly
enable Kazoo hostname verification. The optional downstream profile
`14.3.0-zookeeper-strict-tls-v1` adds `verify_certs=True` and
`check_hostname=True` to that client. It retains the upstream CLI version but
**is a different artifact**, requiring a separately reviewed image digest.
No package is patched, downloaded or installed during launch.

Build the candidate from the exact official wheel in private staging:

```bash
python3 scripts/build_zuul_zookeeper_tls.py \
  --source-wheel private/validation/zuul/14.3.0/zuul-14.3.0-py3-none-any.whl \
  --output-dir private/generated/zuul-strict-tls
```

The builder requires upstream wheel SHA-256
`54b8cfcf397ce7324a87b3c0cfea5a8fcd0f430082ba6be25f9e62326d9949e2`,
refuses existing output, changes only the reviewed client code, updates wheel
RECORD hashes and adds a downstream notice. The reviewable source patch is
`patches/zuul/14.3.0-zookeeper-tls.patch`. Build a separate immutable runtime
image/environment from the resulting wheel and lock all dependencies. The
launch policy pins that client source; it does not establish provenance of the
whole image or qualify other releases by version label alone.

The opt-in `ZuulServicesTests.test_real_zuul_zookeeper_tls_components_and_session_fencing`
uses the owned three-member ZooKeeper fixture, original and candidate SDK locks,
and non-root client subprocesses. It compares actual hostname acceptance/refusal,
CA refusal, real component registration, exclusive locks and protocol-driven
session expiry, then observes the original peers across quorum faults. Its
reports remain private. The accepted local case ran one real test without skips
in 60.610 seconds. Independent audit matched all 422 source hashes, all 445
installed candidate package files, both complete SDK locks, the original peer
identities, and final parent/member/client cleanup. Related regressions ran
177 tests with nine explicit tool/runtime skips; Windows launcher/Compose tests,
lint, CI parity, documented targets and privacy checks passed. The wrong-name
baseline accepted the same server; the candidate produced actual certificate
verification errors for the wrong name and CA. No timeout was counted as TLS
refusal. Closing the real ZooKeeper session invalidated the old lock; a second
peer acquired it, and the original peer re-registered under a new session and
used a new lock only after release. Recovery checkpoints required acknowledged
component writes independently read back by the oracle.

This case is separate from scheduler/job, fleet PKI/ACL, physical failure-domain,
backup/restore and full-stack acceptance. The session-expiry case uses a real
protocol session close, not a client-network isolation test. Default application
ACLs and network-isolated session expiry remain separate acceptance work.

## Kubernetes core

Provision the namespace and two Secrets for each core role:

- `zuul-<role>-runtime`, with `database-uri`, `keystore-password` and
  `gerrit-http-password` keys.
- `zuul-<role>-files`, with the seven files in the configuration contract.

Only those keys are referenced; no Secret values are serialized into the
ConfigMap. Use role-scoped identities and privileges compatible with the
selected Zuul release, and retain compatible keystore encryption material
across the instance. Do not rotate it by recreating unrelated random values.

The embedded code/configuration is content-addressed and immutable. Use an
explicit cluster context and server-side apply after review. A changed image or
version changes the Pod template; configuration changes select a new ConfigMap.
The package does not implement a database migration or an HA-aware upgrade
controller. Stage upgrades and key rotation separately.

Pods have no service-account token or elevated capabilities and use a read-only
root filesystem. Mandatory anti-affinity spreads replicas of each role across
nodes. Each Pod has independent bounded ephemeral local state/cache; capacity,
cache rebuild time and workload requests need validation. These local directories
are not a backup of the keys/state stored in ZooKeeper or results in PostgreSQL.

Readiness checks prove a local command socket or web TCP listener exists, not
that Gerrit events, ZooKeeper quorum, SQL, jobs or reporting work. The web Service
is ClusterIP-only; authenticated TLS ingress and websocket routing remain
separate integration work. Optional executors are emitted separately, never as
privileged Pods.

## Docker core

Before running Compose, privately provide the three environment variables,
`private/<role>/` input directories and `state/<role>/` directories under the
generated bundle. Bind-source creation is disabled. Prepare state ownership for
UID/GID 1000 and input readability deliberately. Each service has its own state,
no capabilities and an explicit guarded command, without an inherited image CMD.

The web listener is published only on host loopback port 9000. Supply a reviewed
TLS/authentication reverse proxy and resilient external entry separately. One
Compose deployment on one host is **not** multi-node HA; replicas on independent
hosts and their coordination/failover require further orchestration and tests.
The image digest and version must be reviewed before any pull or run. The renderer
and Compose-parser tests do not perform either operation.

## Native Linux and executor guard

Install approved Zuul/Python tooling under `/opt/zuul/bin`, helper modules and
configuration under `/etc/zuul`, private role environment files under
`/etc/zuul/<role>.env`, and private input files under `/etc/zuul/private`.
Create the `zuul` service account separately. The units establish its state
directory, run without capabilities, restrict filesystem writes and apply
aggregate resource limits. Enable only the appropriate roles for each host.
Use dedicated, isolated executor hosts rather than cluster control-plane nodes.

Before executing a native executor, the launcher additionally requires:

1. A non-root Linux identity and installed `bwrap`/`zuul-manage-ansible` tooling.
   The guard verifies the manager and executor use the same Python environment
   and checks the manager's actual validation result. Zuul 14.3's
   `zuul-manage-ansible --validate` alone can exit zero even when Ansible
   environments are missing.
2. Successful validation of preinstalled compatible Ansible environments.
3. A fresh bubblewrap probe with new namespaces, a visible read-only test mount,
   an inaccessible outside canary, a distinct network namespace and a rejected
   write to the test mount.

The probe uses only temporary dummy files and retains no credential contents.
It does not disable host confinement, enable root, install packages, add
capabilities or change kernel settings when blocked. Select a properly approved
host/kernel policy instead of adding a privileged fallback. Review the selected
[Zuul executor behavior](https://zuul-ci.org/docs/zuul/latest/configuration.html#executor)
and [bubblewrap model](https://github.com/containers/bubblewrap/blob/main/README.md).

Passing this small generic probe is **not** proof that all real trusted/untrusted
playbooks are isolated correctly by the selected Zuul release. Test the actual
executor, worker access, secret boundaries, resource limits and malicious-job
negative cases before accepting CI. The Windows fixture tests inspect probe
arguments and simulated success/failure; they do not establish Linux isolation.

## Remaining acceptance

The optional [Gerrit/worker canary](ZUUL_CANARY.md) supplies a narrow first job
for the runtime packages. It allocates an approved worker and reports comments;
it does not validate application code, establish production gating, or replace
the acceptance tests below.

Provide and test ZooKeeper HA/TLS, database/schema/backups, node supply,
native/container executor isolation, trusted pipelines, credentials, event recovery,
Keycloak/OIDC and external routing. Run real review-trigger/fetch/merge/job/report
tests, then node-loss, upgrade, restore and adversarial worker tests for each
requested stack/runtime. No live Zuul daemon or job was started in the structural
tests, and all generated CI/HA acceptance flags remain false.

## Optional actual Gerrit SSH transport check

`ZuulServicesTests.test_real_upstream_gerrit_ssh_host_key_enforcement` is gated by
`ZUUL_GERRIT_SSH_TEST_RUNTIME=1`. It requires the reviewed private 14.3.0
environment, its exact dependency/file lock, and the existing pinned Gerrit/Java
artifacts. Set `ZUUL_GERRIT_SSH_TEST_ENVIRONMENT` and a fresh private
`ZUUL_GERRIT_SSH_TEST_REPORT`; run the single test in a fresh isolated root Linux
network namespace with only loopback. Nothing downloads during execution.

The Gerrit fixture enables SSH only through an explicit `ssh=True` loopback
profile. Existing fixtures still disable SSH by default. A separate non-root
client invokes the actual pinned upstream Gerrit driver transport and production
trust helpers. Matching pins must authenticate and return Gerrit's real version;
a changed pin must produce `BadHostKeyException`. Missing/wrong-port pins must
stop before transport opening or trust-file staging. Public host pins come from
the fixture's provisioned host-key files, not an unauthenticated network scan.
Original process/scope removal, source hashes and private proof handling remain
required. The accepted local case ran one real test without skips in 66.222
seconds. Independent inspection matched all 420 source hashes, 445 upstream
wheel files, the complete dependency lock, the exact upstream SSH method bytes,
all four non-root client processes and original Gerrit/parent cleanup. The
pinned dependency set uses Paramiko 5.1.0. Related regressions ran 173 tests with
eight explicit tool/runtime skips; lint, CI parity, documented targets and
privacy checks passed. This qualifies the transport/launch-pin boundary only;
it does not certify scheduler, merger, web, executor, event streams, CI/CD,
physical HA, general Python compatibility or complete runtime support.

## Optional actual core startup and metadata recovery

The local executor experiment additionally has a separate Python 3.12 Zuul
candidate with Ansible 9 and 11. Its package inputs and installed runtime were
fingerprinted and independently rescanned. That mutable local fingerprint is
an experiment baseline, not a signed image, immutable artifact, or evidence
that a Zuul job has run.

In a separate isolated local test, the guarded executor reached Zuul's
`running` component state with that candidate and the two supported Ansible
environments. The test retained the executor through ZooKeeper fault checks,
then verified process cleanup and the unchanged runtime fingerprint. This
proves local daemon startup only; no job or worker execution was tested.

A subsequent isolated integration run combined Gerrit, scheduler, merger,
launcher, executor and two chrooted loopback SSH workers. A disposable Gerrit
change triggered the `startup-probe` job; Zuul's build API reported `SUCCESS`
for that change. An independent host-side check found the expected unique marker
in exactly one disposable worker's mounted evidence directory, owned by the
non-root worker identity. The fixture verified service cleanup and an unchanged
test runtime fingerprint. This establishes one local job path, not production
worker isolation, multi-host HA, or complete stack support.

The local integration fixture also killed the idle executor after one
successful job, waited for its original ZooKeeper session to expire, started a
replacement, and verified a second Gerrit change succeeded with a fresh
worker-side marker. This checks restart recovery between jobs; it does not
by itself test interruption of a running job.

A separate local run paused a job after it wrote a build-specific start marker,
killed the executor, and restarted it after its coordination session expired.
The same Gerrit change later reached `SUCCESS` under a different build ID, with
a matching non-root worker-side completion marker. This demonstrates a retry
in that isolated scenario; it does not establish exactly-once side effects or
multi-host availability.

`ZuulServicesTests.test_real_guarded_web_pair_with_postgres_and_zookeeper` is
opted in with `ZUUL_WEB_TEST_RUNTIME=1`. The local profile requires the reviewed
strict-TLS SDK lock, PostgreSQL artifact lock, pinned Gerrit/Java/ZooKeeper
inputs, a fresh private report and an original bounded Linux systemd unit in a
fresh network/mount namespace. The fixture starts three real PostgreSQL
replicas with synchronous `remote_apply` and TLS, one real Gerrit server, three
TLS ZooKeeper members, and two instances each of scheduler, merger and web.
No existing service, database, repository or company credential is adopted.

A scoped CI account receives repository read access and the
[Stream Events capability](https://gerrit-review.googlesource.com/Documentation/cmd-stream-events.html).
It does not receive administrator access. A real configuration change is
published, reviewed and submitted to the disposable Gerrit configuration
repository before daemon startup. Actual mergers fetch that configuration and
actual schedulers prime the shared state consumed by the web services. The
fixture does not fabricate scheduler cache nodes to make web readiness pass.
A pipeline and worker job are loaded, but no review is triggered to execute the
job in this case. Worker allocation and job execution remain separate work.

Each daemon executes the actual packaged launch guard under its own non-root
identity and UTS/mount namespace, without capabilities. Its resource limits
match the native package: 4 GiB, four CPU quota units and 4096 tasks. Web
instances co-host separate loopback bind addresses at port 9000; their remaining
configuration and secret paths come from the rendered bundle. These loopback
metadata APIs do not qualify external TLS routing, Keycloak or multi-host HA.

The accepted local case ran one real test without skips in 189.460 seconds.
Independent inspection matched all 423 source hashes, all 445 candidate package
files, both reviewed SDK/database artifact closures, seven daemon process
identities including the restarted web peer, and complete original process and
scope cleanup. Four actual application database sessions used TLS. The
configuration contained literal percent signs and an encoded database URI, and
actual Gerrit metadata requests and configuration Git clones used the approved
private CA. Related regressions ran 207 tests with 13 explicit tool/runtime
skips; Windows renderer/launcher/Compose, lint, CI parity, documentation and
privacy checks passed.

The web survivor must respond immediately after its peer is killed. The
fixture preserves Zuul's 120-second session request and waits for the negotiated
ZooKeeper lease to remove the old registration before restart; that observation
took about 41.9 seconds in this case. The original state path is reused. The
remaining original core processes and their metadata APIs are observed after
ZooKeeper leader loss, quorum loss and a member network partition. This is
startup, configuration priming and metadata availability evidence. It does not
prove review-event delivery/replay, executor isolation, real worker jobs,
database promotion, application writes during outages, production ACLs,
backup/restore, upgrades, physical failure domains or the complete stack matrix.
