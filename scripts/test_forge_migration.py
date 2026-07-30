#!/usr/bin/env python3
"""Self-test the forge migration proof helper with local Git repositories."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from unittest import mock
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_SCRIPT = ROOT / "scripts" / "forge_migration.py"
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import forge_migration as migration


def run(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed rc={result.returncode}: {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def git(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], cwd=cwd, check=check)


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_eventually_consistent_metadata_comparison() -> None:
    observations = 0

    def comparison() -> dict[str, object]:
        nonlocal observations
        observations += 1
        return {"verified": observations >= 3, "observation": observations}

    result = migration.poll_verified_comparison(
        comparison,
        attempts=4,
        initial_delay_seconds=0,
    )
    if result.get("verified") is not True or observations != 3:
        raise AssertionError(
            "metadata comparison did not tolerate bounded eventual consistency"
        )

    failed = migration.poll_verified_comparison(
        lambda: {"verified": False},
        attempts=2,
        initial_delay_seconds=0,
    )
    if failed.get("verified") is not False:
        raise AssertionError("metadata comparison accepted a persistent mismatch")


def test_command_timeout_redacts_credentials() -> None:
    credential = "do-not-leak"
    command = [
        "git",
        "clone",
        f"https://operator:{credential}@git.example.test/team/repository.git",
    ]
    with (
        mock.patch.object(
            migration.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(command, 7),
        ),
        mock.patch.dict(
            os.environ,
            {"FORGE_MIGRATION_COMMAND_TIMEOUT_SECONDS": "7"},
            clear=False,
        ),
    ):
        try:
            migration.run_command(command)
        except migration.MigrationError as exc:
            message = str(exc)
        else:
            raise AssertionError("timed-out migration command unexpectedly succeeded")
    if credential in message:
        raise AssertionError("migration timeout diagnostic exposed URL credentials")
    if "<redacted>" not in message or "timed out after 7 seconds" not in message:
        raise AssertionError(f"migration timeout diagnostic was incomplete: {message}")


def normalize_fake_color(value: object) -> str:
    color = str(value or "").strip().lstrip("#").lower()
    if len(color) != 6:
        raise AssertionError(f"fake label color must be six hex characters, got {value!r}")
    return color


def normalize_fake_state(value: object) -> str:
    state = str(value or "open").strip().lower()
    if state in {"open", "active"}:
        return "open"
    if state == "closed":
        return "closed"
    raise AssertionError(f"fake milestone state must be open/active or closed, got {value!r}")


def normalize_fake_due_date(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return text[:10]


class FakeLabelApi:
    def __init__(self) -> None:
        self.repositories: dict[tuple[str, str], dict[str, object]] = {}
        self.labels: dict[tuple[str, str], list[dict[str, object]]] = {}
        self.milestones: dict[tuple[str, str], list[dict[str, object]]] = {}
        self.releases: dict[tuple[str, str], list[dict[str, object]]] = {}
        self.issues: dict[tuple[str, str], list[dict[str, object]]] = {}
        self.change_requests: dict[tuple[str, str], list[dict[str, object]]] = {}
        self.next_id = 1
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def seed_repository(self, provider: str, repository: str, default_branch: str = "legacy") -> None:
        self.repositories[(provider, repository)] = {
            "id": self.next_id,
            "full_name": repository,
            "path_with_namespace": repository,
            "default_branch": default_branch,
        }
        self.next_id += 1

    def seed(self, provider: str, repository: str, labels: list[dict[str, object]]) -> None:
        seeded: list[dict[str, object]] = []
        for label in labels:
            seeded.append(
                {
                    "id": self.next_id,
                    "name": str(label["name"]),
                    "color": normalize_fake_color(label["color"]),
                    "description": "" if label.get("description") is None else str(label.get("description")),
                }
            )
            self.next_id += 1
        self.labels[(provider, repository)] = seeded

    def seed_milestones(self, provider: str, repository: str, milestones: list[dict[str, object]]) -> None:
        seeded: list[dict[str, object]] = []
        for milestone in milestones:
            seeded.append(
                {
                    "id": self.next_id,
                    "number": self.next_id,
                    "title": str(milestone["title"]),
                    "description": "" if milestone.get("description") is None else str(milestone.get("description")),
                    "state": normalize_fake_state(milestone.get("state")),
                    "due_date": normalize_fake_due_date(milestone.get("due_date")),
                }
            )
            self.next_id += 1
        self.milestones[(provider, repository)] = seeded

    def seed_releases(self, provider: str, repository: str, releases: list[dict[str, object]]) -> None:
        seeded: list[dict[str, object]] = []
        for release in releases:
            seeded.append(
                {
                    "id": self.next_id,
                    "tag_name": str(release["tag_name"]),
                    "name": "" if release.get("name") is None else str(release.get("name")),
                    "body": "" if release.get("body") is None else str(release.get("body")),
                }
            )
            self.next_id += 1
        self.releases[(provider, repository)] = seeded

    def seed_issues(self, provider: str, repository: str, issues: list[dict[str, object]]) -> None:
        seeded: list[dict[str, object]] = []
        for issue in issues:
            comments = [
                {"id": self.next_id + index + 1, "body": str(comment)}
                for index, comment in enumerate(issue.get("comments", []))
            ]
            seeded.append(
                {
                    "id": self.next_id,
                    "number": self.next_id,
                    "iid": self.next_id,
                    "title": str(issue["title"]),
                    "body": "" if issue.get("body") is None else str(issue.get("body")),
                    "state": normalize_fake_state(issue.get("state")),
                    "labels": [str(label) for label in issue.get("labels", [])],
                    "milestone": "" if issue.get("milestone") is None else str(issue.get("milestone")),
                    "comments": comments,
                }
            )
            self.next_id += 1 + len(comments)
        self.issues[(provider, repository)] = seeded

    def seed_change_requests(
        self,
        provider: str,
        repository: str,
        requests: list[dict[str, object]],
    ) -> None:
        seeded: list[dict[str, object]] = []
        for request in requests:
            comments = [
                {"id": self.next_id + index + 1, "body": str(comment)}
                for index, comment in enumerate(request.get("comments", []))
            ]
            seeded.append(
                {
                    "id": self.next_id,
                    "number": self.next_id,
                    "iid": self.next_id,
                    "title": str(request["title"]),
                    "body": "" if request.get("body") is None else str(request.get("body")),
                    "state": normalize_fake_state(request.get("state")),
                    "source_branch": str(request["source_branch"]),
                    "target_branch": str(request["target_branch"]),
                    "labels": [str(label) for label in request.get("labels", [])],
                    "milestone": "" if request.get("milestone") is None else str(request.get("milestone")),
                    "comments": comments,
                }
            )
            self.next_id += 1 + len(comments)
        self.change_requests[(provider, repository)] = seeded

    def url_for(self, provider: str) -> str:
        if self.server is None:
            raise AssertionError("fake API is not running")
        host, port = self.server.server_address
        return f"http://{host}:{port}/{provider}"

    def __enter__(self) -> "FakeLabelApi":
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def send_json(self, status: int, payload: object) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def read_json(self) -> dict[str, object]:
                size = int(self.headers.get("Content-Length", "0"))
                if size == 0:
                    return {}
                return json.loads(self.rfile.read(size).decode("utf-8"))

            def parse_resource_path(self) -> tuple[str, str, str, str | None, str | None]:
                raw_parts = [part for part in urlsplit(self.path).path.strip("/").split("/") if part]
                parts = [unquote(part) for part in raw_parts]
                if len(parts) >= 5 and parts[1] == "repos" and parts[4] in {
                    "labels",
                    "milestones",
                    "releases",
                    "issues",
                    "pulls",
                }:
                    provider = parts[0]
                    repository = f"{parts[2]}/{parts[3]}"
                    resource_id = parts[5] if len(parts) > 5 else None
                    subresource = parts[6] if len(parts) > 6 else None
                    return provider, repository, parts[4], resource_id, subresource
                if len(parts) >= 4 and parts[1] == "projects" and parts[3] in {
                    "labels",
                    "milestones",
                    "releases",
                    "issues",
                    "merge_requests",
                }:
                    provider = parts[0]
                    repository = parts[2]
                    resource_id = parts[4] if len(parts) > 4 else None
                    subresource = parts[5] if len(parts) > 5 else None
                    return provider, repository, parts[3], resource_id, subresource
                raise AssertionError(f"unexpected fake API path: {self.path}")

            def request_parts(self) -> list[str]:
                raw_parts = [part for part in urlsplit(self.path).path.strip("/").split("/") if part]
                return [unquote(part) for part in raw_parts]

            def repository_response(self, provider: str, repository: str) -> bool:
                record = api.repositories.get((provider, repository))
                if record is None:
                    self.send_json(404, {"message": "not found"})
                    return True
                self.send_json(200, record)
                return True

            def update_repository_settings(self) -> bool:
                parts = self.request_parts()
                repository = ""
                if len(parts) == 4 and parts[1] == "repos":
                    repository = f"{parts[2]}/{parts[3]}"
                elif len(parts) == 3 and parts[1] == "projects":
                    repository = parts[2]
                else:
                    return False
                record = api.repositories.get((parts[0], repository))
                if record is None:
                    self.send_json(404, {"message": "not found"})
                    return True
                body = self.read_json()
                if "default_branch" in body:
                    record["default_branch"] = str(body["default_branch"])
                self.send_json(200, record)
                return True

            def response_label(self, provider: str, label: dict[str, object]) -> dict[str, object]:
                color = str(label["color"])
                if provider != "github":
                    color = f"#{color}"
                return {
                    "id": label["id"],
                    "name": label["name"],
                    "color": color,
                    "description": label.get("description", ""),
                }

            def response_milestone(self, provider: str, milestone: dict[str, object]) -> dict[str, object]:
                due_date = str(milestone.get("due_date") or "")
                if provider == "gitlab":
                    return {
                        "id": milestone["id"],
                        "title": milestone["title"],
                        "description": milestone.get("description", ""),
                        "state": "active" if milestone.get("state") == "open" else "closed",
                        "due_date": due_date or None,
                    }
                timestamp = f"{due_date}T00:00:00Z" if due_date else None
                if provider == "forgejo":
                    return {
                        "id": milestone["id"],
                        "title": milestone["title"],
                        "description": milestone.get("description", ""),
                        "state": milestone.get("state", "open"),
                        "deadline": timestamp,
                    }
                return {
                    "id": milestone["id"],
                    "number": milestone["number"],
                    "title": milestone["title"],
                    "description": milestone.get("description", ""),
                    "state": milestone.get("state", "open"),
                    "due_on": timestamp,
                }

            def response_release(self, provider: str, release: dict[str, object]) -> dict[str, object]:
                payload = {
                    "id": release["id"],
                    "tag_name": release["tag_name"],
                    "name": release.get("name", ""),
                }
                if provider == "gitlab":
                    payload["description"] = release.get("body", "")
                else:
                    payload["body"] = release.get("body", "")
                return payload

            def response_issue(self, provider: str, repository: str, issue: dict[str, object]) -> dict[str, object]:
                milestone_title = str(issue.get("milestone") or "")
                milestone = None
                if milestone_title:
                    for candidate in api.milestones.setdefault((provider, repository), []):
                        if candidate.get("title") == milestone_title:
                            milestone = self.response_milestone(provider, candidate)
                            break
                labels = []
                label_names = [str(label) for label in issue.get("labels", [])]
                for label_name in label_names:
                    label = self.find_resource(api.labels.setdefault((provider, repository), []), label_name, "name")
                    labels.append(self.response_label(provider, label) if label else {"name": label_name})
                if provider == "gitlab":
                    return {
                        "id": issue["id"],
                        "iid": issue["iid"],
                        "title": issue["title"],
                        "description": issue.get("body", ""),
                        "state": "opened" if issue.get("state") == "open" else "closed",
                        "labels": label_names,
                        "milestone": milestone,
                    }
                return {
                    "id": issue["id"],
                    "number": issue["number"],
                    "title": issue["title"],
                    "body": issue.get("body", ""),
                    "state": issue.get("state", "open"),
                    "labels": labels,
                    "milestone": milestone,
                }

            def response_change_request(
                self,
                provider: str,
                repository: str,
                request: dict[str, object],
            ) -> dict[str, object]:
                milestone_title = str(request.get("milestone") or "")
                milestone = None
                if milestone_title:
                    for candidate in api.milestones.setdefault((provider, repository), []):
                        if candidate.get("title") == milestone_title:
                            milestone = self.response_milestone(provider, candidate)
                            break
                label_names = [str(label) for label in request.get("labels", [])]
                if provider == "gitlab":
                    return {
                        "id": request["id"],
                        "iid": request["iid"],
                        "title": request["title"],
                        "description": request.get("body", ""),
                        "state": "opened" if request.get("state") == "open" else "closed",
                        "source_branch": request["source_branch"],
                        "target_branch": request["target_branch"],
                        "labels": label_names,
                        "milestone": milestone,
                    }
                labels = []
                for label_name in label_names:
                    label = self.find_resource(api.labels.setdefault((provider, repository), []), label_name, "name")
                    labels.append(self.response_label(provider, label) if label else {"name": label_name})
                return {
                    "id": request["id"],
                    "number": request["number"],
                    "title": request["title"],
                    "body": request.get("body", ""),
                    "state": request.get("state", "open"),
                    "head": {
                        "ref": request["source_branch"],
                        "repo": {"full_name": repository},
                    },
                    "base": {"ref": request["target_branch"]},
                    "labels": labels,
                    "milestone": milestone,
                }

            def response_comment(self, provider: str, comment: dict[str, object]) -> dict[str, object]:
                if provider == "gitlab":
                    return {"id": comment["id"], "body": comment["body"], "system": False}
                return {"id": comment["id"], "body": comment["body"]}

            def find_resource(
                self,
                resources: list[dict[str, object]],
                resource_id: str,
                name_key: str,
            ) -> dict[str, object] | None:
                for resource in resources:
                    if (
                        str(resource.get("id")) == resource_id
                        or str(resource.get("number")) == resource_id
                        or str(resource.get(name_key)) == resource_id
                    ):
                        return resource
                return None

            def find_issue_or_change_request(
                self,
                provider: str,
                repository: str,
                resource_id: str,
            ) -> dict[str, object] | None:
                issue = self.find_resource(api.issues.setdefault((provider, repository), []), resource_id, "title")
                if issue is not None:
                    return issue
                return self.find_resource(
                    api.change_requests.setdefault((provider, repository), []), resource_id, "title"
                )

            def labels_from_payload(self, provider: str, repository: str, body: dict[str, object]) -> list[str]:
                raw = body.get("labels", [])
                if isinstance(raw, str):
                    return [part.strip() for part in raw.split(",") if part.strip()]
                labels: list[str] = []
                for item in raw if isinstance(raw, list) else []:
                    if provider == "forgejo" and str(item).isdigit():
                        label = self.find_resource(api.labels.setdefault((provider, repository), []), str(item), "name")
                        if label is not None:
                            labels.append(str(label["name"]))
                            continue
                    labels.append(str(item))
                return labels

            def milestone_from_payload(self, provider: str, repository: str, body: dict[str, object]) -> str:
                raw = body.get("milestone_id")
                if raw is None:
                    raw = body.get("milestone")
                if raw is None:
                    return ""
                milestone = self.find_resource(api.milestones.setdefault((provider, repository), []), str(raw), "title")
                return str(milestone["title"]) if milestone is not None else ""

            def do_GET(self) -> None:
                parts = self.request_parts()
                if len(parts) == 2 and parts[1] == "user":
                    self.send_json(200, {"login": "destination-owner", "username": "destination-owner"})
                    return
                if len(parts) == 4 and parts[1] == "repos":
                    self.repository_response(parts[0], f"{parts[2]}/{parts[3]}")
                    return
                if len(parts) == 3 and parts[1] == "projects":
                    self.repository_response(parts[0], parts[2])
                    return
                provider, repository, resource, resource_id, subresource = self.parse_resource_path()
                if resource == "labels":
                    labels = api.labels.setdefault((provider, repository), [])
                    if resource_id is not None:
                        label = self.find_resource(labels, resource_id, "name")
                        if label is None:
                            self.send_json(404, {"message": "not found"})
                            return
                        self.send_json(200, self.response_label(provider, label))
                        return
                    self.send_json(200, [self.response_label(provider, label) for label in labels])
                    return
                if resource == "milestones":
                    milestones = api.milestones.setdefault((provider, repository), [])
                    if resource_id is not None:
                        milestone = self.find_resource(milestones, resource_id, "title")
                        if milestone is None:
                            self.send_json(404, {"message": "not found"})
                            return
                        self.send_json(200, self.response_milestone(provider, milestone))
                        return
                    self.send_json(200, [self.response_milestone(provider, milestone) for milestone in milestones])
                    return
                if resource == "releases":
                    releases = api.releases.setdefault((provider, repository), [])
                    if resource_id is not None:
                        release = self.find_resource(releases, resource_id, "tag_name")
                        if release is None:
                            self.send_json(404, {"message": "not found"})
                            return
                        self.send_json(200, self.response_release(provider, release))
                        return
                    self.send_json(200, [self.response_release(provider, release) for release in releases])
                    return
                if resource in {"pulls", "merge_requests"}:
                    requests = api.change_requests.setdefault((provider, repository), [])
                    if resource_id is not None:
                        request = self.find_resource(requests, resource_id, "title")
                        if request is None:
                            self.send_json(404, {"message": "not found"})
                            return
                        if subresource in {"comments", "notes"}:
                            self.send_json(
                                200,
                                [self.response_comment(provider, comment) for comment in request.get("comments", [])],
                            )
                            return
                        self.send_json(200, self.response_change_request(provider, repository, request))
                        return
                    self.send_json(
                        200,
                        [self.response_change_request(provider, repository, request) for request in requests],
                    )
                    return
                issues = api.issues.setdefault((provider, repository), [])
                if resource_id is not None:
                    issue = self.find_issue_or_change_request(provider, repository, resource_id)
                    if issue is None:
                        self.send_json(404, {"message": "not found"})
                        return
                    if subresource in {"comments", "notes"}:
                        self.send_json(
                            200,
                            [self.response_comment(provider, comment) for comment in issue.get("comments", [])],
                        )
                        return
                    self.send_json(200, self.response_issue(provider, repository, issue))
                    return
                self.send_json(200, [self.response_issue(provider, repository, issue) for issue in issues])

            def do_POST(self) -> None:
                parts = self.request_parts()
                if len(parts) == 3 and parts[1:] == ["user", "repos"]:
                    body = self.read_json()
                    repository = f"destination-owner/{body['name']}"
                    api.seed_repository(parts[0], repository)
                    self.send_json(201, api.repositories[(parts[0], repository)])
                    return
                if len(parts) == 4 and parts[1] == "orgs" and parts[3] == "repos":
                    body = self.read_json()
                    repository = f"{parts[2]}/{body['name']}"
                    api.seed_repository(parts[0], repository)
                    self.send_json(201, api.repositories[(parts[0], repository)])
                    return
                if len(parts) == 2 and parts[1] == "projects":
                    body = self.read_json()
                    repository = str(body["path"])
                    api.seed_repository(parts[0], repository)
                    self.send_json(201, api.repositories[(parts[0], repository)])
                    return
                provider, repository, resource, resource_id, subresource = self.parse_resource_path()
                body = self.read_json()
                if resource == "issues" and resource_id is not None and subresource in {"comments", "notes"}:
                    issue = self.find_issue_or_change_request(provider, repository, resource_id)
                    if issue is None:
                        self.send_json(404, {"message": "not found"})
                        return
                    comment = {"id": api.next_id, "body": str(body.get("body") or "")}
                    api.next_id += 1
                    issue.setdefault("comments", []).append(comment)
                    self.send_json(201, self.response_comment(provider, comment))
                    return
                if resource in {"pulls", "merge_requests"} and resource_id is not None and subresource in {"comments", "notes"}:
                    request = self.find_resource(
                        api.change_requests.setdefault((provider, repository), []), resource_id, "title"
                    )
                    if request is None:
                        self.send_json(404, {"message": "not found"})
                        return
                    comment = {"id": api.next_id, "body": str(body.get("body") or "")}
                    api.next_id += 1
                    request.setdefault("comments", []).append(comment)
                    self.send_json(201, self.response_comment(provider, comment))
                    return
                if resource_id is not None:
                    self.send_json(404, {"message": "unexpected nested POST"})
                    return
                if resource == "labels":
                    labels = api.labels.setdefault((provider, repository), [])
                    label = {
                        "id": api.next_id,
                        "name": str(body["name"]),
                        "color": normalize_fake_color(body["color"]),
                        "description": "" if body.get("description") is None else str(body.get("description")),
                    }
                    api.next_id += 1
                    labels.append(label)
                    self.send_json(201, self.response_label(provider, label))
                    return
                if resource == "milestones":
                    milestones = api.milestones.setdefault((provider, repository), [])
                    due_value = body.get("due_date")
                    if due_value is None:
                        due_value = body.get("due_on")
                    if due_value is None:
                        due_value = body.get("deadline")
                    milestone = {
                        "id": api.next_id,
                        "number": api.next_id,
                        "title": str(body["title"]),
                        "description": "" if body.get("description") is None else str(body.get("description")),
                        "state": normalize_fake_state(body.get("state")),
                        "due_date": normalize_fake_due_date(due_value),
                    }
                    api.next_id += 1
                    milestones.append(milestone)
                    self.send_json(201, self.response_milestone(provider, milestone))
                    return
                if resource == "releases":
                    release = {
                        "id": api.next_id,
                        "tag_name": str(body["tag_name"]),
                        "name": "" if body.get("name") is None else str(body.get("name")),
                        "body": "",
                    }
                    if body.get("body") is not None:
                        release["body"] = str(body.get("body"))
                    elif body.get("description") is not None:
                        release["body"] = str(body.get("description"))
                    api.next_id += 1
                    api.releases.setdefault((provider, repository), []).append(release)
                    self.send_json(201, self.response_release(provider, release))
                    return
                if resource in {"pulls", "merge_requests"}:
                    source_branch = body.get("source_branch")
                    target_branch = body.get("target_branch")
                    if source_branch is None:
                        source_branch = body.get("head")
                    if target_branch is None:
                        target_branch = body.get("base")
                    request = {
                        "id": api.next_id,
                        "number": api.next_id,
                        "iid": api.next_id,
                        "title": str(body["title"]),
                        "body": "" if body.get("description") is None else str(body.get("description")),
                        "state": "open",
                        "source_branch": str(source_branch or ""),
                        "target_branch": str(target_branch or ""),
                        "labels": self.labels_from_payload(provider, repository, body),
                        "milestone": self.milestone_from_payload(provider, repository, body),
                        "comments": [],
                    }
                    if body.get("body") is not None:
                        request["body"] = str(body.get("body"))
                    api.next_id += 1
                    api.change_requests.setdefault((provider, repository), []).append(request)
                    self.send_json(201, self.response_change_request(provider, repository, request))
                    return
                issue = {
                    "id": api.next_id,
                    "number": api.next_id,
                    "iid": api.next_id,
                    "title": str(body["title"]),
                    "body": "" if body.get("description") is None else str(body.get("description")),
                    "state": normalize_fake_state(body.get("state")),
                    "labels": self.labels_from_payload(provider, repository, body),
                    "milestone": self.milestone_from_payload(provider, repository, body),
                    "comments": [],
                }
                if body.get("body") is not None:
                    issue["body"] = str(body.get("body"))
                api.next_id += 1
                api.issues.setdefault((provider, repository), []).append(issue)
                self.send_json(201, self.response_issue(provider, repository, issue))

            def update_resource(self) -> None:
                provider, repository, resource, resource_id, _subresource = self.parse_resource_path()
                if resource_id is None:
                    self.send_json(404, {"message": "resource id is required"})
                    return
                body = self.read_json()
                if resource in {"pulls", "merge_requests"}:
                    requests = api.change_requests.setdefault((provider, repository), [])
                    request = self.find_resource(requests, resource_id, "title")
                    if request is None:
                        self.send_json(404, {"message": "not found"})
                        return
                    if body.get("title"):
                        request["title"] = str(body["title"])
                    if "body" in body:
                        request["body"] = "" if body.get("body") is None else str(body.get("body"))
                    if "description" in body:
                        request["body"] = "" if body.get("description") is None else str(body.get("description"))
                    if "labels" in body:
                        request["labels"] = self.labels_from_payload(provider, repository, body)
                    if "milestone" in body or "milestone_id" in body:
                        request["milestone"] = self.milestone_from_payload(provider, repository, body)
                    if body.get("base"):
                        request["target_branch"] = str(body["base"])
                    if body.get("target_branch"):
                        request["target_branch"] = str(body["target_branch"])
                    if body.get("state_event"):
                        request["state"] = "closed" if body["state_event"] == "close" else "open"
                    elif body.get("state"):
                        request["state"] = normalize_fake_state(body["state"])
                    self.send_json(200, self.response_change_request(provider, repository, request))
                    return
                if resource == "labels":
                    labels = api.labels.setdefault((provider, repository), [])
                    label = self.find_resource(labels, resource_id, "name")
                    if label is None:
                        self.send_json(404, {"message": "not found"})
                        return
                    if body.get("new_name") or body.get("name"):
                        label["name"] = str(body.get("new_name") or body.get("name"))
                    if body.get("color"):
                        label["color"] = normalize_fake_color(body["color"])
                    if "description" in body:
                        label["description"] = "" if body.get("description") is None else str(body.get("description"))
                    self.send_json(200, self.response_label(provider, label))
                    return
                if resource == "issues":
                    issue = self.find_issue_or_change_request(provider, repository, resource_id)
                    if issue is None:
                        self.send_json(404, {"message": "not found"})
                        return
                    if body.get("title"):
                        issue["title"] = str(body["title"])
                    if "body" in body:
                        issue["body"] = "" if body.get("body") is None else str(body.get("body"))
                    if "description" in body:
                        issue["body"] = "" if body.get("description") is None else str(body.get("description"))
                    if "labels" in body:
                        issue["labels"] = self.labels_from_payload(provider, repository, body)
                    if "milestone" in body or "milestone_id" in body:
                        issue["milestone"] = self.milestone_from_payload(provider, repository, body)
                    if body.get("state_event"):
                        issue["state"] = "closed" if body["state_event"] == "close" else "open"
                    elif body.get("state"):
                        issue["state"] = normalize_fake_state(body["state"])
                    if issue in api.change_requests.setdefault((provider, repository), []):
                        self.send_json(200, self.response_change_request(provider, repository, issue))
                    else:
                        self.send_json(200, self.response_issue(provider, repository, issue))
                    return
                if resource == "releases":
                    releases = api.releases.setdefault((provider, repository), [])
                    release = self.find_resource(releases, resource_id, "tag_name")
                    if release is None:
                        self.send_json(404, {"message": "not found"})
                        return
                    if "tag_name" in body:
                        release["tag_name"] = str(body["tag_name"])
                    if "name" in body:
                        release["name"] = "" if body.get("name") is None else str(body.get("name"))
                    if "body" in body:
                        release["body"] = "" if body.get("body") is None else str(body.get("body"))
                    if "description" in body:
                        release["body"] = "" if body.get("description") is None else str(body.get("description"))
                    self.send_json(200, self.response_release(provider, release))
                    return
                milestones = api.milestones.setdefault((provider, repository), [])
                milestone = self.find_resource(milestones, resource_id, "title")
                if milestone is None:
                    self.send_json(404, {"message": "not found"})
                    return
                if body.get("title"):
                    milestone["title"] = str(body["title"])
                if "description" in body:
                    milestone["description"] = "" if body.get("description") is None else str(body.get("description"))
                if body.get("state_event"):
                    milestone["state"] = "closed" if body["state_event"] == "close" else "open"
                elif body.get("state"):
                    milestone["state"] = normalize_fake_state(body["state"])
                due_value = body.get("due_date")
                if due_value is None:
                    due_value = body.get("due_on")
                if due_value is None:
                    due_value = body.get("deadline")
                if "due_date" in body or "due_on" in body or "deadline" in body:
                    milestone["due_date"] = normalize_fake_due_date(due_value)
                self.send_json(200, self.response_milestone(provider, milestone))

            def do_PATCH(self) -> None:
                if self.update_repository_settings():
                    return
                self.update_resource()

            def do_PUT(self) -> None:
                if self.update_repository_settings():
                    return
                self.update_resource()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


def create_source_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    work = root / "source-work"
    bare = root / "source.git"
    git(["init", str(work)])
    git(["config", "user.email", "migration-test@example.invalid"], cwd=work)
    git(["config", "user.name", "Migration Test"], cwd=work)
    (work / "README.md").write_text("# migration test\n", encoding="utf-8")
    git(["add", "README.md"], cwd=work)
    git(["commit", "-m", "initial"], cwd=work)
    git(["checkout", "-b", "feature/migration-proof"], cwd=work)
    (work / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(["add", "feature.txt"], cwd=work)
    git(["commit", "-m", "feature"], cwd=work)
    git(["tag", "v1.0.0"], cwd=work)
    git(["checkout", "master"], cwd=work)
    git(["notes", "add", "-m", "migration proof note"], cwd=work)
    git(["clone", "--bare", str(work), str(bare)])
    git(["push", str(bare), "refs/notes/commits:refs/notes/commits"], cwd=work)
    return bare


def test_mirror_migration() -> None:
    with tempfile.TemporaryDirectory(prefix="forge-migration-test-") as temp:
        root = Path(temp)
        source = create_source_repo(root)
        destination = root / "destination.git"
        git(["init", "--bare", str(destination)])
        plan = {
            "direction": "github-to-forgejo",
            "repositories": [
                {
                    "name": "example",
                    "source_url": str(source),
                    "destination_url": str(destination),
                    "wiki": False,
                    "lfs": False,
                    "metadata": {"issues": "skip", "pull_requests": "skip"},
                }
            ],
        }
        plan_path = root / "plan.json"
        proof_path = root / "proof.json"
        work_dir = root / "work"
        write_json(plan_path, plan)

        run(
            [
                sys.executable,
                str(MIGRATION_SCRIPT),
                "migrate",
                str(plan_path),
                "--work-dir",
                str(work_dir),
                "--proof",
                str(proof_path),
            ],
            cwd=ROOT,
        )
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        if not proof["verified"]:
            raise AssertionError("migration proof was not verified")
        repo = proof["repositories"][0]
        if repo["git"]["branch_count"] != 2:
            raise AssertionError(f"expected two branches, got {repo['git']['branch_count']}")
        if repo["git"]["tag_count"] != 1:
            raise AssertionError(f"expected one tag, got {repo['git']['tag_count']}")
        if repo["git"]["note_ref_count"] != 1:
            raise AssertionError(f"expected one notes ref, got {repo['git']['note_ref_count']}")
        if not repo["git"]["default_branch_verified"]:
            raise AssertionError("default branch was not verified")
        integrity = run(
            [sys.executable, str(MIGRATION_SCRIPT), "verify-proof", str(proof_path)],
            cwd=ROOT,
        )
        integrity_result = json.loads(integrity.stdout)
        if not integrity_result["accepted"]:
            raise AssertionError("migration proof integrity was not accepted")

        tampered_path = root / "tampered-proof.json"
        tampered = dict(proof)
        tampered["repositories"] = [dict(proof["repositories"][0])]
        tampered["repositories"][0]["git"] = dict(proof["repositories"][0]["git"])
        tampered["repositories"][0]["git"]["branch_count"] = 999
        write_json(tampered_path, tampered)
        tampered_result = run(
            [sys.executable, str(MIGRATION_SCRIPT), "verify-proof", str(tampered_path)],
            cwd=ROOT,
            check=False,
        )
        if tampered_result.returncode == 0:
            raise AssertionError("tampered migration proof unexpectedly passed integrity verification")

        verify_proof = root / "verify-proof.json"
        run(
            [
                sys.executable,
                str(MIGRATION_SCRIPT),
                "verify",
                str(plan_path),
                "--proof",
                str(verify_proof),
            ],
            cwd=ROOT,
        )
        verified_again = json.loads(verify_proof.read_text(encoding="utf-8"))
        if not verified_again["verified"]:
            raise AssertionError("verify command did not prove migrated refs")


def test_metadata_migration_for_supported_directions() -> None:
    with tempfile.TemporaryDirectory(prefix="forge-migration-metadata-test-") as temp, FakeLabelApi() as api:
        root = Path(temp)
        for direction in ("github-to-forgejo", "gitlab-to-forgejo", "forgejo-to-github", "forgejo-to-gitlab"):
            source_provider, destination_provider = direction.split("-to-", 1)
            source = create_source_repo(root / direction)
            destination = root / direction / "destination.git"
            git(["init", "--bare", str(destination)])
            source_repository = f"source-{source_provider}-{destination_provider}/repo"
            destination_repository = f"destination-{source_provider}-{destination_provider}/repo"
            api.seed(
                source_provider,
                source_repository,
                [
                    {"name": "bug", "color": "d73a4a", "description": "Something is broken"},
                    {"name": "ci", "color": "0e8a16", "description": "Continuous integration"},
                ],
            )
            api.seed(
                destination_provider,
                destination_repository,
                [
                    {"name": "bug", "color": "eeeeee", "description": "old description"},
                    {"name": "destination-only", "color": "cccccc", "description": "kept as extra"},
                ],
            )
            api.seed_milestones(
                source_provider,
                source_repository,
                [
                    {
                        "title": "v1.0",
                        "description": "First production release",
                        "state": "open",
                        "due_date": "2026-08-15",
                    },
                    {
                        "title": "legacy cleanup",
                        "description": "Remove old integration path",
                        "state": "closed",
                        "due_date": "",
                    },
                ],
            )
            api.seed_milestones(
                destination_provider,
                destination_repository,
                [
                    {
                        "title": "v1.0",
                        "description": "old milestone description",
                        "state": "closed",
                        "due_date": "2026-01-01",
                    },
                    {
                        "title": "destination-only",
                        "description": "kept as extra",
                        "state": "open",
                        "due_date": "",
                    },
                ],
            )
            api.seed_releases(
                source_provider,
                source_repository,
                [
                    {
                        "tag_name": "v1.0.0",
                        "name": "Platform 1.0",
                        "body": "First production release.",
                    },
                    {
                        "tag_name": "v0.9.0",
                        "name": "Platform 0.9",
                        "body": "Migration rehearsal release.",
                    },
                ],
            )
            api.seed_releases(
                destination_provider,
                destination_repository,
                [
                    {
                        "tag_name": "v1.0.0",
                        "name": "old release name",
                        "body": "old release body",
                    },
                    {
                        "tag_name": "destination-only",
                        "name": "Destination only",
                        "body": "kept as extra",
                    },
                ],
            )
            api.seed_issues(
                source_provider,
                source_repository,
                [
                    {
                        "title": "Production checklist",
                        "body": "Prepare the platform for the first rollout.",
                        "state": "open",
                        "labels": ["bug", "ci"],
                        "milestone": "v1.0",
                        "comments": ["Confirm DNS before cutover.", "Record the final proof artifact."],
                    },
                    {
                        "title": "Closed incident",
                        "body": "Document the resolved bootstrap issue.",
                        "state": "closed",
                        "labels": ["bug"],
                        "milestone": "legacy cleanup",
                        "comments": ["Resolved during migration rehearsal."],
                    },
                ],
            )
            api.seed_issues(
                destination_provider,
                destination_repository,
                [
                    {
                        "title": "Production checklist",
                        "body": "old body",
                        "state": "closed",
                        "labels": ["bug"],
                        "milestone": "legacy cleanup",
                        "comments": [],
                    },
                    {
                        "title": "destination-only issue",
                        "body": "kept as extra",
                        "state": "open",
                        "labels": [],
                        "milestone": "",
                        "comments": [],
                    },
                ],
            )
            api.seed_change_requests(
                source_provider,
                source_repository,
                [
                    {
                        "title": "Portable feature review",
                        "body": "Review the feature branch before cutover.",
                        "state": "open",
                        "source_branch": "feature/migration-proof",
                        "target_branch": "master",
                        "labels": ["bug", "ci"],
                        "milestone": "v1.0",
                        "comments": ["Check the migration proof.", "Approve after ref verification."],
                    },
                    {
                        "title": "Closed compatibility review",
                        "body": "Document the closed migration compatibility decision.",
                        "state": "closed",
                        "source_branch": "feature/migration-proof",
                        "target_branch": "master",
                        "labels": ["bug"],
                        "milestone": "legacy cleanup",
                        "comments": ["Closed after recording the decision."],
                    },
                ],
            )
            api.seed_change_requests(
                destination_provider,
                destination_repository,
                [
                    {
                        "title": "Portable feature review",
                        "body": "old request body",
                        "state": "closed",
                        "source_branch": "feature/migration-proof",
                        "target_branch": "master",
                        "labels": ["bug"],
                        "milestone": "legacy cleanup",
                        "comments": [],
                    },
                    {
                        "title": "destination-only review",
                        "body": "kept as extra",
                        "state": "open",
                        "source_branch": "feature/migration-proof",
                        "target_branch": "master",
                        "labels": [],
                        "milestone": "",
                        "comments": [],
                    },
                ],
            )
            plan = {
                "direction": direction,
                "repositories": [
                    {
                        "name": direction,
                        "source": {
                            "url": str(source),
                            "api_url": api.url_for(source_provider),
                            "api_repository": source_repository,
                        },
                        "destination": {
                            "url": str(destination),
                            "api_url": api.url_for(destination_provider),
                            "api_repository": destination_repository,
                        },
                        "metadata": {
                            "labels": "required",
                            "milestones": "required",
                            "releases": "required",
                            "issues": "required",
                            "merge_requests" if source_provider == "gitlab" else "pull_requests": "required",
                        },
                    }
                ],
            }
            plan_path = root / direction / "plan.json"
            proof_path = root / direction / "proof.json"
            write_json(plan_path, plan)
            run(
                [
                    sys.executable,
                    str(MIGRATION_SCRIPT),
                    "migrate",
                    str(plan_path),
                    "--work-dir",
                    str(root / direction / "work"),
                    "--proof",
                    str(proof_path),
                ],
                cwd=ROOT,
            )
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            labels = proof["repositories"][0]["metadata"]["labels"]
            if not labels["verified"]:
                raise AssertionError(f"{direction}: label proof did not verify")
            if labels["created"] != 1 or labels["updated"] != 1:
                raise AssertionError(f"{direction}: expected one created and one updated label, got {labels}")
            if labels["extra"] != ["destination-only"]:
                raise AssertionError(f"{direction}: expected destination-only label to be reported as extra")
            milestones = proof["repositories"][0]["metadata"]["milestones"]
            if not milestones["verified"]:
                raise AssertionError(f"{direction}: milestone proof did not verify")
            if milestones["created"] != 1 or milestones["updated"] != 1:
                raise AssertionError(
                    f"{direction}: expected one created and one updated milestone, got {milestones}"
                )
            if milestones["extra"] != ["destination-only"]:
                raise AssertionError(f"{direction}: expected destination-only milestone to be reported as extra")
            releases = proof["repositories"][0]["metadata"]["releases"]
            if not releases["verified"]:
                raise AssertionError(f"{direction}: release proof did not verify")
            if releases["created"] != 1 or releases["updated"] != 1:
                raise AssertionError(
                    f"{direction}: expected one created and one updated release, got {releases}"
                )
            if releases["extra"] != ["destination-only"]:
                raise AssertionError(f"{direction}: expected destination-only release to be reported as extra")
            issues = proof["repositories"][0]["metadata"]["issues"]
            if not issues["verified"]:
                raise AssertionError(f"{direction}: issue proof did not verify")
            if issues["created"] != 1 or issues["updated"] != 1 or issues["comments_created"] != 3:
                raise AssertionError(
                    f"{direction}: expected one created issue, one updated issue, and three comments, got {issues}"
                )
            if issues["extra"] != ["destination-only issue"]:
                raise AssertionError(f"{direction}: expected destination-only issue to be reported as extra")
            change_requests = proof["repositories"][0]["metadata"]["change_requests"]
            if not change_requests["verified"]:
                raise AssertionError(f"{direction}: change request proof did not verify")
            if (
                change_requests["created"] != 1
                or change_requests["updated"] != 1
                or change_requests["comments_created"] != 3
            ):
                raise AssertionError(
                    f"{direction}: expected one created, one updated, and three change request comments, got {change_requests}"
                )
            if change_requests["extra"] != ["feature/migration-proof->master:destination-only review"]:
                raise AssertionError(f"{direction}: expected destination-only change request to be reported as extra")

            verify_proof = root / direction / "verify-proof.json"
            run(
                [sys.executable, str(MIGRATION_SCRIPT), "verify", str(plan_path), "--proof", str(verify_proof)],
                cwd=ROOT,
            )
            verified_again = json.loads(verify_proof.read_text(encoding="utf-8"))
            if not verified_again["repositories"][0]["metadata"]["labels"]["verified"]:
                raise AssertionError(f"{direction}: verify command did not prove labels")
            if not verified_again["repositories"][0]["metadata"]["milestones"]["verified"]:
                raise AssertionError(f"{direction}: verify command did not prove milestones")
            if not verified_again["repositories"][0]["metadata"]["releases"]["verified"]:
                raise AssertionError(f"{direction}: verify command did not prove releases")
            if not verified_again["repositories"][0]["metadata"]["issues"]["verified"]:
                raise AssertionError(f"{direction}: verify command did not prove issues")
            if not verified_again["repositories"][0]["metadata"]["change_requests"]["verified"]:
                raise AssertionError(f"{direction}: verify command did not prove change requests")


def test_destination_repository_creation_for_supported_directions() -> None:
    with tempfile.TemporaryDirectory(prefix="forge-migration-create-test-") as temp, FakeLabelApi() as api:
        root = Path(temp)
        for direction in ("github-to-forgejo", "gitlab-to-forgejo", "forgejo-to-github", "forgejo-to-gitlab"):
            source_provider, destination_provider = direction.split("-to-", 1)
            source = create_source_repo(root / direction)
            destination = root / direction / "destination.git"
            git(["init", "--bare", str(destination)])
            repository_name = f"created-{source_provider}-{destination_provider}"
            destination_repository = (
                repository_name
                if destination_provider == "gitlab"
                else f"destination-owner/{repository_name}"
            )
            plan = {
                "direction": direction,
                "repositories": [
                    {
                        "name": f"create-{direction}",
                        "source_url": str(source),
                        "destination": {
                            "url": str(destination),
                            "api_url": api.url_for(destination_provider),
                            "api_repository": destination_repository,
                            "create": "required",
                            "private": True,
                            "description": "Created by the migration proof test",
                        },
                    }
                ],
            }
            plan_path = root / direction / "create-plan.json"
            proof_path = root / direction / "create-proof.json"
            write_json(plan_path, plan)
            run(
                [
                    sys.executable,
                    str(MIGRATION_SCRIPT),
                    "migrate",
                    str(plan_path),
                    "--work-dir",
                    str(root / direction / "create-work"),
                    "--proof",
                    str(proof_path),
                ],
                cwd=ROOT,
            )
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            lifecycle = proof["repositories"][0]["destination_repository"]
            if lifecycle["status"] != "created" or not lifecycle["verified"]:
                raise AssertionError(f"{direction}: destination repository creation was not proven: {lifecycle}")
            if not lifecycle["default_branch_updated"] or not lifecycle["default_branch_verified"]:
                raise AssertionError(f"{direction}: default branch reconciliation was not proven: {lifecycle}")
            if (destination_provider, destination_repository) not in api.repositories:
                raise AssertionError(f"{direction}: fake destination repository was not created")

            verify_path = root / direction / "create-verify-proof.json"
            run(
                [sys.executable, str(MIGRATION_SCRIPT), "verify", str(plan_path), "--proof", str(verify_path)],
                cwd=ROOT,
            )
            verify_proof = json.loads(verify_path.read_text(encoding="utf-8"))
            verified_lifecycle = verify_proof["repositories"][0]["destination_repository"]
            if verified_lifecycle["status"] != "existing" or not verified_lifecycle["verified"]:
                raise AssertionError(f"{direction}: destination repository re-verification failed")


def test_batch_failure_writes_proof_and_continues() -> None:
    with tempfile.TemporaryDirectory(prefix="forge-migration-batch-test-") as temp:
        root = Path(temp)
        good_source = create_source_repo(root / "good")
        bad_source = create_source_repo(root / "bad")
        good_destination = root / "good-destination.git"
        bad_destination = root / "missing" / "bad-destination.git"
        git(["init", "--bare", str(good_destination)])
        plan = {
            "direction": "github-to-forgejo",
            "repositories": [
                {
                    "name": "good",
                    "source_url": str(good_source),
                    "destination_url": str(good_destination),
                },
                {
                    "name": "bad",
                    "source_url": str(bad_source),
                    "destination_url": str(bad_destination),
                },
            ],
        }
        plan_path = root / "batch-plan.json"
        proof_path = root / "batch-proof.json"
        write_json(plan_path, plan)
        result = run(
            [
                sys.executable,
                str(MIGRATION_SCRIPT),
                "migrate",
                str(plan_path),
                "--work-dir",
                str(root / "work"),
                "--proof",
                str(proof_path),
            ],
            cwd=ROOT,
            check=False,
        )
        if result.returncode == 0:
            raise AssertionError("partially failed batch unexpectedly returned success")
        if "forge migration verification failed:" not in result.stderr or "bad:" not in result.stderr:
            raise AssertionError(
                "partially failed batch did not emit compact failure diagnostics"
            )
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        if proof["verified"]:
            raise AssertionError("partially failed batch proof unexpectedly verified")
        by_name = {repo["name"]: repo for repo in proof["repositories"]}
        if not by_name["good"]["verified"] or by_name["bad"]["verified"]:
            raise AssertionError(f"batch proof did not preserve per-repository outcomes: {by_name}")
        if by_name["bad"].get("status") != "failed" or not by_name["bad"].get("error"):
            raise AssertionError("failed repository proof is missing compact diagnostics")


def test_required_metadata_fails_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="forge-migration-test-") as temp:
        root = Path(temp)
        source = create_source_repo(root)
        destination = root / "destination.git"
        git(["init", "--bare", str(destination)])
        plan_path = root / "plan.json"
        write_json(
            plan_path,
            {
                "direction": "gitlab-to-forgejo",
                "repositories": [
                    {
                        "name": "metadata-required",
                        "source_url": str(source),
                        "destination_url": str(destination),
                        "metadata": {"release_assets": "required"},
                    }
                ],
            },
        )
        result = run(
            [sys.executable, str(MIGRATION_SCRIPT), "validate-plan", str(plan_path)],
            cwd=ROOT,
            check=False,
        )
        if result.returncode == 0:
            raise AssertionError("required unsupported metadata unexpectedly passed")
        if "metadata migration is not implemented" not in result.stderr:
            raise AssertionError(result.stderr)


def test_nonportable_change_requests_fail_closed() -> None:
    target = migration.ApiTarget(
        provider="github",
        api_url="https://api.github.test",
        repository="destination-owner/repository",
        token_env=None,
    )
    base_request = {
        "title": "Review",
        "body": "Portable review",
        "state": "open",
        "head": {"ref": "feature", "repo": {"full_name": "destination-owner/repository"}},
        "base": {"ref": "main"},
        "labels": [],
        "milestone": None,
    }
    cases = [
        ({**base_request, "state": "closed", "merged": True}, "merged"),
        ({**base_request, "draft": True}, "draft"),
        ({**base_request, "head": {"ref": "feature", "repo": {"full_name": "fork/repository"}}}, "fork"),
    ]
    for request, label in cases:
        try:
            migration.normalize_change_request(target, request, [])
        except migration.MigrationError:
            continue
        raise AssertionError(f"{label} change request unexpectedly passed the portable migration contract")


def test_change_request_plan_contract() -> None:
    for direction in ("github-to-forgejo", "gitlab-to-forgejo", "forgejo-to-github", "forgejo-to-gitlab"):
        source_provider, destination_provider = direction.split("-to-", 1)
        surface = "merge_requests" if source_provider == "gitlab" else "pull_requests"
        _direction, repositories = migration.parse_plan(
            {
                "direction": direction,
                "repositories": [
                    {
                        "name": direction,
                        "source": {
                            "url": f"https://{source_provider}.example.test/source/repository.git",
                            "api_url": f"https://{source_provider}.example.test/api",
                            "api_repository": "source/repository",
                        },
                        "destination": {
                            "url": f"https://{destination_provider}.example.test/destination/repository.git",
                            "api_url": f"https://{destination_provider}.example.test/api",
                            "api_repository": "destination/repository",
                        },
                        "metadata": {surface: "required"},
                    }
                ],
            }
        )
        preview = migration.validate_metadata_requirements(repositories[0])
        change_requests = preview["change_requests"]
        if change_requests["status"] != "planned" or change_requests["mode"] != "required":
            raise AssertionError(f"{direction}: required change request surface was not planned: {change_requests}")

    _direction, repositories = migration.parse_plan(
        {
            "direction": "gitlab-to-forgejo",
            "repositories": [
                {
                    "name": "wrong-review-surface",
                    "source": {
                        "url": "https://gitlab.example.test/source/repository.git",
                        "api_url": "https://gitlab.example.test/api/v4",
                        "api_repository": "source/repository",
                    },
                    "destination": {
                        "url": "https://forgejo.example.test/destination/repository.git",
                        "api_url": "https://forgejo.example.test/api/v1",
                        "api_repository": "destination/repository",
                    },
                    "metadata": {"pull_requests": "required"},
                }
            ],
        }
    )
    try:
        migration.validate_metadata_requirements(repositories[0])
    except migration.MigrationError:
        return
    raise AssertionError("GitLab source unexpectedly accepted pull_requests instead of merge_requests")


def test_api_read_retry_is_bounded_and_write_safe() -> None:
    target = migration.ApiTarget(
        provider="forgejo",
        api_url="http://127.0.0.1:1",
        repository="owner/repository",
        token_env=None,
    )
    original_urlopen = migration.urlopen

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"result":"ok"}'

    calls = 0

    def flaky_read(_request, timeout: int):
        nonlocal calls
        if timeout != 30:
            raise AssertionError("migration API timeout changed unexpectedly")
        calls += 1
        if calls < 3:
            raise ConnectionResetError("transient reset")
        return Response()

    try:
        migration.urlopen = flaky_read
        payload = migration.api_request(target, "GET", "repos/owner/repository")
        if payload != {"result": "ok"} or calls != 3:
            raise AssertionError(f"GET retry did not recover exactly once bounded: calls={calls}")

        calls = 0

        def failed_write(_request, timeout: int):
            nonlocal calls
            calls += 1
            raise ConnectionResetError("ambiguous write reset")

        migration.urlopen = failed_write
        try:
            migration.api_request(target, "POST", "repos/owner/repository/issues", body={"title": "x"})
        except migration.MigrationError:
            pass
        else:
            raise AssertionError("ambiguous metadata write unexpectedly succeeded")
        if calls != 1:
            raise AssertionError(f"non-idempotent write was retried: calls={calls}")
    finally:
        migration.urlopen = original_urlopen


def test_plan_rejects_literal_credentials() -> None:
    def base_plan() -> dict[str, object]:
        return {
            "direction": "gitlab-to-forgejo",
            "repositories": [
                {
                    "name": "credential-contract",
                    "source": {
                        "url": "https://gitlab.example.test/source/repository.git",
                        "token_env": "GITLAB_SOURCE_TOKEN",
                    },
                    "destination": {
                        "url": "https://forgejo.example.test/destination/repository.git",
                        "token_env": "FORGEJO_DESTINATION_TOKEN",
                    },
                }
            ],
            "services": {
                "registry": {
                    "username_env": "REGISTRY_USERNAME",
                    "password_env": "REGISTRY_PASSWORD",
                }
            },
        }

    migration.parse_plan(base_plan())

    for key in sorted(migration.SENSITIVE_LITERAL_KEYS):
        unsafe = base_plan()
        unsafe["repositories"][0]["source"][key] = "plaintext"  # type: ignore[index]
        try:
            migration.parse_plan(unsafe)
        except migration.MigrationError as exc:
            if "must not contain credential" not in str(exc):
                raise AssertionError(f"unexpected credential validation error for {key}: {exc}") from exc
        else:
            raise AssertionError(f"literal credential key {key!r} unexpectedly passed plan validation")

    for url in (
        "https://user:password@gitlab.example.test/source/repository.git",
        "https://token@gitlab.example.test/source/repository.git",
    ):
        unsafe = base_plan()
        unsafe["repositories"][0]["source"]["url"] = url  # type: ignore[index]
        try:
            migration.parse_plan(unsafe)
        except migration.MigrationError as exc:
            if "must not embed credentials in a URL" not in str(exc):
                raise AssertionError(f"unexpected URL credential validation error: {exc}") from exc
        else:
            raise AssertionError(f"credential-bearing URL unexpectedly passed plan validation: {url}")

    ssh_plan = base_plan()
    ssh_plan["repositories"][0]["source"]["url"] = "ssh://git@gitlab.example.test/source/repository.git"  # type: ignore[index]
    migration.parse_plan(ssh_plan)


def main() -> int:
    if not shutil.which("git"):
        print("git is required for forge migration tests", file=sys.stderr)
        return 1
    test_mirror_migration()
    test_eventually_consistent_metadata_comparison()
    test_command_timeout_redacts_credentials()
    test_metadata_migration_for_supported_directions()
    test_destination_repository_creation_for_supported_directions()
    test_batch_failure_writes_proof_and_continues()
    test_required_metadata_fails_closed()
    test_nonportable_change_requests_fail_closed()
    test_change_request_plan_contract()
    test_api_read_retry_is_bounded_and_write_safe()
    test_plan_rejects_literal_credentials()
    print("Forge migration helper self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
