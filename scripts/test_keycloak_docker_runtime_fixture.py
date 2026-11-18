#!/usr/bin/env python3
"""Offline optimized Keycloak image and actual Docker host-helper acceptance."""

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import tarfile
from uuid import uuid4

import yaml

import activate_keycloak_host as activation
from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
import render_keycloak_ha as renderer
from test_keycloak_ha import fixture
from test_keycloak_host_application_fixture import exercise as application_acceptance
from test_keycloak_runtime_fixture import failure_signals, loopback_listeners


MANIFEST = "4ee8200b0fd00de0e3743b48220f4feadbdb52deb29730ed804f9364bc2be23f"
CONFIG = "135fd1641cb6d9744b6333566f12a26c31c1452664fa897d1c39e760ea2ac611"
REGISTRY = "40df2224d410f72ae425c3371873b078bbdbda3b8b612be9571f0e6751f3acc8"


def image_archive(directory, output):
    manifest_raw = read_bounded_bytes(directory / "manifest.json", max_bytes=256 * 1024)
    config_raw = read_bounded_bytes(directory / "config.json", max_bytes=128 * 1024)
    if hashlib.sha256(manifest_raw).hexdigest() != MANIFEST or hashlib.sha256(config_raw).hexdigest() != CONFIG: raise ValueError("Use the reviewed immutable Keycloak image metadata")
    manifest, config = json.loads(manifest_raw), json.loads(config_raw)
    if config.get("os") != "linux" or config.get("architecture") != "amd64" or config.get("config", {}).get("Volumes"): raise ValueError("Unexpected base image policy")
    layers = manifest["layers"]; diff_ids = config["rootfs"]["diff_ids"]
    if len(layers) != 4 or len(diff_ids) != 4: raise ValueError("Unexpected base image layer inventory")
    total = 0; names = []
    with tarfile.open(output, "w") as archive:
        def add(name, raw):
            item = tarfile.TarInfo(name); item.mode = 0o600; item.size = len(raw); archive.addfile(item, io.BytesIO(raw))
        add(CONFIG + ".json", config_raw)
        for index, (layer, expected) in enumerate(zip(layers, diff_ids)):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", layer["digest"]) or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected) or not 0 < layer["size"] <= 256 * 1024 ** 2: raise ValueError("Invalid bounded base image layer")
            blob = directory / "blobs" / (layer["digest"][7:] + ".tar.gz")
            if blob.is_symlink() or blob.stat().st_size != layer["size"]: raise ValueError("Cached base image layer size differs")
            digest = hashlib.sha256(); count = 0
            with blob.open("rb") as stream:
                while chunk := stream.read(min(1024 * 1024, layer["size"] - count + 1)):
                    count += len(chunk)
                    if count > layer["size"]: raise ValueError("Cached image layer grew beyond its manifest size")
                    digest.update(chunk)
            if count != layer["size"] or digest.hexdigest() != layer["digest"][7:]: raise ValueError("Cached image layer fingerprint differs")
            target = output.parent / ("expanded-layer-" + str(index) + ".tar"); digest = hashlib.sha256(); count = 0
            try:
                with gzip.open(blob, "rb") as source, target.open("xb") as destination:
                    while chunk := source.read(min(1024 * 1024, 1024 ** 3 - count + 1)):
                        count += len(chunk); total += len(chunk)
                        if count > 1024 ** 3 or total > 2 * 1024 ** 3: raise ValueError("Expanded image layer budget exceeded")
                        destination.write(chunk); digest.update(chunk)
                if digest.hexdigest() != expected[7:]: raise ValueError("Expanded layer differs from the pinned image configuration")
                name = "layer-" + str(index) + ".tar"; names.append(name)
                archive.add(target, arcname=name, recursive=False)
            finally:
                if target.exists(): target.unlink()
        add("manifest.json", json.dumps([{"Config": CONFIG + ".json", "RepoTags": [], "Layers": names}]).encode())
    return "sha256:" + CONFIG


