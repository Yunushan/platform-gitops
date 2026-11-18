"""Optional native HAProxy/Keepalived entry pair for external GitLab Rails."""

from hashlib import sha256
from ipaddress import ip_interface, ip_network
import json
import re

from gateway_edge_health import vrrp_config
from render_gerrit_router import dns_name, health_program


BASE = "/etc/haproxy"
HELPER = BASE + "/gitlab-entry-health.py"


def identity(plan):
    return sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate(plan, origin):
    entry = plan["application_vip"]
    required = {"vip", "vrid", "health_port", "entry_hosts", "client_cidrs"}
    if not isinstance(entry, dict) or set(entry) != required or not isinstance(entry["vip"], str):
        raise ValueError("Configure an explicit GitLab application VIP pair")
    vip = ip_interface(entry["vip"])
    if (vip.version != 4 or str(vip) != entry["vip"] or not 16 <= vip.network.prefixlen <= 30
            or str(vip.ip) != plan["application_hosts"]["entry_ipv4"]
            or vip.ip in {vip.network.network_address, vip.network.broadcast_address}):
        raise ValueError("Bind the application VIP to the reviewed entry address and subnet")
    if type(entry["vrid"]) is not int or not 1 <= entry["vrid"] <= 255:
        raise ValueError("Use an explicit VRRP instance ID")
    if (type(entry["health_port"]) is not int or not 1024 <= entry["health_port"] <= 65535
            or entry["health_port"] in {origin.port or 443, plan["application_hosts"]["ssh_port"], 8443, 2222, 3305}):
        raise ValueError("Use a distinct unprivileged loopback health port")
    hosts = entry["entry_hosts"]
    if not isinstance(hosts, list) or len(hosts) != 2:
        raise ValueError("Declare exactly two entry peers")
    for host in hosts:
        if (not isinstance(host, dict) or set(host) != {"name", "address", "interface", "failure_domain"}
                or not dns_name(host["name"]) or len(host["name"]) > 12 or "." in host["name"]
                or not dns_name(host["failure_domain"])
                or not isinstance(host["address"], str)
                or not isinstance(host["interface"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", host["interface"])):
            raise ValueError("Declare peer names, interfaces and distinct failure domains")
        address = ip_interface(host["address"])
        if (address.version != 4 or str(address.ip) != host["address"] or address.ip not in vip.network
                or address.ip in {vip.ip, vip.network.network_address, vip.network.broadcast_address}):
            raise ValueError("Entry peers must be distinct usable addresses on the VIP subnet")
    if ([host["address"] for host in hosts] != plan["application_hosts"]["proxy_ipv4"]
            or any(len({host[key] for host in hosts}) != 2 for key in ("name", "address", "failure_domain"))):
        raise ValueError("Bind the VIP peers to the Rails trusted-proxy identities")
    clients = entry["client_cidrs"]
    if not isinstance(clients, list) or not 1 <= len(clients) <= 32:
        raise ValueError("Declare approved client IPv4 networks")
    for value in clients:
        if not isinstance(value, str) or ip_network(value, strict=True).version != 4 or str(ip_network(value)) != value:
            raise ValueError("Use canonical client IPv4 networks")
    if len(set(clients)) != len(clients):
        raise ValueError("Use distinct approved client networks")


def health_listener(plan):
    return f"""
frontend gitlab_entry_health
    bind 127.0.0.1:{plan['application_vip']['health_port']}
    mode http
    http-request return status 404 unless {{ path -m str /ready }}
    http-request return status 200 content-type text/plain string ready-{identity(plan)} if {{ nbsrv(gitlab_https) gt 0 }} {{ nbsrv(gitlab_ssh) gt 0 }}
    http-request return status 503 content-type text/plain string unavailable
"""


def files(plan):
    settings = {**plan["application_vip"], "name": "gitlab"}
    return {
        "gitlab-entry-health.py": health_program(),
        **{f"keepalived-{host['name']}.conf": vrrp_config(settings, host, index, helper=HELPER, identity=identity(plan))
           for index, host in enumerate(settings["entry_hosts"])},
        "gitlab-entry-haproxy.service": f"""[Unit]
Description=GitLab application HTTPS and SSH entry
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=haproxy
Group=haproxy
ExecStartPre=/usr/sbin/haproxy -c -f {BASE}/gitlab-application-haproxy.cfg
ExecStart=/usr/sbin/haproxy -db -f {BASE}/gitlab-application-haproxy.cfg
Restart=on-failure
RestartSec=2
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_BIND_SERVICE
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
UMask=0077

[Install]
WantedBy=multi-user.target
""",
        "gitlab-entry-vrrp@.service": f"""[Unit]
Description=GitLab application VIP peer %i
Wants=network-online.target gitlab-entry-haproxy.service
After=network-online.target gitlab-entry-haproxy.service

[Service]
Type=simple
ExecStartPre=/usr/sbin/keepalived --config-test --vrrp --use-file={BASE}/keepalived-%i.conf
ExecStart=/usr/sbin/keepalived --dont-fork --log-console --vrrp --no_bfd --use-file={BASE}/keepalived-%i.conf --pid=/run/gitlab-entry-vrrp-%i/main.pid --vrrp_pid=/run/gitlab-entry-vrrp-%i/child.pid
Restart=on-failure
RestartSec=2
RuntimeDirectory=gitlab-entry-vrrp-%i
RuntimeDirectoryMode=0700
ReadWritePaths=/run/gitlab-entry-vrrp-%i
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW CAP_SETGID
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
UMask=0077

[Install]
WantedBy=multi-user.target
""",
    }
