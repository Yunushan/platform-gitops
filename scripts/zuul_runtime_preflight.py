#!/usr/bin/env python3
"""Read-only local Zuul input checks; never connect to services or launch workers."""

from __future__ import annotations

import argparse
import configparser
import json
import ipaddress
import os
from pathlib import Path
import stat
import re
import sys
from urllib.parse import parse_qs, urlsplit


ROLES = ("scheduler", "merger", "web", "executor", "launcher")
ENVIRONMENT = ("ZUUL_DATABASE_URI", "ZUUL_KEYSTORE_PASSWORD", "ZUUL_GERRIT_HTTP_PASSWORD")
PRIVATE_FILES = ("zk-client.pem", "zk-client.key", "zk-ca.pem", "postgres-ca.pem",
                 "gerrit-ca.pem", "gerrit-ssh.key", "known_hosts")


class ZuulPreflightError(ValueError):
    pass


def verify_inputs(environment: dict[str, str], role: str, *, config_root: Path = Path("/etc/zuul")) -> dict:
    if role not in ROLES:
        raise ZuulPreflightError("Unknown Zuul role")
    if any(not isinstance(environment.get(name), str) or not environment[name] or len(environment[name]) > 16384 for name in ENVIRONMENT):
        raise ZuulPreflightError("Required private environment values are missing or invalid")
    try:
        uri = urlsplit(environment["ZUUL_DATABASE_URI"])
        query = parse_qs(uri.query, strict_parsing=True, keep_blank_values=True)
        valid = (uri.scheme == "postgresql" and uri.hostname and uri.path not in {"", "/"}
                 and not uri.fragment and uri.port != 0 and query.get("sslmode") == ["verify-full"]
                 and query.get("sslrootcert") == [str(config_root / "private/postgres-ca.pem")])
    except ValueError:
        valid = False
    if not valid:
        raise ZuulPreflightError("Database URI must select PostgreSQL with explicit CA and full TLS verification")
    files = [config_root / name for name in ("zuul.conf", "tenants.yaml")]
    files += [config_root / "private" / name for name in PRIVATE_FILES]
    if role == "executor":
        files.append(config_root / "private/build-ssh.key")
        advertised = environment.get("ZUUL_EXECUTOR_HOSTNAME", "")
        if not isinstance(advertised, str) or not 1 <= len(advertised) <= 253:
            raise ZuulPreflightError("Provision a bounded executor log-stream address")
        try:
            valid_address = bool(ipaddress.ip_address(advertised))
        except ValueError:
            valid_address = (isinstance(advertised, str) and len(advertised) <= 253 and
                             bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*", advertised)))
        if not valid_address:
            raise ZuulPreflightError("Provision an explicit executor log-stream address reachable from web services")
    # Kubernetes projected Secret paths are intentionally allowed. Only metadata
    # is inspected: no certificate, private key or password value is printed.
    for path in files:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 4 * 1024 ** 2:
            raise ZuulPreflightError("A required configuration/key file is absent or not a bounded regular file")
    if role == "launcher":
        config = configparser.RawConfigParser()
        try:
            config.read(config_root / "zuul.conf", encoding="utf-8")
            if config.get('connection "platform-static"', 'driver') != 'static':
                raise ZuulPreflightError("Integrated launcher requires the reviewed static connection")
        except configparser.Error as exc:
            raise ZuulPreflightError("Integrated launcher configuration is missing or invalid") from exc
    if environment.get("REQUESTS_CA_BUNDLE") != str(config_root / "private/gerrit-ca.pem"):
        raise ZuulPreflightError("Select the approved CA bundle for Gerrit HTTP requests")
    return {"schema_version": 1, "read_only": True, "role": role, "local_inputs_verified": True,
            "connections_verified": False, "sandbox_verified": False, "ci_verified": False, "ha_verified": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=ROLES, required=True)
    args = parser.parse_args(argv)
    try:
        report = verify_inputs(dict(os.environ), args.role)
    except (ValueError, OSError):
        print("Zuul local input check failed; review private environment, TLS and key-file provisioning.", file=sys.stderr)
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
