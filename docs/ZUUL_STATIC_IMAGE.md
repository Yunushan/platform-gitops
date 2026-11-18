# Optional trusted Zuul static-image publication

This package prepares a final, protected post-review job that builds a static
web image from a fresh authenticated Gerrit branch checkout. It uses the existing
Harbor robot publisher and executes no repository-supplied build commands.
It runs in the trusted executor context with an empty nodeset. Application build
workers receive no publication inputs from this package.

The package supports the five Zuul hosting selections: RKE2, K3s, existing
Kubernetes, Docker and native Linux. It prepares trusted configuration files;
it does not publish configuration, create a robot, trigger a job, sign an image
or change a deployment. To deploy the published digest through Argo CD or Flux,
use the [Zuul image promotion command](ZUUL_GITOPS_HANDOFF.md#promote-a-published-image).

## Prepare the private package

Copy [the example](../examples/zuul-static-image.json) under ignored `private/`.
Select one application project, branch and dedicated trusted configuration
project from the existing Zuul tenant plan. The configuration project must not
be loaded by another tenant. Enable the reviewed executor explicitly. The source
HTTPS origin must match its Gerrit connection, including the port. Choose
pipeline/job names that do not collide with existing definitions.

Provide the original [restricted Harbor robot](HARBOR_CI_ROBOT.md) metadata, its
credential-file SHA-256, registry CA/leaf pins, and a reviewed base-manifest digest
in the selected repository. The sorted, bounded file list must include
`index.html`. `static_directory` selects a regular subtree of the exact checkout.
The publisher refuses symlinks and files escaping the selected subtree.

Record the current trusted checkout program's SHA-256 in
`source.checkout_program_sha256`. A changed helper requires regeneration and
review. Witness schema 1 remains restricted to Gerrit patch-set refs. Schema 2
selects a `refs/heads/…` branch and verifies its fetched head against the nonzero
Zuul event revision before writing a witness. A moved branch fails the job.

```bash
python3 scripts/render_zuul_static_image.py \
  --plan private/zuul-static-image.json \
  --zuul-plan private/zuul.json --runtime rke2 \
  --output-dir private/zuul-static-image
```

Review `zuul.yaml`, the playbook, bundled programs and render contract. Only then
put the files into the declared private trusted configuration project. Rendering
performs no Git write and supplies no existing pipeline or secret definition.

## Secret custody and execution

Define the selected encrypted Zuul secret in the trusted configuration project.
Its data has four string values:

- `source_ca`: the reviewed Gerrit CA PEM, with its original line endings.
- `source_token`: a scoped read-only Git credential for `source.read_username`.
- `registry_ca`: the reviewed registry CA PEM.
- `registry_credential`: the exact private robot credential JSON produced by its
  provisioning helper, matching the reviewed SHA-256.

Keep keys, decrypted values and generated private configuration out of the public
project. Use the deployed Zuul release's encryption procedure. The generated job
is post-review only, has no parent, is final/protected, allows only the selected
project, and refuses implicit retries. Branch access controls and review of the
trusted configuration remain the operator's responsibility.

The playbook passes credentials through bounded standard input with Ansible
`no_log`; credentials never enter command arguments. The non-root Python helper
owns ephemeral directories, creates private files atomically and removes them on
exit. No caller-selected Ansible cleanup path is used. Program/contract hashes,
source trust, robot identity, permissions, expiry and registry TLS are checked.
Before handling the secret, a separate `--check-contract` command verifies only
the public bundle. It performs no checkout or publication. A failed publication
stops the job and reports a fixed stage inferred from the exit code. Zuul can
discard both output streams from the secret-bearing command. The helper writes
only the validated public receipt to a fresh owner-only file inside the current
build's work directory. A separate task reads and revalidates that receipt
without credentials before passing it to `zuul_return`.

The helper retains a checkout witness outside the source tree, builds the static
files into an OCI layer, publishes by digest and reads the manifest back. It
returns artifact metadata bound to the Zuul build UUID and publisher contract.
The receipt is not a signature or independent release authorization. Signing,
Kyverno admission, deployment-image binding, live workload verification, failover
and recovery acceptance still apply. Rendering and unit tests do not prove that
a real Zuul job has published or deployed an image.

## Local verification

The generated job and Gerrit trigger passed the exact cached Zuul 14.3.0 parser
schemas for all five hosting selections. A separate non-root Linux case used
real Git over authenticated HTTPS: it fetched the exact branch revision,
refused a moved head, and refused a wrong credential without writing a witness.

The native publication case also passed against the separately reviewed
[Harbor pool-health candidate](HARBOR_DB_POOL_HEALTH.md). It invokes the bundled
publisher CLI as a non-root process, delivers its four inputs through standard
input, publishes through real Harbor core/registry services, verifies the receipt
binding and reads the exact Git-sourced file from the resulting OCI layer.
Original robot permissions, peer-loss, PostgreSQL/Valkey promotion, private-log
checks and owned-process cleanup remain part of the shared native fixture.
The Git server in this case is a generated authenticated TLS Git HTTP backend.
It is not an actual Gerrit/Zuul event-to-job run or a production Harbor deployment.

Select `ZUUL_STATIC_GIT_TEST=1` for the TLS Git case in
[`test_zuul_static_image.py`](../scripts/test_zuul_static_image.py). Select
`ZUUL_STATIC_HARBOR_TEST_RUNTIME=1` and the documented native Harbor candidate
artifact environment for its publication case, inside an owned bounded fresh
Linux network namespace. That case also needs `K3S_VM_TEST_ARTIFACTS` to read
the pinned BusyBox base from the cached image archive; it starts no Kubernetes
cluster. Default validation skips these explicit runtime cases.

The additional `ZUUL_STATIC_JOB_TEST_RUNTIME=1` case selects
`PublicationTests.test_native_encrypted_zuul_harbor_job`. It requires the pinned
native Gerrit, ZooKeeper, PostgreSQL, executor SDK, worker and Harbor fixture
artifacts. Its parent must be an original bounded `platform-zookeeper-runtime-…`
systemd unit inside a fresh network namespace. Give `ZUUL_WEB_TEST_REPORT`,
`ZOOKEEPER_TEST_REPORT`, `VALKEY_TEST_REPORT` and `ZUUL_STATIC_JOB_DIAGNOSTIC`
fresh paths under ignored `private/validation/`. This case uses Zuul's actual
project public key, stores only encrypted publisher inputs in trusted Git, and
requires a successful final voting buildset plus persisted artifact metadata and
the exact Harbor layer bytes. The fixture retains scrubbed job diagnostics in
the private diagnostic file and removes its job directories and original
processes on exit. Selecting the test or starting its services is not a pass.

The isolated native Linux case verified actual secret decryption and delivery,
one final voting Zuul build for a Gerrit ref update, the persisted artifact
metadata, and an independent digest readback of the exact reviewed static file
from Harbor. It uses the reviewed local Harbor pool-health candidate. The
combined rollout fixture also verified live digest pulls through Argo CD and
Flux on two disposable K3s workers. Signed admission, physical failure testing,
and full stack qualification remain open. [Rollout fixture](ZUUL_GITOPS_HANDOFF.md#promote-a-published-image)
