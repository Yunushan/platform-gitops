#!/usr/bin/env python3
"""Explicit real prepared Keycloak activation through a new systemd member."""

import os
import unittest
from unittest import mock

from test_keycloak_native_runtime_fixture import exercise as native_check, preflight
from test_keycloak_runtime_fixture import exercise

if os.environ.get("KEYCLOAK_NATIVE_TEST_ARGOCD") == "1":
    from test_argocd_identity_runtime_fixture import exercise as argocd_check
else:
    argocd_check = None


class NativeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("KEYCLOAK_NATIVE_TEST_PREFLIGHT") == "1", "Explicit root Linux native artifact/host inspection only")
    def test_real_distribution_and_host_preflight_without_service_creation(self):
        with mock.patch("test_keycloak_native_runtime_fixture.socket.socket", side_effect=AssertionError("Preflight must not reserve or connect sockets")):
            self.assertTrue(preflight(self)["keycloak_host_preflight_verified"])

    @unittest.skipUnless(os.environ.get("KEYCLOAK_NATIVE_TEST_RUNTIME") == "1", "Explicit disposable root Linux Keycloak/systemd runtime fixture")
    def test_prepared_distribution_real_host_activation_and_original_resume(self):
        def check(context):
            return native_check(self, context, application_check=argocd_check)
        report = exercise(self, application_check=check)
        for key in ("keycloak_host_prepared_activation_verified", "keycloak_host_original_resume_verified", "keycloak_host_kernel_listener_verified", "keycloak_host_oidc_discovery_verified", "keycloak_host_private_values_absent_from_journal", "keycloak_host_temporary_service_and_files_removed", "temporary_processes_and_data_removed"):
            self.assertIs(report[key], True)
        self.assertFalse(report["ha_verified"]); self.assertFalse(report["full_stack_verified"])
        if argocd_check is not None:
            for key in ("argocd_mfa_login_verified", "argocd_reader_project_write_denied", "argocd_admin_project_write_verified", "argocd_unassigned_user_denied", "keycloak_host_argocd_mfa_and_rbac_verified", "keycloak_host_application_original_invocation_verified"):
                self.assertIs(report[key], True)


if __name__ == "__main__": unittest.main()
