#!/usr/bin/env python3
"""Render fresh GitLab CE repository-cluster host configuration for Linux or Docker."""
from __future__ import annotations

import argparse
from ipaddress import IPv4Address
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import yaml

import gitlab_application_entry as entry_adapter
from bounded_file import read_bounded_text
from render_gerrit_router import dns_name, write_bundle
from strict_json import loads_strict_json

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "docker.io/gitlab/gitlab-ce:19.1.1-ce.0@sha256:c1b65c81a144aaf803e3dda893d35e5a224063bf5918a578702768de401dcde2"
GITALY_PORT = 9999
PRAEFECT_PORT = 3305
RAILS_HTTPS_PORT = 8443
RAILS_SSH_PORT = 2222
SETUP = "/etc/gitlab"
OBJECT_TYPES = ("artifacts", "external_diffs", "lfs", "uploads", "packages",
                "dependency_proxy", "terraform_state", "ci_secure_files", "ci_catalog_bundles", "pages")


def interface_address(value):
    if not isinstance(value, str):
        raise ValueError("Use an explicit host interface address")
    address = IPv4Address(value)
    if address.is_unspecified or address.is_loopback or address.is_multicast or address.is_link_local or address.is_reserved:
        raise ValueError("Use an explicit host interface address")
    return str(address)


def host_address(node):
    if not isinstance(node, dict) or set(node) != {"host", "bind_ipv4"} or not dns_name(node["host"]):
        raise ValueError("Invalid role endpoint")
    return interface_address(node["bind_ipv4"])


def https_origin(value):
    if not isinstance(value, str) or not re.fullmatch(r"https://[a-z0-9.-]+(?::[0-9]{1,5})?", value):
        raise ValueError("Use an HTTPS origin without credentials or a path")
    url = urlsplit(value)
    if not dns_name(url.hostname) or url.port == 0:
        raise ValueError("Invalid HTTPS endpoint")
    return url


def validate(plan):
    fields = {"schema_version", "fresh_install", "gitaly", "praefect", "praefect_endpoint",
              "gitlab_internal_url", "postgres"}
    if not isinstance(plan, dict) or type(plan.get("schema_version")) is not int:
        raise ValueError("A complete fresh-install plan is required")
    version = plan["schema_version"]
    if version in {2, 3, 4}:
        fields.add("application_backends")
    if version in {3, 4}:
        fields.add("application_hosts")
    if version == 4:
        fields.add("application_vip")
    if set(plan) != fields or version not in {1, 2, 3, 4} or plan["fresh_install"] is not True:
        raise ValueError("A complete fresh-install plan is required")
    for role in ("gitaly", "praefect"):
        nodes = plan[role]
        if not isinstance(nodes, list) or len(nodes) != 3:
            raise ValueError("Each role needs three hosts")
        for node in nodes:
            host_address(node)
    nodes = plan["gitaly"] + plan["praefect"]
    if (len({node["bind_ipv4"] for node in nodes}) != 6
            or not dns_name(plan["praefect_endpoint"])
            or len({plan["praefect_endpoint"], *(node["host"] for node in nodes)}) != 7):
        raise ValueError("Use distinct role hosts and a separate Praefect entry point")
    url = https_origin(plan["gitlab_internal_url"])
    db = postgres_endpoint(plan["postgres"])
    if version in {2, 3, 4}:
        app = plan["application_backends"]
        if not isinstance(app, dict) or set(app) != {"postgres", "valkey"}:
            raise ValueError("Application backends require PostgreSQL and Valkey")
        app_db = postgres_endpoint(app["postgres"])
        if db["database"] == app_db["database"] or db["user"] == app_db["user"]:
            raise ValueError("Rails and Praefect need distinct database names and users")
        valkey = app["valkey"]
        if (not isinstance(valkey, dict) or set(valkey) - {"host", "port", "mutual_tls"}
                or not {"host", "port"} <= set(valkey) or type(valkey.get("mutual_tls", False)) is not bool
                or not dns_name(valkey["host"]) or type(valkey["port"]) is not int
                or not 1 <= valkey["port"] <= 65535):
            raise ValueError("Invalid Valkey endpoint")
    if version in {3, 4}:
        validate_application_hosts(plan["application_hosts"], nodes, plan["praefect_endpoint"], url)
    if version == 4:
        entry_adapter.validate(plan, url)
    return plan


