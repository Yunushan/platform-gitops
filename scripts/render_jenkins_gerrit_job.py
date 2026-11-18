#!/usr/bin/env python3
"""Render a reviewed parameterized Jenkins Git-plugin freestyle job."""
import argparse
from pathlib import Path
import re
import shlex
import sys
from xml.etree import ElementTree as ET

from bounded_file import read_bounded_text
from jenkins_static_image import ARTIFACT
from strict_json import loads_strict_json
import verify_jenkins_gerrit as source


def render(plan):
    fields = {"schema_version", "gerrit_origin", "project", "credential_id", "agent_label", "git_plugin_version", "build_argv"}
    if (not isinstance(plan, dict) or set(plan) - {"archive_image_receipt"} != fields
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or type(plan.get("archive_image_receipt", False)) is not bool):
        raise ValueError("invalid-job-plan")
    sample = {"schema_version": 1, "jenkins_origin": "https://ci.example.invalid", "job": ["example"], "build": 1,
              "job_config_sha256": "1" * 64, "gerrit_origin": plan["gerrit_origin"], "project": plan["project"],
              "branch": "main", "change": 1, "patchset": 1, "revision": "1" * 40}
    source.validate(sample)
    for key in ("credential_id", "agent_label", "git_plugin_version"):
        if not isinstance(plan[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", plan[key]):
            raise ValueError("invalid-job-identity")
    args = plan["build_argv"]
    if (not isinstance(args, list) or not 1 <= len(args) <= 32 or any(not isinstance(arg, str) or not arg or len(arg) > 512
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in arg) for arg in args)):
        raise ValueError("invalid-build-command")
    root = ET.Element("project")
    def add(parent, tag, text=None, **attrs):
        item = ET.SubElement(parent, tag, attrs)
        if text is not None: item.text = text
        return item
    add(root, "actions")
    add(root, "keepDependencies", "false")
    props = add(root, "properties")
    params = add(add(props, "hudson.model.ParametersDefinitionProperty"), "parameterDefinitions", **{"class": "java.util.Arrays$ArrayList"})
    array = add(params, "a", **{"class": "hudson.model.ParameterDefinition-array"})
    for name in ("GERRIT_CHANGE_NUMBER", "GERRIT_PATCHSET_NUMBER", "GERRIT_PATCHSET_REVISION", "GERRIT_REFSPEC"):
        parameter = add(array, "hudson.model.StringParameterDefinition")
        add(parameter, "name", name); add(parameter, "trim", "false")
    scm = add(root, "scm", **{"class": "hudson.plugins.git.GitSCM", "plugin": "git@" + plan["git_plugin_version"]})
    add(scm, "configVersion", "2")
    remote = add(add(scm, "userRemoteConfigs"), "hudson.plugins.git.UserRemoteConfig")
    add(remote, "refspec", "+$GERRIT_REFSPEC:refs/remotes/origin/fixture")
    add(remote, "url", plan["gerrit_origin"] + "/a/" + plan["project"] + ".git")
    add(remote, "credentialsId", plan["credential_id"])
    add(add(add(scm, "branches"), "hudson.plugins.git.BranchSpec"), "name", "*/fixture")
    add(scm, "doGenerateSubmoduleConfigurations", "false")
    add(scm, "submoduleCfg", **{"class": "empty-list"})
    extensions = add(scm, "extensions")
    if plan.get("archive_image_receipt"):
        add(extensions, "hudson.plugins.git.extensions.impl.WipeWorkspace")
    add(root, "assignedNode", plan["agent_label"]); add(root, "canRoam", "false"); add(root, "disabled", "false")
    add(root, "blockBuildWhenDownstreamBuilding", "false"); add(root, "blockBuildWhenUpstreamBuilding", "false")
    add(root, "triggers"); add(root, "concurrentBuild", "false")
    shell = add(add(root, "builders"), "hudson.tasks.Shell")
    add(shell, "command", 'set -eu\ntest "${GIT_COMMIT:-}" = "${GERRIT_PATCHSET_REVISION:-}"\nexec ' + " ".join(shlex.quote(arg) for arg in args))
    add(shell, "configuredLocalRules")
    publishers = add(root, "publishers")
    if plan.get("archive_image_receipt"):
        archive = add(publishers, "hudson.tasks.ArtifactArchiver")
        add(archive, "artifacts", ARTIFACT)
        for name, value in (("onlyIfSuccessful", "true"), ("allowEmptyArchive", "false"), ("followSymlinks", "false")):
            add(archive, name, value)
    add(root, "buildWrappers")
    ET.indent(root, space="  ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8", short_empty_elements=True) + b"\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    try: data = render(loads_strict_json(read_bounded_text(args.plan, max_bytes=16384)))
    except Exception:
        print("Jenkins Gerrit job render failed; review the plan.", file=sys.stderr); return 1
    sys.stdout.buffer.write(data); return 0


if __name__ == "__main__": raise SystemExit(main())
