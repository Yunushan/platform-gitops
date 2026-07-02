#!/usr/bin/env python3
"""Self-test the GitOps profile completeness checker."""
from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
CHECKER_PATH = ROOT / "scripts/check_gitops_profile.py"
sys.dont_write_bytecode = True


def load_checker():
    spec = importlib.util.spec_from_file_location("check_gitops_profile", CHECKER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {CHECKER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_check(checker, repo: Path, profile: str = "premium-3node") -> tuple[int, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = checker.check_profile(repo, profile)
    return rc, stdout.getvalue() + stderr.getvalue()


def create_fixture(repo: Path) -> None:
    write(
        repo / "gitops/clusters/rke2-main/projects/platform-project.yaml",
        """apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: platform
  namespace: argocd
spec:
  destinations:
    - namespace: '*'
      server: https://kubernetes.default.svc
  sourceRepos:
    - '*'
""",
    )
    write(
        repo / "gitops/clusters/rke2-main/premium-3node/platform-apps.yaml",
        """apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: forgejo
  namespace: argocd
spec:
  project: platform
  source:
    repoURL: <THIS_REPO_URL>
    targetRevision: main
    path: gitops/clusters/rke2-main/premium-3node/apps/forgejo
  destination:
    server: https://kubernetes.default.svc
    namespace: forgejo
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: quoted-app
  namespace: argocd
spec:
  project: platform
  source:
    repoURL: <THIS_REPO_URL>
    targetRevision: main
    path: "gitops/clusters/rke2-main/premium-3node/apps/quoted-app" # quoted and commented path
    destination:
    server: https://kubernetes.default.svc
    namespace: quoted-app
""",
    )
    write(
        repo / "gitops/clusters/rke2-main/platform-apps.yaml",
        """apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: default-app
  namespace: argocd
spec:
  project: platform
  source:
    repoURL: <THIS_REPO_URL>
    targetRevision: main
    path: gitops/clusters/rke2-main/apps/default-app
  destination:
    server: https://kubernetes.default.svc
    namespace: default-app
""",
    )
    app_dir = repo / "gitops/clusters/rke2-main/premium-3node/apps/forgejo"
    write(
        app_dir / "deployment.yaml",
        """apiVersion: apps/v1
kind: Deployment
metadata:
  name: forgejo
spec:
  replicas: 1
""",
    )
    write(app_dir / "charts/upstream-template.yaml", "value: <IGNORED_CHART_PLACEHOLDER>\n")
    write(app_dir / "crds/upstream-template.yaml", "value: <IGNORED_CRD_PLACEHOLDER>\n")
    write(app_dir / "values.example.yaml", "value: <IGNORED_EXAMPLE_PLACEHOLDER>\n")
    write(
        repo / "gitops/clusters/rke2-main/premium-3node/apps/quoted-app/deployment.yaml",
        """apiVersion: apps/v1
kind: Deployment
metadata:
  name: quoted-app
spec:
  replicas: 1
""",
    )
    write(
        repo / "gitops/clusters/rke2-main/apps/default-app/deployment.yaml",
        """apiVersion: apps/v1
kind: Deployment
metadata:
  name: default-app
spec:
  replicas: 1
""",
    )


def main() -> int:
    checker = load_checker()
    with tempfile.TemporaryDirectory(prefix="platform-profile-check-") as tmp:
        repo = Path(tmp)
        create_fixture(repo)

        rc, output = run_check(checker, repo)
        if rc != 0:
            raise AssertionError(f"expected clean profile to pass, got rc={rc}\n{output}")

        rc, output = run_check(checker, repo, "default")
        if rc != 0:
            raise AssertionError(f"expected clean default profile to pass, got rc={rc}\n{output}")

        write(
            repo / "gitops/clusters/rke2-main/premium-3node/apps/forgejo/private-values.yaml",
            "storage: <FORGEJO_DATA_SIZE>\n",
        )
        rc, output = run_check(checker, repo)
        if rc == 0:
            raise AssertionError("expected unresolved profile placeholder to fail")
        if "<FORGEJO_DATA_SIZE>" not in output:
            raise AssertionError(f"expected failure output to name the unresolved placeholder\n{output}")
        if "Public template checkouts are expected to contain placeholders" not in output:
            raise AssertionError(f"expected failure output to explain public template placeholders\n{output}")
        if "do not use skip-incomplete output as production proof" not in output:
            raise AssertionError(f"expected failure output to reject skip-incomplete as production proof\n{output}")
        if "IGNORED_CHART_PLACEHOLDER" in output or "IGNORED_CRD_PLACEHOLDER" in output:
            raise AssertionError(f"vendored chart/CRD placeholders should stay ignored\n{output}")
        if "IGNORED_EXAMPLE_PLACEHOLDER" in output:
            raise AssertionError(f"example placeholders should stay ignored\n{output}")

    with tempfile.TemporaryDirectory(prefix="platform-profile-check-missing-") as tmp:
        repo = Path(tmp)
        create_fixture(repo)
        missing_app = repo / "gitops/clusters/rke2-main/premium-3node/apps/quoted-app"
        for file_path in sorted(missing_app.rglob("*"), reverse=True):
            file_path.unlink()
        missing_app.rmdir()

        rc, output = run_check(checker, repo)
        if rc == 0:
            raise AssertionError("expected missing Application source path to fail")
        if "quoted-app: missing path" not in output:
            raise AssertionError(f"expected failure output to name the missing source path\n{output}")

    print("GitOps profile checker self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
