#!/usr/bin/env python3
"""Observe original CNPG instances with fixed read-only SQL; never certify write durability."""

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
import deploy_postgres_ha as deploy
from kubernetes_management import Client
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ("fsync", "full_page_writes", "data_checksums", "ssl", "wal_level", "password_encryption",
            "ssl_min_protocol_version", "synchronous_commit", "synchronous_standby_names")
# No conninfo, role/password catalog, user tables, arbitrary SQL, SQL writes,
# network credentials, extension functions, or recovery/promotion calls.
SQL = """BEGIN READ ONLY;
SET LOCAL search_path = pg_catalog;
SET LOCAL statement_timeout = '5s';
SET LOCAL lock_timeout = '1s';
SELECT jsonb_build_object(
 'schema_version', 1,
 'transaction_read_only', current_setting('transaction_read_only'),
 'user', current_user, 'database', current_database(),
 'server_version_num', current_setting('server_version_num')::integer,
 'postmaster_started_at', extract(epoch FROM pg_postmaster_start_time())::text,
 'in_recovery', pg_is_in_recovery(),
 'system_identifier', (SELECT system_identifier::text FROM pg_control_system()),
 'timeline_id', (SELECT timeline_id FROM pg_control_checkpoint()),
 'settings', (SELECT jsonb_object_agg(name, jsonb_build_object('value', setting, 'pending_restart', pending_restart))
              FROM pg_settings WHERE name IN (
               'fsync', 'full_page_writes', 'data_checksums', 'ssl', 'wal_level', 'password_encryption',
               'ssl_min_protocol_version', 'synchronous_commit', 'synchronous_standby_names')),
 'senders', (SELECT coalesce(jsonb_agg(jsonb_build_object(
   'application_name', r.application_name, 'client_addr', host(r.client_addr),
   'state', r.state, 'sync_state', r.sync_state, 'sync_priority', r.sync_priority,
   'flush_lsn', r.flush_lsn::text, 'replay_lsn', r.replay_lsn::text,
   'tls', s.ssl, 'tls_version', s.version, 'client_certificate_present', s.client_dn IS NOT NULL
  ) ORDER BY r.application_name), '[]'::jsonb)
  FROM pg_stat_replication r LEFT JOIN pg_stat_ssl s ON s.pid = r.pid),
 'receivers', (SELECT coalesce(jsonb_agg(jsonb_build_object(
   'status', status, 'sender_host', sender_host, 'sender_port', sender_port,
   'received_tli', received_tli, 'flushed_lsn', flushed_lsn::text,
   'last_message_age_seconds', extract(epoch FROM clock_timestamp() - last_msg_receipt_time)
  )), '[]'::jsonb) FROM pg_stat_wal_receiver)
);
ROLLBACK;
"""
UNVERIFIED = {key: False for key in ("synchronous_replication_verified", "write_durability_verified", "promotion_verified",
                                     "restore_verified", "ha_verified", "full_stack_verified")}


