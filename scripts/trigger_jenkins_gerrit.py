#!/usr/bin/env python3
"""Explicitly queue one current Gerrit patch set on a reviewed Jenkins job."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import Request

from atomic_file import atomic_write_text
from bounded_file import read_bounded_text
from http_transport import open_http_request, read_bounded_response
from strict_json import loads_strict_json
from tekton_forge_bridge import private_input
import tekton_gerrit_bridge as gerrit
import verify_jenkins_gerrit as verifier

ROOT = Path(__file__).resolve().parents[1]


def reservation_key(plan):
    validate(plan)
    identity = {k: plan[k] for k in ("jenkins_origin", "job", "gerrit_origin", "project", "branch", "change", "patchset", "revision")}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def private_journal_directory(journal_dir):
    directory = Path(journal_dir)
    private = (ROOT / "private").resolve()
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir() or not directory.resolve().is_relative_to(private):
        raise ValueError("private-journal-directory-required")
    if os.name == "posix" and directory.stat().st_mode & 0o077: raise ValueError("private-journal-permissions-required")
    return directory


def validate(plan):
    if not isinstance(plan, dict) or set(plan) != {"schema_version", "jenkins_origin", "job", "job_config_sha256", "gerrit_origin", "project", "branch", "change", "patchset", "revision"}:
        raise ValueError("invalid-trigger-plan")
    verifier.validate({**plan, "build": 1})
    return plan


def submit(plan, jenkins_get, gerrit_get, post, journal_dir, *, apply=False):
    validate(plan)
    if type(apply) is not bool: raise ValueError("invalid-mode")
    job = "".join("/job/" + quote(part, safe="") for part in plan["job"])
    expected = {"change": plan["change"], "patchset": plan["patchset"], "revision": plan["revision"],
                "ref": gerrit.reference(plan["change"], plan["patchset"])}
    source = {"source": {"project": plan["project"], "branch": plan["branch"]}}
    path = "/a/changes/" + str(plan["change"]) + "?o=CURRENT_REVISION"
    def check():
        if hashlib.sha256(jenkins_get(job + "/config.xml", False)).hexdigest() != plan["job_config_sha256"]:
            raise ValueError("jenkins-job-config-changed")
        if gerrit.current(source, gerrit_get(path), status="NEW") != expected:
            raise ValueError("gerrit-patchset-changed")
    check()
    if not apply:
        return {"read_only": True, "preflight_verified": True, "queue_write_attempted": False}
    directory = private_journal_directory(journal_dir)
    name = reservation_key(plan)
    journal = directory / (name + ".json")
    descriptor = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"schema_version": 2, "state": "reserved", "identity_sha256": name, "job_config_sha256": plan["job_config_sha256"]}, stream, sort_keys=True)
        stream.flush(); os.fsync(stream.fileno())
    if os.name == "posix":
        parent = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(parent)
        finally: os.close(parent)
    # An interrupted request remains reserved: blindly retrying could enqueue
    # a duplicate build. The caller must inspect Jenkins before recovery.
    check()
    params = {"GERRIT_CHANGE_NUMBER": str(plan["change"]), "GERRIT_PATCHSET_NUMBER": str(plan["patchset"]),
              "GERRIT_PATCHSET_REVISION": plan["revision"], "GERRIT_REFSPEC": expected["ref"]}
    status, location = post(job + "/buildWithParameters", params)
    parsed = urlsplit(urljoin(plan["jenkins_origin"] + "/", location if isinstance(location, str) else ""))
    origin = urlsplit(plan["jenkins_origin"])
    match = re.fullmatch(r"/queue/item/([1-9][0-9]{0,18})/", parsed.path)
    if status != 201 or (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc) or parsed.query or parsed.fragment or not match:
        raise ValueError("jenkins-queue-acknowledgement-unverified")
    queue_id = int(match[1])
    atomic_write_text(journal, json.dumps({"schema_version": 2, "state": "queued", "identity_sha256": name,
                                         "job_config_sha256": plan["job_config_sha256"], "queue_id": queue_id}, sort_keys=True) + "\n")
    return {"read_only": False, "preflight_verified": True, "queue_write_attempted": True, "queue_id": queue_id,
            "build_verified": False, "review_vote_verified": False, "deployment_authorized": False}


def post_requester(origin, user, token):
    auth = "Basic " + base64.b64encode(user.encode("ascii") + b":" + token).decode("ascii")
    def post(path, params):
        request = Request(origin + path, data=urlencode(params).encode(), method="POST",
                          headers={"Authorization": auth, "Content-Type": "application/x-www-form-urlencoded"})
        with open_http_request(request, timeout=15, use_environment_proxy=False) as response:
            read_bounded_response(response, max_bytes=65536)
            return response.status, response.headers.get("Location")
    return post


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--journal-dir", type=Path, required=True); parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        plan = validate(loads_strict_json(read_bounded_text(args.plan, max_bytes=16384)))
        users = [private_input(name, minimum=1, maximum=100, root=args.input_dir).decode("ascii") for name in ("jenkins-user", "gerrit-user")]
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", user) for user in users): raise ValueError("invalid-user")
        tokens = [private_input(name, minimum=16, maximum=512, root=args.input_dir) for name in ("jenkins-token", "gerrit-token")]
        if any(any(c < 33 or c > 126 for c in token) for token in tokens): raise ValueError("invalid-token")
        private_input("ca.crt", maximum=256 * 1024, root=args.input_dir)
        os.environ["SSL_CERT_FILE"] = str(args.input_dir / "ca.crt")
        result = submit(plan, verifier.requester(plan["jenkins_origin"], users[0], tokens[0]),
                        verifier.requester(plan["gerrit_origin"], users[1], tokens[1], prefix=True),
                        post_requester(plan["jenkins_origin"], users[0], tokens[0]), args.journal_dir, apply=args.apply)
    except Exception:
        print("Jenkins trigger stopped; inspect the private reservation and Jenkins queue before retrying.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True)); return 0


if __name__ == "__main__": raise SystemExit(main())
