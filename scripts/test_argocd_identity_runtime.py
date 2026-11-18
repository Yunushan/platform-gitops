#!/usr/bin/env python3
"""Explicit opt-in to real local Keycloak/Argo CD MFA and project RBAC checks."""

import os
import unittest

from test_argocd_identity_runtime_fixture import exercise as application_check
from test_keycloak_runtime_fixture import exercise


class RuntimeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("ARGOCD_IDENTITY_TEST_RUNTIME") == "1", "Explicit disposable Linux runtime acceptance only")
    def test_real_argocd_mfa_and_project_authorization(self):
        report = exercise(self, application_check=lambda context: application_check(self, context))
        for name in ("argocd_configuration_apply_resume_verified", "argocd_server_restart_observed", "argocd_tls_settings_observed", "argocd_mfa_login_verified", "argocd_reader_project_access_verified", "argocd_reader_project_write_denied", "argocd_admin_project_write_verified", "argocd_unassigned_user_denied", "argocd_loopback_listeners_verified", "argocd_private_values_absent_from_logs", "temporary_processes_and_data_removed"):
            self.assertIs(report[name], True)
        self.assertFalse(report["ha_verified"]); self.assertFalse(report["full_stack_verified"])


if __name__ == "__main__": unittest.main()
