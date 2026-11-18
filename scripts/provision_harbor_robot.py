#!/usr/bin/env python3
"""Create one expiring project pull/push robot; retain uncertain writes privately."""

import argparse
import base64
from datetime import datetime
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import stat
import sys
import time
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
from deploy_keycloak_ha import lock
from http_transport import read_bounded_response
from strict_json import loads_strict_json

ROOT = Path(__file__).resolve().parents[1]
LABEL = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*")
SHA = re.compile(r"[0-9a-f]{64}")
ACCESS = [{"resource": "repository", "action": action, "effect": "allow"} for action in ("pull", "push")]


def canonical(value): return json.dumps(value, sort_keys=True, separators=(",", ":"))
def fingerprint(value): return hashlib.sha256(canonical(value).encode()).hexdigest()
def positive(value): return type(value) is int and 0 < value < 2 ** 63


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys.split()): raise ValueError("Missing or unreviewed robot fields")


def validate(plan):
    exact(plan, "schema_version server project robot")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1: raise ValueError("Unsupported robot plan")
    server = plan["server"]; exact(server, "origin harbor_version ca_file ca_sha256 leaf_sha256 admin_username password_env")
    value = server["origin"]
    if not isinstance(value, str) or len(value) > 512: raise ValueError("Use a canonical HTTPS origin")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", parsed.hostname)
            or parsed.path or parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None
            or parsed.netloc != parsed.hostname + (":" + str(parsed.port) if parsed.port is not None else "")
            or parsed.port is not None and not 1 <= parsed.port <= 65535): raise ValueError("Use a credential-free canonical HTTPS origin")
    if not isinstance(server["harbor_version"], str) or not re.fullmatch(r"v2[.]15[.][0-9]+(?:-poolhealth[.]local)?-[A-Za-z0-9-]{8,64}", server["harbor_version"]):
        raise ValueError("Pin a reviewed Harbor 2.15 build identity")
    for key in ("ca_sha256", "leaf_sha256"):
        if not isinstance(server[key], str) or not SHA.fullmatch(server[key]): raise ValueError("Pin CA and leaf identities")
    if not isinstance(server["ca_file"], str) or not Path(server["ca_file"]).is_absolute(): raise ValueError("Use a private absolute CA path")
    if not isinstance(server["admin_username"], str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", server["admin_username"]): raise ValueError("Use an explicit management identity")
    if not isinstance(server["password_env"], str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", server["password_env"]): raise ValueError("Reference a management password environment variable")
    project = plan["project"]; exact(project, "name id")
    if not isinstance(project["name"], str) or not LABEL.fullmatch(project["name"]) or len(project["name"]) > 63 or not positive(project["id"]): raise ValueError("Pin one private project's name and numeric identity")
    robot = plan["robot"]; exact(robot, "name username duration_days")
    if (not isinstance(robot["name"], str) or not LABEL.fullmatch(robot["name"]) or len(robot["name"]) > 40
            or robot["username"] != "robot$" + project["name"] + "+" + robot["name"]
            or type(robot["duration_days"]) is not int or not 1 <= robot["duration_days"] <= 90): raise ValueError("Use an explicit project robot with a bounded lifetime")


def permission(plan): return [{"kind": "project", "namespace": plan["project"]["name"], "access": ACCESS}]
def marker(operation, plan): return "platform.gitops/harbor-robot/" + operation + "/" + fingerprint(plan)
def payload(plan, operation):
    return {"name": plan["robot"]["name"], "description": marker(operation, plan), "duration": plan["robot"]["duration_days"],
        "level": "project", "disable": False, "permissions": permission(plan)}


def basic(username, password):
    if not isinstance(password, str) or not 8 <= len(password) <= 1024 or any(ord(c) < 32 or ord(c) == 127 for c in password): raise ValueError("Management or robot credential missing or malformed")
    return "Basic " + base64.b64encode((username + ":" + password).encode()).decode()


class Client:
    def __init__(self, plan, password=None):
        validate(plan); self.plan = plan; self.server = plan["server"]; self.origin = urlsplit(self.server["origin"])
        path = Path(self.server["ca_file"])
        if path.is_symlink() or hashlib.sha256(read_bounded_bytes(path, max_bytes=2 * 1024 ** 2)).hexdigest() != self.server["ca_sha256"]: raise ValueError("Original CA changed")
        self.trust = ssl.create_default_context(cafile=str(path)); self.trust.minimum_version = ssl.TLSVersion.TLSv1_2
        self.auth = basic(self.server["admin_username"], os.environ.get(self.server["password_env"], "") if password is None else password)
        # Harbor's q parser unescapes again, turning a literal '+' in names
        # into a space. Enumerate only the pinned project, with complete bounds.
        self.collections = ["/api/v2.0/robots?" + urlencode({"q": "Level=project,ProjectID=" + str(plan["project"]["id"]),
            "sort": "id", "page": page, "page_size": 100}) for page in range(1, 11)]
        self.collection = self.collections[0]
        self.token_path = "/service/token?" + urlencode({"service": "harbor-registry", "scope": "repository:" + plan["project"]["name"] + "/ci-credential-check:pull,push"})

    def request(self, method, path, body=None, authorization=None):
        allowed = {"/api/v2.0/systeminfo", "/api/v2.0/projects/" + str(self.plan["project"]["id"]), *self.collections, self.token_path}
        if method == "GET":
            if path not in allowed and not re.fullmatch(r"/api/v2[.]0/robots/[1-9][0-9]{0,18}", path): raise ValueError("Unreviewed management read")
            if body is not None: raise ValueError("Read requests cannot carry mutations")
        elif method == "POST" and path == "/api/v2.0/robots":
            if not isinstance(body, dict): raise ValueError("Only one reviewed project robot may be created")
            description = body.get("description", "")
            operation = description.split("/")[-2] if isinstance(description, str) and "/" in description else ""
            if not re.fullmatch(r"[0-9a-f]{32}", operation) or canonical(body) != canonical(payload(self.plan, operation)): raise ValueError("Robot creation scope changed")
        else: raise ValueError("No update, delete, rotation or unrelated create operation is allowed")
        if authorization is not None and (not isinstance(authorization, str) or len(authorization) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in authorization)):
            raise ValueError("Bound the original authorization header")
        owner = self
        class Pinned(http.client.HTTPSConnection):
            def connect(self):
                super().connect()
                if hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest() != owner.server["leaf_sha256"]:
                    self.close(); raise ValueError("Original Harbor TLS identity changed")
        connection = Pinned(self.origin.hostname, self.origin.port or 443, context=self.trust, timeout=10)
        raw_body = None if body is None else canonical(body).encode()
        if raw_body is not None and len(raw_body) > 16384: raise ValueError("Bound robot request")
        try:
            connection.request(method, path, body=raw_body, headers={"Authorization": self.auth if authorization is None else authorization,
                "Content-Type": "application/json", "Accept": "application/json"})
            response = connection.getresponse()
            if response.status != (201 if method == "POST" else 200): raise ValueError("Harbor operation refused; API details retained privately by the caller")
            raw = read_bounded_response(response, max_bytes=1024 ** 2)
            return response.status, {k.lower(): v for k, v in response.getheaders()}, loads_strict_json(raw)
        except (OSError, http.client.HTTPException): raise ValueError("Harbor request outcome unavailable; do not repeat a creation") from None
        finally: connection.close()

    def ready(self):
        _, _, info = self.request("GET", "/api/v2.0/systeminfo")
        if not isinstance(info, dict) or info.get("harbor_version") != self.server["harbor_version"]: raise ValueError("Reviewed Harbor build identity changed")
        _, _, project = self.request("GET", "/api/v2.0/projects/" + str(self.plan["project"]["id"]))
        if (not isinstance(project, dict) or type(project.get("project_id")) is not int or project["project_id"] != self.plan["project"]["id"]
                or project.get("name") != self.plan["project"]["name"] or project.get("metadata", {}).get("public") != "false"
                or project.get("registry_id", 0) != 0): raise ValueError("Pinned private project changed or became a proxy project")

    def lookup(self):
        total = None; matches = []; previous_id = 0
        for page, path in enumerate(self.collections):
            _, headers, rows = self.request("GET", path)
            count = headers.get("x-total-count")
            if not isinstance(count, str) or not re.fullmatch(r"0|[1-9][0-9]{0,3}", count) or int(count) > 1000:
                raise ValueError("Bound the complete project robot inventory")
            if total is None: total = int(count)
            if total != int(count) or not isinstance(rows, list) or len(rows) != min(100, max(0, total - page * 100)):
                raise ValueError("Robot lookup is incomplete or changed during enumeration")
            for row in rows:
                if (not isinstance(row, dict) or not positive(row.get("id")) or row["id"] <= previous_id
                        or not isinstance(row.get("name"), str) or not 1 <= len(row["name"]) <= 512):
                    raise ValueError("Robot inventory identities are duplicate, unordered or malformed")
                previous_id = row["id"]
                if row["name"] == self.plan["robot"]["username"]: matches.append(row)
            if (page + 1) * 100 >= total:
                if len(matches) > 1: raise ValueError("Robot name is ambiguous")
                return matches[0] if matches else None
        raise ValueError("Project robot enumeration exceeded its reviewed bound")

    def robot(self, identifier):
        if not positive(identifier): raise ValueError("Original robot identity required")
        return self.request("GET", "/api/v2.0/robots/" + str(identifier))[2]

    def create(self, operation): return self.request("POST", "/api/v2.0/robots", payload(self.plan, operation))[2]

    def authenticate(self, credential):
        _, _, document = self.request("GET", self.token_path, authorization=basic(credential["username"], credential["secret"]))
        token = document.get("token") if isinstance(document, dict) else None
        if not isinstance(token, str) or len(token) > 32768 or not re.fullmatch(r"[A-Za-z0-9_-]+[.][A-Za-z0-9_-]+[.][A-Za-z0-9_-]+", token): raise ValueError("Robot token response changed")
        header, claims = [loads_strict_json(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))) for p in token.split(".")[:2]]
        access = claims.get("access") if isinstance(claims, dict) else None
        scope_ok = (isinstance(access, list) and len(access) == 1 and isinstance(access[0], dict)
            and set(access[0]) == {"type", "name", "actions"} and access[0].get("type") == "repository"
            and access[0].get("name") == self.plan["project"]["name"] + "/ci-credential-check"
            and isinstance(access[0].get("actions"), list) and sorted(access[0]["actions"]) == ["pull", "push"])
        if (not isinstance(header, dict) or header.get("alg") != "RS256" or not isinstance(claims, dict)
                or claims.get("sub") != credential["username"] or claims.get("aud") not in ("harbor-registry", ["harbor-registry"])
                or not scope_ok or type(claims.get("exp")) is not int or claims["exp"] <= time.time()):
            raise ValueError("Robot authentication or project scope was not verified")


