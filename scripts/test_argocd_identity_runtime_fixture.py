#!/usr/bin/env python3
"""Disposable native Argo CD/Valkey acceptance inside the Keycloak fixture."""

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request
from unittest import mock

import yaml

import apply_argocd_identity as application
import configure_keycloak_argocd as identity
from test_argocd_identity_api_fixture import RolloutClient, prepare
from test_keycloak_runtime_fixture import Browser, Forms, loopback_listeners, otp
from test_rook_api_fixture import exercise as api_fixture


ARTIFACTS = {"argocd-linux-amd64": "82f2ffe3f3df32baed1229963f0866e9a8a3c74e136e9e5f36646d9004332cc5",
    "install.yaml": "a32bf36a437071a1f563ebf9e81c8a39fba9057c17db7d5d041afb7b6e3f4afe"}
VALKEY = "f2fc0aba1341baac0a1a42d41d14ea6bccf0b7a1cc45cfa2bdb906522643436c"
VALKEY_SOURCE = "db3642dfc2fb24e42313b9e9c1557cf0160acce3bf2cd366ce654ce291133b50"


def exercise(test, context):
    root = context["root"]; artifacts = Path(os.environ["ARGOCD_TEST_ARTIFACTS"]).resolve(strict=True); cache = Path(os.environ["ARGOCD_TEST_VALKEY"]).resolve(strict=True)
    for name, digest in ARTIFACTS.items():
        path = artifacts / name
        if path.is_symlink(): raise ValueError("Use the reviewed local application artifacts")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest: raise ValueError("Application artifact changed")
    with cache.open("rb") as stream:
        if cache.is_symlink() or hashlib.file_digest(stream, "sha256").hexdigest() != VALKEY: raise ValueError("Use the reviewed private Valkey fixture build")
    crds = [o for o in yaml.safe_load_all((artifacts / "install.yaml").read_text()) if isinstance(o, dict) and o.get("kind") == "CustomResourceDefinition"]
    if {o["metadata"]["name"] for o in crds} != {"applications.argoproj.io", "applicationsets.argoproj.io", "appprojects.argoproj.io"}: raise ValueError("The pinned application schema inventory changed")
    for name, source in (("argocd-server", artifacts / "argocd-linux-amd64"), ("valkey-server", cache)):
        shutil.copyfile(source, root / name); (root / name).chmod(0o755); os.chown(root / name, 65534, 65534)
    reservations = [socket.socket() for _ in range(3)]
    for handle in reservations: handle.bind(("127.0.0.1", 0))
    app_port, metrics_port, cache_port = [handle.getsockname()[1] for handle in reservations]
    origin = "https://localhost:" + str(app_port)
    credential, cache_password = (secrets.token_urlsafe(48) for _ in range(2)); sensitive = [credential, cache_password]
    report = {"argocd_configuration_apply_resume_verified": False, "argocd_tls_settings_observed": False, "argocd_mfa_login_verified": False,
        "argocd_reader_project_access_verified": False, "argocd_reader_project_write_denied": False, "argocd_admin_project_write_verified": False,
        "argocd_unassigned_user_denied": False, "argocd_loopback_listeners_verified": False, "argocd_private_values_absent_from_logs": False,
        "argocd_server_restart_observed": False,
        "argocd_artifacts_sha256": {**ARTIFACTS, "valkey_binary": VALKEY, "valkey_source": VALKEY_SOURCE}, "argocd_physical_node_ha_verified": False}
    def admin_token():
        request = Request(context["origin"] + "/realms/master/protocol/openid-connect/token", data=urlencode({"grant_type": "client_credentials", "client_id": "fixture-admin", "client_secret": context["admin_secret"]}).encode())
        with context["http"].open(request, timeout=15) as response:
            raw = response.read(128 * 1024 + 1)
            if len(raw) > 128 * 1024: raise ValueError("Excessive fixture administration response")
        value = json.loads(raw)["access_token"]; sensitive.append(value); return value
    client_plan = copy.deepcopy({"schema_version": 1, "realm_plan": context["realm_plan"], "realm_proof": {"path": str(context["realm_operation"] / "keycloak-realm-proof.json"),
        "sha256": hashlib.sha256((context["realm_operation"] / "keycloak-realm-proof.json").read_bytes()).hexdigest()},
        "application": {"kind": "argocd", "version": "3.5.0", "client_id": "fixture-argocd", "origin": origin, "client_secret_env": "ARGOCD_FIXTURE_OIDC_CREDENTIAL", "namespace": "argocd",
            "secret_name": "fixture-argocd-oidc", "secret_key": "client-secret", "group_roles": [{"group": "/fixture-readers", "role": "readonly"}, {"group": "/fixture-admins", "role": "admin"}]}})
    provider_operation = root / "private/argocd-provider"
    with mock.patch.dict(os.environ, {"KEYCLOAK_FIXTURE_ADMIN_TOKEN": admin_token(), "ARGOCD_FIXTURE_OIDC_CREDENTIAL": credential}), mock.patch.object(identity, "ROOT", root):
        test.assertTrue(identity.operate(context["bundle"], client_plan, mode="apply", output=provider_operation)["client_policy_verified"])
    plan_file = context["write"]("private/argocd-identity-plan.json", json.dumps(client_plan)); proof_file = provider_operation / "keycloak-argocd-proof.json"

    def check(client, api_root, kubectl):
        namespace, deployment, configmaps = prepare(client, kubectl, crds=crds)
        # Native server and real API are exercised; the Deployment readiness
        # object is a synthetic guard input, not a claim that workers ran Pods.
        configmaps["argocd-cm"] = kubectl(["patch", "configmap/argocd-cm", "-n", "argocd", "--type=merge", "-p", '{"data":{"admin.enabled":"false"}}', "-o", "json"])
        kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "argoproj.io/v1alpha1", "kind": "AppProject", "metadata": {"name": "default", "namespace": "argocd"}, "spec": {"sourceRepos": ["https://repo.example.invalid/fixture.git"], "destinations": [{"server": "https://kubernetes.default.svc", "namespace": "argocd"}]}})
        for name in ("argocd-cmd-params-cm", "argocd-tls-certs-cm", "argocd-ssh-known-hosts-cm"):
            kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": "argocd", "labels": {"app.kubernetes.io/part-of": "argocd"}}, "data": {}})
        for obj in (
            {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "fixture-argocd", "namespace": "argocd"}},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {"name": "fixture-argocd", "namespace": "argocd"}, "rules": [
                {"apiGroups": [""], "resources": ["configmaps", "secrets", "pods", "services", "endpoints"], "verbs": ["get", "list", "watch"]},
                {"apiGroups": [""], "resources": ["events"], "verbs": ["get", "list", "watch", "create", "patch"]},
                {"apiGroups": ["argoproj.io"], "resources": ["applications", "appprojects", "applicationsets"], "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"]}]},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": "fixture-argocd", "namespace": "argocd"}, "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "fixture-argocd"}, "subjects": [{"kind": "ServiceAccount", "name": "fixture-argocd", "namespace": "argocd"}]}):
            kubectl(["create", "-f", "-", "-o", "json"], obj)
        token = client.call(["create", "token", "fixture-argocd", "-n", "argocd", "--duration=10m"]).strip(); sensitive.append(token)
        manager_config = json.loads((api_root / "kubeconfig.json").read_text()); manager_config["users"] = [{"name": "fixture", "user": {"token": token}}]
        manager_config["contexts"][0]["context"]["namespace"] = "argocd"
        kubeconfig = context["write"]("argocd-kubeconfig.json", json.dumps(manager_config))
        context["run"](["/usr/bin/openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-subj", "/CN=localhost", "-keyout", root / "argo.key", "-out", root / "argo.csr"])
        extension = context["write"]("argo.ext", "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n")
        context["run"](["/usr/bin/openssl", "x509", "-req", "-days", "1", "-in", root / "argo.csr", "-CA", root / "ca.crt", "-CAkey", root / "ca.key", "-CAcreateserial", "-extfile", extension, "-out", root / "argo.crt"])
        encoded = lambda path: base64.b64encode(path.read_bytes()).decode()
        key = secrets.token_urlsafe(48); sensitive.append(key)
        for name, data, kind in (("argocd-secret", {"server.secretkey": base64.b64encode(key.encode()).decode()}, "Opaque"), ("argocd-server-tls", {"tls.crt": encoded(root / "argo.crt"), "tls.key": encoded(root / "argo.key")}, "kubernetes.io/tls")):
            kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": "argocd", "labels": {"app.kubernetes.io/part-of": "argocd"}}, "type": kind, "data": data})
        context["write"]("argo-tls/tls.crt", (root / "argo.crt").read_text()); context["write"]("argo-tls/tls.key", (root / "argo.key").read_text())
        cache_config = context["write"]("valkey.conf", "bind 127.0.0.1\nport " + str(cache_port) + "\nprotected-mode yes\ndaemonize no\nsave \"\"\nappendonly no\nrequirepass " + cache_password + "\nloglevel warning\n")
        app_process = cache_process = None
        try:
            for handle in reservations: handle.close()
            cache_process = context["start"]("argocd-valkey", [root / "valkey-server", cache_config], context["environment"]())
            def cache_ready():
                with socket.create_connection(("127.0.0.1", cache_port), timeout=0.5): return True
            context["wait"](cache_ready)
            app_arguments = [root / "argocd-server", "--kubeconfig=" + str(kubeconfig), "--context=storage", "--namespace=argocd", "--address=127.0.0.1", "--port=" + str(app_port),
                "--metrics-address=127.0.0.1", "--metrics-port=" + str(metrics_port), "--redis=127.0.0.1:" + str(cache_port), "--loglevel=warn"]
            app_environment = context["environment"](REDIS_PASSWORD=cache_password, ARGOCD_TLS_DATA_PATH=str(root / "argo-tls"), GRPC_ENABLE_TXT_SERVICE_CONFIG="false")
            app_process = context["start"]("argocd-server", app_arguments, app_environment)
            plan = {"schema_version": 1, "identity_plan": {"path": str(plan_file), "sha256": hashlib.sha256(plan_file.read_bytes()).hexdigest()}, "identity_proof": {"path": str(proof_file), "sha256": hashlib.sha256(proof_file.read_bytes()).hexdigest()},
                "target": {"kubeconfig_env": "DISPOSABLE_MANAGEMENT_CONFIG", "context": "storage", "cluster_uid": kubectl(["get", "ns/kube-system", "-o", "json"])["metadata"]["uid"]}, "namespace_uid": namespace["metadata"]["uid"],
                "server": {"deployment": deployment["metadata"]["name"], "uid": deployment["metadata"]["uid"], "pod_template_sha256": application.fingerprint(deployment["spec"]["template"]), "spec_sha256": application.fingerprint(deployment["spec"]), "image": deployment["spec"]["template"]["spec"]["containers"][0]["image"]}, "rollout_id": str(uuid.uuid4()),
                "configmaps": {name: {"uid": obj["metadata"]["uid"], "sha256": application.fingerprint(application.material(obj))} for name, obj in configmaps.items()},
                "projects_sha256": application.projects(client, "argocd"), "argo_ca_file": str(root / "ca.crt"), "argo_ca_sha256": hashlib.sha256((root / "ca.crt").read_bytes()).hexdigest(), "admitted_sha256": None}
            public = application.Server(plan, client_plan); context["wait"](lambda: public.get("/api/version"))
            def restart_server(obj):
                nonlocal app_process
                previous = app_process; context["stop"](previous)
                app_process = context["start"]("argocd-server-restarted", app_arguments, app_environment)
                deadline = time.monotonic() + 30
                # The outer fixture retains stopped handles for log/privacy
                # checks; readiness here observes only the new server handle.
                while True:
                    if app_process.poll() is not None: raise ValueError("The restarted application process exited")
                    try:
                        application.server_version(public, client_plan)
                        break
                    except ValueError:
                        if time.monotonic() >= deadline: raise TimeoutError("The restarted application did not become ready")
                        time.sleep(0.25)
                if previous.poll() is None or previous.pid == app_process.pid: raise ValueError("The original server process was not replaced")
                report["argocd_server_restart_observed"] = True
            managed = RolloutClient(client, kubectl, hook=restart_server)
            def action(mode, directory):
                with mock.patch.dict(os.environ, {"KEYCLOAK_FIXTURE_ADMIN_TOKEN": admin_token(), "ARGOCD_FIXTURE_OIDC_CREDENTIAL": credential}), mock.patch.object(application, "ROOT", root):
                    return application.operate(context["bundle"], plan, mode=mode, output=root / "private" / directory, wait_seconds=30, client=managed)
            plan["admitted_sha256"] = action("check-server", "argo-check")["admitted_sha256"]
            result = action("apply", "argo-apply"); test.assertTrue(result["server_settings_observed"])
            test.assertTrue(action("resume", "argo-apply")["argocd_configuration_applied"])
            report["argocd_configuration_apply_resume_verified"] = True; report["argocd_tls_settings_observed"] = True

            def api(browser, path, *, payload=None, expected=(200,)):
                request = Request(origin + path, data=None if payload is None else json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
                try:
                    with browser.opener.open(request, timeout=15) as response: status, raw = response.status, response.read(512 * 1024 + 1)
                except HTTPError as error:
                    status = error.code; error.close(); raw = b"{}"
                if status not in expected or len(raw) > 512 * 1024: raise ValueError("Unexpected application authorization result")
                return json.loads(raw)
            def login(username, seed=None):
                browser = Browser(context["trust"]); current, page = browser.fetch(origin + "/auth/login?" + urlencode({"return_url": origin + "/"}))
                current, page = browser.submit(current, Forms(page).select("username"), username=username, password=context["login_password"])
                if current.startswith(origin + "/"): raise ValueError("Application login bypassed required OTP")
                if seed is None:
                    form = Forms(page).select("totpSecret"); raw = form["fields"]["totpSecret"]; sensitive.append(raw)
                    seed = base64.b32encode(raw.encode()).decode().rstrip("=")
                    current, page = browser.submit(current, form, totp=otp(seed), userLabel="argocd-fixture")
                else: current, page = browser.submit(current, Forms(page).select("otp"), otp=otp(seed))
                if not current.startswith(origin + "/"): raise ValueError("MFA login did not return to the original application")
                user = api(browser, "/api/v1/session/userinfo")
                if user.get("loggedIn") is not True:
                    cookies = [cookie for handler in browser.opener.handlers if hasattr(handler, "cookiejar") for cookie in handler.cookiejar]
                    diagnostic = ""
                    for log_path in root.glob("argocd-server*.log"):
                        with log_path.open("rb") as stream: diagnostic += stream.read(8 * 1024 * 1024 + 1).decode(errors="replace").lower()
                    print(json.dumps({"argocd_login_failure_signals": {"returned_to_root": urlsplit(current).path == "/", "returned_to_login": urlsplit(current).path == "/login",
                        "error_query_present": "error" in urlsplit(current).query, "session_cookie_present": any(c.name == "argocd.token" for c in cookies),
                        "userinfo_known_fields": sorted(k for k in user if k in {"loggedIn", "username", "groups", "iss", "isAdmin"}),
                        "forbidden": "forbidden" in diagnostic, "noauth": "noauth" in diagnostic, "wrongpass": "wrongpass" in diagnostic,
                        "certificate_error": "x509:" in diagnostic, "token_exchange_error": "failed to get token" in diagnostic or "failed to exchange" in diagnostic,
                        "token_verification_error": "failed to verify" in diagnostic, "state_error": "invalid state" in diagnostic or "state mismatch" in diagnostic}}, sort_keys=True), flush=True)
                    raise ValueError("The application did not accept the authenticated principal")
                for handler in browser.opener.handlers:
                    if hasattr(handler, "cookiejar"):
                        sensitive.extend(cookie.value for cookie in handler.cookiejar if cookie.name == "argocd.token")
                return browser, user
            reader, reader_info = login("fixture-user", context["totp_secret"])
            if reader_info.get("username") != context["reader_email"] or reader_info.get("iss") != context["issuer"] or "/fixture-readers" not in reader_info.get("groups", []): raise ValueError("Application principal or reader group differs from the IdP identity")
            report["argocd_mfa_login_verified"] = True
            api(reader, "/api/v1/projects/default"); report["argocd_reader_project_access_verified"] = True
            project = {"project": {"metadata": {"name": "fixture-oidc-project", "namespace": "argocd"}, "spec": {"sourceRepos": ["https://repo.example.invalid/fixture.git"], "destinations": [{"server": "https://kubernetes.default.svc", "namespace": "argocd"}]}}}
            api(reader, "/api/v1/projects", payload=project, expected=(403,)); report["argocd_reader_project_write_denied"] = True
            bearer = admin_token()
            for resource, obj in (("groups", {"name": "fixture-admins"}), ("users", {"username": "fixture-denied", "email": "fixture-denied@example.invalid", "emailVerified": True, "firstName": "Fixture", "lastName": "Denied", "enabled": True, "credentials": [{"type": "password", "value": context["login_password"], "temporary": False}]}),
                ("users", {"username": "fixture-admin-user", "email": "fixture-admin-user@example.invalid", "emailVerified": True, "firstName": "Fixture", "lastName": "Admin", "enabled": True, "groups": ["/fixture-admins"], "credentials": [{"type": "password", "value": context["login_password"], "temporary": False}]})):
                request = Request(context["origin"] + "/admin/realms/" + context["realm_name"] + "/" + resource, data=json.dumps(obj).encode(), headers={"Authorization": "Bearer " + bearer, "Content-Type": "application/json"})
                with context["http"].open(request, timeout=15) as response:
                    if response.status != 201: raise ValueError("Disposable authorization identity preparation failed")
            denied, denied_info = login("fixture-denied"); api(denied, "/api/v1/projects/default", expected=(403,)); report["argocd_unassigned_user_denied"] = True
            administrator, admin_info = login("fixture-admin-user")
            if "/fixture-admins" not in admin_info.get("groups", []): raise ValueError("The intended application administrator group is absent")
            api(administrator, "/api/v1/projects", payload=project, expected=(200, 201))
            test.assertTrue(application.uid(client.get("appproject/fixture-oidc-project", "argocd")["metadata"]["uid"]))
            report["argocd_admin_project_write_verified"] = True
            loopback_listeners([(process, log) for process, log in context["processes"] if process.poll() is None], {app_port, metrics_port, cache_port})
            report["argocd_loopback_listeners_verified"] = True
        finally:
            for handle in reservations: handle.close()
            if app_process: context["stop"](app_process)
            if cache_process: context["stop"](cache_process)
        context["private_logs"](sensitive); report["argocd_private_values_absent_from_logs"] = True
        return report
    try: return api_fixture(test, management_action=check)
    finally:
        for handle in reservations: handle.close()
