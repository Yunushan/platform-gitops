"""Mount the original, completed CSI canary claims for a point-in-time read."""

import hashlib
import os
from pathlib import Path
import re
import stat
import time
import uuid

from bounded_file import read_bounded_bytes
from ceph_recovery_read import FILES
from strict_json import loads_strict_json
import verify_ceph_clients as canary


program = canary.program
renderer = canary.renderer
storage = canary.shared
policy = renderer.storage


def prepare(plan, proof, proof_sha, storage_plan, runtime, selected):
    objects = renderer.objects(plan, storage_plan, runtime)
    reader = plan["nodes"][1]
    if reader["failure_domain"] == selected["failure_domain"]:
        raise ValueError("Use an original reader outside the selected outage domain")
    info = proof.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077)):
        raise ValueError("Use the original private client receipt")
    raw = program.read_bootstrap_bytes(proof, 256 * 1024, follow_symlinks=False)
    digest = hashlib.sha256(raw).hexdigest()
    state = loads_strict_json(raw)
    if (digest != proof_sha or state["phase"] != "completed" or state["action"] != "create"
            or state["runtime"] != runtime or state["schema_version"] != 1
            or state["operation_id"] != plan["operation_id"] or state["plan_sha256"] != policy.fingerprint(plan)
            or state["toolset_sha256"] != canary.toolset() or not policy.sha(plan["admitted_sha256"])
            or state["admitted_sha256"] != plan["admitted_sha256"]
            or set(state["objects"]) != {storage.key(obj) for obj in objects}
            or set(state["volumes"]) != set(FILES)
            or any(state.get(key) is not True for key in (
                "server_checked", "kubernetes_client_mounts_verified", "kubernetes_data_roundtrip_verified"))):
        raise ValueError("Require the matching completed client canary")
    for obj in objects:
        entry = state["objects"][storage.key(obj)]
        if not policy.uid(entry["uid"]) or not policy.sha(entry["admitted_sha256"]):
            raise ValueError("Missing original canary object identity")
        obj["metadata"]["annotations"] = {canary.OPERATION: state["operation_id"], canary.CONTRACT: state["plan_sha256"]}
    for volume in state["volumes"].values():
        if not policy.uid(volume["uid"]) or not policy.resource(volume["name"]) or not policy.sha(volume["spec_sha256"]):
            raise ValueError("Missing original canary PV identity")
    program_path = Path(__file__).with_name("ceph_recovery_read.py")
    source = read_bounded_bytes(program_path, max_bytes=32 * 1024)
    binding = {"client_proof_sha256": digest, "client_plan_sha256": state["plan_sha256"],
               "reader_toolset_sha256": policy.fingerprint({
                   "runner": hashlib.sha256(read_bounded_bytes(__file__, max_bytes=256 * 1024)).hexdigest(),
                   "program": hashlib.sha256(source).hexdigest(),
               })}
    return binding, state, objects, source.decode()


def check_claims(client, plan, storage_plan, objects, state, allowed_jobs=()):
    ns = client.get("namespace/" + plan["namespace"]) or {}
    node = plan["nodes"][1]
    current = client.get("node/" + node["name"]) or {}
    metadata = current.get("metadata", {})
    if (ns.get("metadata", {}).get("uid") != plan["namespace_uid"] or ns["metadata"].get("deletionTimestamp")
            or ns["metadata"].get("labels", {}).get("pod-security.kubernetes.io/enforce") != "restricted"
            or metadata.get("uid") != node["uid"] or metadata.get("deletionTimestamp")
            or metadata.get("labels", {}).get("kubernetes.io/hostname") != node["name"]
            or metadata.get("labels", {}).get(storage_plan["provider"]["domain_label"]) != node["failure_domain"]
            or not storage.condition(current, "Ready") or current.get("spec", {}).get("unschedulable")
            or current["status"].get("nodeInfo", {}).get("operatingSystem") != "linux"):
        raise ValueError("The original restricted namespace or reader node changed")
    claims = objects[1:3]
    if not all(canary.binding(client, plan, storage_plan, claim, state) for claim in claims):
        raise ValueError("The original canary claims are unavailable")
    names = {claim["metadata"]["name"] for claim in claims}
    for pod in storage.items(client, "pods", plan["namespace"]):
        uses_claim = any(vol.get("persistentVolumeClaim", {}).get("claimName") in names
                         for vol in pod.get("spec", {}).get("volumes", []))
        if (uses_claim and pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
                and not any(storage.controlled(pod, "Job", uid) for uid in allowed_jobs)):
            raise ValueError("Another active pod is using the original canary claims")


