#!/usr/bin/env python3
"""Fixed checkout step embedded in the reviewed Tekton push pipeline."""

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit


def git_environment(username: str, credentials: Path, temporary: Path) -> tuple[dict, Path]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", username):
        raise ValueError("Invalid scoped Git login")
    home, empty = temporary / "home", temporary / "empty-template"
    home.mkdir(mode=0o700)
    empty.mkdir(mode=0o700)
    askpass = temporary / "askpass"
    # Tekton inlines this script into the step. Keep the exclusive write self-contained.
    descriptor = os.open(askpass, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o700)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as script:
        script.write('#!/usr/bin/env python3\nimport os,sys\n'
        'if "Username" in sys.argv[1]:\n    print(os.environ["PLATFORM_GIT_USERNAME"])\n'
        'else:\n    with open(os.environ["PLATFORM_GIT_TOKEN_FILE"], "rb") as source:\n        token = source.read(513)\n'
        '    token = token.rstrip(b"\\r\\n")\n'
        '    if not 16 <= len(token) <= 512 or any(ch < 33 or ch > 126 for ch in token):\n        raise SystemExit(1)\n'
        '    print(token.decode("ascii"))\n')
    askpass.chmod(0o700)
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(home), "TMPDIR": str(temporary),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "https", "GIT_ASKPASS": str(askpass), "PLATFORM_GIT_USERNAME": username,
            "GIT_SSL_CAINFO": str(credentials / "ca.crt"), "PLATFORM_GIT_TOKEN_FILE": str(credentials / "token")}, empty


def branch_reference(value):
    if not isinstance(value, str) or not value.startswith("refs/heads/"):
        return False
    branch = value.removeprefix("refs/heads/")
    return (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,199}", branch) is not None
            and not any(item in branch for item in ("..", "//"))
            and all(part and not part.startswith(".") and not part.endswith((".", ".lock")) for part in branch.split("/")))


def checkout(url: str, revision: str, workspace: Path, username: str, *, runner=None, credentials: Path = Path("/run/source"), patchset_ref: str | None = None, branch_ref: str | None = None) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not re.fullmatch(r"[0-9a-f]{40}", revision) or revision == "0" * 40
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", username)):
        raise ValueError("Checkout requires a canonical HTTPS URL and an exact commit")
    if patchset_ref is not None:
        match = re.fullmatch(r"refs/changes/([0-9]{2})/([1-9][0-9]{0,9})/([1-9][0-9]{0,9})", patchset_ref) if isinstance(patchset_ref, str) else None
        if match is None or match[1] != f"{int(match[2]) % 100:02d}":
            raise ValueError("Select one canonical Gerrit patch-set ref")
    if branch_ref is not None and (patchset_ref is not None or not branch_reference(branch_ref)):
        raise ValueError("Select one explicit canonical branch reference")
    if workspace.is_symlink() or workspace.exists() and (not workspace.is_dir() or any(workspace.iterdir())):
        raise ValueError("Checkout workspace is not fresh")
    runner = runner or subprocess.run
    # Tekton's working-directory initializer may already have created this
    # directory. Only an empty directory on the fresh job volume is reusable.
    workspace.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="checkout-") as directory:
        temporary = Path(directory)
        env, empty = git_environment(username, credentials, temporary)
        # An EmptyDir mount root remains root-owned with the Pod's writable
        # fsGroup. Trust only this validated fresh workspace, never '*'.
        prefix = ["git", "-c", "safe.directory=" + str(workspace), "-c", "credential.helper=", "-c", "core.hooksPath=" + os.devnull, "-c", "http.followRedirects=false",
                  "-c", "http.sslVerify=true", "-c", "protocol.ext.allow=never", "-C", str(workspace)]
        def git(arguments, *, data=None):
            io = {"stdin": subprocess.DEVNULL} if data is None else {"input": data}
            try:
                runner([*prefix, *arguments], env=env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True, timeout=180, **io)
            except (OSError, subprocess.SubprocessError) as error:
                # Preserve exception compatibility without returning a remote
                # URL, command, credential or unbounded Git stderr to CI logs.
                error.checkout_stage = arguments[0]
                raise
        git(["init", "--template=" + str(empty)])
        selected_ref = patchset_ref or branch_ref
        local_ref = "refs/platform/patchset" if patchset_ref else "refs/platform/source"
        git(["fetch", "--depth=1", "--no-tags", "--no-recurse-submodules", "--", url, selected_ref + ":" + local_ref if selected_ref else revision])
        if patchset_ref is not None:
            # A visible Gerrit ref can be fetched without arbitrary-SHA wants.
            # A moved/mistyped ref must never build some other fetched commit.
            git(["update-ref", "--no-deref", "--stdin"], data=("verify refs/platform/patchset " + revision + "\n").encode("ascii"))
        elif branch_ref is not None:
            git(["update-ref", "--no-deref", "--stdin"], data=("verify refs/platform/source " + revision + "\n").encode("ascii"))
        git(["cat-file", "-e", revision + "^{commit}"])
        git(["checkout", "--detach", revision])
        # A verify-only ref transaction checks the object ID without writing a
        # ref, capturing arbitrary output or opening repository files directly.
        git(["update-ref", "--no-deref", "--stdin"], data=("verify HEAD " + revision + "\n").encode("ascii"))


def main() -> int:
    if os.name != "posix" or os.geteuid() == 0 or len(sys.argv) not in {5, 6}:
        return 1
    try:
        checkout(sys.argv[1], sys.argv[2], Path(sys.argv[3]), sys.argv[4], patchset_ref=sys.argv[5] if len(sys.argv) == 6 else None)
    except Exception as error:
        stage = getattr(error, "checkout_stage", "workspace-or-credentials")
        if stage not in {"init", "fetch", "update-ref", "cat-file", "checkout", "workspace-or-credentials"}: stage = "unknown"
        print("Exact-commit checkout failed [" + stage + "]; private Git diagnostics were suppressed.", file=sys.stderr)
        facts = {"workspace_writable": os.access(sys.argv[3], os.W_OK),
            "source_token_readable": os.access("/run/source/token", os.R_OK), "source_ca_readable": os.access("/run/source/ca.crt", os.R_OK),
            "git_binary_present": any(os.access(path, os.X_OK) for path in ("/usr/local/bin/git", "/usr/bin/git", "/bin/git"))}
        print("Checkout preconditions: " + ", ".join(key + "=" + str(value) for key, value in facts.items()), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
