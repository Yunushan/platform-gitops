#!/usr/bin/env python3
"""Validate opt-in Kubernetes scheduler workload and rejection policy."""
import copy
import unittest

from render_gitlab_gitops_cronjob import render, valid_schedule


class CronJobTest(unittest.TestCase):
    def setUp(self):
        self.plan = {"schema_version": 1, "name": "gitlab-handoff", "namespace": "platform-automation",
                     "image": "registry.example/scheduler@sha256:" + "a" * 64, "schedule": "*/5 * * * *",
                     "plan_configmap": "reviewed-policy", "plan_sha256": "b" * 64, "credential_secret": "handoff-inputs",
                     "proof_pvc": "handoff-proofs", "suspend": True, "apply": False, "wait_seconds": 900,
                     "active_deadline_seconds": 2700}

    def test_default_render_is_suspended_restricted_and_scoped(self):
        account, job = render(self.plan)
        self.assertFalse(account["automountServiceAccountToken"])
        spec = job["spec"]
        self.assertTrue(spec["suspend"])
        self.assertEqual(spec["concurrencyPolicy"], "Forbid")
        self.assertEqual(spec["jobTemplate"]["spec"]["backoffLimit"], 0)
        pod = spec["jobTemplate"]["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        container = pod["containers"][0]
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertNotIn("--apply", container["args"])
        self.assertEqual(next(v for v in pod["volumes"] if v["name"] == "proof")["persistentVolumeClaim"]["claimName"], "handoff-proofs")
        self.assertEqual({x["key"] for x in next(v for v in pod["volumes"] if v["name"] == "credentials")["secret"]["items"]},
                         {"api-token", "token", "ca.crt", "cd-kubeconfig"})

    def test_apply_requires_explicit_option(self):
        self.plan["apply"] = True
        self.plan["suspend"] = False
        job = render(self.plan)[1]
        self.assertIn("--apply", job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"])

    def test_invalid_options_fail_closed(self):
        for key, value in [("image", "example/latest:latest"), ("plan_sha256", "short"), ("namespace", "default"),
                           ("schedule", "60 * * * *"), ("schedule", "*/0 * * * *"), ("apply", "false"),
                           ("active_deadline_seconds", 901), ("wait_seconds", True), ("name", "UPPER")]:
            with self.subTest(key=key):
                plan = copy.deepcopy(self.plan)
                plan[key] = value
                with self.assertRaises(ValueError): render(plan)

    def test_cron_bounds(self):
        for expression in ("0 0 1 1 0", "*/5 0-23 * * 1-5", "0,30 * * * *"):
            self.assertTrue(valid_schedule(expression))
        for expression in ("* * * *", "* * 0 * *", "* * * 13 *", "* 24 * * *", "* * * * 8", "30-10 * * * *"):
            self.assertFalse(valid_schedule(expression))

    def test_cilium_policy_matches_only_the_scheduler_and_exact_destinations(self):
        egress = {"gitlab_addresses": ["192.0.2.10", "2001:db8::10"], "gitlab_port": 443,
                  "api": {"kind": "in-cluster", "ports": [6443, 443]}}
        account, policy, job = render(dict(self.plan, schema_version=2, egress=egress))
        self.assertEqual(policy["kind"], "CiliumNetworkPolicy")
        selector = policy["spec"]["endpointSelector"]["matchLabels"]
        self.assertEqual(selector["io.cilium.k8s.policy.serviceaccount"], account["metadata"]["name"])
        self.assertEqual(selector["app.kubernetes.io/name"],
                         job["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/name"])
        dns, gitlab, api = policy["spec"]["egress"]
        self.assertEqual(dns["toEndpoints"][0]["matchLabels"]["k8s:k8s-app"], "kube-dns")
        self.assertEqual(gitlab["toCIDR"], ["192.0.2.10/32", "2001:db8::10/128"])
        self.assertEqual(gitlab["toPorts"], [{"ports": [{"port": "443", "protocol": "TCP"}]}])
        self.assertEqual(api["toEntities"], ["kube-apiserver"])
        egress["api"] = {"kind": "external", "addresses": ["198.51.100.2"], "ports": [6443]}
        external = render(dict(self.plan, schema_version=2, egress=egress))[1]["spec"]["egress"][2]
        self.assertEqual(external, {"toCIDR": ["198.51.100.2/32"],
                                    "toPorts": [{"ports": [{"port": "6443", "protocol": "TCP"}]}]})

    def test_cilium_policy_rejects_wildcards_invalid_hosts_and_missing_api_scope(self):
        base = {"gitlab_addresses": ["192.0.2.10"], "gitlab_port": 443,
                "api": {"kind": "in-cluster", "ports": [6443]}}
        invalid = [dict(base, gitlab_addresses=["0.0.0.0/0"]), dict(base, gitlab_addresses=["::"]),
                   dict(base, gitlab_port=True), dict(base, gitlab_addresses=["2001:db8::a", "2001:db8::A"]),
                   dict(base, api={"kind": "external", "ports": [443]}),
                   dict(base, api={"kind": "in-cluster", "ports": [443], "addresses": ["192.0.2.10"]})]
        for egress in invalid:
            with self.subTest(egress=egress), self.assertRaises(ValueError):
                render(dict(self.plan, schema_version=2, egress=egress))


if __name__ == "__main__": unittest.main()
