# Optional PostgreSQL HA policy packages

This adapter prepares the database policy required by the multi-runtime stacks.
It renders CloudNativePG resources for RKE2/K3s/existing Kubernetes and Patroni
configuration and startup packages for Docker/native Linux. Rendering and
credential materialization run without starting a database. The separate guarded
Kubernetes installer can explicitly create a fresh CNPG `Cluster`; the prepared
operator then initializes its members. It never adopts an existing database,
upgrades it, promotes a node or deletes resources/data. The existing
platform-postgres GitOps manifests have their own
[private renderer settings for synchronous replication and failover quorum](GITLAB_REPOSITORY_HA.md#provision-the-databases-with-cloudnativepg).

## Prepare a reviewed private plan

Copy `examples/postgres-ha.json` under ignored `private/`. Replace its UUID,
image digests, exact versions, addresses, failure domains, trust fingerprints
and storage references. The current policy lanes are PostgreSQL 17/18,
CloudNativePG 1.30 and Patroni 4.1. These are configuration compatibility lanes;
the selected images, executables and dependencies still require provenance,
version, licensing and live compatibility checks before installation.
Declare positive resource requests and limits; Kubernetes resources carry those
budgets, while native/container enforcement remains part of runtime provisioning.

Declare three distinct member hosts, addresses, state paths and physical failure
domains. Kubernetes plans also bind three distinct live Node UIDs and hostname
labels. Native/Docker plans may set `node_uid` to null; they do not require a
Kubernetes database controller. Declared topology is not live identity evidence.
The CNPG affinity policy restricts scheduling to the selected hostname labels
and requires placement across the selected failure-domain key. The guarded
Kubernetes installer checks Node UIDs and actual labels before creation.

Choose one or two synchronous standbys and either `on` or `remote_apply` commit
semantics. Zero standbys, asynchronous fallback and relaxed durability fields
are refused. Losing the required standby count can block writes. Client/session
overrides, promotion safety, client reconnection and workload latency still need
live acceptance; a server default alone is not an RPO guarantee.

The CNPG policy uses required quorum replication and failover-quorum checks,
with supervised primary updates. Its bootstrap Secret references an application
owner rather than a database administrator. Native templates use a dedicated
Patroni scope/UUID, mTLS ZooKeeper identities/ACLs, strict synchronous quorum,
TLS-verified replication, restricted HBA rules and a required watchdog. REST
administration requires both client certificates and a separate Basic-auth
credential. No automatic deletion of divergent/failed-rewind data is enabled.

These settings follow the documented
[CNPG synchronous replication policy](https://cloudnative-pg.io/docs/1.28/replication/)
and [Patroni configuration model](https://patroni.readthedocs.io/en/latest/yaml_configuration.html).
Patroni's `bootstrap.dcs` only initializes dynamic configuration; editing it in
a new local file does not update an existing cluster. Such changes need a
separate reviewed dynamic-configuration operation and live verification.

```bash
python3 scripts/render_postgres_ha.py \
  --plan private/postgres-ha.json --runtime rke2 \
  --output-dir private/postgres-ha-rke2
```

Use `k3s`, `k8s`, `docker` or `raw` for another package. Output must be a new
directory under `private/`; an existing directory is never replaced. The
contract records the plan and generated/program hashes. Render status keeps
deployment, synchronous replication, promotion, restore and HA evidence false.
The planner retains every database foundation and full-stack acceptance gap.

## Guarded fresh Kubernetes installation

For an empty cluster, the optional [fresh operator bootstrap](CNPG_OPERATOR_BOOTSTRAP.md)
can prepare the separately pinned CNPG operator first. It does not upgrade or
adopt an existing installation.

The default 1.30.0 database package retains its vendored Cluster schema. Selecting
1.30.1 requires the reviewed official manifest via `--cnpg-release
private/cnpg-1.30.1.yaml` when running `render_postgres_ha.py`. The renderer extracts
the exact selected patch's schema; missing or unreviewed patches are refused
instead of silently falling back to 1.30.0. Docker/native plans do not consume
this Kubernetes-only release argument.

`scripts/deploy_postgres_ha.py` supports the RKE2, K3s and existing-Kubernetes
packages. Prepared Docker and native hosts use the separate startup path below. The
default command only verifies local files; it does not read a kubeconfig,
construct a server client or create a journal.

Copy `examples/postgres-installation.json` under `private/`. Bind the reviewed
contract to the exact context/kube-system UID, database and operator namespace
UIDs, operator deployment/CRD UIDs, digest-pinned operator image, storage-class
UID and canonical SHA-256 of its `parameters` JSON. Secret keys in this plan
must exactly match the names in the policy. Declare their reviewed UIDs/types;
never put credential values in either plan.

Provision the prerequisites independently:

- A database namespace with restricted Pod Security admission and three ready,
  schedulable nodes matching the declared UIDs, internal IPs, hostname labels
  and distinct failure-domain labels.
- The matching established CNPG CRD schema and a fully rolled-out operator
  deployment with at least two current pods across nodes/failure domains.
  Leader election must be enabled. Its container image and
  `OPERATOR_IMAGE_NAME` must both use the reviewed digest so injected instance
  managers do not fall back to a floating tag.
- An expandable Ceph RBD StorageClass with `Retain` reclamation and reviewed
  parameters. This is not evidence that Ceph itself passed failure/recovery
  acceptance.
- Immutable snapshots for the server CA, TLS certificate/key and application
  bootstrap credentials. The TLS Secret must use `kubernetes.io/tls`; the
  bootstrap Secret must use `kubernetes.io/basic-auth`. The tool requests key
  presence, type and UID, plus only the public CA and bootstrap username. It
  never requests password/private-key values or full Secret JSON.

The generated Kubernetes package includes a canonical copy of its reviewed
release's Cluster schema. The live served/storage schema must match it exactly. A schema
change requires review/regeneration, not an automatic compatibility guess.

First collect the concrete admission result:

```bash
python3 scripts/deploy_postgres_ha.py \
  --bundle-dir private/postgres-ha-rke2 \
  --deployment-plan private/postgres-installation.json \
  --check-server --output-dir private/postgres-admission-review
```

This performs server-side dry-run creation and writes `admitted-spec.json` and
its SHA-256 to the private proof. It rejects weakening of the desired policy or
alternate recovery/bootstrap sources. Review the admitted defaults and copy
the resulting digest into the private plan's `admitted_spec_sha256`. Then use
a new operation directory:

```bash
python3 scripts/deploy_postgres_ha.py \
  --bundle-dir private/postgres-ha-rke2 \
  --deployment-plan private/postgres-installation.json \
  --apply --output-dir private/postgres-installation-001
```

The installer repeats preflight/dry-run checks, records creation intent, then
uses create-only semantics. It does not apply over an existing object, force
field conflicts, prune resources or delete data. Existing children (including
unlabelled matching names), controller-generated identities and old/static
volumes are rejected. External snapshot names cannot collide with the
controller's generated client CA, replication or superuser identities.

Keep `postgres-install-proof.json`, the private plan, admission snapshot and
matching generator/installer revision in recovery storage. The journal pins
the installer's first-party import inventory as well as the policy contract;
changed programs are refused before server access on resume. To resume observation, repeat
the command with `--resume` and the same operation directory. A lost creation
acknowledgment is resolved against the same resource name, operation annotation,
contract, admitted specification and eventually recorded UID. Resume never
reissues an uncertain creation. If no resource appears before the bounded wait,
the journal is retained for further observation/investigation. A previously
observed UID disappearing or changing requires recovery, not automatic
recreation. A failure before creation intent may retry its checks and creation
on explicit resume. Fixes that change the private deployment plan require a new
reviewed operation rather than silently changing an old journal's scope.

Resource checks require three ready, pinned-image pods on the selected nodes,
current primary reports, three distinct owned Bound claims/Ceph volume handles,
`Retain` volumes and ready `-rw` service endpoints targeting the current primary
pod UID/IP. Privileged/host-network/host-path database pods are refused. These
are controller/resource observations, not SQL durability or application tests.
`resources_ready` can become true while synchronous-write, promotion, restore,
HA and complete-stack flags remain false. Prerequisites and resource readiness
are checked again before completion. Independent operator/root actions are not
fenced by this protocol.

## Observe the original CNPG database processes

After a completed fresh installation, use `scripts/verify_postgres_runtime.py`
to inspect the original database instances. It requires the same package and
deployment plan plus a SHA-256 pin of the completed `postgres-install-proof.json`.
Default mode validates these local inputs without constructing a Kubernetes
client, reading the management kubeconfig or writing evidence:

```bash
python3 scripts/verify_postgres_runtime.py \
  --bundle-dir private/postgres-ha-rke2 \
  --deployment-plan private/postgres-installation.json \
  --installation-proof private/postgres-installation-001/postgres-install-proof.json \
  --installation-sha256 REVIEWED_INSTALLATION_RECEIPT_SHA256
```

For live observation, append `--observe --output-dir private/postgres-runtime-001`.
The output directory must be new. Provision the deployment plan's explicit
management kubeconfig first. The identity needs the installer's read permissions
and `pods/exec` in the database namespace. SQL runs as the local `postgres`
database superuser through the CNPG container's peer-authenticated Unix socket;
this is a privileged diagnostic identity. Keep that capability scoped to the
reviewed database namespace and operators.

The verifier freezes the management configuration, repeats the operator,
node, namespace, Secret and storage prerequisites, and matches the original
Cluster UID, operation annotations and complete admitted specification. It
checks the primary, both standbys, then the primary again. Each exec selects
the `postgres` container and a fixed PostgreSQL 17/18 `psql` path, clears the
environment, disables psql startup files/password prompts, directs password-file
lookup to `/dev/null` and executes only a
fixed, bounded read-only transaction ending in rollback. No configurable SQL,
password/role catalog, application table, connection string, promotion or
configuration-changing function is queried.

Acceptance of this observation requires:

- The exact reviewed running PostgreSQL patch version on all three instances;
  one physical system identifier and timeline; the expected primary/standby roles.
- Active checksums, fsync, full-page writes, TLS, SCRAM defaults and the reviewed
  synchronous-commit policy, with no pending restart in the checked settings.
- An explicit `ANY N` quorum naming exactly the two current standby pods;
  two streaming senders in quorum state whose client IPs match those pods,
  with TLS 1.2/1.3 and presented client certificates.
- Live standby receivers connected to the expected primary service/address
  on the expected timeline, recent WAL messages and non-regressing WAL positions.
- Unchanged pod UIDs, container process/restart identities, primary promotion
  timestamp, generation, claim/PV identities and CSI bindings across the check.

The private `postgres-runtime-proof.json` stores scoped identities, summary
observations and hashes of the inputs/query/tooling. Raw SQL payloads are not
persisted; stdout contains only result flags. A changed or failed observation
records a failed proof with all runtime acceptance flags false. Existing evidence
is never overwritten; perform a new observation into a new directory.

`runtime_configuration_observed` and `streaming_quorum_observed` may become true.
`synchronous_replication_verified`, `write_durability_verified`, promotion,
restore, HA and full-stack flags remain false: no application write, quorum-loss
test, client TLS handshake, login/permissions test or failover occurs. SQL
observations are a bounded sample, not fencing against concurrent independent
administrators or a guarantee of future state.

The default runtime suite uses simulated Kubernetes and SQL responses. An
additional disposable root Linux fixture runs the exact query on three real
PostgreSQL 18 processes, using TLS client certificates for replication. It
initializes only temporary `/run` data, runs the servers under UID/GID 65534,
verifies read-only write refusal and weaker-quorum rejection, and stops its
own processes before deleting their temporary data. It refuses an occupied
loopback port 5432; it never borrows or stops an existing server. Opt in with
reviewed local PostgreSQL 18 binaries and, if necessary, an extracted library
directory:

```bash
POSTGRES_RUNTIME_TEST_BIN=/opt/validation/postgres18/bin \
POSTGRES_RUNTIME_TEST_LIBS=/opt/validation/postgres18/lib \
  python3 -B scripts/test_postgres_runtime.py
```

The fixture adapts only executable/socket/library locations for isolated local
processes; production observation always uses CNPG's fixed container/socket
locations. This fixture does not run CNPG, Ceph, Kubernetes or real failure
domains. The SQL fields and meanings follow the PostgreSQL
[statistics views](https://www.postgresql.org/docs/18/monitoring-stats.html) and
[control data functions](https://www.postgresql.org/docs/18/functions-info.html#FUNCTIONS-CONTROLDATA).
CNPG's pinned source defines its
[socket location and local peer mapping](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.0/pkg/postgres/configuration.go).

## Separate disposable database promotion evidence

The opt-in local PostgreSQL fixture can execute a controlled clean-shutdown
promotion with `POSTGRES_PROMOTION_TEST_RUNTIME=1` in an owned isolated network
namespace. It stops the exact old primary, requires replay of its final shutdown
checkpoint, promotes the original candidate process and rejoins the old primary
as a standby. The unchanged SQL verifier must then accept all three members,
the advanced timeline, verified-TLS replication and the original strict
synchronous policy. The standalone case passed in 5.460 seconds:

```bash
PYTHONPATH=scripts python3 -B -m unittest \
  test_postgres_switchover.SwitchoverTests.test_actual_clean_shutdown_checkpoint_promotion_and_strict_replica_rejoin
```

Supply the same private PostgreSQL binary/library inputs as the real runtime
query fixture. This mode requires `pg_controldata` as well. It refuses to run
without explicit opt-in and a separate loopback-only network namespace. Its
initialization and negative-policy checks use disposable owned data only.

The [Harbor reconnection component test](HARBOR_SERVICE_RUNTIME.md#optional-planned-database-promotion-and-client-reconnection)
also passes new authenticated API writes and real job execution through original
application processes after this handover. It uses an explicit local TCP route
switch after strict verification. Neither case invokes the CNPG controller or
Patroni; neither proves automatic failover, watchdog/DCS fencing, network
partition handling or independent physical failure domains. These results must
not be substituted for live operator/HA acceptance.

## Request a planned CNPG switchover

`scripts/manage_postgres_switchover.py` provides an explicit planned primary
handover for the RKE2/K3s/Kubernetes CNPG path. Applying it changes the writable
primary; application connections may need to reconnect. The operator owns the
demotion, promotion and rejoining of the old primary. Native/Patroni handover
and database-image upgrades remain separate work.

First create a new runtime proof with the current verifier, as described above.
Copy `examples/postgres-switchover.json` into ignored `private/` and pin the
original deployment plan, completed installation receipt, new runtime receipt,
current primary pod and chosen standby pod. The deployment-plan hash is the
SHA-256 of canonical sorted compact JSON, rather than the original file bytes;
the installation/runtime receipt hashes cover their exact file bytes.

This local preparation recipe builds those pins without displaying private
evidence. Set `target` to a reviewed standby name from the private runtime
receipt; the selected source and target must be distinct. It validates the
prepared plan and creates the output once, with private file permissions:

```bash
python3 - <<'PY'
import hashlib, json, os, sys
from pathlib import Path
sys.path.insert(0, "scripts")
from bounded_file import read_bounded_bytes
from strict_json import loads_strict_json
from deploy_postgres_ha import fingerprint
from manage_postgres_switchover import load_inputs

bundle = Path("private/postgres-ha-rke2")
deployment = loads_strict_json(read_bounded_bytes(Path("private/postgres-installation.json"), max_bytes=128*1024))
installation = Path("private/postgres-installation-001/postgres-install-proof.json")
runtime = Path("private/postgres-runtime-001/postgres-runtime-proof.json")
raw = read_bounded_bytes(runtime, max_bytes=256*1024)
proof = loads_strict_json(raw)
target = "platform-postgres-2"  # Replace with the reviewed standby pod name.
source = proof["topology"]["primary"]
plan = {
    "schema_version": 1, "contract_sha256": deployment["contract_sha256"],
    "deployment_plan_sha256": fingerprint(deployment),
    "installation_sha256": hashlib.sha256(read_bounded_bytes(installation, max_bytes=256*1024)).hexdigest(),
    "runtime_sha256": hashlib.sha256(raw).hexdigest(),
    "source_primary": {"name": source, "uid": proof["topology"]["pods"][source]["uid"]},
    "target_primary": {"name": target, "uid": proof["topology"]["pods"][target]["uid"]},
}
load_inputs(bundle, deployment, plan, installation, runtime)
path = Path("private/postgres-switchover.json")
with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as output:
    json.dump(plan, output, sort_keys=True, indent=2)
    output.write("\n")
print("Prepared a separate private switchover plan; no server was contacted.")
PY
```

Validate the local inputs first:

```bash
python3 scripts/manage_postgres_switchover.py \
  --bundle-dir private/postgres-ha-rke2 \
  --deployment-plan private/postgres-installation.json \
  --switchover-plan private/postgres-switchover.json \
  --installation-proof private/postgres-installation-001/postgres-install-proof.json \
  --runtime-proof private/postgres-runtime-001/postgres-runtime-proof.json
```

Default mode constructs no Kubernetes client, reads no management kubeconfig
and writes no journal. For server review, append
`--check-server --output-dir private/postgres-switchover-review-001` to the same
command. The management identity needs read/diagnostic permissions and `patch`
on `clusters.postgresql.cnpg.io/status`, restricted to the reviewed cluster.
Server review repeats the complete live SQL/topology checks and sends only a
server dry-run status patch. It does not change the live primary.

For the deliberate handover, use the same command with
`--apply --output-dir private/postgres-switchover-001`. Live SQL/topology checks
are repeated after dry-run. A different pod/process, database history, primary,
node, volume, operator, trust snapshot or admitted specification causes refusal.
The request uses the status fields employed by the
[pinned CNPG promotion plugin](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.0/internal/cmd/plugin/promote/promote.go),
with JSON-patch tests on UID, resourceVersion, the full specification, current
primary, prior target and prior promotion timestamp. The private patch file
is read directly by kubectl and is never placed in command arguments or public
files. Its temporary copy is removed after the request. API admission can still
reject the patch; Kubernetes CEL/webhooks and controller behavior require live
acceptance on the intended cluster.

The helper writes `status-request.json` and fsyncs the request intent into
`postgres-switchover-proof.json` before the single real status request. After
acknowledgment loss, process interruption or an observation timeout, resume the
same directory using `--resume` in place of `--apply`. Keep the same inputs and
tool revision. A recorded request intent is never submitted again, even if no
effect has been observed. Its exact target and target timestamp remain the
observation handle. A different request, missing/replaced Cluster, changed
chosen-primary pod or changed request file causes refusal. An unobserved intent
may remain unresolved; retain the evidence and inspect the private API/operator
diagnostics before preparing any separately reviewed new operation.

Completion requires the selected original target to be the ready primary, a
new timeline within the same physical database, the original claim/PV/CSI
bindings, the same selected target and third-standby processes, all three
instances streaming with the reviewed TLS quorum, and the read/write endpoint
pointing at that new primary. The former primary may be restarted/recreated by
the operator while preserving its data bindings and placement. The helper
rechecks readiness and topology before recording completion. Resume re-observes
these conditions instead of trusting a prior completed receipt.

`planned_switchover_observed` is a scoped observation. Promotion, synchronous
write durability, restore, HA and full-stack acceptance remain false. There is
no distributed transaction covering Cluster status, pod identity and SQL:
concurrent independent administrators/controllers cannot be fenced by these
client checks. Partition fencing, acknowledged-write survival, application
reconnection and load/failure behavior still require acceptance tests.

The default switchover suite simulates admission, the operator, SQL responses,
CAS conflicts, late scope changes, lost acknowledgments, superseded requests
and interrupted evidence writes. `POSTGRES_POLICY_TEST_SCHEMA=1` additionally
checks a patched resource against the actual vendored OpenAPI schema (not CEL
or admission webhooks). Set `POSTGRES_SWITCH_TEST_KUBECTL` to a local kubectl
binary to exercise the real private-file JSON patch and UID-test rejection
with an isolated configuration and `--local`; no API endpoint is contacted.
These tests do not establish live CNPG switchover or fencing behavior.

## Materialize native credentials separately

Docker/native packages contain one template per member and the pinned first-party
materialization helper. Default mode checks only the reviewed package and does
not read private credentials or write a runtime file:

```bash
python3 scripts/materialize_patroni.py \
  --bundle-dir private/postgres-ha-raw \
  --contract-sha256 REVIEWED_CONTRACT_SHA256 --member pg-1
```

For explicit materialization, stage the bundle read-only and prepare an absolute
input directory for the declared non-root UID/GID. All ancestors must be trusted,
without symlinks or writes by other identities. Provision these private files
outside the public repository:

- `superuser-password`, `replication-password`, `rewind-password`, `rest-password`:
  distinct credentials of at least 16 characters, readable only by the runtime
  identity, with at most one trailing newline.
- `postgres-ca.crt`, `postgres.crt`, `postgres.key`: the reviewed database trust
  anchor and member server certificate/key, with the member hostname in its SAN.
- `rest-ca.crt`, `rest.crt`, `rest.key`: the reviewed REST trust anchor and a
  member certificate/key usable for server and client authentication.
- `zk-ca.crt`, `zk.crt`, `zk.key`: the reviewed DCS trust anchor and client
  certificate/key. Its sole subject CN must match this member's ordered
  `zookeeper.principals` entry.

Use certificate authorities whose issuance scope matches these trust planes.
Keys must support noninteractive use and private files must have no group/other
permissions. Prepare a private owned output directory under `/run` for the
runtime identity. Then, as that UID/GID:

```bash
python3 -E -s -B /opt/platform/postgres-bundle/materialize_patroni.py \
  --bundle-dir /opt/platform/postgres-bundle \
  --contract-sha256 REVIEWED_CONTRACT_SHA256 --member pg-1 \
  --materialize --inputs-dir /etc/platform-postgres/inputs \
  --output-file /run/platform-postgres/pg-1/patroni.yaml
```

The helper checks the CA fingerprints and performs in-memory TLS handshakes to
verify chains, server hostnames, certificate/key pairs and client purposes. It
also checks the DCS certificate principal. No network connection is opened.
Credentials go into an atomic mode-0600 runtime file; they are not passed in
command arguments or printed. Identical materialization is idempotent. A
different existing file requires a separately reviewed rotation and is refused.
Member-local certificate paths stay outside shared DCS parameters.

CNPG uses `bootstrap` when it initializes the cluster. The Patroni host
package uses the same declaration for its application provisioner and accepts
an optional `applications` list of additional `database`, `owner`, and
`secret` declarations. Each database has a dedicated owner and credential
reference. Application passwords live in the private directory supplied at
execution.

## Start a Patroni member on a prepared host

The `raw` package renders one `patroni-MEMBER.service` per declared member.
The `docker` package renders one `compose.patroni-MEMBER.yaml` per member.
Install the package at `/opt/platform/postgres-bundle` and provision the
credentials at `/etc/platform-postgres/inputs` on each reviewed host. Stage
the reviewed Patroni 4.1.5 downstream wheel, Kazoo 2.11.0, the remaining
Python dependencies and the exact PostgreSQL binaries at the reviewed paths.
The launcher uses Python at
`/opt/platform/patroni/bin/python3`. For Docker, the pinned Patroni image must
already contain that environment and the PostgreSQL tools and be present in
the local image store.

Build the wheel from a local copy of the pinned upstream wheel and retain the
generated SHA-256 receipt with the package. Use a fresh output directory for
each build. The host renderer requires a Patroni release with a reviewed
ZooKeeper adapter. The Docker and native Linux stack plans identify the
builder alongside the host startup helper.

```bash
python3 scripts/build_patroni_zookeeper_tls.py \
  --source-wheel private/artifacts/patroni-4.1.5-py3-none-any.whl \
  --output-dir private/patroni-tls-build
```

The upstream 4.1.5 ZooKeeper adapter forwards CA verification to Kazoo and
relies on Kazoo's default of disabled hostname verification. The downstream
wheel sets `check_hostname=True` for TLS. Stage this wheel in the managed
Python environment or pinned container image before enabling the service.
[Patroni ZooKeeper adapter](https://patroni.readthedocs.io/en/latest/modules/patroni.dcs.zookeeper.html)
[Kazoo 2.11.0 client defaults](https://github.com/python-zk/kazoo/blob/2.11.0/kazoo/client.py)

Prepare a dedicated empty `state_path` owned by the declared UID/GID with mode
0700 on each host, along with separate watchdog access and the three mTLS
ZooKeeper endpoints. The launcher claims the state directory exclusively and
keeps its lock while Patroni runs. It records the cluster UUID and member in
a private marker and uses `state_path/pgdata` for the database. Subsequent
starts require the same claim. This layout requires a fresh state directory
or one already initialized by this launcher for the reviewed member. Keep
the storage provision mounted before starting the service.

With `/run/platform-postgres-MEMBER` created as mode 0700 for the non-root
UID/GID, run the packaged `start_patroni.py` as that identity with `--member`,
`--contract-sha256`, and `--check-runtime` for an optional preflight. It
materializes the volatile config, checks the exact runtime versions and the
installed ZooKeeper adapter's digest, and runs Patroni's upstream validator,
which probes all three ZooKeeper endpoints. Normal service startup checks the
same runtime identity and uses Patroni's quorum logic to handle DCS reachability.
[Patroni configuration validator](https://patroni.readthedocs.io/en/rel_4_1/patroni_configuration.html)

On native Linux, install the selected unit and explicitly start it with
`systemctl start patroni-MEMBER.service`. On Docker, run
`docker compose -f compose.patroni-MEMBER.yaml config` before using the same
file with `up -d`. Both runtime packages require accessible `/dev/watchdog`.
The Compose service uses host networking for the declared bind addresses,
read-only credential and code mounts, a private volatile runtime directory,
and only the member's data directory as a writable bind mount.

## Provision a PostgreSQL application on native Linux or Docker

Once the three Patroni members have established synchronous replication, run
the packaged provisioner from an administration runner with Psycopg 3 and
the reviewed PyYAML version. Select the current primary by its member name,
and use the PostgreSQL system identifier recorded independently for that
cluster. The bundled contract and TLS CA pin the planned cluster and member.
Supply `postgres-ca.crt` and `superuser-password` under `--inputs-dir`.
Under `--application-secrets-dir`, place each application's password at
`SECRET_NAME/password` using an owner-only file.

```bash
bundle=/opt/platform/postgres-bundle
python3 -E -s -B "$bundle/provision_patroni_application.py" \
  --contract-sha256 "$CONTRACT_SHA256" \
  --member "$PRIMARY_MEMBER" \
  --database "$APPLICATION_DATABASE" \
  --system-identifier "$POSTGRES_SYSTEM_IDENTIFIER" \
  --inputs-dir /etc/platform-postgres/operator-inputs \
  --application-secrets-dir /etc/platform-postgres/application-secrets \
  --apply
```

Omit `--database` to select the `bootstrap` database. Omit `--apply` to
validate the local package without opening a connection. For additional
applications, add their reviewed declarations to the host plan, render the
package, and apply its generated access rules to each member before running
the provisioner. Use the selected application's login and CA for its
connection to the platform's PostgreSQL write endpoint.

The provisioner serializes runs with a PostgreSQL advisory lock. It first
creates a disabled login and a database that refuses connections. A
transaction then removes public database privileges and enables the
declared login. A retry can finish that prepared state and verify an existing
managed login. Foreign ownership or a mismatched password causes a refusal.
The tool waits for the configured synchronous quorum and verifies login over
TLS with the declared application credential. [PostgreSQL CREATE DATABASE](https://www.postgresql.org/docs/18/sql-createdatabase.html)

## Publish a stable Patroni write endpoint

For a native Linux or Docker Patroni plan, add an optional `write_endpoint`
object with the shared DNS `host`, frontend `port`, allowed `client_cidrs`,
a digest-pinned `haproxy_image`, and two entries under `routers`. Each router
entry declares its own `name`, locally assigned IPv4 `address`, and
`failure_domain`. Run the routers on separate hosts or failure domains
and publish both frontend addresses under the shared DNS name. The addresses
serve TCP and need to remain reachable from the declared clients. Use the
existing CloudNativePG read/write Service on Kubernetes.

Every PostgreSQL serving certificate must cover both its member name and the
shared write hostname. The Patroni startup helper verifies both names against
the planned PostgreSQL CA. The rendered application access rules also allow
the two router source addresses. Copy the configuration for each router from
`postgres-write-NAME.cfg` to `/etc/platform-postgres-router/haproxy.cfg`.
In the same directory provision the Patroni REST CA as `rest-ca.crt` and a
dedicated client certificate and key for that router in `rest-client.pem`.
Restrict the private PEM to the router service identity.

On native Linux, install and start the generated `postgres-write-NAME.service`
using HAProxy 3.2. On Docker, stage the image locally, then use the generated
`compose.postgres-write-NAME.yaml`. The router permits only the declared
source networks and forwards a new SQL connection when exactly one member
passes a mutual-TLS `/primary` check. It uses TCP passthrough, so the
application verifies the PostgreSQL certificate with `sslmode=verify-full`
and the shared write hostname. When a backend fails its primary check,
HAProxy closes its existing sessions so that clients can reconnect through
the same endpoint. [Patroni REST API](https://patroni.readthedocs.io/en/rel_4_1/rest_api.html) [HAProxy health checks](https://www.haproxy.com/documentation/haproxy-configuration-tutorials/reliability/health-checks/)

Clients need to try both router addresses during reconnect. For libpq,
`host=postgres-write.example.com,postgres-write.example.com` with
`hostaddr=192.0.2.10,192.0.2.11` makes the fallback explicit while preserving
the same certificate name for both addresses. Set `target_session_attrs=read-write`
when the client supports it. A lost session leaves transaction outcome
handling to the application. [libpq connection parameters](https://www.postgresql.org/docs/current/libpq-connect.html#LIBPQ-PARAMKEYWORDS)

## Verification and remaining acceptance

The separate [physical backup helper](POSTGRES_BACKUPS.md) now captures a
prepared PostgreSQL source over certificate-authenticated TLS and verifies its
manifest, required WAL and whole-tree seal. It supports sources on all five
runtimes with a separately prepared Linux backup runner. Source-account wiring,
independent storage placement and continuous WAL archiving remain unfinished.
The [database restore drill](POSTGRES_RESTORE.md) can now boot a fresh verified
backup copy, replay its WAL and check reserved canaries before clean shutdown.
Full application/cluster recovery and HA gates remain false.

`scripts/test_postgres_ha.py` covers all five render paths, strict durability,
placement, template/program tampering and default read-only behavior. On a
disposable root Linux test host, opt in to CRD validation and a temporary
non-root materialization fixture:

```bash
POSTGRES_POLICY_TEST_SCHEMA=1 POSTGRES_POLICY_TEST_NONROOT=1 \
  python3 -B scripts/test_postgres_ha.py
```

The first option requires `jsonschema` and checks the generated resources against
the actual vendored CNPG OpenAPI schema. It does not execute Kubernetes CEL or
replace server-side dry-run/webhook validation. The second uses temporary
`/run` files, OpenSSL and UID/GID 65534 through `setpriv`. It checks real private
output permissions, certificate identities, idempotence and refusal of a
conflicting rotation. It starts no database, watchdog or system service.
With `POSTGRES_POLICY_TEST_PATRONI_PYTHON`, `POSTGRES_POLICY_TEST_PG_BIN`,
and `POSTGRES_POLICY_TEST_PG_LIBS` set, the isolated Linux case also runs
the packaged startup preflight with the reviewed Patroni wheel and PostgreSQL
18.6.
The fixture uses temporary TCP listeners for the validator's endpoint probes.
This verifies the configuration and CLI checks before database startup.

The disposable KVM watchdog fixture enables an emulated `i6300esb` device on
three guests. A non-root systemd service maintained the selected guest's
watchdog and then stopped sending keepalives. QEMU reported the watchdog event,
paused the original guest and kept the other two guests reachable. Run the
case with the pinned VM inputs from [the worker lab](K3S_VM_WORKERS.md),
`PATRONI_WATCHDOG_TEST_RUNTIME=1`, and the unittest selector
`WorkerRuntimeTests.test_real_patroni_watchdog_prerequisite` in
`scripts/test_k3s_worker_runtime.py`. [Patroni watchdog behavior](https://patroni.readthedocs.io/en/latest/watchdog.html)

A native service run deployed the generated units on three disposable Debian
guests using the downstream Patroni 4.1.5 wheel, PostgreSQL 18.4 and ZooKeeper
3.9.7. Two DNS names resolved to the same ZooKeeper TLS listener and used the
same trusted CA and client credentials. The certificate's DNS name connected
successfully and the other alias failed with a hostname mismatch. The launcher
accepted the installed downstream wheel and rejected the stock 4.1.5 wheel.

The PostgreSQL client verified server hostnames and the active `remote_apply`
setting. Both standbys replayed through the primary's checkpoint before its
Patroni process was suspended. PostgreSQL served queries until the watchdog
paused the guest. A survivor promoted with its remaining synchronous standby,
read the original row and committed a second row. Both survivors contained both
rows. The original guest then rebooted and rejoined as a read-only standby
with the same database system identifier. All three members received a third
committed row. The 198.121-second receipt matched the 553 Python sources,
fixture driver, downstream wheel and guest runtime archive. Independent
cleanup checks confirmed removal of the VM processes, transient unit and
scratch directory. An earlier run without the replay checkpoint gate timed
out waiting for promotion.

A separate three-guest test ran the rendered Compose packages with Docker
Engine 29.9.3 and Compose v2.40.3. It staged the same digest-pinned image on
each guest and stopped the fixture registry before starting the services.
The test observed non-root containers, read-only root filesystems, a single
writable state bind mount per member, TLS-verified SQL and synchronous
replication. After both standbys replayed the primary's checkpoint, the test
suspended the Patroni child that held the watchdog. The watchdog fenced the
guest, a standby promoted and the original member rebooted and rejoined as a
read-only standby. All three contained the new committed row. Compose removed
the containers, and independent cleanup checks confirmed removal of the VM
processes, transient unit and scratch directory. The 371.806-second receipt
matched 553 Python sources, both fixture drivers and all five runtime inputs.

Subsequent native Linux and Docker Compose runs exercised the packaged
application provisioner on their three-member Patroni clusters. The tests
created a dedicated database and login for each of two applications, retried
completed runs, resumed a database left closed by an interrupted run, and
refused a standby, a different system identifier, a mismatched credential,
and an unmanaged role. The application logins could write in their own
databases. Cross-database access, connections without TLS, and global role
or database creation were refused. Following watchdog fencing, both
applications wrote on the promoted primary and the rejoined standby could
read each application's pre- and post-promotion rows. The native and Docker
receipts matched the current Python sources and fixture inputs. Independent
checks confirmed guest and workspace cleanup in both runs.

Native Linux and Docker Compose runs also exercised the two generated
HAProxy routers with application SQL traffic. Each run used three disposable
Patroni guests, with routers on two separate guests. A libpq client kept the
same TLS hostname and two frontend addresses throughout the test. It wrote
through a router, connected through the remaining router during a router
outage, and rejected a mismatched SQL certificate name. A router configured
with a client certificate from the wrong CA refused SQL while the healthy
router continued serving it. After watchdog fencing, the previously open SQL
session closed. The client reconnected with the original settings, read its
committed row and wrote to the promoted primary. The original member rejoined
as a standby. Both runs passed their source and artifact checks, and
independent checks confirmed cleanup of the disposable VM processes and
workspaces.

Forgejo and Gitea also ran as native and Docker Compose services with
separately provisioned application credentials against this generated write
endpoint. Both retained their process identities and committed repository
files after a router outage and watchdog-driven PostgreSQL promotion in each
runtime. [Forge verification](FORGE_HOST.md#local-validation)

The installation suite simulates server admission, ownership, partial creation,
lost acknowledgments, UID drift, stale volumes, topology changes and endpoint
checks. It does not establish behavior of a live CNPG controller or database.
Set `POSTGRES_INSTALL_TEST_KUBECTL` to a local kubectl executable to additionally
exercise the real Secret metadata/presence Go-template with client-only secret
generation and an isolated synthetic configuration. It prints no fixture data
values and contacts no Kubernetes endpoint.

Other application services against the verified write endpoint, off-platform
WAL/base backups, restore, physical fencing and quorum-loss behavior,
controller-managed promotion, upgrade and production client reconnection and
load tests remain required. Complete-stack and physical HA acceptance remain open.
