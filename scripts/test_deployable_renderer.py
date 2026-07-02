#!/usr/bin/env python3
"""Self-test the skip-incomplete Argo CD Application renderer."""
from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
RENDERER_PATH = ROOT / "scripts/render_deployable_gitops_apps.py"
sys.dont_write_bytecode = True


def load_renderer():
    spec = importlib.util.spec_from_file_location("render_deployable_gitops_apps", RENDERER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {RENDERER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def render(
    renderer,
    repo: Path,
    output: Path,
    applications_file: Path | None = None,
    profile: str | None = None,
    required_paths: list[Path] | None = None,
) -> tuple[int, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    args = SimpleNamespace(
        repo_root=repo,
        applications_file=applications_file,
        profile=profile,
        repo_url="git://seed.example/platform-gitops.git",
        output=output,
        required_path=required_paths or [],
    )
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = renderer.render(args)
    return rc, stdout.getvalue() + stderr.getvalue()


def create_fixture(repo: Path) -> Path:
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
    applications_file = repo / "gitops/clusters/rke2-main/premium-3node/platform-apps.yaml"
    write(
        applications_file,
        """apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: forgejo
  namespace: argocd
spec:
  source:
    repoURL: <THIS_REPO_URL>
    targetRevision: main
    path: gitops/clusters/rke2-main/premium-3node/apps/forgejo
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: quoted-app
  namespace: argocd
spec:
  source:
    repoURL: <THIS_REPO_URL>
    targetRevision: main
    path: "gitops/clusters/rke2-main/premium-3node/apps/quoted-app" # quoted and commented path
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: incomplete-app
  namespace: argocd
spec:
  source:
    repoURL: <THIS_REPO_URL>
    targetRevision: main
    path: gitops/clusters/rke2-main/premium-3node/apps/incomplete-app
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: missing-app
  namespace: argocd
spec:
  source:
    repoURL: <THIS_REPO_URL>
    targetRevision: main
    path: gitops/clusters/rke2-main/premium-3node/apps/missing-app
""",
    )
    write(
        repo / "gitops/clusters/rke2-main/premium-3node/apps/forgejo/deployment.yaml",
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: forgejo\n",
    )
    write(
        repo / "gitops/clusters/rke2-main/premium-3node/apps/forgejo/kustomization.yaml",
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
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: quoted-app\n",
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
    incomplete_dir = repo / "gitops/clusters/rke2-main/premium-3node/apps/incomplete-app"
    write(incomplete_dir / "values.yaml", "domain: <PLATFORM_DOMAIN>\n")
    write(incomplete_dir / "charts/upstream.yaml", "ignored: <IGNORED_CHART_PLACEHOLDER>\n")
    write(incomplete_dir / "crds/upstream.yaml", "ignored: <IGNORED_CRD_PLACEHOLDER>\n")
    write(incomplete_dir / "values.example.yaml", "ignored: <IGNORED_EXAMPLE_PLACEHOLDER>\n")
    write(
        repo / "profiles/default-forgejo-woodpecker-argocd.yaml",
        """profile: default-forgejo-woodpecker-argocd
includes:
  - gitops/clusters/rke2-main/premium-3node/apps/forgejo
  - gitops/clusters/rke2-main/premium-3node/apps/quoted-app
""",
    )
    write(
        repo / "profiles/gitea-woodpecker-argocd.yaml",
        """profile: gitea-woodpecker-argocd
inherits: default-forgejo-woodpecker-argocd
remove:
  - gitops/clusters/rke2-main/premium-3node/apps/forgejo
includes:
  - gitops/clusters/rke2-main/alternatives/gitea
""",
    )
    write(
        repo / "gitops/clusters/rke2-main/alternatives/gitea/kustomization.yaml",
        """apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: gitea
helmCharts:
  - name: gitea
    version: 12.6.0
    repo: https://dl.gitea.com/charts/
    releaseName: gitea
    namespace: gitea
    valuesFile: values.yaml
""",
    )
    write(repo / "gitops/clusters/rke2-main/alternatives/gitea/values.yaml", "replicaCount: 1\n")
    return applications_file


def main() -> int:
    renderer = load_renderer()
    with tempfile.TemporaryDirectory(prefix="deployable-renderer-") as tmp:
        repo = Path(tmp)
        applications_file = create_fixture(repo)
        output = repo / "rendered.yaml"

        rc, logs = render(renderer, repo, output, applications_file=applications_file)
        if rc != 0:
            raise AssertionError(f"expected deployable subset to render, got rc={rc}\n{logs}")

        rendered = output.read_text(encoding="utf-8")
        if "git://seed.example/platform-gitops.git" not in rendered:
            raise AssertionError(f"expected <THIS_REPO_URL> to be replaced\n{rendered}")
        if "name: forgejo" not in rendered or "name: quoted-app" not in rendered:
            raise AssertionError(f"expected complete apps to be kept\n{rendered}")
        if "name: incomplete-app" in rendered or "name: missing-app" in rendered:
            raise AssertionError(f"expected incomplete and missing apps to be skipped\n{rendered}")
        if "quoted-app" not in logs or "Skipped incomplete GitOps applications" not in logs:
            raise AssertionError(f"expected logs to include kept and skipped apps\n{logs}")
        if "IGNORED_CHART_PLACEHOLDER" in logs or "IGNORED_CRD_PLACEHOLDER" in logs:
            raise AssertionError(f"vendored chart/CRD placeholders should stay ignored\n{logs}")
        if "IGNORED_EXAMPLE_PLACEHOLDER" in logs:
            raise AssertionError(f"example placeholders should stay ignored\n{logs}")

    with tempfile.TemporaryDirectory(prefix="deployable-renderer-empty-") as tmp:
        repo = Path(tmp)
        applications_file = create_fixture(repo)
        rendered_apps = repo / "gitops/clusters/rke2-main/premium-3node/apps"
        for path in sorted(rendered_apps.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        rc, logs = render(renderer, repo, repo / "rendered.yaml", applications_file=applications_file)
        if rc != 2:
            raise AssertionError(f"expected no-deployable-apps failure rc=2, got rc={rc}\n{logs}")
        if "No deployable GitOps applications remain" not in logs:
            raise AssertionError(f"expected no-deployable-apps message\n{logs}")

    with tempfile.TemporaryDirectory(prefix="deployable-renderer-required-") as tmp:
        repo = Path(tmp)
        applications_file = create_fixture(repo)
        write(
            repo / "gitops/clusters/rke2-main/projects/platform-project.yaml",
            "spec:\n  sourceRepos:\n    - <PROJECT_REPO_URL>\n",
        )
        rc, logs = render(
            renderer,
            repo,
            repo / "rendered.yaml",
            applications_file=applications_file,
            required_paths=[Path("gitops/clusters/rke2-main/projects")],
        )
        if rc != 1:
            raise AssertionError(f"expected incomplete required path failure rc=1, got rc={rc}\n{logs}")
        if "Required shared GitOps paths are incomplete" not in logs:
            raise AssertionError(f"expected required path failure message\n{logs}")
        if "<PROJECT_REPO_URL>" not in logs:
            raise AssertionError(f"expected required path placeholder in output\n{logs}")

    with tempfile.TemporaryDirectory(prefix="deployable-renderer-profile-") as tmp:
        repo = Path(tmp)
        create_fixture(repo)
        output = repo / "rendered-profile.yaml"
        rc, logs = render(renderer, repo, output, profile="gitea-woodpecker-argocd")
        if rc != 0:
            raise AssertionError(f"expected profile-catalog render to pass, got rc={rc}\n{logs}")
        rendered = output.read_text(encoding="utf-8")
        if "name: forgejo" in rendered:
            raise AssertionError(f"expected removed Forgejo app to be absent\n{rendered}")
        if "name: quoted-app" not in rendered:
            raise AssertionError(f"expected inherited known app doc to remain\n{rendered}")
        if "name: gitea" not in rendered or "namespace: gitea" not in rendered:
            raise AssertionError(f"expected generated Gitea Application from profile include\n{rendered}")
        if "git://seed.example/platform-gitops.git" not in rendered:
            raise AssertionError(f"expected repo URL replacement in profile render\n{rendered}")

    with tempfile.TemporaryDirectory(prefix="deployable-renderer-inherited-placeholder-") as tmp:
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
        rc, logs = render(renderer, repo, repo / "rendered-profile.yaml", profile="child-profile")
        if rc != 1:
            raise AssertionError(f"expected inherited profile placeholder failure rc=1, got rc={rc}\n{logs}")
        if "metadata is incomplete and cannot be skipped" not in logs:
            raise AssertionError(f"expected profile metadata failure message\n{logs}")
        if "<PROFILE_SUMMARY>" not in logs:
            raise AssertionError(f"expected inherited profile placeholder in output\n{logs}")

    with tempfile.TemporaryDirectory(prefix="deployable-renderer-missing-include-") as tmp:
        repo = Path(tmp)
        create_fixture(repo)
        write(
            repo / "profiles/broken-profile.yaml",
            """profile: broken-profile
includes:
  - gitops/clusters/rke2-main/apps/does-not-exist
""",
        )
        rc, logs = render(renderer, repo, repo / "rendered-profile.yaml", profile="broken-profile")
        if rc != 1:
            raise AssertionError(f"expected missing profile include failure rc=1, got rc={rc}\n{logs}")
        if "references missing path(s): gitops/clusters/rke2-main/apps/does-not-exist" not in logs:
            raise AssertionError(f"expected missing profile include path in output\n{logs}")

    print("Deployable GitOps application renderer self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
