#!/usr/bin/env python3
"""Actual private Harbor image signatures and TLS Rekor inclusion; no admission claim."""

from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import ssl
import subprocess
from urllib.parse import urlencode
from urllib.request import build_opener, HTTPSHandler, ProxyHandler, Request

from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
import provision_harbor_robot as robot
from strict_json import loads_strict_json
from test_harbor_jwt_runtime_fixture import CoreRegistry, JWTChecks
from test_harbor_registry_runtime_fixture import digest, RegistryChecks
from test_harbor_robot_fixture import RobotChecks
from test_harbor_pool_health_fixture import PoolHealthChecks
from test_harbor_jobservice_replica_fixture import JobReplicaChecks
from test_harbor_core_replica_fixture import CoreReplicaChecks
from test_harbor_database_promotion_fixture import DatabasePromotionChecks
from test_harbor_service_runtime_fixture import ServiceChecks
import verify_image_signature as verifier

REKOR_SHA = "b050940f23bacca24605dad050e1e73ded1c2079dd5761546c0c597a304dddc6"
ORIGIN = "https://192.0.2.100:3000"
CHECKPOINT_ORIGIN = "192.0.2.100"


def pin(path, expected):
    path = Path(path)
    if path.is_symlink() or not path.is_file(): raise ValueError("Require a regular reviewed executable")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected: raise ValueError("Reviewed signature artifact changed")
    return path


def roles():
    active = set()
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            active.update(int(line.split()[1]) for line in (proc / "status").read_text().splitlines() if line.startswith("Uid:"))
        except (FileNotFoundError, ProcessLookupError):
            continue
    start = 200000 + secrets.randbelow(50000)
    result = []
    for candidate in range(start, start + 50):
        try: pwd.getpwuid(candidate)
        except KeyError:
            if candidate not in active: result.append(candidate)
        if len(result) == 3: return result
    raise ValueError("No separate disposable numeric identities available")


