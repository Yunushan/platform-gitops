#!/usr/bin/env python3
"""Behavior-test signed GitHub release ref validation without network access."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from verify_github_release_ref import (
    ReleaseRefError,
    build_release_evidence,
    validate_release_ref,
)


COMMIT_SHA = "1" * 40
TAG_OBJECT_SHA = "2" * 40


def signed_verification() -> dict[str, object]:
    return {
        "verified": True,
        "reason": "valid",
        "signature": "-----BEGIN SIGNATURE-----\nfixture\n-----END SIGNATURE-----",
        "payload": "fixture payload",
        "verified_at": "2026-07-28T00:00:00Z",
    }


def fixtures() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    ref = {
        "ref": "refs/tags/v1.2.3",
        "object": {"type": "tag", "sha": TAG_OBJECT_SHA},
    }
    tag = {
        "sha": TAG_OBJECT_SHA,
        "tag": "v1.2.3",
        "object": {"type": "commit", "sha": COMMIT_SHA},
        "verification": signed_verification(),
    }
    commit = {
        "sha": COMMIT_SHA,
        "verification": signed_verification(),
    }
    return ref, tag, commit


def expect_rejected(label: str, mutate) -> None:  # type: ignore[no-untyped-def]
    ref, tag, commit = fixtures()
    mutate(ref, tag, commit)
    try:
        validate_release_ref(
            repository="example/platform-gitops",
            tag="v1.2.3",
            expected_sha=COMMIT_SHA,
            ref_document=ref,
            tag_document=tag,
            commit_document=commit,
        )
    except ReleaseRefError:
        return
    raise AssertionError(f"release verifier accepted {label}")


def main() -> int:
    ref, tag, commit = fixtures()
    summary = validate_release_ref(
        repository="example/platform-gitops",
        tag="v1.2.3",
        expected_sha=COMMIT_SHA,
        ref_document=ref,
        tag_document=tag,
        commit_document=commit,
    )
    if summary["commit_sha"] != COMMIT_SHA or summary["tag_object_sha"] != TAG_OBJECT_SHA:
        raise AssertionError("release verifier returned incorrect immutable identities")
    generated_at = datetime(2026, 7, 28, tzinfo=timezone.utc)
    retained = build_release_evidence(
        summary,
        ref_document=ref,
        tag_document=tag,
        commit_document=commit,
        generated_at=generated_at,
    )
    if retained["result"] != "passed" or retained["commit"] != COMMIT_SHA:
        raise AssertionError("release verifier did not retain a passing commit-bound record")
    if set(retained["inputSha256"]) != {"tagRef", "annotatedTag", "commit"}:
        raise AssertionError("release evidence is missing immutable input digests")
    if any(len(value) != 64 for value in retained["inputSha256"].values()):
        raise AssertionError("release evidence input digests are not SHA-256 values")

    expect_rejected("a lightweight tag", lambda ref, _tag, _commit: ref["object"].update(type="commit"))
    expect_rejected(
        "an unsigned annotated tag",
        lambda _ref, tag, _commit: tag["verification"].update(verified=False, reason="unsigned"),
    )
    expect_rejected(
        "an unsigned release commit",
        lambda _ref, _tag, commit: commit["verification"].update(verified=False, reason="unsigned"),
    )
    expect_rejected(
        "a tag pointing at another commit",
        lambda _ref, tag, _commit: tag["object"].update(sha="3" * 40),
    )
    expect_rejected(
        "a mismatched annotated tag object",
        lambda _ref, tag, _commit: tag.update(sha="4" * 40),
    )
    expect_rejected(
        "a verification result without evidence",
        lambda _ref, tag, _commit: tag["verification"].update(signature=""),
    )

    invalid_ref, invalid_tag, invalid_commit = deepcopy(fixtures())
    try:
        validate_release_ref(
            repository="example/platform-gitops",
            tag="v1.2.3-rc.1",
            expected_sha=COMMIT_SHA,
            ref_document=invalid_ref,
            tag_document=invalid_tag,
            commit_document=invalid_commit,
        )
    except ReleaseRefError:
        pass
    else:
        raise AssertionError("release verifier accepted a prerelease tag")

    print("Signed GitHub release ref behavior validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
