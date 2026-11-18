#!/usr/bin/env python3
"""Disposable, network-isolated Jenkins LTS WAR and real build smoke test."""
import hashlib
import contextlib
import ctypes
import io
import base64
import http.client
import json
import os
from pathlib import Path
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from functools import partial
import ssl
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import threading
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
from unittest.mock import patch

import verify_jenkins_gerrit as verifier
import prepare_jenkins_fixture_plugins as plugin_fixture
import trigger_jenkins_gerrit as trigger_module
import scan_jenkins_gerrit as scanner
import render_jenkins_gerrit_job as job_renderer
import resolve_jenkins_gerrit_build as build_resolver


WAR_SHA = "d98032d2401c540a35613a82ed358921fcf3166d5e6eaaaf60a7941845be3251"
JAVA_SHA = "a535a1df9c6284d9618872984e026ae0352d1f7e9e0605425c0e6114f47351dd"
INIT = '''import jenkins.model.Jenkins
import hudson.model.FreeStyleProject
import hudson.model.ParametersDefinitionProperty
import hudson.model.StringParameterDefinition
import hudson.tasks.Shell
def j = Jenkins.get()
j.setNumExecutors(1)
def project = j.getItem("fixture-job") ?: j.createProject(FreeStyleProject, "fixture-job")
project.addProperty(new ParametersDefinitionProperty(
  new StringParameterDefinition("GERRIT_CHANGE_NUMBER", ""),
  new StringParameterDefinition("GERRIT_PATCHSET_NUMBER", ""),
  new StringParameterDefinition("GERRIT_PATCHSET_REVISION", ""),
  new StringParameterDefinition("GERRIT_REFSPEC", "")))
project.buildersList.clear()
project.buildersList.add(new Shell("printf fixture-success"))
project.save()
j.save()
'''


def digest(path, expected):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024 * 1024: raise ValueError("invalid-fixture-artifact")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected: raise ValueError("fixture-artifact-changed")


