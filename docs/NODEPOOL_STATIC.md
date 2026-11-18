# Static worker-supply configuration

Status: initial Nodepool configuration contract, not active worker supply or
an accepted multi-tenant CI environment. [Launcher packages](NODEPOOL_SERVICES.md)
now provide guarded runtime artifacts; release compatibility and real
allocation/reset/isolation tests remain open. This does not complete the
[Zuul runtime](ZUUL_SERVICES.md) or [full stack expansion](MULTI_RUNTIME_STACKS.md).

## What the adapter generates

`scripts/render_nodepool_static.py` takes a private worker plan and the same
private Zuul plan used for the service packages. It outputs `nodepool.yaml`, a
matching restricted `zuul-tenants.yaml`, and an incomplete status report.
It never scans the network, starts Nodepool, registers a worker, changes a host,
creates an account or resets a disk. Existing output directories are rejected.

The design follows the upstream [Nodepool static driver](https://zuul-ci.org/docs/nodepool/latest/static.html)
and [TLS configuration](https://zuul-ci.org/docs/nodepool/latest/configuration.html).
The [Zuul tenant reference](https://zuul-ci.org/docs/zuul/latest/tenants.html)
documents label restrictions, including their deprecation for future versions.
Pin and test a compatible release pair; matching version numbers alone are not a
compatibility rule. Do not assume that a future Zuul launcher or removed option
can be substituted without a separate adapter and acceptance tests.

## Private plans and worker identity

Copy `examples/nodepool-static.json` into ignored private storage. Replace all
version, image and key placeholders after independent approval. The public
example deliberately cannot be rendered unchanged.

Each pool declares a tenant, unique label, and at least two worker hostnames in
distinct declared failure domains. The adapter rejects duplicate hosts, root
logins, unsupported fields and malformed SSH key wire structures. It accepts
Ed25519, bounded RSA and the standard NIST ECDSA public-key formats without
comments. This is structural validation, not cryptographic validation or evidence
that a key belongs to the named machine. Confirm approved fingerprints through
an independent private channel; do not replace a failing pin with an unverified
live scan.

Failure-domain names are inventory assertions, not physical-placement proof.
Verify hypervisor, rack, storage, power and network dependencies independently.
The adapter does not register spare worker VMs or turn existing control-plane
nodes into job workers. Provision dedicated, unprivileged Linux worker accounts
and Python 3 before enabling a launcher. The initial static contract uses one
parallel job per worker and one label per pool.

ZooKeeper endpoints come from the supplied Zuul plan, rather than a second
independently editable list. TLS certificate/key/CA paths are fixed private
references under `/etc/nodepool/private`. No certificates, private keys, cloud
credentials or worker SSH private keys are included in public configuration.
The executor's build-node SSH identity remains separately provisioned.

## Tenant label restrictions

Before generating static supply, every tenant in the supplied Zuul plan must
declare a nonempty explicit `allowed_labels` list, for example:

```json
{
  "name": "platform",
  "config_branch": "main",
  "config_projects": ["platform/ci-config"],
  "untrusted_projects": ["applications/example"],
  "allowed_labels": ["platform-linux"]
}
```

This is a tenant excerpt, not a complete plan. Existing Zuul plans that omit the
field preserve their prior output; **the static-supply adapter refuses that
unrestricted mode**. Labels must belong to exactly one declared tenant and match
the supplied pool labels. The generated restrictions are anchored literal-label
expressions, not caller-supplied wildcard patterns. A pool's `platform-tenant`
attribute is informational metadata; it is not an authorization mechanism.

Deploy Zuul's tenant restrictions from this same reviewed source plan before
starting Nodepool. Generating a second YAML file does not prove the live
schedulers use it. Check the pinned release's semantics and run negative tests
where a tenant tries to request another tenant's label. Other undeclared live
tenants, config scripts or overlapping configurations must be audited separately.

## Render without registration

```bash
python3 scripts/render_nodepool_static.py \
  --plan private/nodepool-static.json \
  --zuul-plan private/zuul-config.json \
  --runtime rke2 \
  --output-dir private/generated/nodepool-static
```

Configuration-target selections also include `k3s`, `k8s`, `docker` and `raw`.
At this milestone they are not installed controllers. All allocation, worker
mutation and HA acceptance flags remain false. Do not report a valid YAML file
as successful worker registration, SSH authentication or job execution.

## Static reuse is not isolation

Nodepool's static driver does not itself build or replace a machine between
jobs. A single-job concurrency limit prevents overlapping assignments; it does
not remove persistent files, malware, modified tools, credentials or background
processes before reuse. Tenant label separation likewise does not isolate
different untrusted projects inside the same tenant.

Use static workers only in an approved trust domain with an independently tested
reset/reimage and credential-lifecycle policy. Do not feed mutually untrusted
production jobs to these hosts simply because tenant labels validate. An
ephemeral worker-provider/reset design and adversarial cross-job persistence
tests are still required for that use case. No cleanup, reimage or destructive
host operation is performed by this renderer.

## Upstream migration status

Upstream now marks Nodepool as deprecated and recommends its integrated
`zuul-launcher` system. Its [build-node documentation](https://zuul-ci.org/docs/zuul/latest/build-nodes.html)
describes a per-label transition and a Nodepool fallback during migration. The
presence of that fallback does not establish compatibility for any particular
Zuul and Nodepool release pair. Test the selected pair, ZooKeeper TLS behavior,
allocation and worker isolation before relying on this optional profile.

An integrated-launcher profile and its own allocation, isolation and recovery
acceptance are still needed for the requested end state. This deprecation does
not itself change an existing deployment or validate an automatic migration.

## Required remaining work

- Launcher runtime acceptance and real release-pair/schema validation.
- ZooKeeper connectivity, namespace/ACL consistency and private client identity.
- Real host-key mismatch tests, SSH access and executor-to-worker log streaming.
- Simultaneous allocation/release, single-worker concurrency and launcher/node
  failure recovery tests; no duplicate-assignment guarantee is claimed here.
- Physical-placement, capacity, reset/reimage, job-state and credential isolation.
- Ephemeral/provider-specific worker adapters where static reuse is unsuitable.
- Live pipeline jobs, review reporting, backups and complete-stack acceptance.

Keep real hostnames, host keys, tenant inventories, credentials and allocation
proofs in private deployment storage, never public Git.
# Experimental integrated static provider

For a Zuul 14.3.0 evaluation, `scripts/render_zuul_integrated_static.py`
accepts the same private static inventory and Zuul plans as the Nodepool
renderer, plus `--runtime` and a new `--output-dir` under `private/`. It
creates one review-only YAML fragment per tenant and a status file. The
inventory currently retains the Nodepool version and image fields even though
the fragment does not deploy Nodepool.

These files do not start a launcher or change a trusted project. The Zuul
service plan can opt in with `"integrated_launcher": {"enabled": true,
"nodepool_fallback": false, "provider_projects": {"tenant-name":
"trusted/project"}}`, selecting one distinct trusted project per tenant;
this adds the `platform-static` connection and launcher service. Deployment
still needs reviewed trusted-project placement, tenant-specific worker authentication,
and complete layout and live allocation tests. Static machines retain state
between builds; a separate cleanup and isolation design is required.
Choose `nodepool_fallback: true` when retaining an operational Nodepool
during migration; with `false`, nonexistent integrated labels are declined.

An isolated local Zuul 14.3.0 integration fixture has loaded the generated
provider from a disposable Gerrit trusted project and observed two guarded
launcher processes while exercising ZooKeeper faults. It checked the provider
API, configuration errors, source stability, and fixture process cleanup.
The local fixture now also publishes a disposable Gerrit change, observes its
job's nodeset request, and matches that request ID to an assigned static node.
The two loopback test endpoints only support SSH host-key handshakes and refuse
authentication. The fixture did not execute a job and does not prove multi-host
availability, runtime image compatibility, or production readiness.

A separate local OpenSSH fixture has verified a generated authorized key, a
rejected unapproved key and rejected wrong host pin. It used a private network
namespace with only loopback, a private mount namespace and a forced non-root
UID command. This is an authentication test; it is not yet a general-purpose
worker and has not executed a Zuul job.

A further isolated test enabled a disposable chroot with a read-only host
`/usr`, a private writable `/tmp`, and no mounted host `/mnt`, `/proc`, or
`/var/tmp`. It checked SSH and SFTP with generated keys and ran a harmless
Ansible 11 command as the non-root fixture user. This is a local test boundary,
not a hardened production worker or an end-to-end Zuul job result.