def exercise(test, context, engine, *, application_check=None):
    root = engine["root"]; backend = engine["backend"]; assets = engine["storage"] / "fixture-assets"; assets.mkdir(mode=0o700)
    builder = member = container_id = registry_process = None; plan = selected = None; reservations = []; stage = "base-image-loading"
    sensitive = [context["database_password"], context["login_password"]]
    report = {key: False for key in ("keycloak_host_docker_runtime_verified", "keycloak_host_docker_original_resume_verified", "keycloak_host_docker_kernel_listener_verified",
        "keycloak_host_docker_oidc_discovery_verified", "keycloak_host_docker_private_values_absent_from_logs", "keycloak_host_docker_container_cleanup_verified",
        "keycloak_host_docker_physical_node_ha_verified", "keycloak_host_docker_publisher_signature_verified")}
    fixture_id = uuid4().hex

    def docker(*arguments, seconds=45): return engine["run"]([backend.binary, "--host=unix://" + backend.socket, *arguments], seconds=seconds)
    def write(name, value, mode=0o644):
        path = root / name; path.parent.mkdir(mode=0o755, parents=True, exist_ok=True); path.write_text(value, encoding="utf-8", newline="\n"); path.chmod(mode); return path
    def owned(identity):
        row = json.loads(docker("container", "inspect", identity))[0]
        if row.get("Id") != identity or row.get("Config", {}).get("Labels", {}).get("platform.fixture") != fixture_id: raise ValueError("Original image-preparation container changed")
        return row
    def worker(mode):
        result = run_bounded(["/usr/bin/python3", "-B", str(root / "scripts/docker_fixture_worker.py"), str(root / "host-plan.json"), str(root / "bundle"), mode, str(root / "private/operation")],
            env=activation.CLEAN, text=True, timeout=240, output_max_bytes=256 * 1024, check=False)
        if result.returncode:
            try: diagnostic = json.loads(result.stdout)
            except ValueError: diagnostic = {"unclassified_worker_failure": True}
            print(json.dumps({"keycloak_docker_safe_failure": diagnostic}, sort_keys=True), flush=True)
            raise ValueError("Prepared Docker host activation failed; private details suppressed")
        return json.loads(result.stdout)
    def container_logs():
        if container_id: return docker("container", "logs", container_id)
        return ""
    def private_logs(values):
        sensitive.extend(values); context["private_logs"](values)
        text = container_logs()
        if "prvKey" in text or ("-----BEGIN " + "PRIVATE KEY-----") in text or any(value and value in text for value in sensitive): raise ValueError("A generated private value entered the container log")

    try:
        image_inputs = Path(os.environ["KEYCLOAK_DOCKER_TEST_IMAGE"]).resolve(strict=True)
        archive = assets / "base-image.tar"; base_id = image_archive(image_inputs, archive)
        docker("image", "load", "--input", str(archive), seconds=180); archive.unlink()
        test.assertEqual(json.loads(docker("image", "inspect", base_id))[0]["Id"], base_id)
        stage = "optimized-image-build"
        print("Disposable Keycloak docker fixture: building an optimized image offline", flush=True)
        builder = docker("container", "create", "--pull=never", "--label=platform.fixture=" + fixture_id, "--network=none", "--user=1000:1000", "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true", "--memory=2g", "--memory-swap=2g", "--cpus=2", "--pids-limit=512", "--entrypoint=/opt/keycloak/bin/kc.sh", base_id, *renderer.build_arguments()).strip()
        if not activation.container_id(builder): raise ValueError("Image preparation did not return its original container ID")
        owned(builder); docker("container", "start", builder)
        test.assertEqual(docker("container", "wait", builder, seconds=180).strip(), "0")
        owned(builder); prepared_id = docker("container", "commit", builder, seconds=180).strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", prepared_id): raise ValueError("Prepared image has no immutable configuration ID")
        docker("container", "rm", builder); builder = None
        stage = "registry-publish"
        registry_archive = Path(os.environ["KEYCLOAK_DOCKER_TEST_REGISTRY"]).resolve(strict=True) / "registry_3.1.2_linux_amd64.tar.gz"
        if registry_archive.is_symlink() or registry_archive.stat().st_size > 32 * 1024 ** 2: raise ValueError("Use the reviewed bounded registry archive")
        with registry_archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != REGISTRY: raise ValueError("Registry archive fingerprint differs")
        with tarfile.open(registry_archive) as archive:
            item = archive.getmember("registry")
            if not item.isfile() or not 0 < item.size <= 64 * 1024 ** 2: raise ValueError("Unexpected registry binary")
            stream = archive.extractfile(item); binary = context["root"] / "registry"; binary.write_bytes(stream.read(64 * 1024 ** 2 + 1)); stream.close(); binary.chmod(0o755); os.chown(binary, 65534, 65534)
        reserve = socket.socket(); reserve.bind(("127.0.0.1", 0)); registry_port = reserve.getsockname()[1]
        import secrets
        secret = secrets.token_urlsafe(48); sensitive.append(secret)
        registry_config = context["write"]("private/registry.yaml", yaml.safe_dump({"version": 0.1, "log": {"level": "error"}, "storage": {"filesystem": {"rootdirectory": str(context["root"] / "registry-data")}},
            "http": {"addr": "127.0.0.1:" + str(registry_port), "secret": secret}}))
        reserve.close(); registry_process = context["start"]("registry", [binary, "serve", registry_config], context["environment"]())
        from urllib.request import build_opener, ProxyHandler
        public_http = build_opener(ProxyHandler({}))
        def registry_ready():
            loopback_listeners([pair for pair in context["processes"] if pair[0] is registry_process], {registry_port})
            with public_http.open("http://127.0.0.1:" + str(registry_port) + "/v2/", timeout=2) as response: return response.status == 200
        context["wait"](registry_ready)
        repository = "127.0.0.1:" + str(registry_port) + "/fixture/keycloak"
        docker("image", "tag", prepared_id, repository + ":prepared"); docker("image", "push", repository + ":prepared", seconds=180)
        row = json.loads(docker("image", "inspect", prepared_id))[0]
        refs = [value for value in row.get("RepoDigests", []) if value.startswith(repository + "@sha256:")]
        if len(refs) != 1 or not re.fullmatch(r"sha256:[0-9a-f]{64}", refs[0].split("@", 1)[1]): raise ValueError("Prepared image has no unique immutable registry reference")
        test.assertEqual(json.loads(docker("image", "inspect", refs[0]))[0]["Id"], prepared_id)
        entries = json.loads(engine["run"](["/usr/sbin/ip", "-j", "-4", "address", "show"]))
        addresses = sorted({a["local"] for row in entries for a in row.get("addr_info", []) if a.get("scope") == "global" and not a["local"].startswith("127.")})
        if not addresses: raise ValueError("The container fixture needs a local reviewed peer IPv4 address")
        address = addresses[0]; member = "kcdocker-fixture-" + uuid4().hex[:12]
        for host, port in (("127.0.0.1", 8443), ("127.0.0.1", 9000), (address, 7800), (address, 57800)):
            handle = socket.socket(); handle.bind((host, port)); reservations.append(handle)
        selected = fixture(); selected.update(hostname="localhost", public_port=8443, realm="platform-docker-fixture", private_dir=str(root / "inputs"), image=refs[0])
        selected["database"].update(host="127.0.0.1", port=context["database_port"], name="keycloak_fixture", username="keycloak_fixture")
        selected["members"][0].update(name=member, address=address)
        for source, destination in ((context["root"] / "tls/tls.crt", "inputs/tls/tls.crt"), (context["root"] / "tls/tls.key", "inputs/tls/tls.key"),
            (context["root"] / "ca.crt", "inputs/db-ca/ca.crt"), (context["root"] / "ca.crt", "inputs/health-ca.crt")):
            write(destination, source.read_text())
        key = root / "inputs/tls/tls.key"; key.chmod(0o640); os.chown(key, 0, 1000)
        write("inputs/database.env", "KC_DB_PASSWORD=" + context["database_password"] + "\n", 0o600)
        for path in Path(__file__).parent.glob("*.py"):
            target = root / "scripts" / path.name; target.parent.mkdir(exist_ok=True); shutil.copyfile(path, target); target.chmod(0o644)
        write("scripts/docker_fixture_worker.py", '''import json,sys,traceback
from pathlib import Path
import activate_keycloak_host as activation
try:
 plan=json.loads(Path(sys.argv[1]).read_text())
 result=activation.operate(Path(sys.argv[2]),plan,mode=sys.argv[3],output=Path(sys.argv[4]),wait_seconds=120)
 print(json.dumps(result))
except Exception as error:
 labels={"Original Docker command or identity changed":"command-or-identity","Original Docker isolation changed":"isolation","Original Docker private mounts changed":"mounts"}
 frames=[{"file":Path(f.filename).name,"line":f.lineno,"function":f.name} for f in traceback.extract_tb(error.__traceback__)]
 print(json.dumps({"exception_type":type(error).__name__,"locations":frames[-8:],"classification":labels.get(str(error),"unclassified")}));raise SystemExit(1)
''')
        files = renderer.render(selected, "docker")
        for name, value in files.items(): write("bundle/" + name, value)
        contract = json.loads(files["keycloak-contract.json"])
        plan = {"schema_version": 1, "contract_sha256": hashlib.sha256((root / "bundle/keycloak-contract.json").read_bytes()).hexdigest(), "member": member,
            "machine_id_sha256": hashlib.sha256(Path("/etc/machine-id").read_text().strip().encode()).hexdigest(), "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "service_uid": 1000, "service_gid": 1000, "bind_addresses": {"https": "127.0.0.1", "management": "127.0.0.1"},
            "files_sha256": {name: hashlib.sha256((root / "inputs" / name).read_bytes()).hexdigest() for name in activation.FILES}, "native": None,
            "docker": {"binary": backend.binary, "socket": backend.socket, "binary_sha256": activation.file_hash(Path(backend.binary)), "daemon_id": engine["engine_id"], "server_version": "29.9.3", "image_id": prepared_id}}
        write("host-plan.json", json.dumps(plan), 0o600)
        stage = "host-validation"; test.assertTrue(worker("check-host")["host_checked"])
        for handle in reservations: handle.close()
        reservations.clear(); stage = "host-activation"
        print("Disposable Keycloak docker fixture: activating the prepared immutable image", flush=True)
        result = worker("activate"); test.assertTrue(result["original_member_active"]); test.assertTrue(result["tls_readiness_observed"])
        report["keycloak_host_docker_runtime_verified"] = True
        journal_path = root / "private/operation/keycloak-host-proof.json"; journal = json.loads(journal_path.read_text()); container_id = journal["handle"]
        test.assertEqual(journal["toolset_sha256"], activation.tools())
        original = backend.inspect(contract, plan, journal, {"image": {k: row[k] for k in ("Id", "Config")}})
        test.assertTrue(activation.owns_listener(original, "127.0.0.1", 8443)); test.assertTrue(activation.owns_listener(original, "127.0.0.1", 9000)); report["keycloak_host_docker_kernel_listener_verified"] = True
        test.assertTrue(worker("resume")["read_only"]); after = json.loads(journal_path.read_text()); test.assertEqual(after["invocation"], journal["invocation"])
        report["keycloak_host_docker_original_resume_verified"] = True
        connection = activation.DirectHTTPS("127.0.0.1", "localhost", 8443, ssl.create_default_context(cafile=str(root / "inputs/health-ca.crt")))
        try:
            connection.request("GET", "/realms/master/.well-known/openid-configuration"); response = connection.getresponse(); raw = response.read(128 * 1024 + 1)
            test.assertEqual(response.status, 200); test.assertLessEqual(len(raw), 128 * 1024); test.assertEqual(json.loads(raw)["issuer"], "https://localhost:8443/realms/master")
        finally: connection.close()
        report["keycloak_host_docker_oidc_discovery_verified"] = True; stage = "application-authentication"
        report.update(application_acceptance(test, {**context, "private_logs": private_logs}, bundle=root / "bundle", selected=selected,
            contract_sha256=plan["contract_sha256"], application_check=application_check, name="docker"))
        final = backend.inspect(contract, plan, after, {"image": {k: row[k] for k in ("Id", "Config")}})
        test.assertEqual(final["invocation"], journal["invocation"]); test.assertTrue(backend.healthy(contract, plan, final))
        report["keycloak_host_artifacts_sha256"] = {"base_manifest": MANIFEST, "base_config": CONFIG, "registry_archive": REGISTRY, "prepared_image": prepared_id[7:],
            "prepared_manifest": refs[0].split("@sha256:")[1], "activation_plan": activation.fingerprint(plan), "activation_journal": hashlib.sha256(journal_path.read_bytes()).hexdigest(), "host_toolset": journal["toolset_sha256"]}
    except BaseException as error:
        text = container_logs() if container_id else docker("container", "logs", builder) if builder else ""
        print(json.dumps({"keycloak_docker_safe_failure": {"stage": stage, "exception_type": type(error).__name__, "signals": failure_signals(text)}}), flush=True)
        raise
    finally:
        for handle in reservations: handle.close()
        if member and plan:
            identities = docker("container", "ls", "--all", "--no-trunc", "--filter=name=^/platform-" + member + "$", "--format={{.ID}}").split()
            if len(identities) > 1: raise ValueError("Ambiguous original activation fixture")
            if identities:
                current = json.loads(docker("container", "inspect", identities[0]))[0]
                receipt = json.loads((root / "private/operation/keycloak-host-proof.json").read_text())
                if current["Config"].get("Labels") != {**(row["Config"].get("Labels") or {}), **activation.labels(receipt)} or container_id and current["Id"] != container_id: raise ValueError("Original activation ownership changed; preserve handles")
                container_id = current["Id"]; private_logs(sensitive[:]); report["keycloak_host_docker_private_values_absent_from_logs"] = True
                docker("container", "rm", "--force", container_id); container_id = None
        if builder: owned(builder); docker("container", "rm", "--force", builder)
        if registry_process: context["stop"](registry_process)
    report["keycloak_host_docker_container_cleanup_verified"] = True
    return report
