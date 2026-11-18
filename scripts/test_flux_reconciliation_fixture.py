"""Native Flux GitRepository and Kustomization reconciliation on an isolated API."""

from contextlib import ExitStack
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
from unittest.mock import patch

import yaml

from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
from kubernetes_management import Client, KubernetesOperationError, verify_target
from test_argocd_identity_api_fixture import await_crd_conditions
import test_argocd_reconciliation_fixture as native
from test_rook_api_fixture import BINARIES, exercise as disposable_api
from verify_oci_archive import verify_archive


FLUX_SHA256 = "7cd043a823666727b4b9de408346148b28741b47eb0b3e4237ab7259d4ad3ab8"
CONTROLLERS = {
    "source-controller": (
        "5386533e407df789f7340fb21e28ba8251f4b497cc8a1077fa769e17ae813010",
        "bd6333778489646206f72d7c15b6119af9c7f340d676d92ac868064fd2718df8",
        "571166e2b00e8b85e221117999a30f6b37475be00cbef7e1f1a205e589021557",
        "4e1d6b945f2c882c05ab3c6b4468818e5c4a09a861bce061b1add4eead898d4e", 138570280),
    "kustomize-controller": (
        "770ea7d04ce37161397cd5f42fa5c54e277e2e7877c4b85fa1b9d23298aa45a3",
        "729760547784f384aa57eb1979e84e5eb8dabddc3564656f0517d31026198986",
        "168bf03d8ade06313128ca102ffe72e1398215839916953a8e5eadff5e7009cc",
        "dbd4e21d14cf6a0a12c24c3a2541280d9999f3a85cce6d2e3087f9ea9f6433fa", 114929590),
}
NAMESPACE = "flux-system"
DESTINATION = "synthetic-application"
BRANCH = "deploy/staging"


def extract_binary(assets, binary):
    image_digest, archive_digest, layer, executable_digest, executable_size = CONTROLLERS[binary.name]
    path = assets / (binary.name + ".oci.tar")
    if verify_archive(path, f"ghcr.io/fluxcd/{binary.name}@sha256:{image_digest}") != archive_digest:
        raise ValueError("Flux OCI archive changed")
    with tarfile.open(path, mode="r:") as oci, oci.extractfile("blobs/sha256/" + layer) as compressed:
        with tarfile.open(fileobj=compressed, mode="r|*") as files:
            for member in files:
                if member.name == "usr/local/bin/" + binary.name:
                    if not member.isfile() or member.size != executable_size:
                        raise ValueError("Unexpected Flux executable inventory")
                    with files.extractfile(member) as source, binary.open("xb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
    with binary.open("rb") as source:
        import hashlib
        if hashlib.file_digest(source, "sha256").hexdigest() != executable_digest:
            raise ValueError("Flux executable digest changed")
    binary.chmod(0o755)


def stop(process):
    if process.poll() is None:
        if os.getpgid(process.pid) != process.pid:
            raise ValueError("A native Flux fixture process escaped its original process group")
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)


def reserve(stack):
    sock = stack.enter_context(socket.socket())
    sock.bind(("127.0.0.1", 0))
    return sock, str(sock.getsockname()[1])


