#!/usr/bin/env python3
"""Test allowlisted offline scheduler image context preparation."""
import ast
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import prepare_gitlab_gitops_image as image


class ImageContextTest(unittest.TestCase):
    def setUp(self):
        self.binary = b"\x7fELF\x02\x01" + b"\x00" * 12 + b"\x3e\x00" + b"\x00" * 44
        self.plan = {"schema_version": 1, "base_image": "registry.example/runtime@sha256:" + "a" * 64,
                     "kubectl_sha256": hashlib.sha256(self.binary).hexdigest(), "architecture": "amd64"}

    def test_fixed_allowlist_covers_static_local_imports(self):
        names = set(image.PROGRAMS)
        self.assertEqual(len(names), len(image.PROGRAMS))
        for name in names:
            path = image.ROOT / "scripts" / (name + ".py")
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                modules = ([node.module] if isinstance(node, ast.ImportFrom) and node.module else
                           [a.name for a in node.names] if isinstance(node, ast.Import) else [])
                for module in modules:
                    local = module.split(".")[0]
                    if (image.ROOT / "scripts" / (local + ".py")).is_file():
                        self.assertIn(local, names, f"{name} imports {local}")

    def test_context_only_copies_allowlisted_programs_and_verified_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary, output = root / "kubectl-input", root / "context"
            binary.write_bytes(self.binary)
            result = image.prepare(self.plan, binary, output)
            self.assertFalse(result["image_built"])
            self.assertEqual(set(p.name for p in (output / "scripts").iterdir()), {n + ".py" for n in image.PROGRAMS})
            self.assertEqual((output / "kubectl").read_bytes(), self.binary)
            dockerfile = (output / "Dockerfile").read_text()
            self.assertIn("FROM --platform=linux/amd64", dockerfile)
            self.assertLess(dockerfile.index("chmod 0755 scripts && chmod 0644 scripts/*.py"),
                            dockerfile.index("USER 65532:65532"))
            self.assertLess(dockerfile.index("USER 65532:65532"),
                            dockerfile.index("scripts/gitlab_gitops_schedule.py --help"))
            with self.assertRaises(ValueError): image.prepare(self.plan, binary, output)

    def test_unreviewed_or_wrong_architecture_binary_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "kubectl-input"
            binary.write_bytes(self.binary)
            for plan in [dict(self.plan, kubectl_sha256="0" * 64), dict(self.plan, architecture="arm64"),
                         dict(self.plan, base_image="registry.example/runtime:latest")]:
                with self.subTest(plan=plan), self.assertRaises(ValueError):
                    image.prepare(plan, binary, root / "context")
                self.assertFalse((root / "context").exists())

    @unittest.skipUnless(os.environ.get("GITLAB_KUBECTL_IMAGE_FIXTURE"), "reviewed local kubectl fixture not selected")
    def test_context_accepts_selected_real_kubectl(self):
        binary = Path(os.environ["GITLAB_KUBECTL_IMAGE_FIXTURE"]).resolve(strict=True)
        data = binary.read_bytes()
        plan = dict(self.plan, kubectl_sha256=hashlib.sha256(data).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "context"
            result = image.prepare(plan, binary, output)
            self.assertEqual(hashlib.sha256((output / "kubectl").read_bytes()).hexdigest(), result["kubectl_sha256"])
            if os.name == "posix":
                executable = output / "kubectl"
                executable.chmod(0o755)
                env = {"PATH": "/usr/bin:/bin", "HOME": directory, "LANG": "C.UTF-8"}
                version = subprocess.run([str(executable), "version", "--client", "-o", "json"], env=env,
                                         capture_output=True, text=True, timeout=20, check=True)
                self.assertIn("clientVersion", version.stdout)
                cli = subprocess.run(["/usr/bin/python3", "-B", str(output / "scripts" / "gitlab_gitops_schedule.py"), "--help"],
                                     cwd=directory, env=env, capture_output=True, text=True, timeout=20, check=True)
                self.assertIn("--plan-sha256", cli.stdout)


if __name__ == "__main__": unittest.main()
