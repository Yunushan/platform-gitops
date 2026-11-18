# Harbor cache-client component verification

Status: the exact Harbor 2.15.1 cache and legacy Redis-pool constructors have
passed an isolated native test through the optional
[TLS Valkey application gateway](VALKEY_APPLICATION_GATEWAY.md).
This is constructor component evidence. Full Harbor core/jobservice/registry
integration, chart deployment, worker networking, independent gateway hosts and
full Harbor HA remain unverified.
No existing Harbor profile or live configuration is changed by this fixture.

A separate [real registry daemon fixture](HARBOR_REGISTRY_RUNTIME.md) now covers
the fork's OCI protocol, peer process loss and cache promotion. Its shared local
test directory does not establish independent storage or full Harbor HA.

The [actual service integration fixture](HARBOR_SERVICE_RUNTIME.md) separately
checks core/jobservice startup, release SQL migrations, authenticated project
APIs and real dry-run jobs across cache promotion. These are distinct component
results; full Harbor compatibility and HA remain open.

## Exact clients and artifacts

The fixture imports the unchanged Harbor
[`lib/cache/redis.New`](https://github.com/goharbor/harbor/blob/v2.15.1/src/lib/cache/redis/redis.go)
and [`lib/redis.GetRedisPool`](https://github.com/goharbor/harbor/blob/v2.15.1/src/lib/redis/pool.go)
constructors. It does not substitute a Python Redis implementation or modify
those packages to add TLS support. The pinned source revision is
`335c05f2b9507b4c7116fe69b84e31ef9f4b91dd`.

The build uses the original
[`src/go.mod`](https://github.com/goharbor/harbor/blob/v2.15.1/src/go.mod)
and `go.sum` with `-mod=readonly`, Go 1.25.9, `CGO_ENABLED=0`,
`-trimpath` and `-buildvcs=false`. Its dependencies include go-redis 9.17.2
and the upstream module's Redigo replacement at 1.9.2. Source/toolchain archives
are retained privately; the toolchain download is checked against the official
Go checksum and selected Harbor source blobs against the pinned Git tree.
Publisher signatures and the full dependency-license audit remain unverified.

| Artifact | SHA-256 |
|---|---|
| Go 1.25.9 Linux amd64 archive | `00859d7bd6defe8bf84d9db9e57b9a4467b2887c18cd93ae7460e713db774bc1` |
| Harbor revision source archive | `eb2b81020144653c50adb58894d831268dd934b0a38e376281e12548b276dfd0` |
| First-party Go fixture source | `dd26aa8b61ad21cce6d952dd0acb7a9992b786faa8144078f87be16b3cd40c13` |
| Client fixture executable | `aa6ba76c88925d15f7f116e0da49a89511c8f60bc989927bf3b7940d86144a6a` |

The first-party entry point is
[`scripts/fixtures/harbor-cache-client/main.go`](../scripts/fixtures/harbor-cache-client/main.go).
To reproduce the binary, stage that file as
`src/cmd/platform-cache-client/main.go` inside the reviewed source archive, then
build from `src` with the exact toolchain and options above. Keep the original
module locks unchanged. A matching executable alone does not replace the
separate build receipt: the test also requires the bounded `build-inputs.json`
manifest, exact source hashes and explicit successful original-build cleanup.
Receipt fields retain their exact JSON types, and duplicate/extra fields are
refused. Executable bytes are checked again before create-only staging; an
input swap cannot select an unverified executable or overwrite a staged target.
Do not create an acceptance receipt merely because a build was attempted.

## Live test scope

The test starts three real Valkey 8.1.11 servers and three authenticated
Sentinels in a fresh network namespace. The gateway retains verified upstream
mTLS. Applications use standard verified TLS and authenticate as a dedicated
synthetic `harbor_component` user. The fixture ACL permits only the commands
under test, on `harbor-component:*` keys. It is **not a production Harbor ACL**;
it does not cover job queues, notifications, registry metadata or all core paths.

One original non-root Go process creates both client objects once. The test:

- checks the cache interface's save, fetch, contains and delete operations;
- checks the legacy pool's ping, expiring set, get and delete operations;
- verifies that database 0 and database 1 do not expose the same witness key;
- requires both clients to reject foreign keys and configuration access;
- uses separate client processes to reject wrong credentials and an unrelated CA;
- stops one forwarder and the original primary/co-located Sentinel;
- verifies successful writes through the same surviving forwarder and the same
  original Go process/client objects after real quorum-driven promotion;
- stops the checker and requires both clients to refuse further operations.

Only the forwarders' read-only readiness addresses are relocated so they can
share this disposable network namespace. One checker serves both forwarders in
the fixture. No physical or independently hosted gateway failover is established.
The outer cache fixture additionally checks member rejoin, quorum loss/recovery,
write guards, bounded process cleanup and absence of private fixture values in
logs. Original build/runtime invocation handles are observed until terminal.

A final rerun exposed an idle-freshness problem before the client stage: the
server's default ten-second replication heartbeat exceeded the fixture's strict
one-second freshness policy. The optional cache renderer now sets a one-second
heartbeat explicitly. The fixture samples replica freshness through a quiet
interval longer than the policy; it does not increase the allowed lag or add
data writes to disguise the idle case. The failed run remains a failed receipt.

The final source-bound constructor case passed in **56.119 seconds**. Its receipts and
raw logs are private. The outcome supports these two constructors through the
gateway; it does not prove the entire applications using them. In particular,
the chart's registry Redis configuration and Harbor's job execution must still
be tested with their actual services. The gateway does not change asynchronous
replication into lossless storage, and logical database numbers are not an ACL
security boundary.

The registry is built from a separate Harbor distribution fork selected by
the pinned [Harbor Makefile](https://github.com/goharbor/harbor/blob/v2.15.1/Makefile).
The distribution library replacement in `src/go.mod` does not qualify that
registry executable; its exact source and runtime need independent verification.

## Repeating the isolated case

After preparing the reviewed native artifacts and exact build receipt, select:

```text
HARBOR_CACHE_TEST_RUNTIME=1
HARBOR_CACHE_TEST_ARTIFACTS=<PRIVATE_DIRECTORY_WITH_CLIENT_AND_BUILD_RECEIPT>
VALKEY_TEST_RUNTIME=1
VALKEY_TEST_ARTIFACTS=<PRIVATE_DIRECTORY_WITH_REVIEWED_TLS_VALKEY_BUILD>
VALKEY_GATEWAY_HAPROXY=<PRIVATE_REVIEWED_HAPROXY_EXECUTABLE>
```

Run the following explicit test in an owned fresh Linux network namespace under
a bounded systemd cgroup, with a foreground supervisor retained through exit:

```bash
PYTHONPATH=scripts python3 -B -m unittest \
  test_valkey_gateway.GatewayTests.test_real_harbor_cache_clients_authenticate_and_reconnect_after_promotion
```

The test never uses a company kubeconfig, a running Harbor instance or production
credentials. Ordinary repository validation discovers the case but skips it
unless its explicit runtime flag is enabled. All full-application/HA readiness
flags remain false.
