#!/usr/bin/env python3
"""Opt-in, separate-identity reporting of verified terminal Tekton outcomes."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote, urlencode
from urllib.request import Request

from http_transport import open_http_request, read_bounded_response
from strict_json import loads_strict_json
import tekton_forge_bridge as bridge

INPUTS = Path("/run/platform/reporter")

def uid(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value):
        raise ValueError("Missing controller object UID")
    return value


def instant(value) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Missing bounded completion timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Completion timestamp has no time zone")
    return result


def terminal(obj: dict) -> str | None:
    conditions = [c for c in obj.get("status", {}).get("conditions", []) if c.get("type") == "Succeeded"]
    if not conditions or len(conditions) == 1 and conditions[0].get("status") == "Unknown":
        return None
    if len(conditions) != 1 or conditions[0].get("status") not in {"True", "False"}:
        raise ValueError("Ambiguous completion condition")
    instant(obj["status"].get("completionTime"))
    return "success" if conditions[0]["status"] == "True" else "failure"


def identity(client, contract: dict, template: dict, digest: str, run: dict) -> tuple[dict, dict]:
    params = run.get("spec", {}).get("params", [])
    if len(params) != 1 or params[0].get("name") != "revision":
        raise ValueError("Run does not contain the sole reviewed revision parameter")
    expected, data = bridge.event_run(contract, template, digest, params[0].get("value"))
    meta = run.get("metadata", {})
    if (meta.get("name") != data["run"] or meta.get("namespace") != contract["plan"]["namespace"]
            or meta.get("annotations", {}).get(bridge.OWNER) != digest
            or meta.get("labels", {}).get(bridge.OWNER) != digest[:32] or not bridge.includes(expected["spec"], run.get("spec"))):
        raise ValueError("Run spec or event ownership differs from the reviewed contract")
    data["uid"] = uid(meta.get("uid"))
    bridge.receipt(client, contract["plan"]["namespace"], data["run"] + "-done", digest, data, create=False)
    return expected, data


def owned(child: dict, kind: str, parent_uid: str) -> bool:
    return any(o.get("kind") == kind and o.get("uid") == parent_uid and o.get("controller") is True
               for o in child.get("metadata", {}).get("ownerReferences", []))


def substitute(value, revision, patchset_ref=None):
    if isinstance(value, dict):
        return {k: substitute(v, revision, patchset_ref) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, revision, patchset_ref) for v in value]
    if patchset_ref is not None and value == "$(params.patchset-ref)": return patchset_ref
    return revision if value == "$(params.revision)" else value


def inline_task_spec(embedded: dict) -> dict:
    if not isinstance(embedded, dict): raise ValueError("Missing reviewed inline task")
    result = copy.deepcopy(embedded)
    if result.pop("metadata", {}) != {} or result.pop("spec", None) is not None:
        raise ValueError("Custom-task content or inline task metadata is outside the supported execution contract")
    return result


def success_evidence(client, plan: dict, expected: dict, run: dict, data: dict, *, publication=False) -> dict:
    children = run.get("status", {}).get("childReferences", [])
    if (len(children) != 1 or children[0].get("kind") != "TaskRun" or children[0].get("pipelineTaskName") != "build"
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", children[0].get("name", ""))):
        raise ValueError("Successful run has no sole build TaskRun")
    task = client.get("taskrun.tekton.dev/" + children[0]["name"], plan["namespace"])
    if not task or terminal(task) != "success" or not owned(task, "PipelineRun", data["uid"]):
        raise ValueError("Build TaskRun ownership or completion did not verify")
    task_uid = uid(task["metadata"].get("uid"))
    patchset_ref = data.get("ref")
    expected_task = substitute(inline_task_spec(expected["spec"]["pipelineSpec"]["tasks"][0]["taskSpec"]), data["revision"], patchset_ref)
    if publication:
        from gerrit_artifact_contract import resolve, verify_receipt
        expected_task = resolve(expected_task, data)
    expected_params = [{"name": "revision", "value": data["revision"]}]
    if patchset_ref is not None: expected_params.append({"name": "patchset-ref", "value": patchset_ref})
    if (task.get("spec", {}).get("serviceAccountName") != plan["service_account"]
            or task["spec"].get("params") != expected_params
            or not bridge.includes(expected_task, resolve(task["spec"].get("taskSpec"), data) if publication else substitute(task["spec"].get("taskSpec"), data["revision"], patchset_ref))
            or task["spec"].get("taskRef") or task["spec"].get("stepSpecs") or task["spec"].get("sidecarSpecs")):
        raise ValueError("Executed TaskRun differs from the reviewed inline task")
    pod_name = task["status"].get("podName", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,252}", pod_name):
        raise ValueError("TaskRun has no valid worker reference")
    pod = client.get("pod/" + pod_name, plan["namespace"])
    if (not pod or not owned(pod, "TaskRun", task_uid) or pod.get("status", {}).get("phase") != "Succeeded"
            or pod.get("spec", {}).get("serviceAccountName") != plan["service_account"]):
        raise ValueError("Worker Pod is not the completed child of this TaskRun")
    pod_uid = uid(pod["metadata"].get("uid"))
    spec = pod["spec"]
    if any(spec.get(k) for k in ("hostNetwork", "hostPID", "hostIPC", "shareProcessNamespace")):
        raise ValueError("Worker unexpectedly shares host/process namespaces")
    if (not bridge.includes(expected["spec"]["taskRunTemplate"]["podTemplate"]["securityContext"], spec.get("securityContext"))
            or spec.get("automountServiceAccountToken") is True):
        raise ValueError("Worker security context differs from the job contract")
    containers = spec.get("containers", [])
    reviewed_steps = ["checkout", "build", "publish"] if publication else ["checkout", "build"]
    if [v.get("name") for v in expected_task["steps"]] != reviewed_steps or len(containers) != len(reviewed_steps) or {c.get("name") for c in containers} != {"step-" + name for name in reviewed_steps}:
        raise ValueError("Unexpected worker steps or sidecars")
    task_volumes = {v["name"]: v for v in expected_task.get("volumes", [])}
    reviewed_secrets = {name: v["secret"] for name,v in task_volumes.items() if "secret" in v}
    if publication and any(any(m.get("name") in reviewed_secrets for m in c.get("volumeMounts", [])) for c in spec.get("initContainers", [])):
        raise ValueError("Helper initialization must not mount publication or source credentials")
    initializers = spec.get("initContainers", [])
    if not initializers or any(c.get("image") not in bridge.reporting_settings(plan)["helper_images"] for c in initializers):
        raise ValueError("Entrypoint/working-directory init images are outside the reviewed helper inventory")
    init_images = []
    for container in initializers:
        states = [s for s in pod["status"].get("initContainerStatuses", []) if s.get("name") == container.get("name")]
        if (len(states) != 1 or type(states[0].get("state", {}).get("terminated", {}).get("exitCode")) is not int
                or states[0]["state"]["terminated"]["exitCode"] != 0
                or not re.search(r"sha256:[0-9a-f]{64}$", states[0].get("imageID", ""))):
            raise ValueError("Reviewed helper initialization has no successful image identity")
        init_images.append({"name": container["name"], "image_id": states[0]["imageID"]})
    exits = []
    for index, expected_step in enumerate(expected_task["steps"]):
        if publication: expected_step = resolve(expected_step, data, pod=True)
        name = expected_step["name"]
        container = next(c for c in containers if c["name"] == "step-" + name)
        args = container.get("args", [])
        command = expected_step["command"]
        separator = args.index("--") if "--" in args else -1
        prefix = args[:separator] if separator >= 0 else []
        def flag_value(flag):
            if prefix.count(flag) != 1 or prefix.index(flag) + 1 >= len(prefix):
                raise ValueError("Worker entrypoint has no unique execution/order flag")
            return prefix[prefix.index(flag) + 1]
        sensitive = {"entrypoint", "on_error", "wait_file", "post_file", "when_expressions", "breakpoint_on_failure", "debug_before_step"}
        if any(arg.startswith("-") and arg.lstrip("-").split("=", 1)[0] in sensitive
               and (arg.startswith("--") or "=" in arg) for arg in prefix):
            raise ValueError("Unexpected alternate entrypoint flag syntax")
        if flag_value("-post_file") != "/tekton/run/" + str(index) + "/out":
            raise ValueError("Worker post-step ordering differs from the supported controller")
        if index and flag_value("-wait_file") != "/tekton/run/" + str(index - 1) + "/out":
            raise ValueError("Build did not wait for checkout completion")
        if not index and "-wait_file" in prefix and flag_value("-wait_file") != "/tekton/downward/ready":
            raise ValueError("Checkout readiness wait differs from the supported controller")
        if (container.get("image") != expected_step["image"] or container.get("command") != ["/tekton/bin/entrypoint"]
                or prefix.count("-entrypoint") != 1 or prefix.index("-entrypoint") + 1 >= len(prefix)
                or prefix[prefix.index("-entrypoint") + 1] != command[0]
                or args[separator + 1:] != command[1:] + expected_step.get("args", [])
                or any(flag in prefix for flag in ("--when_expressions", "-breakpoint_on_failure", "-debug_before_step"))
                or "-on_error" in prefix and flag_value("-on_error") != "stopAndFail"
                or container.get("workingDir") != expected_step.get("workingDir", container.get("workingDir"))
                or not bridge.includes(expected_step["securityContext"], container.get("securityContext"))
                or container.get("securityContext", {}).get("privileged")
                or container.get("securityContext", {}).get("capabilities", {}).get("add")
                or container.get("securityContext", {}).get("runAsUser", 1000) != 1000):
            raise ValueError("Worker step image, command or permissions differ from the fixed pipeline")
        if (container.get("envFrom") or any("secretKeyRef" in e.get("valueFrom", {}) for e in container.get("env", []))
                or any(not any(bridge.includes(v, actual) for actual in container.get("volumeMounts", [])) for v in expected_step["volumeMounts"])):
            raise ValueError("Worker step mounts or environment expose unexpected credentials")
        if not any(v.get("mountPath") == "/tekton/bin" and v.get("readOnly") is True for v in container.get("volumeMounts", [])):
            raise ValueError("Worker entrypoint binaries are not mounted read-only")
        if publication:
            expected_mounts = {v["name"]: v for v in expected_step["volumeMounts"]}
            for mounted in container.get("volumeMounts", []):
                if mounted.get("name") in task_volumes and (mounted["name"] not in expected_mounts or not bridge.includes(expected_mounts[mounted["name"]], mounted)):
                    raise ValueError("A task volume crossed its reviewed step boundary")
        if name == "build" and any(v.get("name") == "source" for v in container.get("volumeMounts", [])):
            raise ValueError("Build step unexpectedly mounts the checkout credential")
        states = [s for s in pod["status"].get("containerStatuses", []) if s.get("name") == "step-" + name]
        task_states = [s for s in task["status"].get("steps", []) if s.get("name") == name]
        if (len(states) != 1 or len(task_states) != 1
                or any(type(s.get("terminated", {}).get("exitCode")) is not int or s["terminated"]["exitCode"] != 0
                       or s["terminated"].get("reason", "Completed") != "Completed"
                       for s in [states[0].get("state", {}), task_states[0]])
                or not re.search(r"sha256:[0-9a-f]{64}$", states[0].get("imageID", ""))):
            raise ValueError("Both checkout and build must have recorded zero exit status and an image identity")
        if publication and "provider" in plan and name == "publish":
            digest = expected_step["image"].rsplit("@", 1)[1]
            if any(state.get("imageID", "").rsplit("@", 1)[-1] != digest for state in (states[0], task_states[0])):
                raise ValueError("Publisher image ID differs from the reviewed platform digest")
        exits.append({"step": name, "exit_code": 0, "image_id": states[0]["imageID"]})
    for volume in spec.get("volumes", []):
        if "secret" in volume:
            secret = volume["secret"]
            if publication:
                selected = reviewed_secrets.get(volume.get("name"))
                if not selected or not bridge.includes(selected, secret): raise ValueError("Worker mounts an unexpected publication Secret")
            elif (volume.get("name") != "source" or secret.get("secretName") != plan["read_secret"]
                    or secret.get("items") != [{"key": "token", "path": "token"}, {"key": "ca.crt", "path": "ca.crt"}]):
                raise ValueError("Worker mounts an unexpected Secret")
        elif not any(k in volume for k in ("emptyDir", "configMap", "downwardAPI", "projected")):
            raise ValueError("Worker mounts unexpected external or host storage")
        if any("secret" in item or "serviceAccountToken" in item for item in volume.get("projected", {}).get("sources", [])):
            raise ValueError("Worker unexpectedly mounts projected credentials")
    node_name = spec.get("nodeName", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", node_name):
        raise ValueError("Worker has no node assignment")
    node = client.get("node/" + node_name)
    labels = (node or {}).get("metadata", {}).get("labels", {})
    if (any(labels.get(k) != v for k, v in plan["node_selector"].items())
            or any(k in labels for k in ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master"))
            or instant(node["metadata"].get("creationTimestamp")) > instant(pod["status"].get("startTime"))):
        raise ValueError("Worker node identity or placement did not verify")
    proof = {"task_uid": task_uid, "pod_uid": pod_uid, "node_uid": uid(node["metadata"].get("uid")), "steps": exits, "init_images": init_images}
    if publication and "provider" in plan:
        proof["artifact"] = verify_receipt(plan, data, task, run)
    return proof


def evidence(client, contract, template, digest, run) -> tuple[dict, dict]:
    expected, data = identity(client, contract, template, digest, run)
    state = terminal(run)
    if state is None:
        return data, {"state": "pending"}
    proof = {**data, "state": state, "completion_time": run["status"]["completionTime"]}
    if state == "success":
        if run["spec"].get("status"):
            raise ValueError("Cancelled run cannot establish success")
        plan = contract["plan"]
        proof.update(success_evidence(client, plan, expected, run, data, publication=plan["schema_version"] == 2))
    return data, proof


def forge_request(plan: dict, suffix: str, payload: dict | None = None) -> tuple[dict, dict]:
    token = bridge.private_input("status-token", minimum=16, maximum=512, root=INPUTS).decode("ascii")
    if any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("Invalid private status token")
    request = Request(plan["source"]["api_url"] + suffix, data=bridge.canonical(payload) if payload is not None else None,
                      headers={"Authorization": "token " + token, "Accept": "application/json", "Content-Type": "application/json"})
    with open_http_request(request, timeout=10, use_environment_proxy=False, tls_context=bridge.source_tls(plan, INPUTS)) as response:
        data = loads_strict_json(read_bounded_response(response, max_bytes=256 * 1024))
        headers = dict(response.headers)
    if not isinstance(data, dict):
        raise ValueError("Forge response is not a status object")
    return data, {k.casefold(): v for k, v in headers.items()}


def repo_base(plan):
    return "/repos/" + "/".join(quote(p, safe="") for p in plan["source"]["repository"].split("/"))


def latest_status(plan: dict, revision: str, context: str, *, requester=None) -> dict | None:
    requester = requester or forge_request
    for page in range(1, 11):
        result, headers = requester(plan, repo_base(plan) + "/commits/" + revision + "/status?" + urlencode({"page": page, "limit": 50}))
        rows = result.get("statuses") or []
        if not isinstance(rows, list) or len(rows) > 50 or rows and result.get("sha") != revision:
            raise ValueError("Combined status response has no matching commit identity")
        matches = [row for row in rows if row.get("context") == context]
        if len(matches) > 1:
            raise ValueError("Status context has ambiguous latest outcomes")
        if matches:
            return matches[0]
        if 'rel="next"' not in headers.get("link", "") and len(rows) < 50:
            return None
    raise ValueError("Status context is outside the bounded pagination window")


def same_status(actual, expected):
    # The create API calls this field "state". Individual status objects
    # returned by the Forgejo/Gitea API call it "status".
    return actual is not None and all(
        actual.get("status" if key == "state" else key, "") == value
        for key, value in expected.items()
    )


def report_run(client, contract, template, digest, run) -> str:
    plan = contract["plan"]
    if not bridge.reporting_settings(plan)["enabled"]:
        raise ValueError("Status reporting is not enabled")
    expected, data = identity(client, contract, template, digest, run)
    state = terminal(run)
    if state is None:
        return "pending"
    context = bridge.reporting_settings(plan)["context"] + "/" + digest[:12]
    payload = {"context": context, "state": state, "description": "Tekton " + state + "; run " + data["uid"], "target_url": ""}
    base_record = {**data, "state": state, "context": context, "payload_sha256": hashlib.sha256(bridge.canonical(payload)).hexdigest()}
    name, namespace = data["run"], plan["namespace"]
    recorded = client.get("configmap/" + name + "-report", namespace, optional=True)
    repository, _ = forge_request(plan, repo_base(plan))
    if type(repository.get("id")) is not int or repository["id"] != plan["source"]["repository_id"] or repository.get("full_name") != plan["source"]["repository"]:
        raise ValueError("Status token does not resolve the reviewed repository identity")
    actual = latest_status(plan, data["revision"], context)
    if recorded is not None:
        record = recorded.get("data", {})
        if (set(record) != {*base_record, "evidence_sha256"} or not bridge.includes(base_record, record)
                or not re.fullmatch(r"[0-9a-f]{64}", record.get("evidence_sha256", ""))):
            raise ValueError("Recorded status evidence differs from this terminal run")
        bridge.receipt(client, namespace, name + "-report", digest, record, create=False)
        if not same_status(actual, payload):
            raise ValueError("Published status drifted; no status was overwritten")
        return "already-reported"
    _, proof = evidence(client, contract, template, digest, run)
    record = {**base_record, "evidence_sha256": hashlib.sha256(bridge.canonical(proof)).hexdigest()}
    if actual is not None and not same_status(actual, payload):
        raise ValueError("Status context is already occupied by another outcome")
    if not same_status(actual, payload):
        current = client.get("pipelinerun.tekton.dev/" + name, namespace)
        _, refreshed = evidence(client, contract, template, digest, current)
        if refreshed != proof:
            raise ValueError("Controller evidence changed before publication")
        if not bridge.receipt(client, namespace, name + "-rlock", digest, record, create=True):
            if not same_status(latest_status(plan, data["revision"], context), payload):
                raise ValueError("Ambiguous status reservation needs explicit recovery")
        else:
            try:
                forge_request(plan, repo_base(plan) + "/statuses/" + data["revision"], payload)
            except Exception:
                pass  # A lost response is not proof that the append failed.
            if not same_status(latest_status(plan, data["revision"], context), payload):
                raise ValueError("Status append did not verify; reservation retained")
    bridge.receipt(client, namespace, name + "-report", digest, record, create=True)
    return "reported"


def poll(client, contract, template, digest, cursor="") -> tuple[dict, str]:
    settings = bridge.reporting_settings(contract["plan"])
    if not settings["enabled"]:
        raise ValueError("Status reporting is not enabled")
    if not isinstance(cursor, str) or len(cursor) > 4096:
        raise ValueError("Invalid bounded continuation token")
    query = urlencode({"limit": settings["max_runs_per_poll"], "labelSelector": bridge.OWNER + "=" + digest[:32], "continue": cursor})
    path = "/apis/tekton.dev/v1/namespaces/" + contract["plan"]["namespace"] + "/pipelineruns?" + query
    result = loads_strict_json(client.call(["get", "--raw", path]))
    if not isinstance(result, dict) or result.get("apiVersion") != "tekton.dev/v1" or result.get("kind") != "PipelineRunList":
        raise ValueError("Management API returned no PipelineRun list")
    items = result.get("items", [])
    if not isinstance(items, list) or len(items) > settings["max_runs_per_poll"]:
        raise ValueError("Run list exceeds the reviewed pagination bound")
    summary = {"pending": 0, "reported": 0, "already-reported": 0, "rejected": 0}
    for run in items:
        try:
            summary[report_run(client, contract, template, digest, run)] += 1
        except Exception:
            summary["rejected"] += 1
    next_cursor = result.get("metadata", {}).get("continue", "")
    if not isinstance(next_cursor, str) or len(next_cursor) > 4096:
        raise ValueError("Invalid server continuation token")
    return summary, next_cursor


def operate(root: Path, digest: str, *, once=False, watch=False) -> dict:
    contract, template = bridge.load_bundle(root, digest)
    bridge.verify_running_sources(contract, Path(__file__))
    if not once and not watch:
        return {"local_inputs_verified": True, "read_only": True, "result_reporting_verified": False}
    if not bridge.reporting_settings(contract["plan"])["enabled"] or os.name != "posix" or os.geteuid() == 0:
        raise ValueError("Enable the separate reporter and use a non-root Linux service identity")
    bridge.private_input("status-token", minimum=16, maximum=512, root=INPUTS)
    with bridge.frozen_target(contract["plan"], inputs=INPUTS):
        client = bridge.Client(contract["plan"]["management_target"])
        bridge.verify_target(client, contract["plan"]["management_target"])
        namespace = client.get("namespace/" + contract["plan"]["namespace"])
        if not namespace or namespace.get("metadata", {}).get("labels", {}).get("pod-security.kubernetes.io/enforce") != "restricted":
            raise ValueError("Reporter requires the restricted job namespace")
        cursor = ""
        while True:
            summary, cursor = poll(client, contract, template, digest, cursor)
            if once:
                return {"read_only": False, "page": summary, "has_more": bool(cursor), "result_reporting_verified": False}
            print(json.dumps({"reporter_page": summary}), flush=True)
            time.sleep(bridge.reporting_settings(contract["plan"])["poll_seconds"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--contract-sha256", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--report-once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = operate(args.bundle_dir, args.contract_sha256, once=args.report_once, watch=args.watch)
        print(json.dumps(result))
        return 1 if result.get("page", {}).get("rejected") else 0
    except Exception:
        print("Tekton status reporting stopped; inspect private credentials, controller evidence, receipts and forge status drift.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
