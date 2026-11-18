#!/usr/bin/env python3
"""Render a Linux host collector for selected platform logs and a Loki gateway."""

import argparse
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from bounded_file import read_bounded_text
from render_gerrit_router import dns_name, write_bundle
from render_gerrit_sites import label
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
CONFIG = "/etc/platform-log-collector"
STATE = "/var/lib/platform-log-collector"
UNIT_NAME = "platform-log-collector.service"
SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
SYSTEMD_UNIT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@-]{0,117}\.service")
LOG_PATH = re.compile(r"/(?:var/log/|var/lib/|srv/)(?:[A-Za-z0-9_][A-Za-z0-9_.-]*/)*[A-Za-z0-9_][A-Za-z0-9_.-]*")
RECEIVER = "loki.write.platform.receiver"


def validate_gateway(plan):
    gateway = plan["gateway"]
    if (not isinstance(gateway, str) or len(gateway) > 320
            or not re.fullmatch(r"https://[a-z0-9.-]+(?::[0-9]{1,5})?/loki/api/v1/push", gateway)):
        raise ValueError("Use the credential-free HTTPS Loki push URL")
    url = urlsplit(gateway)
    if not dns_name(url.hostname) or url.port == 0:
        raise ValueError("Use an explicit Loki gateway hostname and optional port")
    if not isinstance(plan["username"], str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", plan["username"]):
        raise ValueError("Declare a Loki gateway username and supply its password separately")


def validate(plan, runtime):
    fields = {"schema_version", "cluster", "node", "gateway", "tenant", "username", "services", "files"}
    if (runtime not in {"docker", "raw"} or not isinstance(plan, dict) or set(plan) != fields
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or any(not label(plan[key]) for key in ("cluster", "node", "tenant"))):
        raise ValueError("Declare a supported runtime and explicit cluster, node, and tenant identities")
    validate_gateway(plan)

    services, files = plan["services"], plan["files"]
    if (not isinstance(services, dict) or not isinstance(files, dict)
            or not 1 <= len(services) + len(files) <= 64):
        raise ValueError("Select between one and 64 service or file sources")
    selector = SERVICE if runtime == "docker" else SYSTEMD_UNIT
    for app, value in services.items():
        if not label(app) or not isinstance(value, str) or not selector.fullmatch(value):
            raise ValueError("Select exact Compose services or systemd service units")
    for app, value in files.items():
        if not label(app) or not isinstance(value, str) or len(value) > 240 or not LOG_PATH.fullmatch(value):
            raise ValueError("Select exact application log files under /var/log, /var/lib, or /srv")
    if len(set(services.values())) != len(services) or len(set(files.values())) != len(files):
        raise ValueError("Select each log source once")
    if runtime == "raw" and UNIT_NAME in services.values():
        raise ValueError("The collector cannot collect its own service journal")


def label_map(values):
    return "{ " + ", ".join(f"{key} = {json.dumps(value)}" for key, value in values.items()) + " }"


def render(plan, runtime):
    validate(plan, runtime)
    services, files = plan["services"], plan["files"]
    # Loki labels identify the platform workload without forwarding arbitrary journal fields.
    common = {"cluster": plan["cluster"], "node": plan["node"], "runtime": runtime}
    blocks = []
    if runtime == "docker" and services:
        blocks.append('''loki.relabel "docker_records" {
  forward_to = []
  rule {
    source_labels = ["__journal_container_id_full"]
    regex = "[0-9a-f]{64}"
    action = "keep"
  }
}
''')
    compose = ["services:"]
    for app, service in sorted(services.items()):
        docker = runtime == "docker"
        tag = f"platform.{plan['cluster']}.{app}"
        match = f"CONTAINER_TAG={tag}" if docker else f"_SYSTEMD_UNIT={service}"
        labels = label_map({**common, "app": app, "source": "journal"})
        extra = '  relabel_rules = loki.relabel.docker_records.rules\n' if docker else ""
        blocks.append(f'''loki.source.journal "{app}" {{
  matches = {json.dumps(match)}
  max_age = "1h"
  labels = {labels}
{extra}  forward_to = [{RECEIVER}]
}}
''')
        if docker:
            # Replace options for the previous driver together with its name.
            compose.extend((f"  {json.dumps(service)}:", "    logging: !override",
                            "      driver: journald", "      options:", f"        tag: {json.dumps(tag)}"))

    if files:
        targets = [
            label_map({"__path__": path, **common, "app": app, "source": "file"})
            for app, path in sorted(files.items())
        ]
        blocks.append('''loki.source.file "applications" {
  targets = [
    ''' + ",\n    ".join(targets) + ''',
  ]
  tail_from_end = true
  forward_to = [loki.relabel.file_records.receiver]
}

loki.relabel "file_records" {
  forward_to = [''' + RECEIVER + ''']
  rule {
    action = "labeldrop"
    regex = "filename"
  }
}
''')

    blocks.append(render_loki_writer(plan, 'sys.env("CREDENTIALS_DIRECTORY")'))
    journal_group = "SupplementaryGroups=systemd-journal\n" if services else ""
    bundle = {
        "config.alloy": "\n".join(blocks),
        UNIT_NAME: f'''[Unit]
Description=Collect platform logs for Loki
Wants=network-online.target
After=network-online.target

[Service]
User=platform-alloy
Group=platform-alloy
{journal_group}UMask=0077
NoNewPrivileges=yes
CapabilityBoundingSet=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
StateDirectory=platform-log-collector
StateDirectoryMode=0700
LoadCredential=loki-password:{CONFIG}/loki-password
LoadCredential=loki-ca.pem:{CONFIG}/loki-ca.pem
ExecStart=/usr/bin/alloy run --disable-reporting --server.http.listen-addr=127.0.0.1:12346 --server.http.enable-pprof=false --server.http.disable-support-bundle --storage.path={STATE} {CONFIG}/config.alloy
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
''',
    }
    if runtime == "docker" and services:
        bundle["compose.logging.yaml"] = "\n".join(compose) + "\n"
    return bundle


def render_loki_writer(plan, credential_directory):
    return f'''loki.write "platform" {{
  endpoint {{
    url = {json.dumps(plan["gateway"])}
    tenant_id = {json.dumps(plan["tenant"])}
    follow_redirects = false
    max_backoff_period = "30s"
    basic_auth {{
      username = {json.dumps(plan["username"])}
      password_file = {credential_directory} + "/loki-password"
    }}
    tls_config {{
      ca_file = {credential_directory} + "/loki-ca.pem"
      min_version = "TLS12"
    }}
  }}
}}
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=("raw", "docker"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, max_bytes=65536))
        write_bundle(render(plan, args.runtime), args.output_dir, private_root=ROOT / "private")
    except (ValueError, OSError):
        print("Host log collector render failed. Review the private plan and output path.", file=sys.stderr)
        return 1
    print(json.dumps({"rendered": True, "deployed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
