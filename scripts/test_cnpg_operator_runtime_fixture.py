"""Opt-in original CNPG candidates and webhook on isolated K3s VM workers."""

import json
import os
from pathlib import Path
from unittest import mock
from uuid import uuid4

from bounded_file import read_bounded_bytes
import cnpg_release
import deploy_cnpg_operator as deploy
import render_cnpg_operator as renderer
from test_cnpg_operator import fixture
from test_k3s_vm_fixture import exercise as vm_exercise
from verify_oci_archive import verify_archive


def exercise(test):
    release = read_bounded_bytes(Path(os.environ["CNPG_TEST_RELEASE"]), max_bytes=2 * 1024 ** 2)
    archive = Path(os.environ["CNPG_TEST_OPERATOR_OCI"]).resolve(strict=True)
    image = cnpg_release.RELEASES["1.30.1"]["image"]
    archive_digest = verify_archive(archive, image)
    def workload(context):
        client, root = context["client"], context["root"]
        def create(value):
            return json.loads(client.call(["create", "-f", "-", "-o", "json"], payload=json.dumps(value).encode()))
        namespace = create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "cnpg-system",
            "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
        plan = fixture()
        plan.update(operation_id=str(uuid4()), target=context["target"], namespace_uid=namespace["metadata"]["uid"])
        rows = json.loads(client.call(["get", "nodes", "-o", "json"]))["items"]
        workers = {node["name"]: node for node in context["nodes"][1:]}
        plan["nodes"] = [{"name": row["metadata"]["name"], "uid": row["metadata"]["uid"],
                          "domain": row["metadata"]["labels"][plan["domain_label"]]}
                         for row in rows if row["metadata"]["name"] in workers]
        test.assertEqual(len(plan["nodes"]), 2)
        for node in workers.values():
            destination = "/tmp/fixture-image-" + image.rsplit("sha256:", 1)[1] + ".tar"
            context["upload"](node, archive, destination)
            context["ssh"](node, "sudo /mnt/fixtures/k3s ctr --address /run/k3s/containerd/containerd.sock --namespace k8s.io images import --digests " + destination, seconds=120)
            context["ssh"](node, "rm -- " + destination)
        directory = root / "private/cnpg-create"
        with mock.patch.object(deploy, "ROOT", root):
            checked = deploy.operate(plan, "k3s", release, mode="check-server", output=root / "private/cnpg-check", client=client)
            plan["admitted_sha256"] = checked["admitted_sha256"]
            result = deploy.operate(plan, "k3s", release, mode="create", output=directory, wait_seconds=180, client=client)
            test.assertTrue(result["operator_resources_ready"])
            test.assertTrue(result["webhook_admission_checked"])
            def pods():
                return json.loads(client.call(["get", "pods", "-n", renderer.NAMESPACE, "-l", "app.kubernetes.io/name=cloudnative-pg", "-o", "json"]))["items"]
            original = pods()
            test.assertEqual(len(original), 2)
            # Pinned upstream v1.30.1 controller.go defines this operator Lease;
            # this is distinct from a PostgreSQL Cluster's primary Lease.
            def lease():
                return client.get("lease/db9c8771.cnpg.io", renderer.NAMESPACE)
            initial_lease = lease()
            holder = initial_lease["spec"]["holderIdentity"]
            removed = next(row for row in original if holder.startswith(row["metadata"]["name"] + "_"))
            original_lease_uid = initial_lease["metadata"]["uid"]
            original_transitions = initial_lease["spec"].get("leaseTransitions", 0)
            client.call(["delete", "pod/" + removed["metadata"]["name"], "-n", renderer.NAMESPACE, "--wait=false"])
            def replacement_ready():
                current = pods()
                return (len(current) == 2 and removed["metadata"]["uid"] not in {row["metadata"]["uid"] for row in current}
                        and all(deploy.postgres.condition(row, "Ready") for row in current))
            context["wait"](replacement_ready, seconds=180)
            def changed_holder():
                observed = lease()
                test.assertEqual(observed["metadata"]["uid"], original_lease_uid)
                current_holder = observed["spec"].get("holderIdentity", "")
                return (current_holder != holder and observed["spec"].get("leaseTransitions", 0) > original_transitions
                        and any(current_holder.startswith(row["metadata"]["name"] + "_") for row in pods()))
            context["wait"](changed_holder, seconds=60)
            previous_renewal = lease()["spec"]["renewTime"]
            context["wait"](lambda: changed_holder() and lease()["spec"]["renewTime"] != previous_renewal, seconds=30)
            resumed = deploy.operate(plan, "k3s", release, mode="resume", output=directory, wait_seconds=60, client=client)
            test.assertTrue(resumed["webhook_admission_checked"])
            test.assertEqual(json.loads(client.call(["get", "clusters.postgresql.cnpg.io", "-A", "-o", "json"]))["items"], [])
            test.assertEqual(json.loads(client.call(["get", "pvc", "-n", renderer.NAMESPACE, "-o", "json"]))["items"], [])
        return {"cnpg_operator_runtime_verified": True, "cnpg_original_resources_resumed": True,
                "cnpg_candidate_pod_replacement_verified": True, "cnpg_webhook_admission_verified": True,
                "cnpg_operator_lease_transition_verified": True,
                "cnpg_operator_archive_sha256": archive_digest, "database_created": False,
                "database_io_verified": False, "operator_leader_failover_verified": False,
                "physical_node_ha_verified": False, "full_stack_verified": False}
    return vm_exercise(test, workload=workload)
