"""Verify live Kyverno admission for a signed CI release."""

import copy
import json
from pathlib import Path

from bounded_subprocess import run_bounded
from ci_gitops_handoff import sha256
from strict_json import loads_strict_json
from test_gerrit_artifact_promotion_worker_fixture import NAME, retain
from test_kyverno_admission_authority_fixture import AuthorityChecks, observe_admission, unsigned_init_container
from test_kyverno_private_tuf_fixture import PrivateTUFChecks


class ReleaseAdmission(AuthorityChecks, PrivateTUFChecks):
    def __init__(self, context, kind, namespace, authority, lifetime, *, provider="zuul"):
        self.context = context
        self.client = context["client"]
        self.kind = kind
        self.provider = provider
        self.evidence_name = f"{provider}-artifact-{kind}"
        self.namespace = namespace
        self.installed = authority[1]
        self.installation_sha256 = authority[2]
        self.credential_name = "signature-read-" + kind
        self.private_tuf_lifetime = lifetime
        self.address = {"argocd": "192.0.2.101", "flux": "192.0.2.102"}[kind]

    def private_tuf(self, native):
        super().private_tuf(native, address=self.address, directory_name="private-tuf-" + self.kind)

    def verify_mirror(self, test):
        original = self.private_tuf_active_process
        proc = Path("/proc") / str(original["pid"])
        test.assertEqual(int((proc / "stat").read_text().rsplit(") ", 1)[1].split()[19]), original["start_ticks"])
        test.assertEqual((proc / "cgroup").read_text().strip(), original["cgroup"])

    def pod(self, namespace, name, image):
        spec = copy.deepcopy(self.original_deployment["spec"]["template"]["spec"])
        spec["containers"][0]["image"] = image
        return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": namespace}, "spec": spec}

    def refuse(self, test, document, policy, case, arguments):
        result = run_bounded([*self.client.prefix, "--request-timeout=45s", *arguments], input=json.dumps(document),
                             text=True, timeout=60, check=False, output_max_bytes=1024 * 1024)
        test.assertFalse(any(value in result.stderr.encode() for value in self.signature_private_values))
        named = ("denied the request" in result.stderr and policy in result.stderr
                 and "Release signature and signed bindings must verify." in result.stderr)
        if result.returncode == 0 or not named:
            retain(f"{self.evidence_name}-denial-{case}.json", {"returncode": result.returncode, "stderr": result.stderr})
            raise AssertionError(f"Kyverno policy did not refuse {self.kind} {case}")
        self.denials.append({"case": case, "policy": policy, "named_signature_policy_denial": True})

    def signed_dryrun(self, test, name):
        image = self.signature_plan["image"]
        pod = self.pod(self.namespace, name, image)
        actual = loads_strict_json(self.client.call(["create", "--dry-run=server", "-f", "-", "-o", "json"],
                                                   payload=json.dumps(pod).encode()))
        test.assertEqual(actual["spec"]["containers"][0]["image"], image)
        test.assertEqual(actual["metadata"]["namespace"], self.namespace)

    def private_tuf_live(self, native, files, status, policy):
        test = native["test"]
        self.verify_mirror(test)
        self.original_deployment = observe_admission(lambda: self.client.get("deployment/" + NAME, self.namespace))
        self.unsigned = self.original_deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        image, annotations = self.signature_plan["image"], self.signature_plan["annotations"]
        test.assertNotEqual(self.unsigned, image)
        test.assertEqual(policy["spec"]["attestors"][0]["cosign"]["annotations"], annotations)
        self.create_signature_credential(self.context, self.credential_name)
        bad_key = f"{self.provider}-{self.kind}-wrong-key"
        wrong_bindings = {f"{self.provider}-{self.kind}-wrong-binding-{i}": key for i, key in enumerate(sorted(annotations))}
        self.namespaces = {self.namespace: self.client.get("namespace/" + self.namespace)["metadata"]["uid"]}
        for name in (bad_key, *wrong_bindings):
            spec = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name,
                    "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}}
            original = loads_strict_json(self.client.call(["create", "-f", "-", "-o", "json"], payload=json.dumps(spec).encode()))
            self.namespaces[name] = original["metadata"]["uid"]
        self.policies, self.webhooks = self.compile_authority_policies(native, self.context, policy, self.namespaces,
            wrong_bindings=wrong_bindings, credential_name=self.credential_name)
        self.denials = []
        cases = ((self.namespace, self.unsigned, "unsigned"), (bad_key, image, "wrong-key"),
                 *((ns, image, "wrong-binding-" + str(i)) for i, ns in enumerate(wrong_bindings)))
        dry_run = ["create", "--dry-run=server", "-f", "-"]
        for namespace, selected, case in cases:
            self.refuse(test, self.pod(namespace, "deny-" + case, selected), namespace, case, dry_run)
        init = self.pod(self.namespace, "deny-init", image)
        init["spec"]["initContainers"] = [unsigned_init_container(init["spec"]["containers"][0], self.unsigned)]
        self.refuse(test, init, self.namespace, "unsigned-init", dry_run)
        self.signed_dryrun(test, "signed-before-promotion")
        self.before = self.audit_authority(native, self.context, self.installed, self.namespaces, self.policies)
        self.tuf_root_sha256 = sha256(loads_strict_json(files["root.json"]))
        test.assertEqual(status["role_keyids"]["root"]["threshold"], 2)
        print(json.dumps({f"{self.provider}_admission_stage": "enforcing-before-promotion", "controller": self.kind}), flush=True)

    def verify_rollout(self, native, live, pods, revision):
        test = native["test"]
        self.verify_mirror(test)
        test.assertEqual(live["metadata"]["uid"], self.original_deployment["metadata"]["uid"])
        image = self.signature_plan["image"]
        test.assertEqual(live["spec"]["template"]["spec"]["containers"][0]["image"], image)
        before = {row["metadata"]["uid"] for row in self.before["controller_pods"]}
        after = self.audit_authority(native, self.context, self.installed, self.namespaces, self.policies)
        test.assertEqual({row["metadata"]["uid"] for row in after["controller_pods"]}, before)
        ephemeral = {"name": "unsigned-debug", "targetContainerName": "web",
            "image": self.unsigned, "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]}}}
        pod_name = pods[0]["metadata"]["name"]
        self.refuse_unsigned_ephemeral(test, pods[0], ephemeral)
        test.assertFalse(self.client.get("pod/" + pod_name, self.namespace)["spec"].get("ephemeralContainers"))
        self.signed_dryrun(test, "signed-after-promotion")
        digest = retain(f"{self.evidence_name}-admission.json", {
            "image": image, "annotations": self.signature_plan["annotations"],
            "promotion_revision": revision, "deployment_uid": live["metadata"]["uid"],
            "pod_uids": [pod["metadata"]["uid"] for pod in pods], "negative_cases": self.denials,
            "tuf_root_sha256": self.tuf_root_sha256, "installation_sha256": self.installation_sha256,
            "webhooks_before_promotion": self.webhooks, "before": self.before, "after": after,
            "signed_image_admission_verified": True, "full_stack_verified": False})
        return {f"{self.provider}_artifact_{self.kind}_admission_verified": True,
                f"{self.provider}_artifact_{self.kind}_admission_sha256": digest}
