#!/usr/bin/env python3
"""Guard clean offline Harbor builds and equivalent reproducible provenance."""

import copy
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import build_harbor_pool_health as build
import harbor_pool_health_policy as policy


def receipt():
    return {"build_schema": policy.BUILD_SCHEMA, "variant": policy.VARIANT,
        "patch_sha256": policy.PATCH_SHA, "patched_files": policy.PATCHED_FILES,
        "module_inputs_sha256": policy.MODULE_INPUTS, "source_revision": policy.REVISION,
        "source_archive_sha256": policy.SOURCE_SHA, "go_archive_sha256": policy.GO_SHA,
        "binaries_sha256": policy.BINARIES, "exit": {"core": {"exit_code": 0}, "jobservice": {"exit_code": 0}},
        "compiler_sha256": policy.GO_BINARY_SHA, "generator_sha256": policy.SWAGGER_SHA,
        "source_files_count": 3231, "source_tree_sha256": policy.SOURCE_TREE_SHA,
        "generated_sources_count": 970, "generated_sources_sha256": policy.GENERATED_SHA,
        "patched_files_count": 4201, "patched_tree_sha256": policy.PATCHED_TREE_SHA,
        "preparation_exit": {k: {"exit_code": 0} for k in ("module-verification", "api-generation", "patch")},
        "prepared_from_archives": True, "offline_modules_verified": True, "build_network_disabled": True,
        "temporary_work_removed": True, "original_sources_unchanged": True,
        "patched_sources_unchanged_during_build": True, "original_process_and_cgroup_removed": True,
        "runtime_verified": False, "publisher_signatures_verified": False}


