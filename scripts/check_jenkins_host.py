#!/usr/bin/env python3
"""Read-only prerequisite checks for a rendered native Jenkins host plan."""
import argparse
import json
import os
from pathlib import Path
import sys

from bounded_file import read_bounded_bytes, read_bounded_text
from strict_json import loads_strict_json
import activate_keycloak_host as host
import render_jenkins_host as renderer


def check(plan):
    if sys.platform != "linux": raise ValueError("linux-required")
    import grp
    import pwd
    files = renderer.render(plan)
    if plan["runtime"] != "raw": raise ValueError("native-plan-required")
    account, group = pwd.getpwnam(plan["user"]), grp.getgrnam(plan["group"])
    if account.pw_uid <= 0 or group.gr_gid <= 0 or account.pw_gid != group.gr_gid:
        raise ValueError("invalid-service-identity")
    identity = {"service_uid": account.pw_uid, "service_gid": group.gr_gid}
    home = Path(plan["home"])
    home_info = host.trusted(home, owner=account.pw_uid, directory=True)
    if home_info.st_gid != group.gr_gid or not home_info.st_mode & 0o200:
        raise ValueError("service-home-not-writable")
    host.readable(home, identity)
    java = Path(plan["java"]); war = Path(plan["war"]); checksum = Path(plan["checksum_file"])
    for path in (java, war, checksum): host.trusted(path)
    host.readable(java, identity, executable=True)
    host.readable(war, identity)
    host.readable(checksum, identity)
    if host.file_hash(war) != plan["war_sha256"]: raise ValueError("war-digest-mismatch")
    if read_bounded_bytes(checksum, max_bytes=1024) != files["jenkins.war.sha256"].encode():
        raise ValueError("checksum-file-mismatch")
    return {"read_only": True, "service_identity_verified": True, "home_permissions_verified": True,
            "war_sha256_verified": True, "checksum_file_verified": True, "java_path_permissions_verified": True,
            "java_version_verified": False, "jenkins_started": False, "recovery_verified": False, "full_stack_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    try: result = check(loads_strict_json(read_bounded_text(args.plan, max_bytes=16384)))
    except Exception:
        print("Native Jenkins preflight failed; review the staged files and service identity.", file=sys.stderr); return 1
    print(json.dumps(result, sort_keys=True)); return 0


if __name__ == "__main__": sys.exit(main())
