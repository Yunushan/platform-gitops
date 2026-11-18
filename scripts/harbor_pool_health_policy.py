#!/usr/bin/env python3
"""Immutable local Harbor candidate identities; no deployment or support claim."""

import json

VARIANT = "harbor-2.15.1-pg-pool-pre-use-health-v1"
BUILD_SCHEMA = "harbor-pool-health-build-v2"
REVISION = "335c05f2b9507b4c7116fe69b84e31ef9f4b91dd"
SOURCE_SHA = "eb2b81020144653c50adb58894d831268dd934b0a38e376281e12548b276dfd0"
GO_SHA = "00859d7bd6defe8bf84d9db9e57b9a4467b2887c18cd93ae7460e713db774bc1"
GO_BINARY_SHA = "b0a317af44dab6d5a66007bf33fc2215310a2412ce0059fc70582edb5cb975d5"
SWAGGER_SHA = "d0fecb8bd31b4bf62f3b94a84d82cb739abee7f8cf867d57375bdf79faca4de8"
PATCH_SHA = "552bd0105348bfb0edbf789003f999efa30e81924835793642b3fafcde7760af"
BASE_RECEIPT_SHA = "512f71010be65b4b723cfc374c684316ec33abcc8b7b4004d691edfc9201f16a"
GENERATED_SHA = "e103aad6452e30c16b4b6be4143f7f25b914345a30103087ed4370c70c1abbed"
SOURCE_TREE_SHA = "7229dde2e2e211a5f7332b7a46bc5f06b3a9f636b7ba1f590b235d1a1b3e6e63"
PATCHED_TREE_SHA = "dd527d0d134e7933607dde7b55f837df354dc6ef8e56be7edf369b7fa93a8883"
PATCHED_FILES = {"src/common/dao/pgsql.go": {
    "before": "e32120f0fe1ade257ebb3721698fbd140cfb4624acb08300518a40e0bdd17350",
    "after": "61190ae9fed4f62e8a615c3309c8107e89cbe94c8fdd420578fefdcc919f9b0a"}}
MODULE_INPUTS = {"src/go.mod": "2452db301573926edd007e67acdafb0178dcdbe2af7dc030e5d60d6addedf7c4",
    "src/go.sum": "11a39a7e13d325dbb236653c7758b0846a76d9e13085d7e19229524fb18cd7bd"}
BINARIES = {"harbor-core": "f902cbfea8b53f0bd774bb63deef01b358471a6d9531bc948227a86bc5f7faf6",
    "harbor-jobservice": "050dbffac1a232af079bbdcf9c9d71c2549ef7ce6c8a6cad637987e237f02794"}
LINKER_FLAGS = "-s -w -X github.com/goharbor/harbor/src/pkg/version.GitCommit=335c05f2-poolhealth -X github.com/goharbor/harbor/src/pkg/version.ReleaseVersion=v2.15.1-poolhealth.local"


def validate_receipt(receipt):
    if not isinstance(receipt, dict): raise ValueError("Require a typed candidate build receipt")
    expected = {"variant": VARIANT, "patch_sha256": PATCH_SHA, "patched_files": PATCHED_FILES,
        "module_inputs_sha256": MODULE_INPUTS, "source_revision": REVISION,
        "source_archive_sha256": SOURCE_SHA, "go_archive_sha256": GO_SHA, "binaries_sha256": BINARIES,
        "exit": {"core": {"exit_code": 0}, "jobservice": {"exit_code": 0}}}
    if receipt.get("build_schema") is None:
        expected["base_generated_build_receipt_sha256"] = BASE_RECEIPT_SHA
    elif receipt.get("build_schema") == BUILD_SCHEMA:
        expected.update(build_schema=BUILD_SCHEMA, compiler_sha256=GO_BINARY_SHA, generator_sha256=SWAGGER_SHA,
            generated_sources_count=970, generated_sources_sha256=GENERATED_SHA,
            source_files_count=3231, source_tree_sha256=SOURCE_TREE_SHA,
            patched_files_count=4201, patched_tree_sha256=PATCHED_TREE_SHA,
            preparation_exit={k: {"exit_code": 0} for k in ("module-verification", "api-generation", "patch")})
        for key in ("prepared_from_archives", "offline_modules_verified", "build_network_disabled", "temporary_work_removed"):
            if receipt.get(key) is not True: raise ValueError("Candidate preparation evidence is incomplete")
    else: raise ValueError("Unreviewed candidate build schema")
    if (any(json.dumps(receipt.get(k), sort_keys=True) != json.dumps(v, sort_keys=True) for k, v in expected.items())
            or any(receipt.get(k) is not True for k in ("original_sources_unchanged", "patched_sources_unchanged_during_build", "original_process_and_cgroup_removed"))
            or receipt.get("runtime_verified") is not False or receipt.get("publisher_signatures_verified") is not False):
        raise ValueError("Candidate executable, patch or build provenance changed")