def toolset():
    names = [Path(__file__).name, "atomic_file.py", "bounded_file.py", "bounded_subprocess.py", "strict_json.py", "deploy_keycloak_ha.py"]
    return {name: hashlib.sha256(read_bounded_bytes(Path(__file__).parent / name, max_bytes=256 * 1024)).hexdigest() for name in names}


def private_directory(path, create=False):
    path = Path(path).absolute()
    if path != path.resolve() or path.is_symlink(): raise ValueError("Use a canonical private operation directory")
    if create: path.mkdir(mode=0o700, parents=False)
    info = path.stat()
    if not path.is_dir() or os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077): raise ValueError("Operation directory is not private")
    return path


def load(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.is_symlink()
            or os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077)): raise ValueError("Private credential or state file changed ownership or mode")
    return loads_strict_json(read_bounded_bytes(path, max_bytes=128 * 1024))


def save(path, value): atomic_write_text(path, canonical(value) + "\n")


def observed(plan, row, operation):
    if (not isinstance(row, dict) or not positive(row.get("id")) or row.get("name") != plan["robot"]["username"]
            or row.get("level") != "project" or row.get("disable") is not False or row.get("editable") is not True
            or type(row.get("duration")) is not int or row["duration"] != plan["robot"]["duration_days"]
            or row.get("description") != marker(operation, plan) or type(row.get("expires_at")) is not int or row["expires_at"] <= time.time() + 60):
        raise ValueError("Original robot ownership, expiry or flags changed")
    perms = row.get("permissions")
    if not isinstance(perms, list) or len(perms) != 1 or perms[0].get("kind") != "project" or perms[0].get("namespace") != plan["project"]["name"]:
        raise ValueError("Robot permission namespace changed")
    access = perms[0].get("access")
    if not isinstance(access, list) or sorted(map(canonical, access)) != sorted(map(canonical, ACCESS)): raise ValueError("Robot permissions are not exactly pull/push")
    created = row.get("creation_time")
    if not isinstance(created, str) or len(created) > 50: raise ValueError("Original creation timestamp missing")
    stamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
    if stamp.tzinfo is None or abs(row["expires_at"] - stamp.timestamp() - plan["robot"]["duration_days"] * 86400) > 7200: raise ValueError("Robot lifetime exceeds its reviewed duration")
    return {k: row[k] for k in ("id", "name", "creation_time", "expires_at", "duration", "description", "level", "disable", "editable")}


