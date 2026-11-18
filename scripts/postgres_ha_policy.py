#!/usr/bin/env python3
"""Finite PostgreSQL HA policy shared by private renderers and materialization."""

import ipaddress
import re
import uuid


RUNTIMES = ("rke2", "docker", "raw", "k3s", "k8s")
HOST_RUNTIMES = {"raw", "docker"}
WRITE_ROUTER_DIR = "/etc/platform-postgres-router"
OWNER = "platform.gitops/postgres-ha"
STRICT_PATRONI_DCS = {"4.1.5": "c11a370ebf6098442bd0756f2e9f3504795c91aba5ce652531252baca6920154"}
STRICT_PATRONI_KAZOO = "2.11.0"
PASSWORDS = {"superuser": "superuser-password", "replication": "replication-password", "rewind": "rewind-password"}
REST_PASSWORD = "rest-password"


def label(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value))


def hostname(value):
    return isinstance(value, str) and len(value) <= 253 and all(label(part) for part in value.split("."))


def absolute(value):
    return isinstance(value, str) and bool(re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+", value))


def canonical_cpu(value):
    if value.endswith("m") and int(value[:-1]) % 1000 == 0: return str(int(value[:-1]) // 1000)
    return value


def canonical_capacity(value):
    amount, unit = int(value[:-2]), value[-2:]
    if unit == "Mi" and amount % 1024 == 0: amount, unit = amount // 1024, "Gi"
    if unit == "Gi" and amount % 1024 == 0: amount, unit = amount // 1024, "Ti"
    return str(amount) + unit


def application_databases(plan):
    return [plan["bootstrap"], *plan.get("applications", [])]


def validate_applications(plan, runtime):
    if "applications" in plan and (runtime not in HOST_RUNTIMES or not isinstance(plan["applications"], list)
                                   or not 1 <= len(plan["applications"]) <= 32):
        raise ValueError("Declare a bounded list of additional Patroni application databases")
    items = application_databases(plan)
    for item in items:
        if (not isinstance(item, dict) or set(item) != {"database", "owner", "secret"} or not label(item["secret"])
                or any(not isinstance(item[k], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", item[k])
                       for k in ("database", "owner"))):
            raise ValueError("Declare a reviewed application database, owner and private credential reference")
        if (item["database"] in {"postgres", "template0", "template1"}
                or item["owner"] in {"postgres", "platform_pg_admin", "platform_pg_replica", "platform_pg_rewind"}
                or item["owner"].startswith("pg_")):
            raise ValueError("Application databases and logins must use dedicated identities")
    if any(len({item[key] for item in items}) != len(items) for key in ("database", "owner", "secret")):
        raise ValueError("Give each application its own database, login and credential reference")


def usable_ipv4(value):
    address = ipaddress.IPv4Address(value)
    return str(address) == value and not (address.is_loopback or address.is_unspecified
                                          or address.is_multicast or address.is_link_local)


def validate_write_endpoint(plan, runtime):
    if "write_endpoint" not in plan:
        return
    endpoint = plan["write_endpoint"]
    if (runtime not in HOST_RUNTIMES or not isinstance(endpoint, dict)
            or set(endpoint) != {"host", "port", "client_cidrs", "routers", "haproxy_image"}):
        raise ValueError("Declare the complete optional Patroni write endpoint")
    if (not hostname(endpoint["host"]) or "." not in endpoint["host"]
            or endpoint["host"] in {member["host"] for member in plan["members"]}
            or type(endpoint["port"]) is not int or not 1024 <= endpoint["port"] <= 65535):
        raise ValueError("Use a distinct DNS name and a valid write endpoint port")
    if (not isinstance(endpoint["haproxy_image"], str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}", endpoint["haproxy_image"])):
        raise ValueError("Pin the HAProxy image by digest")
    networks = endpoint["client_cidrs"]
    if not isinstance(networks, list) or not 1 <= len(networks) <= 32:
        raise ValueError("Declare a bounded set of write endpoint client networks")
    for value in networks:
        network = ipaddress.IPv4Network(value, strict=True)
        if str(network) != value or network.prefixlen == 0 or network.is_multicast or network.is_loopback or network.is_link_local:
            raise ValueError("Use valid bounded write endpoint source networks")
    routers = endpoint["routers"]
    if not isinstance(routers, list) or len(routers) != 2:
        raise ValueError("Declare two independent HAProxy routers")
    for router in routers:
        if (not isinstance(router, dict) or set(router) != {"name", "address", "failure_domain"}
                or any(not label(router[k]) for k in ("name", "failure_domain"))
                or not usable_ipv4(router["address"])
                or router["address"] in {member["address"] for member in plan["members"]}):
            raise ValueError("Declare independent router addresses and failure domains")
    if any(len({router[key] for router in routers}) != 2 for key in ("name", "address", "failure_domain")):
        raise ValueError("The two routers must have distinct identities and failure domains")


def write_router_config(plan, router):
    endpoint = plan["write_endpoint"]
    address = router["address"]
    sql_port = plan["ports"]["sql"]
    rest_port = plan["ports"]["rest"]
    lines = [
        "global",
        "    ssl-default-server-options ssl-min-ver TLSv1.2",
        "defaults",
        "    mode tcp",
        "    timeout connect 3s",
        "    timeout client 1h",
        "    timeout server 1h",
        "    timeout check 2s",
        "frontend postgres_write",
        f"    bind {address}:{endpoint['port']}",
        "    acl approved_client src " + " ".join(endpoint["client_cidrs"]),
        "    tcp-request connection reject unless approved_client",
        "    tcp-request connection reject unless { nbsrv(patroni_primary) eq 1 }",
        "    default_backend patroni_primary",
        "backend patroni_primary",
        "    option httpchk",
        f"    http-check connect port {rest_port} ssl",
        "    http-check send meth GET uri /primary ver HTTP/1.0",
        "    http-check expect status 200",
        "    default-server inter 1s rise 2 fall 1 init-state fully-down on-marked-down shutdown-sessions",
    ]
    lines.extend(
        f"    server {member['name']} {member['address']}:{sql_port} source {address} check check-ssl "
        f"check-sni {member['host']} verify required verifyhost {member['host']} "
        f"ca-file {WRITE_ROUTER_DIR}/rest-ca.crt crt {WRITE_ROUTER_DIR}/rest-client.pem"
        for member in plan["members"]
    )
    return "\n".join(lines) + "\n"


def validate(plan, runtime):
    fields = {"schema_version", "cluster_id", "name", "namespace", "postgres_version", "cnpg_version", "patroni_version", "images", "storage", "bootstrap",
              "failure_domain_key", "replication", "ports", "client_cidrs", "members", "zookeeper", "tls", "native", "resources"}
    if (runtime not in RUNTIMES or not isinstance(plan, dict) or set(plan) - {"applications", "write_endpoint"} != fields
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1):
        raise ValueError("Use a complete reviewed PostgreSQL policy")
    if not isinstance(plan["cluster_id"], str) or str(uuid.UUID(plan["cluster_id"])) != plan["cluster_id"]: raise ValueError("Declare a canonical independent cluster UUID")
    if any(not label(plan[k]) for k in ("name", "namespace")): raise ValueError("Use exact PostgreSQL resource identities")
    if not isinstance(plan["postgres_version"], str) or not re.fullmatch(r"(?:17|18)[.][1-9][0-9]*", plan["postgres_version"]): raise ValueError("Review an exact supported PostgreSQL 17 or 18 patch release")
    if not isinstance(plan["cnpg_version"], str) or not re.fullmatch(r"1[.]30[.][0-9]+", plan["cnpg_version"]): raise ValueError("This policy targets the reviewed CloudNativePG 1.30 schema")
    if not isinstance(plan["patroni_version"], str) or not re.fullmatch(r"4[.]1[.][0-9]+", plan["patroni_version"]): raise ValueError("This policy targets the Patroni 4.1 configuration lane")
    if runtime in HOST_RUNTIMES and plan["patroni_version"] not in STRICT_PATRONI_DCS:
        raise ValueError("Select a Patroni release with a reviewed hostname-verifying ZooKeeper adapter")
    if (not isinstance(plan["images"], dict) or set(plan["images"]) != {"postgres", "patroni"}
            or any(not isinstance(v, str) or not re.fullmatch(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}", v) for v in plan["images"].values())):
        raise ValueError("Pin both reviewed runtime images by digest")
    storage = plan["storage"]
    if not isinstance(storage, dict) or set(storage) != {"size", "class"} or not re.fullmatch(r"[1-9][0-9]*(?:Gi|Ti)", str(storage["size"])) or not label(storage["class"]): raise ValueError("Declare reviewed persistent capacity and class")
    resources = plan["resources"]
    if not isinstance(resources, dict) or set(resources) != {"requests", "limits"}: raise ValueError("Declare resource requests and limits")
    for values in resources.values():
        if (not isinstance(values, dict) or set(values) != {"cpu", "memory"} or not isinstance(values["cpu"], str) or not re.fullmatch(r"[1-9][0-9]*m?", values["cpu"])
                or not isinstance(values["memory"], str) or not re.fullmatch(r"[1-9][0-9]*(?:Mi|Gi)", values["memory"])): raise ValueError("Use positive CPU and memory quantities")
    cpu = lambda value: int(value[:-1]) if value.endswith("m") else int(value) * 1000
    memory = lambda value: int(value[:-2]) * (1024 if value.endswith("Gi") else 1)
    if cpu(resources["limits"]["cpu"]) < cpu(resources["requests"]["cpu"]) or memory(resources["limits"]["memory"]) < memory(resources["requests"]["memory"]): raise ValueError("Resource limits cannot be below requests")
    validate_applications(plan, runtime)
    if plan["failure_domain_key"] not in {"topology.kubernetes.io/zone", "kubernetes.io/hostname"}: raise ValueError("Declare an enforceable failure-domain key")
    replication = plan["replication"]
    if (not isinstance(replication, dict) or set(replication) != {"standbys", "synchronous_commit"} or type(replication["standbys"]) is not int or replication["standbys"] not in {1, 2}
            or replication["synchronous_commit"] not in {"on", "remote_apply"}): raise ValueError("Choose a strict one- or two-standby durability policy")
    ports = plan["ports"]
    if not isinstance(ports, dict) or set(ports) != {"sql", "rest"} or any(type(p) is not int or not 1024 <= p <= 65535 for p in ports.values()) or len(set(ports.values())) != 2: raise ValueError("Declare distinct SQL and REST ports")
    if runtime in {"rke2", "k3s", "k8s"} and ports["sql"] != 5432: raise ValueError("CloudNativePG uses its managed PostgreSQL port")
    networks = plan["client_cidrs"]
    if not isinstance(networks, list) or not 1 <= len(networks) <= 32: raise ValueError("Inventory approved database-client networks")
    for value in networks:
        network = ipaddress.IPv4Network(value, strict=True)
        if str(network) != value or network.prefixlen == 0 or network.is_multicast or network.is_loopback or network.is_link_local: raise ValueError("Use bounded non-loopback client networks")
    members = plan["members"]
    if not isinstance(members, list) or len(members) != 3: raise ValueError("Declare three independent database members")
    for member in members:
        if (not isinstance(member, dict) or set(member) != {"name", "host", "address", "failure_domain", "node_name", "node_uid", "state_path", "tls_secret"}
                or any(not label(member[k]) for k in ("name", "failure_domain", "node_name", "tls_secret")) or not hostname(member["host"])
                or not (member["node_uid"] is None and runtime in {"docker", "raw"}) and (not isinstance(member["node_uid"], str) or str(uuid.UUID(member["node_uid"])) != member["node_uid"])
                or not absolute(member["state_path"]) or not member["state_path"].startswith("/srv/postgres/")):
            raise ValueError("Bind each database member to reviewed host, node, storage and TLS identities")
        address = ipaddress.IPv4Address(member["address"])
        if str(address) != member["address"] or address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local: raise ValueError("Declare usable member bind addresses")
    for key in ("name", "host", "address", "failure_domain", "node_name", "state_path", "tls_secret"):
        if len({m[key] for m in members}) != 3: raise ValueError("Members must span three distinct physical failure domains and state locations")
    uids = [m["node_uid"] for m in members]
    if any(uid is not None for uid in uids) and len(set(uids)) != 3: raise ValueError("Database Node identities must be distinct")
    if any(b["state_path"].startswith(a["state_path"] + "/") for a in members for b in members if a != b): raise ValueError("Database state paths cannot overlap")
    zk = plan["zookeeper"]
    if (not isinstance(zk, dict) or set(zk) != {"hosts", "principals"} or not isinstance(zk["hosts"], list) or len(zk["hosts"]) != 3
            or not isinstance(zk["principals"], list) or len(zk["principals"]) != 3
            or any(not isinstance(p, str) or not re.fullmatch(r"CN=[A-Za-z0-9_-]{1,63}", p) for p in zk["principals"])):
        raise ValueError("Declare three mTLS DCS endpoints and exact certificate principals")
    if len(set(zk["principals"])) != 3: raise ValueError("Use distinct DCS certificate identities")
    for item in zk["hosts"]:
        if not isinstance(item, dict) or set(item) != {"host", "port"} or not hostname(item["host"]) or type(item["port"]) is not int or not 1024 <= item["port"] <= 65535: raise ValueError("Invalid TLS DCS endpoint")
    if len({item["host"] for item in zk["hosts"]}) != 3: raise ValueError("DCS endpoints must be distinct")
    tls = plan["tls"]
    if (not isinstance(tls, dict) or set(tls) != {"server_ca_secret", "server_secret", "ca_sha256"} or not label(tls["server_ca_secret"]) or not label(tls["server_secret"])
            or not isinstance(tls["ca_sha256"], dict) or set(tls["ca_sha256"]) != {"postgres", "rest", "zk"}
            or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in tls["ca_sha256"].values())):
        raise ValueError("Declare separate runtime trust references and CA fingerprints")
    native = plan["native"]
    if (not isinstance(native, dict) or set(native) != {"postgres_bin_dir", "uid", "gid"} or not absolute(native["postgres_bin_dir"])
            or any(type(native[k]) is not int or not 1 <= native[k] <= 65535 for k in ("uid", "gid"))): raise ValueError("Declare non-root runtime identity and reviewed PostgreSQL binary directory")
    validate_write_endpoint(plan, runtime)
    return plan


def patroni(plan, member):
    trust = "/etc/platform-postgres/inputs"
    sql = str(plan["ports"]["sql"]); rest = str(plan["ports"]["rest"])
    parameters = {"ssl": "on", "ssl_cert_file": trust + "/postgres.crt", "ssl_key_file": trust + "/postgres.key", "ssl_ca_file": trust + "/postgres-ca.crt",
                  "ssl_min_protocol_version": "TLSv1.2", "password_encryption": "scram-sha-256", "wal_log_hints": "on", "synchronous_commit": plan["replication"]["synchronous_commit"]}
    auth = {role: {"username": {"superuser": "platform_pg_admin", "replication": "platform_pg_replica", "rewind": "platform_pg_rewind"}[role],
                   "password": "__PRIVATE_" + key.upper().replace("-", "_") + "__", "sslmode": "verify-full", "sslrootcert": trust + "/postgres-ca.crt"} for role, key in PASSWORDS.items()}
    replication_hba = ["hostssl replication platform_pg_replica " + m["address"] + "/32 scram-sha-256" for m in plan["members"]]
    internal_hba = ["hostssl all " + role + " " + m["address"] + "/32 scram-sha-256" for m in plan["members"] for role in ("platform_pg_admin", "platform_pg_rewind")]
    applications = application_databases(plan)
    application_users = ",".join(f'"{app["owner"]}"' for app in applications)
    application_networks = dict.fromkeys([
        *plan["client_cidrs"], *[f'{router["address"]}/32' for router in plan.get("write_endpoint", {}).get("routers", [])]
    ])
    application_hba = [f'hostssl "{app["database"]}" "{app["owner"]}" {cidr} scram-sha-256'
                       for app in applications for cidr in application_networks]
    hba = ["local all platform_pg_admin scram-sha-256", "local all platform_pg_rewind scram-sha-256",
           f"local all {application_users} reject", "local all all peer", *replication_hba, *internal_hba,
           *application_hba, f"host all {application_users} 0.0.0.0/0 reject",
           f"host all {application_users} ::/0 reject",
           *["hostssl all all " + cidr + " scram-sha-256" for cidr in plan["client_cidrs"]],
           "host all all 0.0.0.0/0 reject", "host all all ::/0 reject"]
    return {"scope": plan["name"], "name": member["name"], "namespace": "/platform-postgres/" + plan["cluster_id"],
        "restapi": {"listen": member["address"] + ":" + rest, "connect_address": member["host"] + ":" + rest,
                    "cafile": trust + "/rest-ca.crt", "certfile": trust + "/rest.crt", "keyfile": trust + "/rest.key", "verify_client": "required",
                    "authentication": {"username": "platform_patroni_admin", "password": "__PRIVATE_REST_PASSWORD__"}},
        "ctl": {"insecure": False, "cacert": trust + "/rest-ca.crt", "certfile": trust + "/rest.crt", "keyfile": trust + "/rest.key",
                "authentication": {"username": "platform_patroni_admin", "password": "__PRIVATE_REST_PASSWORD__"}},
        "zookeeper": {"hosts": [e["host"] + ":" + str(e["port"]) for e in plan["zookeeper"]["hosts"]], "use_ssl": True, "verify": True,
                      "cacert": trust + "/zk-ca.crt", "cert": trust + "/zk.crt", "key": trust + "/zk.key",
                      "set_acls": {principal: ["ALL"] for principal in plan["zookeeper"]["principals"]}},
        "bootstrap": {"dcs": {"ttl": 30, "loop_wait": 10, "retry_timeout": 10, "maximum_lag_on_failover": 0, "check_timeline": True,
                              "synchronous_mode": "quorum", "synchronous_mode_strict": True, "synchronous_node_count": plan["replication"]["standbys"],
                              "postgresql": {"use_pg_rewind": True, "parameters": {k: v for k, v in parameters.items() if not k.endswith("_file")}}},
                      "initdb": [{"encoding": "UTF8"}, "data-checksums"], "pg_hba": hba},
        "postgresql": {"listen": member["address"] + ":" + sql, "connect_address": member["host"] + ":" + sql, "data_dir": member["state_path"],
                       "bin_dir": plan["native"]["postgres_bin_dir"], "pgpass": "/run/platform-postgres/pgpass", "authentication": auth,
                       "parameters": parameters, "pg_hba": hba, "remove_data_directory_on_rewind_failure": False, "remove_data_directory_on_diverged_timelines": False},
        "watchdog": {"mode": "required", "device": "/dev/watchdog", "safety_margin": 5},
        "tags": {"nofailover": False, "nosync": False, "noloadbalance": False}}


def cnpg(plan):
    namespace = plan["namespace"]; labels = {OWNER: plan["name"]}
    base = {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster", "metadata": {"name": plan["name"], "namespace": namespace, "labels": labels},
        "spec": {"instances": 3, "imageName": plan["images"]["postgres"], "enableSuperuserAccess": False, "enablePDB": True, "primaryUpdateStrategy": "supervised",
            "resources": {kind: {"cpu": canonical_cpu(values["cpu"]), "memory": canonical_capacity(values["memory"])} for kind, values in plan["resources"].items()},
            "certificates": {"serverCASecret": plan["tls"]["server_ca_secret"], "serverTLSSecret": plan["tls"]["server_secret"]},
            "bootstrap": {"initdb": {"database": plan["bootstrap"]["database"], "owner": plan["bootstrap"]["owner"], "secret": {"name": plan["bootstrap"]["secret"]}, "dataChecksums": True}},
            "storage": {"size": canonical_capacity(plan["storage"]["size"]), "storageClass": plan["storage"]["class"]},
            "affinity": {"enablePodAntiAffinity": True, "podAntiAffinityType": "required", "topologyKey": plan["failure_domain_key"],
                "nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [
                    {"key": "kubernetes.io/hostname", "operator": "In", "values": [m["node_name"] for m in plan["members"]]}]}]}}},
            "postgresql": {"synchronous": {"method": "any", "number": plan["replication"]["standbys"], "dataDurability": "required", "failoverQuorum": True},
                "parameters": {"synchronous_commit": plan["replication"]["synchronous_commit"], "password_encryption": "scram-sha-256", "ssl_min_protocol_version": "TLSv1.2"},
                "pg_hba": [*["hostssl all all " + cidr + " scram-sha-256" for cidr in plan["client_cidrs"]], "host all all 0.0.0.0/0 reject", "host all all ::/0 reject"]}}}
    return base
