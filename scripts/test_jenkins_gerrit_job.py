#!/usr/bin/env python3
"""Check fixed-argument parameterized Jenkins job generation."""
import copy
import unittest
from xml.etree import ElementTree as ET

from render_jenkins_gerrit_job import render
from jenkins_static_image import ARTIFACT


class JenkinsGerritJob(unittest.TestCase):
    def setUp(self):
        self.plan = {"schema_version": 1, "gerrit_origin": "https://review.example", "project": "team/project",
                     "credential_id": "ci-reader", "agent_label": "ci-workers", "git_plugin_version": "5.11.0",
                     "build_argv": ["python3", "-m", "unittest", "a b; echo dangerous"]}

    def test_job_pins_source_agent_parameters_and_escapes_arguments(self):
        root = ET.fromstring(render(self.plan))
        self.assertIsNotNone(root.find("actions"))
        self.assertEqual(root.findtext("assignedNode"), "ci-workers")
        self.assertEqual(root.findtext("concurrentBuild"), "false")
        self.assertEqual(root.findtext("scm/userRemoteConfigs/hudson.plugins.git.UserRemoteConfig/refspec"), "+$GERRIT_REFSPEC:refs/remotes/origin/fixture")
        self.assertEqual(root.findtext("scm/userRemoteConfigs/hudson.plugins.git.UserRemoteConfig/url"), "https://review.example/a/team/project.git")
        params = [p.findtext("name") for p in root.findall("properties/hudson.model.ParametersDefinitionProperty/parameterDefinitions/a/hudson.model.StringParameterDefinition")]
        self.assertEqual(params, ["GERRIT_CHANGE_NUMBER", "GERRIT_PATCHSET_NUMBER", "GERRIT_PATCHSET_REVISION", "GERRIT_REFSPEC"])
        command = root.findtext("builders/hudson.tasks.Shell/command")
        self.assertIn('test "${GIT_COMMIT:-}" = "${GERRIT_PATCHSET_REVISION:-}"', command)
        self.assertIn("'a b; echo dangerous'", command)
        self.assertEqual(len(root.find("publishers")), 0)

    def test_artifact_job_clears_workspace_and_archives_only_the_receipt(self):
        root = ET.fromstring(render({**self.plan, "archive_image_receipt": True}))
        self.assertIsNotNone(root.find("scm/extensions/hudson.plugins.git.extensions.impl.WipeWorkspace"))
        archive = root.find("publishers/hudson.tasks.ArtifactArchiver")
        self.assertEqual({item.tag: item.text for item in archive}, {
            "artifacts": ARTIFACT, "onlyIfSuccessful": "true", "allowEmptyArchive": "false", "followSymlinks": "false"})
        with self.assertRaises(ValueError):
            render({**self.plan, "archive_image_receipt": "true"})

    def test_bad_source_identity_and_arguments_rejected(self):
        for key, value in [("gerrit_origin", "http://review.example"), ("project", "../bad"),
                           ("credential_id", "$(unsafe)"), ("agent_label", "worker && unsafe"),
                           ("build_argv", ["echo", "line\nbreak"])]:
            with self.subTest(key=key):
                plan = copy.deepcopy(self.plan); plan[key] = value
                with self.assertRaises(ValueError): render(plan)


if __name__ == "__main__": unittest.main()
