#!/usr/bin/env python3
"""Versioned Tekton static publication contracts and result binding."""

import base64
import hashlib
import re

import publish_harbor_static_image as publisher
import source_bound_static_image as source_binding
from strict_json import loads_strict_json


EXTRA_PROGRAMS = ("gerrit_artifact_contract.py", "source_bound_static_image.py", "publish_harbor_static_image.py")


def validate(plan):
    choice = plan["publication"]
    publisher.exact(choice, "publisher_image robot_secret trust_secret registry")
    if not isinstance(choice["publisher_image"], str) or not re.fullmatch(r"[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}", choice["publisher_image"]): raise ValueError("Pin the trusted publisher image")
    for key in ("robot_secret", "trust_secret"):
        if not isinstance(choice[key], str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", choice[key]) or len(choice[key]) > 63: raise ValueError("Use dedicated publication inputs")
    names = [choice["robot_secret"], choice["trust_secret"], plan["read_secret"], plan["bridge_secret"]]
    if "reporting" in plan:
        names.append(plan["reporting"]["secret"])
    if len(set(names)) != len(names): raise ValueError("Separate all source, control and publication credentials")
    registry = choice["registry"]
    if not isinstance(registry, dict) or "revision" in registry: raise ValueError("Bind publication revision to the discovered source commit")
    publisher.validate({**registry, "revision": "1" * 40})
    if (not registry["origin"].startswith("https://") or registry["ca_file"] != "/publisher-input/ca.crt"
            or registry["credential_file"] != "/run/robot/credential.json"): raise ValueError("Use reviewed canonical registry mounts and origin")
    return choice


def source_plan(plan, revision, ref):
    source = plan["source"]
    result = {"schema_version": 1, "clone_url": source["clone_url"], "read_username": source["read_username"], "revision": revision,
        "patchset_ref": ref, "ca_sha256": source["ca_sha256"], "checkout_program_sha256": source_binding.checkout_program_sha256()}
    source_binding.validate_source(result)
    return result


def forge_source_plan(plan, revision):
    source = plan["source"]
    result = {"schema_version": 3, "clone_url": source["clone_url"], "read_username": source["read_username"],
              "revision": revision, "branch_ref": "refs/heads/" + source["branch"],
              "ca_sha256": source["ca_sha256"], "checkout_program_sha256": source_binding.checkout_program_sha256()}
    source_binding.validate_source(result)
    return result


def bootstrap(programs):
    if set(programs) != set(source_binding.PROGRAMS): raise ValueError("Incomplete trusted step programs")
    encoded = base64.b64encode(publisher.canonical(programs)).decode()
    code = """import base64,contextlib,json,sys,tempfile
from pathlib import Path
with tempfile.TemporaryDirectory(prefix='trusted-publication-') as directory:
 root=Path(directory)
 for name,content in json.loads(base64.b64decode('ENCODED')).items():
  path=root/name
  with path.open('x',encoding='utf-8',newline='\\n') as stream: stream.write(content)
  path.chmod(0o600)
 sys.path.insert(0,str(root))
 from source_bound_static_image import main
 source=root/'source.json';source.write_text(sys.argv[2],encoding='utf-8')
 args=['--mode',sys.argv[1],'--source-plan',str(source),'--witness','/run/provenance/checkout.json']
 if sys.argv[1]=='checkout':
  result=main(args+['--workspace','/workspace/source','--credentials','/run/source'])
 elif sys.argv[1]=='publish':
  plan=root/'publication.json';plan.write_text(sys.argv[3],encoding='utf-8')
  with open(sys.argv[4],'x') as receipt,contextlib.redirect_stdout(receipt):
   result=main(args+['--plan',str(plan),'--output','/workspace/output'])
 else:raise SystemExit(1)
 raise SystemExit(result)
""".replace("ENCODED", encoded)
    if len(code.encode()) > 96 * 1024: raise ValueError("Trusted step code exceeds its argv bound")
    compile(code, "trusted-publication-step.py", "exec")
    return code


def run_template(plan):
    validate(plan)
    from render_gerrit_harbor_static_task import programs, task_spec
    from render_tekton_forge_bridge import run_template as base_template
    forge = "provider" in plan
    dummy = forge_source_plan(plan, "1" * 40) if forge else source_plan(plan, "1" * 40, "refs/changes/01/1/1")
    publication = {**plan["publication"]["registry"], "revision": dummy["revision"]}
    task = task_spec(publication, dummy, checkout_image=plan["checkout_image"], build_image=plan["build_image"],
        publisher_image=plan["publication"]["publisher_image"], build_argv=plan["build_argv"], program_map="unused-program",
        input_map="unused-publication-input", robot_secret=plan["publication"]["robot_secret"], source_map="unused-source-input", source_secret=plan["read_secret"])
    source = {**dummy, "revision": "$(params.revision)"}
    params = [{"name": "revision", "type": "string"}]
    if not forge:
        source["patchset_ref"] = "$(params.patchset-ref)"
        params.append({"name": "patchset-ref", "type": "string"})
    publication["revision"] = "$(params.revision)"
    code = bootstrap(programs())
    omitted = {"program", "input", "source-input"}
    task["volumes"] = [v for v in task["volumes"] if v["name"] not in omitted]
    task["volumes"].append({"name": "publication-trust", "secret": {"secretName": plan["publication"]["trust_secret"], "defaultMode": 0o440, "items": [{"key": "ca.crt", "path": "ca.crt"}]}})
    for step in task["steps"]:
        step["volumeMounts"] = [v for v in step["volumeMounts"] if v["name"] not in omitted]
        if step["name"] == "build": continue
        step["command"] = ["python3", "-I", "-B", "-c", code]
        step["args"] = [step["name"], publisher.canonical(source).decode(), publisher.canonical(publication).decode() if step["name"] == "publish" else ""]
        if step["name"] == "publish":
            step["args"].append("$(results.image-receipt.path)")
            step["volumeMounts"].append({"name": "publication-trust", "mountPath": "/publisher-input", "readOnly": True})
    result = base_template(plan, "unused")
    result["spec"]["pipelineSpec"]["params"] = params
    result["spec"]["pipelineSpec"]["results"] = [{"name": "image-receipt", "description": "", "type": "string", "value": "$(tasks.build.results.image-receipt)"}]
    result["spec"]["pipelineSpec"]["tasks"] = [{"name": "build", "params": [{"name": p["name"], "value": "$(params." + p["name"] + ")"} for p in params],
        "taskSpec": {"metadata": {}, "spec": None, "params": params, **task}}]
    return result


def resolve(value, data, *, pod=False):
    if isinstance(value, dict): return {k: resolve(v, data, pod=pod) for k,v in value.items()}
    if isinstance(value, list): return [resolve(v, data, pod=pod) for v in value]
    if isinstance(value, str):
        value = value.replace("$(params.revision)", data["revision"])
        if "ref" in data:
            value = value.replace("$(params.patchset-ref)", data["ref"])
        if pod: value = value.replace("$(results.image-receipt.path)", "/tekton/results/image-receipt")
    return value


def result_value(obj):
    status = obj.get("status", {})
    values = status.get("results", status.get("taskResults", status.get("pipelineResults", [])))
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict) or values[0].get("name") != "image-receipt": raise ValueError("Require the sole original image receipt")
    raw = values[0].get("value")
    if not isinstance(raw, str) or len(raw.encode()) > 8192: raise ValueError("Bound the original image receipt")
    return raw