class SignatureChecks(RobotChecks):
    application_timeout_seconds = 1800

    def jwt_workload_completed(self, context, phase):
        super().jwt_workload_completed(context, phase)
        if phase == "initial" and not hasattr(self, "signature_plan"):
            self.signature_setup(context)
        proof = self.signature_verify(context)
        context["test"].assertTrue(proof["image_signature_verified"])
        context["test"].assertTrue(proof["signed_annotations_verified"])
        context["test"].assertEqual(proof["signed_annotations_sha256"], hashlib.sha256(verifier.canonical(self.signature_plan["annotations"])).hexdigest())
        self.signature_last_proof = proof
        self.signature_phases.add(phase)

    def signature_setup(self, context, *, image=None, annotations=None, directory_name="image-signatures"):
        test = context["test"]; root = context["root"]
        cosign = pin(os.environ["COSIGN_TEST_BINARY"], verifier.COSIGN_SHA256)
        rekor = pin(os.environ["REKOR_POSIX_TEST_BINARY"], REKOR_SHA)
        signer_uid, log_uid, reader_uid = roles()
        self.signature_uids = {"signer": signer_uid, "log": log_uid, "reader": reader_uid}
        base = root / directory_name; base.mkdir(mode=0o711); base.chmod(0o711)
        directories = {}
        for name, uid in self.signature_uids.items():
            path = base / name; path.mkdir(mode=0o700); os.chown(path, uid, uid)
            directories[name] = path
        signer, log, reader = (directories[name] for name in ("signer", "log", "reader"))
        def write(path, value, uid, mode=0o400):
            path.write_bytes(value if isinstance(value, bytes) else value.encode()); path.chmod(mode); os.chown(path, uid, uid)
            return path
        for directory, uid in ((signer, signer_uid), (log, log_uid), (reader, reader_uid)):
            write(directory / "ca.crt", (root / "ca.crt").read_bytes(), uid)
        self.signature_private_values = []
        self.signature_logs = []
        def command(uid, directory, arguments, *, password=None, expected=0):
            env = {"PATH": "/usr/bin:/bin", "HOME": str(directory), "TMPDIR": str(directory), "DOCKER_CONFIG": str(directory),
                "SSL_CERT_FILE": str(directory / "ca.crt"), "LANG": "C", "LC_ALL": "C"}
            if password is not None: env["COSIGN_PASSWORD"] = password
            argv = ["/usr/bin/setpriv", "--reuid=" + str(uid), "--regid=" + str(uid), "--clear-groups", "--no-new-privs", "--", *map(str, arguments)]
            value = run_bounded(argv, env=env, cwd=directory, text=True, timeout=120, output_max_bytes=1024*1024, check=False)
            self.signature_logs.append((value.stdout + value.stderr).encode())
            if value.returncode != expected:
                target = Path(os.environ["SIGSTORE_TEST_REPORT"]).resolve().parent / "sigstore-last-cli-error.log"
                target.relative_to((Path(__file__).resolve().parents[1] / "private").resolve())
                target.write_text(value.stdout + value.stderr); target.chmod(0o600)
                print(json.dumps({"signature_cli_failed": True, "return_code": value.returncode,
                    "stage": self.signature_stage}), flush=True)
                raise ValueError("Actual signature CLI stage failed")
            return value
        self.signature_command = command
        context["run"](["/usr/sbin/ip", "address", "replace", "192.0.2.100/32", "dev", "lo"])
        extensions = base / "rekor-extensions.cnf"
        extensions.write_text("subjectAltName=IP:192.0.2.100\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n")
        context["run"](["/usr/bin/openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
            "-subj", "/CN=rekor-fixture", "-keyout", log / "tls.key", "-out", log / "tls.csr"])
        context["run"](["/usr/bin/openssl", "x509", "-req", "-in", log / "tls.csr", "-CA", root / "ca.crt", "-CAkey", root / "ca.key",
            "-CAcreateserial", "-days", "1", "-extfile", extensions, "-out", log / "tls.crt"])
        context["run"](["/usr/bin/openssl", "genpkey", "-algorithm", "ED25519", "-out", log / "checkpoint.key"])
        context["run"](["/usr/bin/openssl", "pkey", "-in", log / "checkpoint.key", "-pubout", "-out", log / "checkpoint.pub"])
        for path in log.iterdir():
            path.chmod(0o400); os.chown(path, log_uid, log_uid)
        storage = log / "storage"; storage.mkdir(mode=0o700); os.chown(storage, log_uid, log_uid)
        write(signer / "checkpoint.pub", (log / "checkpoint.pub").read_bytes(), signer_uid)
        start = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        log_args = [str(rekor), "serve", "--hostname=" + CHECKPOINT_ORIGIN, "--storage-dir=" + str(storage),
            "--signer-filepath=" + str(log / "checkpoint.key"), "--http-address=192.0.2.100", "--http-port=3000",
            "--grpc-address=127.0.0.1", "--grpc-port=3001", "--http-metrics-port=2115",
            "--http-tls-cert-file=" + str(log / "tls.crt"), "--http-tls-key-file=" + str(log / "tls.key"),
            "--checkpoint-interval=1s", "--log-level=error"]
        log_file = base / "rekor-private.log"
        with log_file.open("xb") as output:
            child = subprocess.Popen(["/usr/bin/setpriv", "--reuid=" + str(log_uid), "--regid=" + str(log_uid), "--clear-groups", "--no-new-privs", "--", *log_args],
                cwd=log, env={"PATH": "/usr/bin:/bin", "HOME": str(log), "LANG": "C"}, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            trust = ssl.create_default_context(cafile=str(root / "ca.crt"))
            opener = build_opener(ProxyHandler({}), HTTPSHandler(context=trust))
            def ready():
                if child.poll() is not None: raise AssertionError("Original Rekor process exited")
                try:
                    with opener.open(Request(ORIGIN + "/healthz"), timeout=3) as response: return response.status == 200
                except OSError: return False
            context["wait"](ready, seconds=30)
            proc = Path("/proc") / str(child.pid)
            status = dict(line.split(":", 1) for line in (proc / "status").read_text().splitlines() if ":" in line)
            executable = (proc / "exe").stat(); pinned = rekor.stat()
            test.assertEqual((executable.st_dev, executable.st_ino), (pinned.st_dev, pinned.st_ino))
            test.assertEqual([int(v) for v in status["Uid"].split()], [log_uid] * 4)
            test.assertEqual(status["NoNewPrivs"].strip(), "1"); test.assertEqual(int(status["CapEff"].strip(), 16), 0)
            original = {"pid": child.pid, "start_ticks": int((proc / "stat").read_text().rsplit(") ", 1)[1].split()[19]),
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(), "uid": log_uid, "binary_sha256": REKOR_SHA,
                "no_new_privileges": status["NoNewPrivs"].strip(), "effective_capabilities": status["CapEff"].strip(),
                "binary_identity_matches": True, "cgroup": (proc / "cgroup").read_text().strip()}
            self.signature_stage = "trusted-root"
            command(signer_uid, signer, [cosign, "trusted-root", "create", "--rekor=url=" + ORIGIN + ",public-key=" + str(signer / "checkpoint.pub") + ",origin=" + CHECKPOINT_ORIGIN + ",start-time=" + start, "--out", signer / "trusted-root.json"])
            command(signer_uid, signer, [cosign, "signing-config", "create", "--rekor=url=" + ORIGIN + ",api-version=2,start-time=" + start + ",operator=fixture", "--rekor-config=ANY", "--out", signer / "signing-config.json"])
            password = secrets.token_urlsafe(36); self.signature_private_values.append(password.encode())
            command(signer_uid, signer, [cosign, "generate-key-pair", "--output-key-prefix", signer / "release"], password=password)
            anonymous = CoreRegistry(self.service_clients["harbor-core"])
            def token(username, credential, actions):
                query = urlencode({"service": "harbor-registry", "scope": "repository:fixture/ha:" + actions})
                code, _, raw = anonymous._request("GET", "/service/token?" + query, authorization=robot.basic(username, credential))
                test.assertEqual(code, 200); value = loads_strict_json(raw)["token"]
                self.signature_private_values.append(value.encode()); return value
            writer = token(self.robot_credential["username"], self.robot_credential["secret"], "pull,push")
            reader_token = token("fixture-reader", self.reader_password, "pull")
            write(signer / "config.json", json.dumps({"auths": {"192.0.2.91:8443": {"registrytoken": writer}}}), signer_uid)
            image = image or "192.0.2.91:8443/fixture/ha@" + digest(self.robot_witness[2][2])
            for name, path in (("public_key", signer / "release.pub"), ("trusted_root", signer / "trusted-root.json")):
                write(reader / name, path.read_bytes(), reader_uid)
            write(reader / "credential", json.dumps({"registry": "192.0.2.91:8443", "repository": "fixture/ha", "token": reader_token}), reader_uid)
            annotations = annotations or {"platform.gitops/fixture-image-sha256": hashlib.sha256(image.encode()).hexdigest(),
                "platform.gitops/fixture-policy-sha256": hashlib.sha256(b"explicit-native-signature-protocol-v2").hexdigest()}
            plan = {"schema_version": 2, "image": image, "annotations": annotations}
            for name, path in (("cosign_binary", cosign), ("registry_ca", reader / "ca.crt"), ("public_key", reader / "public_key"),
                    ("trusted_root", reader / "trusted_root"), ("credential", reader / "credential")):
                with path.open("rb") as incoming: sha = hashlib.file_digest(incoming, "sha256").hexdigest()
                plan[name] = {"path": str(path), "sha256": sha}
            self.signature_reader = reader; self.signature_plan = plan; self.signature_phases = set()
            self.signature_stage = "unsigned-refusal"
            self.signature_verify(context, expected=1)
            self.signature_stage = "sign-image"
            command(signer_uid, signer, [cosign, "sign", "--yes", "--key", signer / "release.key", "--signing-config", signer / "signing-config.json",
                "--trusted-root", signer / "trusted-root.json", "--registry-cacert", signer / "ca.crt", "--bundle", signer / "image.bundle.json",
                *[part for key, value in sorted(annotations.items()) for part in ("--annotations", key + "=" + value)], image], password=password)
            self.signature_stage = "verify-image"
            self.signature_verify(context)
            bundle = loads_strict_json(read_bounded_bytes(signer / "image.bundle.json", max_bytes=1024*1024))
            entries = bundle["verificationMaterial"]["tlogEntries"]
            test.assertEqual(len(entries), 1); test.assertIn("checkpoint", entries[0]["inclusionProof"])
            self.signature_bundle = bundle
            self.signature_public_inputs = {name: (reader / name).read_text() for name in ("public_key", "trusted_root")}
            self.signature_public_inputs["registry_ca"] = (reader / "ca.crt").read_text()
            status, _, manifest = CoreRegistry(self.service_clients["harbor-core"], "Bearer " + reader_token).request(
                "GET", "/v2/fixture/ha/manifests/" + image.rsplit("@", 1)[1])
            test.assertEqual(status, 200); test.assertEqual(digest(manifest), image.rsplit("@", 1)[1])
            self.signature_public_inputs["manifest"] = manifest.decode()
            self.signature_bundle_raw = (signer / "image.bundle.json").read_text()
            test.assertEqual((signer / "release.key").stat().st_uid, signer_uid)
            test.assertEqual((signer / "release.key").stat().st_mode & 0o777, 0o600)
            self.signature_stage = "wrong-signed-binding-refusal"
            changed = {**plan, "annotations": {**annotations, sorted(annotations)[0]: "0" * 64}}
            self.signature_verify(context, plan=changed, expected=1)
            self.signature_stage = "missing-signed-binding-refusal"
            changed = {**plan, "annotations": {**annotations, "platform.gitops/absent-required-binding": "required"}}
            self.signature_verify(context, plan=changed, expected=1)
            self.signature_binding_negatives = True
            self.signature_stage = "wrong-key-refusal"
            wrong = signer / "wrong"
            command(signer_uid, signer, [cosign, "generate-key-pair", "--output-key-prefix", wrong], password=password)
            write(reader / "wrong.pub", (signer / "wrong.pub").read_bytes(), reader_uid)
            changed = {**plan, "public_key": {"path": str(reader / "wrong.pub"), "sha256": hashlib.sha256((reader / "wrong.pub").read_bytes()).hexdigest()}}
            self.signature_verify(context, plan=changed, expected=1)
            self.signature_stage = "wrong-log-refusal"
            context["run"](["/usr/bin/openssl", "genpkey", "-algorithm", "ED25519", "-out", log / "wrong-log.key"])
            context["run"](["/usr/bin/openssl", "pkey", "-in", log / "wrong-log.key", "-pubout", "-out", log / "wrong-log.pub"])
            (log / "wrong-log.key").chmod(0o400); os.chown(log / "wrong-log.key", log_uid, log_uid)
            write(signer / "wrong-log.pub", (log / "wrong-log.pub").read_bytes(), signer_uid)
            command(signer_uid, signer, [cosign, "trusted-root", "create",
                "--rekor=url=" + ORIGIN + ",public-key=" + str(signer / "wrong-log.pub") + ",origin=" + CHECKPOINT_ORIGIN + ",start-time=" + start,
                "--out", signer / "wrong-trusted-root.json"])
            write(reader / "wrong-trusted-root.json", (signer / "wrong-trusted-root.json").read_bytes(), reader_uid)
            changed = {**plan, "trusted_root": {"path": str(reader / "wrong-trusted-root.json"),
                "sha256": hashlib.sha256((reader / "wrong-trusted-root.json").read_bytes()).hexdigest()}}
            self.signature_verify(context, plan=changed, expected=1)
            self.signature_stage = "role-isolation"
            script = "import os,sys\ntry: os.open(sys.argv[1],os.O_RDONLY)\nexcept PermissionError: sys.exit(0)\nraise SystemExit(1)"
            command(reader_uid, reader, ["/usr/bin/python3", "-I", "-B", "-c", script, signer / "release.key"])
            command(reader_uid, reader, ["/usr/bin/python3", "-I", "-B", "-c", script, log / "checkpoint.key"])
            command(signer_uid, signer, ["/usr/bin/python3", "-I", "-B", "-c", script, log / "checkpoint.key"])
            self.signature_original = original
            self.signature_stage = "signed-and-isolated"
        finally:
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=10)
                except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=10)
            test.assertIsNotNone(child.returncode)
            self.signature_logs.append(log_file.read_bytes())
            for value in self.signature_private_values:
                test.assertFalse(any(value in raw for raw in self.signature_logs))
        self.signature_original["original_process_removed"] = not Path("/proc", str(child.pid)).exists()

    def signature_verify(self, context, *, plan=None, expected=0):
        reader = self.signature_reader; uid = self.signature_uids["reader"]
        selected = self.signature_plan if plan is None else plan
        path = reader / ("plan-" + secrets.token_hex(4) + ".json")
        path.write_bytes(verifier.canonical(selected)); path.chmod(0o400); os.chown(path, uid, uid)
        code = ("import json,sys\nsys.path.insert(0,sys.argv[1])\nfrom strict_json import loads_strict_json\n"
            "from verify_image_signature import operate\nfrom pathlib import Path\n"
            "try: result=operate(loads_strict_json(Path(sys.argv[2]).read_bytes()))\n"
            "except Exception: print('signature-refused'); raise SystemExit(1)\n"
            "print(json.dumps(result))\n")
        value = self.signature_command(uid, reader, ["/usr/bin/python3", "-I", "-B", "-c", code,
            str(Path(__file__).resolve().parent), path], expected=expected)
        return loads_strict_json(value.stdout) if expected == 0 else None

    def exercise(self, context):
        result = super().exercise(context)
        test = context["test"]
        test.assertEqual(self.signature_phases, self.expected_jwt_phases)
        test.assertTrue(self.signature_original["original_process_removed"])
        test.assertTrue(self.signature_binding_negatives)
        proof = {"original_rekor": self.signature_original, "uids": self.signature_uids, "phases": sorted(self.signature_phases),
            "plan": self.signature_plan, "bundle": self.signature_bundle, "bundle_raw": self.signature_bundle_raw,
            "verification_inputs": self.signature_public_inputs, "verified": self.signature_last_proof,
            "admission_verified": False, "ci_signature_handoff_verified": False, "ha_verified": False, "full_stack_verified": False}
        target = Path(os.environ["SIGSTORE_TEST_REPORT"])
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream: stream.write(verifier.canonical(proof) + b"\n")
        result.update(valkey_gateway_harbor_image_signature_verified=True, valkey_gateway_harbor_transparency_inclusion_verified=True,
            valkey_gateway_harbor_signature_roles_isolated_verified=True, valkey_gateway_harbor_unsigned_and_wrong_key_refused=True,
            valkey_gateway_harbor_original_signature_after_promotions_verified=True, valkey_gateway_harbor_wrong_transparency_log_refused=True,
            valkey_gateway_harbor_offline_log_inclusion_verified=True,
            valkey_gateway_harbor_signed_annotations_verified=True,
            valkey_gateway_harbor_wrong_or_missing_signed_binding_refused=True,
            valkey_gateway_harbor_signature_proof_sha256={"proof": hashlib.sha256(target.read_bytes()).hexdigest()},
            valkey_gateway_harbor_signed_image_admission_verified=False, valkey_gateway_harbor_ci_signature_handoff_verified=False)
        return result


def exercise(test):
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    class Application(SignatureChecks, PoolHealthChecks, JobReplicaChecks, CoreReplicaChecks, JWTChecks,
            DatabasePromotionChecks, ServiceChecks, RegistryChecks, gateway.Application): pass
    return cache.exercise(test, application=Application())
