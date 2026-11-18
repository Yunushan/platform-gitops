"""Allura-authorized smart Git HTTP for the pinned Allura deployment."""

from contextlib import ExitStack
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from bounded_subprocess import run_bounded, stream_stdout
from strict_json import loads_strict_json
from paste.deploy import appconfig
from webob import Request, Response, exc

# Allura applies dependency patches when this module is loaded.
from allura.config.middleware import _make_core_app
from allura import model as M
from allura.controllers.rest import is_active_user
from allura.controllers.root import RootController
from allura.lib.security import has_access
from forgegit.git_main import ForgeGitApp
from tg import expose, request, response


INTERNAL = "platform.allura.git_authorization"
AUTHORIZE = "/platform_git_authorization"
SEGMENT = r"[a-zA-Z0-9][a-zA-Z0-9_-]*"
ROUTE = re.compile(
    rf"/git/(?P<project>{SEGMENT}(?:/{SEGMENT})+)/(?P<tool>{SEGMENT})(?:\.git)?/"
    r"(?P<operation>info/refs|git-upload-pack|git-receive-pack)"
)
TOKEN = re.compile(r"Bearer ([A-Za-z0-9_-]{16,128})", re.IGNORECASE)
PACK = {"git-upload-pack": "read", "git-receive-pack": "write"}
CHALLENGE = {"WWW-Authenticate": 'Bearer realm="Allura Git"', "Cache-Control": "no-store"}
MAX_BODY = 1024 ** 3
CHUNK = 64 * 1024
BACKEND_TIMEOUT = 90


class GitRoot(RootController):
    @expose("json")
    def platform_git_authorization(self):
        details = request.environ.get(INTERNAL)
        if details is None:
            raise exc.HTTPNotFound()
        token_value, project_path, tool, permission = details
        token = M.OAuthAccessToken.query.get(api_key=token_value, is_bearer=True)
        user = token.user if token else None
        if not is_active_user(user) or user.is_anonymous():
            response.status_int = 401
            return {}

        namespace, _, project_name = project_path.partition("/")
        neighborhood = M.Neighborhood.query.get(url_prefix=f"/{namespace}/")
        if neighborhood is None:
            raise exc.HTTPNotFound()
        project = M.Project.query.get(
            neighborhood_id=neighborhood._id, shortname=neighborhood.shortname_prefix + project_name,
            deleted=False,
        )
        if project is None or (project.is_user_project and not project.get_userproject_user()):
            raise exc.HTTPNotFound()
        app = project.app_instance(tool)
        if not isinstance(app, ForgeGitApp) or not has_access(app, permission, user=user, project=project):
            raise exc.HTTPNotFound()
        repo = app.repo
        if repo is None:
            raise exc.HTTPNotFound()
        return {"repository": repo.full_fs_path, "user": user.username}


def git_request(req):
    path = req.path_info
    route = ROUTE.fullmatch(path) if len(path) <= 1024 else None
    if route is None:
        raise exc.HTTPNotFound()
    operation = route["operation"]
    if operation == "info/refs":
        if req.method != "GET":
            raise exc.HTTPMethodNotAllowed(headers={"Allow": "GET"})
        if req.query_string not in {"service=" + service for service in PACK}:
            raise exc.HTTPNotFound()
        service = req.query_string.removeprefix("service=")
    else:
        if req.method != "POST":
            raise exc.HTTPMethodNotAllowed(headers={"Allow": "POST"})
        if req.query_string:
            raise exc.HTTPBadRequest()
        service = operation
        if req.environ.get("CONTENT_TYPE") != f"application/x-{service}-request":
            raise exc.HTTPUnsupportedMediaType()
    return route, PACK[service]


class BackendOutput:
    def __init__(self, stream, lifetime):
        self.stream = stream
        self.lifetime = lifetime

    def __iter__(self):
        try:
            while chunk := self.stream.read1(CHUNK):
                yield chunk
        finally:
            self.close()

    def close(self):
        self.lifetime.close()


def copy_request(req, body):
    length = req.content_length
    if length is not None and length > MAX_BODY:
        raise exc.HTTPRequestEntityTooLarge()
    size = 0
    stream = req.environ["wsgi.input"]
    while length is None or size < length:
        limit = CHUNK if length is None else min(CHUNK, length - size)
        chunk = stream.read(limit)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_BODY:
            raise exc.HTTPRequestEntityTooLarge()
        body.write(chunk)
    if length is not None and size != length:
        raise exc.HTTPBadRequest()
    body.seek(0)
    return size


