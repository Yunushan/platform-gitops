#!/usr/bin/env python3
"""Test optional release policies, bounded image-only edits and renderer integration."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import date
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import platform_release_policy as policy
import render_private_platform_values as renderer
from synthetic_private_profile import RENDERER_ENV_PREFIXES
import update_platform_versions as updater


FORGEJO = '''# Keep storage and auth exactly as configured.
image:
  rootless: true
  tag: "15.0.6-rootless" # reviewed pin
persistence:
  size: 100Gi
  storageClass: longhorn-critical
gitea:
  config:
    server:
      ROOT_URL: https://git.example.test/
'''
WOODPECKER = '''server:
  image:
    repository: woodpeckerci/woodpecker-server
    tag: "v3.16.0"
  persistentVolume:
    size: 10Gi
  env:
    WOODPECKER_OPEN: "false"
agent:
  image:
    repository: woodpeckerci/woodpecker-agent
    tag: "v3.16.0"
'''
ARGOCD = '''global:
  domain: argocd.example.test

server:
  replicas: 3
redis:
  image:
    tag: "8.10.0-alpine"
configs:
  cm:
    admin.enabled: "true"
    oidc.config: |
      name: Existing provider
      clientSecret: $x:key
'''


class VersionUpdatesTest(unittest.TestCase):
    def setUp(self):
        self.catalog = policy.load_catalog()
        self.env = patch.dict(os.environ, {
            key: value for key, value in os.environ.items()
            if not key.startswith(RENDERER_ENV_PREFIXES)
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_default_is_pinned_not_latest(self):
        for component, tag in (("forgejo", "15.0.6"), ("woodpecker", "v3.16.0"), ("argocd", "v3.5.0")):
            self.assertEqual(policy.select_release(component), tag)
        self.assertEqual(policy.select_release("forgejo", current="16.0.5-rootless"), "16.0.5-rootless")

    def test_reviewed_channels_and_expiry(self):
        for channel in ("lts", "lts-stable"):
            self.assertEqual(policy.select_release("forgejo", channel=channel, current="15.0.6-rootless", today=date(2026, 10, 1)), "15.0.9-rootless")
        self.assertEqual(policy.select_release("forgejo", channel="stable", today=date(2026, 10, 1)), "16.0.5")
        for component in ("woodpecker", "argocd"):
            with self.assertRaises(policy.ReleasePolicyError):
                policy.select_release(component, channel="lts")
        for today in (date(2026, 9, 1), date(2026, 10, 29), date(2027, 1, 1)):
            with self.assertRaises(policy.ReleasePolicyError):
                policy.select_release("forgejo", channel="lts", today=today)
        self.assertEqual(policy.select_release("forgejo", today=date(2027, 1, 1)), "15.0.6")

    def test_exact_versions_and_conflicts(self):
        for component in policy.COMPONENTS:
            for tag in ("latest", "stable", "next", "3.5", "v3.6.0-rc1", "15.0.9+build", "https://credential.example.test", "03.5.0", "1.2.3\n"):
                with self.assertRaises(policy.ReleasePolicyError):
                    policy.select_release(component, channel="specific", specific=tag)
            with self.assertRaises(policy.ReleasePolicyError):
                policy.select_release(component, channel="specific")
        self.assertEqual(policy.select_release("woodpecker", channel="specific", specific="3.18.1"), "v3.18.1")
        self.assertEqual(policy.select_release("forgejo", channel="specific", specific="v15.0.9-rootless"), "15.0.9-rootless")
        with self.assertRaises(policy.ReleasePolicyError):
            policy.select_release("forgejo", channel="lts", specific="15.0.6", today=date(2026, 10, 1))

    def test_only_image_scalars_change_and_comments_survive(self):
        rendered = updater.update_values_text("forgejo", FORGEJO, "15.0.9-rootless", current=["15.0.6-rootless"])
        self.assertEqual(rendered, FORGEJO.replace('"15.0.6-rootless"', '"15.0.9-rootless"'))
        wood = updater.update_values_text("woodpecker", WOODPECKER, "v3.18.1", current=["v3.16.0", "v3.16.0"])
        self.assertEqual(wood, WOODPECKER.replace("v3.16.0", "v3.18.1"))
        argo = updater.update_values_text("argocd", ARGOCD, "v3.5.3", current=["v3.5.0"])
        expected = updater.values_document(ARGOCD)
        expected["global"]["image"] = {"tag": "v3.5.3"}
        self.assertEqual(updater.values_document(argo), expected)
        self.assertIn('tag: "8.10.0-alpine"', argo)
        self.assertIn("clientSecret: $x:key", argo)
        self.assertEqual(updater.update_values_text("argocd", argo, "v3.5.3", current=["v3.5.3"]), argo)
        crlf = FORGEJO.replace("\n", "\r\n")
        self.assertEqual(updater.update_values_text("forgejo", crlf, "15.0.9-rootless", current=["15.0.6-rootless"]), crlf.replace("15.0.6", "15.0.9"))

    def test_digest_and_overrides_cannot_silently_defeat_update(self):
        digest_values = FORGEJO.replace('  rootless: true', '  digest: sha256:' + 'a' * 64 + '\n  rootless: true')
        with self.assertRaises(policy.ReleasePolicyError):
            updater.update_values_text("forgejo", digest_values, "15.0.9", current=["15.0.6"])
        with self.assertRaises(policy.ReleasePolicyError):
            updater.update_values_text("forgejo", FORGEJO.replace("  rootless: true", "  fullOverride: example.test/forgejo:15.0.6\n  rootless: true"), "15.0.9", current=["15.0.6"])
        with self.assertRaises(policy.ReleasePolicyError):
            updater.update_values_text("forgejo", FORGEJO.replace("rootless: true", "rootless: false"), "15.0.9-rootless", current=["15.0.6"])
        argo = ARGOCD.replace("  replicas: 3", '  image:\n    tag: "v3.5.0"\n  replicas: 3')
        with self.assertRaises(policy.ReleasePolicyError):
            updater.update_values_text("argocd", argo, "v3.5.3", current=["v3.5.0"])
        for text in ("image: {}\n", 'image:\n  tag: x\n  tag: y\n', "image: &x\n  tag: x\nother: *x\n"):
            with self.assertRaises(policy.ReleasePolicyError):
                updater.update_values_text("forgejo", text, "15.0.9", current=["15.0.6"])

    def test_cli_plan_write_preflight_and_downgrades(self):
        with tempfile.TemporaryDirectory() as folder:
            forgejo = Path(folder) / "forgejo.yaml"
            wood = Path(folder) / "wood.yaml"
            argo = Path(folder) / "argo.yaml"
            forgejo.write_text(FORGEJO, encoding="utf-8")
            wood.write_text(WOODPECKER, encoding="utf-8")
            argo.write_text(ARGOCD, encoding="utf-8")
            args = ["--forgejo", "specific", "--forgejo-version", "15.0.9", "--forgejo-values", str(forgejo),
                    "--woodpecker", "specific", "--woodpecker-version", "3.18.1", "--woodpecker-values", str(wood),
                    "--argocd", "specific", "--argocd-version", "3.5.3", "--argocd-values", str(argo)]
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(updater.main(args), 0)
            report = json.loads(output.getvalue())
            self.assertFalse(report["cluster_changed"])
            self.assertNotIn(str(forgejo), output.getvalue())
            self.assertEqual(forgejo.read_text(encoding="utf-8"), FORGEJO)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(updater.main(args + ["--write"]), 0)
            self.assertIn("15.0.9-rootless", forgejo.read_text(encoding="utf-8"))
            self.assertIn('tag: "v3.5.3"', argo.read_text(encoding="utf-8"))
            new_forgejo = forgejo.read_text(encoding="utf-8")
            for extra in (["--write", "--forgejo-version", "15.0.6"], ["--write", "--woodpecker-version", "next"],
                          ["--write", "--forgejo-version", "16.0.5"]):
                with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    self.assertEqual(updater.main(args + extra), 1)
                self.assertEqual(forgejo.read_text(encoding="utf-8"), new_forgejo)
            wood.write_text(WOODPECKER + "host: <PLATFORM_DOMAIN>\n", encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                self.assertEqual(updater.main(args + ["--write"]), 1)
            self.assertEqual(forgejo.read_text(encoding="utf-8"), new_forgejo)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(updater.main(["--write", "--allow-major-upgrade", "--forgejo", "specific",
                                              "--forgejo-version", "16.0.5", "--forgejo-values", str(forgejo)]), 0)

    def test_full_renderer_honors_opt_in_and_pin_refresh_keeps_newer_versions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "forgejo.yaml"
            path.write_text(FORGEJO, encoding="utf-8")
            with patch.dict(os.environ, {"FORGEJO_UPDATE_CHANNEL": "specific", "FORGEJO_IMAGE_TAG": "15.0.9"}):
                self.assertTrue(renderer.refresh_forgejo_reviewed_image_pin(path))
                self.assertFalse(renderer.refresh_forgejo_reviewed_image_pin(path))
            self.assertFalse(renderer.refresh_forgejo_reviewed_image_pin(path))
            self.assertIn("100Gi", path.read_text(encoding="utf-8"))
            argo = Path(folder) / "argo.yaml"
            argo.write_text(ARGOCD, encoding="utf-8")
            with patch.dict(os.environ, {"ARGOCD_UPDATE_CHANNEL": "specific", "ARGOCD_IMAGE_TAG": "3.5.3", "PLATFORM_SSO_ENABLED": "false"}):
                self.assertTrue(renderer.render_argocd(argo, {"platform_argocd_host": "argocd.example.test"}))
            self.assertIn('tag: "v3.5.3"', argo.read_text(encoding="utf-8"))
            with patch.dict(os.environ, {"WOODPECKER_UPDATE_CHANNEL": "specific", "WOODPECKER_IMAGE_TAG": "3.18.1"}):
                self.assertEqual(renderer.selected_image_tag("woodpecker"), "v3.18.1")

    def test_catalog_baselines_match_vendored_app_versions(self):
        root = Path(__file__).resolve().parents[1]
        charts = {
            "forgejo": "forgejo/charts/forgejo-17.1.4/forgejo/Chart.yaml",
            "woodpecker": "woodpecker/charts/woodpecker-3.6.5/woodpecker/Chart.yaml",
            "argocd": "argocd-ha/charts/argo-cd-10.3.2/argo-cd/Chart.yaml",
        }
        for component, chart in charts.items():
            values = updater.values_document((root / "gitops/clusters/rke2-main/premium-3node/apps" / chart).read_text(encoding="utf-8"))
            self.assertEqual(policy.version_tuple(component, str(values["appVersion"])), policy.version_tuple(component, self.catalog["components"][component]["pinned"]))

    def test_renderer_rejects_invalid_policy_before_writing_another_app(self):
        with tempfile.TemporaryDirectory() as folder:
            argo = Path(folder) / "argo.yaml"
            argo.write_text(ARGOCD, encoding="utf-8")
            with patch.dict(os.environ, {"FORGEJO_UPDATE_CHANNEL": "specific", "FORGEJO_IMAGE_TAG": "latest"}), patch("sys.argv", ["renderer", "--argocd-values", str(argo)]):
                with self.assertRaises(SystemExit):
                    renderer.main()
            self.assertEqual(argo.read_text(encoding="utf-8"), ARGOCD)


if __name__ == "__main__":
    unittest.main()
