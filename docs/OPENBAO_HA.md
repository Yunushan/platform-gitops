# Optional three-voter OpenBao Raft packages

This package advances the shared secrets requirement in the
[multi-runtime objective](MULTI_RUNTIME_STACKS.md). It renders a separate,
optional OpenBao 2.7.2 deployment for RKE2, K3s, existing Kubernetes, Docker
and native Linux. It does not change the existing profile, upgrade a server,
install workloads, initialize a cluster, handle production unseal shares or
certify HA. Version 2.7.2 is the tested configuration family, not an LTS claim.

The separate [workload installer](OPENBAO_INSTALLATION.md) now provides
explicit-target create-only Kubernetes installation and original-handle resume.
It stops at startup observation pending an external initialization/unseal
ceremony; it does not handle recovery shares or certify a working cluster.
The [host activation helper](OPENBAO_HOST_ACTIVATION.md) now provides the
corresponding fresh Docker/native process activation and original-handle resume.
It requires prepared private inputs and observes uninitialized TLS serving;
it does not perform a cluster ceremony or certify HA.
The separate [snapshot helper](OPENBAO_SNAPSHOTS.md) captures with a restricted
credential and verifies backup integrity without contacting the source.

The implementation follows OpenBao's
[server configuration](https://openbao.org/docs/configuration/),
[Raft storage](https://openbao.org/docs/configuration/storage/raft/),
[TLS listener](https://openbao.org/docs/configuration/listener/tcp/) and
[declarative audit device](https://openbao.org/docs/configuration/audit/)
interfaces. Keep these version-sensitive interfaces and artifact provenance
under review before selecting a production version.

## Prepare and render

Copy [the example](../examples/openbao-ha.json) into ignored private storage.
Replace its placeholder image digest and native executable checksum with
independently reviewed artifacts. Declare exactly three different member names,
Linux hosts, peer IPv4 addresses and failure domains. Metadata does not prove
that those are independent physical domains.

```bash
python3 scripts/render_openbao_ha.py \
  --plan private/openbao-ha.json --runtime rke2 \
  --output-dir private/generated/openbao-rke2
```

Use `k3s`, `k8s`, `docker` or `raw` for the other packages. Output must be a new
directory inside ignored `private/` storage. Existing output is never replaced.
The contract binds the private plan, runtime and generated artifacts by SHA-256;
it is not a signed deployment attestation. Literal credentials and unrecognized
plan fields are rejected; plans contain paths and Secret references only.

All members use Raft with explicit node IDs, HTTPS API/cluster addresses and
verified-CA retry-join candidates. API and peer ports are 8200 and 8201.
Server certificates must cover each advertised address: IP SANs for host
packages, or the generated per-member Service DNS names for Kubernetes.
No development mode, plaintext API, automatic initialization, auto-unseal
credentials or bootstrap root token is generated.

The initial seal mode is manually managed Shamir. Initialize only the designated
fresh cluster member, join followers to that cluster and unseal them with its
shares using an approved external ceremony. Keep encrypted shares, custodians,
recovery instructions and independent bootstrap material outside this platform.
PGP custody, KMS/HSM auto-unseal, recovery ceremonies and production auth-method
configuration remain separate work; rendering does not establish them.

## Runtime inputs and storage

| Runtime | Package | Prepared inputs |
|---|---|---|
| RKE2, K3s, Kubernetes | Immutable ConfigMap, three single-member Deployments and Services, disruption budget, NetworkPolicy | Existing namespace, six distinct data/audit PVCs, three TLS Secrets, reviewed hostname/zone placement |
| Docker | One host-network Compose service and JSON configuration per host | Reviewed digest, non-root UID/GID 1000, prepared member data directory, audit directory, read-only configuration and TLS binds |
| Native Linux | One systemd unit, JSON configuration and binary checksum per host | Dedicated `openbao` account, exact reviewed executable, readable TLS/configuration and writable member data/audit paths |

For Docker/native, copy the selected member's JSON to
`private_dir/member/config.json`. Docker mounts that file at
`/etc/openbao/config.json`; it does not create missing host bind sources.
Install native `member-binary.sha256` as `private_dir/member/binary.sha256`.
Native units check the executable digest before starting. These units are
templates; guarded native/Docker install/start/resume helpers are still required.

Container commands explicitly select `/usr/bin/bao server` and the generated
configuration, following the
[versioned upstream Dockerfile](https://github.com/openbao/openbao/blob/v2.7.2/Dockerfile).
No upstream development-server command or entrypoint initialization is used.

Each host's `private_dir/member/tls/` contains `tls.crt`, `tls.key` and `ca.crt`.
The service identity must be able to traverse its ancestors and read those
files. Keep private keys protected and readable only by the service identity.
For Kubernetes, each referenced Secret contains the same three keys; projected
files use mode 0440 with the dedicated Pod group. Secrets and PVCs are referenced,
not created or altered by the renderer.

Raft data must be durable and independently placed, with encrypted storage and
recovery capacity verified separately. Audit storage is separate from the Raft
claim. The same audit device name and in-service audit path are configured on
every member, as required for consistent leader transitions. Separate Linux
hosts provide their own local audit paths; a local multi-process fixture shares
one audit file and does not prove independent audit storage.

Raft uses `disable_mlock=true`; avoid swapping sensitive process memory. Native
units set `MemorySwapMax=0`; Docker declares equal memory/swap limits. Verify
actual kernel enforcement, disabled/encrypted node swap and storage encryption
before production use. Kubernetes resource limits alone do not prove this.

## Audit, network and health boundaries

The declarative `platform-local` file audit device uses HMAC output with
`log_raw=false`. Enabling that device is only one control: forward records
off-platform, monitor free space and failures, arrange rotation/reopen and test
collector loss. Do not delete audit configuration from an active cluster as
a shortcut for disk pressure. Production alerting and collection remain open.

Kubernetes uses one `Recreate` Deployment per stable Raft identity and requires
different host/zone placements. Services publish unready addresses for joining.
The budget requires two Ready members. TCP liveness keeps sealed members from
being repeatedly restarted; HTTPS readiness accepts an unsealed standby.
Kubelet HTTPS probes do not establish certificate validity. The policy admits
peer traffic, selected client/monitoring namespaces and cluster DNS; verify its
actual datapath and DNS labels on the selected CNI. No unauthenticated metrics
access or service-account API access is enabled.

## Verification and remaining work

`python3 scripts/test_openbao_ha.py` checks all five packages, disjoint storage
and identity inputs, TLS/join/audit configuration, privilege boundaries and
private create-only output. Its real API and runtime cases require explicit
opt-in; default CI does not initialize or unseal anything.

`OPENBAO_TEST_API=1` selects the existing disposable Kubernetes API fixture with
`ROOK_BOOTSTRAP_TEST_BIN`. This verifies admission/schema behavior, not real
Pod scheduling, CNI, storage, unseal or serving availability.

The real Raft case requires `OPENBAO_TEST_RUNTIME=1`, a disposable root Linux
runner, a fresh network namespace containing only an unconfigured loopback
device, OpenSSL and iproute2. `OPENBAO_TEST_ARTIFACTS` selects a directory holding
the exact pinned `openbao_2.7.2_linux_amd64.tar.gz` archive. It is available from
the [official release](https://github.com/openbao/openbao/releases/tag/v2.7.2).
The fixture performs no downloads and checks its exact size and SHA-256 before
extracting only the executable. HTTPS/release-digest checks do not establish an
independently verified publisher signature.

Run the selected unittest inside a finite resource-limited service and
`unshare --net`, as an isolated fixture rather than on a company workload host:

```bash
python3 -B scripts/test_openbao_ha.py \
  OpenBaoTests.test_real_raft_tls_restricted_access_audit_and_leader_recovery
```

The fixture configures only its own namespace, uses three actual unprivileged
OpenBao processes with the generated native configuration and private PKI,
and keeps synthetic initialization shares and tokens in memory. It checks
three voting members, restricted read access and denied writes/admin access,
initial root-token revocation, audit HMACs, leader-process loss, original-data
restart/unseal, guarded snapshot capture and fresh-target restore after stopping
every source process. It terminates only its retained child
processes and checks its unique root-owned directory before cleanup.
The same fixture exercises the production
[read-only runtime verifier](OPENBAO_RUNTIME.md) with separate observer and
restricted canary credentials, including reads and denials through every member.
`OPENBAO_TEST_REPORT` may select a new private JSON receipt with aggregate flags,
artifact/source commitments and no secret values. This is single-host evidence;
snapshot capture alone does not prove restore. Complete live deployment, production key
custody, application auth integration, physical-node failure, upgrades,
off-platform restore and complete-stack acceptance remain unverified.

The local three-process case has passed with the pinned release, verified TLS
and explicit rejection of an untrusted CA. The tested restricted tokens have
independent parentage and exact policies, so revoking the initial root does not
also revoke those identities. Readiness waits for the newly replicated KV mount
to finish its versioned-backend initialization; other HTTP errors still fail
the fixture. Real Kubernetes API admission/read-back has also passed. This is
not evidence of container startup, kubelet scheduling or physical-node HA.
