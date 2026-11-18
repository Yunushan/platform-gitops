import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

import prepare_jenkins_poller_image as subject


def make_wheel(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("pyyaml-6.0.3.dist-info/METADATA", "Metadata-Version: 2.1\nName: PyYAML\nVersion: 6.0.3\n")
        archive.writestr("pyyaml-6.0.3.dist-info/WHEEL", "Wheel-Version: 1.0\n")
        archive.writestr("pyyaml-6.0.3.dist-info/RECORD", "")


class PollerImageContextTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0
                         and os.environ.get("POLLER_NONROOT_FIXTURE") == "1" and shutil.which("runuser"), "explicit non-root Linux fixture not requested")
    def test_prepared_scripts_run_as_nonroot_with_private_state(self):
        with tempfile.TemporaryDirectory(prefix="poller-image-", dir="/tmp") as directory:
            root = Path(directory); root.chmod(0o755)
            (root / "private").mkdir(mode=0o700); (root / "scripts").mkdir()
            source = Path(__file__).resolve().parent
            for name in subject.MODULES: shutil.copyfile(source / (name + ".py"), root / "scripts" / (name + ".py"))
            wheel = root / "pyyaml-6.0.3-py3-none-any.whl"; make_wheel(wheel)
            plan = {"schema_version": 1, "base_image": "python@sha256:" + "a" * 64,
                    "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
            context = root / "private" / "context"; subject.prepare(plan, wheel, context, root=root)
            image = root / "image"; image.mkdir(mode=0o755)
            shutil.copytree(context / "scripts", image / "scripts")
            state = image / "private"; state.mkdir(mode=0o700); os.chown(state, 65534, 65534)
            queue = state / "queue"; queue.mkdir(mode=0o700); os.chown(queue, 65534, 65534)
            reviews = state / "reviews"; reviews.mkdir(mode=0o700); os.chown(reviews, 65534, 65534)
            for script in ("scan_jenkins_gerrit.py", "scan_jenkins_gerrit_results.py"):
                run = subprocess.run(["runuser", "-u", "nobody", "--", sys.executable, "-B", str(image / "scripts" / script), "--help"],
                                     cwd="/tmp", capture_output=True, text=True, timeout=30)
                self.assertEqual(run.returncode, 0, run.stderr)
            check = ("import sys,pathlib; sys.path.insert(0,sys.argv[1]); import trigger_jenkins_gerrit as t; "
                     "import os; assert os.geteuid()!=0 and not os.access(pathlib.Path(sys.argv[1])/'scan_jenkins_gerrit.py', os.W_OK); "
                     "import scan_jenkins_gerrit as s; t.ROOT=pathlib.Path(sys.argv[2]); "
                     "lock=s.cycle_lock(pathlib.Path(sys.argv[2])/'private'/'queue', '.jenkins-queue-cycle.lock'); "
                     "lock.__enter__(); lock.__exit__(None,None,None)")
            run = subprocess.run(["runuser", "-u", "nobody", "--", sys.executable, "-B", "-c", check,
                                  str(image / "scripts"), str(image)], cwd="/tmp", capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual((queue / ".jenkins-queue-cycle.lock").stat().st_uid, 65534)
            self.assertEqual((image / "scripts" / "scan_jenkins_gerrit.py").stat().st_uid, 0)
            command = ["runuser", "-u", "nobody", "--", sys.executable, "-B", str(image / "scripts/check_jenkins_poller_state.py")]
            result = subprocess.run(command, cwd="/tmp", capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["state_directories_ready"])
            self.assertTrue(json.loads(result.stdout)["local_process_lock_verified"])
            self.assertFalse(json.loads(result.stdout)["cross_node_locking_verified"])
            broken_lock = ("import sys,pathlib,fcntl; sys.path.insert(0,sys.argv[1]); "
                           "import check_jenkins_poller_state as c; fcntl.flock=lambda *args: None; c.check(pathlib.Path(sys.argv[2]))")
            result = subprocess.run(["runuser", "-u", "nobody", "--", sys.executable, "-B", "-c", broken_lock,
                                     str(image / "scripts"), str(image)], cwd="/tmp", capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            reviews.chmod(0o750)
            result = subprocess.run(command, cwd="/tmp", capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)

    def test_context_scripts_can_load_their_local_helpers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "private").mkdir(); (root / "scripts").mkdir()
            source = Path(__file__).resolve().parent
            for name in subject.MODULES: shutil.copyfile(source / (name + ".py"), root / "scripts" / (name + ".py"))
            wheel = root / "PyYAML-6.0.2-py3-none-any.whl"; make_wheel(wheel)
            plan = {"schema_version": 1, "base_image": "python@sha256:" + "a" * 64,
                    "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
            output = root / "private" / "context"; subject.prepare(plan, wheel, output, root=root)
            for script in ("scan_jenkins_gerrit.py", "scan_jenkins_gerrit_results.py"):
                env = dict(os.environ); env.pop("PYTHONPATH", None)
                result = subprocess.run([sys.executable, "-B", str(output / "scripts" / script), "--help"], cwd=root, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_local_import_graph_is_included(self):
        root = Path(__file__).resolve().parent
        pending = ["scan_jenkins_gerrit", "scan_jenkins_gerrit_results", "check_jenkins_poller_state"]; seen = set()
        while pending:
            name = pending.pop()
            if name in seen: continue
            seen.add(name)
            for node in ast.walk(ast.parse((root / (name + ".py")).read_text(encoding="utf-8"))):
                names = [item.name.split(".")[0] for item in node.names] if isinstance(node, ast.Import) else ([node.module.split(".")[0]] if isinstance(node, ast.ImportFrom) and node.module else [])
                pending.extend(name for name in names if (root / (name + ".py")).is_file())
        self.assertEqual(seen, set(subject.MODULES))

    def test_context_contains_only_explicit_files_and_requires_matching_wheel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "private").mkdir(); (root / "scripts").mkdir()
            for name in subject.MODULES: (root / "scripts" / (name + ".py")).write_text("# fixture\n")
            wheel = root / "PyYAML-6.0.2-py3-none-any.whl"; make_wheel(wheel)
            plan = {"schema_version": 1, "base_image": "python@sha256:" + "a" * 64,
                    "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
            output = root / "private" / "context"
            result = subject.prepare(plan, wheel, output, root=root)
            self.assertFalse(result["image_built"])
            self.assertEqual({p.name for p in output.iterdir()}, {"scripts", wheel.name, "Dockerfile", "context-manifest.json"})
            self.assertIn("--no-index --no-deps", (output / "Dockerfile").read_text())
            with self.assertRaises(FileExistsError): subject.prepare(plan, wheel, output, root=root)
            plan["wheel_sha256"] = "0" * 64
            with self.assertRaises(ValueError): subject.prepare(plan, wheel, root / "private" / "wrong", root=root)
            self.assertFalse((root / "private" / "wrong").exists())
            with self.assertRaises(ValueError): subject.prepare(plan, wheel, root / "public", root=root)

    def test_normalized_lowercase_wheel_name_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "private").mkdir(); (root / "scripts").mkdir()
            for name in subject.MODULES: (root / "scripts" / (name + ".py")).write_text("# fixture\n")
            wheel = root / "pyyaml-6.0.3-cp312-cp312-manylinux_x86_64.whl"; make_wheel(wheel)
            plan = {"schema_version": 1, "base_image": "python@sha256:" + "a" * 64,
                    "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
            self.assertFalse(subject.prepare(plan, wheel, root / "private" / "context", root=root)["image_built"])

    def test_matching_digest_does_not_accept_non_wheel_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "private").mkdir()
            wheel = root / "pyyaml-6.0.3-py3-none-any.whl"; wheel.write_bytes(b"not a wheel")
            plan = {"schema_version": 1, "base_image": "python@sha256:" + "a" * 64,
                    "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
            with self.assertRaises((ValueError, zipfile.BadZipFile)):
                subject.prepare(plan, wheel, root / "private" / "context", root=root)
            self.assertFalse((root / "private" / "context").exists())


if __name__ == "__main__": unittest.main()
