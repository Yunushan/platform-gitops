#!/usr/bin/env python3
"""Self-test the GitOps profile completeness checker."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
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
        app_dir / "kustomization.yaml",
        """apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: forgejo
helmCharts:
  - name: forgejo
    namespace: forgejo
""",
    )
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
        repo / "gitops/clusters/rke2-main/premium-3node/apps/quoted-app/kustomization.yaml",
        """apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: quoted-app
helmCharts:
  - name: quoted-app
    namespace: quoted-app
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
        (repo / "gitops/clusters/rke2-main/premium-3node/apps/forgejo/private-values.yaml").unlink()

        write(
            repo / "profiles/premium-3node.yaml",
            """profile: premium-3node
internal_ca_optional: step-ca
""",
        )
        write(
            repo / "gitops/clusters/rke2-main/premium-3node/apps/step-ca/kustomization.yaml",
            """apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
helmCharts:
  - name: step-ca
""",
        )
        write(
            repo / "gitops/clusters/rke2-main/premium-3node/apps/step-ca/values.yaml",
            """ca:
  name: <STEP_CA_NAME>
""",
        )
        premium_apps = repo / "gitops/clusters/rke2-main/premium-3node/platform-apps.yaml"
        premium_apps.write_text(
            premium_apps.read_text(encoding="utf-8")
            + """---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: step-ca
spec:
  source:
    repoURL: <THIS_REPO_URL>
    path: gitops/clusters/rke2-main/premium-3node/apps/step-ca
""",
            encoding="utf-8",
        )

        previous_step_ca_mode = os.environ.get("STEP_CA_MODE")
        try:
            os.environ.pop("STEP_CA_MODE", None)
            rc, output = run_check(checker, repo)
            if rc != 0:
                raise AssertionError(
                    f"disabled optional step-ca should not block profile check\n{output}"
                )

            os.environ["STEP_CA_MODE"] = "bootstrap"
            rc, output = run_check(checker, repo)
            if rc == 0 or "<STEP_CA_NAME>" not in output:
                raise AssertionError(
                    f"enabled step-ca must be checked as a required application\n{output}"
                )
        finally:
            if previous_step_ca_mode is None:
                os.environ.pop("STEP_CA_MODE", None)
            else:
                os.environ["STEP_CA_MODE"] = previous_step_ca_mode

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

    with tempfile.TemporaryDirectory(prefix="platform-profile-check-missing-include-") as tmp:
        repo = Path(tmp)
        create_fixture(repo)
        write(
            repo / "profiles/broken-profile.yaml",
            """profile: broken-profile
includes:
  - gitops/clusters/rke2-main/apps/does-not-exist
""",
        )

        rc, output = run_check(checker, repo, "broken-profile")
        if rc == 0:
            raise AssertionError("expected missing profile include path to fail")
        if "references missing path(s): gitops/clusters/rke2-main/apps/does-not-exist" not in output:
            raise AssertionError(f"expected failure output to name the missing profile include path\n{output}")

    with tempfile.TemporaryDirectory(prefix="platform-profile-check-inherited-placeholder-") as tmp:
        repo = Path(tmp)
        create_fixture(repo)
        write(
            repo / "profiles/parent-profile.yaml",
            """profile: parent-profile
summary: <PROFILE_SUMMARY>
includes:
  - gitops/clusters/rke2-main/premium-3node/apps/forgejo
""",
        )
        write(
            repo / "profiles/child-profile.yaml",
            """profile: child-profile
inherits: parent-profile
includes:
  - gitops/clusters/rke2-main/premium-3node/apps/quoted-app
""",
        )

        rc, output = run_check(checker, repo, "child-profile")
        if rc == 0:
            raise AssertionError("expected inherited profile placeholder to fail")
        if "<PROFILE_SUMMARY>" not in output:
            raise AssertionError(f"expected failure output to name inherited profile placeholder\n{output}")

    print("GitOps profile checker self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
