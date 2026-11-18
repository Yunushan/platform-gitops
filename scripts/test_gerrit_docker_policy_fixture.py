#!/usr/bin/env python3
"""Actual isolated Docker create/inspect mechanics; no Gerrit or Ceph workload."""

import json
from uuid import uuid4

import activate_gerrit_host as activate
from test_gerrit_host import plan


def exercise(test):
    from test_docker_engine_fixture import exercise as engine_fixture

    def workload(context):
        root, backend = context["root"], context["backend"]
        selected = plan("docker"); selected["member"] = "gerrit-a"
        for site in selected["site_plan"]["sites"]:
            directory = root / site["name"]; directory.mkdir(); site["host_path"] = str(directory)
        shared = root / "shared"; shared.mkdir(); selected["site_plan"]["shared_host_path"] = str(shared)
        bundle = root / "gerrit-package"; bundle.mkdir()
        for filename, content in activate.sites.render(selected["site_plan"], "docker").items(): (bundle / filename).write_bytes(content.encode())
        # The engine's sole imported static probe image exercises CLI materialization.
        # It is not a reviewed Gerrit runtime image or a valid full activation plan.
        probes = backend.docker("container", "ls", "--all", "--quiet", "--no-trunc", "--filter", "label=platform.fixture").split()
        test.assertEqual(len(probes), 1)
        probe = json.loads(backend.docker("container", "inspect", probes[0]))[0]
        image_id = probe["Image"]
        images_before = backend.docker("image", "ls", "--all", "--quiet", "--no-trunc").split()
        selected["docker"]["image_id"] = image_id; selected["site_plan"]["java_image"] = image_id
        image = json.loads(backend.docker("image", "inspect", image_id))[0]
        state = {"operation_id": str(uuid4()), "plan_sha256": activate.values.fingerprint(selected), "toolset_sha256": activate.tools()}
        container = None
        try:
            container = backend.docker(*activate.create_arguments(bundle, selected, state)).strip()
            test.assertTrue(activate.host_tools.container_id(container))
            original = json.loads(backend.docker("container", "inspect", container))[0]
            test.assertEqual(original["State"]["Status"], "created")
            test.assertFalse(original["State"]["Running"])
            first = activate.container_policy(original, bundle, selected, state, image)
            observed = json.loads(backend.docker("container", "inspect", container))[0]
            test.assertEqual(observed["Id"], container)
            test.assertEqual(activate.container_policy(observed, bundle, selected, state, image), first)
            test.assertEqual(set(backend.docker("image", "ls", "--all", "--quiet", "--no-trunc").split()), set(images_before))
            return {"docker_cli_create_inspect_verified": True, "created_container_not_started": True, "no_image_download": True,
                "gerrit_runtime_verified": False, "ceph_mounts_verified": False, "ha_verified": False, "full_stack_verified": False}
        finally:
            if container:
                original = json.loads(backend.docker("container", "inspect", container))[0]
                if original.get("Id") != container or original.get("Config", {}).get("Labels", {}).get(activate.LABEL + "operation") != state["operation_id"]:
                    raise ValueError("Preserve the changed original fixture container")
                test.assertFalse(original["State"]["Running"])
                backend.docker("container", "rm", container)

    return engine_fixture(test, workload=workload)
