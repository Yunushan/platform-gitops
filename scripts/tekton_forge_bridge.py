#!/usr/bin/env python3
"""Explicit TLS Forgejo/Gitea push receiver for a reviewed, fixed Tekton job."""

from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import sys
import tempfile
from urllib.parse import quote, urlsplit
from urllib.request import Request

from bounded_file import read_bounded_bytes, read_bounded_text
from http_transport import open_http_request, read_bounded_response
from kubernetes_management import Client, validate_target, verify_target
from strict_json import loads_strict_json


INPUTS = Path("/run/platform/bridge")
BODY_LIMIT = 1024 * 1024
OWNER = "platform.gitops/forge-tekton-contract"
PIPELINE_LABEL = "tekton.dev/pipeline"
IMAGE = re.compile(r"[a-z0-9][a-z0-9.:/_-]*(?::[A-Za-z0-9_.-]+)?@sha256:[0-9a-f]{64}")
NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
PROGRAMS = ("tekton_forge_bridge.py", "tekton_forge_reporter.py", "tekton_checkout.py", "ci_gitops_handoff.py",
            "atomic_file.py", "bounded_file.py", "strict_json.py", "http_transport.py", "kubernetes_management.py",
            "bounded_subprocess.py", "subprocess_timeout.py")


def programs(plan):
    if plan["schema_version"] == 2:
        from gerrit_artifact_contract import EXTRA_PROGRAMS
        return (*PROGRAMS, *EXTRA_PROGRAMS)
    return PROGRAMS


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def valid_selector(values: object) -> bool:
    if not isinstance(values, dict) or not 1 <= len(values) <= 8 or values.get("kubernetes.io/os") != "linux":
        return False
    atom = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?")
    dns = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
    for key, value in values.items():
        if not isinstance(key, str) or not isinstance(value, str) or not atom.fullmatch(value):
            return False
        parts = key.split("/")
        if len(parts) > 2 or not atom.fullmatch(parts[-1]):
            return False
        if len(parts) == 2 and (len(parts[0]) > 253 or not all(dns.fullmatch(part) for part in parts[0].split("."))):
            return False
    return True


def reporting_settings(plan: dict) -> dict:
    return plan.get("reporting", {"enabled": False, "account": plan["name"] + "-reporter", "secret": plan["name"] + "-reporter",
                                  "context": "ci/tekton/" + plan["name"], "poll_seconds": 30, "max_runs_per_poll": 25, "helper_images": []})