def validate_application_hosts(app, repository_nodes, repository_endpoint, origin):
    if not isinstance(app, dict) or set(app) != {"nodes", "entry_ipv4", "ssh_port", "proxy_ipv4", "object_storage"}:
        raise ValueError("Configure Rails nodes, load-balancer addresses, SSH port and shared object storage")
    nodes = app["nodes"]
    proxies = app["proxy_ipv4"]
    if not isinstance(nodes, list) or len(nodes) < 2 or not isinstance(proxies, list) or len(proxies) != 2:
        raise ValueError("Configure at least two Rails nodes and two HAProxy peer addresses")
    addresses = [host_address(node) for node in repository_nodes + nodes]
    addresses += [interface_address(address) for address in [app["entry_ipv4"], *proxies]]
    names = [node["host"] for node in repository_nodes + nodes] + [repository_endpoint, origin.hostname]
    if len(set(addresses)) != len(addresses) or len(set(names)) != len(names):
        raise ValueError("Use distinct role hosts, proxy peers and entry points")
    ssh_port = app["ssh_port"]
    if (type(ssh_port) is not int or not 1 <= ssh_port <= 65535
            or ssh_port in {PRAEFECT_PORT, origin.port or 443} or origin.port == PRAEFECT_PORT):
        raise ValueError("Use distinct GitLab HTTPS, Git SSH and Praefect entry ports")
    objects = app["object_storage"]
    if not isinstance(objects, dict) or set(objects) != {"endpoint", "region", "buckets"}:
        raise ValueError("Configure the shared S3-compatible endpoint, region and buckets")
    https_origin(objects["endpoint"])
    if not isinstance(objects["region"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,48}", objects["region"]):
        raise ValueError("Invalid S3 region")
    buckets = objects["buckets"]
    if (not isinstance(buckets, dict) or set(buckets) != set(OBJECT_TYPES)
            or any(not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket)
                   for bucket in buckets.values())
            or len(set(buckets.values())) != len(buckets)):
        raise ValueError("Use a distinct S3 bucket for each GitLab object type")


def postgres_endpoint(db):
    if (not isinstance(db, dict) or set(db) != {"host", "port", "database", "user"}
            or not dns_name(db["host"]) or type(db["port"]) is not int or not 1 <= db["port"] <= 65535):
        raise ValueError("Invalid PostgreSQL endpoint")
    for key in ("database", "user"):
        if not isinstance(db[key], str) or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", db[key]):
            raise ValueError("Invalid PostgreSQL identity")
    return db


def credential(name):
    return (f"File.read('{SETUP}/repository-cluster/{name}').strip.tap "
            f"{{ |value| raise 'Empty repository-cluster {name}' if value.empty? }}")


def role_config(plan, role, index, listen):
    port = GITALY_PORT if role == "gitaly" else PRAEFECT_PORT
    config = f"""postgresql['enable'] = false
redis['enable'] = false
nginx['enable'] = false
puma['enable'] = false
sidekiq['enable'] = false
gitlab_workhorse['enable'] = false
gitlab_kas['enable'] = false
prometheus_monitoring['enable'] = false
gitlab_rails['auto_migrate'] = false
gitaly['enable'] = {str(role == 'gitaly').lower()}
praefect['enable'] = {str(role == 'praefect').lower()}
"""
    listener = f"""  listen_addr: nil,
  tls_listen_addr: '{listen}:{port}',
  tls: {{ certificate_path: '{SETUP}/ssl/{role}.crt', key_path: '{SETUP}/ssl/{role}.key' }},
"""
    if role == "gitaly":
        return config + f"""gitlab_rails['internal_api_url'] = '{plan['gitlab_internal_url']}'
gitaly['gitlab_secret'] = {credential('shell-token')}
gitaly['configuration'] = {{
{listener}  auth: {{ token: {credential('gitaly-token')} }},
  storage: [{{ name: 'gitaly-{index}', path: '/var/opt/gitlab/git-data/repositories' }}]
}}
"""
    db = plan["postgres"]
    nodes = ",\n".join(
        f"      {{ storage: 'gitaly-{i}', address: 'tls://{node['host']}:{GITALY_PORT}', **gitaly_node_auth }}"
        for i, node in enumerate(plan["gitaly"], 1)
    )
    return config + f"""praefect['auto_migrate'] = false
gitaly_node_auth = {{ token: {credential('gitaly-token')} }}
praefect['configuration'] = {{
{listener}  auth: {{ token: {credential('praefect-token')} }},
  failover: {{ enabled: true }},
  database: {{
    host: '{db['host']}', port: {db['port']}, user: '{db['user']}', dbname: '{db['database']}',
    password: {credential('postgres-password')}, sslmode: 'verify-full',
    sslrootcert: '{SETUP}/trusted-certs/praefect-db.crt'
  }},
  virtual_storage: [{{ name: 'default', default_replication_factor: 3, node: [
{nodes}
  ] }}]
}}
"""


