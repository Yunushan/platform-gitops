# Opt-in Zuul container executors

Status: executor packaging and startup guards, not live isolation/CI acceptance.
These artifacts extend the [Zuul service packages](ZUUL_SERVICES.md). All
[full-stack requirements](MULTI_RUNTIME_STACKS.md) still apply.

## Explicit selection

Core-only behavior is unchanged when the optional `executor` object is absent
or is exactly `{"enabled": false}`. To select container executors, add the
following object to the **private** Zuul plan, replacing the image placeholder:

```json
{
  "executor": {
    "enabled": true,
    "image": "registry.example.com/platform/zuul-executor@sha256:<REVIEWED_EXECUTOR_IMAGE_DIGEST>",
    "replicas": 2,
    "kubernetes_seccomp": "runtime-default",
    "kubernetes_apparmor": "runtime-default",
    "docker_seccomp": "runtime-default",
    "docker_apparmor": "runtime-default"
  }
}
```

This is a plan fragment, not a standalone plan. The image must contain the
reviewed Zuul version, Python/helper prerequisites, bubblewrap and compatible
**preinstalled** Ansible environments. Merely pinning a digest does not prove
any of those properties. The startup wrapper verifies the version and runs
local Ansible validation plus its namespace probe before executing Zuul.

Use the existing service-render command. It produces a separate
`zuul-executors.yaml` or `zuul-executors-compose.yaml` alongside the core bundle.
Nothing is applied, pulled or started by rendering. Runtime-default is a
selection of existing runtime policy, **not a promise that user namespaces are
permitted**. Expect startup to fail safely on incompatible hosts.

## Host policy is not bypassed

The packages keep non-root execution, dropped capabilities, no-new-privileges
and a read-only root filesystem. They do not emit privileged mode, host PID/IPC
or network sharing, a host Docker socket, or an Unconfined policy. They do not
change kernel sysctls, install host profiles, relabel nodes or weaken admission.

An operator may reference a **separately reviewed and provisioned** local policy
using these constrained, versioned name forms:

- Kubernetes seccomp: `zuul/zuul-executor-<64-lowercase-hex>.json`.
- Kubernetes AppArmor: `zuul-executor-<64-lowercase-hex>`.
- Docker seccomp: `/etc/docker/seccomp/zuul-executor-<64-lowercase-hex>.json`.
- Docker AppArmor: `zuul-executor-<64-lowercase-hex>`.

The hexadecimal suffix is a naming convention, **not content verification**.
Verify actual deployed profile bytes, ownership, scope and effective kernel
policy in the private host inventory. The renderer references profiles; it
does not generate, load or approve them. Runtime-default leaves AppArmor/SELinux
selection to the host/runtime rather than assuming one Linux security system
exists everywhere. Explicit local AppArmor references require compatible hosts
and Kubernetes APIs (use Kubernetes 1.31+ for the stable security-context field).

Review the platform's
[Kubernetes security-context rules](https://kubernetes.io/docs/tasks/configure-pod-container/security-context/)
and [Docker seccomp behavior](https://docs.docker.com/engine/security/seccomp/).
Do not remove confinement to work around a failed probe. Select a host/runtime
policy that has been reviewed for the exact executor workload, then test the
real trusted/untrusted playbook boundaries. The small generic startup probe
alone cannot establish that broader security property.

## Kubernetes placement, inputs and log access

Executors require nodes labelled `platform.gitops/ci-executor=true` and tolerate
the corresponding `NoSchedule` taint. Affinity excludes both standard
control-plane/master labels, and anti-affinity requires separate nodes per
replica. A cluster with only control-plane nodes will leave executors Pending;
this is not silently downgraded to co-location. Provision dedicated approved
worker capacity, filesystem headroom and policies before starting executors.

The replicas have independent ephemeral Git/build state, so Git caches and job
directories remain on the same per-Pod filesystem. Pod/process resource limits
are initial constraints, not validated production sizing. Review cache rebuild,
job recovery and artifact/log durability under node loss.

Provision `zuul-executor-runtime` with the same three environment keys described
in the service guide, and `zuul-executor-files` with the seven common private
files **plus** `build-ssh.key`. No Secret content is rendered. Executors get no
Kubernetes service-account token or host filesystem mount.

The advertised executor address is its Pod IP, provided by the downward API.
Zuul web must reach that address on TCP 7900 for log streaming. A generated
ingress NetworkPolicy allows that port only from Zuul web-labelled Pods in the
same namespace; it does not expose an external Service or public listener.
Kubernetes allow policies are additive: inspect overlapping policies and run
allowed/denied traffic tests. This rule cannot revoke a broader allow elsewhere.
Namespace RBAC must prevent untrusted users from impersonating trusted labels.
The CNI must enforce NetworkPolicy. This ingress rule does **not** establish a
complete egress policy, transport encryption or job/worker isolation; those
foundation/integration gates remain open.

## Docker placement and routing

The generated services are `zuul-executor-1`, `zuul-executor-2`, etc., with
separate `private/<service>/` and `state/<service>/` bind sources. Sources must
already exist and have suitable UID/GID 1000 ownership and private permissions.
No automatic bind-source creation or ownership reset is performed.

Use both core and executor files in the same reviewed Compose project. To parse
without starting anything, with a synthetic or privately provisioned environment:

```bash
docker compose -f zuul-compose.yaml -f zuul-executors-compose.yaml config --quiet
```

This does not contact Docker's engine or launch jobs. Running the project is a
separate explicit action. Executor log ports are exposed only inside the Docker
network, not published on host interfaces. Internal service names are the default
advertised addresses. For a deliberately routed cross-host design, set private
`ZUUL_EXECUTOR_HOSTNAME_1`, `_2`, etc. to reachable DNS names or IP addresses and
verify connectivity from web before acceptance. Advertising a name does not
create routing, DNS or TLS.

Multiple containers on one Docker host are not multi-host HA. Separate hosts,
worker supply, resilient coordination services and log routing need explicit
orchestration and failure tests. Local seccomp files are read by the Docker
client, so provision them where the command actually runs; AppArmor policy must
be loaded on the daemon host. No host policies were installed by these tools.

## Native executor update

Native units still use the same mandatory guard. Their private environment now
also requires `ZUUL_EXECUTOR_HOSTNAME` so web receives an explicit reachable
log-stream address rather than an accidental local hostname. The local-input
checker validates address shape only; it does not test reachability.

## Remaining acceptance

No live executor, job or host-policy modification was performed in the fixture
tests. Version/profile naming, YAML structure, Compose parsing and simulated
guard failures are not proof of working isolation. Complete real sandbox,
malicious-job, log-stream, resource, node-loss, restore and whole-stack tests,
then verify ZooKeeper, databases, node supply, pipelines, identities and event
recovery. Generated CI/HA acceptance flags remain false throughout this phase.
