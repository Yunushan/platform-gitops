#!/usr/bin/env python3
"""Render independent HTTPS endpoint probing and alert delivery."""

import argparse
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import IMAGE, dns_name, write_bundle
from render_gerrit_sites import label
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
CONFIG = "/etc/platform-monitoring"
COMPONENTS = ("prometheus", "blackbox", "alertmanager")
JOB = "platform-https"


def validate(plan, runtime):
    fields = {"schema_version", "monitor", "targets"}
    if runtime == "docker":
        fields.add("images")
    if (runtime not in {"raw", "docker"} or not isinstance(plan, dict) or set(plan) != fields
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or not label(plan["monitor"])):
        raise ValueError("Use an explicit external monitor name and runtime")
    if runtime == "docker":
        images = plan["images"]
        if (not isinstance(images, dict) or set(images) != set(COMPONENTS)
                or any(not isinstance(image, str) or not IMAGE.fullmatch(image) for image in images.values())):
            raise ValueError("Pin all three monitoring images by digest")
    targets = plan["targets"]
    if not isinstance(targets, list) or not 1 <= len(targets) <= 64:
        raise ValueError("Select between one and 64 platform endpoints")
    names, urls = set(), set()
    for target in targets:
        if (not isinstance(target, dict) or set(target) != {"name", "url", "status", "owner"}
                or not label(target["name"]) or not label(target["owner"])
                or type(target["status"]) is not int or target["status"] not in {200, 204, 401, 403}
                or not isinstance(target["url"], str) or len(target["url"]) > 512
                or not re.fullmatch(r"https://[a-z0-9.:-]+/[A-Za-z0-9/_~.-]*", target["url"])):
            raise ValueError("Declare credential-free HTTPS targets with one expected status and owner")
        url = urlsplit(target["url"])
        if not dns_name(url.hostname) or url.port == 0 or url.username is not None or url.password is not None:
            raise ValueError("Use an explicit HTTPS hostname and optional port")
        if target["name"] in names or target["url"] in urls:
            raise ValueError("Declare each endpoint once")
        names.add(target["name"])
        urls.add(target["url"])


def rules(targets):
    signal = f'probe_success{{job="{JOB}"}}'
    return {"groups": [{"name": JOB, "rules": [
        {"alert": "PlatformEndpointUnavailable", "expr": signal + " == 0", "for": "30s",
         "labels": {"severity": "critical"},
         "annotations": {"summary": "{{ $labels.endpoint }} failed its HTTPS check",
                         "description": "Check DNS, TLS, ingress and the endpoint response from the monitoring host."}},
        *[
            {"alert": "PlatformEndpointProbeMissing",
             "expr": f'absent(probe_success{{job="{JOB}",endpoint="{target["name"]}"}})', "for": "30s",
             "labels": {"severity": "warning", "owner": target["owner"]},
             "annotations": {"summary": target["name"] + " has no HTTPS probe result",
                             "description": "Check the independent Blackbox Exporter and its scrape configuration."}}
            for target in targets
        ],
    ]}]}


def render(plan, runtime):
    validate(plan, runtime)
    targets = plan["targets"]
    directory = CONFIG
    blackbox = "blackbox:9115" if runtime == "docker" else "127.0.0.1:9115"
    alertmanager = "alertmanager:9093" if runtime == "docker" else "127.0.0.1:9093"
    configs = {
        "blackbox.yaml": {"modules": {
            f"http_{code}": {"prober": "http", "timeout": "4s", "http": {
                "method": "GET", "valid_status_codes": [code], "follow_redirects": False,
                "fail_if_not_ssl": True, "tls_config": {"ca_file": directory + "/probe-ca.pem", "min_version": "TLS12"},
            }}
            for code in sorted({target["status"] for target in targets})
        }},
        "prometheus.yaml": {
            "global": {"scrape_interval": "10s", "scrape_timeout": "5s", "evaluation_interval": "5s",
                       "external_labels": {"monitor": plan["monitor"]}},
            "rule_files": [directory + "/alerts.yaml"],
            "alerting": {"alertmanagers": [{"static_configs": [{"targets": [alertmanager]}]}]},
            "scrape_configs": [{
                "job_name": JOB, "metrics_path": "/probe",
                "static_configs": [{"targets": [blackbox], "labels": {
                    "__param_target": target["url"], "__param_module": f'http_{target["status"]}',
                    "endpoint": target["name"], "instance": target["name"], "owner": target["owner"],
                }} for target in targets],
            }],
        },
        "alerts.yaml": rules(targets),
        "alertmanager.yaml": {
            "route": {"receiver": "platform", "group_by": ["monitor", "alertname", "endpoint"],
                      "group_wait": "5s", "group_interval": "15s", "repeat_interval": "4h"},
            "receivers": [{"name": "platform", "webhook_configs": [{
                "url_file": directory + "/webhook-url", "send_resolved": True,
                "http_config": {"follow_redirects": False,
                                "tls_config": {"ca_file": directory + "/receiver-ca.pem", "min_version": "TLS12"}},
            }]}],
        },
    }
    if runtime == "docker":
        services = {}
        for name, inputs, command in (
            ("prometheus", ("prometheus.yaml", "alerts.yaml"),
             ["--config.file=" + CONFIG + "/prometheus.yaml", "--storage.tsdb.path=/state", "--storage.tsdb.retention.time=15d"]),
            ("blackbox", ("blackbox.yaml", "probe-ca.pem"), ["--config.file=" + CONFIG + "/blackbox.yaml"]),
            ("alertmanager", ("alertmanager.yaml", "webhook-url", "receiver-ca.pem"),
             ["--config.file=" + CONFIG + "/alertmanager.yaml", "--storage.path=/state", "--cluster.listen-address="]),
        ):
            mounts = [{"type": "bind", "source": "./" + path, "target": CONFIG + "/" + path,
                       "read_only": True, "bind": {"create_host_path": False}} for path in inputs]
            if name != "blackbox":
                mounts.append({"type": "bind", "source": "./" + name + "-state", "target": "/state",
                               "bind": {"create_host_path": False}})
            services[name] = {"image": plan["images"][name], "user": "65534:65534", "init": True, "read_only": True,
                              "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "volumes": mounts,
                              "command": command, "restart": "unless-stopped"}
        configs["compose.yaml"] = {"services": services}
    return {name: yaml.safe_dump(value, sort_keys=False) for name, value in configs.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=("docker", "raw"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, max_bytes=65536))
        write_bundle(render(plan, args.runtime), args.output_dir, private_root=ROOT / "private")
    except (ValueError, OSError):
        print("External monitoring render failed. Review the private plan and output path.")
        return 1
    print(json.dumps({"rendered": True, "deployed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
