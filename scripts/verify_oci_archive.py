"""Verify the reviewed Linux amd64 image and content in an OCI tar archive."""

import hashlib
import os
import re
import stat
import tarfile

from strict_json import loads_strict_json


def verify_archive(path, image):
    name, separator, original = image.rpartition("@")
    if not name or not separator or not re.fullmatch(r"sha256:[0-9a-f]{64}", original):
        raise ValueError("Use a fully qualified image digest")
    canonical = name.rsplit(":", 1)[0] if ":" in name.rsplit("/", 1)[-1] else name
    canonical += "@" + original
    if path.is_symlink():
        raise ValueError("Use a regular OCI archive")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 1024 ** 3:
            raise ValueError("Use a bounded OCI archive")
        with tarfile.open(fileobj=stream, mode="r:") as archive:
            verify_graph(archive, canonical, original)
        stream.seek(0)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        updated = os.fstat(stream.fileno())
        if (updated.st_size, updated.st_mtime_ns, updated.st_ctime_ns) != (info.st_size, info.st_mtime_ns, info.st_ctime_ns):
            raise ValueError("OCI archive changed during verification")
        return digest


def verify_graph(archive, canonical, original):
    members = []
    for member in archive:
        members.append(member)
        if len(members) > 80:
            raise ValueError("Invalid OCI archive inventory")
    names = {m.name: m for m in members}
    if len(members) != len(names) or len(names) < 3:
        raise ValueError("Invalid OCI archive inventory")
    for member in members:
        if (not member.isfile() or not 0 < member.size <= 512 * 1024 ** 2
                or not (member.name in {"oci-layout", "index.json"} or re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", member.name))):
            raise ValueError("Unexpected OCI archive member")
        if member.name.startswith("blobs/"):
            with archive.extractfile(member) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != member.name.rsplit("/", 1)[1]:
                    raise ValueError("OCI blob changed")

    def document(name):
        member = names.get(name)
        if member is None or member.size > 512 * 1024:
            raise ValueError("Missing or oversize OCI metadata")
        with archive.extractfile(member) as stream:
            return loads_strict_json(stream.read(512 * 1024 + 1))

    if document("oci-layout") != {"imageLayoutVersion": "1.0.0"}:
        raise ValueError("Unsupported OCI layout")
    index = document("index.json")
    if index.get("schemaVersion") != 2 or len(index.get("manifests", [])) != 1:
        raise ValueError("Unexpected OCI root index")
    top = index["manifests"][0]
    if (top.get("digest") != original
            or top.get("annotations") != {"io.containerd.image.name": canonical, "org.opencontainers.image.ref.name": canonical}):
        raise ValueError("OCI archive selects another image")
    selected = set()

    def descriptor(value):
        digest = value.get("digest")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("Invalid OCI descriptor")
        name = "blobs/sha256/" + digest[7:]
        member = names.get(name)
        if member is None or member.size != value.get("size"):
            raise ValueError("Missing original OCI content")
        selected.add(name)
        return name

    root = document(descriptor(top))
    if root.get("mediaType") != top.get("mediaType"):
        raise ValueError("OCI media type changed")
    if "manifests" in root:
        choices = [item for item in root["manifests"] if item.get("platform", {}).get("os") == "linux"
                   and item["platform"].get("architecture") == "amd64"]
        if len(choices) != 1:
            raise ValueError("Ambiguous OCI Linux platform")
        root = document(descriptor(choices[0]))
    config = document(descriptor(root["config"]))
    if config.get("architecture") != "amd64" or config.get("os") != "linux":
        raise ValueError("Unexpected image platform")
    for layer in root["layers"]:
        descriptor(layer)
    if names.keys() != {"oci-layout", "index.json", *selected}:
        raise ValueError("Unreferenced OCI content in archive")
