#!/usr/bin/env python3
"""Render an opt-in Woodpecker forge switch without reading OAuth credentials."""
import argparse
import copy
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import yaml
from bounded_file import read_bounded_text
from strict_json import loads_strict_json
from strict_yaml import loads_strict_yaml_all


def render(values, plan):
    if not isinstance(plan, dict) or set(plan) != {"schema_version", "provider", "url", "oauth_secret", "previous_oauth_secret"} or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("invalid-forge-plan")
    if plan["provider"] not in {"forgejo", "gitea"}:
        raise ValueError("unsupported-forge")
    if not isinstance(plan["url"], str): raise ValueError("invalid-forge-url")
    url = urlsplit(plan["url"])
    if (url.scheme != "https" or not url.hostname or any(ch.isspace() or ord(ch) < 33 for ch in plan["url"])
            or "\\" in plan["url"] or url.username is not None or url.password is not None
            or url.query or url.fragment or url.path not in ("", "/") or (url.port is not None and not 1 <= url.port <= 65535)):
        raise ValueError("verified-https-origin-required")
    for key in ("oauth_secret", "previous_oauth_secret"):
        if not isinstance(plan[key], str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", plan[key]):
            raise ValueError("invalid-secret-name")
    if not isinstance(values, dict) or not isinstance(values.get("server"), dict): raise ValueError("invalid-chart-values")
    output = copy.deepcopy(values)
    server = output["server"]
    env = server.get("env")
    if not isinstance(env, dict): raise ValueError("invalid-server-environment")
    for key in ("WOODPECKER_FORGEJO_CLIENT", "WOODPECKER_FORGEJO_SECRET", "WOODPECKER_GITEA_CLIENT", "WOODPECKER_GITEA_SECRET"):
        if key in env:
            raise ValueError("oauth-values-must-be-provided-through-selected-secret")
    for key in ("WOODPECKER_GITHUB", "WOODPECKER_GITLAB", "WOODPECKER_BITBUCKET", "WOODPECKER_CODEBERG"):
        if key in env and str(env[key]).lower() not in {"false", "0", ""}:
            raise ValueError("another-forge-is-enabled")
    for key in ("WOODPECKER_FORGEJO", "WOODPECKER_FORGEJO_URL", "WOODPECKER_GITEA", "WOODPECKER_GITEA_URL"):
        env.pop(key, None)
    provider = plan["provider"].upper()
    env["WOODPECKER_" + provider] = "true"
    env["WOODPECKER_" + provider + "_URL"] = plan["url"].rstrip("/")
    secrets = server.get("extraSecretNamesForEnvFrom", [])
    if not isinstance(secrets, list) or any(not isinstance(item, str) for item in secrets): raise ValueError("invalid-secret-references")
    server["extraSecretNamesForEnvFrom"] = [item for item in secrets if item not in {plan["previous_oauth_secret"], plan["oauth_secret"]}] + [plan["oauth_secret"]]
    return output


def gitea_values_ready(path):
    """Return whether values explicitly select Gitea and reference its OAuth Secret."""
    try:
        documents = loads_strict_yaml_all(read_bounded_text(path, max_bytes=1024 * 1024))
        if len(documents) != 1: return False
        values = documents[0]
        server = values["server"]; env = server["env"]
        enabled = lambda key: str(env.get(key, "false")).lower() in {"true", "1"}
        if (not enabled("WOODPECKER_GITEA")
                or str(env.get("WOODPECKER_FORGEJO", "false")).lower() not in {"false", "0", ""}): return False
        secrets = server.get("extraSecretNamesForEnvFrom")
        expected_secret = os.environ.get("WOODPECKER_GITEA_OAUTH_SECRET_NAME", "woodpecker-gitea-oauth").strip()
        if not isinstance(secrets, list) or expected_secret not in secrets: return False
        plan = {"schema_version": 1, "provider": "gitea", "url": env.get("WOODPECKER_GITEA_URL"),
                "oauth_secret": expected_secret, "previous_oauth_secret": expected_secret}
        render(values, plan)
        return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--values", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    try:
        documents = loads_strict_yaml_all(read_bounded_text(args.values, max_bytes=1024 * 1024))
        if len(documents) != 1: raise ValueError("one-values-document-required")
        values = documents[0]
        plan = loads_strict_json(read_bounded_text(args.plan, max_bytes=16384))
        result = render(values, plan)
    except Exception:
        print("Woodpecker forge rendering failed; inspect the plan and values locally.", file=sys.stderr)
        return 1
    print(yaml.safe_dump(result, sort_keys=False), end="")
    return 0


if __name__ == "__main__": sys.exit(main())
