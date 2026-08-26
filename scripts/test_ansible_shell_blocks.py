#!/usr/bin/env python3
"""Syntax-check inline shell blocks embedded in Ansible playbooks.

The standalone shell validator catches scripts under scripts/, but a lot of the
production repair logic lives in ansible.builtin.shell blocks. This test extracts
literal/folded free-form and structured cmd blocks, normalizes common Jinja
expressions to shell-safe tokens, and runs bash -n over each block.
"""

from __future__ import annotations

import contextlib
import io
import json
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


def validate_jinja_bash_collisions(path: Path) -> list[str]:
    errors: list[str] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if "${#" in line:
            errors.append(
                f"{path.relative_to(ROOT)}:{line_no}: Bash parameter-length "
                "syntax starts a Jinja comment; use an explicit counter"
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
        ".status.reconciledAt",
        "wait_for_reconciliation",
        "hard-refresh-not-reconciled",
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
        "PLATFORM_ARGOCD_SERVICE_REPAIR_REFRESH_TIMEOUT",
        "declare -A refresh_baseline_reconciled_at",
        "refresh_deadline",
        "refresh_last_state",
        "action=clear-preexisting-refresh",
        "action=hard-refresh-acknowledged",
        "reason=hard-refresh-unacknowledged",
        "Explain skipped legacy Traefik prune after unacknowledged refresh",
        "Retry failed Argo CD application operations after service repair",
        "PLATFORM_ARGOCD_SERVICE_REPAIR_RETRY_APPS",
        "PLATFORM_ARGOCD_SERVICE_REPAIR_APP_SYNC_TIMEOUT",
        "PLATFORM_ARGOCD_SERVICE_REPAIR_RECOVER_STUCK_OPERATIONS",
        "PLATFORM_ARGOCD_SERVICE_REPAIR_OPERATION_TERMINATION_TIMEOUT",
        "read_application_state()",
        "remaining_operation_timeout()",
        "recover_timed_out_operation()",
        "action=terminate-stuck-operation",
        "action=clear-stale-operation-state",
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
    refresh_index = text.find("Refresh platform applications after Argo CD service repair")
    retry_index = text.find("Retry failed Argo CD application operations after service repair")
    prune_index = text.find("Prune known stale Traefik chart resources")
    readiness_index = text.find("Verify final Argo CD core readiness after application retries")
    if refresh_index < 0 or retry_index < 0:
        errors.append("Argo CD application refresh and retry tasks must both exist")
    else:
        refresh_block = text[refresh_index:retry_index]
        if "wait_for_refresh()" in refresh_block:
            errors.append("Argo CD application refreshes must not wait serially per application")
        for fragment in (
            ".status.reconciledAt",
            "refresh_apps+=(\"${app}\")",
            "reason=hard-refresh-not-acknowledged",
            'if [ "${reconciliation_observed}" = "true" ]; then',
            "action=hard-refresh-acknowledged",
            '[ "${sync_status}" = "Synced" ]',
            '""|Succeeded) stable=true',
            "argocd.argoproj.io/refresh-",
        ):
            if fragment not in refresh_block:
                errors.append(f"Argo CD shared refresh wait is missing fragment: {fragment}")
        collapsed_refresh_block = re.sub(r"\\\s*\n\s*", " ", refresh_block)
        if re.search(
            r'if \[ "\$\{reconciliation_observed\}" = "true" \] && '
            r'\[ -z "\$\{requested_revision\}" \]',
            collapsed_refresh_block,
        ):
            errors.append(
                "Argo CD refresh acknowledgement must not wait for an active application operation"
            )
        if 'if ! state="$("$K" --kubeconfig "$C" -n argocd get' not in refresh_block:
            errors.append("Argo CD refresh acknowledgement must reject unavailable application state")
        retry_block = text[retry_index:prune_index]
        for fragment in (
            "read_application_state()",
            "read_operation_message()",
            'if ! read_application_state "${app}"; then',
            "reason=state-unavailable-after-wait",
            "reason=sync-request-failed",
            "reason=retry-${operation_phase}",
            "message=${operation_message:-unavailable}",
            '"op": "test",',
            '"path": "/metadata/resourceVersion",',
            '"value": "Terminating",',
            "reason=operation-changed-during-termination",
            "reason=stuck-operation-recovery-rejected",
            '"status":{"operationState":null}',
            'exit "${failed}"',
        ):
            if fragment not in retry_block:
                errors.append(f"Argo CD operation retry is missing fragment: {fragment}")
        recovery_marker = (
            'if ! recovery_action="$(python3 - "${application_json}" "${patch_json}"'
        )
        recovery_start = retry_block.find(recovery_marker)
        recovery_heredoc = retry_block.find("<<'PY'\n", recovery_start)
        recovery_end = retry_block.find("\n        PY\n", recovery_heredoc)
        if recovery_start < 0 or recovery_heredoc < 0 or recovery_end < 0:
            errors.append("Argo CD operation retry is missing its structured recovery decision")
        else:
            recovery_body = retry_block[recovery_heredoc + len("<<'PY'\n") : recovery_end]
            try:
                recovery_code = compile(
                    textwrap.dedent(recovery_body),
                    str(ARGOCD_REPAIR_PLAYBOOK),
                    "exec",
                )
            except SyntaxError as exc:
                errors.append(f"Argo CD operation recovery Python is invalid: {exc}")
            else:
                def recovery_decision(
                    application: dict[str, object],
                    expected_requested: str,
                    expected_started: str,
                    expected_state_revision: str,
                ) -> tuple[str, list[dict[str, object]]]:
                    with tempfile.TemporaryDirectory(prefix="argocd-operation-recovery-") as temp:
                        source_path = Path(temp) / "application.json"
                        patch_path = Path(temp) / "patch.json"
                        source_path.write_text(json.dumps(application), encoding="utf-8")
                        original_argv = sys.argv
                        output = io.StringIO()
                        try:
                            sys.argv = [
                                "argocd-operation-recovery",
                                str(source_path),
                                str(patch_path),
                                expected_requested,
                                expected_started,
                                expected_state_revision,
                            ]
                            with contextlib.redirect_stdout(output):
                                exec(recovery_code, {"__name__": "__main__"})
                        finally:
                            sys.argv = original_argv
                        return output.getvalue().strip(), json.loads(
                            patch_path.read_text(encoding="utf-8")
                        )

                running_application = {
                    "metadata": {"resourceVersion": "42"},
                    "operation": {"sync": {"revision": "main"}},
                    "status": {
                        "sync": {"status": "Synced"},
                        "operationState": {
                            "phase": "Running",
                            "startedAt": "2026-08-26T12:00:00Z",
                            "operation": {"sync": {"revision": "main"}},
                        },
                    },
                }
                action, patch = recovery_decision(
                    running_application,
                    "main",
                    "2026-08-26T12:00:00Z",
                    "main",
                )
                if action != "terminate" or not any(
                    item.get("value") == "Terminating" for item in patch
                ):
                    errors.append("unchanged timed-out Argo CD operation is not terminated safely")

                changed_application = json.loads(json.dumps(running_application))
                changed_application["operation"]["sync"]["revision"] = "newer"
                changed_application["status"]["operationState"]["startedAt"] = (
                    "2026-08-26T12:15:00Z"
                )
                action, patch = recovery_decision(
                    changed_application,
                    "main",
                    "2026-08-26T12:00:00Z",
                    "main",
                )
                if action != "reject-operation-changed" or patch:
                    errors.append("newer Argo CD operation is not protected from stale recovery")

                stale_application = json.loads(json.dumps(running_application))
                stale_application.pop("operation")
                action, patch = recovery_decision(
                    stale_application,
                    "main",
                    "2026-08-26T12:00:00Z",
                    "main",
                )
                if action != "clear-stale-state" or not any(
                    item.get("op") == "remove"
                    and item.get("path") == "/status/operationState"
                    for item in patch
                ):
                    errors.append("synced Argo CD application cannot clear stale operation status")

                stale_application["status"]["sync"]["status"] = "OutOfSync"
                action, patch = recovery_decision(
                    stale_application,
                    "main",
                    "2026-08-26T12:00:00Z",
                    "main",
                )
                if action != "reject-stale-state-not-synced" or patch:
                    errors.append("out-of-sync Argo CD stale status does not fail closed")
        collapsed_retry_block = re.sub(r"\\\s*\n\s*", " ", retry_block)
        if not re.search(
            r'if ! wait_for_requested_operation "\$\{app\}" '
            r'"\$\{initial_operation_timeout\}"; then\s+'
            r'if \[ "\$\{RECOVER_STUCK_OPERATIONS\}" != "true" \]; then.*?'
            r'recover_timed_out_operation "\$\{app\}"',
            collapsed_retry_block,
            flags=re.S,
        ):
            errors.append(
                "Argo CD stuck-operation recovery must run only after the full operation wait"
            )
    if not (retry_index < prune_index < readiness_index):
        errors.append("legacy Traefik pruning must run after Argo CD repair and application retries")
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
    playbook_contract_errors = (
        validate_argocd_cleanup_contract()
        + validate_coredns_rollout_contract()
        + validate_longhorn_embedded_python()
    )
    for path in playbooks():
        playbook_contract_errors.extend(validate_free_form_comment_quotes(path))
        playbook_contract_errors.extend(validate_jinja_bash_collisions(path))
    if len(shell_blocks(OPENBAO_READINESS_PLAYBOOK)) != 1:
        playbook_contract_errors.append(
            "OpenBao readiness playbook shell cmd block is not covered by syntax validation"
        )
    if playbook_contract_errors:
        print("Ansible repair contract validation failed:")
        for error in playbook_contract_errors:
            print(f" - {error}")
        return 1

    try:
        _, flavor = bash_executable()
    except BashRuntimeUnavailable as exc:
        print(f"Ansible inline shell syntax validation skipped: {exc}; bash is required for Ansible inline shell syntax validation.")
        return 0

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
