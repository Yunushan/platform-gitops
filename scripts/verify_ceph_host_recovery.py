#!/usr/bin/env python3
"""Observe replica recovery across the loss and return of one Ceph storage host."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
from kubernetes_management import Client, verify_target
import provision_ceph_storage as storage
import render_ceph_storage as policy
from strict_json import loads_strict_json
from verify_ceph_clients import storage_receipt
import verify_ceph_recovery_reads as reads


PHASES = ("baseline", "rebuilt", "returned")
PG_STATE = {"active", "clean", "scrubbing", "deep"}
READS = (("osd", "dump"), ("osd", "crush", "dump"), ("status",), ("mon", "dump"),
         ("fs", "dump"), ("pg", "dump", "pgs_brief"), ("osd", "dump"))


def read_json(path):
    return loads_strict_json(read_bounded_bytes(path, max_bytes=1024 * 1024))


def toolbox_handle(client, plan):
    provider = plan["provider"]
    toolbox = provider["toolbox"]
    pod = client.get("pod/" + toolbox["pod"], provider["namespace"]) or {}
    if pod.get("metadata", {}).get("uid") != toolbox["pod_uid"] or not storage.pod_ready(pod, [toolbox["image"]]):
        raise ValueError("The reviewed toolbox is unavailable")
    states = pod["status"]["containerStatuses"]
    handles = [(row.get("containerID"), row.get("restartCount"), row["imageID"]) for row in states]
    if any(not container or not policy.integer(restarts, 0, 1000000) for container, restarts, _ in handles):
        raise ValueError("The toolbox process identity is missing")
    return handles


def collect(client, plan, expected_objects, receipt):
    verify_target(client, plan["target"])
    provider = plan["provider"]
    namespace = provider["namespace"]
    namespace_obj = client.get("namespace/" + namespace)
    cluster = client.get("cephcluster.ceph.rook.io/" + provider["cluster"], namespace)
    for obj, uid in ((namespace_obj, provider["namespace_uid"]), (cluster, provider["cluster_uid"])):
        if not obj or obj["metadata"]["uid"] != uid or obj["metadata"].get("deletionTimestamp"):
            raise ValueError("The original Ceph provider identity changed")
    if policy.fingerprint(cluster.get("spec", {})) != provider["spec_sha256"]:
        raise ValueError("The Ceph provider specification changed")
    for expected in expected_objects:
        current = storage.get(client, expected)
        if current is None:
            raise ValueError("An original storage resource is missing")
        storage.owned(current, expected, receipt, receipt["objects"][storage.key(expected)])

    ready = {}
    for expected in plan["nodes"]:
        node = client.get("node/" + expected["name"]) or {}
        meta = node.get("metadata", {})
        if (meta.get("uid") != expected["uid"] or meta.get("deletionTimestamp")
                or meta.get("labels", {}).get(provider["domain_label"]) != expected["failure_domain"]):
            raise ValueError("A storage host identity or failure domain changed")
        conditions = [row.get("status") for row in node.get("status", {}).get("conditions", [])
                      if row.get("type") == "Ready"]
        if len(conditions) != 1 or conditions[0] not in {"True", "False", "Unknown"}:
            raise ValueError("Missing explicit storage node readiness")
        ready[expected["crush_host"]] = conditions[0] == "True"

    toolbox = provider["toolbox"]
    original_handle = toolbox_handle(client, plan)
    command = ["exec", "pod/" + toolbox["pod"], "--namespace", namespace,
               "--container", toolbox["container"], "--", "env", "-i",
               "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL=C",
               "ceph", "--conf=/etc/ceph/ceph.conf"]
    osd, crush, status, mon, filesystem, pg_dump, after = (
        loads_strict_json(client.call([*command, *query, "--format=json"], seconds=30)) for query in READS)
    if osd != after or original_handle != toolbox_handle(client, plan):
        raise ValueError("The OSD map or toolbox changed during the sample")
    return osd, crush, status, mon, filesystem, pg_dump["pg_stats"], ready


def assess(plan, expected, observation, lost_host, phase, baseline=None, rebuilt=None):
    osd, crush, status, mon, filesystem, pgs, ready = observation
    fsid = plan["provider"]["fsid"]
    known_mons = {row["name"] for row in mon["mons"]}
    quorum = set(status["quorum_names"])
    outage = phase == "rebuilt"
    if (osd["fsid"] != fsid or status["fsid"] != fsid or mon["fsid"] != fsid
            or not policy.integer(osd.get("epoch"), 1, 2 ** 63 - 1)
            or status["monmap"]["epoch"] != mon["epoch"]
            or len(mon["mons"]) != 3 or len(known_mons) != 3 or not quorum <= known_mons
            or len(quorum) < (2 if outage else 3)
            or status["health"]["status"] not in ({"HEALTH_OK", "HEALTH_WARN"} if outage else {"HEALTH_OK"})):
        raise ValueError("The reviewed Ceph identity, quorum, or health requirement failed")
    unready_hosts = {host for host, is_ready in ready.items() if not is_ready}
    if unready_hosts != ({lost_host} if outage else set()):
        raise ValueError("Storage host readiness does not match the selected drill phase")

    identities = {}
    for row in osd["osds"]:
        number = row["osd"]
        if (not policy.integer(number, 0, 2 ** 31 - 2) or number in identities
                or not policy.uid(row["uuid"]) or row["uuid"] == "00000000-0000-0000-0000-000000000000"):
            raise ValueError("Invalid OSD identity")
        identities[number] = row
    placements = {}
    if len({row["uuid"] for row in identities.values()}) != len(identities):
        raise ValueError("OSD UUIDs must be distinct")
    for host in ready:
        bucket, = (row for row in crush["buckets"] if row.get("name") == host and row.get("type_name") == "host")
        if not bucket["items"]:
            raise ValueError("An original host lacks OSDs")
        for item in bucket["items"]:
            number = item["id"]
            if number in placements or number not in identities:
                raise ValueError("The CRUSH host map does not cover a distinct OSD inventory")
            placements[number] = host
    if set(placements) != set(identities):
        raise ValueError("The reviewed hosts must account for every OSD")
    for number, host in placements.items():
        row = identities[number]
        wanted = 0 if outage and host == lost_host else 1
        if not policy.integer(row.get("up"), wanted, wanted) or not policy.integer(row.get("in"), wanted, wanted):
            raise ValueError("OSDs do not match the expected down/out or up/in state")

    names = plan["names"]
    pools = {names["block_pool"]: expected[0]["spec"],
             names["filesystem"] + "-metadata": expected[1]["spec"]["metadataPool"],
             names["filesystem"] + "-data": expected[1]["spec"]["dataPools"][0]}
    pool_ids = {}
    by_id = {}
    for name, spec in pools.items():
        pool, = (row for row in osd["pools"] if row["pool_name"] == name)
        number = pool["pool"]
        if (not policy.integer(number, 1, 2 ** 63 - 1) or number in by_id
                or not policy.integer(pool.get("pg_num"), 1, 65536)
                or not storage.pool_ready(pool, spec, crush)):
            raise ValueError("An original pool's replication, placement or quota differs")
        pool_ids[name] = number
        by_id[number] = pool["pg_num"]

    fs, = (row for row in filesystem["filesystems"] if row["mdsmap"]["fs_name"] == names["filesystem"])
    mds = fs["mdsmap"]
    if (not policy.integer(fs.get("id"), 1, 2 ** 63 - 1)
            or mds["metadata_pool"] != pool_ids[names["filesystem"] + "-metadata"]
            or mds["data_pools"] != [pool_ids[names["filesystem"] + "-data"]]
            or mds["max_mds"] != 1
            or sum(row["state"] == "up:active" for row in mds["info"].values()) != 1):
        raise ValueError("The original filesystem lacks its expected pools and active metadata server")

    pg_ids = set()
    counts = dict.fromkeys(by_id, 0)
    host_pgs = dict.fromkeys(by_id, 0)
    if not isinstance(pgs, list) or not pgs:
        raise ValueError("Missing placement group inventory")
    for row in pgs:
        match = re.fullmatch(r"([0-9]+)[.][0-9a-f]+", row["pgid"])
        if not match:
            raise ValueError("Invalid placement group ID")
        number = int(match[1])
        if number not in by_id:
            continue
        flags = set(row["state"].split("+"))
        acting = row["acting"]
        if (row["pgid"] in pg_ids or len(acting) != 3 or acting != row["up"]
                or row["acting_primary"] != acting[0] or row["up_primary"] != acting[0]
                or any(type(i) is not int or i not in placements for i in acting)
                or not {"active", "clean"} <= flags <= PG_STATE
                or len({placements[i] for i in acting}) != 3
                or any(identities[i]["up"] != 1 or identities[i]["in"] != 1 for i in acting)):
            raise ValueError("A placement group lacks three clean, live host-separated replicas")
        host_pgs[number] += int(any(placements[i] == lost_host for i in acting))
        pg_ids.add(row["pgid"])
        counts[number] += 1
    if counts != by_id:
        raise ValueError("Incomplete placement group inventory")
    if phase == "baseline" and not all(host_pgs.values()):
        raise ValueError("Select a host that holds replicas for each original pool")
    identity = {"osd_hosts_and_uuids_sha256": policy.fingerprint(sorted(
                    (i, placements[i], row["uuid"]) for i, row in identities.items())),
                "pool_ids_sha256": policy.fingerprint(pool_ids), "pg_ids_sha256": policy.fingerprint(sorted(pg_ids)),
                "filesystem_id": fs["id"], "monitor_names_sha256": policy.fingerprint(sorted(known_mons))}
    if phase != "baseline" and (identity != baseline["identity"] or osd["epoch"] <= baseline["osd_epoch"]):
        raise ValueError("The original OSD/pool/PG inventory changed or the map did not advance")
    if phase == "returned" and (identity != rebuilt["identity"] or osd["epoch"] <= rebuilt["osd_epoch"]):
        raise ValueError("The returning host was not observed in a newer OSD map")
    return {"identity": identity, "osd_epoch": osd["epoch"], "pool_count": len(pool_ids),
            "pg_count": len(pg_ids), "pgs_on_selected_host": sum(host_pgs.values()), "monitors_in_quorum": len(quorum)}


def operate(plan, runtime, storage_proof, storage_sha, lost_node, phase, output, *,
            client=None, client_inputs=None, read_wait=600):
    policy.validate(plan, runtime)
    if phase not in PHASES or not policy.sha(storage_sha):
        raise ValueError("Select a phase and the reviewed storage receipt SHA-256")
    selected, = (row for row in plan["nodes"] if row["name"] == lost_node)
    plan_sha = policy.fingerprint(plan)
    receipt, objects = storage_receipt({"storage_proof_sha256": storage_sha, "storage_plan_sha256": plan_sha},
                                       plan, runtime, storage_proof)
    prepared = None
    if client_inputs is not None:
        client_plan, client_proof, client_sha = client_inputs
        if client_plan["storage_proof_sha256"] != storage_sha or not policy.integer(read_wait, 5, 1800):
            raise ValueError("Use the original storage receipt and a bounded read deadline")
        prepared = reads.prepare(client_plan, client_proof, client_sha, plan, runtime, selected)
    directory = output.resolve()
    if directory == (storage.ROOT / "private").resolve() or not directory.is_relative_to((storage.ROOT / "private").resolve()) or output.is_symlink():
        raise ValueError("Keep recovery evidence in private storage")
    if phase == "baseline":
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    elif not directory.is_dir():
        raise ValueError("Use the original private recovery directory")
    evidence = directory / "ceph-host-recovery.json"
    info = directory.stat()
    if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise ValueError("Use a private recovery directory owned by the operator")
    binding = {"schema_version": 1, "runtime": runtime, "storage_plan_sha256": plan_sha, "storage_proof_sha256": storage_sha,
               "storage_operation": receipt["operation_id"], "selected_node_uid": selected["uid"],
               "observer_sha256": hashlib.sha256(read_bounded_bytes(__file__, max_bytes=256 * 1024)).hexdigest(),
               "storage_toolset_sha256": receipt["toolset_sha256"]}
    if prepared is not None:
        binding["canary"] = prepared[0]
    with storage.lock(directory):
        if evidence.is_symlink() or (phase == "baseline" and evidence.exists()):
            raise ValueError("Select a fresh evidence path for the baseline")
        if phase != "baseline":
            prior = evidence.lstat()
            if not stat.S_ISREG(prior.st_mode) or prior.st_nlink != 1 or (
                    os.name == "posix" and (prior.st_uid != os.geteuid() or prior.st_mode & 0o077)):
                raise ValueError("Use the original private recovery evidence")
        state = {**binding, "observations": {}} if phase == "baseline" else read_json(evidence)
        samples = state["observations"]
        if (any(state.get(k) != v for k, v in binding.items())
                or (prepared is None and "canary" in state)
                or set(samples) != set(PHASES[:PHASES.index(phase)])):
            raise ValueError("Observe the same immutable recovery scope in phase order")
        connection = client or Client(plan["target"])
        with connection.frozen():
            def sample():
                observation = collect(connection, plan, objects, receipt)
                return assess(plan, objects, observation, selected["crush_host"], phase,
                              samples.get("baseline"), samples.get("rebuilt"))

            observation = sample()
            if prepared is not None:
                result = reads.observe(connection, client_plan, plan, prepared, phase, read_wait)
                if sample() != observation:
                    raise ValueError("Ceph recovery state changed during the canary reads")
                prior_pods = {entry["pod_uid"] for prior in samples.values() for entry in prior["canary_reads"].values()}
                seen_pods = {entry["pod_uid"] for entry in result.values()}
                if len(seen_pods) != len(result) or seen_pods & prior_pods:
                    raise ValueError("Each phase requires fresh reads")
                observation["canary_reads"] = result
        samples[phase] = {**observation, "observed_at": datetime.now(timezone.utc).isoformat()}
        state["replica_rebuild_observed"] = "rebuilt" in samples
        state["original_host_return_observed"] = "returned" in samples
        state["application_io_verified"] = False
        state["ha_verified"] = False
        atomic_write_text(evidence, policy.canonical(state))
    return {"phase": phase, "pool_count": samples[phase]["pool_count"], "pg_count": samples[phase]["pg_count"],
            "replica_rebuild_observed": state["replica_rebuild_observed"],
            "original_host_return_observed": state["original_host_return_observed"],
            "canary_reads_verified": prepared is not None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-plan", type=Path, required=True)
    parser.add_argument("--storage-proof", type=Path, required=True)
    parser.add_argument("--storage-proof-sha256", required=True)
    parser.add_argument("--runtime", choices=policy.RUNTIMES, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--phase", choices=PHASES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--client-plan", type=Path)
    parser.add_argument("--client-proof", type=Path)
    parser.add_argument("--client-proof-sha256")
    parser.add_argument("--read-wait-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    client_args = (args.client_plan, args.client_proof, args.client_proof_sha256)
    if any(client_args) and not all(client_args):
        parser.error("Provide all three client canary arguments together")
    try:
        client_inputs = (read_json(args.client_plan), *client_args[1:]) if all(client_args) else None
        print(json.dumps(operate(read_json(args.storage_plan), args.runtime, args.storage_proof, args.storage_proof_sha256,
                                 args.host, args.phase, args.output_dir, client_inputs=client_inputs,
                                 read_wait=args.read_wait_seconds)))
        return 0
    except (OSError, KeyError, IndexError, AttributeError, ValueError, TypeError, RuntimeError):
        print("Ceph recovery evidence refused. Check the original scope, phase and current replica health.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
