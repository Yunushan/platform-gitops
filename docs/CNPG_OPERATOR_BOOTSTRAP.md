# Optional fresh CloudNativePG operator bootstrap

This helper creates a fresh, explicitly scoped operator for RKE2, K3s or an
existing Kubernetes cluster. It is separate from the default installation path.
It does not adopt or upgrade an existing operator, install PostgreSQL, or establish
database durability or HA acceptance. Docker/native hosts use the
[Patroni path](POSTGRES_HA.md) instead.

## Prepare the target and reviewed release

Copy [the example plan](../examples/cnpg-operator.json) under ignored `private/`.
Replace every example UUID with the original live identity. Preprovision a
`cnpg-system` namespace with restricted Pod Security admission. Declare at least
two ready, schedulable Linux nodes spanning distinct zone/rack labels, and their
original Node UIDs. Declare finite CPU/memory requests and limits. Physical
independence of those domains still needs verification.

The reviewed release is CloudNativePG **1.30.1**, on Kubernetes **1.34–1.36**.
See the upstream [supported releases](https://cloudnative-pg.io/docs/1.30/supported_releases/)
and [installation documentation](https://cloudnative-pg.io/docs/1.30/installation_upgrade/).
Obtain the official
[release manifest](https://github.com/cloudnative-pg/cloudnative-pg/releases/download/v1.30.1/cnpg-1.30.1.yaml)
independently and retain it privately. The helper accepts only this exact artifact:

```text
manifest SHA-256: 37237f145d8138256ea25ae830f87759255665ff08f8d552fdd8224a5ec032fb
operator image: ghcr.io/cloudnative-pg/cloudnative-pg@sha256:b0f9805bd5f98592e7320b5ea624dd10a7a6ef8e477df5650655c94f0b242497
```

These pins establish the reviewed input identity; they do not verify a publisher
signature. New releases need separate provenance, schema and compatibility review.
The helper performs no download or automatic version selection.

## Review, then create once

Set the environment variable named by `target.kubeconfig_env` to the explicit
management kubeconfig path. Use static credentials and verified HTTPS. Review
the generated runtime RBAC: the upstream manager needs cluster-wide access.
Convenience editor/viewer roles are omitted; its runtime role is preserved.
Permit control-plane access to the webhook Service and operator port 9443.
Do not reuse existing operator resources, optional controller configuration,
or preexisting operator certificate Secrets.

```bash
python3 scripts/render_cnpg_operator.py \
  --plan private/cnpg-operator.json --release private/cnpg-1.30.1.yaml \
  --runtime rke2 --output-dir private/cnpg-operator-rendered

python3 scripts/deploy_cnpg_operator.py \
  --plan private/cnpg-operator.json --release private/cnpg-1.30.1.yaml \
  --runtime rke2

python3 scripts/deploy_cnpg_operator.py \
  --plan private/cnpg-operator.json --release private/cnpg-1.30.1.yaml \
  --runtime rke2 --check-server --output-dir private/cnpg-operator-check
```

The first deployment command checks local input only. The explicit server check
reads the pinned target and dry-runs all twenty resources. Review the retained
admitted bodies, then copy its returned `admitted_sha256` into the private plan.
Use a new operation output directory for creation:

```bash
python3 scripts/deploy_cnpg_operator.py \
  --plan private/cnpg-operator.json --release private/cnpg-1.30.1.yaml \
  --runtime rke2 --create --output-dir private/cnpg-operator-create
```

Two operator candidates use leader election, required domain anti-affinity, a
one-pod disruption budget and bounded resources. Both the controller image and
injected instance-manager image use the reviewed digest. Webhooks retain `Fail`
failure policy. Readiness requires original resource identities, established CRDs,
two current ready pods, a consistent webhook CA and a real server dry-run through
the admission webhook. That probe persists no database, credentials or PVCs.

An interrupted or uncertain create retains a durable intent. Preserve the private
journal, plan, release and implementation; retry with `--resume` and the original
create directory. An uncertain missing object is never recreated automatically.
Do not delete the journal or substitute an existing resource to bypass this check.

The completed private journal's `operator` object supplies the six original
operator handles required by [database installation](POSTGRES_HA.md). Select the
matching 1.30.1 database policy and pass the same release to its renderer with
`--cnpg-release`. Backups, Ceph readiness, PostgreSQL synchronous replication,
operator failover and full-stack acceptance remain separate checks.

## Local rehearsal coverage

A disposable local K3s 1.35.0 VM rehearsal on 2026-11-17 ran the pinned operator
on two dedicated workers. It checked the original resource journal, both ready
candidates and the actual admission webhook, then deleted one candidate pod,
observed its replacement and resumed the same journal. No PostgreSQL Cluster or
PVC persisted. The fixture removed its guests and verified unchanged host kernel
tunables. An earlier API-only rehearsal also checked all twenty admitted objects.
A follow-up rehearsal identified the current operator leader, removed its pod,
and observed the original election Lease change holder and renew again. The
[upstream operator election identifier](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.1/internal/cmd/manager/controller/controller.go)
distinguishes that Lease from a PostgreSQL Cluster's primary-election Lease.

The repeatable opt-in case is
[`test_real_worker_operator`](../scripts/test_cnpg_operator.py), backed by
the [isolated worker fixture](../scripts/test_cnpg_operator_runtime_fixture.py).
It requires the existing pinned K3s/QEMU fixture assets, an owned bounded Linux
VM service/network namespace, `CNPG_TEST_RELEASE`, `CNPG_TEST_OPERATOR_OCI`, and
`CNPG_TEST_VM_RUNTIME=1`. It is skipped by the default validation lane.
Both workers share one physical test machine. An operator election transition
does not establish successful database reconciliation, physical-host HA,
database promotion, Ceph durability, or acceptance of a complete application stack.
