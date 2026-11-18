#!/usr/bin/env python3
"""Optionally create a Gitea OAuth Secret from private files; never rotate it."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import stat
import sys

from bounded_file import read_bounded_text
from kubernetes_management import Client, validate_target, verify_target
from strict_json import loads_strict_json

KEYS = {"client-id": "WOODPECKER_GITEA_CLIENT", "client-secret": "WOODPECKER_GITEA_SECRET"}


def validate(plan):
    if not isinstance(plan, dict) or set(plan) != {"schema_version", "target", "namespace", "namespace_uid", "secret_name"} or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("invalid-plan")
    validate_target(plan["target"])
    for key in ("namespace", "secret_name"):
        if not isinstance(plan[key], str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", plan[key]):
            raise ValueError("invalid-name")
    if plan["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}: raise ValueError("dedicated-namespace-required")
    if not isinstance(plan["namespace_uid"], str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", plan["namespace_uid"]):
        raise ValueError("namespace-uid-required")
    return plan


def read_inputs(directory):
    if os.name != "posix": raise ValueError("private-posix-files-required")
    directory = Path(directory)
    state = directory.lstat()
    if not stat.S_ISDIR(state.st_mode) or state.st_uid != os.geteuid() or state.st_mode & 0o077:
        raise ValueError("private-input-directory-required")
    data = {}
    for filename, key in KEYS.items():
        fd = os.open(directory / filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > 4096:
                raise ValueError("private-input-file-required")
            with os.fdopen(fd, "rb", closefd=False) as stream: value = stream.read(4097).rstrip(b"\r\n")
            if not 1 <= len(value) <= 4096 or any(ch < 33 or ch > 126 for ch in value): raise ValueError("invalid-oauth-value")
            data[key] = base64.b64encode(value).decode("ascii")
        finally: os.close(fd)
    return data


def provision(client, plan, data, *, apply=False):
    validate(plan)
    if type(apply) is not bool or set(data) != set(KEYS.values()): raise ValueError("invalid-provision-request")
    verify_target(client, plan["target"])
    namespace = client.get("namespace/" + plan["namespace"])
    if not namespace or namespace.get("metadata", {}).get("uid") != plan["namespace_uid"] or namespace["metadata"].get("deletionTimestamp"):
        raise ValueError("namespace-identity-changed")
    name = "secret/" + plan["secret_name"]
    current = client.get(name, plan["namespace"], optional=True)
    if current is None and apply:
        payload = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": plan["secret_name"], "namespace": plan["namespace"]}, "type": "Opaque", "data": data}
        try: client.call(["create", "-f", "-", "-o", "json"], payload=json.dumps(payload).encode())
        except ValueError: pass
        current = client.get(name, plan["namespace"], optional=True)
    if current is not None and (current.get("type") != "Opaque" or current.get("data") != data or current.get("metadata", {}).get("deletionTimestamp")):
        raise ValueError("existing-secret-differs")
    if apply and current is None: raise ValueError("secret-creation-not-confirmed")
    return {"read_only": not apply, "secret_present_and_matching": current is not None, "credentials_printed": False, "oauth_login_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--check-server", action="store_true"); parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        if args.apply and args.check_server: raise ValueError("select-one-mode")
        plan = validate(loads_strict_json(read_bounded_text(args.plan, max_bytes=16384)))
        data = read_inputs(args.input_dir)
        if not (args.apply or args.check_server): result = {"local_inputs_verified": True, "read_only": True, "oauth_login_verified": False}
        else:
            with Client(plan["target"]).frozen() as client: result = provision(client, plan, data, apply=args.apply)
    except Exception:
        print("Gitea OAuth Secret provisioning stopped; inspect private inputs and target state.", file=sys.stderr); return 1
    print(json.dumps(result, sort_keys=True)); return 0


if __name__ == "__main__": sys.exit(main())
