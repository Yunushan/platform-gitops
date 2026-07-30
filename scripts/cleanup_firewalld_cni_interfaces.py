#!/usr/bin/env python3
"""Remove transient CNI interface bindings from a firewalld zone file."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from bounded_file import read_bounded_bytes


DEFAULT_ZONE_PATH = Path("/etc/firewalld/zones/trusted.xml")
STABLE_INTERFACES = {
    "cilium_geneve",
    "cilium_health",
    "cilium_host",
    "cilium_net",
    "cilium_vxlan",
    "cni0",
}
TRANSIENT_INTERFACE_RE = re.compile(r"^(?:lxc|veth|cni|cilium).+")
STABLE_CILIUM_WIREGUARD_RE = re.compile(r"^cilium_wg[0-9]+$")


@dataclass(frozen=True)
class CleanupResult:
    changed: bool
    removed: int
    interfaces_before: int
    interfaces_after: int


def is_transient_interface(name: str) -> bool:
    """Return true for per-workload CNI links that must not be persisted."""
    if name in STABLE_INTERFACES or STABLE_CILIUM_WIREGUARD_RE.fullmatch(name):
        return False
    return TRANSIENT_INTERFACE_RE.fullmatch(name) is not None


def _atomic_write(tree: ET.ElementTree, path: Path, include_declaration: bool) -> None:
    source_stat = path.stat()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            tree.write(
                stream,
                encoding="utf-8",
                xml_declaration=include_declaration,
                short_empty_elements=True,
            )
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copystat(path, temporary_path, follow_symlinks=False)
        if hasattr(os, "chown"):
            os.chown(temporary_path, source_stat.st_uid, source_stat.st_gid)
        os.replace(temporary_path, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


def cleanup_zone_file(path: Path) -> CleanupResult:
    """Prune transient interface elements and atomically update the zone."""
    if not path.exists():
        return CleanupResult(False, 0, 0, 0)

    raw = read_bounded_bytes(path)
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    root = ET.fromstring(raw, parser=parser)
    tree = ET.ElementTree(root)
    interfaces = list(root.findall("interface"))
    transient = [
        element
        for element in interfaces
        if is_transient_interface(element.attrib.get("name", ""))
    ]
    if not transient:
        return CleanupResult(False, 0, len(interfaces), len(interfaces))

    for element in transient:
        root.remove(element)
    _atomic_write(tree, path, raw.lstrip().startswith(b"<?xml"))
    return CleanupResult(True, len(transient), len(interfaces), len(interfaces) - len(transient))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=DEFAULT_ZONE_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = cleanup_zone_file(args.path)
    except (OSError, ET.ParseError) as exc:
        print(f"firewalld_ephemeral_interface_cleanup=failed error={exc}", file=sys.stderr)
        return 1

    state = "changed" if result.changed else "unchanged"
    print(
        f"firewalld_ephemeral_interface_cleanup={state} "
        f"removed={result.removed} "
        f"interfaces_before={result.interfaces_before} "
        f"interfaces_after={result.interfaces_after}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
