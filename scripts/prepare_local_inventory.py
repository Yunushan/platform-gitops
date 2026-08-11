#!/usr/bin/env python3
"""Normalize and fail-closed validate the private Ansible inventory."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "inventory" / "hosts.local.ini"
DEFAULT_EXPECTED_HOSTS = 3
PARSER_TIMEOUT_SECONDS = 30
PLACEHOLDER_PREFIX = "<"
PLACEHOLDER_SUFFIX = ">"


class InventoryPreflightError(ValueError):
    """Raised when the local inventory cannot safely drive Ansible."""


def normalize_inventory_bytes(raw: bytes) -> tuple[str, bool]:
    """Decode UTF-8 and normalize harmless newline/terminator differences."""

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InventoryPreflightError(
            "inventory must be UTF-8 text; it could not be decoded safely"
        ) from exc

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized and not normalized.endswith("\n"):
        normalized += "\n"
    return normalized, normalized.encode("utf-8") != raw


def _is_placeholder(value: object) -> bool:
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    return candidate.startswith(PLACEHOLDER_PREFIX) and candidate.endswith(
        PLACEHOLDER_SUFFIX
    )


def validate_inventory_payload(
    payload: object,
    *,
    expected_hosts: int = DEFAULT_EXPECTED_HOSTS,
) -> list[str]:
    """Validate the small inventory contract required by the cluster playbooks."""

    if not isinstance(payload, dict):
        raise InventoryPreflightError("ansible-inventory returned a non-object JSON document")

    group = payload.get("rke2_servers")
    if not isinstance(group, dict):
        raise InventoryPreflightError(
            "inventory is missing the [rke2_servers] group"
        )

    hosts = group.get("hosts")
    if not isinstance(hosts, list) or not all(isinstance(host, str) for host in hosts):
        raise InventoryPreflightError(
            "[rke2_servers] must contain a host list"
        )
    if len(set(hosts)) != len(hosts):
        raise InventoryPreflightError("[rke2_servers] contains duplicate hosts")
    if expected_hosts > 0 and len(hosts) != expected_hosts:
        raise InventoryPreflightError(
            f"[rke2_servers] must define exactly {expected_hosts} hosts; found {len(hosts)}"
        )
    if not hosts:
        raise InventoryPreflightError("[rke2_servers] must define at least one host")

    metadata = payload.get("_meta")
    hostvars = metadata.get("hostvars") if isinstance(metadata, dict) else None
    if not isinstance(hostvars, dict):
        raise InventoryPreflightError("ansible-inventory returned no host variables")

    missing_addresses: list[str] = []
    placeholder_addresses: list[str] = []
    for host in hosts:
        variables = hostvars.get(host)
        address = variables.get("ansible_host") if isinstance(variables, dict) else None
        if not isinstance(address, str) or not address.strip():
            missing_addresses.append(host)
        elif _is_placeholder(address):
            placeholder_addresses.append(host)

    if missing_addresses:
        raise InventoryPreflightError(
            "every [rke2_servers] host must define ansible_host; missing for: "
            + ", ".join(missing_addresses)
        )
    if placeholder_addresses:
        raise InventoryPreflightError(
            "replace the example ansible_host values before connecting; placeholders "
            "remain for: "
            + ", ".join(placeholder_addresses)
        )

    return hosts


def _diagnostic(stderr: str, stdout: str) -> str:
    details = stderr.strip() or stdout.strip()
    if not details:
        return "ansible-inventory returned no diagnostic output"
    lines = details.splitlines()
    if len(lines) > 20:
        lines = lines[:20] + ["... inventory diagnostic truncated ..."]
    return "\n".join(lines)


def parse_inventory(
    inventory: Path,
    *,
    expected_hosts: int = DEFAULT_EXPECTED_HOSTS,
    parser: str = "ansible-inventory",
) -> list[str]:
    """Ask Ansible to parse the inventory and validate its JSON result."""

    environment = os.environ.copy()
    environment.update(
        {
            "ANSIBLE_NOCOLOR": "true",
            "ANSIBLE_FORCE_COLOR": "false",
            "ANSIBLE_DEPRECATION_WARNINGS": "false",
        }
    )
    try:
        result = run_bounded(
            [parser, "-i", str(inventory), "--list"],
            cwd=ROOT,
            env=environment,
            text=True,
            timeout=PARSER_TIMEOUT_SECONDS,
            output_max_bytes=1024 * 1024,
            check=False,
        )
    except FileNotFoundError as exc:
        raise InventoryPreflightError(
            "ansible-inventory is not installed or is not on PATH; install Ansible "
            "before running cluster targets"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise InventoryPreflightError(
            f"ansible-inventory did not finish within {PARSER_TIMEOUT_SECONDS}s"
        ) from exc
    except OSError as exc:
        raise InventoryPreflightError(f"could not execute ansible-inventory: {exc}") from exc

    if result.returncode != 0:
        raise InventoryPreflightError(
            "ansible-inventory rejected the local inventory:\n"
            + _diagnostic(result.stderr, result.stdout)
        )
    try:
        payload = loads_strict_json(result.stdout)
    except json.JSONDecodeError as exc:
        raise InventoryPreflightError(
            "ansible-inventory did not return valid JSON:\n"
            + _diagnostic(result.stderr, result.stdout)
        ) from exc
    return validate_inventory_payload(payload, expected_hosts=expected_hosts)


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def prepare_inventory(
    inventory: Path,
    *,
    expected_hosts: int = DEFAULT_EXPECTED_HOSTS,
    parser: str = "ansible-inventory",
) -> list[str]:
    """Normalize harmless local formatting, then validate before Ansible runs."""

    if not inventory.exists():
        raise InventoryPreflightError(
            f"{_display_path(inventory)} is missing; run `make init-local`, then "
            "replace its example node addresses and SSH users"
        )
    if not inventory.is_file():
        raise InventoryPreflightError(
            f"{_display_path(inventory)} is not a regular file"
        )

    try:
        raw = read_bounded_bytes(inventory, max_bytes=1024 * 1024)
        normalized, changed = normalize_inventory_bytes(raw)
    except (OSError, ValueError) as exc:
        if isinstance(exc, InventoryPreflightError):
            raise
        raise InventoryPreflightError(
            f"could not read {_display_path(inventory)} safely: {exc}"
        ) from exc

    if changed:
        try:
            mode = inventory.stat().st_mode & 0o777
            atomic_write_text(
                inventory,
                normalized,
                encoding="utf-8",
                mode=mode or 0o600,
            )
        except OSError as exc:
            raise InventoryPreflightError(
                f"could not normalize {_display_path(inventory)} safely: {exc}"
            ) from exc

    hosts = parse_inventory(
        inventory,
        expected_hosts=expected_hosts,
        parser=parser,
    )
    if changed:
        print(f"Normalized local inventory encoding/line endings: {_display_path(inventory)}")
    return hosts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize and validate the private Ansible inventory."
    )
    parser.add_argument(
        "--inventory",
        type=Path,
        default=DEFAULT_INVENTORY,
        help="local Ansible inventory path",
    )
    parser.add_argument(
        "--expected-hosts",
        type=int,
        default=DEFAULT_EXPECTED_HOSTS,
        help="required number of hosts in rke2_servers (0 disables exact count)",
    )
    parser.add_argument(
        "--ansible-inventory",
        default="ansible-inventory",
        help="Ansible inventory executable",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.expected_hosts < 0:
        print("Inventory preflight failed: --expected-hosts cannot be negative", file=sys.stderr)
        return 2
    try:
        hosts = prepare_inventory(
            args.inventory,
            expected_hosts=args.expected_hosts,
            parser=args.ansible_inventory,
        )
    except InventoryPreflightError as exc:
        print(f"Inventory preflight failed: {exc}", file=sys.stderr)
        return 2

    print(f"Inventory preflight passed: rke2_servers={','.join(hosts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