def backend_response(req, repository, operation, username, backend):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/"),
        "LANG": "C.UTF-8",
        "PATH_TRANSLATED": f"{repository}/{operation}",
        "GIT_HTTP_EXPORT_ALL": "1",
        "REQUEST_METHOD": req.method,
        "QUERY_STRING": req.query_string,
        "CONTENT_TYPE": req.environ.get("CONTENT_TYPE", ""),
        "REMOTE_USER": username,
        "REMOTE_ADDR": req.remote_addr or "",
    }
    for setting in ("CURL_CA_BUNDLE", "SSL_CERT_FILE", "NO_PROXY"):
        if setting in os.environ:
            env[setting] = os.environ[setting]
    if req.headers.get("Git-Protocol") in {"version=1", "version=2"}:
        env["HTTP_GIT_PROTOCOL"] = req.headers["Git-Protocol"]
    encoding = req.headers.get("Content-Encoding", "").lower()
    if encoding in {"gzip", "x-gzip"}:
        env["HTTP_CONTENT_ENCODING"] = encoding
    elif encoding not in {"", "identity"}:
        raise exc.HTTPUnsupportedMediaType()

    try:
        with ExitStack() as lifetime:
            with tempfile.TemporaryFile() as body:
                size = copy_request(req, body) if req.method == "POST" else 0
                env["CONTENT_LENGTH"] = str(size)
                stream = lifetime.enter_context(stream_stdout([backend], stdin=body, env=env,
                                                               timeout=BACKEND_TIMEOUT))
            status = "200 OK"
            headers = []
            for _ in range(24):
                line = stream.readline(4096)
                if line in {b"\r\n", b"\n"}:
                    break
                if not line.endswith(b"\n"):
                    raise ValueError("Incomplete Git response headers")
                name, value = line.decode("ascii").rstrip("\r\n").split(":", 1)
                if name.lower() == "status":
                    status = value.strip()
                else:
                    headers.append((name, value.strip()))
            else:
                raise ValueError("Too many Git response headers")
            output = BackendOutput(stream, lifetime)
            reply = Response(status=status, headerlist=headers, app_iter=output)
            output.lifetime = lifetime.pop_all()
            return reply
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise exc.HTTPBadGateway() from None


class GitHTTP:
    def __init__(self, app, repository_root, backend):
        self.app = app
        self.repository_root = repository_root
        self.backend = backend

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path != "/git" and not path.startswith("/git/"):
            return self.app(environ, start_response)
        req = Request(environ)
        try:
            # Gunicorn sets this from TLS or a trusted reverse proxy. Raw
            # X-Forwarded-* values must never substitute for this check.
            if req.scheme != "https":
                raise exc.HTTPForbidden()
            route, permission = git_request(req)
            match = TOKEN.fullmatch(req.headers.get("Authorization", ""))
            if match is None:
                raise exc.HTTPUnauthorized(headers=CHALLENGE)
            authorization = Request.blank(
                AUTHORIZE, base_url="https://localhost",
                environ={INTERNAL: (match[1], route["project"], route["tool"], permission),
                         "HTTP_X_REQUESTED_WITH": "XMLHttpRequest"},
            ).get_response(self.app)
            result = authorization.body
            if authorization.status_int != 200:
                if authorization.status_int == 401:
                    raise exc.HTTPUnauthorized(headers=CHALLENGE)
                if authorization.status_int in {403, 404}:
                    raise exc.HTTPNotFound()
                raise exc.HTTPServiceUnavailable()
            identity = loads_strict_json(result)
            repository = Path(identity["repository"]).resolve()
            if not repository.is_relative_to(self.repository_root) or repository == self.repository_root:
                raise exc.HTTPNotFound()
            response = backend_response(req, repository, route["operation"], identity["user"], self.backend)
        except exc.HTTPException as error:
            response = error
        except OSError:
            response = exc.HTTPServiceUnavailable()
        return response(environ, start_response)


def make_app(config_file):
    config = appconfig("config:" + str(Path(config_file).resolve()))
    root = Path(config["scm.repos.root"], "git").resolve()
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Git is required by Allura")
    exec_path = run_bounded([git, "--exec-path"], text=True, timeout=5,
                            output_max_bytes=16384, check=True).stdout.strip()
    backend = Path(exec_path) / "git-http-backend"
    if not backend.is_file():
        raise RuntimeError("The Git HTTP backend is unavailable")
    app = _make_core_app(GitRoot(), config.global_conf, **config.local_conf)
    return GitHTTP(app, root, backend)
