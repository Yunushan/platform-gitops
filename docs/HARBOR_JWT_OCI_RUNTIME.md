# Harbor bearer-token OCI workflow component

**Validation update:** the shared audit-job fixture now requests an explicit
`pull` event type so its dry run must execute SQL. The stronger
[jobservice continuity case](HARBOR_JOBSERVICE_REPLICA_RUNTIME.md#observed-database-continuity-failure)
failed on the worker's first database query after handover (`unexpected EOF`).
Earlier dry-run receipts used an empty operation filter and do not prove this
SQL-backed job-continuity requirement. Full acceptance remains pending; keep the
original process, TLS, quorum and route-fencing checks in place.

Status: the optional local integration case passed in **103.084 seconds**
(one test, zero skips). Its supervisor verified original source stability and
process/cgroup cleanup in 105.664 seconds. It does not qualify a full Harbor
deployment or a complete stack/runtime combination.

The fixture runs the unchanged pinned Harbor 2.15.1 core, jobservice and
registry-controller binaries, the selected registry fork, real TLS PostgreSQL
replicas and the TLS Valkey gateway. It composes the existing registry protocol
and [database-promotion service tests](HARBOR_SERVICE_RUNTIME.md).

## Actual application path

The pinned core [registry proxy](https://github.com/goharbor/harbor/blob/v2.15.1/src/server/registry/proxy.go)
verifies the client's bearer authorization at core, then uses private internal
Basic credentials toward the backend registry. This test exercises that actual
path over verified HTTPS. It does not replace the backend with a mock token
handler or modify Harbor's authentication implementation.

Core's resumable-upload accounting uses its separate `_REDIS_URL_REG` client.
The fixture wires that URL to logical database 2 through the verified TLS
gateway, with the existing finite core application ACL. The backend registry's
restricted account is unchanged. Omitting this source-required URL caused a
real upload PATCH failure in an earlier private run; that result was rejected.
The fixture's all-key core ACL remains an isolated test policy, as documented
by the service fixture, and does not qualify production tenant isolation.

The accepted run checks all of the following in the initial phase,
after cache promotion, and after the controlled PostgreSQL promotion:

- Core advertises the expected bearer challenge at its original HTTPS origin.
  Token requests use only that fixed origin, the `harbor-registry` service and
  reviewed synthetic project/user names. Redirects are not followed.
- Core issues real RSA-signed tokens for an administrator and a synthetic
  project guest. Issued token identity/service/access claims are inspected;
  acceptance of the signatures and permissions is tested by actual core APIs.
- OCI configuration/layer blobs and a manifest are pushed through core. The
  manifest and blobs are read back byte-for-byte with matching SHA-256 digests.
  The guest can pull but cannot start an upload or replace the manifest.
- Anonymous reads/writes, a token for another project, a tampered signature,
  a signed wrong-audience token and a signed expired token are refused. The
  audience/expiry negatives are signed using the fixture's own ephemeral core
  signing key; they are not merely malformed signatures. Backend-only Basic
  credentials must not authenticate a public core request.
- Wrong credentials fail the login-only token request. Harbor may return a
  signed anonymous token for a scoped request; that token must have no subject
  and empty permissions for the private project, and must fail real access.
  An empty optional `sub` claim may be omitted by the JWT serializer.
- Core's artifact API reports the original digest and tag, and real PostgreSQL
  artifact/repository rows must match. Direct backend uploads alone cannot
  satisfy this metadata requirement.
- A previously issued guest token and its image remain usable after both
  promotions. The existing fixture still requires original application PIDs,
  distinct real dry-run jobs, strict PostgreSQL TLS/quorum verification and
  private-log/process/data cleanup.

Authorization coverage is project scoped, matching the selected Harbor role
model. It does not establish per-repository delegated credentials, role-change
revocation or a complete SSO/MFA/RBAC matrix. Token authentication signatures
also do not establish image-signature admission or publisher provenance.

## Run explicitly

Prepare the same private artifacts/environment described by the
[service fixture](HARBOR_SERVICE_RUNTIME.md#run-explicitly), then enable:

```text
HARBOR_JWT_TEST_RUNTIME=1
POSTGRES_PROMOTION_TEST_RUNTIME=1
```

Inside the owned fresh network namespace and bounded cgroup, retain a foreground
supervisor through the original process's terminal receipt and cleanup:

```bash
PYTHONPATH=scripts python3 -B -m unittest \
  test_valkey_gateway.GatewayTests.test_real_harbor_jwt_oci_flow_after_cache_and_database_promotions
```

All passwords, tokens, signing inputs/keys, temporary SQL/storage, service
configuration, logs and execution receipts stay in private storage. Upload
locations are accepted only on the original core origin and original fixture
repository with its signed resumable state. Ordinary validation skips service
execution unless explicitly selected.

The separate [two-core peer-loss case](HARBOR_CORE_REPLICA_RUNTIME.md) also
passes direct access to both original peers, verified HTTPS health routing,
SIGKILL of one core and fresh work through its original survivor before cache
and route-fenced database promotion. This is process-level component evidence;
independent host, routing and storage HA remain open.

## Remaining acceptance

This is an HTTP OCI protocol/client component on local native processes.
Docker CLI credential handling, Kubernetes image pulls, independent core hosts,
independent Ceph/S3 storage, full SSO/MFA and authorization coverage, scanner and
signed-image admission, automated database/cache failover, controller/watchdog
integration, upgrade/restore, off-platform recovery, physical failure domains
and load still need their own evidence. Generic application/HA/full-stack gates
remain false.
