#!/usr/bin/env python3
"""Read bounded Gerrit WAR/JAR identities; never load or execute Java code."""

from contextlib import contextmanager
import io
import os
from pathlib import Path
import re
import stat
import struct
import zipfile


VERSION = re.compile(r"[1-9][0-9]*\.[0-9]+\.[0-9]+")
VERSION_JAR = "WEB-INF/lib/com_google_gerrit_common_libversion.jar"
VERSION_RESOURCE = "com/google/gerrit/common/Version"
PLUGINS = {
    "plugins/high-availability.jar": ("high-availability", "com.ericsson.gerrit.plugins.highavailability.Module"),
    "plugins/healthcheck.jar": ("healthcheck", "com.googlesource.gerrit.plugins.healthcheck.Module"),
    "lib/global-refdb.jar": (None, "com.gerritforge.gerrit.globalrefdb.GlobalRefDatabase"),
}


class ArtifactError(ValueError):
    pass


@contextmanager
def archive(source):
    """Bound ZIP metadata before ZipFile allocates its central directory."""
    stream = None
    try:
        if isinstance(source, bytes):
            if len(source) > 1024 * 1024: raise ArtifactError("Nested Gerrit version archive is excessive")
            stream = io.BytesIO(source); size = len(source)
        else:
            descriptor = os.open(Path(source), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
            stream = os.fdopen(descriptor, "rb")
            metadata = os.fstat(stream.fileno()); size = metadata.st_size
            if not stat.S_ISREG(metadata.st_mode) or size > 1024 ** 3:
                raise ArtifactError("Require bounded regular Gerrit artifacts")
        if size < 22: raise ArtifactError("Gerrit artifact is not a ZIP archive")
        stream.seek(max(0, size - 65557)); tail = stream.read(65557)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or len(tail) - offset < 22: raise ArtifactError("Missing Gerrit archive directory")
        if offset >= 20 and tail[offset - 20:offset - 16] == b"PK\x06\x07":
            raise ArtifactError("ZIP64 Gerrit artifacts require a separately reviewed bounded layout")
        _, disk, directory_disk, disk_count, count, directory_size, directory_offset, comment = struct.unpack("<4s4H2LH", tail[offset:offset + 22])
        if (disk or directory_disk or disk_count != count or not 1 <= count <= 8192 or directory_size > 8 * 1024 ** 2
                or directory_offset + directory_size > size - 22 or len(tail) - offset != 22 + comment):
            raise ArtifactError("Unsupported or excessive Gerrit archive directory")
        stream.seek(0)
        with zipfile.ZipFile(stream) as selected:
            names = selected.namelist()
            if len(names) != count or len(set(names)) != count: raise ArtifactError("Ambiguous Gerrit archive entries")
            yield selected
    except (OSError, zipfile.BadZipFile, struct.error, UnicodeError, RuntimeError) as error:
        raise ArtifactError("Gerrit artifact metadata could not be verified") from error
    finally:
        if stream is not None: stream.close()


def member(selected, name, maximum):
    try: info = selected.getinfo(name)
    except KeyError: raise ArtifactError("A required Gerrit artifact identity is missing") from None
    if info.is_dir() or not 0 < info.file_size <= maximum or info.flag_bits & 1:
        raise ArtifactError("A Gerrit identity entry is invalid or excessive")
    # ZipFile.read uses the ZipInfo's checked uncompressed size to bound its entry.
    value = selected.read(info)
    if len(value) != info.file_size or len(value) > maximum: raise ArtifactError("A Gerrit identity entry changed")
    return value


def manifest(selected):
    raw = member(selected, "META-INF/MANIFEST.MF", 65536).decode("utf-8")
    text = raw.replace("\r\n", "\n")
    if "\r" in text or "\0" in text: raise ArtifactError("Invalid Gerrit manifest encoding")
    fields = {}; previous = None
    for line in text.split("\n"):
        if not line: break
        if line.startswith(" "):
            if previous is None: raise ArtifactError("Orphan Gerrit manifest continuation")
            fields[previous] += line[1:]; continue
        name, separator, value = line.partition(": ")
        key = name.casefold()
        if not separator or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,69}", name) or key in fields:
            raise ArtifactError("Ambiguous Gerrit manifest fields")
        fields[key] = value; previous = key
    if fields.get("manifest-version") != "1.0": raise ArtifactError("Unsupported Gerrit manifest")
    return fields


def version(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value): raise ArtifactError("Require an exact stable Gerrit release")


def inspect_war(path, expected):
    version(expected)
    with archive(path) as war:
        if manifest(war).get("main-class") != "Main": raise ArtifactError("Unexpected Gerrit WAR entry point")
        nested = member(war, VERSION_JAR, 1024 * 1024)
    with archive(nested) as selected: declared = member(selected, VERSION_RESOURCE, 128).decode("ascii")
    if declared not in {expected, "v" + expected, expected + "\n", "v" + expected + "\n"}:
        raise ArtifactError("Gerrit WAR version differs from the reviewed stable release")


def inspect_plugin(path, expected, identity, entry_class):
    version(expected)
    with archive(path) as selected:
        fields = manifest(selected)
        if fields.get("gerrit-apitype") != "plugin" or fields.get("gerrit-apiversion") != expected:
            raise ArtifactError("Gerrit artifact API must match the exact stable server release")
        if identity is not None and (fields.get("gerrit-pluginname") != identity or fields.get("gerrit-module") != entry_class):
            raise ArtifactError("Gerrit plugin identity differs from its selected role")
        if not member(selected, entry_class.replace(".", "/") + ".class", 1024 * 1024).startswith(b"\xca\xfe\xba\xbe"):
            raise ArtifactError("Gerrit artifact entry class is invalid")


def verify(site, expected):
    site = Path(site); inspect_war(site / "bin/gerrit.war", expected)
    for relative, (identity, entry) in PLUGINS.items(): inspect_plugin(site / relative, expected, identity, entry)
    return {"artifact_metadata_verified": True, "live_plugin_compatibility_verified": False,
        "publisher_signature_verified": False, "ha_verified": False}
