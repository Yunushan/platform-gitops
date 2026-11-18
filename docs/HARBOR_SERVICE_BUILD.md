# Pinned Harbor native service build preparation

Status: Harbor 2.15.1 core, jobservice and registry-controller executables have
been built locally from the pinned source. A separate optional
[actual service integration fixture](HARBOR_SERVICE_RUNTIME.md) exercises their
startup and selected API/job behavior. These builds are not an installer or
proof of Harbor HA. Existing deployment profiles remain unchanged.

## Source and required generation

The selected source revision is
`335c05f2b9507b4c7116fe69b84e31ef9f4b91dd`; its source archive SHA-256 is
`eb2b81020144653c50adb58894d831268dd934b0a38e376281e12548b276dfd0`.

An initial direct core build failed because the release source archive omits
generated API packages. The original
[`gen_apis` target](https://github.com/goharbor/harbor/blob/v2.15.1/Makefile)
selects Go Swagger **v0.33.1**, Harbor's templates and additional initialisms
`CVE`, `GC`, `OIDC`. Running that generation produced **970 files** under
`src/server/v2.0/models` and `src/server/v2.0/restapi`. Original source files,
schema/templates and module locks stayed unchanged. Generated files and all
build receipts remain in private validation storage.

The successful build used the checksum-verified Go **1.25.9** toolchain,
`CGO_ENABLED=0`, `-mod=readonly`, `-trimpath`, `-buildvcs=false`, and the upstream
core linker fields `pkg/version.GitCommit=335c05f2` and
`pkg/version.ReleaseVersion=v2.15.1`. The generator was installed at its explicit
version into private tool storage; its module build information is retained.
Compilation completed in a bounded cgroup with an explicit exit receipt for
generation, core and jobservice, followed by original process/cgroup cleanup.

| Measured artifact/input | SHA-256 |
|---|---|
| Go 1.25.9 Linux amd64 archive | `00859d7bd6defe8bf84d9db9e57b9a4467b2887c18cd93ae7460e713db774bc1` |
| Go Swagger executable | `d0fecb8bd31b4bf62f3b94a84d82cb739abee7f8cf867d57375bdf79faca4de8` |
| Original `api/v2.0/swagger.yaml` | `9be2b505d4c6baba40e6d646eac2b0aa4ff11f938727ae1eaa491ca01ebf71c7` |
| Original `src/go.mod` | `2452db301573926edd007e67acdafb0178dcdbe2af7dc030e5d60d6addedf7c4` |
| Original `src/go.sum` | `11a39a7e13d325dbb236653c7758b0846a76d9e13085d7e19229524fb18cd7bd` |
| Native `harbor-core` | `e8f62f7c58209e192c5bbcbc8f5fcf33f1e0815fab7cf75a9f2ac119ec81812f` |
| Native `harbor-jobservice` | `9890f53be225edc131c1e86c383ddd7210f7f96eef92939c57fa1fdfe6e7106e` |
| Native `harbor-registryctl` | `42ab076f5c8da5f5ce1fd8d9f88585c3d6355a28a69ffcda534207c010a1bad1` |

These are measured local artifacts, not official release-container signatures
or a claim of independently reproduced builds. Publisher signatures and the full
dependency-license audit remain unverified.

## Repeating the build steps

Prepare a fresh private source extraction, the reviewed toolchain, private Go
module/build caches and output/tool directories. `GO_TOOL`, `SWAGGER_TOOL`,
`HARBOR_SOURCE` and `PRIVATE_BUILD_OUTPUT` below refer to those reviewed absolute
paths. Put the reviewed Go tool directory on the private build's `PATH`.
Install the pinned generator with the reviewed Go toolchain into private
`GOBIN` storage, then use Harbor's original generation options:

```bash
GOTOOLCHAIN=local CGO_ENABLED=0 "$GO_TOOL" install -trimpath -buildvcs=false \
  github.com/go-swagger/go-swagger/cmd/swagger@v0.33.1

"$SWAGGER_TOOL" generate server \
  --template-dir="$HARBOR_SOURCE/tools/swagger/templates" \
  --exclude-main --additional-initialism=CVE \
  --additional-initialism=GC --additional-initialism=OIDC \
  -f "$HARBOR_SOURCE/api/v2.0/swagger.yaml" -A harbor \
  --target "$HARBOR_SOURCE/src/server/v2.0"

cd "$HARBOR_SOURCE/src"
harbor_linker_flags='-s -w -X github.com/goharbor/harbor/src/pkg/version.GitCommit=335c05f2 -X github.com/goharbor/harbor/src/pkg/version.ReleaseVersion=v2.15.1'
for harbor_component in core jobservice registryctl; do
  GOTOOLCHAIN=local CGO_ENABLED=0 "$GO_TOOL" build -mod=readonly -trimpath -buildvcs=false \
    -ldflags "$harbor_linker_flags" \
    -o "$PRIVATE_BUILD_OUTPUT/harbor-$harbor_component" "./$harbor_component"
done
```

Retain exact tool/source/template/generated-file hashes, the original build
invocation and explicit exit receipts. Preserve the original module locks.
Accept a build only after the original unit/process/cgroup is terminal and its
cleanup and source comparison are verified. The commands above describe the
build inputs; the local validation additionally retained a foreground supervisor
and enforced time, memory and task bounds.

A separate [offline pool-health candidate builder](HARBOR_DB_POOL_HEALTH.md#rebuild-from-reviewed-offline-inputs)
now verifies fresh archives, complete regenerated source identity and exact
patched binaries under retained systemd supervision. It is an explicit local
experiment; it does not change these unmodified-release builds or deployment
profiles.

## Next runtime gates

The [service integration fixture](HARBOR_SERVICE_RUNTIME.md) uses these artifacts
for actual startup, TLS database/cache authentication, release SQL migration,
authenticated project APIs and real dry-run job execution across cache
promotion. Database promotion, mail, SSO/RBAC, service replicas,
upgrade/restore, network policy and physical failure/load checks still need
their own evidence. The separately verified
[registry daemon component](HARBOR_REGISTRY_RUNTIME.md) and
[cache constructors](HARBOR_CACHE_CLIENTS.md) do not qualify the entire Harbor deployment.
