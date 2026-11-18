#!/usr/bin/env python3
"""Explicit local patched-build experiment; never substitute it for release evidence."""

import hashlib
import os
from pathlib import Path

from bounded_file import read_bounded_bytes
from strict_json import loads_strict_json
from test_harbor_core_replica_fixture import CoreReplicaChecks
from test_harbor_database_promotion_fixture import DatabasePromotionChecks
from test_harbor_jobservice_replica_fixture import JobReplicaChecks
from test_harbor_jwt_runtime_fixture import JWTChecks
from test_harbor_registry_runtime_fixture import RegistryChecks
from test_harbor_service_runtime_fixture import ServiceChecks


from harbor_pool_health_policy import (
    BASE_RECEIPT_SHA as BASE_RECEIPT_SHA, BINARIES, GO_SHA as GO_SHA, MODULE_INPUTS as MODULE_INPUTS, PATCHED_FILES as PATCHED_FILES, PATCH_SHA,
    REVISION as REVISION, SOURCE_SHA as SOURCE_SHA, VARIANT as VARIANT, validate_receipt,
)


def stage_candidate(directory, name, target):
    if os.environ.get("HARBOR_POOL_HEALTH_TEST_RUNTIME") != "1" or name not in BINARIES:
        raise ValueError("Enable only the explicitly reviewed local pool-health experiment")
    patch = Path(__file__).resolve().parents[1] / "patches/harbor/2.15.1-pg-pool-pre-use-health.patch"
    if patch.is_symlink() or hashlib.sha256(read_bounded_bytes(patch, max_bytes=16384)).hexdigest() != PATCH_SHA:
        raise ValueError("Reviewed local pool-health patch changed")
    source = directory / name
    if source.is_symlink(): raise ValueError("Do not substitute a candidate service link")
    raw = read_bounded_bytes(source, max_bytes=96 * 1024 ** 2)
    receipt = loads_strict_json(read_bounded_bytes(directory / "build-inputs.json", max_bytes=65536))
    validate_receipt(receipt)
    if hashlib.sha256(raw).hexdigest() != BINARIES[name]:
        raise ValueError("Candidate service executable changed")
    with target.open("xb") as stream: stream.write(raw)
    target.chmod(0o755)
    return target


class PoolHealthChecks:
    def stage_service_binary(self, directory, name, target):
        if name in BINARIES:
            return stage_candidate(Path(os.environ["HARBOR_POOL_HEALTH_TEST_ARTIFACTS"]), name, target)
        return super().stage_service_binary(directory, name, target)

    def exercise(self, context):
        result = super().exercise(context)
        for name, digest in BINARIES.items():
            context["test"].assertEqual(result["valkey_gateway_harbor_service_binaries_sha256"][name], digest)
        result.update(valkey_gateway_harbor_local_pool_health_patch_verified=True,
            valkey_gateway_harbor_local_pool_health_patch_sha256={"patch": PATCH_SHA},
            valkey_gateway_harbor_unmodified_release_sql_continuity_verified=False,
            valkey_gateway_harbor_candidate_upstream_supported=False)
        return result


def exercise(test):
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    class Application(PoolHealthChecks, JobReplicaChecks, CoreReplicaChecks, JWTChecks, DatabasePromotionChecks, ServiceChecks, RegistryChecks, gateway.Application): pass
    return cache.exercise(test, application=Application())
