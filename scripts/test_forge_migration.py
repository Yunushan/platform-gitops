#!/usr/bin/env python3
"""Self-test the forge migration proof helper with local Git repositories."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_SCRIPT = ROOT / "scripts" / "forge_migration.py"


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
        self.labels: dict[tuple[str, str], list[dict[str, object]]] = {}
        self.milestones: dict[tuple[str, str], list[dict[str, object]]] = {}
        self.issues: dict[tuple[str, str], list[dict[str, object]]] = {}
        self.next_id = 1
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

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
                if len(parts) >= 5 and parts[1] == "repos" and parts[4] in {"labels", "milestones", "issues"}:
                    provider = parts[0]
                    repository = f"{parts[2]}/{parts[3]}"
                    resource_id = parts[5] if len(parts) > 5 else None
                    subresource = parts[6] if len(parts) > 6 else None
                    return provider, repository, parts[4], resource_id, subresource
                if len(parts) >= 4 and parts[1] == "projects" and parts[3] in {"labels", "milestones", "issues"}:
                    provider = parts[0]
                    repository = parts[2]
                    resource_id = parts[4] if len(parts) > 4 else None
                    subresource = parts[5] if len(parts) > 5 else None
                    return provider, repository, parts[3], resource_id, subresource
                raise AssertionError(f"unexpected fake API path: {self.path}")

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
                issues = api.issues.setdefault((provider, repository), [])
                if resource_id is not None:
                    issue = self.find_resource(issues, resource_id, "title")
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
                provider, repository, resource, resource_id, subresource = self.parse_resource_path()
                body = self.read_json()
                if resource == "issues" and resource_id is not None and subresource in {"comments", "notes"}:
                    issue = self.find_resource(api.issues.setdefault((provider, repository), []), resource_id, "title")
                    if issue is None:
                        self.send_json(404, {"message": "not found"})
                        return
                    comment = {"id": api.next_id, "body": str(body.get("body") or "")}
                    api.next_id += 1
                    issue.setdefault("comments", []).append(comment)
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
                    issues = api.issues.setdefault((provider, repository), [])
                    issue = self.find_resource(issues, resource_id, "title")
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
                    self.send_json(200, self.response_issue(provider, repository, issue))
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
                self.update_resource()

            def do_PUT(self) -> None:
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
    git(["clone", "--bare", str(work), str(bare)])
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
                        "metadata": {"labels": "required", "milestones": "required", "issues": "required"},
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
            issues = proof["repositories"][0]["metadata"]["issues"]
            if not issues["verified"]:
                raise AssertionError(f"{direction}: issue proof did not verify")
            if issues["created"] != 1 or issues["updated"] != 1 or issues["comments_created"] != 3:
                raise AssertionError(
                    f"{direction}: expected one created issue, one updated issue, and three comments, got {issues}"
                )
            if issues["extra"] != ["destination-only issue"]:
                raise AssertionError(f"{direction}: expected destination-only issue to be reported as extra")

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
            if not verified_again["repositories"][0]["metadata"]["issues"]["verified"]:
                raise AssertionError(f"{direction}: verify command did not prove issues")


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
                        "metadata": {"pull_requests": "required"},
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


def main() -> int:
    if not shutil.which("git"):
        print("git is required for forge migration tests", file=sys.stderr)
        return 1
    test_mirror_migration()
    test_metadata_migration_for_supported_directions()
    test_required_metadata_fails_closed()
    print("Forge migration helper self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
