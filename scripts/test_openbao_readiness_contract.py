#!/usr/bin/env python3
"""Validate the dedicated fail-closed OpenBao production-readiness gate."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = ROOT / "ansible/playbooks/verify-openbao.yml"
MAKEFILE = ROOT / "Makefile"
PRODUCTION_CHECK = ROOT / "scripts/bootstrap/run-platform-production-check.sh"
READINESS = ROOT / "docs/PRODUCTION_READINESS.md"
PREMIUM = ROOT / "docs/PREMIUM_3NODE.md"
EVIDENCE_RUNNER = ROOT / "scripts/bootstrap/run-platform-production-evidence.sh"
EVIDENCE_VALIDATOR = ROOT / "scripts/verify_production_evidence.py"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} is missing required text: {needle}")


def forbid(text: str, needle: str, label: str) -> None:
    if needle in text:
        raise AssertionError(f"{label} must not contain mutating command: {needle}")


def main() -> int:
    playbook = read(PLAYBOOK)
    for needle in (
        "PLATFORM_OPENBAO_VERIFY_STRICT",
        "app.kubernetes.io/name=openbao,component=server",
        'exec "pod/${pod}" -c openbao',
        "bao status -format=json",
        'status_rc}" -ne 0',
        'status_rc}" -ne 2',
        "cluster_id_sha256=",
        'desired}" -ge 3',
        'current}" -eq "${desired}',
        'updated}" -eq "${desired}',
        'ready}" -eq "${desired}',
        'initialized_count}" -eq "${desired}',
        'unsealed_count}" -eq "${desired}',
        'ha_count}" -eq "${desired}',
        'unique_cluster_ids}" -eq 1',
        "result=pass reason=openbao-production-ready",
        "result=fail reason=openbao-production-readiness-incomplete",
        "failed_when: false",
    ):
        require(playbook, needle, "OpenBao readiness playbook")

    for forbidden in (
        "${#",
        "operator init",
        "operator unseal",
        "kubectl apply",
        "kubectl delete",
        "kubectl patch",
        "rollout restart",
        "create secret",
    ):
        forbid(playbook, forbidden, "OpenBao readiness playbook")

    makefile = read(MAKEFILE)
    production_check = read(PRODUCTION_CHECK)
    for needle in (
        "platform-openbao-status:",
        "PLATFORM_OPENBAO_VERIFY_STRICT=false",
        "platform-openbao-verify:",
        "PLATFORM_OPENBAO_VERIFY_STRICT=true",
        "ansible/playbooks/verify-openbao.yml",
    ):
        require(makefile, needle, "Makefile OpenBao readiness surface")
    require(
        production_check,
        '"${make_command}" platform-openbao-verify',
        "production-check OpenBao readiness surface",
    )

    readiness = read(READINESS)
    require(readiness, "make platform-openbao-verify", str(READINESS.relative_to(ROOT)))
    premium = read(PREMIUM)
    require(premium, "make platform-openbao-status", str(PREMIUM.relative_to(ROOT)))
    require(premium, "make platform-openbao-verify", str(PREMIUM.relative_to(ROOT)))

    runner = read(EVIDENCE_RUNNER)
    require(runner, '"schemaVersion": 7', "production evidence runner")
    require(runner, '"openbaoReadiness": "passed"', "production evidence runner")
    require(runner, '"openbaoCeremony": "passed"', "production evidence runner")

    validator = read(EVIDENCE_VALIDATOR)
    require(validator, '"openbaoReadiness"', "production evidence validator")
    require(validator, '"openbaoCeremony"', "production evidence validator")
    require(validator, "schemaVersion must be 7", "production evidence validator")

    print("OpenBao production-readiness contract passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