def resources(objects, source, phase, attempt):
    config = loads_strict_json(policy.canonical(objects[0]))
    base = config["metadata"]["name"].removesuffix("-program")
    config["metadata"]["name"] = f"{base}-program-{attempt}"
    config["data"]["read.py"] = source
    jobs = {}
    claims = {obj["metadata"]["name"] for obj in objects[1:3]}
    for kind in FILES:
        # Rendered RBD reader policy already pins a non-root pod and the survivor.
        job = loads_strict_json(policy.canonical(objects[3 + program.ROLES.index("rbd-read")]))
        name = f"{base}-{kind}-{attempt}"
        labels = {"platform.gitops/client-role": name}
        job["metadata"]["name"] = name
        job["spec"]["selector"]["matchLabels"] = labels
        job["spec"]["template"]["metadata"]["labels"] = labels
        pod = job["spec"]["template"]["spec"]
        # The original writers set ownership. A read must not request an ownership change.
        pod["securityContext"].pop("fsGroup")
        pod["securityContext"].pop("fsGroupChangePolicy")
        pod["volumes"][0]["configMap"]["name"] = config["metadata"]["name"]
        claim, = (name for name in claims if name.endswith("-" + kind))
        pod["volumes"][1]["persistentVolumeClaim"]["claimName"] = claim
        container = pod["containers"][0]
        container["command"][-1] = "/program/read.py"
        container["args"] = [kind, phase]
        jobs[kind] = job
    return config, jobs


def result(client, plan, job, entry, kind, phase):
    live = storage.get(client, job)
    if live is None:
        raise ValueError("The recovery read Job disappeared")
    check = {"operation_id": plan["operation_id"], "plan_sha256": policy.fingerprint(plan)}
    canary.owned(live, job, check, entry)
    if storage.condition(live, "Failed") or live.get("status", {}).get("failed", 0):
        raise ValueError("The fresh recovery read Job failed")
    if not storage.condition(live, "Complete"):
        return None
    if live["status"].get("succeeded") != 1:
        raise ValueError("The recovery read must complete exactly once")
    namespace = plan["namespace"]
    pod, = [row for row in storage.items(client, "pods", namespace) if storage.controlled(row, "Job", entry["uid"])]
    spec = dict(pod["spec"])
    node = spec.pop("nodeName")
    meta = pod["metadata"]
    container, = pod["status"]["containerStatuses"]
    scope = renderer.scope(plan)
    if (not policy.uid(meta["uid"]) or meta.get("deletionTimestamp") or node != scope["nodes"]["rbd-read"]
            or canary.provider.pod_policy(spec) != canary.provider.pod_policy(job["spec"]["template"]["spec"])
            or pod["status"]["phase"] != "Succeeded" or container["name"] != "canary"
            or container["restartCount"] != 0 or container["state"]["terminated"]["exitCode"] != 0
            or not re.search(r"sha256:[0-9a-f]{64}$", container["imageID"])):
        raise ValueError("The recovery read pod policy or result changed")
    raw = client.call(["logs", "pod/" + meta["name"], "--namespace", namespace,
                       "--container", "canary", "--limit-bytes=16384"])
    wanted = {"phase": phase, "node": node, "scope_sha256": program.digest(scope),
              "filesystem": "ext4" if kind == "rbd" else "ceph",
              "payloads_sha256": {identity: program.expected(scope, identity) for identity in FILES[kind]}}
    if loads_strict_json(raw) != wanted:
        raise ValueError("The recovery read did not verify the original bytes")
    return {"job_uid": entry["uid"], "pod_uid": meta["uid"], **wanted}


def observe(client, plan, storage_plan, prepared, phase, wait_seconds):
    _, state, objects, source = prepared
    check_claims(client, plan, storage_plan, objects, state)
    config, jobs = resources(objects, source, phase, uuid.uuid4().hex[:12])
    resources_to_create = [config, *jobs.values()]
    entries = {}
    for obj in resources_to_create:
        payload = policy.canonical(obj).encode()
        dry_run = loads_strict_json(client.call(["create", "--dry-run=server", "-f", "-", "-o", "json"], payload=payload))
        entries[storage.key(obj)] = {"uid": None, "admitted_sha256": canary.admitted(obj, dry_run)}
    for obj in resources_to_create:
        response = loads_strict_json(client.call(["create", "-f", "-", "-o", "json"], payload=policy.canonical(obj).encode()))
        entry = entries[storage.key(obj)]
        canary.owned(response, obj, state, entry)
        entry["uid"] = response["metadata"]["uid"]
    deadline = time.monotonic() + wait_seconds
    while True:
        check_claims(client, plan, storage_plan, objects, state, [entries[storage.key(job)]["uid"] for job in jobs.values()])
        live = storage.get(client, config)
        if live is None:
            raise ValueError("The recovery reader program disappeared")
        canary.owned(live, config, state, entries[storage.key(config)])
        results = {kind: result(client, plan, job, entries[storage.key(job)], kind, phase) for kind, job in jobs.items()}
        if all(results.values()):
            return results
        if time.monotonic() >= deadline:
            raise TimeoutError("Timed out waiting for the finite recovery reads")
        time.sleep(2)
