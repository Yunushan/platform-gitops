#!/usr/bin/env python3
"""Build the reviewed Patroni wheel with ZooKeeper hostname verification."""

import argparse
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

from atomic_file import atomic_write_bytes, atomic_write_text
from bounded_file import read_bounded_bytes
from postgres_ha_policy import STRICT_PATRONI_DCS


ROOT = Path(__file__).resolve().parents[1]
RELEASE = "4.1.5"
WHEEL = f"patroni-{RELEASE}-py3-none-any.whl"
SOURCE_SHA256 = "7aad8c1539e6c51c269a8005169dd54ddc7668437b7900fa6099d7894e224c84"
DCS = "patroni/dcs/zookeeper.py"
DIST = f"patroni-{RELEASE}.dist-info"


def patch_dcs(source):
    needle = b"        kwargs = {v: config[k] for k, v in mapping.items() if k in config}\n"
    patched = source.replace(needle, needle + b"        if kwargs.get('use_ssl'):\n            kwargs['check_hostname'] = True\n")
    if source.count(needle) != 1 or hashlib.sha256(patched).hexdigest() != STRICT_PATRONI_DCS[RELEASE]:
        raise ValueError("Patroni ZooKeeper source differs from the reviewed upstream release")
    return patched


def build(source, output):
    payload = read_bounded_bytes(source, max_bytes=4 * 1024 * 1024)
    if hashlib.sha256(payload).hexdigest() != SOURCE_SHA256:
        raise ValueError("Require the pinned upstream Patroni 4.1.5 wheel")
    if not output.resolve().is_relative_to((ROOT / "private").resolve()):
        raise ValueError("Place the local candidate under private/")
    with zipfile.ZipFile(io.BytesIO(payload)) as original:
        files = {item.filename: original.read(item) for item in original.infolist() if not item.is_dir()}
    files[DCS] = patch_dcs(files[DCS])
    files[f"{DIST}/PLATFORM_ZOOKEEPER_TLS.txt"] = (
        f"Patroni {RELEASE} downstream build with ZooKeeper TLS hostname verification.\n"
        f"Original upstream wheel SHA-256: {SOURCE_SHA256}\n"
    ).encode()
    record_name = f"{DIST}/RECORD"
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for name, content in sorted(files.items()):
        if name != record_name:
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
            writer.writerow((name, "sha256=" + digest, len(content)))
    writer.writerow((record_name, "", ""))
    files[record_name] = record.getvalue().encode()
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as candidate:
        for name, content in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            candidate.writestr(entry, content)
    report = {"release": RELEASE, "source_wheel_sha256": SOURCE_SHA256,
              "downstream_wheel_sha256": hashlib.sha256(data.getbuffer()).hexdigest(),
              "dcs_source_sha256": STRICT_PATRONI_DCS[RELEASE]}
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    atomic_write_bytes(output / WHEEL, data.getvalue(), replace=False)
    atomic_write_text(output / "build-report.json", json.dumps(report, sort_keys=True) + "\n", replace=False)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-wheel", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.source_wheel, args.output_dir), sort_keys=True))
    except (OSError, ValueError, zipfile.BadZipFile):
        print("Patroni wheel build refused; review the upstream wheel and the private output path.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