def verify_receipt(plan, data, task, run):
    validate(plan)
    raw = result_value(task)
    if raw != result_value(run): raise ValueError("TaskRun and PipelineRun artifact results differ")
    receipt = loads_strict_json(raw)
    publisher.exact(receipt, "schema_version image manifest_digest config_digest output_layer_digest output_tree_sha256 base_digest source_revision artifact_publication_verified publisher_signature_verified deployment_verified ha_verified full_stack_verified source_checkout_witness_bound checkout_witness_sha256 source_plan_sha256")
    if type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1: raise ValueError("Invalid artifact receipt schema")
    registry = plan["publication"]["registry"]
    for key in ("manifest_digest", "config_digest", "output_layer_digest"):
        if not isinstance(receipt[key], str) or not publisher.DIGEST.fullmatch(receipt[key]): raise ValueError("Artifact content digest is missing")
    source = forge_source_plan(plan, data["revision"]) if "provider" in plan else source_plan(plan, data["revision"], data["ref"])
    expected = {"image": registry["origin"].removeprefix("https://") + "/" + registry["repository"] + "@" + receipt["manifest_digest"],
        "base_digest": registry["base_digest"], "source_revision": data["revision"], "checkout_witness_sha256": hashlib.sha256(source_binding.witness_bytes(source)).hexdigest(),
        "source_plan_sha256": hashlib.sha256(publisher.canonical(source)).hexdigest()}
    if any(receipt[k] != v for k,v in expected.items()) or not publisher.hex64(receipt["output_tree_sha256"]): raise ValueError("Image receipt is outside the reviewed source or registry scope")
    if any(receipt[k] is not True for k in ("artifact_publication_verified", "source_checkout_witness_bound")) or any(receipt[k] is not False for k in ("publisher_signature_verified", "deployment_verified", "ha_verified", "full_stack_verified")): raise ValueError("Artifact receipt claims unsupported acceptance")
    return receipt
