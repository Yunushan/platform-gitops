#!/usr/bin/env python3
"""Read the local HAProxy route-health gate; never advertise or modify a VIP."""

import argparse
import http.client
import re
import sys

from http_transport import HttpTransportPolicyError, read_bounded_response


def vrrp_config(plan, host, index, *, helper, identity, gerrit=False):
    other = plan["entry_hosts"][1 - index]
    flags = " --gerrit" if gerrit else ""
    return "\n".join([
        "global_defs {", "    router_id " + plan["name"] + "-" + host["name"],
        "    enable_script_security", "    script_user root", "}", "",
        "vrrp_script edge_ready {",
        f'    script "/usr/bin/python3 -I -B {helper} --port {plan["health_port"]} --identity {identity}{flags}"',
        "    interval 2", "    timeout 2", "    fall 2", "    rise 2", "    weight 0", "    init_fail", "}", "",
        "vrrp_instance EDGE {", "    state BACKUP", "    version 3", "    interface " + host["interface"],
        "    virtual_router_id " + str(plan["vrid"]), "    priority " + str(150 if index == 0 else 100),
        "    advert_int 1", "    nopreempt", "    unicast_src_ip " + host["address"],
        "    check_unicast_src", "    unicast_peer {", "        " + other["address"] + " min_ttl 255 max_ttl 255", "    }",
        "    virtual_ipaddress {", "        " + plan["vip"] + " dev " + host["interface"], "    }",
        "    track_script {", "        edge_ready", "    }", "}", "",
    ])


def healthy(port, identity, *, gerrit=False):
    if type(port) is not int or not 1024 <= port <= 65535 or not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
        return False
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request("GET", "/routez" if gerrit else "/ready", headers={"Connection": "close"})
        response = connection.getresponse()
        values = (identity + " primary", identity + " backup") if gerrit else ("ready-" + identity,)
        return response.status == 200 and read_bounded_response(response, max_bytes=8192) in [value.encode("ascii") for value in values]
    except (OSError, http.client.HTTPException, HttpTransportPolicyError):
        return False
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--gerrit", action="store_true")
    args = parser.parse_args(argv)
    return 0 if healthy(args.port, args.identity, gerrit=args.gerrit) else 1


if __name__ == "__main__":
    sys.exit(main())