def operate(plan, directory=None, *, mode="preview", client=None):
    validate(plan)
    if mode not in {"preview", "apply", "resume"}: raise ValueError("Select preview, apply or original-operation resume")
    client = client or Client(plan); client.ready(); existing = client.lookup()
    if mode == "preview":
        return {"read_only": True, "creation_available": existing is None, "credential_saved": False, "artifact_publication_verified": False, "ha_verified": False, "full_stack_verified": False}
    if directory is None: raise ValueError("Select a separate private state directory")
    if mode == "apply" and existing is not None: raise ValueError("Refuse to adopt an existing robot")
    path = private_directory(directory, create=mode == "apply")
    with lock(path):
        state_path, response_path, credential_path = (path / name for name in ("state.json", "create-response.json", "credential.json"))
        if mode == "apply":
            state = {"schema_version": 1, "plan_sha256": fingerprint(plan), "toolset_sha256": toolset(), "operation_id": uuid4().hex, "phase": "create-pending"}
            save(state_path, state)  # Reserve once before the only allowed POST.
            response = client.create(state["operation_id"])
            save(response_path, response)  # Preserve the one-time response before interpreting it.
        else:
            state = load(state_path)
            if (not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1 or state.get("plan_sha256") != fingerprint(plan) or state.get("toolset_sha256") != toolset()
                    or not re.fullmatch(r"[0-9a-f]{32}", state.get("operation_id", ""))
                    or state.get("phase") not in {"create-pending", "completed", "credential-unavailable"}): raise ValueError("Original reservation, source or plan changed")
        if state["phase"] == "completed" and (not isinstance(state.get("identity"), dict) or not isinstance(state.get("credential_sha256"), str) or not SHA.fullmatch(state["credential_sha256"])):
            raise ValueError("Completed original credential binding is incomplete")
        if existing is None: existing = client.lookup()
        if existing is None: raise ValueError("Original create outcome unresolved or robot removed; never repeat creation")
        row = client.robot(existing.get("id")); identity = observed(plan, row, state["operation_id"])
        if state.get("identity") is not None and canonical(identity) != canonical(state["identity"]): raise ValueError("Original robot identity changed")
        if not response_path.exists():
            state.update(phase="credential-unavailable", identity=identity); save(state_path, state)
            raise ValueError("Robot exists without its one-time secret response; manual recovery required")
        response = load(response_path)
        if (not isinstance(response, dict) or response.get("id") != identity["id"] or response.get("name") != identity["name"]
                or response.get("creation_time") != identity["creation_time"] or response.get("expires_at") != identity["expires_at"]):
            raise ValueError("Original one-time creation response changed")
        secret = response.get("secret")
        basic(identity["name"], secret)
        credential = {"schema_version": 1, "operation_id": state["operation_id"], "plan_sha256": fingerprint(plan),
            "origin": plan["server"]["origin"], "project_id": plan["project"]["id"], "robot_id": identity["id"],
            "username": identity["name"], "secret": secret, "expires_at": identity["expires_at"]}
        if credential_path.exists():
            if canonical(load(credential_path)) != canonical(credential): raise ValueError("Original private credential changed")
        else: save(credential_path, credential)
        if state.get("credential_sha256") is not None and fingerprint(credential) != state["credential_sha256"]: raise ValueError("Original credential digest changed")
        client.authenticate(credential)
        state.update(phase="completed", identity=identity, credential_sha256=fingerprint(credential)); save(state_path, state)
        return {"read_only": False, "server_read_only": mode == "resume", "local_state_written": True, "credential_saved": True, "permissions_verified": True, "credential_authentication_verified": True,
            "artifact_publication_verified": False, "ha_verified": False, "full_stack_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--mode", choices=("preview", "apply", "resume"), default="preview"); parser.add_argument("--state-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.mode != "preview":
            if args.state_dir is None or not args.state_dir.absolute().resolve().is_relative_to((ROOT / "private").resolve()): raise ValueError("Keep credentials beneath private/")
            result = run_bounded(["git", "-c", "safe.directory=" + str(ROOT), "check-ignore", "--quiet", "--", str(args.state_dir / "credential.json")], cwd=ROOT, timeout=10, output_max_bytes=16384)
            if result.returncode: raise ValueError("Credential output must be ignored by Git")
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=65536))
        print(canonical(operate(plan, args.state_dir, mode=args.mode))); return 0
    except Exception:
        print("Harbor robot operation stopped; retain private state and do not repeat an uncertain creation.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
