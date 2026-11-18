#!/usr/bin/env python3
"""Opt-in Docker engine prerequisites for prepared Keycloak activation."""

import json
import os
from pathlib import Path
import unittest

from test_docker_engine_fixture import exercise, SHA256 as ENGINE_ARCHIVE
from test_keycloak_docker_runtime_fixture import exercise as docker_workload
from test_keycloak_runtime_fixture import exercise as identity_runtime

if os.environ.get("KEYCLOAK_DOCKER_TEST_ARGOCD") == "1":
    from test_argocd_identity_runtime_fixture import exercise as application_check
else:
    application_check = None


class DockerRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("KEYCLOAK_DOCKER_TEST_RUNTIME") == "1", "Explicit disposable optimized Keycloak Docker runtime fixture")
    def test_real_prepared_keycloak_docker_activation_and_original_resume(self):
        def check(context):
            engine = exercise(self, workload=lambda selected: docker_workload(self, context, selected, application_check=application_check))
            result = engine["workload"]
            for key in ("docker_engine_transport_verified", "docker_socket_alias_refusal_verified", "docker_kernel_isolation_verified", "docker_resource_controllers_verified", "docker_engine_cleanup_verified"):
                self.assertIs(engine[key], True)
            result["keycloak_host_docker_engine_cleanup_verified"] = engine["docker_engine_cleanup_verified"]
            result["keycloak_host_docker_resource_controllers_verified"] = engine["docker_resource_controllers_verified"]
            result["keycloak_host_artifacts_sha256"]["engine_archive"] = ENGINE_ARCHIVE
            return result
        report = identity_runtime(self, application_check=check)
        for key in ("keycloak_host_docker_runtime_verified", "keycloak_host_docker_original_resume_verified", "keycloak_host_docker_kernel_listener_verified",
                    "keycloak_host_docker_oidc_discovery_verified", "keycloak_host_docker_private_values_absent_from_logs", "keycloak_host_docker_container_cleanup_verified", "keycloak_host_docker_engine_cleanup_verified", "temporary_processes_and_data_removed"):
            self.assertIs(report[key], True)
        for key in ("ha_verified", "full_stack_verified", "keycloak_host_docker_physical_node_ha_verified", "keycloak_host_docker_publisher_signature_verified"):
            self.assertIs(report[key], False)
        if application_check is not None:
            for key in ("argocd_mfa_login_verified", "argocd_reader_project_write_denied", "argocd_admin_project_write_verified", "argocd_unassigned_user_denied"):
                self.assertIs(report[key], True)

    @unittest.skipUnless(os.environ.get("KEYCLOAK_DOCKER_TEST_ENGINE") == "1", "Explicit disposable root Linux Docker engine fixture")
    def test_real_engine_private_transport_and_kernel_isolation(self):
        report = exercise(self)
        for key in ("docker_engine_transport_verified", "docker_socket_alias_refusal_verified", "docker_kernel_isolation_verified", "docker_resource_controllers_verified", "docker_engine_cleanup_verified"):
            self.assertIs(report[key], True)
        for key in ("keycloak_runtime_verified", "ha_verified", "full_stack_verified"): self.assertIs(report[key], False)
        destination = os.environ.get("DOCKER_ENGINE_TEST_REPORT")
        if destination:
            root = Path(__file__).resolve().parents[1] / "private"; path = Path(destination).absolute()
            if path.resolve() != path or not path.is_relative_to(root.resolve()) or not path.parent.is_dir(): raise ValueError("Use a new report inside existing private storage")
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                json.dump(report, stream, sort_keys=True, indent=2); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())


if __name__ == "__main__": unittest.main()
