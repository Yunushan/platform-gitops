#!/usr/bin/env python3
"""Render opt-in systemd units for the Jenkins/Gerrit pollers; never install."""
import argparse
import json
from pathlib import Path
import re
import sys

from bounded_file import read_bounded_text
from render_jenkins_host import host_path
from strict_json import loads_strict_json


def render(plan):
    fields = {"schema_version", "root", "user", "group", "queue_dir", "review_dir", "trigger_policy", "result_policy",
              "trigger_inputs", "result_inputs", "trigger_sha256", "result_sha256", "interval_seconds"}
    if not isinstance(plan, dict) or set(plan) != fields or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("invalid-poller-plan")
    for key in ("root", "queue_dir", "review_dir", "trigger_policy", "result_policy", "trigger_inputs", "result_inputs"):
        if not host_path(plan[key]): raise ValueError("invalid-poller-path")
    if plan["root"].split("/")[1] not in {"opt", "srv", "var"}:
        raise ValueError("use-service-root-outside-protected-home")
    private = plan["root"] + "/private/"
    if any(not plan[key].startswith(private) for key in ("queue_dir", "review_dir", "trigger_policy", "result_policy", "trigger_inputs", "result_inputs")):
        raise ValueError("private-poller-path-required")
    def overlaps(left, right):
        return left == right or left.startswith(right + "/") or right.startswith(left + "/")
    if overlaps(plan["queue_dir"], plan["review_dir"]): raise ValueError("separate-review-directory-required")
    for state in (plan["queue_dir"], plan["review_dir"]):
        if any(overlaps(state, plan[key]) for key in ("trigger_policy", "result_policy", "trigger_inputs", "result_inputs")):
            raise ValueError("writable-state-must-not-overlap-inputs")
    if overlaps(plan["trigger_inputs"], plan["result_inputs"]):
        raise ValueError("separate-credential-directories-required")
    for key in ("user", "group"):
        if not isinstance(plan[key], str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", plan[key]) or plan[key] == "root":
            raise ValueError("nonroot-poller-account-required")
    for key in ("trigger_sha256", "result_sha256"):
        if not isinstance(plan[key], str) or not re.fullmatch(r"[0-9a-f]{64}", plan[key]): raise ValueError("invalid-policy-digest")
    if type(plan["interval_seconds"]) is not int or not 10 <= plan["interval_seconds"] <= 3600:
        raise ValueError("invalid-poll-interval")
    base = ("[Unit]\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\n"
            f"User={plan['user']}\nGroup={plan['group']}\nWorkingDirectory={plan['root']}\n"
            "UMask=0077\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=yes\n"
            f"ReadWritePaths={plan['queue_dir']} {plan['review_dir']}\n"
            "RestrictSUIDSGID=yes\nCapabilityBoundingSet=\nLockPersonality=yes\n"
            "Restart=no\nTasksMax=64\nMemoryMax=512M\n")
    suffix = "\n\n[Install]\nWantedBy=multi-user.target\n"
    interval = str(plan["interval_seconds"])
    trigger = (f"ExecStart=/usr/bin/python3 -B {plan['root']}/scripts/scan_jenkins_gerrit.py"
               f" --policy {plan['trigger_policy']} --policy-sha256 {plan['trigger_sha256']}"
               f" --input-dir {plan['trigger_inputs']} --journal-dir {plan['queue_dir']} --watch --interval-seconds {interval} --apply")
    result = (f"ExecStart=/usr/bin/python3 -B {plan['root']}/scripts/scan_jenkins_gerrit_results.py"
              f" --policy {plan['result_policy']} --policy-sha256 {plan['result_sha256']}"
              f" --input-dir {plan['result_inputs']} --queue-dir {plan['queue_dir']} --review-dir {plan['review_dir']}"
              f" --watch --interval-seconds {interval} --apply")
    return {"jenkins-gerrit-trigger.service": "[Unit]\nDescription=Jenkins Gerrit trigger poller\n" + base[len("[Unit]\n"): ] + trigger + suffix,
            "jenkins-gerrit-results.service": "[Unit]\nDescription=Jenkins Gerrit result poller\n" + base[len("[Unit]\n"): ] + result + suffix}


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    try: files = render(loads_strict_json(read_bounded_text(args.plan, max_bytes=16384)))
    except Exception:
        print("Poller rendering failed; review the private plan.", file=sys.stderr); return 1
    print(json.dumps(files, sort_keys=True)); return 0


if __name__ == "__main__": sys.exit(main())