def validate(plan: object) -> dict:
    fields = {"schema_version", "name", "provider", "source", "management_target", "namespace", "service_account",
              "submitter_account", "submitter_namespace", "node_selector", "checkout_image", "build_image", "bridge_image",
              "build_argv", "read_secret", "timeout_minutes", "publish_address", "bridge_secret", "controller_selector"}
    if not isinstance(plan, dict) or type(plan.get("schema_version")) is not int or plan["schema_version"] not in {1, 2}:
        raise ValueError("Unsupported forge bridge plan")
    publication = plan["schema_version"] == 2
    if set(plan) - {"reporting"} != fields | ({"publication"} if publication else set()):
        raise ValueError("Unexpected forge bridge plan fields")
    for key in ("name", "namespace", "service_account", "submitter_account", "submitter_namespace", "read_secret", "bridge_secret"):
        if not isinstance(plan[key], str) or not NAME.fullmatch(plan[key]) or len(plan[key]) > (20 if key == "name" else 63):
            raise ValueError("Invalid bridge identity")
    reserved = {"default", "kube-system", "kube-public", "kube-node-lease", "tekton-pipelines", "tekton-pipelines-resolvers"}
    if (plan["namespace"] in reserved or plan["submitter_namespace"] in reserved
            or plan["namespace"] == plan["submitter_namespace"] or plan["provider"] not in {"forgejo", "gitea"}):
        raise ValueError("Select a dedicated job namespace, separate submitter namespace and forge provider")
    validate_target(plan["management_target"])
    for key in ("node_selector", "controller_selector"):
        if not valid_selector(plan[key]):
            raise ValueError("Select explicitly provisioned Linux controller and worker nodes")
    if any(key in plan["node_selector"] for key in ("node-role.kubernetes.io/master", "node-role.kubernetes.io/control-plane")):
        raise ValueError("Application jobs require separate worker nodes")
    for key in ("checkout_image", "build_image", "bridge_image"):
        if not isinstance(plan[key], str) or not IMAGE.fullmatch(plan[key]):
            raise ValueError("Pin all bridge and job images")
    argv = plan["build_argv"]
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 32
            or any(not isinstance(arg, str) or not arg or len(arg) > 1000 or "$(" in arg
                   or any(ord(ch) < 32 for ch in arg) for arg in argv)):
        raise ValueError("Declare a fixed build argument vector without Tekton substitutions")
    if type(plan["timeout_minutes"]) is not int or not 5 <= plan["timeout_minutes"] <= 60:
        raise ValueError("Bound job duration")
    if not isinstance(plan["publish_address"], str):
        raise ValueError("Publication address must be explicit")
    address = ipaddress.ip_address(plan["publish_address"])
    if address.version != 4 or address.is_unspecified or address.is_multicast:
        raise ValueError("Select the actual native/Docker publication address")
    source = plan["source"]
    source_fields = {"repository_id", "repository", "branch", "api_url", "clone_url", "read_username"}
    if (not isinstance(source, dict) or set(source) != source_fields | ({"ca_sha256"} if publication else set())
            or type(source["repository_id"]) is not int or source["repository_id"] <= 0
            or not isinstance(source["repository"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", source["repository"])
            or not isinstance(source["read_username"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", source["read_username"])):
        raise ValueError("Bind one exact repository ID and name")
    branch = source["branch"]
    if (not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", branch)
            or any(part.startswith(".") or part.endswith((".", ".lock")) or not part for part in branch.split("/")) or ".." in branch):
        raise ValueError("Select one exact branch")
    for key in ("api_url", "clone_url"):
        value = source[key]
        if not isinstance(value, str) or any(ch.isspace() or ord(ch) < 32 for ch in value):
            raise ValueError("Require canonical source URLs")
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment or "%" in url.path:
            raise ValueError("Source URLs require verified HTTPS without embedded credentials")
    if (not source["api_url"].endswith("/api/v1")
            or source["clone_url"] != source["api_url"][:-7] + "/" + source["repository"] + ".git"):
        raise ValueError("Git and API origins/repository paths must agree exactly")
    reporting = reporting_settings(plan)
    if (not isinstance(reporting, dict) or set(reporting) != {"enabled", "account", "secret", "context", "poll_seconds", "max_runs_per_poll", "helper_images"}
            or type(reporting["enabled"]) is not bool
            or any(not isinstance(reporting[key], str) or not NAME.fullmatch(reporting[key]) or len(reporting[key]) > 63 for key in ("account", "secret"))
            or reporting["account"] in {plan["submitter_account"], plan["service_account"]}
            or reporting["secret"] in {plan["bridge_secret"], plan["read_secret"]}
            or not isinstance(reporting["context"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,79}", reporting["context"])
            or type(reporting["poll_seconds"]) is not int or not 10 <= reporting["poll_seconds"] <= 60
            or type(reporting["max_runs_per_poll"]) is not int or not 1 <= reporting["max_runs_per_poll"] <= 100
            or not isinstance(reporting["helper_images"], list) or len(reporting["helper_images"]) > 16
            or reporting["enabled"] and not reporting["helper_images"]
            or any(not isinstance(image, str) or not IMAGE.fullmatch(image) for image in reporting["helper_images"])):
        raise ValueError("Declare a separate bounded reporter identity, Secret and status context")
    if publication:
        from gerrit_artifact_contract import forge_source_plan, validate as validate_publication
        if not reporting["enabled"]:
            raise ValueError("Artifact runs require a separate verified outcome reporter")
        validate_publication(plan)
        forge_source_plan(plan, "1" * 40)
    return plan


def private_input(name: str, *, minimum: int = 1, maximum: int = 8192, root: Path | None = None,
                  trim_line_endings: bool = True) -> bytes:
    path = (root or INPUTS) / name
    if os.name == "posix" and path.stat().st_mode & 0o007:
        raise ValueError("Private bridge input is world-accessible")
    value = read_bounded_bytes(path, max_bytes=maximum)
    if trim_line_endings:
        value = value.rstrip(b"\r\n")
    if len(value) < minimum:
        raise ValueError("Private bridge input is missing or too short")
    return value


def authenticate(plan: dict, headers, body: bytes, secret: bytes) -> str:
    prefix = "X-Forgejo-" if plan["provider"] == "forgejo" else "X-Gitea-"
    def header(name):
        values = headers.get_all(name, [])
        if len(values) != 1:
            raise ValueError("Missing or ambiguous webhook header")
        return values[0]
    signature = header(prefix + "Signature")
    if (not 0 < len(body) <= BODY_LIMIT or not re.fullmatch(r"[0-9a-f]{64}", signature)
            or not hmac.compare_digest(hmac.new(secret, body, hashlib.sha256).hexdigest(), signature)):
        raise ValueError("Webhook signature did not verify")
    if header(prefix + "Event") != "push":
        raise ValueError("Only push events are accepted")
    payload = loads_strict_json(body)
    source = plan["source"]
    repository = payload.get("repository", {}) if isinstance(payload, dict) else {}
    revision = payload.get("after") if isinstance(payload, dict) else None
    if (type(repository.get("id")) is not int or repository.get("id") != source["repository_id"]
            or repository.get("full_name") != source["repository"] or payload.get("ref") != "refs/heads/" + source["branch"]
            or not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision) or revision == "0" * 40):
        raise ValueError("Push repository, branch or commit is outside the reviewed contract")
    return revision


def source_tls(plan, inputs):
    if plan["schema_version"] == 1:
        return None
    ca = private_input("ca.crt", maximum=65536, root=inputs, trim_line_endings=False)
    if hashlib.sha256(ca).hexdigest() != plan["source"]["ca_sha256"]:
        raise ValueError("Reviewed forge CA changed")
    return ssl.create_default_context(cadata=ca.decode("ascii"))


def api_get(plan: dict, suffix: str) -> dict:
    token = private_input("api-token", minimum=16, maximum=512).decode("ascii")
    if any(ch.isspace() or ord(ch) < 33 or ord(ch) > 126 for ch in token):
        raise ValueError("Invalid private source API token")
    request = Request(plan["source"]["api_url"] + suffix, headers={"Authorization": "token " + token, "Accept": "application/json"})
    with open_http_request(request, timeout=10, use_environment_proxy=False, tls_context=source_tls(plan, INPUTS)) as response:
        result = loads_strict_json(read_bounded_response(response, max_bytes=256 * 1024))
    if not isinstance(result, dict):
        raise ValueError("Source API returned no object")
    return result


def verify_head(plan: dict, revision: str) -> None:
    source = plan["source"]
    base = "/repos/" + "/".join(quote(part, safe="") for part in source["repository"].split("/"))
    repository = api_get(plan, base)
    branch = api_get(plan, base + "/branches/" + quote(source["branch"], safe=""))
    if (repository.get("id") != source["repository_id"] or repository.get("full_name") != source["repository"]
            or branch.get("commit", {}).get("id") != revision):
        raise ValueError("Signed push no longer matches the configured source branch head")
    if plan["schema_version"] == 2 and (repository.get("clone_url") != source["clone_url"]
            or branch.get("name") != source["branch"] or branch.get("protected") is not True):
        raise ValueError("Artifact publication requires the exact protected source branch")


def load_bundle(root: Path, digest: str) -> tuple[dict, dict]:
    payload = read_bounded_bytes(root / "event-contract.json", max_bytes=128 * 1024)
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("Bridge contract differs from reviewed bytes")
    contract = loads_strict_json(payload)
    if (not isinstance(contract, dict) or set(contract) != {"schema_version", "runtime", "plan", "files_sha256"}
            or type(contract["schema_version"]) is not int or contract["schema_version"] != 1
            or contract["runtime"] not in {"rke2", "k3s", "k8s", "docker", "raw"}):
        raise ValueError("Invalid event contract")
    validate(contract["plan"])
    expected = {"run-template.json", *programs(contract["plan"])}
    if set(contract["files_sha256"]) != expected:
        raise ValueError("Incomplete bridge program inventory")
    files = {}
    for name, sha in contract["files_sha256"].items():
        content = read_bounded_bytes(root / name, max_bytes=1024 * 1024)
        if hashlib.sha256(content).hexdigest() != sha:
            raise ValueError("Bridge program or run template changed")
        files[name] = content
    template = loads_strict_json(files["run-template.json"])
    return contract, template


def verify_running_sources(contract: dict, program: Path) -> None:
    # A new working-tree CLI must not silently execute different verifier code
    # while claiming the integrity of an older copied package.
    paths = {Path(__file__), program}
    for name in contract["files_sha256"]:
        module = sys.modules.get(name.removesuffix(".py")) if name.endswith(".py") else None
        if module is not None and getattr(module, "__file__", None):
            paths.add(Path(module.__file__))
    for path in paths:
        payload = read_bounded_text(path, encoding="utf-8").encode()
        if hashlib.sha256(payload).hexdigest() != contract["files_sha256"].get(path.name):
            raise ValueError("Executing helper code differs from the reviewed package")


def receipt(client: Client, namespace: str, name: str, digest: str, data: dict, *, create: bool) -> bool:
    current = client.get("configmap/" + name, namespace, optional=True)
    expected = {"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
                "metadata": {"name": name, "namespace": namespace, "annotations": {OWNER: digest}, "labels": {OWNER: digest[:32]}}, "data": data}
    created = False
    if current is None and create:
        try:
            client.call(["create", "-f", "-", "-o", "json"], payload=canonical(expected))
            created = True
        except ValueError:
            pass
        current = client.get("configmap/" + name, namespace, optional=True)
    if (current is None or current.get("immutable") is not True or current.get("data") != data
            or current.get("metadata", {}).get("annotations", {}).get(OWNER) != digest):
        raise ValueError("Event reservation/receipt is unavailable or has different ownership")
    return created


def event_run(contract: dict, template: dict, digest: str, revision: str) -> tuple[dict, dict]:
    plan = contract["plan"]
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision) or revision == "0" * 40:
        raise ValueError("Invalid event revision")
    key = hashlib.sha256(canonical({"contract": digest, "revision": revision})).hexdigest()
    name = plan["name"] + "-" + key[:32]
    run = copy.deepcopy(template)
    run["metadata"]["name"] = name
    run["metadata"]["annotations"] = {OWNER: digest}
    run["metadata"]["labels"] = {OWNER: digest[:32], PIPELINE_LABEL: name}
    run["spec"]["params"] = [{"name": "revision", "value": revision}]
    spec_digest = hashlib.sha256(canonical(run["spec"])).hexdigest()
    data = {"run": name, "spec_sha256": spec_digest, "revision": revision}
    return run, data


def includes(expected, actual) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and includes(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(includes(a, b) for a, b in zip(expected, actual))
    return type(expected) is type(actual) and expected == actual


def submit(client: Client, contract: dict, template: dict, digest: str, revision: str) -> dict:
    namespace = contract["plan"]["namespace"]
    run, data = event_run(contract, template, digest, revision)
    name = data["run"]
    existing = client.get("pipelinerun.tekton.dev/" + name, namespace, optional=True)
    done = client.get("configmap/" + name + "-done", namespace, optional=True)
    if done is not None:
        if set(done.get("data", {})) != {*data, "uid"} or not done["data"].get("uid"):
            raise ValueError("Event receipt has no complete run identity")
        receipt(client, namespace, name + "-done", digest, {**data, "uid": done["data"]["uid"]}, create=False)
        if existing is None:
            return {"accepted": True, "duplicate": True, "run_retained": False}
    if existing is None:
        verify_head(contract["plan"], revision)
        if not receipt(client, namespace, name + "-lock", digest, data, create=True):
            # A previous submit may have timed out after accepting the job. Do
            # not recreate a missing run after an ambiguous reservation.
            existing = client.get("pipelinerun.tekton.dev/" + name, namespace, optional=True)
            if existing is None:
                raise ValueError("Reserved event needs explicit recovery; no duplicate job was created")
        else:
            try:
                client.call(["create", "-f", "-", "-o", "json"], payload=canonical(run))
            except ValueError:
                pass
            existing = client.get("pipelinerun.tekton.dev/" + name, namespace, optional=True)
    if (existing is None or existing.get("metadata", {}).get("annotations", {}).get(OWNER) != digest
            or not existing.get("metadata", {}).get("uid")):
        raise ValueError("Submitted run has no matching event identity")
    # API defaulting can add fields; every submitted spec field must agree.
    if not includes(run["spec"], existing.get("spec")):
        raise ValueError("Existing run differs from the reviewed immutable job")
    uid = existing["metadata"]["uid"]
    if done is not None and done["data"]["uid"] != uid:
        raise ValueError("Retained run was replaced after the event receipt")
    recorded = receipt(client, namespace, name + "-done", digest, {**data, "uid": uid}, create=True)
    return {"accepted": True, "duplicate": not recorded, "run_retained": True}


def make_handler(client: Client, contract: dict, template: dict, digest: str, secret: bytes):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PlatformWebhook"
        sys_version = ""

        def log_message(self, *args):
            pass  # Raw paths, headers, payloads and API errors stay private.

        def reply(self, status: int):
            self.close_connection = True
            self.send_response_only(status)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def send_error(self, code, message=None, explain=None):
            self.reply(code)

        def do_GET(self):
            self.reply(200 if self.path == "/healthz" else 404)

        def do_POST(self):
            lengths = self.headers.get_all("Content-Length", [])
            if (self.path != "/hooks/push" or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,7}", lengths[0])
                    or not 0 < int(lengths[0]) <= BODY_LIMIT or self.headers.get_all("Transfer-Encoding", [])
                    or self.headers.get_all("Content-Encoding", []) or self.headers.get_all("Content-Type", []) != ["application/json"]):
                self.reply(400)
                return
            try:
                body = self.rfile.read(int(lengths[0]))
                if len(body) != int(lengths[0]):
                    raise ValueError("Incomplete body")
                revision = authenticate(contract["plan"], self.headers, body, secret)
            except (ValueError, TypeError, KeyError, AttributeError, OSError):
                self.reply(403)
                return
            try:
                submit(client, contract, template, digest, revision)
            except Exception:
                self.reply(503)
                return
            self.reply(202)

    return Handler


def make_server(address, handler, context):
    class Server(HTTPServer):
        request_queue_size = 8

        def handle_error(self, request, client_address):
            print("Webhook connection failed; no request details recorded.", file=sys.stderr)

        def get_request(self):
            socket, client_address = self.socket.accept()
            socket.settimeout(5)
            try:
                return context.wrap_socket(socket, server_side=True), client_address
            except Exception:
                socket.close()
                raise

    return Server(address, handler)


def normalized_target(client: Client) -> bytes:
    config = loads_strict_json(client.call(["config", "view", "--flatten", "--raw", "--minify", "-o", "json"]))
    users = config.get("users", []) if isinstance(config, dict) else []
    credentials = users[0].get("user") if len(users) == 1 else None
    if (not isinstance(credentials, dict) or set(credentials) not in ({"token"}, {"client-certificate-data", "client-key-data"})
            or any(not isinstance(value, str) or not value for value in credentials.values())
            or len(config.get("clusters", [])) != 1
            or "certificate-authority" in config["clusters"][0].get("cluster", {})):
        raise ValueError("Use a self-contained scoped token/client certificate; dynamic credential plugins and file references are not supported")
    return canonical(config)


@contextmanager
def frozen_target(plan: dict, *, inputs: Path | None = None, kubeconfig_name: str = "kubeconfig"):
    inputs = inputs or INPUTS
    private_input("ca.crt", maximum=256 * 1024, root=inputs)
    private_input(kubeconfig_name, maximum=256 * 1024, root=inputs)
    os.environ["SSL_CERT_FILE"] = str(inputs / "ca.crt")
    os.environ[plan["management_target"]["kubeconfig_env"]] = str(inputs / kubeconfig_name)
    # Resolve relative CA/certificate paths before relocating the snapshot.
    # This local config view captures credentials only in private process memory.
    kubeconfig = normalized_target(Client(plan["management_target"]))
    # A projected Secret can rotate while the service is running. Bind every
    # kubectl operation to the same private configuration verified at startup;
    # rotating the target/credential requires an explicit receiver restart.
    with tempfile.TemporaryDirectory(prefix="forge-tekton-target-") as directory:
        frozen = Path(directory) / "kubeconfig"
        descriptor = os.open(frozen, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(kubeconfig)
        os.environ[plan["management_target"]["kubeconfig_env"]] = str(frozen)
        yield


def serve(root: Path, digest: str) -> None:
    if os.name != "posix" or os.geteuid() == 0:
        raise ValueError("Bridge service requires a non-root Linux identity")
    contract, template = load_bundle(root, digest)
    verify_running_sources(contract, Path(__file__))
    secret = private_input("webhook-secret", minimum=32, maximum=512)
    private_input("api-token", minimum=16, maximum=512)
    private_input("tls.key", maximum=16384)
    with frozen_target(contract["plan"]):
        serve_target(contract, template, digest, secret)


def serve_target(contract: dict, template: dict, digest: str, secret: bytes) -> None:
    plan = contract["plan"]
    client = Client(plan["management_target"])
    verify_target(client, plan["management_target"])
    namespace = client.get("namespace/" + plan["namespace"])
    if not namespace or namespace.get("metadata", {}).get("labels", {}).get("pod-security.kubernetes.io/enforce") != "restricted":
        raise ValueError("Preprovision the restricted job namespace")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(INPUTS / "tls.crt", INPUTS / "tls.key", password="")
    handler = make_handler(client, contract, template, digest, secret)
    bind = plan["publish_address"] if contract["runtime"] == "raw" else "0.0.0.0"
    with make_server((bind, 8443), handler, context) as server:
        server.serve_forever(poll_interval=0.5)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args(argv)
    try:
        load_bundle(args.bundle_dir, args.contract_sha256)
        if args.serve:
            serve(args.bundle_dir, args.contract_sha256)
        else:
            print(json.dumps({"local_inputs_verified": True, "serving": False, "event_integration_verified": False}))
        return 0
    except Exception:
        print("Forge Tekton bridge stopped; inspect private contract, TLS, scoped credentials, source API and event receipts.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
