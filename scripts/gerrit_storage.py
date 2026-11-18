#!/usr/bin/env python3
"""Read-only startup guard for a reviewed CephFS-backed Gerrit shared directory."""

import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import uuid

from strict_json import loads_strict_json


def validate(scope):
    if (not isinstance(scope, dict) or set(scope) != {"schema_version", "fsid", "filesystem", "path"}
            or type(scope["schema_version"]) is not int or scope["schema_version"] != 1 or not isinstance(scope["fsid"], str)
            or str(uuid.UUID(scope["fsid"])) != scope["fsid"] or not isinstance(scope["filesystem"], str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{1,47}", scope["filesystem"]) or not safe_path(scope["path"])): raise ValueError("Use the reviewed CephFS identity and subtree")


def safe_path(value, *, root=False):
    return isinstance(value, str) and len(value) <= 512 and ((root and value == "/") or bool(re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", value)) and not any(p in {".", ".."} for p in value.split("/")))


def read_bootstrap_bytes(path, max_bytes, *, follow_symlinks=True):
    """Self-contained primitive mirrored by the pre-import SSH staging agent."""
    import os
    import stat
    if type(max_bytes) is not int or not 0 < max_bytes <= 512 * 1024 * 1024:
        raise ValueError("Invalid bootstrap file limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    if not follow_symlinks: flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ValueError("Invalid or excessive bootstrap file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            value = stream.read(max_bytes + 1)
        if len(value) > max_bytes:
            raise ValueError("Bootstrap file grew beyond its limit")
        return value
    finally:
        os.close(descriptor)


def read(path, limit):
    return read_bootstrap_bytes(path, limit)


def load(path):
    scope = loads_strict_json(read(path, 16384))
    validate(scope)
    return scope


def options(value):
    result = {}
    for item in value.split(","):
        key, _, content = item.partition("=")
        if not key or key in result: raise ValueError("Ambiguous mount options")
        result[key] = content
    return result


def inspect(scope, shared, raw):
    validate(scope)
    if not safe_path(shared): raise ValueError("Use an explicit direct shared mount path")
    matching = []
    for line in raw.decode().splitlines():
        before, separator, after = line.partition(" - "); fields, tail = before.split(), after.split()
        if not separator or len(fields) < 6 or len(tail) != 3: raise ValueError("Invalid mount inventory")
        if fields[4].startswith(shared + "/"): raise ValueError("Nested mounts cannot replace shared Git paths")
        if fields[4] != shared: continue
        mount, superblock = options(fields[5]), options(tail[2])
        if (not fields[0].isdecimal() or not re.fullmatch(r"[0-9]+:[0-9]+", fields[2]) or tail[0] != "ceph" or "rw" not in mount or "ro" in mount or "ro" in superblock
                or superblock.get("ms_mode") != "secure" or superblock.get("recover_session") == "clean" or not safe_path(fields[3], root=True)): raise ValueError("Require writable encrypted kernel CephFS")
        source = tail[1]
        if "@" in source:
            actor, _, rest = source.partition("@"); device, separator, subtree = rest.partition("=")
            if (not actor or not re.fullmatch(r"[A-Za-z0-9_.-]+", actor) or not separator or device != scope["fsid"] + "." + scope["filesystem"]): raise ValueError("The CephFS device identity differs")
            if "fsid" in superblock and superblock["fsid"] != scope["fsid"]: raise ValueError("Conflicting Ceph cluster identity")
        else:
            # CSI kernel clients may use monitor-address:/subtree syntax.
            separator = source.rfind(":/")
            if (separator < 1 or superblock.get("fsid") != scope["fsid"]
                    or superblock.get("mds_namespace", superblock.get("fs")) != scope["filesystem"]): raise ValueError("The CSI CephFS identity is not explicit")
            subtree = source[separator + 1:]
        if any(superblock[k] != scope["filesystem"] for k in ("fs", "mds_namespace") if k in superblock): raise ValueError("Conflicting Ceph filesystem identity")
        if not safe_path(subtree, root=True): raise ValueError("Invalid CephFS source path")
        effective = "/" + "/".join(v.strip("/") for v in (subtree, fields[3]) if v != "/")
        if effective != scope["path"]: raise ValueError("The mounted CephFS subtree differs")
        matching.append({"mount_id": int(fields[0]), "device": fields[2], "mount_sha256": hashlib.sha256(line.encode()).hexdigest()})
    if len(matching) != 1: raise ValueError("Require one exact shared CephFS mount, without stacked mounts")
    return matching[0]


def check(scope, shared, *, inventory=Path("/proc/self/mountinfo")):
    if sys.platform != "linux": raise ValueError("The shared-storage guard requires Linux")
    return inspect(scope, str(shared), read(inventory, 1024 ** 2))


def configuration_bytes(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 4 * 1024 ** 2:
            raise ValueError("Require bounded regular Gerrit configuration files")
        value = stream.read(4 * 1024 ** 2 + 1)
    if len(value) > 4 * 1024 ** 2: raise ValueError("Configuration grew beyond its limit")
    return value


def config_query(program, content, arguments):
    # -I deliberately excludes the script directory from sys.path. Load only
    # this adjacent, package-locked first-party helper, not a module from cwd.
    specification = importlib.util.spec_from_file_location("_gerrit_bounded", Path(__file__).with_name("bounded_subprocess.py"))
    module = importlib.util.module_from_spec(specification); specification.loader.exec_module(module)
    environment = {"PATH": os.defpath, "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1"}
    if os.name == "nt" and "SYSTEMROOT" in os.environ: environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    result = module.run_bounded([program, "config", "--no-includes", "--file", "-", "--null", *arguments],
                                input=content, env=environment, timeout=15, output_max_bytes=65536, check=False)
    if result.returncode not in (0, 1): raise ValueError("Gerrit configuration could not be parsed")
    if result.returncode == 1:
        if result.stdout: raise ValueError("Ambiguous Git configuration response")
        return []
    if not result.stdout: return []
    if not result.stdout.endswith(b"\0"): raise ValueError("Incomplete Git configuration response")
    return [item.decode("utf-8") for item in result.stdout[:-1].split(b"\0")]


def check_configuration(site, shared):
    """Bind repository/HA paths to the checked mount; not a full config audit."""
    site, shared = Path(site), Path(shared)
    if site.resolve(strict=True) != site.absolute() or shared.resolve(strict=True) != shared.absolute():
        raise ValueError("Symlinked Gerrit site/shared ancestors are not accepted")
    git_root = shared / "git"
    if git_root.resolve() != git_root.absolute(): raise ValueError("The shared Git directory must not redirect elsewhere")
    program = shutil.which("git")
    if not program: raise ValueError("The reviewed runtime must contain Git's configuration parser")
    paths = {}
    for name in ("gerrit.config", "high-availability.config", "secure.config"):
        path = site / "etc" / name
        if path.resolve(strict=True) != path.absolute(): raise ValueError("Configuration paths must not redirect elsewhere")
        paths[name] = configuration_bytes(path)
    for name, content in paths.items():
        keys = config_query(program, content, ["--name-only", "--list"])
        if any(key.casefold().startswith(("include.", "includeif.")) for key in keys):
            raise ValueError("Configuration includes cannot redirect the guarded storage paths")
        if name == "secure.config" and any(key.casefold().startswith("gerrit.") for key in keys):
            raise ValueError("Private configuration must not override the guarded Git target")
    if config_query(program, paths["gerrit.config"], ["--get-all", "gerrit.basePath"]) != [str(git_root)]:
        raise ValueError("Gerrit basePath must be the guarded shared Git directory")
    if config_query(program, paths["high-availability.config"], ["--get-all", "main.sharedDirectory"]) != [str(shared)]:
        raise ValueError("The HA plugin sharedDirectory must be the guarded shared mount")


REQUIRED_HEALTHCHECKS = ("querychanges", "jgit", "projectslist", "auth", "activeworkers",
    "httpactiveworkers", "deadlock", "blockedthreads", "changesindex", "gitspace")


def check_health_configuration(site):
    """Require explicit enabled probes, not a successful empty health endpoint."""
    site = Path(site)
    path = site / "etc/healthcheck.config"
    if path.resolve(strict=True) != path.absolute(): raise ValueError("Health configuration must not redirect elsewhere")
    content = configuration_bytes(path)
    program = shutil.which("git")
    if not program: raise ValueError("Health configuration requires Git's configuration parser")
    entries = {}
    for item in config_query(program, content, ["--list"]):
        key, separator, value = item.partition("\n")
        entries.setdefault(key, []).append(value if separator else None)
    if any(key.casefold().startswith(("include.", "includeif.")) for key in entries):
        raise ValueError("Health configuration includes are not accepted")
    def values(key):
        # Git canonicalizes variable names, preserving subsection case.
        prefix, _, variable = key.rpartition(".")
        return entries.get(prefix + "." + variable.lower(), [])
    def finite_timeout(items):
        return (len(items) == 1 and isinstance(items[0], str) and re.fullmatch(r"[0-9]{1,4}", items[0])
            and 1 <= int(items[0]) <= 5000)
    defaults = values("healthcheck.timeout") or ["500"]
    if not finite_timeout(defaults): raise ValueError("Require a finite bounded Gerrit health probe timeout")
    for name in REQUIRED_HEALTHCHECKS:
        enabled = values("healthcheck." + name + ".enabled")
        if len(enabled) != 1 or not isinstance(enabled[0], str) or enabled[0].casefold() not in {"true", "yes", "on", "1"}:
            raise ValueError("Every required Gerrit health probe must be explicitly enabled once")
        timeouts = values("healthcheck." + name + ".timeout") or defaults
        if not finite_timeout(timeouts):
            raise ValueError("Require a finite bounded Gerrit health probe timeout")
    for key in ("username", "password"):
        credentials = values("healthcheck.auth." + key)
        if len(credentials) != 1 or not isinstance(credentials[0], str) or not credentials[0].strip():
            raise ValueError("Provision the private health authentication check credentials")
    free = values("healthcheck.gitspace.minFreeDiskPercent") or ["10"]
    if len(free) != 1 or not isinstance(free[0], str) or not re.fullmatch(r"[0-9]{1,2}", free[0]) or not 1 <= int(free[0]) <= 99:
        raise ValueError("Require a positive Gerrit Git storage free-space threshold")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=Path, required=True); parser.add_argument("--shared-dir", required=True)
    parser.add_argument("--site-dir", type=Path, required=True)
    parser.add_argument("--require-healthcheck", action="store_true")
    parser.add_argument("--gerrit-version")
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 12): raise ValueError("Use reviewed Python 3.12 or newer")
        check(load(args.scope), args.shared_dir)
        check_configuration(args.site_dir, args.shared_dir)
        if args.require_healthcheck != (args.gerrit_version is not None): raise ValueError("Guarded health startup requires the reviewed stable release")
        if args.require_healthcheck:
            check_health_configuration(args.site_dir)
            specification = importlib.util.spec_from_file_location("_gerrit_artifacts", Path(__file__).with_name("gerrit_artifacts.py"))
            module = importlib.util.module_from_spec(specification); specification.loader.exec_module(module)
            module.verify(args.site_dir, args.gerrit_version)
        print('{"shared_storage_verified":true,"storage_configuration_verified":true,"ha_verified":false}'); return 0
    except Exception:
        print("Shared Gerrit storage refused; Java was not started by this guard.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
