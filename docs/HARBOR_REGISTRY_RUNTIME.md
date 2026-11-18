# Harbor registry daemon component verification

Status: an explicit native fixture runs the actual registry fork selected by
Harbor 2.15.1. Only checks in a completed, source-bound receipt establish a pass.
Full Harbor installation/HA, independent shared storage and physical host failure
remain unverified. Existing deployment profiles and live services are unchanged.

The final isolated case passed in **56.543 seconds**, with exact source hashes,
original process/cgroup cleanup and absence of private fixture values in logs
verified. This result applies to the native component scope below.

## Original service and build

The pinned [Harbor Makefile](https://github.com/goharbor/harbor/blob/v2.15.1/Makefile)
selects the `goharbor/distribution` fork at `v2.8.3-harbor.1-rc.1`, resolved to
commit `086d3c33b3981cf598d0d82336a907fcf5c4c848`. This is a separate executable
from the distribution library replacement in Harbor's `src/go.mod`.

The local build follows Harbor's registry builder selection: Go 1.25.9,
`CGO_ENABLED=0`, `include_oss include_gcs`, the original vendored dependencies,
and the version/revision/package linker values. It additionally uses `-trimpath`.
The original source has no `go.mod`; the build uses the declared GOPATH package
with modules/network module fetching disabled. All extracted source files are
hashed before/after the bounded build. Original build exit and cgroup/process
cleanup are retained privately. No upstream code is patched for the test.

| Artifact | SHA-256 |
|---|---|
| Registry fork source archive | `2651addb228cfb4a23c10822c6d8f77b51964bd5f24ea84989d7a32e66e941f3` |
| Original `vendor.conf` | `08c824e9ea5439760cd9cee99c01e1a7ecb04759c4756da959bd8a2f02914a30` |
| Go 1.25.9 Linux amd64 archive | `00859d7bd6defe8bf84d9db9e57b9a4467b2887c18cd93ae7460e713db774bc1` |
| Native registry executable | `76e40d525ad206ab6bc426bc2ee34f265fb8f097147d081cdf62a9bd0e7264a4` |

The runtime fixture requires that executable and its exact typed build receipt.
Bytes are verified before create-only staging, and the test rejects changed
source revisions, missing build tags, numeric substitutes for boolean flags,
unknown receipt fields and existing output targets. Publisher signatures and
the complete vendored dependency-license audit remain unverified.

## Actual protocol and failure cases

Two non-root registry processes use independently pinned HTTPS endpoints,
private bcrypt credentials and the same private upload-signing secret. The
fixture also checks the original process IDs/start times and empty capability
sets. They share a disposable **local filesystem directory** on one host. That
directory establishes shared-file protocol behavior, not CephFS, S3 availability
or independent host/storage HA.

The actual daemon configuration selects Redis metadata caching with a dedicated
named ACL user, database 2 and verified TLS through the
[Valkey application gateway](VALKEY_APPLICATION_GATEWAY.md). Upstream cache
mTLS, authenticated Sentinel quorum, strict idle freshness, AOF/write guards and
original identity checks remain in place. The application user can access only
blob hashes and the `fixture/ha` repository's metadata commands/keys.

The fixture checks:

- positive HTTPS access, wrong-password/anonymous refusal, untrusted CA and
  changed certificate refusal, and rejection of plaintext on the TLS listener;
- an OCI layer/config upload started and patched at replica A, completed with
  its original signed upload state at replica B, then published as a manifest;
- exact digest/body retrieval from both replicas and continued serving by the
  same original replica B after replica A's process is stopped;
- a fresh OCI image after real Valkey primary/co-located Sentinel loss and
  promotion, with the same original registry process;
- actual descriptor hashes populated in the scoped TLS cache before and after
  promotion, plus refusal of foreign keys and configuration commands;
- another fresh image after checker/cache access is stopped, with authenticated
  reads/writes still working from authoritative filesystem data;
- anonymous manifest reads and upload starts still refused during each phase.

Cache-outage serving is deliberate metadata-cache fallback. It does not mean
authentication is bypassed or cache writes continue after quorum/checker loss.
The gateway itself still refuses cache traffic. The fixture never queries a
debug endpoint that could reveal runtime configuration.

Upload responses cannot redirect credentials: only the original relative
`fixture/ha` upload path, one bounded signed state value and the locally computed
digest are accepted. Responses, requests and fixture image bodies are bounded;
no automatic cross-origin redirect is followed.

## Running the explicit test

Prepare the exact reviewed artifacts and private build receipt, then select:

```text
HARBOR_REGISTRY_TEST_RUNTIME=1
HARBOR_REGISTRY_TEST_ARTIFACTS=<PRIVATE_DIRECTORY_WITH_REGISTRY_AND_BUILD_RECEIPT>
VALKEY_TEST_RUNTIME=1
VALKEY_TEST_ARTIFACTS=<PRIVATE_REVIEWED_TLS_VALKEY_BUILD>
VALKEY_GATEWAY_HAPROXY=<PRIVATE_REVIEWED_HAPROXY_EXECUTABLE>
```

The reviewed fixture uses bcrypt 5.0.0 with a pinned native module hash only to
generate synthetic private credentials. Run on Linux in an owned fresh network
namespace under a bounded cgroup, retaining a foreground supervisor until the
original invocation is terminal:

```bash
PYTHONPATH=scripts python3 -B -m unittest \
  test_valkey_gateway.GatewayTests.test_real_registry_cross_replica_upload_and_cache_promotion
```

Normal validation runs the artifact/upload-location contract tests and skips
actual daemons unless the explicit flag is enabled. Runtime checks use only
fresh test addresses, generated credentials, temporary files and the original
owned processes; no company kubeconfig or production registry is used.

The outer fixture verifies member rejoin, quorum loss/recovery, data write guards,
absence of private values from logs and original process/data cleanup. Receipts
and logs remain ignored private artifacts. No result here establishes Harbor
core/jobservice integration, database promotion, SSO/RBAC, signed-image policy,
external storage recovery, physical HA or a complete requested stack.

The separate [actual service integration fixture](HARBOR_SERVICE_RUNTIME.md)
uses the [native service build inputs](HARBOR_SERVICE_BUILD.md) for selected
core/jobservice/database behavior. Neither component test qualifies full Harbor HA.
