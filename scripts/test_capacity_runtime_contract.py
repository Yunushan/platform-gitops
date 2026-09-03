#!/usr/bin/env python3
"""Validate the fail-closed production capacity runtime contract."""

from __future__ import annotations

from pathlib import Path
import copy
import shutil
import subprocess
import sys
import tempfile

import yaml

from refresh_platform_resource_budget import BUDGET, refresh, set_leaf, values


ROOT = Path(__file__).resolve().parents[1]


def read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"required capacity file is missing: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} is missing {needle!r}")


def forbid(text: str, needle: str, label: str) -> None:
    if needle in text:
        raise AssertionError(f"{label} must not contain {needle!r}")


def main() -> int:
    check_compact_budget()
    verifier = read(ROOT / "ansible/playbooks/verify-platform-capacity.yml")
    longhorn_bootstrap = read(ROOT / "ansible/playbooks/bootstrap-longhorn.yml")
    forgejo_storage_repair = read(ROOT / "ansible/playbooks/repair-forgejo-storage.yml")
    longhorn_disk_path_task = read(ROOT / "ansible/tasks/validate-longhorn-disk-path.yml")
    node_prepare = read(ROOT / "ansible/playbooks/prepare-nodes.yml")
    makefile = read(ROOT / "Makefile")
    production_check = read(ROOT / "scripts/bootstrap/run-platform-production-check.sh")
    planning = read(ROOT / "docs/CAPACITY_PLANNING.md")
    readiness = read(ROOT / "docs/PRODUCTION_READINESS.md")

    for needle in (
        "hosts: rke2_servers",
        "hosts: rke2_servers[0]",
        "PLATFORM_CAPACITY_ROOT_FREE_PERCENT",
        "PLATFORM_CAPACITY_STORAGE_FREE_PERCENT",
        "PLATFORM_CAPACITY_DEDICATED_STORAGE_REQUIRED",
        "PLATFORM_CAPACITY_MAX_CPU_PERCENT",
        "PLATFORM_CAPACITY_MAX_MEMORY_PERCENT",
        "PLATFORM_CAPACITY_MAX_PODS_PERCENT",
        "PLATFORM_CAPACITY_LONGHORN_FREE_PERCENT",
        "PLATFORM_STORAGE_ENCRYPTION_REQUIRED",
        "LONGHORN_ENCRYPTION_SECRET_NAME",
        "df -Pk",
        "stat -f -c '%i'",
        'get nodes -o json',
        'get pods -A -o json',
        "nodes.longhorn.io",
        "platform_capacity_storage_path_effective",
        "diskPath",
        "longhorn-disk-path-missing",
        "longhorn-disk-path-mismatch",
        "configured_storage_path",
        "os.path.normpath",
        "unsupported Kubernetes quantity",
        "DiskPressure",
        "MemoryPressure",
        "PIDPressure",
        "regular_cpu",
        "init_cpu",
        "longhorn_schedulable_nodes",
        "filesystem-headroom-below-threshold",
        "platform-storage-path-missing",
        "platform-storage-filesystem-detection-failed",
        "platform-storage-shares-root-filesystem",
        "ready-node-count-below-threshold",
        "cpu-requests-above-threshold",
        "memory-requests-above-threshold",
        "pod-capacity-above-threshold",
        "longhorn-free-capacity-below-threshold",
        "platform-capacity-headroom-verified",
        "longhorn-standard-encrypted",
        "longhorn-critical-encrypted",
        "longhorn-cache-encrypted",
        "CRYPTO_KEY_VALUE",
        'get pvc -A -o json',
        'get pv -o json',
        "nodeExpandSecretRef",
        "bound-longhorn-pvc-not-encrypted",
        "longhorn-storage-encryption-verified",
    ):
        require(verifier, needle, "capacity verifier")

    for text, label in (
        (longhorn_bootstrap, "Longhorn bootstrap"),
        (forgejo_storage_repair, "Forgejo storage repair"),
    ):
        for needle in (
            "validate-longhorn-disk-path.yml",
            "platform_longhorn_dedicated_storage_required_effective",
        ):
            require(text, needle, label)
    for needle in (
        "root_source",
        "storage_source",
        "root_fsid",
        "storage_fsid",
        "PLATFORM_LONGHORN_DEDICATED_STORAGE_REQUIRED=false",
        "Longhorn disk path",
    ):
        require(longhorn_disk_path_task, needle, "Longhorn disk-path preflight")

    for forbidden in (
        "kubectl patch",
        "kubectl delete",
        "kubectl apply",
        "allowScheduling=true",
    ):
        forbid(verifier, forbidden, "read-only capacity verifier")

    for needle in (
        "cryptsetup",
        "dm_crypt",
        "Verify encrypted Longhorn volume prerequisites",
    ):
        require(node_prepare, needle, "RKE2 node preparation")

    require(makefile, "platform-capacity-verify:", "Makefile")
    require(
        production_check,
        'PLATFORM_CAPACITY_DEDICATED_STORAGE_REQUIRED=true "${make_command}" platform-capacity-verify',
        "production readiness gate",
    )
    require(planning, "make platform-capacity-verify", "capacity planning runbook")
    require(
        planning,
        "PLATFORM_CAPACITY_DEDICATED_STORAGE_REQUIRED=true",
        "capacity planning runbook",
    )
    require(readiness, "make platform-capacity-verify", "production readiness runbook")

    print("Production capacity runtime contract passed.")
    return 0


