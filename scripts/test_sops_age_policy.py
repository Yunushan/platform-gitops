#!/usr/bin/env python3
"""Validate the SOPS + age starter policy is safe and useful."""

from __future__ import annotations

from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "config/sops.age.example.yaml"
SECRETS_DOC = ROOT / "docs/SECRETS_AND_PRIVACY.md"
README = ROOT / "README.md"
RELEASE_GUIDE = ROOT / "docs/RELEASE_GUIDE.md"

AGE_RECIPIENT_RE = re.compile(r"\bage1[ac-hj-np-z02-9]{58,}\b", re.IGNORECASE)


def fail(message: str) -> int:
    print(f"SOPS age policy validation failed: {message}", file=sys.stderr)
    return 1


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AssertionError(f"missing required file: {path.relative_to(ROOT)}")


def main() -> int:
    problems: list[str] = []
    policy = read(POLICY)

    for needle in (
        "creation_rules:",
        "path_regex: ^(private|secrets|rendered)/.*",
        "path_regex: ^(config|inventory)/.*",
        "path_regex: ^gitops/.*/.*(secret|credential|datasource).*",
        "encrypted_regex:",
        "data",
        "stringData",
        "password",
        "token",
        "secretKey",
        "accessKey",
        "secretAccessKey",
        "clientSecret",
        "credentials",
        "datasource",
        "age: age1REPLACE_WITH_PUBLIC_AGE_RECIPIENT",
    ):
        if needle not in policy:
            problems.append(f"{POLICY.relative_to(ROOT)} is missing required text: {needle}")

    if "AGE-SECRET-KEY-" in policy:
        problems.append("SOPS age example must never contain an age private key")
    real_recipients = [
        recipient
        for recipient in AGE_RECIPIENT_RE.findall(policy)
        if recipient.lower() != "age1replace_with_public_age_recipient"
    ]
    if real_recipients:
        problems.append("SOPS age example must not contain real age recipients")

    for path, required in (
        (
            SECRETS_DOC,
            (
                "config/sops.age.example.yaml",
                "age-keygen",
                "age1REPLACE_WITH_PUBLIC_AGE_RECIPIENT",
                "private age key must stay outside Git",
                "Review the rules with your security team",
            ),
        ),
        (
            README,
            (
                "config/sops.age.example.yaml",
                "SOPS + age starter policy",
                "keep age private keys outside Git",
            ),
        ),
        (
            RELEASE_GUIDE,
            (
                "SOPS/age recipient policy",
                "private deployment repositories",
            ),
        ),
    ):
        text = read(path)
        for needle in required:
            if needle not in text:
                problems.append(f"{path.relative_to(ROOT)} is missing required text: {needle}")

    if problems:
        for problem in problems:
            print(f" - {problem}", file=sys.stderr)
        return 1

    print("SOPS age policy validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
