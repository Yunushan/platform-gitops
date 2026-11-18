#!/usr/bin/env python3
"""Shared Kubernetes failures preserve safe categories without retry or secrets."""

import json
from types import SimpleNamespace
import unittest
from unittest import mock

import kubernetes_management as management


class KubernetesFailureTests(unittest.TestCase):
    def client(self):
        client = management.Client.__new__(management.Client)
        client.prefix = ["kubectl", "--kubeconfig", "/private/config", "--context", "explicit"]
        return client

    def test_uncertain_write_is_not_retried_and_private_details_are_not_retained(self):
        secret = "<SYNTHETIC_PRIVATE_VALUE>"
        result = SimpleNamespace(returncode=1, stdout=secret, stderr="failed calling webhook: context deadline exceeded " + secret)
        with mock.patch.object(management, "run_bounded", return_value=result) as runner:
            with self.assertRaises(management.KubernetesOperationError) as caught:
                self.client().call(["apply", "-f", "-"], payload=secret.encode())
        runner.assert_called_once()
        error = caught.exception
        self.assertIsInstance(error, ValueError)
        self.assertTrue(error.diagnostic["signals"]["webhook_failure"])
        self.assertTrue(error.diagnostic["signals"]["timeout"])
        self.assertNotIn(secret, str(error) + repr(vars(error)) + json.dumps(error.diagnostic))
        self.assertEqual(set(vars(error)), {"diagnostic"})

    def test_discovery_conflict_and_unclassified_errors_remain_distinct(self):
        cases = (("no matches for kind: ensure CRDs are installed first", {"discovery_not_ready"}),
            ("Apply failed with conflict with another field manager", {"field_conflict"}),
            ("x509: certificate signed by unknown authority", {"tls_failure", "certificate_untrusted", "tls_non_timeout_failure"}),
            ('failed calling webhook: https://example.invalid/?timeout=10s: x509: certificate has expired or is not yet valid',
                {"webhook_failure", "tls_failure", "certificate_time_invalid", "tls_non_timeout_failure"}),
            ('https://example.invalid/?timeout=10s: net/http: TLS handshake timeout',
                {"tls_failure", "timeout", "tls_handshake_timeout"}),
            ('Error from server (Invalid): Task is invalid: Invalid value', {"invalid_resource"}),
            ('Error from server (Timeout): the server was unable to return a response', {"timeout"}),
            ('Unable to connect to the server: net/http: request canceled (Client.Timeout exceeded while awaiting headers)', {"timeout"}),
            ("<SYNTHETIC_PRIVATE_VALUE>", set()))
        for text, expected in cases:
            with self.subTest(expected=expected):
                error = management.KubernetesOperationError(1, text)
                self.assertEqual({name for name, value in error.diagnostic["signals"].items() if value}, expected)

    def test_timeout_only_and_mixed_certificate_or_protocol_errors_stay_distinct(self):
        timeout='net/http: TLS handshake timeout'
        self.assertFalse(management.KubernetesOperationError(1,timeout).diagnostic['signals']['tls_non_timeout_failure'])
        for text in ('x509: certificate has expired','remote error: tls: bad certificate','TLS handshake failure','SSL protocol error','certificate validation failed'):
            with self.subTest(text=text):
                self.assertTrue(management.KubernetesOperationError(1,timeout+'; '+text).diagnostic['signals']['tls_non_timeout_failure'])

    def test_success_returns_output_without_a_second_operation(self):
        result = SimpleNamespace(returncode=0, stdout='{"items": []}', stderr="")
        with mock.patch.object(management, "run_bounded", return_value=result) as runner:
            self.assertEqual(self.client().call(["get", "pods", "-o", "json"]), result.stdout)
        runner.assert_called_once()


if __name__ == "__main__":
    unittest.main()