def exercise(test, git_server, clone_url, callback, *, deployment_branch=BRANCH, worker_context=None, artifact_context=None):
    if sys.platform != "linux" or os.geteuid() != 0 or Path("/proc/self/ns/net").stat().st_ino == Path("/proc/1/ns/net").stat().st_ino:
        raise ValueError("Use an isolated Linux network namespace for native Flux")
    if artifact_context is not None and (worker_context is None or set(artifact_context) != {"path", "prepare", "verify"}
            or not all(callable(artifact_context[key]) for key in ("prepare", "verify"))):
        raise ValueError("Select a worker-backed artifact Deployment")
    destination = DESTINATION if artifact_context is None else "synthetic-flux-application"
    manifest_path = BRANCH if artifact_context is None else artifact_context["path"]
    if worker_context is not None:
        from test_gerrit_review_runtime_fixture import namespace
        namespace(worker_context)
        verify_target(worker_context["client"], worker_context["target"])
    assets = Path(os.environ["FLUX_RECONCILIATION_TEST_ARTIFACTS"]).resolve(strict=True)
    install = read_bounded_bytes(assets / "install.yaml", max_bytes=4 * 1024 * 1024)
    if sha256(install).hexdigest() != FLUX_SHA256:
        raise ValueError("Flux release manifest changed")
    schemas = [obj for obj in yaml.safe_load_all(install) if isinstance(obj, dict) and obj.get("kind") == "CustomResourceDefinition"
               and obj["spec"]["group"] in {"source.toolkit.fluxcd.io", "kustomize.toolkit.fluxcd.io"}]
    test.assertTrue(schemas)
    report = {}
    roots = []

    def run(manager, root, kubectl):
        roots.append(root)

        def create(version, kind, name, namespace=None, **body):
            metadata = {"name": name}
            if namespace:
                metadata["namespace"] = namespace
            return kubectl(["create", "-f", "-", "-o", "json"], {
                "apiVersion": version, "kind": kind, "metadata": metadata, **body})

        def role_binding(name, namespace, role, account, *, cluster=False):
            create("rbac.authorization.k8s.io/v1", "ClusterRoleBinding" if cluster else "RoleBinding", name, namespace,
                   roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole" if cluster else "Role", "name": role},
                   subjects=[{"kind": "ServiceAccount", "name": account, "namespace": NAMESPACE}])

        for name in (NAMESPACE, destination):
            create("v1", "Namespace", name)
        for schema in schemas:
            await_crd_conditions(manager, kubectl(["create", "-f", "-", "-o", "json"], schema))
        create("v1", "ServiceAccount", "flux-workload", NAMESPACE)
        create("v1", "ServiceAccount", "flux-promoter-read", NAMESPACE)
        workload_rules = [
            {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "list", "watch", "create", "patch", "update"]}]
        if worker_context is not None:
            workload_rules += [
                {"apiGroups": [""], "resources": ["services"], "verbs": ["get", "list", "watch", "create", "patch", "update"]},
                {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "list", "watch", "create", "patch", "update"]},
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
                {"apiGroups": ["apps"], "resources": ["replicasets"], "verbs": ["get", "list", "watch"]}]
        create("rbac.authorization.k8s.io/v1", "Role", "scoped-apply", destination, rules=workload_rules)
        role_binding("scoped-apply", destination, "scoped-apply", "flux-workload")
        create("rbac.authorization.k8s.io/v1", "Role", "read-promotion", NAMESPACE, rules=[
            {"apiGroups": [group], "resources": [resource], "resourceNames": ["application"], "verbs": ["get"]}
            for group, resource in (("source.toolkit.fluxcd.io", "gitrepositories"), ("kustomize.toolkit.fluxcd.io", "kustomizations"))])
        role_binding("read-promotion", NAMESPACE, "read-promotion", "flux-promoter-read")
        create("rbac.authorization.k8s.io/v1", "ClusterRole", "promotion-target", rules=[
            {"apiGroups": [""], "resources": ["namespaces"], "resourceNames": ["kube-system"], "verbs": ["get"]}])
        role_binding("promotion-target", None, "promotion-target", "flux-promoter-read", cluster=True)
        deploy_identity = f"system:serviceaccount:{NAMESPACE}:flux-workload"
        for verb, scope in (("create", destination), ("get", NAMESPACE)):
            denial = run_bounded([*manager.prefix, "auth", "can-i", verb, "secrets", "-n", scope, "--as=" + deploy_identity],
                                 text=True, timeout=15, check=False)
            test.assertEqual((denial.returncode, denial.stdout.strip()), (1, "no"))
        create("v1", "Secret", "fixture-git-read", NAMESPACE, stringData={
            "username": "ci-read", "password": git_server["users"]["ci-read"],
            "ca.crt": read_bounded_bytes(git_server["tls"] / "ca.crt", max_bytes=256 * 1024).decode("ascii")})
        source = create("source.toolkit.fluxcd.io/v1", "GitRepository", "application", NAMESPACE, spec={
            "url": clone_url, "interval": "5s", "ref": {"branch": deployment_branch}, "secretRef": {"name": "fixture-git-read"}})
        app = create("kustomize.toolkit.fluxcd.io/v1", "Kustomization", "application", NAMESPACE, spec={
            "sourceRef": {"kind": "GitRepository", "name": "application"}, "path": "./" + manifest_path,
            "interval": "5s", "retryInterval": "5s", "timeout": "35s", "prune": False, "wait": True,
            "targetNamespace": destination, "serviceAccountName": "flux-workload"})
        config = json.loads((root / "kubeconfig.json").read_bytes())
        target = {"kubeconfig_env": "DISPOSABLE_FLUX_READ_CONFIG", "context": config["current-context"],
                  "cluster_uid": manager.get("namespace/kube-system")["metadata"]["uid"]}
        artifact_state = None
        if artifact_context is not None:
            from render_gerrit_promotion_access import artifact_workload_objects
            artifact_state = artifact_context["prepare"](manager, target)
            test.assertEqual(artifact_state["policy"]["namespace"], destination)
            for extra in artifact_workload_objects({"management_target": target},
                    {"account": "flux-promoter-read", "namespace": NAMESPACE}, artifact_state["policy"]):
                kubectl(["create", "-f", "-", "-o", "json"], extra)
        token = manager.call(["create", "token", "flux-promoter-read", "-n", NAMESPACE, "--duration=10m"]).strip()
        for user in config["users"]:
            user["user"] = {"token": token}
        read_config = root / "flux-read-config.json"
        read_config.write_text(json.dumps(config), encoding="utf-8")
        read_config.chmod(0o600)
        for name in CONTROLLERS:
            extract_binary(assets, root / "bin" / name)
        datastore = root / "flux-artifacts"
        datastore.mkdir(mode=0o700)
        os.chown(datastore, 65534, 65534)
        with ExitStack() as stack, patch.dict(os.environ, {target["kubeconfig_env"]: str(read_config)}):
            reader = Client(target)
            verify_target(reader, target)
            for verb, resource in (("patch", "kustomizations.kustomize.toolkit.fluxcd.io"),
                                   ("get", "secrets"), ("list", "gitrepositories.source.toolkit.fluxcd.io")):
                denied = run_bounded([*reader.prefix, "auth", "can-i", verb, resource, "-n", NAMESPACE],
                                     text=True, timeout=15, check=False)
                test.assertEqual((denied.returncode, denied.stdout.strip()), (1, "no"))
            denied = run_bounded([*reader.prefix, "auth", "can-i", "get", "gitrepositories.source.toolkit.fluxcd.io/application", "-n", "default"],
                                 text=True, timeout=15, check=False)
            test.assertEqual((denied.returncode, denied.stdout.strip()), (1, "no"))
            report.update(promoter_kubernetes_write_denied=True, promoter_cd_out_of_scope_reads_denied=True,
                          flux_workload_secret_access_denied=True)
            storage_socket, port = reserve(stack)
            address = "127.0.0.1:" + port
            storage_socket.close()
            processes = {}; identities = {}
            for name in CONTROLLERS:
                metrics, metrics_port = reserve(stack)
                probe, probe_port = reserve(stack)
                extra = ["--storage-path=" + str(datastore), "--storage-addr=" + address, "--storage-adv-addr=" + address] if name == "source-controller" else ["--no-remote-bases=true"]
                args = [str(root / "bin" / name), "--watch-all-namespaces=false", "--enable-leader-election=false",
                        "--metrics-addr=127.0.0.1:" + metrics_port, "--health-addr=127.0.0.1:" + probe_port, *extra]
                log = stack.enter_context((root / (name + ".log")).open("w+b"))
                env = {"KUBECONFIG": str(root / "kubeconfig.json"), "RUNTIME_NAMESPACE": NAMESPACE, "RUNTIME_NAME": name,
                       "PATH": "/usr/bin:/bin", "HOME": str(root), "LC_ALL": "C", "GOMAXPROCS": "2"}
                metrics.close()
                probe.close()
                process = subprocess.Popen([sys.executable, "-B", str(Path(native.__file__).resolve()), "--child", str(os.getpid()), *args],
                                           cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                stack.callback(stop, process)
                processes[name] = process
                identities[name] = (Path("/proc") / str(process.pid) / "stat").read_text().split(") ", 1)[1].split()[19]
            try:
                outcome = callback(reader, app, source, target)
                report.update(outcome)
                if artifact_context is None:
                    current = manager.get("configmap/synthetic-application", destination)
                    test.assertEqual(current["data"], {"result": "tested"})
                    test.assertTrue(current["metadata"]["uid"])
                else:
                    report.update(artifact_context["verify"](manager, artifact_state, outcome))
                test.assertTrue(all(process.poll() is None for process in processes.values()))
                samples = {name: native.process_sample(process, identities[name],
                    "repository" if name == "source-controller" else "controller", root / "bin" / name)
                    for name, process in processes.items()}
                for sample in samples.values():
                    test.assertTrue(sample["executable_matches"]); test.assertEqual(sample["uid"], [65534] * 4)
                    test.assertEqual(sample["no_new_privileges"], "1"); test.assertEqual(int(sample["effective_capabilities"], 16), 0)
                report["flux_native_process_after"] = samples
                if worker_context is not None and artifact_context is None:
                    from test_argocd_worker_workload import verify_workload
                    report.update(verify_workload(test, worker_context, controller="flux"))
                    inventory = manager.get("kustomization.kustomize.toolkit.fluxcd.io/application", NAMESPACE)["status"]["inventory"]["entries"]
                    ids = {item["id"] for item in inventory}
                    test.assertTrue({DESTINATION + "_synthetic-web_apps_Deployment", DESTINATION + "_synthetic-web__Service"} <= ids)
                    report.update(flux_worker_execution_verified=True, flux_workload_inventory_verified=True)
            except Exception as error:
                if isinstance(error, KubernetesOperationError):
                    print(json.dumps({"flux_kubernetes_failure": error.diagnostic}), flush=True)
                for name, process in processes.items():
                    raw = read_bounded_bytes(root / (name + ".log"), max_bytes=4 * 1024 * 1024).decode(errors="replace").casefold()
                    print(json.dumps({"flux_fixture": name, "exit": process.poll(), "signals": {
                        signal_: signature in raw for signal_, signature in {"unsupported_flag": "unknown flag",
                            "missing_config": "no configuration has been provided", "forbidden": "forbidden",
                            "untrusted": "x509:", "port": "address already in use", "source_ready": "stored artifact",
                            "path": "kustomization path not found", "failure": "reconciler error"}.items()}}), flush=True)
                raise
        test.assertTrue(all(process.poll() is not None for process in processes.values()))
        report["original_flux_processes_removed"] = True

    if worker_context is None:
        disposable_api(test, management_action=run, disk_workspace=True)
    else:
        with tempfile.TemporaryDirectory(prefix="platform-flux-worker-", dir="/var/tmp") as temporary:
            root = Path(temporary); root.chmod(0o700); os.chown(root, 65534, 65534)
            (root / "bin").mkdir(mode=0o700); os.chown(root / "bin", 65534, 65534)
            kubectl = Path(worker_context["client"].prefix[0]); native.artifact(kubectl, BINARIES["kubectl"])
            shutil.copyfile(kubectl, root / "bin/kubectl"); (root / "bin/kubectl").chmod(0o755)
            config = worker_context["client"].call(["config", "view", "--minify", "--flatten", "--raw", "-o", "json"])
            path = root / "kubeconfig.json"
            path.write_text(config); path.chmod(0o600); os.chown(path, 65534, 65534)
            def worker_kubectl(arguments, obj=None):
                return json.loads(worker_context["client"].call(arguments, payload=json.dumps(obj).encode() if obj is not None else None))
            run(worker_context["client"], root, worker_kubectl)
    test.assertTrue(roots and all(not root.exists() for root in roots))
    report.update(actual_flux_reconciliation_verified=True, real_configmap_reconciliation_verified=artifact_context is None,
                  temporary_native_flux_scope_removed=True, flux_physical_ha_verified=False, flux_status_modeled=False)
    return report
