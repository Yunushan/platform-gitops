"""Opt-in publisher CLI against real native Harbor; Zuul job execution is separate."""

import hashlib
import io
import json
import tarfile
from uuid import uuid4

from bounded_subprocess import run_bounded
import publish_harbor_static_image as publisher
import render_zuul_static_image as renderer
from test_harbor_core_replica_fixture import CoreReplicaChecks
from test_harbor_database_promotion_fixture import DatabasePromotionChecks
from test_harbor_jobservice_replica_fixture import JobReplicaChecks
from test_harbor_jwt_runtime_fixture import JWTChecks
from test_harbor_pool_health_fixture import PoolHealthChecks
from test_harbor_registry_runtime_fixture import RegistryChecks
from test_harbor_robot_fixture import RobotChecks
from test_harbor_service_runtime_fixture import ServiceChecks
from test_harbor_worker_fixture import base_image
from test_k3s_vm_fixture import PROBE_MANIFEST
from test_zuul_static_image import fixture
from test_zuul_static_image_runtime_fixture import exercise as git_exercise
import zuul_static_image as worker


class StaticPublicationChecks(RobotChecks):
    def jwt_identities_ready(self, context):
        super().jwt_identities_ready(context)
        ca = (context["root"] / "ca.crt").read_bytes()
        self.static_registry = publisher.Registry({"origin": "https://192.0.2.91:8443", "repository": "fixture/ha",
            "leaf_sha256": self.service_clients["harbor-core"].leaf}, ca, self.robot_credential)
        self.static_registry.authenticate()
        selected = base_image()
        base = selected.pop("blobs/sha256/" + PROBE_MANIFEST)
        for raw in selected.values(): self.static_registry.upload(raw)
        status, headers, _ = self.static_registry.request("PUT", self.static_registry.prefix + "/manifests/sha256:" + PROBE_MANIFEST, base, publisher.MEDIA)
        context["test"].assertEqual(status, 201)
        context["test"].assertEqual(headers["docker-content-digest"], "sha256:" + PROBE_MANIFEST)
        self.static_report = None

    def publish(self, context, source):
        test = context["test"]
        package = context["root"] / ("zuul-static-package-" + uuid4().hex)
        package.mkdir(mode=0o755)
        plan, ci, event, _ = fixture(package)
        from urllib.parse import urlsplit
        origin = urlsplit(source["source"]["clone_url"])
        ci["gerrit"].update(host=origin.hostname, https_port=origin.port)
        plan["source"] = {key: value for key, value in source["source"].items() if key not in {"schema_version", "branch_ref", "revision"}}
        event["revision"] = source["source"]["revision"]
        ca = (context["root"] / "ca.crt").read_bytes()
        credential = (self.robot_directory / "credential.json").read_bytes()
        plan["registry"].update(origin="https://192.0.2.91:8443", ca_sha256=hashlib.sha256(ca).hexdigest(),
            leaf_sha256=self.service_clients["harbor-core"].leaf, credential_sha256=hashlib.sha256(credential).hexdigest(),
            project_id=self.robot_plan["project"]["id"], robot_id=self.robot_id, base_digest="sha256:" + PROBE_MANIFEST)
        files = renderer.render(plan, ci, "raw")
        for name, contents in files.items():
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8", newline="\n")
            path.chmod(0o444)
        for path in package.rglob("*"):
            if path.is_dir(): path.chmod(0o555)
        contract_path = package / renderer.PACKAGE / "contract.json"
        contract = json.loads(files[renderer.PACKAGE + "/contract.json"])
        payload = {"source_ca": source["source_ca"].decode(), "source_token": source["source_token"],
            "registry_ca": ca.decode(), "registry_credential": credential.decode()}
        context["private_values"].append(source["source_token"])
        arguments = ["/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "--no-new-privs", "/usr/bin/python3", "-E", "-s", "-B",
            str(contract_path.parent / "programs/zuul_static_image.py"), "--contract", str(contract_path),
            "--contract-sha256", worker.fingerprint(contract), "--event-json", json.dumps(event), "--input-stdin"]
        result = run_bounded(arguments, input=json.dumps(payload), text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8", "TMPDIR": "/var/tmp"},
            timeout=240, output_max_bytes=256 * 1024, check=False)
        if result.returncode: raise ValueError("The scoped native publication CLI failed; private diagnostics suppressed")
        value = json.loads(result.stdout)
        receipt = worker.verify_artifact(contract, event, [value["artifact"]])
        manifest = json.loads(self.static_registry.fetch("manifests", receipt["manifest_digest"]))
        test.assertEqual(manifest["config"]["digest"], receipt["config_digest"])
        test.assertEqual(manifest["layers"][-1]["digest"], receipt["output_layer_digest"])
        layer = self.static_registry.fetch("blobs", receipt["output_layer_digest"])
        with tarfile.open(fileobj=io.BytesIO(layer), mode="r:") as archive:
            test.assertEqual(archive.extractfile("www/index.html").read(), b"exact-source-v1\n")
        for secret in (source["source_token"], self.robot_credential["secret"]):
            test.assertNotIn(secret, result.stdout)
            test.assertNotIn(secret, result.stderr)
            test.assertNotIn(secret.encode(), layer)
        return {"zuul_static_nonroot_cli_publication_verified": True, "zuul_static_exact_source_oci_content_verified": True,
            "zuul_static_programs_sha256": contract["programs_sha256"],
            "zuul_static_publication_sha256": {"contract": worker.fingerprint(contract),
                "source_revision": hashlib.sha256(event["revision"].encode()).hexdigest(),
                "manifest": receipt["manifest_digest"].removeprefix("sha256:"),
                "config": receipt["config_digest"].removeprefix("sha256:"),
                "layer": receipt["output_layer_digest"].removeprefix("sha256:"),
                "tree": receipt["output_tree_sha256"], "checkout_witness": receipt["checkout_witness_sha256"],
                "source_plan": receipt["source_plan_sha256"]}}

    def jwt_workload_completed(self, context, phase):
        super().jwt_workload_completed(context, phase)
        if self.static_report is None:
            self.static_report = git_exercise(context["test"], publication=lambda source: self.publish(context, source))

    def exercise(self, context):
        result = super().exercise(context)
        if not self.static_report: raise ValueError("The native CLI publication did not run")
        result.update({"valkey_gateway_" + key: value for key, value in self.static_report.items()})
        result.update(valkey_gateway_actual_zuul_job_executed=False, valkey_gateway_full_stack_verified=False)
        return result


def exercise(test):
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    class Application(StaticPublicationChecks, PoolHealthChecks, JobReplicaChecks, CoreReplicaChecks, JWTChecks, DatabasePromotionChecks, ServiceChecks, RegistryChecks, gateway.Application): pass
    return cache.exercise(test, application=Application())
