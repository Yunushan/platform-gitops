#!/usr/bin/env python3
"""Prepare Allura 1.22.1's core configuration for a fresh Solr 9.11 index."""

import argparse
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import xml.etree.ElementTree as ET

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_SHA512 = (
    "2d427dad0c2f99af5175998624e853a4db8faaabad61fac401b61a567a80807427f9"
    "b18aa94e76793b6bd52274f2cecbd2ef9947aaf6819f9492881feef2d4f4"
)
CORE_PATH = "allura-1.22.1/solr_config/allura/"
CORE_FILES = (
    "core.properties", "conf/solrconfig.xml", "conf/schema.xml", "conf/stopwords.txt",
    "conf/synonyms.txt", "conf/protwords.txt", "conf/currency.xml", "conf/_rest_managed.json",
)


def prepare_core(archive):
    data = read_bounded_bytes(archive, max_bytes=16 * 1024 * 1024)
    if hashlib.sha512(data).hexdigest() != ARCHIVE_SHA512:
        raise ValueError("The archive does not match the Apache Allura 1.22.1 SHA-512")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for name in CORE_FILES:
            member = tar.getmember(CORE_PATH + name)
            if not member.isfile():
                raise ValueError(f"Invalid Solr core member {name}")
            with tar.extractfile(member) as source:
                files[name] = source.read().decode("utf-8")

    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    config = ET.fromstring(files["conf/solrconfig.xml"], parser=parser)
    config.find("luceneMatchVersion").text = "9.0.0"
    for element in ("jmx", "admin"):
        config.remove(config.find(element))
    query = config.find("query")
    query.remove(query.find("cache"))
    for name in ("filterCache", "queryResultCache", "documentCache"):
        query.find(name).set("class", "solr.CaffeineCache")
    config.find("requestDispatcher/requestParsers").attrib.pop("enableRemoteStreaming")
    files["conf/solrconfig.xml"] = ET.tostring(config, encoding="unicode")

    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    schema = ET.fromstring(files["conf/schema.xml"], parser=parser)
    location = schema.find("./types/fieldType[@name='location']")
    location.set("class", "solr.LatLonPointSpatialField")
    location.attrib.pop("subFieldSuffix")
    files["conf/schema.xml"] = ET.tostring(schema, encoding="unicode")
    files["conf/index_synonyms.txt"] = ""
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-archive", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        files = prepare_core(args.source_archive)
        output = args.output_dir.resolve()
        if not output.relative_to((ROOT / "private").resolve()).parts or args.output_dir.is_symlink():
            raise ValueError("Output must be a new directory under private/")
        output.mkdir(mode=0o755, parents=True, exist_ok=False)
        (output / "conf").mkdir(mode=0o755)
        for name, content in files.items():
            atomic_write_text(output / name, content, mode=0o644, replace=False)
    except (OSError, ValueError, RuntimeError, tarfile.TarError) as exc:
        print(f"Allura Solr preparation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
