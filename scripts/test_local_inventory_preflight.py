#!/usr/bin/env python3
"""Behavior-test the local inventory preflight and safe normalization."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
from unittest import mock

from prepare_local_inventory import (
    InventoryPreflightError,
    normalize_inventory_bytes,
    parse_inventory,
    prepare_inventory,
    validate_inventory_payload,
)


def valid_payload() -> dict[str, object]:
    hosts = ["node-1", "node-2", "node-3"]
    return {
        "rke2_servers": {"hosts": hosts},
        "_meta": {
            "hostvars": {
                host: {"ansible_host": f"172.16.134.{index + 43}"}
                for index, host in enumerate(hosts)
            }
        },
    }


def test_normalization() -> None:
    normalized, changed = normalize_inventory_bytes(
        b"\xef\xbb\xbf[rke2_servers]\r\nnode-1\rnode-2"
    )
    if normalized != "[rke2_servers]\nnode-1\nnode-2\n" or not changed:
        raise AssertionError("inventory normalization did not remove BOM and normalize lines")

    same, changed = normalize_inventory_bytes(b"[rke2_servers]\nnode-1\n")
    if same != "[rke2_servers]\nnode-1\n" or changed:
        raise AssertionError("already normalized inventory was rewritten")


def test_payload_contract() -> None:
    if validate_inventory_payload(valid_payload()) != ["node-1", "node-2", "node-3"]:
        raise AssertionError("valid inventory payload was rejected")

    invalid = valid_payload()
    invalid["_meta"]["hostvars"]["node-2"] = {"ansible_host": "<NODE_2_IP>"}
    try:
        validate_inventory_payload(invalid)
    except InventoryPreflightError as exc:
        if "node-2" not in str(exc) or "placeholder" not in str(exc):
            raise AssertionError(f"placeholder diagnostic was incomplete: {exc}")
    else:
        raise AssertionError("placeholder inventory was accepted")

    invalid_group = valid_payload()
    invalid_group["rke2_servers"]["hosts"] = ["node-1"]
    try:
        validate_inventory_payload(invalid_group)
    except InventoryPreflightError as exc:
        if "exactly 3" not in str(exc):
            raise AssertionError(f"host-count diagnostic was incomplete: {exc}")
    else:
        raise AssertionError("short inventory was accepted")


def test_parser_and_normalizer() -> None:
    payload = valid_payload()
    completed = subprocess.CompletedProcess(
        ["ansible-inventory"],
        0,
        stdout=json.dumps(payload),
        stderr="",
    )
    with mock.patch("prepare_local_inventory.run_bounded", return_value=completed) as run:
        with tempfile.TemporaryDirectory(prefix="platform-inventory-") as temporary_name:
            inventory = Path(temporary_name) / "hosts.local.ini"
            inventory.write_bytes(b"\xef\xbb\xbf[rke2_servers]\r\n")
            hosts = prepare_inventory(inventory)
            if hosts != ["node-1", "node-2", "node-3"]:
                raise AssertionError("normalized inventory returned the wrong hosts")
            if inventory.read_bytes() != b"[rke2_servers]\n":
                raise AssertionError("normalized inventory was not written atomically")
            run.assert_called_once()


def test_parser_failure_is_actionable() -> None:
    completed = subprocess.CompletedProcess(
        ["ansible-inventory"],
        1,
        stdout="",
        stderr="Unable to parse /tmp/hosts.local.ini",
    )
    with mock.patch("prepare_local_inventory.run_bounded", return_value=completed):
        with tempfile.TemporaryDirectory(prefix="platform-inventory-") as temporary_name:
            inventory = Path(temporary_name) / "hosts.local.ini"
            inventory.write_text("[broken", encoding="utf-8")
            try:
                parse_inventory(inventory)
            except InventoryPreflightError as exc:
                if "ansible-inventory rejected" not in str(exc):
                    raise AssertionError(f"parser failure was not classified: {exc}")
            else:
                raise AssertionError("parser failure was accepted")


def test_missing_inventory() -> None:
    with tempfile.TemporaryDirectory(prefix="platform-inventory-") as temporary_name:
        inventory = Path(temporary_name) / "missing.ini"
        try:
            prepare_inventory(inventory)
        except InventoryPreflightError as exc:
            if "make init-local" not in str(exc):
                raise AssertionError(f"missing-inventory guidance was incomplete: {exc}")
        else:
            raise AssertionError("missing inventory was accepted")


def main() -> int:
    test_normalization()
    test_payload_contract()
    test_parser_and_normalizer()
    test_parser_failure_is_actionable()
    test_missing_inventory()
    print("Local inventory preflight self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