def compose(name, node, *ports):
    service = {
        "image": IMAGE, "hostname": node["host"], "restart": "unless-stopped", "shm_size": "256m",
        "ports": [f"{node['bind_ipv4']}:{port}:{port}/tcp" for port in ports],
        "healthcheck": {"test": ["CMD", "bash", "-c", f"exec 3<>/dev/tcp/127.0.0.1/{ports[0]}"],
                        "start_period": "120s", "interval": "30s", "timeout": "5s", "retries": 3},
        "volumes": [
            {"type": "bind", "source": f"./{directory}", "target": target, "bind": {"create_host_path": False}}
            for directory, target in (("config", SETUP), ("logs", "/var/log/gitlab"), ("data", "/var/opt/gitlab"))
        ],
    }
    return yaml.safe_dump({"services": {name: service}}, sort_keys=False)


def application_backend_config(backends):
    db = backends["postgres"]
    cache = backends["valkey"]
    config = f"""postgresql['enable'] = false
gitlab_rails['auto_migrate'] = false
gitlab_rails['db_host'] = '{db["host"]}'
gitlab_rails['db_port'] = {db["port"]}
gitlab_rails['db_database'] = '{db["database"]}'
gitlab_rails['db_username'] = '{db["user"]}'
gitlab_rails['db_password'] = {credential('rails-postgres-password')}
gitlab_rails['db_sslmode'] = 'verify-full'
gitlab_rails['db_sslrootcert'] = '{SETUP}/trusted-certs/rails-postgres-ca.crt'
redis['enable'] = false
gitlab_rails['redis_host'] = '{cache["host"]}'
gitlab_rails['redis_port'] = {cache["port"]}
gitlab_rails['redis_password'] = {credential('rails-valkey-password')}
gitlab_rails['redis_ssl'] = true
gitlab_rails['redis_tls_ca_cert_file'] = '{SETUP}/trusted-certs/rails-valkey-ca.crt'
"""
    if cache.get("mutual_tls"):
        config += f"""gitlab_rails['redis_tls_client_cert_file'] = '{SETUP}/ssl/rails-valkey-client.crt'
gitlab_rails['redis_tls_client_key_file'] = '{SETUP}/ssl/rails-valkey-client.key'
"""
    return config


def application_node_config(plan, node, runtime):
    app = plan["application_hosts"]
    origin = plan["gitlab_internal_url"]
    host = https_origin(origin).hostname
    listen = "0.0.0.0" if runtime == "docker" else node["bind_ipv4"]
    peers = [address + "/32" for address in app["proxy_ipv4"]]
    objects = app["object_storage"]
    config = f"""external_url '{origin}'
letsencrypt['enable'] = false
nginx['listen_addresses'] = ['{listen}']
nginx['listen_port'] = {RAILS_HTTPS_PORT}
nginx['redirect_http_to_https'] = false
nginx['ssl_certificate'] = '{SETUP}/ssl/rails.crt'
nginx['ssl_certificate_key'] = '{SETUP}/ssl/rails.key'
nginx['real_ip_trusted_addresses'] = {json.dumps(peers)}
nginx['real_ip_header'] = 'X-Forwarded-For'
gitlab_rails['monitoring_whitelist'] = {json.dumps(["127.0.0.1/32", *peers])}
raise 'Install the shared GitLab application secrets first' unless File.size?('{SETUP}/gitlab-secrets.json')
gitlab_rails['gitlab_ssh_host'] = '{host}'
gitlab_rails['gitlab_shell_ssh_port'] = {app["ssh_port"]}
gitlab_sshd['enable'] = true
gitlab_sshd['listen_address'] = '{listen}:{RAILS_SSH_PORT}'
gitlab_sshd['proxy_protocol'] = true
gitlab_sshd['proxy_policy'] = 'require'
gitlab_sshd['generate_host_keys'] = false
gitlab_sshd['host_keys_glob'] = 'ssh_host_ed25519_key'
gitlab_rails['object_store']['enabled'] = true
gitlab_rails['object_store']['proxy_download'] = true
gitlab_rails['object_store']['connection'] = {{
  'provider' => 'AWS', 'region' => '{objects["region"]}', 'endpoint' => '{objects["endpoint"]}',
  'path_style' => true,
  'aws_access_key_id' => {credential('rails-s3-access-key')},
  'aws_secret_access_key' => {credential('rails-s3-secret-key')}
}}
"""
    return config + "".join(
        f"gitlab_rails['object_store']['objects']['{kind}']['bucket'] = '{objects['buckets'][kind]}'\n"
        for kind in OBJECT_TYPES
    )