class BuildContracts(unittest.TestCase):
    def test_reproducible_receipt_uses_full_source_and_generation_identity(self):
        value = receipt(); policy.validate_receipt(value)
        changes = [("build_schema", "unknown"), ("generated_sources_count", 969),
            ("generated_sources_sha256", "0" * 64), ("source_tree_sha256", "0" * 64),
            ("patched_tree_sha256", "0" * 64), ("compiler_sha256", "0" * 64),
            ("generator_sha256", "0" * 64), ("offline_modules_verified", False),
            ("build_network_disabled", 1), ("temporary_work_removed", False),
            ("original_process_and_cgroup_removed", False), ("prepared_from_archives", False),
            ("preparation_exit", {k: {"exit_code": False} for k in ("module-verification", "api-generation", "patch")})]
        for key, replacement in changes:
            changed = copy.deepcopy(value); changed[key] = replacement
            with self.subTest(field=key), self.assertRaises(ValueError): policy.validate_receipt(changed)
        del value["build_schema"]
        with self.assertRaises(ValueError): policy.validate_receipt(value)

    def test_legacy_receipt_still_requires_its_exact_original_base_receipt(self):
        value = receipt()
        del value["build_schema"]
        value["base_generated_build_receipt_sha256"] = policy.BASE_RECEIPT_SHA
        policy.validate_receipt(value)
        value["base_generated_build_receipt_sha256"] = "0" * 64
        with self.assertRaises(ValueError): policy.validate_receipt(value)

    def archive(self, root, rows):
        path = root / "input.tar.gz"
        with tarfile.open(path, "w:gz") as bundle:
            for name, kind, raw in rows:
                member = tarfile.TarInfo(name); member.mode = 0o4755
                if kind == "file": member.size = len(raw); bundle.addfile(member, io.BytesIO(raw))
                else:
                    member.type = {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE, "fifo": tarfile.FIFOTYPE}[kind]
                    member.linkname = "outside"; bundle.addfile(member)
        return path

    def test_extract_rejects_escape_links_duplicates_and_parent_files_before_writes(self):
        invalid = [[(name, "file", b"x")] for name in ("root/../outside", "/root/outside", "other/file", "root/a\\b", "root/a//b", "root/./b")]
        invalid += [[("root/link", kind, b"")] for kind in ("symlink", "hardlink", "fifo")]
        invalid += [[("root/a", "file", b"x"), ("root/a", "file", b"y")],
            [("root/a", "file", b"x"), ("root/a/b", "file", b"y")]]
        for rows in invalid:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); archive = self.archive(root, rows); destination = root / "out"; destination.mkdir()
                with self.subTest(shape=str(rows)), self.assertRaises(ValueError): build.extract_archive(archive, destination, "root")
                self.assertEqual(list(destination.iterdir()), [])

    def test_extract_keeps_reviewed_file_bytes_and_drops_privileged_mode_bits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive = self.archive(root, [("root/bin/tool", "file", b"reviewed bytes")])
            destination = root / "out"; destination.mkdir()
            extracted = build.extract_archive(archive, destination, "root")
            self.assertEqual((extracted / "bin/tool").read_bytes(), b"reviewed bytes")
            self.assertEqual((extracted / "bin/tool").stat().st_mode & 0o7000, 0)

    def test_archive_limits_and_duplicate_tree_identity_refuse(self):
        member = tarfile.TarInfo("root/large"); member.size = 32 * 1024 ** 2 + 1
        with self.assertRaises(ValueError): build.archive_member(member, "root")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive = self.archive(root, [("root/file", "file", b"123")])
            with mock.patch.object(build, "MAX_UNPACKED", 2), self.assertRaises(ValueError): build.extract_archive(archive, root / "never-created", "root")
            self.assertFalse((root / "never-created").exists())
            (root / "original").mkdir(); (root / "original/source.go").write_text("original")
            before = build.digest(build.tree_files(root / "original"))
            (root / "original/extra.go").write_text("unreviewed")
            self.assertNotEqual(build.digest(build.tree_files(root / "original")), before)

    def test_unreviewed_inputs_refuse_before_any_command_or_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "source.tar.gz"; source.write_bytes(b"unreviewed")
            with mock.patch.object(build, "run_bounded") as execute, self.assertRaises(ValueError):
                build.checked_inputs({"source_archive": str(source)})
            execute.assert_not_called(); self.assertEqual(list(root.iterdir()), [source])

    def test_private_output_is_create_only_and_git_trust_is_checkout_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); private = root / "private/validation/harbor"; private.mkdir(parents=True)
            with mock.patch.object(build, "ROOT", root), mock.patch.object(build, "PRIVATE", private), mock.patch.object(build, "run_bounded", return_value=SimpleNamespace(returncode=0)) as execute:
                out = private / "new-build"; self.assertEqual(build.private_output(out, create=True), out)
                command = execute.call_args.args[0]
                self.assertEqual(command[:5], ["git", "-c", "safe.directory=" + str(root), "check-ignore", "--quiet"])
                self.assertNotIn("--global", command)
                with self.assertRaises(FileExistsError): build.private_output(out, create=True)
                with self.assertRaises(ValueError): build.private_output(root / "outside", create=True)
                execute.return_value.returncode = 1
                with self.assertRaises(ValueError): build.private_output(private / "unignored", create=True)
                self.assertFalse((private / "unignored").exists())

    def test_failed_or_timed_out_stage_never_reports_success_or_prints_private_details(self):
        stages = {}; out = Path("synthetic-private-output")
        with mock.patch.object(build, "write_json") as report, mock.patch.object(build, "atomic_write_text") as log, mock.patch.object(build, "run_bounded", return_value=SimpleNamespace(returncode=1, stdout="private synthetic detail", stderr="")):
            with self.assertRaises(ValueError) as failure: build.stage_command(out, "core", ["reviewed-go"], out, {}, stages, 3)
            self.assertNotIn("private synthetic detail", str(failure.exception)); self.assertEqual(stages, {"core": {"exit_code": 1}})
            log.assert_called_once(); report.assert_called_once()
        stages = {}
        with mock.patch.object(build, "write_json"), mock.patch.object(build, "run_bounded", side_effect=subprocess.TimeoutExpired("reviewed-go", 3)):
            with self.assertRaises(ValueError): build.stage_command(out, "core", ["reviewed-go"], out, {}, stages, 3)
            self.assertEqual(stages, {"core": {"completed": False}})

    def test_initial_observation_failure_reuses_the_original_job_handle(self):
        unit = "platform-harbor-pool-build-" + "a" * 32 + ".service"
        state = {"LoadState": "loaded", "ActiveState": "active", "MainPID": "123", "InvocationID": "b" * 32,
            "ControlGroup": "/system.slice/" + unit, "PrivateNetwork": "yes"}
        with mock.patch.object(build, "unit_state", side_effect=[OSError("temporary"), state]) as observe, mock.patch.object(build, "process_ticks", return_value="owned-ticks"), mock.patch.object(build.time, "sleep") as pause:
            self.assertEqual(build.capture_launch(unit), (state, 123, "owned-ticks"))
            self.assertEqual(observe.call_args_list, [mock.call(unit), mock.call(unit)]); pause.assert_called_once_with(1)
        with mock.patch.object(build, "unit_state", return_value={**state, "ControlGroup": "/unowned"}), self.assertRaises(ValueError): build.capture_launch(unit)

    def test_authoritative_terminal_state_does_not_relaunch_or_fake_identity(self):
        with mock.patch.object(build, "unit_state", return_value={"LoadState": "not-found", "ActiveState": "inactive"}), mock.patch.object(build, "run_bounded") as execute:
            with self.assertRaisesRegex(ValueError, "ended before identity capture"): build.capture_launch("original.service")
            execute.assert_not_called()

    def test_missing_job_observation_is_not_terminal_evidence(self):
        with mock.patch.object(build, "run_bounded", return_value=SimpleNamespace(stdout="", returncode=1)):
            with self.assertRaisesRegex(ValueError, "observation unavailable"): build.unit_state("owned-job.service")


if __name__ == "__main__": unittest.main()
