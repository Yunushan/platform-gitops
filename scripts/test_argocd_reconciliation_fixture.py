#!/usr/bin/env python3
"""Opt-in native repository/controller reconciliation on an isolated real API."""

import base64
import copy
import ctypes
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

import render_gerrit_promotion_access as promotion_access
import yaml
from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
from kubernetes_management import Client, verify_target
from test_argocd_identity_api_fixture import await_crd_conditions
from test_argocd_identity_runtime_fixture import ARTIFACTS
from test_gerrit_review_runtime_fixture import artifact
from test_rook_api_fixture import BINARIES
from test_rook_api_fixture import exercise as api_fixture

VALKEY_TLS_SHA = "7e0ca79f225ef4211c47be7a77513e0ef9e88ab64ce078fb21292658c55eceef"
NATIVE_ARTIFACTS = {**ARTIFACTS,
    "git-verify-wrapper.sh": "e5271467f0d070e24ef690318206eb6a3d23ec002c9fd17d1c5cb2a52792001e",
    "gpg-wrapper.sh": "06f3503b80ff2c9753c599fe4afdd21e36d1cec7b95244186fc06c2bd96d7180"}


def process_sample(process, starttime, role, executable):
    """Observe only the original child; retain no argv, environment or paths."""
    if role not in {"cache", "repository", "controller"}: raise ValueError("Unknown native fixture role")
    result = {"role": role, "pid": process.pid, "return_code": process.poll()}
    if result["return_code"] is not None: return result
    proc = Path("/proc") / str(process.pid)
    fields = (proc / "stat").read_text().split(") ", 1)[1].split()
    if fields[19] != starttime: raise ValueError("Original native process identity changed")
    status = dict(line.split(":", 1) for line in (proc / "status").read_text().splitlines() if ":" in line)
    actual, expected = (proc / "exe").stat(), executable.stat()
    result.update(starttime=starttime, state=fields[0] if fields[0] in "RSDZTtXIWPN" else "unknown",
        executable_matches=actual.st_dev == expected.st_dev and actual.st_ino == expected.st_ino,
        major_faults=int(fields[9]), cpu_seconds=round((int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"), 3),
        uid=[int(v) for v in status["Uid"].split()], no_new_privileges=status["NoNewPrivs"].strip(), effective_capabilities=status["CapEff"].strip())
    for name in ("VmRSS", "VmHWM", "VmSwap", "RssAnon", "RssFile", "Threads"):
        if name in status: result[name] = int(status[name].split()[0])
    return result


def child():
    parent = int(sys.argv[2])
    if os.getppid() != parent: raise SystemExit(1)
    os.setgroups([]); os.setgid(65534); os.setuid(65534)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) or libc.prctl(38, 1, 0, 0, 0) or os.getppid() != parent: raise SystemExit(1)
    os.execve(sys.argv[3], sys.argv[3:], dict(os.environ))


def exercise(test, context, source_plan, callback, *, worker_context=None, artifact_context=None, compose_pair=None, compose_revision=None, recovery_source=None):
    # The enclosing Gerrit fixture already verified a new namespace with only
    # loopback. Neither the management API nor any company endpoint is borrowed.
    if os.geteuid() != 0 or not callable(callback): raise ValueError("Use the explicit isolated native fixture")
    if recovery_source is not None:
        from test_gerrit_review_runtime_fixture import namespace
        namespace()
        expected_source = {"repoURL": source_plan["source"]["clone_url"], "path": "deploy", "directory": {"include": "config.yaml"}}
        if (worker_context is not None or artifact_context is not None or compose_pair is not None
                or {key: value for key, value in recovery_source.items() if key != "targetRevision"} != expected_source
                or not re.fullmatch(r"[0-9a-f]{40}", recovery_source.get("targetRevision", ""))):
            raise ValueError("Use only a pinned single source in the disposable recovery fixture")
    if compose_pair is not None and (worker_context is None or not callable(compose_pair)
                                     or not isinstance(compose_revision, str) or not re.fullmatch(r"[a-f0-9]{40}", compose_revision)):
        raise ValueError("Docker reconciliation requires the original isolated worker fixture")
    if artifact_context is not None and (worker_context is None or set(artifact_context) != {"prepare", "verify"} or not all(callable(v) for v in artifact_context.values())):
        raise ValueError("Artifact reconciliation requires explicit preparation/verification and the original worker laboratory")
    if worker_context is not None:
        from test_gerrit_review_runtime_fixture import namespace
        namespace(worker_context)
        verify_target(worker_context["client"], worker_context["target"])
    artifacts = Path(os.environ["ARGOCD_TEST_ARTIFACTS"]).resolve(strict=True)
    valkey = Path(os.environ["ARGOCD_TEST_VALKEY"]).resolve(strict=True)
    for name, digest in NATIVE_ARTIFACTS.items(): artifact(artifacts / name, digest)
    artifact(valkey, VALKEY_TLS_SHA)
    crds = [o for o in yaml.safe_load_all((artifacts / "install.yaml").read_text()) if isinstance(o, dict) and o.get("kind") == "CustomResourceDefinition"]
    if {o["metadata"]["name"] for o in crds} != {"applications.argoproj.io", "applicationsets.argoproj.io", "appprojects.argoproj.io"}: raise ValueError("Unexpected reviewed Argo CD schema inventory")
    scopes = []; report = {}

    def check(manager, root, kubectl):
        scopes.append(root)
        processes = []; sensitive = [*context["users"].values()]; native_processes = {}; native_ready = {}
        from test_k3s_vm_fixture import guest_identity_lifetime
        token_lifetime=guest_identity_lifetime(worker_context)
        report["argocd_fixture_token_lifetime"]=token_lifetime
        def write(name, value):
            path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8"); path.chmod(0o600); os.chown(path, 65534, 65534)
            for parent in path.parents:
                if parent == root: break
                os.chown(parent, 65534, 65534); parent.chmod(0o700)
            return path
        def create(obj): return kubectl(["create", "-f", "-", "-o", "json"], obj)
        def secret(name, values, label=None):
            return create({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": "argocd", "labels": {"app.kubernetes.io/part-of": "argocd", **({"argocd.argoproj.io/secret-type": label} if label else {})}},
                "type": "Opaque", "data": {key: base64.b64encode(value.encode()).decode() for key, value in values.items()}})
        def identity(name, namespace, rules):
            create({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": name, "namespace": namespace}})
            create({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {"name": name, "namespace": namespace}, "rules": rules})
            create({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": name, "namespace": namespace}, "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
                "subjects": [{"kind": "ServiceAccount", "name": name, "namespace": namespace}]})
            token = manager.call(["create", "token", name, "-n", namespace, "--duration="+token_lifetime]).strip(); sensitive.append(token); return token
        def kubeconfig(name, token):
            value = copy.deepcopy(json.loads((root / "kubeconfig.json").read_text()))
            value["users"] = [{"name": "fixture", "user": {"token": token}}]
            selected = [c for c in value["contexts"] if c["name"] == value["current-context"]]
            if len(selected) != 1: raise ValueError("Original fixture context changed")
            selected[0]["context"].update(namespace="argocd", user="fixture")
            return write(name + ".json", json.dumps(value))
        def start(name, arguments, environment):
            log = (root / (name + ".log")).open("w+b")
            process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--child", str(os.getpid()), *map(str, arguments)],
                cwd=root, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            processes.append((process, log)); observe(name, process, arguments); return process
        def observe(name, process, arguments):
            starttime = (Path("/proc") / str(process.pid) / "stat").read_text().split(") ", 1)[1].split()[19]
            native_processes[name] = {"process": process, "starttime": starttime, "executable": Path(arguments[0]), "started": time.monotonic()}
            print(json.dumps({"native_argocd_stage": "starting", "role": name, "pid": process.pid}), flush=True)
            return process
        def listener_ready(name):
            value = native_processes[name]
            sample = process_sample(value["process"], value["starttime"], name, value["executable"])
            test.assertTrue(sample["executable_matches"]); test.assertEqual(sample["uid"], [65534] * 4)
            test.assertEqual(sample["no_new_privileges"], "1"); test.assertEqual(int(sample["effective_capabilities"], 16), 0)
            native_ready[name] = sample
            print(json.dumps({"native_argocd_stage": "tcp-listener-ready", "role": name,
                "elapsed_seconds": round(time.monotonic() - value["started"], 3)}), flush=True)
        def healthy():
            if any(p.poll() is not None for p, _ in processes): raise ValueError("A disposable native Argo CD process stopped")
        def wait(function, seconds=30):
            deadline = time.monotonic() + seconds
            while True:
                healthy()
                try:
                    if function(): return
                except (OSError, ValueError): pass
                if time.monotonic() >= deadline: raise TimeoutError("Native Argo CD fixture readiness deadline")
                time.sleep(0.25)

        for namespace in ("argocd", "synthetic-application"):
            create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace, "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
        for obj in crds:
            await_crd_conditions(manager, create(obj))
        api_config = json.loads((root / "kubeconfig.json").read_text()); destination = api_config["clusters"][0]["cluster"]["server"]
        target = {"kubeconfig_env": "DISPOSABLE_ARGOCD_READ_CONFIG", "context": api_config["current-context"], "cluster_uid": manager.get("namespace/kube-system")["metadata"]["uid"]}
        controller_token = identity("fixture-controller", "argocd", [
            {"apiGroups": [""], "resources": ["configmaps", "secrets"], "verbs": ["get", "list", "watch"]},
            {"apiGroups": [""], "resources": ["events"], "verbs": ["create", "patch"]},
            {"apiGroups": ["argoproj.io"], "resources": ["applications", "applications/status"], "verbs": ["get", "list", "watch", "update", "patch"]},
            {"apiGroups": ["argoproj.io"], "resources": ["appprojects"], "verbs": ["get", "list", "watch"]}])
        workload_rules = [{"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "list", "watch", "create", "update", "patch"]}]
        if worker_context is not None:
            workload_rules.extend([
                {"apiGroups": [""], "resources": ["services"], "verbs": ["get", "list", "watch", "create", "update", "patch"]},
                {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "list", "watch", "create", "update", "patch"]}])
        deploy_token = identity("fixture-deployer", "synthetic-application", workload_rules)
        for obj in promotion_access.cd_objects({"management_target": target, "namespace": "argocd", "name": "application"}, {"account": "fixture-promoter-read", "namespace": "argocd"}): create(obj)
        read_token = manager.call(["create", "token", "fixture-promoter-read", "-n", "argocd", "--duration="+token_lifetime]).strip(); sensitive.append(read_token)
        artifact_state = artifact_context["prepare"](manager, target) if artifact_context is not None else None
        if artifact_state is not None:
            for extra in promotion_access.artifact_workload_objects({"management_target": target}, {"account": "fixture-promoter-read", "namespace": "argocd"}, artifact_state["policy"]): create(extra)
        controller_config = kubeconfig("controller-kubeconfig", controller_token); read_config = kubeconfig("promoter-kubeconfig", read_token)
        api_settings = {}
        api_policy = {}
        api_secrets = {}
        if compose_pair is not None:
            create({"apiVersion": "argoproj.io/v1alpha1", "kind": "AppProject",
                    "metadata": {"name": "default", "namespace": "argocd"},
                    "spec": {"sourceRepos": [], "destinations": []}})
            api_token = identity("fixture-argocd-api", "argocd", [
                {"apiGroups": [""], "resources": ["configmaps", "secrets", "pods", "services", "endpoints"], "verbs": ["get", "list", "watch"]},
                {"apiGroups": [""], "resources": ["events"], "verbs": ["get", "list", "watch", "create", "patch"]},
                {"apiGroups": ["argoproj.io"], "resources": ["applications", "appprojects", "applicationsets"], "verbs": ["get", "list", "watch"]}])
            kubeconfig("api-kubeconfig", api_token)
            appset_token = identity("fixture-argocd-appset", "argocd", [
                {"apiGroups": [""], "resources": ["configmaps", "secrets"], "verbs": ["get", "list", "watch"]},
                {"apiGroups": [""], "resources": ["events"], "verbs": ["create", "patch"]},
                {"apiGroups": ["argoproj.io"], "resources": ["applicationsets", "appprojects"], "verbs": ["get", "list", "watch"]},
                {"apiGroups": ["argoproj.io"], "resources": ["applications"], "verbs": ["get", "list", "watch", "create", "update", "patch"]},
                {"apiGroups": ["argoproj.io"], "resources": ["applicationsets/status", "applicationsets/finalizers"],
                 "verbs": ["get", "update", "patch"]}])
            appset_config = kubeconfig("appset-kubeconfig", appset_token)
            for verb, resource, namespace in (("delete", "applications.argoproj.io", "argocd"),
                                               ("create", "applications.argoproj.io", "synthetic-application"),
                                               ("patch", "secrets", "argocd")):
                denial = run_bounded([manager.prefix[0], "--kubeconfig", str(appset_config), "auth", "can-i", verb, resource,
                                      "-n", namespace], text=True, timeout=20, check=False)
                test.assertEqual((denial.returncode, denial.stdout.strip()), (1, "no"))
            notifications_token = identity("fixture-argocd-notifications", "argocd", [
                {"apiGroups": [""], "resources": ["configmaps", "secrets"], "verbs": ["list", "watch"]},
                {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["argocd-notifications-cm"], "verbs": ["get"]},
                {"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["argocd-notifications-secret"], "verbs": ["get"]},
                {"apiGroups": ["argoproj.io"], "resources": ["applications", "appprojects"], "verbs": ["get", "list", "watch"]},
                {"apiGroups": ["argoproj.io"], "resources": ["applications"], "resourceNames": ["application"], "verbs": ["patch", "update"]}])
            notifications_config = kubeconfig("notifications-kubeconfig", notifications_token)
            for verb, resource, namespace in (("patch", "secrets", "argocd"),
                                               ("patch", "applications.argoproj.io/other-application", "argocd"),
                                               ("patch", "applications.argoproj.io/application", "synthetic-application")):
                denial = run_bounded([manager.prefix[0], "--kubeconfig", str(notifications_config), "auth", "can-i",
                                      verb, resource, "-n", namespace], text=True, timeout=20, check=False)
                test.assertEqual((denial.returncode, denial.stdout.strip()), (1, "no"))
            observer_password = secrets.token_urlsafe(32); sensitive.append(observer_password)
            observer_hash = run_bounded([artifacts / "argocd-linux-amd64", "account", "bcrypt", "--password", observer_password],
                                        text=True, timeout=20, check=True).stdout.strip()
            sensitive.append(observer_hash)
            write("observer-password", observer_password)
            api_settings = {"admin.enabled": "false", "accounts.observer": "login"}
            api_policy = {"policy.csv": "p, role:fixture-observer, applications, get, fixture/application, allow\ng, observer, role:fixture-observer\n"}
            api_secrets = {"accounts.observer.password": observer_hash, "accounts.observer.passwordMtime": "2020-01-01T00:00:00Z"}
        labels = {"app.kubernetes.io/part-of": "argocd"}
        if compose_pair is not None:
            notify_token = secrets.token_urlsafe(32)
            sensitive.append(notify_token)
            write("notification-token", notify_token)
            secret("argocd-notifications-secret", {"fixture-webhook-token": notify_token})
            create({"apiVersion": "v1", "kind": "ConfigMap",
                    "metadata": {"name": "argocd-notifications-cm", "namespace": "argocd", "labels": labels},
                    "data": {
                        "service.webhook.receipt": yaml.safe_dump({
                            "url": "https://192.0.2.1:19090",
                            "headers": [{"name": "Authorization", "value": "Bearer $fixture-webhook-token"},
                                        {"name": "Content-Type", "value": "application/json"}],
                        }),
                        "template.tested-revision": yaml.safe_dump({"webhook": {"receipt": {
                            "method": "POST", "path": "/sync",
                            "body": '{"application": "{{.app.metadata.name}}", "revision": "{{.app.status.sync.revision}}"}',
                        }}}),
                        "trigger.tested-sync": yaml.safe_dump([{
                            "when": f"app.status.sync.status == 'Synced' and app.status.health.status == 'Healthy' and app.status.sync.revision == '{compose_revision}'",
                            "oncePer": "app.status.sync.revision", "send": ["tested-revision"],
                        }]),
                        "template.recovery-witness": yaml.safe_dump({"webhook": {"receipt": {
                            "method": "POST", "path": "/sync",
                            "body": '{"application": "{{.app.metadata.name}}", "revision": "{{.app.status.sync.revision}}", "event": "recovery-witness"}',
                        }}}),
                        "trigger.recovery-witness": yaml.safe_dump([{
                            "when": "app.metadata.annotations['fixture.example.invalid/environment'] == 'recovery-checked'",
                            "oncePer": "app.status.sync.revision", "send": ["recovery-witness"],
                        }]),
                    }})
        inclusions = [{"apiGroups": [""], "kinds": ["ConfigMap"], "clusters": ["*"]}]
        if worker_context is not None:
            inclusions.extend([{"apiGroups": [""], "kinds": ["Service"], "clusters": ["*"]}, {"apiGroups": ["apps"], "kinds": ["Deployment"], "clusters": ["*"]}])
        for name, values in (("argocd-cm", {"cluster.inClusterEnabled": "false", "resource.inclusions": json.dumps(inclusions), **api_settings}), ("argocd-rbac-cm", api_policy), ("argocd-cmd-params-cm", {}), ("argocd-ssh-known-hosts-cm", {}), ("argocd-tls-certs-cm", {})):
            create({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": "argocd", "labels": labels}, "data": values})
        secret("argocd-secret", {"server.secretkey": secrets.token_urlsafe(48), **api_secrets})
        repository_user = context.get("repository_user", "ci-read")
        secret("fixture-repository", {"type": "git", "url": source_plan["source"]["clone_url"], "username": repository_user, "password": context["users"][repository_user], "forceHttpBasicAuth": "true"}, "repository")
        secret("fixture-cluster", {"name": "fixture-destination", "server": destination, "namespaces": "synthetic-application", "clusterResources": "false", "shard": "0",
            "config": json.dumps({"bearerToken": deploy_token, "tlsClientConfig": {"insecure": False, "caData": api_config["clusters"][0]["cluster"]["certificate-authority-data"]}})}, "cluster")
        allowed = [{"group": "", "kind": "ConfigMap"}]
        if worker_context is not None: allowed.extend([{"group": "", "kind": "Service"}, {"group": "apps", "kind": "Deployment"}])
        create({"apiVersion": "argoproj.io/v1alpha1", "kind": "AppProject", "metadata": {"name": "fixture", "namespace": "argocd"}, "spec": {"sourceRepos": [source_plan["source"]["clone_url"]],
            "destinations": [{"server": destination, "namespace": "synthetic-application"}], "namespaceResourceWhitelist": allowed, "clusterResourceBlacklist": [{"group": "*", "kind": "*"}]}})
        application = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": {"name": "application", "namespace": "argocd"}, "spec": {"project": "fixture",
            "source": {"repoURL": source_plan["source"]["clone_url"], "targetRevision": "deploy/staging", "path": "deploy/staging", **({} if artifact_context is not None else {"directory": {"include": "web.json" if worker_context is not None else "configmap.yaml"}})},
            "destination": {"server": destination, "namespace": "synthetic-application"}, "syncPolicy": {"automated": {"prune": False, "selfHeal": True}}}}
        if recovery_source is not None:
            application["spec"]["source"] = recovery_source
        if compose_pair is not None:
            appset = create({"apiVersion": "argoproj.io/v1alpha1", "kind": "ApplicationSet",
                             "metadata": {"name": "fixture-generator", "namespace": "argocd"},
                             "spec": {"goTemplate": True, "goTemplateOptions": ["missingkey=error"],
                                      "generators": [{"list": {"elements": [{"name": "application", "environment": "staging"}]}}],
                                      "syncPolicy": {"preserveResourcesOnDeletion": True},
                                      "template": {"metadata": {"name": "{{ .name }}",
                                                                "annotations": {"fixture.example.invalid/environment": "{{ .environment }}",
                                                                                "notifications.argoproj.io/subscribe.tested-sync.receipt": "fixture",
                                                                                "notifications.argoproj.io/subscribe.recovery-witness.receipt": "fixture"}},
                                                   "spec": application["spec"]}}})
            test.assertIsNone(manager.get("application/application", "argocd", optional=True))
        else:
            obj = create(application)
        for name, binary in (("argocd", artifacts / "argocd-linux-amd64"), ("valkey-server", valkey)):
            shutil.copyfile(binary, root / "bin" / name); (root / "bin" / name).chmod(0o755)
        for name in ("argocd-repo-server", "argocd-application-controller", "argocd-git-ask-pass"):
            os.link(root / "bin/argocd", root / "bin" / name)
        for name in ("git-verify-wrapper.sh", "gpg-wrapper.sh"):
            shutil.copyfile(artifacts / name, root / "bin" / name); (root / "bin" / name).chmod(0o755)
        # Native Argo CD uses Git and kubectl from this isolated PATH. Both
        # management and workload identities are explicit private kubeconfigs.
        run_bounded(["openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-subj", "/CN=localhost", "-keyout", str(root / "services.key"), "-out", str(root / "services.csr")], timeout=20, check=True)
        certificates = [("services", "serverAuth"), ("client", "clientAuth")]
        if compose_pair is not None:
            run_bounded(["openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                         "-subj", "/CN=argocd-api-fixture", "-keyout", str(root / "api.key"),
                         "-out", str(root / "api.csr")], timeout=20, check=True)
            certificates.append(("api", "serverAuth"))
        for name, usage in certificates:
            names = "DNS:localhost,IP:127.0.0.1"
            if compose_pair is not None: names += ",DNS:argocd-repo-server,IP:192.0.2.1"
            extension = write(name + ".ext", "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=" + usage
                              + "\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\nsubjectAltName=" + names + "\n")
            csr = "api.csr" if name == "api" else "services.csr"
            run_bounded(["openssl", "x509", "-req", "-days", "1", "-in", str(root / csr), "-CA", str(root / "ca.crt"), "-CAkey", str(root / "ca.key"), "-CAcreateserial", "-extfile", str(extension), "-out", str(root / (name + ".crt"))], timeout=20, check=True)
        write("services.key", (root / "services.key").read_text())
        write("config/reposerver/tls/tls.crt", (root / "services.crt").read_text()); write("config/reposerver/tls/tls.key", (root / "services.key").read_text())
        if compose_pair is not None:
            create({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "argocd-server-tls", "namespace": "argocd", "labels": labels},
                    "type": "kubernetes.io/tls", "data": {name: base64.b64encode((root / filename).read_bytes()).decode()
                                                          for name, filename in (("tls.crt", "api.crt"), ("tls.key", "api.key"))}})
        write("git-trust/" + urlsplit(source_plan["source"]["clone_url"]).hostname, (context["tls"] / "ca.crt").read_text())
        for name in ("services.crt", "client.crt"): write(name, (root / name).read_text())
        temporary = root / "scratch"; temporary.mkdir(mode=0o700); os.chown(temporary, 65534, 65534)
        reservations = [socket.socket() for _ in range(4)]
        for sock in reservations: sock.bind(("127.0.0.1", 0))
        repo_port, repo_metrics, controller_metrics, cache_port = [s.getsockname()[1] for s in reservations]
        cache_password = secrets.token_urlsafe(48); sensitive.append(cache_password)
        config = write("valkey.conf", "bind 127.0.0.1\nport 0\ntls-port " + str(cache_port) + "\ntls-cert-file " + str(root / "services.crt") + "\ntls-key-file " + str(root / "services.key") + "\ntls-ca-cert-file " + str(root / "ca.crt") + "\ntls-auth-clients yes\nprotected-mode yes\ndaemonize no\nsave \"\"\nappendonly no\nrequirepass " + cache_password + "\nloglevel warning\n")
        environment = {"PATH": str(root / "bin") + ":/usr/bin:/bin", "HOME": str(root), "TMPDIR": str(temporary), "LC_ALL": "C", "GOMAXPROCS": "2", "REDIS_PASSWORD": cache_password,
            "ARGOCD_APP_CONF_PATH": str(root / "config"), "ARGOCD_TLS_DATA_PATH": str(root / "git-trust"), "ARGOCD_ASK_PASS_SOCK": str(temporary / "askpass.sock"),
            "ARGOCD_GIT_MODULES_ENABLED": "false", "ARGOCD_GPG_ENABLED": "false", "ARGOCD_CONTROLLER_REPLICAS": "1", "ARGOCD_CONTROLLER_SHARD": "0", "GRPC_ENABLE_TXT_SERVICE_CONFIG": "false"}
        cache_args = ["--redis=127.0.0.1:" + str(cache_port), "--redis-use-tls", "--redis-ca-certificate=" + str(root / "ca.crt"), "--redis-client-certificate=" + str(root / "client.crt"), "--redis-client-key=" + str(root / "services.key")]
        from contextlib import ExitStack
        pair_scope = ExitStack()
        pair = None
        try:
            for sock in reservations: sock.close()
            start("cache", [root / "bin/valkey-server", config], environment)
            def listening(port):
                with socket.create_connection(("127.0.0.1", port), timeout=0.5): return True
            wait(lambda: listening(cache_port))
            listener_ready("cache")
            if compose_pair is not None:
                pair = pair_scope.enter_context(compose_pair(root, controller_config, target, cache_port, cache_password))

                def generated():
                    return manager.get("application/application", "argocd", optional=True)

                wait(generated, seconds=45)
                obj = generated()
                original_uid = obj["metadata"]["uid"]
                test.assertTrue(any(owner["uid"] == appset["metadata"]["uid"] and owner["kind"] == "ApplicationSet"
                                    and owner.get("controller") is True for owner in obj["metadata"]["ownerReferences"]))
                test.assertEqual(obj["metadata"]["annotations"]["fixture.example.invalid/environment"], "staging")
                test.assertFalse(obj["metadata"].get("finalizers"))
                kubectl(["patch", "applicationset/fixture-generator", "-n", "argocd", "--type=json", "-p",
                         json.dumps([{"op": "replace", "path": "/spec/generators/0/list/elements/0/environment", "value": "reviewed"}]),
                         "-o", "json"])

                def updated():
                    return generated()["metadata"]["annotations"].get("fixture.example.invalid/environment") == "reviewed"

                wait(updated, seconds=45)
                obj = generated()
                test.assertEqual(obj["metadata"]["uid"], original_uid)
                pair.stop_notifications()
                print("ApplicationSet generated and updated the same Application before promotion.", flush=True)
                report.update(argocd_compose_applicationset_generation_verified=True,
                              argocd_compose_applicationset_update_verified=True,
                              argocd_compose_applicationset_rbac_verified=True)
            else:
                start("repository", [root / "bin/argocd-repo-server", "--address=127.0.0.1", "--port=" + str(repo_port), "--metrics-address=127.0.0.1", "--metrics-port=" + str(repo_metrics),
                "--client-ca-path=" + str(root / "ca.crt"), "--revision-cache-expiration=1s", "--parallelismlimit=1", "--loglevel=debug", *cache_args], environment)
                wait(lambda: listening(repo_port))
                listener_ready("repository")
                start("controller", [root / "bin/argocd-application-controller", "--kubeconfig=" + str(controller_config), "--context=" + api_config["current-context"], "--namespace=argocd", "--repo-server=127.0.0.1:" + str(repo_port),
                "--repo-server-ca-cert-path=" + str(root / "ca.crt"), "--repo-server-client-cert-path=" + str(root / "client.crt"), "--repo-server-client-cert-key-path=" + str(root / "services.key"),
                "--metrics-port=" + str(controller_metrics), "--app-resync=2", "--app-resync-jitter=0", "--status-processors=1", "--operation-processors=1", "--loglevel=warn", *cache_args], environment)
                wait(lambda: listening(controller_metrics))
                listener_ready("controller")
            with mock.patch.dict(os.environ, {target["kubeconfig_env"]: str(read_config)}):
                reader = Client(target)
                verify_target(reader, target)
                for attributes in ({"verb": "get", "group": "argoproj.io", "resource": "applications", "name": "outside-reviewed-application"},
                        {"verb": "list", "group": "argoproj.io", "resource": "applications"}, {"verb": "get", "resource": "secrets", "name": "argocd-secret"},
                        {"verb": "get", "resource": "nodes", "name": "outside-reviewed-worker-inventory", "namespace": ""}):
                    review = json.loads(reader.call(["create", "--raw", "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", "-f", "-"],
                        payload=json.dumps({"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview", "spec": {"resourceAttributes": {"namespace": "argocd", **attributes}}}).encode()))
                    test.assertIs(review["status"]["allowed"], False); test.assertFalse(review["status"].get("evaluationError"))
                denied = run_bounded([*reader.prefix, "--request-timeout=10s", "auth", "can-i", "patch", "applications.argoproj.io", "-n", "argocd"], text=True, timeout=20, check=False)
                test.assertEqual(denied.returncode, 1); test.assertEqual(denied.stdout.strip(), "no")
                denied_write = run_bounded([*reader.prefix, "--request-timeout=10s", "patch", "applications.argoproj.io/application", "-n", "argocd", "--dry-run=server", "--type=merge", "-p", '{"metadata":{"annotations":{"fixture":"denied"}}}'], text=True, timeout=20, check=False)
                test.assertNotEqual(denied_write.returncode, 0); test.assertIn("forbidden", denied_write.stderr.lower())
                outcome = callback(reader, obj, target)
            if artifact_context is not None:
                report.update(artifact_context["verify"](manager, artifact_state, outcome))
            else:
                observed = manager.get("configmap/synthetic-application", "synthetic-application")
                test.assertIsNotNone(observed); test.assertEqual(observed["data"], {"result": "tested"})
                test.assertTrue(observed["metadata"]["uid"])
                test.assertIn("argocd.argoproj.io/tracking-id", observed["metadata"].get("annotations", {}))
                if worker_context is not None:
                    from test_argocd_worker_workload import verify_workload
                    report.update(verify_workload(test, worker_context))
            healthy()
            after = {name: process_sample(value["process"], value["starttime"], name, value["executable"]) for name, value in native_processes.items()}
            for name, value in after.items():
                test.assertTrue(value["executable_matches"]); test.assertEqual(value["uid"], [65534] * 4)
                test.assertEqual(value["no_new_privileges"], "1"); test.assertEqual(int(value["effective_capabilities"], 16), 0)
                test.assertEqual((value["pid"], value["starttime"]), (native_ready[name]["pid"], native_ready[name]["starttime"]))
            report["argocd_native_process_before"] = native_ready
            report["argocd_native_process_after"] = after
            logs = b"".join(read_bounded_bytes(root / (name + ".log"), max_bytes=8 * 1024 ** 2) for name in native_processes)
            if pair is not None:
                observed = manager.get("application/application", "argocd")
                test.assertEqual(observed["status"]["sync"]["revision"], compose_revision)
                test.assertEqual(observed["status"]["health"]["status"], "Healthy")
                test.assertFalse(observed["metadata"]["annotations"].get("notified.notifications.argoproj.io"))
                pair.replace_notifications()
                report.update(pair.verify(compose_revision, sensitive))

                def recorded_notification():
                    current = manager.get("application/application", "argocd")
                    return current["metadata"]["annotations"].get("notified.notifications.argoproj.io")

                wait(recorded_notification, seconds=30)
                first_delivery_state = recorded_notification()
                pair.replace_notifications()
                kubectl(["patch", "applicationset/fixture-generator", "-n", "argocd", "--type=json", "-p",
                         json.dumps([{"op": "replace", "path": "/spec/generators/0/list/elements/0/environment", "value": "recovery-checked"}]),
                         "-o", "json"])
                pair.verify_notification_recovery(compose_revision)
                wait(lambda: recorded_notification() not in (None, "", first_delivery_state), seconds=30)
                pair.verify_notification_recovery(compose_revision)
                report["argocd_compose_notifications_rbac_verified"] = True
                report["argocd_compose_notifications_outage_recovery_verified"] = True
                report["argocd_compose_notification_deduplication_verified"] = True
                report.update(pair.verify_cache_recovery(manager, compose_revision))
                logs += pair.logs()
            test.assertFalse(any(value.encode() in logs for value in sensitive))
            report.update(outcome)
            report.update(argocd_status_modeled=False, actual_argocd_reconciliation_verified=True, real_configmap_reconciliation_verified=artifact_context is None,
                promoter_kubernetes_write_denied=True, promoter_cd_out_of_scope_reads_denied=True, argocd_repository_mtls_configured=True, argocd_cache_mtls_configured=True,
                argocd_artifacts_sha256={**NATIVE_ARTIFACTS, "valkey_tls_binary": VALKEY_TLS_SHA, **({"kubectl": BINARIES["kubectl"]} if worker_context is not None else BINARIES)}, argocd_worker_execution_verified=worker_context is not None, argocd_physical_ha_verified=False)
        except Exception:
            if pair is not None:
                pair.diagnose()
            observed = []
            for name, value in native_processes.items():
                try: sample = process_sample(value["process"], value["starttime"], name, value["executable"])
                except (OSError, ValueError, KeyError) as error: sample = {"role": name, "observation_error": type(error).__name__}
                sample["elapsed_seconds"] = round(time.monotonic() - value["started"], 3); observed.append(sample)
            print(json.dumps({"native_argocd_startup_processes": observed,
                "native_argocd_log_bytes": {name: (root / (name + ".log")).stat().st_size for name in native_processes}}, sort_keys=True), flush=True)
            text = "\n".join(read_bounded_bytes(root / (name + ".log"), max_bytes=8 * 1024 ** 2).decode(errors="replace").lower() for name in ("cache", "repository", "controller") if (root / (name + ".log")).exists())
            print(json.dumps({"native_argocd_failure_signals": {key: value in text for key, value in {"unknown_flag": "unknown flag", "forbidden": "forbidden", "certificate_error": "x509:", "authentication_error": "authentication failed", "unauthorized": "unauthorized", "credential_expired": "token has expired", "connection_refused": "connection refused", "missing_file": "no such file", "permission_denied": "permission denied", "invalid_config": "failed to unmarshal"}.items()}}, sort_keys=True), flush=True)
            messages = []
            diagnostic_log = root / "repository.log"
            for line in (diagnostic_log.read_text(errors="replace").splitlines()[-50:] if diagnostic_log.exists() else []):
                try: row = json.loads(line)
                except ValueError: continue
                value = " | ".join(str(row.get(key, "")) for key in ("msg", "error", "grpc.error"))
                for secret_value in sensitive: value = value.replace(secret_value, "<redacted>")
                value = re.sub(r"https?://[^\s\"']+|/tmp/[^\s\"']+", "<fixture-location>", value)
                messages.append(re.sub(r"[A-Za-z0-9_+/=-]{32,}", "<redacted>", value)[:700])
            print(json.dumps({"native_repository_diagnostics": messages}), flush=True)
            raise
        finally:
            pair_scope.close()
            for sock in reservations: sock.close()
            for process, log in reversed(processes):
                if process.poll() is None:
                    if os.getpgid(process.pid) != process.pid: raise ValueError("A fixture process escaped its original group")
                    os.killpg(process.pid, signal.SIGTERM)
                    try: process.wait(timeout=10)
                    except subprocess.TimeoutExpired: os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=10)
                log.close()

    if worker_context is None:
        api_fixture(test, management_action=check, disk_workspace=True)
    else:
        # Borrow only the original bounded KVM laboratory's verified API. The
        # native processes receive their own private files and scoped tokens.
        with tempfile.TemporaryDirectory(prefix="platform-argocd-worker-", dir="/var/tmp") as temporary:
            root = Path(temporary); root.chmod(0o700); os.chown(root, 65534, 65534)
            (root / "bin").mkdir(mode=0o700); os.chown(root / "bin", 65534, 65534)
            kubectl = Path(worker_context["client"].prefix[0]); artifact(kubectl, BINARIES["kubectl"])
            shutil.copyfile(kubectl, root / "bin/kubectl"); (root / "bin/kubectl").chmod(0o755)
            # Freeze the original verified client's configuration rather than
            # assuming the hosting distribution's kubeconfig filename.
            config = worker_context["client"].call(["config", "view", "--minify", "--flatten", "--raw", "-o", "json"])
            (root / "kubeconfig.json").write_text(config); (root / "kubeconfig.json").chmod(0o600)
            run_bounded(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "1", "-subj", "/CN=isolated-native-services",
                "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                "-addext", "subjectKeyIdentifier=hash",
                "-keyout", str(root / "ca.key"), "-out", str(root / "ca.crt")], timeout=20, check=True)
            for name in ("ca.crt", "ca.key"):
                (root / name).chmod(0o600); os.chown(root / name, 65534, 65534)
            def worker_kubectl(arguments, obj=None):
                return json.loads(worker_context["client"].call(arguments, payload=json.dumps(obj).encode() if obj is not None else None))
            check(worker_context["client"], root, worker_kubectl)
    test.assertTrue(scopes); test.assertTrue(all(not path.exists() for path in scopes))
    report["temporary_argocd_api_and_scope_removed"] = worker_context is None
    report["temporary_native_argocd_scope_removed"] = True
    return report


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--child": child()
