#!/usr/bin/env python3
"""Syntax-check inline shell blocks embedded in Ansible playbooks.

The standalone shell validator catches scripts under scripts/, but a lot of the
production repair logic lives in ansible.builtin.shell blocks. This test extracts
literal/folded free-form and structured cmd blocks, normalizes common Jinja
expressions to shell-safe tokens, and runs bash -n over each block.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys
import tempfile
import textwrap

from test_bash_support import BashRuntimeUnavailable, bash_executable, bash_path, run_bash_args

# The Bash adapter owns the bounded subprocess.run invocation for every block.


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK_DIR = ROOT / "ansible" / "playbooks"
ARGOCD_REPAIR_PLAYBOOK = PLAYBOOK_DIR / "repair-argocd-service-path.yml"
COREDNS_REPAIR_PLAYBOOK = PLAYBOOK_DIR / "repair-cluster-dns.yml"
LONGHORN_BOOTSTRAP_PLAYBOOK = PLAYBOOK_DIR / "bootstrap-longhorn.yml"
OPENBAO_READINESS_PLAYBOOK = PLAYBOOK_DIR / "verify-openbao.yml"
SHELL_BLOCK_RE = re.compile(
    r"^(?P<indent>\s*)(?:ansible\.builtin\.)?(?:shell|command):\s*(?P<style>[|>])(?:[-+])?\s*(?:#.*)?$"
)
SHELL_MAPPING_RE = re.compile(
    r"^(?P<indent>\s*)(?:ansible\.builtin\.)?(?:shell|command):\s*(?:#.*)?$"
)
SHELL_CMD_BLOCK_RE = re.compile(
    r"^(?P<indent>\s*)cmd:\s*(?P<style>[|>])(?:[-+])?\s*(?:#.*)?$"
)
EXCLUDE_DIRS = {
    ".git",
    ".cache",
    ".pytest_cache",
    ".terraform",
    ".venv",
    "__pycache__",
    "build",
    "charts",
    "dist",
    "private",
    "rendered",
    "secrets",
}


def should_skip(path: Path) -> bool:
    return any(part in EXCLUDE_DIRS or part.startswith(".ansible-shell-syntax-") for part in path.parts)


def normalize_jinja(text: str) -> str:
    text = re.sub(r"{#.*?#}", "", text, flags=re.S)
    text = re.sub(r"{%.*?%}", ":", text, flags=re.S)
    text = re.sub(r"{{.*?}}", "JINJA_VALUE", text, flags=re.S)
    return text


def deindent_block(raw_lines: list[str]) -> str:
    min_indent: int | None = None
    for line in raw_lines:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        min_indent = indent if min_indent is None else min(min_indent, indent)
    if min_indent is None:
        return ""
    return "\n".join(line[min_indent:] if len(line) >= min_indent else "" for line in raw_lines)


def shell_blocks(path: Path) -> list[tuple[int, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = SHELL_BLOCK_RE.match(line)
        if not match:
            cmd_match = SHELL_CMD_BLOCK_RE.match(line)
            if cmd_match:
                cmd_indent = len(cmd_match.group("indent"))
                for previous in reversed(lines[:index]):
                    if not previous.strip():
                        continue
                    previous_indent = len(previous) - len(previous.lstrip())
                    if previous_indent >= cmd_indent:
                        continue
                    if SHELL_MAPPING_RE.match(previous):
                        match = cmd_match
                    break
        if not match:
            continue
        parent_indent = len(match.group("indent"))
        raw_block: list[str] = []
        for next_line in lines[index + 1 :]:
            if not next_line.strip():
                raw_block.append(next_line)
                continue
            indent = len(next_line) - len(next_line.lstrip())
            if indent <= parent_indent:
                break
            raw_block.append(next_line)
        block = deindent_block(raw_block)
        if block:
            blocks.append((index + 1, normalize_jinja(block)))
    return blocks


def playbooks() -> list[Path]:
    if not PLAYBOOK_DIR.exists():
        return []
    return [path for path in sorted(PLAYBOOK_DIR.glob("*.yml")) if not should_skip(path)]


def validate_free_form_comment_quotes(path: Path) -> list[str]:
    errors: list[str] = []
    for line_no, script in shell_blocks(path):
        for offset, line in enumerate(script.splitlines()):
            comment = line.lstrip()
            if not comment.startswith("#"):
                continue
            if comment.count("'") % 2 or comment.count('"') % 2:
                errors.append(
                    f"{path.relative_to(ROOT)}:{line_no + offset + 1}: "
                    "Ansible free-form shell comment has an unmatched quote"
                )
    return errors


def validate_argocd_cleanup_contract() -> list[str]:
    text = ARGOCD_REPAIR_PLAYBOOK.read_text(encoding="utf-8")
    errors: list[str] = []
    if 'grep -E "/argocd-" |' in text or 'grep -E "^pod/argocd-" |' in text:
        errors.append("stale Argo CD cleanup must not fail on empty grep results under pipefail")
    required_fragments = (
        '*/argocd-*)',
        'pod/argocd-*)',
        'done < <("$K" --kubeconfig "$C" -n argocd get "${kind}" -o name 2>/dev/null || true)',
        'done < <("$K" --kubeconfig "$C" -n argocd get pod -o name 2>/dev/null || true)',
        "Convert active Argo CD HA internal services to headless services",
        "make_headless argo-cd-argocd-repo-server",
        "make_headless argo-cd-redis-ha-haproxy",
        '"clusterIP": "None"',
        "PLATFORM_ARGOCD_SERVICE_REPAIR_HEADLESS_INTERNALS",
        "Reconcile Traefik IngressClass permission in active platform project",
        'required = {"group": "networking.k8s.io", "kind": "IngressClass"}',
        "argocd_application_traefik=refresh-sync-requested",
        "Prune known stale Traefik chart resources",
        "PLATFORM_ARGOCD_PRUNE_LEGACY_TRAEFIK",
        "unexpected-prune-candidates",
        '("networking.k8s.io", "IngressClass", "", "platform-traefik")',
        "action=prune-requested",
        "prune=true",
        "action=skip-unchanged-rollout",
        "Recycle Argo CD pods with timed-out local health probes",
        "PLATFORM_ARGOCD_HEALTH_PROBE_RECYCLE_MIN_AGE",
        "reason=pod-too-young-for-recycle",
        "action=recycle reason=local-health-probe-timeout",
        "argocd_health_probe_recovery recycled=${recycled}",
        "Stabilize active Argo CD controller and repo server for loaded clusters",
        "local_http_exec_probe",
        "/dev/tcp/127.0.0.1/{port}",
        'local_http_exec_probe(8084, "/healthz"',
        "/dev/tcp/127.0.0.1/8082",
        '"httpGet": None',
        '"tcpSocket": None',
        '"grpc": None',
        "Remove obsolete Cilium kubelet health policy after local exec probe migration",
        "ciliumnetworkpolicy/platform-argocd-kubelet-health-probes",
        '"requests": {"cpu": "500m", "memory": "512Mi"}',
        '"startupProbe": startup',
        "Wait for stabilized or recycled Argo CD workloads to become ready",
        "PLATFORM_ARGOCD_HEALTH_PROBE_RECOVERY_TIMEOUT",
        'APPS="{{ platform_argocd_service_repair_retry_apps_effective }}"',
        "action=hard-refresh-requested",
        "Retry failed Argo CD application operations after service repair",
        "PLATFORM_ARGOCD_SERVICE_REPAIR_RETRY_APPS",
        "PLATFORM_ARGOCD_SERVICE_REPAIR_APP_SYNC_TIMEOUT",
        "action=sync-finished",
        "action=sync-requested reason=${retry_reason}",
        '"prune":false',
        "Verify final Argo CD core readiness after application retries",
        "final_ready_endpoints=${ready_endpoints}",
        "poddisruptionbudget networkpolicy",
    )
    for fragment in required_fragments:
        if fragment not in text:
            errors.append(f"stale Argo CD cleanup is missing idempotent fragment: {fragment}")
    return errors


def validate_coredns_rollout_contract() -> list[str]:
    text = COREDNS_REPAIR_PLAYBOOK.read_text(encoding="utf-8")
    errors: list[str] = []
    required_fragments = (
        "Configure CoreDNS rollout strategy for strict node spreading",
        "Wait for CoreDNS rollout with one stuck rollout recovery",
        "PLATFORM_DNS_COREDNS_ROLLOUT_TIMEOUT",
        "ProgressDeadlineExceeded",
        "platform.gitops/coredns-recovery-at",
        "CoreDNS rollout did not converge within",
        "Guard platform workloads from unavailable OpenBao injector admission",
        "Wait for CoreDNS after OpenBao admission guard",
        "platform.gitops/openbao-injection",
        "openbao_injector_guard admission_dry_run=ok",
        "run_kubernetes_api_service_check",
        "Kubernetes API ClusterIP TLS service-path probe",
        "https://kubernetes.default.svc",
        "platform-kubernetes-api-check",
    )
    for fragment in required_fragments:
        if fragment not in text:
            errors.append(f"CoreDNS repair is missing rollout recovery fragment: {fragment}")
    if text.count('"maxUnavailable": 1') < 2:
        errors.append("CoreDNS repair and HA placement must both retain maxUnavailable=1")
    if text.count('"maxSurge": 0') < 2:
        errors.append("CoreDNS repair and HA placement must both retain maxSurge=0")
    if re.search(r"(?m)^\s+- name: Wait for CoreDNS rollout\s*$", text):
        errors.append("CoreDNS must not retry rollout status after ProgressDeadlineExceeded")
    if text.count("run_kubernetes_api_service_check()") < 2:
        errors.append("CoreDNS must probe the Kubernetes API ClusterIP before and after service-path repair")
    if text.count('run_kubernetes_api_service_check || api_service_rc="$?"') < 2:
        errors.append("CoreDNS must enforce both Kubernetes API ClusterIP probe results")
    guard_script = re.search(
        r'''(?ms)^\s*python3 - "\$\{state_file\}" >"\$\{patches_file\}" <<'PY'\s*$\n'''
        r"(?P<body>.*?)^\s*PY\s*$",
        text,
    )
    if guard_script is None:
        errors.append("CoreDNS repair is missing the structured OpenBao webhook guard")
    else:
        try:
            compile(textwrap.dedent(guard_script.group("body")), str(COREDNS_REPAIR_PLAYBOOK), "exec")
        except SyntaxError as exc:
            errors.append(f"OpenBao webhook guard Python is invalid: {exc}")
    return errors


def validate_longhorn_embedded_python() -> list[str]:
    text = LONGHORN_BOOTSTRAP_PLAYBOOK.read_text(encoding="utf-8")
    errors: list[str] = []
    reconciliation_script = re.search(
        r'''(?ms)^\s*python3 - "\$\{node_name\}" "\$\{reason\}" .*? <<'PY'\s*$\n'''
        r"(?P<body>.*?)^\s*PY\s*$",
        text,
    )
    if reconciliation_script is None:
        return ["Longhorn bootstrap is missing automatic disk reconciliation Python"]
    try:
        compile(
            textwrap.dedent(reconciliation_script.group("body")),
            str(LONGHORN_BOOTSTRAP_PLAYBOOK),
            "exec",
        )
    except SyntaxError as exc:
        errors.append(f"Longhorn automatic disk reconciliation Python is invalid: {exc}")
    return errors


def main() -> int:
    try:
        _, flavor = bash_executable()
    except BashRuntimeUnavailable as exc:
        print(f"Ansible inline shell syntax validation skipped: {exc}; bash is required for Ansible inline shell syntax validation.")
        return 0

    playbook_contract_errors = (
        validate_argocd_cleanup_contract()
        + validate_coredns_rollout_contract()
        + validate_longhorn_embedded_python()
    )
    for path in playbooks():
        playbook_contract_errors.extend(validate_free_form_comment_quotes(path))
    if len(shell_blocks(OPENBAO_READINESS_PLAYBOOK)) != 1:
        playbook_contract_errors.append(
            "OpenBao readiness playbook shell cmd block is not covered by syntax validation"
        )
    if playbook_contract_errors:
        print("Ansible repair contract validation failed:")
        for error in playbook_contract_errors:
            print(f" - {error}")
        return 1

    failures: list[tuple[Path, int, str]] = []
    block_count = 0
    with tempfile.TemporaryDirectory(prefix=".ansible-shell-syntax-", dir=ROOT) as temp_root_name:
        temp_root = Path(temp_root_name)
        for playbook in playbooks():
            for line_no, script in shell_blocks(playbook):
                block_count += 1
                rel = playbook.relative_to(ROOT)
                normalized = temp_root / f"{rel.as_posix().replace('/', '__')}-{line_no}.sh"
                normalized.write_text(script + "\n", encoding="utf-8", newline="\n")
                try:
                    result = run_bash_args(["-n", bash_path(normalized, flavor)])
                except BashRuntimeUnavailable as exc:
                    print(f"Ansible inline shell syntax validation skipped: {exc}; bash is required for Ansible inline shell syntax validation.")
                    return 0
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "").strip()
                    failures.append((rel, line_no, detail))

    if failures:
        print("Ansible inline shell syntax validation failed:")
        for rel, line_no, detail in failures:
            print(f" - {rel}:{line_no}")
            if detail:
                print(f"   {detail}")
        return 1

    print(f"Ansible inline shell syntax validation passed for {block_count} blocks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
