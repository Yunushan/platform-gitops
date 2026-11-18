#!/usr/bin/env python3
"""Resolve a pinned public Jenkins update-center snapshot for an offline fixture."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from bounded_file import read_bounded_bytes
from strict_json import loads_strict_json


METADATA_SHA256 = "c10b5d5877144db67c29b8ff3f473a228299abb40bf29f4d64f313df12e23087"


def resolve(path):
    raw = read_bounded_bytes(path, max_bytes=32 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != METADATA_SHA256: raise ValueError("unexpected-update-metadata")
    plugins = loads_strict_json(raw)["plugins"]
    names, pending = set(), ["git"]
    while pending:
        name = pending.pop()
        if name in names: continue
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", name) or name not in plugins:
            raise ValueError("invalid-plugin-dependency")
        names.add(name)
        if len(names) > 100: raise ValueError("too-many-plugins")
        for dep in plugins[name].get("dependencies", []):
            if dep.get("optional") is not True: pending.append(dep["name"])
    result = {}
    for name in sorted(names):
        item = plugins[name]
        version, url = item["version"], item["url"]
        if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", version): raise ValueError("invalid-plugin-version")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "updates.jenkins.io" or parsed.query or parsed.fragment
                or parsed.path != "/download/plugins/" + name + "/" + version + "/" + name + ".hpi"):
            raise ValueError("invalid-plugin-url")
        digest = base64.b64decode(item["sha256"], validate=True)
        if len(digest) != 32: raise ValueError("invalid-plugin-digest")
        result[name] = {"version": version, "url": url, "sha256": digest.hex(), "required_core": item.get("requiredCore")}
    return {"schema_version": 1, "metadata_sha256": METADATA_SHA256, "plugins": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, required=True)
    args = parser.parse_args()
    try: result = resolve(args.metadata)
    except Exception:
        print("Fixture plugin metadata validation failed.", file=sys.stderr); return 1
    print(json.dumps(result, sort_keys=True, indent=2)); return 0


if __name__ == "__main__": sys.exit(main())
