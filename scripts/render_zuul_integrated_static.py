#!/usr/bin/env python3
"""Prepare review-only Zuul 14 static-provider fragments, without deploying them."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import RUNTIMES, write_bundle
import render_nodepool_static as inventory
from strict_json import loads_strict_json

ROOT = Path(__file__).resolve().parents[1]


def fragments(plan: dict, zuul_plan: dict) -> dict[str, str]:
    """Return one private trusted-project fragment per tenant.

    This is not a launcher deployment or a verified working configuration.
    Callers must validate the whole Zuul layout before publishing it.
    """
    inventory.validate(plan, zuul_plan)
    if zuul_plan["zuul_version"] != "14.3.0":
        raise ValueError("Integrated static fragments require the reviewed Zuul 14.3.0 release")
    projects: set[str] = set()
    for tenant in zuul_plan["tenants"]:
        for project in tenant["config_projects"]:
            if project in projects:
                raise ValueError("Trusted projects must not be shared between tenants")
            projects.add(project)
    result = {}
    for tenant in zuul_plan["tenants"]:
        objects = []
        pools = [p for p in plan["pools"] if p["tenant"] == tenant["name"]]
        for pool in pools:
            name = pool["name"]
            label = pool["label"]
            image, flavor, section = (f"{name}-image", f"{name}-flavor", f"{name}-section")
            objects.extend([
                {"image": {"name": image, "type": "cloud"}},
                {"flavor": {"name": flavor}},
                {"label": {"name": label, "image": image, "flavor": flavor}},
                {"section": {"name": section, "connection": "platform-static", "flavors": [{"name": flavor}]}},
                {"provider": {"name": name, "section": section,
                    "labels": [{"name": label, "slots": 1, "host-key-checking": True}],
                    "images": [{"name": image}],
                    "nodes": [{"name": n["host"], "label": label, "username": n["username"],
                               "connection-port": n["port"], "host-key": n["host_key"]}
                              for n in pool["nodes"]]}},
            ])
        result[tenant["name"]] = yaml.safe_dump(objects, sort_keys=False)
    return result


def render(plan: dict, zuul_plan: dict, runtime: str) -> dict[str, str]:
    if runtime not in RUNTIMES:
        raise ValueError("Unknown target runtime")
    per_tenant = fragments(plan, zuul_plan)
    files = {f"tenant-{tenant}.yaml": content for tenant, content in per_tenant.items()}
    files["render-status.json"] = json.dumps({
        "component": "zuul-integrated-static-fragments", "runtime": runtime,
        "rendered": True, "deployed": False, "configuration_loaded": False,
        "allocation_verified": False, "ha_verified": False,
        "required_connection": "platform-static", "required_driver": "static",
        "remaining": ["trusted-project-review-and-publication", "launcher-runtime",
                      "zuul-config-load", "worker-authentication-and-isolation",
                      "allocation-concurrency-node-loss-and-restore-tests"],
    }, indent=2) + "\n"
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--zuul-plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        ci = loads_strict_json(read_bounded_text(args.zuul_plan, encoding="utf-8"))
        write_bundle(render(plan, ci, args.runtime), args.output_dir, private_root=ROOT / "private")
    except (ValueError, OSError, RuntimeError):
        print("Integrated static render failed; inspect the private plans and output directory.", file=sys.stderr)
        return 1
    print(json.dumps({"rendered": True, "deployed": False, "configuration_loaded": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