def load_installation(path, digest, plan):
    raw = read_bounded_bytes(path, max_bytes=256 * 1024)
    if not isinstance(digest, str) or not deploy.SHA.fullmatch(digest) or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Pin the original completed installation receipt")
    state = loads_strict_json(raw)
    if (not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1
            or state.get("action") != "apply" or state.get("phase") != "completed" or state.get("creation_attempted") is not True
            or state.get("resources_ready") is not True or not deploy.uid(state.get("cluster_uid"))
            or not isinstance(state.get("operation_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", state["operation_id"])
            or state.get("deployment_plan_sha256") != deploy.fingerprint(plan) or state.get("contract_sha256") != plan["contract_sha256"]
            or state.get("admitted_spec_sha256") != plan["admitted_spec_sha256"] or plan["admitted_spec_sha256"] is None
            or any(state.get(k) is not False for k in ("synchronous_replication_verified", "promotion_verified", "restore_verified", "ha_verified", "full_stack_verified"))):
        raise ValueError("Use only the matching completed fresh installation receipt")
    return state


def lsn(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9A-F]{1,8}/[0-9A-F]{1,8}", value): raise ValueError("Missing WAL position")
    high, low = value.split("/"); number = (int(high, 16) << 32) + int(low, 16)
    if number == 0: raise ValueError("Missing WAL position")
    return number


def integer(value, lower=1, upper=2 ** 32 - 1): return type(value) is int and lower <= value <= upper


def common(sample, selected):
    fields = {"schema_version", "transaction_read_only", "user", "database", "server_version_num", "postmaster_started_at",
              "in_recovery", "system_identifier", "timeline_id", "settings", "senders", "receivers"}
    major, patch = map(int, selected["postgres_version"].split("."))
    if (not isinstance(sample, dict) or set(sample) != fields or type(sample["schema_version"]) is not int or sample["schema_version"] != 1
            or sample["transaction_read_only"] != "on" or sample["user"] != "postgres" or sample["database"] != "postgres"
            or type(sample["server_version_num"]) is not int or sample["server_version_num"] != major * 10000 + patch
            or not isinstance(sample["postmaster_started_at"], str) or not re.fullmatch(r"[1-9][0-9]{8,11}[.][0-9]{1,6}", sample["postmaster_started_at"])
            or type(sample["in_recovery"]) is not bool or not integer(sample["timeline_id"])
            or not isinstance(sample["system_identifier"], str) or not re.fullmatch(r"[1-9][0-9]{0,19}", sample["system_identifier"])
            or int(sample["system_identifier"]) >= 2 ** 64
            or not isinstance(sample["senders"], list) or len(sample["senders"]) > 2 or not isinstance(sample["receivers"], list) or len(sample["receivers"]) > 1):
        raise ValueError("Database identity, version, role or observation shape changed")
    settings = sample["settings"]
    if (not isinstance(settings, dict) or set(settings) != set(SETTINGS)
            or any(not isinstance(row, dict) or set(row) != {"value", "pending_restart"} or not isinstance(row["value"], str)
                   or row["pending_restart"] is not False for row in settings.values())):
        raise ValueError("Database configuration is incomplete or requires a restart")
    values = {key: row["value"] for key, row in settings.items()}
    if (any(values[k] != "on" for k in ("fsync", "full_page_writes", "data_checksums", "ssl"))
            or values["wal_level"] not in {"replica", "logical"} or values["password_encryption"] != "scram-sha-256"
            or values["ssl_min_protocol_version"] not in {"TLSv1.2", "TLSv1.3"}
            or values["synchronous_commit"] != selected["replication"]["synchronous_commit"]):
        raise ValueError("Runtime durability or TLS configuration differs from the reviewed policy")
    return values


def quorum(value, count, names):
    # The operator emits ANY N ("pod-a","pod-b"). Refuse wildcard,
    # extra/duplicate/ineligible names, FIRST, and a reduced quorum.
    match = re.fullmatch(r'ANY\s+([12])\s*\(\s*((?:"[a-z0-9-]+"|[a-z0-9-]+)(?:\s*,\s*(?:"[a-z0-9-]+"|[a-z0-9-]+))*)\s*\)', value)
    if not match: raise ValueError("Use the reviewed explicit synchronous quorum")
    actual = [item.strip().strip('"') for item in match[2].split(",")]
    if int(match[1]) != count or len(actual) != 2 or len(set(actual)) != 2 or set(actual) != names:
        raise ValueError("The active synchronous quorum differs from the reviewed replicas")


def pod_fence(pod, selected):
    if not isinstance(pod, dict): raise ValueError("The original database pod is missing")
    meta, spec, status = pod.get("metadata", {}), pod.get("spec", {}), pod.get("status", {})
    containers, statuses = spec.get("containers", []), status.get("containerStatuses", [])
    if (not deploy.uid(meta.get("uid")) or meta.get("deletionTimestamp") or len(containers) != 1 or len(statuses) != 1
            or containers[0].get("name") != "postgres" or containers[0].get("image") != selected["images"]["postgres"]
            or statuses[0].get("name") != "postgres" or statuses[0].get("ready") is not True
            or not integer(statuses[0].get("restartCount"), 0) or not isinstance(statuses[0].get("containerID"), str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*://[A-Za-z0-9._:-]{1,200}", statuses[0]["containerID"])
            or not statuses[0].get("state", {}).get("running", {}).get("startedAt")
            or not deploy.condition(pod, "Ready")):
        raise ValueError("Require one identified, ready PostgreSQL container")
    addresses = {str(ipaddress.ip_address(a["ip"])) for a in status.get("podIPs", [])}
    addresses.add(str(ipaddress.ip_address(status.get("podIP"))))
    return {"uid": meta["uid"], "node": spec.get("nodeName"), "addresses": sorted(addresses),
            "container": copy.deepcopy(statuses[0]), "volumes": copy.deepcopy(spec.get("volumes", []))}


def topology(client, selected, expected, state, allowed):
    current = client.get(deploy.CLUSTER + selected["name"], selected["namespace"])
    deploy.owned(current, expected, state)
    if not deploy.ready(client, selected, current, allowed): raise ValueError("Original database resources are not ready")
    pods = deploy.collection(client, "pods", selected["namespace"], "cnpg.io/cluster=" + selected["name"])
    fences = {p["metadata"]["name"]: pod_fence(p, selected) for p in pods}
    if len(fences) != 3 or len({p["uid"] for p in fences.values()}) != 3: raise ValueError("Database pod identities are not distinct")
    ips = [address for p in fences.values() for address in p["addresses"]]
    if len(ips) != len(set(ips)): raise ValueError("Database pod addresses overlap")
    volumes = {}
    for name, fence in fences.items():
        claims = [v["persistentVolumeClaim"]["claimName"] for v in fence["volumes"] if "persistentVolumeClaim" in v]
        if len(claims) != 1: raise ValueError("Require one database claim per original pod")
        claim = client.get("persistentvolumeclaim/" + claims[0], selected["namespace"]) or {}
        volume_name = claim.get("spec", {}).get("volumeName")
        if not isinstance(volume_name, str) or not volume_name: raise ValueError("The database claim is not bound")
        volume = client.get("persistentvolume/" + volume_name) or {}
        claim_uid, volume_uid = claim.get("metadata", {}).get("uid"), volume.get("metadata", {}).get("uid")
        if not deploy.uid(claim_uid) or not deploy.uid(volume_uid): raise ValueError("Database storage identity is missing")
        volumes[name] = {"claim": claims[0], "claim_uid": claim_uid, "volume": volume_name, "volume_uid": volume_uid,
                         "binding": volume.get("spec", {}).get("claimRef"), "csi": volume.get("spec", {}).get("csi")}
    status = current["status"]
    if (status["currentPrimary"] not in fences or not integer(current["metadata"].get("generation"))
            or not isinstance(status.get("currentPrimaryTimestamp"), str)):
        raise ValueError("Primary identity, generation and promotion timestamp must be available")
    promoted = datetime.fromisoformat(status["currentPrimaryTimestamp"].replace("Z", "+00:00"))
    if promoted.tzinfo is None: raise ValueError("Primary timestamp must include its timezone")
    return {"cluster_uid": current["metadata"]["uid"], "generation": current["metadata"].get("generation"),
            "primary": status["currentPrimary"], "primary_since": status.get("currentPrimaryTimestamp"), "pods": fences, "volumes": volumes}


def psql_arguments(major, *, socket="/controller/run", port=5432):
    # Do not inherit PGHOST, PGSERVICE, PGPASSFILE, PGOPTIONS, HOME, or psqlrc.
    # The container's local peer-authenticated socket is intentional. This is
    # not an application-credential or network-client TLS handshake test.
    binary = "/usr/lib/postgresql/" + str(major) + "/bin/psql"
    return ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "LC_ALL=C", "PGCONNECT_TIMEOUT=5", "PGPASSFILE=/dev/null",
            "PGOPTIONS=-c default_transaction_read_only=on", binary, "-X", "-A", "-t", "-q", "--no-password",
            "--set=ON_ERROR_STOP=1", "--dbname=host=" + socket + " port=" + str(port) + " user=postgres dbname=postgres", "--command=" + SQL]


def query(client, selected, name, fence):
    namespace = selected["namespace"]
    if pod_fence(client.get("pod/" + name, namespace), selected) != fence: raise ValueError("Database pod changed before SQL observation")
    result = client.call(["exec", "--namespace", namespace, "pod/" + name, "--container", "postgres", "--",
                          *psql_arguments(int(selected["postgres_version"].split(".")[0]))], seconds=15)
    if len(result.encode()) > 128 * 1024: raise ValueError("Excessive SQL observation output")
    sample = loads_strict_json(result.strip()); common(sample, selected)
    if pod_fence(client.get("pod/" + name, namespace), selected) != fence: raise ValueError("Database pod changed during SQL observation")
    return sample


def primary(sample, selected, view):
    values = common(sample, selected)
    standby_names = set(view["pods"]) - {view["primary"]}
    quorum(values["synchronous_standby_names"], selected["replication"]["standbys"], standby_names)
    if sample["in_recovery"] or sample["receivers"] or len(sample["senders"]) != 2: raise ValueError("Expected a primary with two direct streaming replicas")
    rows = {}
    for row in sample["senders"]:
        if (not isinstance(row, dict) or set(row) != {"application_name", "client_addr", "state", "sync_state", "sync_priority", "flush_lsn", "replay_lsn", "tls", "tls_version", "client_certificate_present"}
                or row["application_name"] not in standby_names or row["application_name"] in rows or row["state"] != "streaming"
                or row["sync_state"] != "quorum" or not integer(row["sync_priority"], 0) or row["tls"] is not True
                or row["tls_version"] not in {"TLSv1.2", "TLSv1.3"} or row["client_certificate_present"] is not True
                or row["client_addr"] not in view["pods"][row["application_name"]]["addresses"]):
            raise ValueError("Streaming replicas do not match the ready peer identities and TLS quorum")
        flush, replay = lsn(row["flush_lsn"]), lsn(row["replay_lsn"])
        if replay > flush: raise ValueError("Invalid replica WAL progress")
        rows[row["application_name"]] = {"flush": flush, "replay": replay}
    return rows


def compare(samples, selected, view):
    name = view["primary"]; first, last = samples[name], samples["primary_recheck"]
    before, after = primary(first, selected, view), primary(last, selected, view)
    identity = (first["system_identifier"], first["timeline_id"])
    if (last["system_identifier"], last["timeline_id"]) != identity or last["postmaster_started_at"] != first["postmaster_started_at"]:
        raise ValueError("Primary process or database identity changed during observation")
    for standby in set(view["pods"]) - {name}:
        sample = samples[standby]; common(sample, selected)
        if (not sample["in_recovery"] or sample["senders"] or len(sample["receivers"]) != 1
                or (sample["system_identifier"], sample["timeline_id"]) != identity): raise ValueError("Replica is not part of the same physical database and timeline")
        row = sample["receivers"][0]
        if (not isinstance(row, dict) or set(row) != {"status", "sender_host", "sender_port", "received_tli", "flushed_lsn", "last_message_age_seconds"}
                or row["status"] != "streaming" or type(row["sender_port"]) is not int or row["sender_port"] != 5432
                or row["sender_host"] not in set(view["pods"][name]["addresses"]) | {selected["name"] + "-rw", selected["name"] + "-rw." + selected["namespace"], selected["name"] + "-rw." + selected["namespace"] + ".svc"}
                or type(row["received_tli"]) is not int or row["received_tli"] != identity[1]
                or type(row["last_message_age_seconds"]) not in {int, float} or not math.isfinite(row["last_message_age_seconds"])
                or not 0 <= row["last_message_age_seconds"] <= 30): raise ValueError("Replica WAL receiver is stale or has an unexpected primary")
        if lsn(row["flushed_lsn"]) < before[standby]["flush"] or after[standby]["flush"] < before[standby]["flush"] or after[standby]["replay"] < before[standby]["replay"]:
            raise ValueError("Replica WAL observation regressed")
    return {"system_identifier": identity[0], "timeline_id": identity[1], "instances_observed": 3, "primary_observations": 2,
            "postgres_version": selected["postgres_version"], "synchronous_standbys_required": selected["replication"]["standbys"],
            "postmaster_started_at": {name: samples[name]["postmaster_started_at"] for name in view["pods"]}}


def snapshot(network, contract, expected, schema, plan, state):
    """Take one identity-fenced read-only sample within the caller's frozen client."""
    selected = contract["plan"]; allowed = {selected["images"]["postgres"], plan["operator"]["image"]}
    deploy.prerequisites(network, plan, contract, schema)
    view = topology(network, selected, expected, state, allowed)
    names = [view["primary"], *sorted(set(view["pods"]) - {view["primary"]})]
    samples = {name: query(network, selected, name, view["pods"][name]) for name in names}
    samples["primary_recheck"] = query(network, selected, view["primary"], view["pods"][view["primary"]])
    evidence = compare(samples, selected, view)
    deploy.prerequisites(network, plan, contract, schema)
    if topology(network, selected, expected, state, allowed) != view: raise ValueError("Database topology changed during observation")
    return view, evidence


def operate(bundle, plan, installation, installation_sha256, *, observe=False, output=None, client=None):
    contract, expected, schema = deploy.load_package(bundle, plan["contract_sha256"])
    deploy.validate(plan, contract, plan["contract_sha256"])
    state = load_installation(installation, installation_sha256, plan)
    expected["metadata"]["annotations"] = {deploy.OPERATION: state["operation_id"], deploy.CONTRACT: plan["contract_sha256"]}
    result = {"local_inputs_verified": True, "read_only": True, "runtime_configuration_observed": False,
              "streaming_quorum_observed": False, **UNVERIFIED}
    if type(observe) is not bool: raise ValueError("Choose an explicit runtime observation")
    if not observe: return result
    if output is None: raise ValueError("Use a new private evidence directory")
    resolved = output.resolve(); relative = resolved.relative_to((ROOT / "private").resolve())
    if not relative.parts or output.is_symlink(): raise ValueError("Use a new private evidence directory")
    resolved.mkdir(mode=0o700, parents=True, exist_ok=False)
    proof = {"schema_version": 1, "command": "observe-postgres-runtime", "contract_sha256": plan["contract_sha256"],
             "deployment_plan_sha256": deploy.fingerprint(plan), "installation_sha256": installation_sha256,
             "query_sha256": hashlib.sha256(SQL.encode()).hexdigest(), "toolset_sha256": deploy.toolset(),
             "verifier_sha256": hashlib.sha256(read_bounded_bytes(Path(__file__), max_bytes=256 * 1024)).hexdigest(), **result}
    def record(phase, **values):
        proof.update(phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **values)
        atomic_write_text(resolved / "postgres-runtime-proof.json", deploy.canonical(proof))
    record("starting")
    try:
        network = client or Client(plan["target"])
        with network.frozen():
            view, evidence = snapshot(network, contract, expected, schema, plan, state)
        # Raw SQL payloads are deliberately not persisted. This is a summary of
        # narrowly scoped observations, never evidence of successful writes.
        result.update(runtime_configuration_observed=True, streaming_quorum_observed=True)
        record("completed", **result, **evidence, cluster_uid=state["cluster_uid"], topology=view)
        return result
    except BaseException:
        record("failed", runtime_configuration_observed=False, streaming_quorum_observed=False,
               error_code="postgres-runtime-unverified", **UNVERIFIED)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True); parser.add_argument("--deployment-plan", type=Path, required=True)
    parser.add_argument("--installation-proof", type=Path, required=True); parser.add_argument("--installation-sha256", required=True)
    parser.add_argument("--observe", action="store_true"); parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.deployment_plan, max_bytes=128 * 1024))
        print(json.dumps(operate(args.bundle_dir, plan, args.installation_proof, args.installation_sha256, observe=args.observe, output=args.output_dir)))
        return 0
    except Exception:
        print("PostgreSQL runtime verification refused; private database details were suppressed.")
        return 1


if __name__ == "__main__": raise SystemExit(main())