def check_compact_budget() -> None:
    apps = ROOT / 'gitops/clusters/rke2-main/premium-3node/apps'
    for app, leaves in BUDGET.items():
        text = read(apps / app / 'values.yaml')
        original = values(text)
        expected = copy.deepcopy(original)
        for key, value in leaves.items():
            set_leaf(expected, key.split('.'), value)
        changed = refresh(text, app)
        assert values(changed) == expected
        assert refresh(changed, app) == changed
        assert original == expected, f'{app} public baseline drifted from the compact budget'

    text = read(apps / 'loki/values.yaml').replace('cpu: 150m', 'cpu: 500m')
    text = text.replace('chunksCache:\n  allocatedMemory: 512\n  allocatedCPU: 100m\n', '')
    text = text.replace('resultsCache:\n  allocatedMemory: 128\n  allocatedCPU: 100m\n', '')
    text += '\nprivateSetting: "https://logs.example.invalid/private" # retained\n'
    result = refresh(text, 'loki')
    assert 'privateSetting: "https://logs.example.invalid/private" # retained' in result
    assert values(result)['chunksCache']['allocatedMemory'] == 512
    assert values(result)['resultsCache']['allocatedCPU'] == '100m'
    assert 'chunksCache:\n  allocatedMemory: 512\n  allocatedCPU: 100m' in result
    assert 'resultsCache:\n  allocatedMemory: 128\n  allocatedCPU: 100m' in result
    for malformed in (
        'write: {}\nwrite: {}\n',
        'write: []\n',
        read(apps / 'loki/values.yaml') + '\nchunksCache: {resources: {requests: {cpu: 1}}}\n',
        read(apps / 'loki/values.yaml').replace('chunksCache:\n',
                                               'chunksCache:\n  resources: {requests: {cpu: 1}}\n'),
    ):
        try:
            refresh(malformed, 'loki')
        except ValueError:
            pass
        else:
            raise AssertionError('unsafe or ambiguous resource refresh was accepted')
    grafana = read(apps / 'monitoring/values.yaml').replace('  replicas: 2\n  deploymentStrategy:',
                                                            '  replicas: 1\n  deploymentStrategy:')
    try:
        refresh(grafana, 'monitoring')
    except ValueError:
        pass
    else:
        raise AssertionError('single-replica Grafana must not get an HA rollout policy')

    with tempfile.TemporaryDirectory(prefix='platform-cpu-budget-test-') as tmp:
        root = Path(tmp)
        target = root / 'argocd-ha/values.yaml'
        target.parent.mkdir()
        old = read(apps / 'argocd-ha/values.yaml').replace('cpu: 200m', 'cpu: 500m')
        target.write_text(old, encoding='utf-8')
        command = [sys.executable, str(ROOT / 'scripts/refresh_platform_resource_budget.py'),
                   '--apps-root', str(root), '--app', 'argocd-ha']
        subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        assert target.read_text(encoding='utf-8') == old, 'default must be read-only'
        subprocess.run(command + ['--apply'], capture_output=True, text=True, check=True, timeout=30)
        assert values(target.read_text(encoding='utf-8')) == values(read(apps / 'argocd-ha/values.yaml'))
        target.write_text(old, encoding='utf-8')
        result = subprocess.run(command[:-2] + ['--apply'], capture_output=True, text=True, timeout=30)
        assert result.returncode != 0
        assert target.read_text(encoding='utf-8') == old, 'preflight must validate all selected files'

    helm = shutil.which('helm')
    if not helm:
        raise AssertionError('Helm is required to verify actual chart resource defaults')
    manifests = subprocess.run(
        [helm, 'template', 'loki', str(apps / 'loki/charts/loki'),
         '--namespace', 'logging', '--kube-version', '1.35.0',
         '-f', str(apps / 'loki/values.yaml')],
        capture_output=True, text=True, check=True, timeout=90,
    ).stdout
    rendered = {d['metadata']['name']: d for d in yaml.safe_load_all(manifests)
                if isinstance(d, dict) and d.get('kind') in {'StatefulSet', 'Deployment'}}
    for name, cpu, memory, cache in (
        ('loki-chunks-cache', '100m', '614Mi', '512'),
        ('loki-results-cache', '100m', '154Mi', '128'),
    ):
        spec = rendered[name]['spec']['template']['spec']
        container = next(c for c in spec['containers'] if c['name'] == 'memcached')
        assert container['resources']['requests'] == {'cpu': cpu, 'memory': memory}
        assert container['resources']['limits'] == {'memory': memory}
        assert '-m ' + cache in container['args']
        assert not rendered[name]['spec'].get('volumeClaimTemplates')
    for name in ('loki-write', 'loki-read', 'loki-backend'):
        assert rendered[name]['spec']['replicas'] == 3
        assert 'cpu' not in rendered[name]['spec']['template']['spec']['containers'][0]['resources']['limits']
    assert rendered['loki-gateway']['spec']['strategy']['rollingUpdate'] == {
        'maxSurge': 0, 'maxUnavailable': 1,
    }
    manifests = subprocess.run(
        [helm, 'template', 'argo-cd', str(apps / 'argocd-ha/charts/argo-cd-10.3.2/argo-cd'),
         '--namespace', 'argocd', '--kube-version', '1.35.0', '-f', str(apps / 'argocd-ha/values.yaml')],
        capture_output=True, text=True, check=True, timeout=90,
    ).stdout
    repo = next(d for d in yaml.safe_load_all(manifests) if isinstance(d, dict)
                and d.get('kind') == 'Deployment' and d['metadata']['name'] == 'argo-cd-argocd-repo-server')
    assert repo['spec']['replicas'] == 3
    assert repo['spec']['strategy']['rollingUpdate'] == {'maxSurge': 0, 'maxUnavailable': 1}
    spec = repo['spec']['template']['spec']
    assert next(c for c in spec['initContainers'] if c['name'] == 'copyutil')['resources']['requests']['cpu'] == '200m'
    assert next(c for c in spec['containers'] if c['name'] == 'repo-server')['resources']['requests']['cpu'] == '200m'


if __name__ == "__main__":
    raise SystemExit(main())
