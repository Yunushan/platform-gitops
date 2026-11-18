#!/usr/bin/env python3
"""Offline, supervised rebuild of the exact local Harbor pool-health candidate."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import sys
import tarfile
import tempfile
import time
import uuid

from atomic_file import atomic_binary_output
from atomic_file import atomic_write_text
from bounded_file import open_bounded_file, read_bootstrap_bytes, read_bounded_bytes, read_bounded_text
from bounded_subprocess import run_bounded
import harbor_pool_health_policy as policy
from strict_json import loads_strict_json

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / "private/validation/harbor"
PATCH = ROOT / "patches/harbor/2.15.1-pg-pool-pre-use-health.patch"
MAX_UNPACKED = 256 * 1024 ** 2


def digest(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_hash(path, limit=128 * 1024 ** 2):
    path = Path(path)
    if path.is_symlink() or not path.is_file(): raise ValueError("Require a regular reviewed input")
    value = hashlib.sha256()
    with open_bounded_file(path, max_bytes=limit) as stream:
        while block := stream.read(1024 ** 2):
            value.update(block)
    return value.hexdigest()


def require_hash(path, expected, limit=128 * 1024 ** 2):
    if file_hash(path, limit) != expected: raise ValueError("Reviewed input identity changed")


def tree_files(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink(): raise ValueError("Do not follow source-tree links")
        if path.is_file(): result[path.relative_to(root).as_posix()] = file_hash(path, 32 * 1024 ** 2)
    return result


def archive_member(member, prefix):
    name = member.name.rstrip("/") if member.isdir() else member.name
    parts = PurePosixPath(name).parts
    if (not name or len(name) > 400 or "\\" in name or name.startswith("/") or "//" in name
            or any(part in {".", ".."} for part in name.split("/")) or not parts or parts[0] != prefix
            or not (member.isfile() or member.isdir()) or member.size < 0 or member.size > 32 * 1024 ** 2):
        raise ValueError("Unreviewed archive member")
    if name != "/".join(parts): raise ValueError("Noncanonical archive member")
    return parts


def extract_archive(archive, destination, prefix):
    # All members are validated before creating any extracted paths.
    payload = read_bootstrap_bytes(archive, 128 * 1024 ** 2, follow_symlinks=False)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as bundle:
        members = []; names = set(); total = 0
        for member in bundle:
            parts = archive_member(member, prefix); name = "/".join(parts)
            if name in names or len(members) >= 20000: raise ValueError("Duplicate or excessive archive members")
            names.add(name); members.append((member, parts)); total += member.size
            if total > MAX_UNPACKED: raise ValueError("Archive exceeds unpacked byte bound")
        files = {"/".join(parts) for member, parts in members if member.isfile()}
        for _, parts in members:
            if any("/".join(parts[:i]) in files for i in range(1, len(parts))):
                raise ValueError("Archive file conflicts with a parent directory")
        for member, parts in members:
            target = destination.joinpath(*parts)
            if member.isdir(): target.mkdir(mode=0o755, parents=True, exist_ok=True); continue
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            mode = 0o755 if member.mode & 0o111 else 0o644
            with bundle.extractfile(member) as source, atomic_binary_output(target, mode=mode, replace=False) as output:
                shutil.copyfileobj(source, output, length=1024 ** 2)
    return destination / prefix


def private_output(path, *, create=False):
    path = Path(path).absolute()
    if path != path.resolve() or path == PRIVATE.resolve() or not path.is_relative_to(PRIVATE.resolve()):
        raise ValueError("Keep build output beneath the ignored private Harbor validation directory")
    probe = path
    while probe != ROOT:
        if probe.is_symlink(): raise ValueError("Do not follow private-output links")
        probe = probe.parent
    result = run_bounded(["git", "-c", "safe.directory=" + str(ROOT), "check-ignore", "--quiet", "--", str(path / "build-inputs.json")], cwd=ROOT,
        text=True, timeout=10, output_max_bytes=16384)
    if result.returncode: raise ValueError("Build output must be ignored by Git")
    if create:
        if not path.parent.is_dir(): raise ValueError("Create the private output parent first")
        path.mkdir(mode=0o700)
    return path


def write_json(path, value):
    atomic_write_text(path, json.dumps(value, indent=2) + "\n")


def checked_inputs(spec):
    require_hash(Path(spec["source_archive"]), policy.SOURCE_SHA, 16 * 1024 ** 2)
    require_hash(Path(spec["go_archive"]), policy.GO_SHA)
    require_hash(Path(spec["swagger"]), policy.SWAGGER_SHA)
    require_hash(PATCH, policy.PATCH_SHA, 16384)
    cache = Path(spec["module_cache"])
    if not cache.is_dir() or cache.is_symlink() or not (cache / "cache/download").is_dir():
        raise ValueError("Use a prepared offline Go module cache")


def stage_command(out, name, arguments, cwd, environment, results, timeout):
    try:
        result = run_bounded(arguments, cwd=cwd, env=environment, text=True, timeout=timeout,
            output_max_bytes=4 * 1024 ** 2)
    except Exception:
        results[name] = {"completed": False}
        write_json(out / "stages.json", results)
        raise ValueError("Bounded build stage failed; retain private diagnostics") from None
    atomic_write_text(out / (name + ".log"), result.stdout + result.stderr)
    results[name] = {"exit_code": result.returncode}
    write_json(out / "stages.json", results)
    if result.returncode: raise ValueError("Offline build stage refused; retain private diagnostics")


def worker(spec_path, spec_sha):
    out = private_output(Path(spec_path).parent)
    require_hash(Path(spec_path), spec_sha, 65536)
    spec = loads_strict_json(read_bounded_bytes(Path(spec_path), max_bytes=65536))
    unit = spec.get("unit")
    if (not isinstance(unit, str) or not re.fullmatch(r"platform-harbor-pool-build-[0-9a-f]{32}\.service", unit)
            or not any(line.endswith("/system.slice/" + unit) for line in read_bounded_text("/proc/self/cgroup", max_bytes=65536).splitlines())
            or not re.fullmatch(r"[0-9a-f]{32}", os.environ.get("INVOCATION_ID", ""))):
        raise ValueError("Build worker requires its original owned systemd job")
    checked_inputs(spec)
    work = Path(tempfile.mkdtemp(prefix="platform-harbor-rebuild-"))
    stages = {}; receipt = None
    try:
        source = extract_archive(Path(spec["source_archive"]), work, "harbor-" + policy.REVISION)
        go_root = extract_archive(Path(spec["go_archive"]), work, "go")
        go = go_root / "bin/go"; require_hash(go, policy.GO_BINARY_SHA, 32 * 1024 ** 2)
        original = tree_files(source)
        if len(original) != 3231 or digest(original) != policy.SOURCE_TREE_SHA:
            raise ValueError("Complete extracted source identity changed")
        (work / "home").mkdir(mode=0o700); (work / "go-cache").mkdir(mode=0o700)
        environment = {"PATH": str(go_root / "bin") + ":/usr/bin:/bin", "HOME": str(work / "home"), "LC_ALL": "C",
            "GOTOOLCHAIN": "local", "GOPROXY": "off", "GOSUMDB": "off", "GOTELEMETRY": "off", "GOENV": "off",
            "GOMODCACHE": spec["module_cache"], "GOPATH": str(work / "go-path"), "GOCACHE": str(work / "go-cache"),
            "GOMAXPROCS": "2", "CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "amd64"}
        stage_command(out, "module-verification", [str(go), "mod", "verify"], source / "src", environment, stages, 90)
        swagger = work / "swagger"; shutil.copyfile(spec["swagger"], swagger); swagger.chmod(0o755)
        require_hash(swagger, policy.SWAGGER_SHA)
        stage_command(out, "api-generation", [str(swagger), "generate", "server", "--template-dir=" + str(source / "tools/swagger/templates"),
            "--exclude-main", "--additional-initialism=CVE", "--additional-initialism=GC", "--additional-initialism=OIDC",
            "-f", str(source / "api/v2.0/swagger.yaml"), "-A", "harbor", "--target", str(source / "src/server/v2.0")], source, environment, stages, 90)
        generated_tree = tree_files(source)
        if any(generated_tree.get(k) != v for k, v in original.items()): raise ValueError("Generation changed original source files")
        generated = {k: v for k, v in generated_tree.items() if k not in original}
        if len(generated) != 970 or digest(generated) != policy.GENERATED_SHA:
            raise ValueError("Complete generated API identity changed")
        stage_command(out, "patch", ["/usr/bin/patch", "-p1", "--fuzz=0", "--batch", "-i", str(PATCH)], source, environment, stages, 10)
        patched = tree_files(source)
        if len(patched) != 4201 or digest(patched) != policy.PATCHED_TREE_SHA:
            raise ValueError("Reviewed complete patched tree changed")
        for name in ("core", "jobservice"):
            stage_command(out, name, [str(go), "build", "-p", "2", "-mod=readonly", "-trimpath", "-buildvcs=false",
                "-ldflags", policy.LINKER_FLAGS, "-o", str(out / ("harbor-" + name)), "./" + name], source / "src", environment, stages, 240)
        if tree_files(source) != patched: raise ValueError("Source or module inputs changed during compilation")
        hashes = {name: file_hash(out / name, 96 * 1024 ** 2) for name in policy.BINARIES}
        if hashes != policy.BINARIES: raise ValueError("Rebuilt candidate binary identity differs")
        checked_inputs(spec)
        receipt = {"build_schema": policy.BUILD_SCHEMA, "variant": policy.VARIANT,
            "source_revision": policy.REVISION, "source_archive_sha256": policy.SOURCE_SHA, "go_archive_sha256": policy.GO_SHA,
            "compiler_sha256": policy.GO_BINARY_SHA, "generator_sha256": policy.SWAGGER_SHA,
            "source_files_count": 3231, "source_tree_sha256": policy.SOURCE_TREE_SHA,
            "generated_sources_count": 970, "generated_sources_sha256": policy.GENERATED_SHA,
            "patched_files_count": 4201, "patched_tree_sha256": policy.PATCHED_TREE_SHA,
            "patch_sha256": policy.PATCH_SHA, "patched_files": policy.PATCHED_FILES, "module_inputs_sha256": policy.MODULE_INPUTS,
            "preparation_exit": {k: stages[k] for k in ("module-verification", "api-generation", "patch")},
            "exit": {k: stages[k] for k in ("core", "jobservice")}, "binaries_sha256": hashes,
            "prepared_from_archives": True, "offline_modules_verified": True, "original_sources_unchanged": True,
            "patched_sources_unchanged_during_build": True, "runtime_verified": False, "publisher_signatures_verified": False,
            "worker_invocation_id": os.environ["INVOCATION_ID"]}
    finally:
        # Only this worker's freshly reserved native temporary directory is removed.
        if work.parent != Path(tempfile.gettempdir()).resolve() or not work.name.startswith("platform-harbor-rebuild-") or work.is_symlink():
            raise ValueError("Refuse cleanup outside the owned build workspace")
        shutil.rmtree(work)
    receipt["temporary_work_removed"] = not work.exists()
    write_json(out / "worker-result.json", receipt)


def unit_state(unit):
    result = run_bounded(["/usr/bin/systemctl", "show", unit, "--property=LoadState,ActiveState,MainPID,InvocationID,ControlGroup,PrivateNetwork"],
        text=True, timeout=10, output_max_bytes=16384)
    state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if "LoadState" not in state: raise ValueError("Original job observation unavailable")
    return state


def process_ticks(pid):
    return read_bounded_text(Path("/proc", str(pid), "stat"), max_bytes=65536).rsplit(") ", 1)[1].split()[19]


def capture_launch(unit):
    deadline = time.monotonic() + 700
    while True:
        try:
            state = unit_state(unit)
        except Exception:
            state = None
        if state is not None:
            if state.get("LoadState") == "not-found" or state.get("ActiveState") not in {"active", "activating", "deactivating"}:
                raise ValueError("Original build ended before identity capture; retain its reservation")
            try: pid = int(state.get("MainPID", "0"))
            except ValueError: pid = 0
            if pid > 0 and re.fullmatch(r"[0-9a-f]{32}", state.get("InvocationID", "")):
                if state.get("PrivateNetwork") != "yes" or state.get("ControlGroup") != "/system.slice/" + unit:
                    raise ValueError("Original build isolation or cgroup identity changed")
                try: return state, pid, process_ticks(pid)
                except OSError: pass  # Reobserve the same job if it exited during PID inspection.
        if time.monotonic() >= deadline:
            raise TimeoutError("Continue observing the original reserved build; do not restart it")
        time.sleep(1)


def supervise(spec, out):
    checked_inputs(spec)
    unit = "platform-harbor-pool-build-" + uuid.uuid4().hex + ".service"; spec["unit"] = unit
    write_json(out / "spec.json", spec)
    snapshots = {p.name: file_hash(p, 2 * 1024 ** 2) for p in sorted((ROOT / "scripts").glob("*.py"))}
    started = time.monotonic()
    command = ["/usr/bin/systemd-run", "--unit=" + unit, "--collect", "--quiet", "--service-type=exec",
        "--property=NoNewPrivileges=yes", "--property=PrivateNetwork=yes", "--property=KillMode=control-group",
        "--property=RuntimeMaxSec=660", "--property=MemoryMax=4G", "--property=TasksMax=512",
        "--property=StandardOutput=append:" + str(out / "build.log"), "--property=StandardError=inherit",
        "/usr/bin/python3", "-B", str(Path(__file__).resolve()), "_worker", str(out / "spec.json"), file_hash(out / "spec.json", 65536)]
    run_bounded(command, text=True, timeout=20, check=True, output_max_bytes=16384)
    state, pid, ticks = capture_launch(unit)
    launch = {"unit": unit, "invocation_id": state["InvocationID"], "pid": pid, "start_ticks": ticks,
        "boot_id": read_bounded_text("/proc/sys/kernel/random/boot_id", max_bytes=256).strip(), "control_group": state["ControlGroup"],
        "source_sha256": snapshots, "build_network_disabled": True}
    write_json(out / "launch.json", launch)
    print(json.dumps({"unit": unit, "invocation_id": launch["invocation_id"], "output": str(out)}), flush=True)
    while state.get("ActiveState") in {"active", "activating", "deactivating"}:
        if time.monotonic() - started > 700: raise TimeoutError("Continue observing the original reserved build; do not restart it")
        time.sleep(1)
        try: state = unit_state(unit)
        except Exception: continue  # An observation failure is not a terminal job or permission to relaunch.
        if state.get("LoadState") != "not-found" and state.get("InvocationID") not in {"", launch["invocation_id"]}:
            raise ValueError("The original build invocation was replaced")
    removed = not Path("/proc", str(pid)).exists() and not Path("/sys/fs/cgroup" + launch["control_group"]).exists()
    unchanged = snapshots == {p.name: file_hash(p, 2 * 1024 ** 2) for p in sorted((ROOT / "scripts").glob("*.py"))}
    audit = {"original_process_and_cgroup_removed": removed, "builder_sources_unchanged": unchanged,
        "build_network_disabled": True, "duration_seconds": round(time.monotonic() - started, 3), "accepted_build": False,
        "runtime_verified": False, "upstream_supported": False, "full_stack_verified": False}
    try:
        receipt = loads_strict_json(read_bounded_bytes(out / "worker-result.json", max_bytes=65536))
        if receipt.get("worker_invocation_id") != launch["invocation_id"] or not removed or not unchanged:
            raise ValueError("Original build or source cleanup evidence incomplete")
        receipt.update(original_process_and_cgroup_removed=True, build_network_disabled=True, duration_seconds=audit["duration_seconds"])
        policy.validate_receipt(receipt)
        for name, expected in policy.BINARIES.items(): require_hash(out / name, expected, 96 * 1024 ** 2)
        write_json(out / "build-inputs.json", receipt); audit["accepted_build"] = True
    finally: write_json(out / "build-audit.json", audit)
    return audit


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "_worker":
        if len(args) != 3: raise ValueError("Internal worker arguments changed")
        worker(Path(args[1]), args[2]); return 0
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-archive", "go-archive", "swagger", "module-cache", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    options = parser.parse_args(args)
    if sys.platform != "linux" or platform.machine() != "x86_64" or not Path("/run/systemd/system").is_dir():
        raise ValueError("Use a Linux amd64 host with systemd supervision")
    spec = {key: str(value.absolute()) for key, value in vars(options).items() if key != "output"}
    checked_inputs(spec)  # Refuse drift before reserving or writing build output.
    out = private_output(options.output, create=True)
    print(json.dumps(supervise(spec, out)), flush=True)
    return 0


if __name__ == "__main__":
    try: raise SystemExit(main())
    except Exception:
        print("Local Harbor build was not accepted; retain its private reservation and diagnostics.", file=sys.stderr)
        raise SystemExit(1)
