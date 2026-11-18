#!/usr/bin/env python3
"""Verify the host agent packages keep TLS and executor access explicit."""

import json
from pathlib import Path
import unittest

import yaml

from render_woodpecker_agent import render


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def example(runtime):
    return json.loads((EXAMPLES / f"woodpecker-agent-{runtime}.json").read_bytes())


class HostAgentTests(unittest.TestCase):
    def test_compose_pins_tls_user_executor_and_state(self):
        plan = example("docker")
        bundle = yaml.safe_load(render(plan)["compose.yaml"])
        agent = bundle["services"][plan["name"]]
        self.assertEqual(agent["image"], plan["image"])
        self.assertEqual(agent["user"], "1000:1000")
        self.assertEqual(agent["group_add"], ["2000"])
        self.assertEqual(agent["command"], ["agent", "--grpc-secure=true", "--grpc-skip-insecure=false"])
        self.assertEqual(agent["environment"]["WOODPECKER_BACKEND"], "docker")
        self.assertEqual(agent["environment"]["WOODPECKER_BACKEND_DOCKER_HOST"], "unix:///run/woodpecker-engine.sock")
        self.assertEqual(agent["environment"]["WOODPECKER_AGENT_SECRET_FILE"], "/run/platform/woodpecker-agent/agent-secret")
        self.assertEqual(agent["environment"]["SSL_CERT_FILE"], "/run/platform/woodpecker-agent/https-ca-bundle.crt")
        self.assertNotIn("WOODPECKER_AGENT_SECRET", agent["environment"])
        self.assertNotIn("ports", agent)
        self.assertFalse(agent.get("privileged", False))
        self.assertTrue(agent["read_only"])
        self.assertEqual([volume["source"] for volume in agent["volumes"]],
                         [plan["state_path"], plan["inputs_path"], plan["docker_socket"]])
        self.assertTrue(all(not volume["bind"]["create_host_path"] for volume in agent["volumes"]))

    def test_native_service_requires_identity_socket_and_private_token_file(self):
        plan = example("native")
        bundle = render(plan)
        service = bundle["woodpecker-agent.service"]
        self.assertIn("SupplementaryGroups=woodpecker_engine\n", service)
        self.assertIn("ExecStartPre=/usr/bin/test -S " + plan["docker_socket"], service)
        self.assertIn("ExecStart=" + plan["binary"] + " agent --grpc-secure=true --grpc-skip-insecure=false", service)
        self.assertIn("Environment=WOODPECKER_AGENT_SECRET_FILE=" + plan["inputs_path"] + "/agent-secret", service)
        self.assertNotIn("WOODPECKER_AGENT_SECRET=", service)
        self.assertEqual(bundle["binary.sha256"], f"{plan['binary_sha256']}  {plan['binary']}\n")

    def test_ca_optional_and_unsupported_values_refused(self):
        plan = example("docker")
        rendered = render({**plan, "custom_https_ca": False})["compose.yaml"]
        self.assertNotIn("SSL_CERT_FILE", rendered)
        for overrides in (
            {"server": "https://ci.example.invalid:443"}, {"server": "ci.example.invalid:65536"},
            {"docker_socket": "tcp://ci.example.invalid:2375"}, {"inputs_path": "/run/woodpecker-engine"},
            {"state_path": "/etc/platform/woodpecker-agent"}, {"image": "woodpecker-agent:latest"},
            {"socket_gid": 0}, {"socket_gid": True}, {"agent_secret": "embedded"},
        ):
            with self.subTest(field=next(iter(overrides))), self.assertRaises(ValueError):
                render({**plan, **overrides})


if __name__ == "__main__":
    unittest.main()