def application_proxy(plan):
    app = plan["application_hosts"]
    origin = https_origin(plan["gitlab_internal_url"])
    https_port = origin.port or 443
    entry = plan.get("application_vip")
    bind = "" if entry else app["entry_ipv4"]
    clients = ("    tcp-request connection reject unless { src -m ip " + " ".join(entry["client_cidrs"]) + " }\n") if entry else ""
    initial = " init-state fully-down" if entry else ""
    servers = [(f"rails-{i}", node["bind_ipv4"]) for i, node in enumerate(app["nodes"], 1)]
    return f"""listen gitlab_https
    bind {bind}:{https_port} ssl crt /etc/haproxy/gitlab-public.pem ssl-min-ver TLSv1.2
    mode http
{clients}    balance leastconn
    timeout connect 5s
    timeout client 1h
    timeout server 1h
    http-request set-header Host {origin.netloc}
    http-request del-header Forwarded
    http-request set-header X-Forwarded-Host {origin.netloc}
    http-request set-header X-Forwarded-Proto https
    http-request set-header X-Forwarded-Port {https_port}
    http-request set-header X-Forwarded-For %[src]
    http-request set-header X-Real-IP %[src]
    option httpchk
    http-check send meth GET uri /-/readiness ver HTTP/1.1 hdr Host {origin.netloc}
    http-check expect status 200
    default-server ssl verify required ca-file /etc/haproxy/gitlab-rails-ca.crt verifyhost {origin.hostname} sni str({origin.hostname}) check-sni {origin.hostname} check inter 2s fall 2 rise 2{initial}
""" + "".join(f"    server {name} {address}:{RAILS_HTTPS_PORT}\n" for name, address in servers) + f"""
listen gitlab_ssh
    bind {bind}:{app["ssh_port"]}
    mode tcp
{clients}    balance leastconn
    timeout connect 5s
    timeout client 1h
    timeout server 1h
    option tcp-check
    tcp-check connect send-proxy
    tcp-check expect string SSH-2.0-
    default-server send-proxy-v2 check inter 2s fall 2 rise 2{initial}
""" + "".join(f"    server {name} {address}:{RAILS_SSH_PORT}\n" for name, address in servers) + (
        entry_adapter.health_listener(plan) if entry else "")


def render(plan, runtime):
    validate(plan)
    if runtime not in {"raw", "docker"}:
        raise ValueError("Select raw or docker hosting")
    files = {}
    for role, port in (("gitaly", GITALY_PORT), ("praefect", PRAEFECT_PORT)):
        for index, node in enumerate(plan[role], 1):
            name = f"{role}-{index}"
            listen = "0.0.0.0" if runtime == "docker" else node["bind_ipv4"]
            files[f"{name}.gitlab.rb"] = role_config(plan, role, index, listen)
            if runtime == "docker":
                files[f"{name}.compose.yaml"] = compose(name, node, port)
    endpoint = plan["praefect_endpoint"]
    files["gitlab-application.rb"] = f"""gitaly['enable'] = false
gitlab_shell['secret_token'] = {credential('shell-token')}
gitlab_rails['repositories_storages'] = {{
  'default' => {{ 'gitaly_address' => 'tls://{endpoint}:{PRAEFECT_PORT}',
                 'gitaly_token' => {credential('praefect-token')} }}
}}
"""
    if "application_backends" in plan:
        files["gitlab-application.rb"] += application_backend_config(plan["application_backends"])
    if "application_hosts" in plan:
        for index, node in enumerate(plan["application_hosts"]["nodes"], 1):
            name = f"rails-{index}"
            files[f"{name}.gitlab.rb"] = files["gitlab-application.rb"] + application_node_config(plan, node, runtime)
            if runtime == "docker":
                files[f"{name}.compose.yaml"] = compose(name, node, RAILS_HTTPS_PORT, RAILS_SSH_PORT)
        files["gitlab-application-haproxy.cfg"] = application_proxy(plan)
        if "application_vip" in plan:
            files.update(entry_adapter.files(plan))
    files["praefect-haproxy.cfg"] = f"""listen gitlab_praefect
    bind :{PRAEFECT_PORT}
    mode tcp
    balance leastconn
    timeout connect 5s
    timeout client 1h
    timeout server 1h
    default-server check check-ssl check-alpn h2 check-sni {endpoint} verify required ca-file /etc/haproxy/praefect-ca.crt verifyhost {endpoint} inter 2s fall 2 rise 2 on-marked-down shutdown-sessions
""" + "".join(f"    server praefect-{i} {node['host']}:{PRAEFECT_PORT}\n"
              for i, node in enumerate(plan["praefect"], 1))
    files["render-status.json"] = json.dumps({"runtime": runtime, "fresh_install_only": True,
                                               "deployed": False, "ha_verified": False}, indent=2) + "\n"
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=("raw", "docker"), required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, max_bytes=32 * 1024))
        write_bundle(render(plan, args.runtime), args.output_dir, private_root=ROOT / "private")
    except (ValueError, OSError):
        print("GitLab repository host render failed. Check the private plan and output directory.", file=sys.stderr)
        return 1
    print(json.dumps({"rendered": True, "runtime": args.runtime, "deployed": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