def child(war, archive, *, real_gerrit=None, publication=None):
    # This must run inside a fresh user/network namespace. No default route or
    # host network interface is permitted, even if callers run the script directly.
    links = json.loads(subprocess.run(["/usr/sbin/ip", "-j", "link", "show"], check=True, timeout=10, capture_output=True, text=True).stdout)
    if os.geteuid() != 0 or {row.get("ifname") for row in links} != {"lo"}:
        raise ValueError("isolated-network-namespace-required")
    subprocess.run(["/usr/sbin/ip", "link", "set", "lo", "up"], check=True, timeout=10, capture_output=True)
    routes = subprocess.run(["/usr/sbin/ip", "route", "show", "default"], check=True, timeout=10, capture_output=True, text=True)
    if routes.stdout.strip(): raise ValueError("unexpected-default-route")
    if publication is not None and real_gerrit is None:
        raise ValueError("publisher acceptance requires real Gerrit")
    authenticated = real_gerrit is not None and (publication is not None or os.environ.get("JENKINS_ARGOCD_FIXTURE") == "1")
    war, archive = Path(war).resolve(strict=True), Path(archive).resolve(strict=True)
    digest(war, WAR_SHA); digest(archive, JAVA_SHA)
    with tempfile.TemporaryDirectory(prefix="jenkins-isolated-") as directory:
        root = Path(directory); home = root / "home"; home.mkdir()
        init = home / "init.groovy.d"; init.mkdir()
        git_mode = os.environ.get("JENKINS_FIXTURE_GIT") == "1" or real_gerrit is not None
        git_server = None; git_thread = None; jenkins_tls_proxy = None; jenkins_tls_thread = None
        fixture_revision = "a" * 40; source_origin = "https://review.example"
        jenkins_origin = "https://ci.example"; jenkins_token = "synthetic-jenkins-token"
        fixture_queue_id = None
        project_name, change_number, patchset_number = "team/project", 123, 2
        fixture_init = INIT
        git_env = {}
        if git_mode:
            base = Path(__file__).resolve().parents[1] / "private/validation/jenkins/2.580.2"
            specs = plugin_fixture.resolve(base / "update-center.json")["plugins"]
            plugins = home / "plugins"; plugins.mkdir()
            for name, spec in specs.items():
                artifact = base / "plugins" / (name + ".hpi")
                digest(artifact, spec["sha256"])
                shutil.copyfile(artifact, plugins / (name + ".jpi"))
            if real_gerrit is not None:
                context, prepared, item = real_gerrit
                project_name, change_number, patchset_number = prepared["project"], item["change"], item["patchset"]
                fixture_revision, source_origin = item["revision"], context["origin"]
                cert = root / "git-ca.crt"
                shutil.copyfile(context["tls"] / "ca.crt", cert)
                token = context["users"]["ci-read"]
                auth_setup = ("import com.cloudbees.plugins.credentials.SystemCredentialsProvider\n"
                    "import com.cloudbees.plugins.credentials.CredentialsScope\n"
                    "import com.cloudbees.plugins.credentials.impl.UsernamePasswordCredentialsImpl\n"
                    "import hudson.plugins.git.UserRemoteConfig\nimport hudson.plugins.git.BranchSpec\n"
                    "def creds = SystemCredentialsProvider.getInstance().getCredentials()\n"
                    "creds.add(new UsernamePasswordCredentialsImpl(CredentialsScope.GLOBAL, 'fixture-reader', '', 'ci-read', '" + token + "'))\n"
                    "project.setScm(new hudson.plugins.git.GitSCM([new UserRemoteConfig('" + source_origin + "/a/" + project_name + ".git', null, '+$GERRIT_REFSPEC:refs/remotes/origin/fixture', 'fixture-reader')], [new BranchSpec('*/fixture')], false, [], null, null, []))\n")
                fixture_init = INIT.replace("project.buildersList.clear()", auth_setup + "project.buildersList.clear()")
                git_env = {"GIT_SSL_CAINFO": str(cert), "GIT_TERMINAL_PROMPT": "0"}
            else:
                source = root / "source"; source.mkdir()
                def git(*args):
                    return subprocess.run(["git", *args], cwd=source, env={"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8"},
                                          capture_output=True, text=True, timeout=30, check=True).stdout.strip()
                git("init", "-q", "-b", "master")
                git("config", "user.name", "Fixture"); git("config", "user.email", "fixture@example.invalid")
                (source / "README").write_text("fixture\n", encoding="utf-8")
                git("add", "."); git("commit", "-qm", "fixture")
                fixture_revision = git("rev-parse", "HEAD")
                webroot = root / "web"; repo = webroot / "a/team/project.git"; repo.parent.mkdir(parents=True)
                git("clone", "--bare", "--quiet", str(source), str(repo))
                git("--git-dir=" + str(repo), "update-server-info")
                cert, key = root / "git-ca.crt", root / "git.key"
                subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                                "-addext", "subjectAltName=DNS:localhost", "-keyout", str(key), "-out", str(cert)],
                               capture_output=True, check=True, timeout=30)
                class Handler(SimpleHTTPRequestHandler):
                    def log_message(self, *args): pass
                git_server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(webroot)))
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert, key)
                git_server.socket = context.wrap_socket(git_server.socket, server_side=True)
                git_thread = threading.Thread(target=git_server.serve_forever, daemon=True); git_thread.start()
                source_origin = "https://localhost:" + str(git_server.server_port)
                fixture_init = INIT.replace("project.buildersList.clear()", "project.setScm(new hudson.plugins.git.GitSCM('" + source_origin + "/a/team/project.git'))\nproject.buildersList.clear()")
                git_env = {"GIT_SSL_CAINFO": str(cert), "GIT_TERMINAL_PROMPT": "0"}
        if authenticated:
            fixture_init += '''
import hudson.security.HudsonPrivateSecurityRealm
import hudson.security.FullControlOnceLoggedInAuthorizationStrategy
import jenkins.security.ApiTokenProperty
def realm = new HudsonPrivateSecurityRealm(false)
def account = realm.createAccount("fixture-reader", "ephemeral-fixture-password")
j.setSecurityRealm(realm)
def authz = new FullControlOnceLoggedInAuthorizationStrategy()
authz.setAllowAnonymousRead(false)
j.setAuthorizationStrategy(authz)
def apiToken = account.getProperty(ApiTokenProperty.class).getTokenStore().generateNewToken("fixture").plainValue
account.save()
new File(j.rootDir, "fixture-api-token").setText(apiToken, "UTF-8")
j.save()
'''
        (init / "fixture.groovy").write_text(fixture_init, encoding="utf-8")
        java_root = root / "java"; java_root.mkdir()
        with tarfile.open(archive, "r:gz") as tar: tar.extractall(java_root, filter="data")
        java = list(java_root.glob("*/bin/java"))
        if len(java) != 1 or not java[0].is_file(): raise ValueError("invalid-java-archive")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0)); port = reservation.getsockname()[1]
        base = "http://127.0.0.1:" + str(port)
        opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "JENKINS_HOME": str(home), "LANG": "C.UTF-8", **git_env}
        log = (root / "jenkins.log").open("w+b")
        command = [str(java[0]), "-Xmx512m", "-Djava.awt.headless=true", "-Djenkins.install.runSetupWizard=false",
                   "-jar", str(war), "--httpListenAddress=127.0.0.1", "--httpPort=" + str(port)]
        if real_gerrit is not None:
            local_war = root / "jenkins.war"; shutil.copyfile(war, local_war); command[5] = str(local_war)
            for path in [root, *root.rglob("*")]:
                if not path.is_symlink(): os.chown(path, 65534, 65534)
            command = [sys.executable, "-B", __file__, "--drop-child", str(os.getpid()), *command]
        process = subprocess.Popen(command,
                                   env=env, cwd=home, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        try:
            deadline = time.monotonic() + 180
            while True:
                if process.poll() is not None:
                    log.flush(); log.seek(0); text = log.read(65536).decode("utf-8", errors="replace")
                    flags = {name: name.casefold() in text.casefold() for name in ("Permission denied", "AccessDeniedException", "No such file", "OutOfMemoryError", "Unrecognized option")}
                    print(json.dumps({"jenkins_fixture_failure_signals": flags}), flush=True)
                    raise ValueError("jenkins-exited-before-ready")
                try:
                    with opener.open(base + "/login", timeout=3) as response:
                        if response.status == 200 and response.headers.get("X-Jenkins") == "2.580.2": break
                except (URLError, TimeoutError): pass
                if time.monotonic() >= deadline: raise TimeoutError("jenkins-start-timeout")
                time.sleep(1)
            if authenticated:
                jenkins_token = (home / "fixture-api-token").read_text().strip()
                if len(jenkins_token) < 16: raise ValueError("jenkins-api-token-missing")
                auth = "Basic " + base64.b64encode(("fixture-reader:" + jenkins_token).encode()).decode()
                opener.addheaders = [("Authorization", auth)]
                with opener.open(base + "/whoAmI/api/json", timeout=5) as response:
                    if json.load(response).get("name") != "fixture-reader": raise ValueError("jenkins-native-token-not-authenticated")
                bad_auth = "Basic " + base64.b64encode(b"fixture-reader:invalid-fixture-token").decode()
                try:
                    with build_opener(ProxyHandler({})).open(Request(base + "/job/fixture-job/config.xml", headers={"Authorization": bad_auth}), timeout=5):
                        raise ValueError("jenkins-invalid-token-accepted")
                except HTTPError as error:
                    if error.code not in (401, 403): raise
                    error.close()
                class JenkinsReadProxy(BaseHTTPRequestHandler):
                    def log_message(self, *args): pass
                    def forward(self):
                        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
                        try:
                            size = int(self.headers.get("Content-Length", "0"))
                            if not 0 <= size <= 65536: raise ValueError("fixture-request-too-large")
                            headers = {"Authorization": self.headers.get("Authorization", ""), "Content-Type": self.headers.get("Content-Type", "")}
                            connection.request(self.command, self.path, body=self.rfile.read(size) if size else None, headers=headers)
                            response = connection.getresponse(); body = response.read(1024 * 1024 + 1)
                            if len(body) > 1024 * 1024: raise ValueError("fixture-response-too-large")
                            self.send_response(response.status)
                            location = response.getheader("Location")
                            if location:
                                parsed = urlsplit(location)
                                if parsed.scheme != "http" or parsed.netloc != "127.0.0.1:" + str(port): raise ValueError("unexpected-fixture-location")
                                self.send_header("Location", jenkins_origin + parsed.path)
                            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                        finally: connection.close()
                    do_GET = forward
                    do_POST = forward
                proxy_host = urlsplit(source_origin).hostname
                jenkins_tls_proxy = ThreadingHTTPServer((proxy_host, 0), JenkinsReadProxy)
                tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                tls_context.load_cert_chain(context["tls"] / "ca.crt", context["tls"] / "tls.key")
                jenkins_tls_proxy.socket = tls_context.wrap_socket(jenkins_tls_proxy.socket, server_side=True)
                jenkins_tls_thread = threading.Thread(target=jenkins_tls_proxy.serve_forever, daemon=True); jenkins_tls_thread.start()
                jenkins_origin = "https://" + proxy_host + ":" + str(jenkins_tls_proxy.server_port)
            with opener.open(base + "/crumbIssuer/api/json", timeout=5) as response: crumb = json.load(response)
            if real_gerrit is not None:
                job_plan = {"schema_version": 1, "gerrit_origin": source_origin, "project": project_name,
                            "credential_id": "fixture-reader", "agent_label": "built-in", "git_plugin_version": "5.11.0",
                            "build_argv": ["python3", "-c", "assert 2 + 2 == 4"]}
                if publication is not None:
                    job_plan.update(build_argv=publication.configure_job(root, jenkins_origin, context, prepared), archive_image_receipt=True)
                request = Request(base + "/job/fixture-job/config.xml", data=job_renderer.render(job_plan), method="POST",
                                  headers={crumb["crumbRequestField"]: crumb["crumb"], "Content-Type": "application/xml"})
                with opener.open(request, timeout=10) as response:
                    if response.status != 200: raise ValueError("generated-job-update-not-accepted")
            if git_mode:
                with opener.open(base + "/job/fixture-job/config.xml", timeout=5) as response: initial_config = response.read(1024 * 1024)
                config_output = os.environ.get("JENKINS_FIXTURE_CONFIG_OUTPUT")
                if config_output:
                    destination = Path(config_output).resolve()
                    destination.relative_to((Path(__file__).resolve().parents[1] / "private").resolve())
                    if real_gerrit is not None and any(value.encode() in initial_config for value in real_gerrit[0]["users"].values()):
                        raise ValueError("fixture-secret-in-job-config")
                    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(descriptor, "wb") as stream: stream.write(initial_config)
                trigger_plan = {"schema_version": 1, "jenkins_origin": jenkins_origin, "job": ["fixture-job"],
                                "job_config_sha256": hashlib.sha256(initial_config).hexdigest(), "gerrit_origin": source_origin,
                                "project": project_name, "branch": "main", "change": change_number, "patchset": patchset_number, "revision": fixture_revision}
                def current_change(_):
                    if real_gerrit is not None:
                        return real_gerrit[0]["api"].call("ci-read", "/a/changes/" + str(change_number) + "?o=CURRENT_REVISION")
                    return {"project": project_name, "branch": "main", "status": "NEW", "_number": change_number,
                            "current_revision": fixture_revision, "revisions": {fixture_revision: {"_number": patchset_number,
                            "ref": "refs/changes/23/123/2"}}}
                def post_to_jenkins(path, params):
                    request = Request(base + path, data=urlencode(params).encode(), method="POST", headers={crumb["crumbRequestField"]: crumb["crumb"]})
                    with opener.open(request, timeout=10) as response:
                        location = urlsplit(response.headers["Location"])
                        if location.scheme != "http" or location.netloc != "127.0.0.1:" + str(port):
                            raise ValueError("fixture-queue-origin-changed")
                        return response.status, jenkins_origin + location.path
                private = root / "private"; private.mkdir(mode=0o700)
                with patch.object(trigger_module, "ROOT", root):
                    if real_gerrit is not None:
                        policy = {k: v for k, v in trigger_plan.items() if k not in {"change", "patchset", "revision"}}
                        policy.update(max_changes=10, max_submissions=5)
                        def source_get(path): return real_gerrit[0]["api"].call("ci-read", path)
                        def scan_trigger(apply=False):
                            if os.environ.get("JENKINS_ARGOCD_FIXTURE") != "1":
                                return scanner.cycle(policy, lambda *_: initial_config, source_get, post_to_jenkins, private, apply=apply)
                            inputs = root / "trigger-scanner-inputs"; inputs.mkdir(mode=0o700, exist_ok=True)
                            values = {"jenkins-user": b"fixture-reader", "jenkins-token": jenkins_token.encode(),
                                      "gerrit-user": b"ci-read", "gerrit-token": context["users"]["ci-read"].encode(),
                                      "ca.crt": (context["tls"] / "ca.crt").read_bytes()}
                            for name, data in values.items():
                                path = inputs / name; path.write_bytes(data); path.chmod(0o600)
                            policy_file = private / "trigger-scan-policy.json"; policy_file.write_text(json.dumps(policy)); policy_file.chmod(0o600)
                            argv = ["scan", "--policy", str(policy_file), "--policy-sha256", scanner.canonical_digest(policy),
                                    "--input-dir", str(inputs), "--journal-dir", str(private)] + (["--apply"] if apply else [])
                            output = io.StringIO()
                            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
                                if scanner.main() != 0: raise ValueError("trigger-scan-cli-failed")
                            if any(value.decode() in output.getvalue() for name, value in values.items() if name.endswith("-token")):
                                raise ValueError("trigger-scan-secret-in-output")
                            return json.loads(output.getvalue())
                        preview = scan_trigger()
                        if preview["pending"] != 1: raise ValueError("fixture-scan-preflight-failed")
                        queued = scan_trigger(apply=True)
                        duplicate = scan_trigger(apply=True)
                        if queued["queued"] != 1 or duplicate["already_queued"] != 1: raise ValueError("fixture-scan-dedup-failed")
                        queued = {"queue_write_attempted": True}
                    else:
                        queued = trigger_module.submit(trigger_plan, lambda *_: initial_config, current_change, post_to_jenkins, private, apply=True)
                        try:
                            trigger_module.submit(trigger_plan, lambda *_: initial_config, current_change, post_to_jenkins, private, apply=True)
                        except FileExistsError:
                            pass
                        else:
                            raise ValueError("fixture-duplicate-trigger-accepted")
                    journal = private / (trigger_module.reservation_key(trigger_plan) + ".json")
                    fixture_queue_id = json.loads(journal.read_text(encoding="utf-8"))["queue_id"]
                if not queued.get("queue_write_attempted"): raise ValueError("fixture-trigger-not-queued")
            else:
                body = urlencode({"GERRIT_CHANGE_NUMBER": str(change_number), "GERRIT_PATCHSET_NUMBER": str(patchset_number), "GERRIT_PATCHSET_REVISION": fixture_revision}).encode()
                request = Request(base + "/job/fixture-job/buildWithParameters", data=body, method="POST", headers={crumb["crumbRequestField"]: crumb["crumb"]})
                with opener.open(request, timeout=10) as response:
                    if response.status not in (201, 202): raise ValueError("jenkins-build-not-queued")
            deadline = time.monotonic() + 90
            while True:
                try:
                    with opener.open(base + "/job/fixture-job/lastBuild/api/json?tree=number,building,result", timeout=5) as response:
                        build = json.load(response)
                    if build.get("building") is False:
                        if build.get("result") != "SUCCESS" or build.get("number") != 1: raise ValueError("jenkins-build-failed")
                        break
                except HTTPError as error:
                    if error.code != 404: raise
                if process.poll() is not None or time.monotonic() >= deadline: raise TimeoutError("jenkins-build-timeout")
                time.sleep(1)
            if real_gerrit is not None:
                api = real_gerrit[0]["api"]
                first_revision = fixture_revision
                api.call("fixture-admin", "/a/changes/" + str(change_number) + "/edit/second.txt", b"second patch set\n", method="PUT", content_type="text/plain")
                api.call("fixture-admin", "/a/changes/" + str(change_number) + "/edit:publish", {"notify": "NONE"})
                import tekton_gerrit_bridge as review_bridge
                item = review_bridge.current({"source": {"project": project_name, "branch": "main"}}, current_change(None))
                if item["change"] != change_number or item["patchset"] == patchset_number or item["revision"] == first_revision:
                    raise ValueError("fixture-second-patchset-not-created")
                fixture_revision, patchset_number = item["revision"], item["patchset"]
                trigger_plan = {**trigger_plan, "patchset": patchset_number, "revision": fixture_revision}
                with patch.object(trigger_module, "ROOT", root):
                    next_cycle = scan_trigger(apply=True)
                    next_duplicate = scan_trigger(apply=True)
                    if next_cycle["queued"] != 1 or next_duplicate["already_queued"] != 1:
                        raise ValueError("fixture-second-scan-dedup-failed")
                    journal = private / (trigger_module.reservation_key(trigger_plan) + ".json")
                    fixture_queue_id = json.loads(journal.read_text(encoding="utf-8"))["queue_id"]
                deadline = time.monotonic() + 90
                while True:
                    with opener.open(base + "/job/fixture-job/lastBuild/api/json?tree=number,building,result", timeout=5) as response:
                        second = json.load(response)
                    if second.get("number") == 2 and second.get("building") is False:
                        if second.get("result") != "SUCCESS": raise ValueError("jenkins-second-build-failed")
                        break
                    if process.poll() is not None or time.monotonic() >= deadline: raise TimeoutError("jenkins-second-build-timeout")
                    time.sleep(1)
                with opener.open(base + "/job/fixture-job/config.xml", timeout=5) as response:
                    open_config = response.read(1024 * 1024)
                if config_output:
                    if any(value.encode() in open_config for value in real_gerrit[0]["users"].values()):
                        raise ValueError("fixture-secret-in-job-config")
                    after = Path(str(destination) + ".after")
                    descriptor = os.open(after, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(descriptor, "wb") as stream: stream.write(open_config)
                open_plan = {"schema_version": 1, "jenkins_origin": jenkins_origin, "job": ["fixture-job"], "build": 2,
                             "job_config_sha256": hashlib.sha256(open_config).hexdigest(), "gerrit_origin": source_origin,
                             "project": project_name, "branch": "main", "change": change_number, "patchset": patchset_number, "revision": fixture_revision}
                def open_jenkins(path, json_output=True):
                    with opener.open(base + path, timeout=5) as response: raw = response.read(1024 * 1024)
                    return json.loads(raw) if json_output else raw
                if verifier.queue_id_from_journal(open_plan, journal) != fixture_queue_id:
                    raise ValueError("fixture-open-journal-mismatch")
                checked = verifier.verify(open_plan, open_jenkins, lambda path: api.call("ci-read", path),
                                          journal=journal, change_state="NEW")
                if (checked.get("review_vote_authorized") is not False or checked.get("change_state") != "NEW"
                        or checked.get("job_config_reservation_verified") is not True):
                    raise ValueError("open-review-check-mismatch")
                resolved = build_resolver.resolve(trigger_plan, journal, open_jenkins, lambda path: api.call("ci-read", path))
                if resolved.get("status") != "matched-success" or resolved.get("build") != 2:
                    raise ValueError("queue-to-build-resolution-failed")
                import report_jenkins_gerrit as result_reporter
                from urllib.parse import quote
                class ReviewAPI:
                    def request(self, path, payload=None):
                        return api.call("ci-review", path, payload)
                review_api = ReviewAPI()
                account = review_api.request("/a/accounts/self")
                labels = review_api.request("/a/projects/" + quote(project_name, safe="") + "/labels/?inherited")
                label = next(x for x in labels if x.get("name") == "Verified")
                report_plan = {"schema_version": 1, "trigger": trigger_plan,
                               "reviewer_username": "ci-review", "reviewer_account_id": account["_account_id"],
                               "label_definition_sha256": result_reporter.reporter.fingerprint(label),
                               "trust_jenkins_controller": True}
                receipt_dir = private / "review-receipts"
                receipt_dir.mkdir(mode=0o700)
                with patch.object(trigger_module, "ROOT", root):
                    arguments = (report_plan, journal, receipt_dir, open_jenkins,
                                 lambda path: api.call("ci-read", path), review_api)
                    if result_reporter.report(*arguments)["status"] != "ready":
                        raise ValueError("reporter-preflight-failed")
                    import scan_jenkins_gerrit_results as result_scanner
                    result_policy = {"schema_version": 1, "scan": policy, "reviewer_username": "ci-review",
                        "reviewer_account_id": account["_account_id"], "label_definition_sha256": result_reporter.reporter.fingerprint(label),
                        "trust_jenkins_controller": True, "max_reports": 5}
                    cycle_args = (result_policy, private, receipt_dir, open_jenkins, lambda path: api.call("ci-read", path), lambda _: review_api)
                    if os.environ.get("JENKINS_ARGOCD_FIXTURE") == "1":
                        scan_inputs = root / "result-scanner-inputs"; scan_inputs.mkdir(mode=0o700)
                        values = {"jenkins-user": b"fixture-reader", "jenkins-token": jenkins_token.encode(),
                                  "gerrit-user": b"ci-read", "gerrit-token": context["users"]["ci-read"].encode(),
                                  "reviewer-user": b"ci-review", "reviewer-token": context["users"]["ci-review"].encode(),
                                  "ca.crt": (context["tls"] / "ca.crt").read_bytes()}
                        for name, data in values.items():
                            path = scan_inputs / name; path.write_bytes(data); path.chmod(0o600)
                        policy_file = private / "result-scan-policy.json"; policy_file.write_text(json.dumps(result_policy)); policy_file.chmod(0o600)
                        cli = ["results", "--policy", str(policy_file), "--policy-sha256", scanner.canonical_digest(result_policy),
                               "--input-dir", str(scan_inputs), "--queue-dir", str(private), "--review-dir", str(receipt_dir), "--apply"]
                        for key in ("reported", "already_reported"):
                            output = io.StringIO()
                            with patch.object(sys, "argv", cli), contextlib.redirect_stdout(output):
                                if result_scanner.main() != 0: raise ValueError("result-scan-cli-failed")
                            if json.loads(output.getvalue())[key] != 1: raise ValueError("result-scan-cli-count-mismatch")
                            if any(value.decode() in output.getvalue() for name, value in values.items() if name.endswith("-token")):
                                raise ValueError("result-scan-cli-secret-in-output")
                    else:
                        if result_scanner.cycle(*cycle_args, apply=True)["reported"] != 1: raise ValueError("reporter-publish-failed")
                        if result_scanner.cycle(*cycle_args, apply=True)["already_reported"] != 1: raise ValueError("reporter-dedup-failed")
                api.call("fixture-admin", "/a/changes/" + str(change_number) + "/revisions/" + fixture_revision + "/review",
                         {"labels": {"Code-Review": 2}, "notify": "NONE"})
                api.call("fixture-admin", "/a/changes/" + str(change_number) + "/submit", {"wait_for_merge": True, "notify": "NONE"})
                with patch.object(trigger_module, "ROOT", root):
                    merged = result_reporter.verify_merged(*arguments)
                    if merged.get("status") != "merged-evidence-matched" or merged.get("deployment_authorized") is not False:
                        raise ValueError("merged-reporter-evidence-mismatch")
                    import jenkins_gerrit_gitops_handoff as gitops_adapter
                    promotion = json.loads((Path(__file__).resolve().parents[1] / "examples/jenkins-gerrit-argocd-handoff.json").read_text())["promotion"]
                    promotion["bridge_contract_sha256"] = result_reporter.reporter.fingerprint(report_plan)
                    promotion["cd"]["path"] = "deploy/staging"
                    application = {"metadata": {"uid": promotion["cd"]["uid"]}, "spec": {
                        "source": {"repoURL": source_origin + "/a/" + project_name + ".git", "targetRevision": "deploy/staging", "path": "deploy/staging"},
                        "syncPolicy": {"automated": {"prune": False}}}}
                    promotion["cd"]["spec_sha256"] = gitops_adapter.handoff.sha256(application["spec"])
                    class CDModel:
                        def get(self, resource, namespace=None): return application
                    git_inputs = root / "promoter-inputs"; git_inputs.mkdir(mode=0o700)
                    (git_inputs / "token").write_text(context["users"]["cd-writer"])
                    shutil.copyfile(context["tls"] / "ca.crt", git_inputs / "ca.crt")
                    for p in git_inputs.iterdir(): p.chmod(0o600)
                    handoff_policy = {"schema_version": 1, "runtime": "rke2", "report": report_plan, "promotion": promotion}
                    expected_deployment_revision = fixture_revision
                    def promote(cd, obj, target, *, real_controller):
                        promotion["cd"].update(uid=obj["metadata"]["uid"], name=obj["metadata"].get("name", promotion["cd"]["name"]),
                                               namespace=obj["metadata"].get("namespace", promotion["cd"]["namespace"]), management_target=target,
                                               spec_sha256=gitops_adapter.handoff.sha256(obj["spec"]))
                        def call(apply=False):
                            if real_controller:
                                inputs = root / "operator-inputs"
                                inputs.mkdir(mode=0o700, exist_ok=True)
                                values = {"jenkins-user": b"fixture-reader", "jenkins-token": jenkins_token.encode(),
                                          "gerrit-user": b"ci-read", "gerrit-token": context["users"]["ci-read"].encode(),
                                          "reviewer-user": b"ci-review", "reviewer-token": context["users"]["ci-review"].encode(),
                                          "ca.crt": (context["tls"] / "ca.crt").read_bytes(), "cd-kubeconfig": Path(cd.prefix[2]).read_bytes()}
                                for name, data in values.items():
                                    path = inputs / name; path.write_bytes(data); path.chmod(0o600)
                                policy_file = private / "handoff-policy.json"; policy_file.write_text(json.dumps(handoff_policy)); policy_file.chmod(0o600)
                                proof = private / ("handoff-apply" if apply else "handoff-preflight")
                                cli = ["handoff", "--policy", str(policy_file), "--queue-journal", str(journal),
                                       "--review-journal-dir", str(receipt_dir), "--input-dir", str(inputs),
                                       "--git-input-dir", str(git_inputs), "--output-dir", str(proof)] + (["--apply"] if apply else [])
                                output = io.StringIO()
                                with patch.object(gitops_adapter.handoff, "ROOT", root), \
                                     patch.object(sys, "argv", cli), contextlib.redirect_stdout(output):
                                    if gitops_adapter.main() != 0:
                                        diagnostic = json.loads((proof / "handoff-proof.json").read_text()) if (proof / "handoff-proof.json").exists() else {}
                                        raise ValueError("operator-handoff-failed-" + diagnostic.get("error_code", "before-proof"))
                                result = json.loads(output.getvalue())
                                saved = json.loads((proof / "handoff-proof.json").read_text())
                                expected_phase = "controller-verified" if apply else "git-preflight-verified"
                                if saved.get("phase") != expected_phase: raise ValueError("operator-proof-phase-mismatch")
                                if any(value.decode() in output.getvalue() or value.decode() in json.dumps(saved) for key, value in values.items() if key.endswith("-token")):
                                    raise ValueError("operator-secret-in-output")
                                return result
                            return gitops_adapter.execute(handoff_policy, journal, receipt_dir, open_jenkins,
                                lambda path: api.call("ci-read", path), review_api, cd, git_inputs=git_inputs, apply=apply)
                        if not call().get("preflight_verified"): raise ValueError("jenkins-handoff-preflight-failed")
                        value = call(apply=True)
                        if not value.get("branch_push_attempted"): raise ValueError("jenkins-handoff-push-not-attempted")
                        return {"jenkins_handoff_git_verified": True, "jenkins_handoff_controller_verified": real_controller and value.get("controller_verified") is True}
                    if publication is not None:
                        expected_deployment_revision = publication.verify_job(handoff_policy, root, journal, receipt_dir,
                                                                              jenkins_token, context, prepared, git_inputs)
                    elif os.environ.get("JENKINS_ARGOCD_FIXTURE") == "1":
                        from test_argocd_reconciliation_fixture import exercise as reconcile
                        promotion["timeout_seconds"] = 90
                        observed = reconcile(unittest.TestCase(), context,
                            {"source": {"clone_url": source_origin + "/a/" + project_name + ".git"}},
                            lambda cd, obj, target: promote(cd, obj, target, real_controller=True))
                        if not observed.get("actual_argocd_reconciliation_verified") or not observed.get("jenkins_handoff_controller_verified"):
                            raise ValueError("jenkins-argocd-reconciliation-not-verified")
                    else:
                        with patch.object(gitops_adapter, "verify_target"), patch.object(gitops_adapter.handoff, "cd_observed", return_value=True):
                            promote(CDModel(), application, promotion["cd"]["management_target"], real_controller=False)
                    if api.call("ci-read", "/a/projects/" + quote(project_name, safe="") + "/branches/deploy%2Fstaging")["revision"] != expected_deployment_revision:
                        raise ValueError("jenkins-handoff-branch-mismatch")
            # Query the real API with exactly the field selection used by the
            # verifier. The no-plugin mode must reject a parameter-only job;
            # Git mode must observe the real plugin's checkout record.
            with opener.open(base + "/job/fixture-job/config.xml", timeout=5) as response: config = response.read(1024 * 1024)
            plan = {"schema_version": 1, "jenkins_origin": jenkins_origin, "job": ["fixture-job"], "build": 2 if real_gerrit is not None else 1,
                    "job_config_sha256": hashlib.sha256(config).hexdigest(), "gerrit_origin": source_origin,
                    "project": project_name, "branch": "main", "change": change_number, "patchset": patchset_number, "revision": fixture_revision}
            def read_jenkins(path, json_output=True):
                with opener.open(base + path, timeout=5) as response: raw = response.read(1024 * 1024)
                return json.loads(raw) if json_output else raw
            def read_gerrit(path):
                if real_gerrit is not None:
                    return real_gerrit[0]["api"].call("ci-read", path)
                if "/changes/" in path:
                    return {"project": "team/project", "branch": "main", "status": "MERGED", "_number": 123,
                            "current_revision": fixture_revision, "revisions": {fixture_revision: {"_number": 2, "ref": "refs/changes/23/123/2"}}}
                return {"ref": "refs/heads/main", "revision": fixture_revision}
            if git_mode:
                if verifier.queue_id_from_journal(plan, journal) != fixture_queue_id:
                    raise ValueError("fixture-journal-identity-mismatch")
                result = verifier.verify(plan, read_jenkins, read_gerrit, journal=journal)
                if result.get("jenkins_build_and_gerrit_match") is not True: raise ValueError("real-git-record-not-matched")
                if result.get("queue_identity_verified") is not True: raise ValueError("real-queue-identity-not-matched")
                if result.get("job_config_reservation_verified") is not True: raise ValueError("real-job-reservation-not-matched")
            else:
                try:
                    verifier.verify(plan, read_jenkins, lambda _: (_ for _ in ()).throw(ValueError("unexpected-gerrit-read")))
                except ValueError as error:
                    if str(error) != "jenkins-scm-record-mismatch": raise
                else: raise ValueError("unbound-jenkins-build-was-accepted")
            if real_gerrit is not None:
                failed_job = {"schema_version": 1, "gerrit_origin": source_origin, "project": project_name,
                              "credential_id": "fixture-reader", "agent_label": "built-in", "git_plugin_version": "5.11.0",
                              "build_argv": ["python3", "-c", "raise SystemExit(1)"]}
                request = Request(base + "/job/fixture-job/config.xml", data=job_renderer.render(failed_job), method="POST",
                                  headers={crumb["crumbRequestField"]: crumb["crumb"], "Content-Type": "application/xml"})
                with opener.open(request, timeout=10) as response:
                    if response.status != 200: raise ValueError("failed-job-update-not-accepted")
                with opener.open(base + "/job/fixture-job/config.xml", timeout=5) as response: failed_config = response.read(1024 * 1024)
                created = api.call("fixture-admin", "/a/changes/", {"project": project_name, "branch": "main", "subject": "Synthetic failing build", "work_in_progress": False})
                failed_change = created["_number"]
                api.call("fixture-admin", "/a/changes/" + str(failed_change) + "/edit/failing.txt", b"failing fixture\n", method="PUT", content_type="text/plain")
                api.call("fixture-admin", "/a/changes/" + str(failed_change) + "/edit:publish", {"notify": "NONE"})
                failed_item = review_bridge.current({"source": {"project": project_name, "branch": "main"}}, api.call("ci-read", "/a/changes/" + str(failed_change) + "?o=CURRENT_REVISION"))
                failed_plan = {**trigger_plan, "job_config_sha256": hashlib.sha256(failed_config).hexdigest(),
                               "change": failed_item["change"], "patchset": failed_item["patchset"], "revision": failed_item["revision"]}
                with patch.object(trigger_module, "ROOT", root):
                    trigger_module.submit(failed_plan, lambda *_: failed_config, lambda _: api.call("ci-read", "/a/changes/" + str(failed_change) + "?o=CURRENT_REVISION"), post_to_jenkins, private, apply=True)
                    failed_journal = private / (trigger_module.reservation_key(failed_plan) + ".json")
                    deadline = time.monotonic() + 90
                    while True:
                        with opener.open(base + "/job/fixture-job/lastBuild/api/json?tree=number,building,result", timeout=5) as response: failed = json.load(response)
                        if failed.get("number") == 3 and failed.get("building") is False:
                            if failed.get("result") != "FAILURE": raise ValueError("failed-build-result-mismatch")
                            break
                        if time.monotonic() >= deadline: raise TimeoutError("failed-build-timeout")
                        time.sleep(1)
                    failed_report = {**report_plan, "trigger": failed_plan}
                    failed_arguments = (failed_report, failed_journal, receipt_dir, read_jenkins, lambda path: api.call("ci-read", path), review_api)
                    if os.environ.get("JENKINS_ARGOCD_FIXTURE") == "1":
                        failure_policy = {**result_policy, "scan": {**policy, "job_config_sha256": failed_plan["job_config_sha256"]}, "report_failures": True}
                        failed_policy_file = private / "failure-scan-policy.json"; failed_policy_file.write_text(json.dumps(failure_policy)); failed_policy_file.chmod(0o600)
                        argv = ["results", "--policy", str(failed_policy_file), "--policy-sha256", scanner.canonical_digest(failure_policy),
                                "--input-dir", str(scan_inputs), "--queue-dir", str(private), "--review-dir", str(receipt_dir), "--apply"]
                        output = io.StringIO()
                        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
                            if result_scanner.main() != 0: raise ValueError("failure-scan-cli-failed")
                        if json.loads(output.getvalue())["reported"] != 1: raise ValueError("failure-scan-vote-missing")
                    elif result_reporter.report(*failed_arguments, apply=True, failure=True)["status"] != "reported": raise ValueError("failed-vote-missing")
                    if result_reporter.report(*failed_arguments, apply=True, failure=True)["status"] != "already-reported": raise ValueError("failed-vote-duplicate")
                    try: result_reporter.verify_merged(*failed_arguments)
                    except ValueError: pass
                    else: raise ValueError("failed-build-authorized-merged-evidence")
            print(json.dumps({"jenkins_version": "2.580.2", "private_network_namespace": True, "login_ready": True,
                              "freestyle_build_succeeded": True, "parameter_only_build_rejected": not git_mode,
                              "real_git_checkout_record_matched": git_mode, "gerrit_verified": real_gerrit is not None,
                              "open_patchset_queued_once": git_mode, "multiple_patchsets_verified": real_gerrit is not None,
                              "generated_job_verified": real_gerrit is not None,
                              "open_change_build_compared": real_gerrit is not None,
                              "queue_to_build_resolved": real_gerrit is not None,
                              "jenkins_real_gerrit_git_handoff_verified": real_gerrit is not None,
                              "jenkins_argocd_reconciliation_verified": real_gerrit is not None and os.environ.get("JENKINS_ARGOCD_FIXTURE") == "1",
                              "jenkins_failed_build_vote_verified": real_gerrit is not None,
                              "queue_identity_verified": git_mode, "job_config_reservation_verified": git_mode, "kubernetes_controller_verified": False,
                              "ha_verified": False, "full_stack_verified": False}, sort_keys=True))
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try: process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=10)
            log.close()
            if git_server is not None:
                git_server.shutdown(); git_server.server_close(); git_thread.join(timeout=5)
            if jenkins_tls_proxy is not None:
                jenkins_tls_proxy.shutdown(); jenkins_tls_proxy.server_close(); jenkins_tls_thread.join(timeout=5)


@unittest.skipUnless(sys.platform == "linux" and os.environ.get("JENKINS_NATIVE_RUNTIME_FIXTURE") == "1", "isolated Jenkins runtime not requested")
class JenkinsNativeRuntime(unittest.TestCase):
    def test_isolated_war_starts_and_runs_build(self):
        root = Path(__file__).resolve().parents[1]
        war = root / "private/validation/jenkins/2.580.2/jenkins.war"
        java = root / "private/validation/keycloak/OpenJDK21U-jre_x64_linux_hotspot_21.0.13_9.tar.gz"
        result = subprocess.run(["unshare", "--user", "--map-root-user", "--net", sys.executable, "-B", __file__, "--child", str(war), str(java)],
                                capture_output=True, text=True, timeout=360, check=True)
        report = json.loads(result.stdout.strip())
        self.assertTrue(report["freestyle_build_succeeded"])
        self.assertTrue(report["real_git_checkout_record_matched"] if os.environ.get("JENKINS_FIXTURE_GIT") == "1" else report["parameter_only_build_rejected"])
        self.assertFalse(report["full_stack_verified"])


def dropped_child():
    parent = int(sys.argv[2])
    if os.geteuid() != 0 or os.getppid() != parent: raise SystemExit(1)
    os.setgroups([]); os.setgid(65534); os.setuid(65534)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) or libc.prctl(38, 1, 0, 0, 0) or os.getppid() != parent: raise SystemExit(1)
    allowed = {name: os.environ[name] for name in ("PATH", "HOME", "JENKINS_HOME", "LANG", "GIT_SSL_CAINFO", "GIT_TERMINAL_PROMPT") if name in os.environ}
    os.execve(sys.argv[3], sys.argv[3:], allowed)


if __name__ == "__main__":
    if len(sys.argv) > 3 and sys.argv[1] == "--drop-child": dropped_child()
    elif len(sys.argv) == 4 and sys.argv[1] == "--child": child(sys.argv[2], sys.argv[3])
    else: unittest.main()
