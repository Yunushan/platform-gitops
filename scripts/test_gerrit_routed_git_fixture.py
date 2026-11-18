"""Exercise real review uploads through the generated HAProxy router."""

import base64
from contextlib import ExitStack, contextmanager, redirect_stdout
import hashlib
import io
import http.client
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import ssl
import subprocess
import time
from urllib.parse import urlencode, urlsplit

from bounded_subprocess import run_bounded
from check_gerrit_router_backends import check as check_backends, check_routers, main as check_main
import render_gerrit_router as router
from test_gerrit_review_runtime_fixture import artifact, backend_ready, stop_gerrit
from test_gerrit_router_runtime_fixture import HAPROXY_SHA


def git_client(test, root, user, token, ca, hostname, address, key, known, *, proxy_command=None, hooks=None):
    authorization = base64.b64encode(user.encode("ascii") + b":" + token).decode("ascii")
    env = {"HOME": str(root), "PATH": "/usr/bin:/bin", "LC_ALL": "C",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    settings = {
        "http.extraHeader": "Authorization: Basic " + authorization, "http.sslCAInfo": str(ca),
        "http.curloptResolve": f"{hostname}:8443:{address}", "http.followRedirects": "false",
    }
    env["GIT_CONFIG_COUNT"] = str(len(settings))
    for index, (name, value) in enumerate(settings.items()):
        env[f"GIT_CONFIG_KEY_{index}"] = name
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    ssh_options = [
        "/usr/bin/ssh", "-F", "/dev/null", "-i", str(key), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known}", "-o", "GlobalKnownHostsFile=/dev/null",
    ]
    if proxy_command is not None:
        ssh_options.extend(["-o", f"ProxyCommand={proxy_command}"])
    env["GIT_SSH_COMMAND"] = shlex.join(ssh_options)
    git_options = ["-c", "user.name=Fixture", "-c", f"user.email={user}@example.invalid",
                   "-c", f"core.hooksPath={hooks or os.devnull}", "-c", "commit.gpgSign=false"]

    def git(*args, cwd=root, label, expect_failure=False):
        result = run_bounded(["git", *git_options, *args], cwd=cwd, env=env, timeout=30,
                             text=True, output_max_bytes=256 * 1024)
        signals = {signal: signal in result.stderr.lower() for signal in
                   ("certificate", "host key", "permission denied", "authentication", "connection", "remote rejected")}
        if expect_failure:
            test.assertNotEqual(result.returncode, 0, f"{label} unexpectedly succeeded")
        else:
            test.assertEqual(result.returncode, 0, f"{label} failed. Signals {signals}")
        return result.stdout.strip()

    return git


def prepare_review(test, git, url, clone, original, subject, *, change_id=None):
    git("clone", "--branch", "main", url, str(clone), label="Clone review project")
    test.assertEqual(git("rev-parse", "HEAD", cwd=clone, label="Read original branch"), original)
    if change_id is None:
        change_id = "I" + os.urandom(20).hex()
    (clone / "fixture.txt").write_text(subject + "\n", encoding="utf-8")
    git("add", "fixture.txt", cwd=clone, label="Stage fixture content")
    git("commit", "-m", subject, "-m", "Change-Id: " + change_id,
        cwd=clone, label="Create synthetic review commit")
    revision = git("rev-parse", "HEAD", cwd=clone, label="Read expected review revision")
    return change_id, revision


def verify_review(test, api, user, original, change_id, revision):
    review = api.call(user, f"/a/changes/plugin-canary~main~{change_id}/detail?o=CURRENT_REVISION")
    test.assertEqual(review["current_revision"], revision)
    test.assertEqual(review["status"], "NEW")
    test.assertEqual(api.call(user, "/a/projects/plugin-canary/branches/main")["revision"], original)
    number, patchset = review["_number"], review["revisions"][revision]["_number"]
    test.assertGreater(number, 0)
    test.assertGreater(patchset, 0)
    return f"refs/changes/{number % 100:02d}/{number}/{patchset}"


def publish_review(test, git, api, user, url, clone, original, subject, *, change_id=None):
    change_id, revision = prepare_review(test, git, url, clone, original, subject, change_id=change_id)
    git("push", url, "HEAD:refs/for/main%notify=NONE", cwd=clone, label="Upload review")
    ref = verify_review(test, api, user, original, change_id, revision)
    return ref, revision, change_id


@contextmanager
def routed(running, hostname, backup=None, *, two_routers=False):
    root = running["root"] / "router"
    root.mkdir()
    os.chown(root, 65534, 65534)
    binary = root / "haproxy"
    source = Path(os.environ["GERRIT_ROUTER_TEST_HAPROXY"])
    artifact(source, HAPROXY_SHA)
    shutil.copyfile(source, binary)
    binary.chmod(0o755)
    artifact(binary, HAPROXY_SHA)
    authority = urlsplit(running["origin"])
    ca = root / "ca.crt"
    members = (running,) if backup is None else (running, backup)
    ca.write_bytes(b"".join((member["tls"] / "ca.crt").read_bytes() for member in members))
    ca.chmod(0o644)
    plan = {
        "schema_version": 1, "canonical_host": hostname, "primary_host": "127.0.0.1",
        "backup_host": "127.0.0.2", "https_port": authority.port, "ssh_port": running["ssh_endpoint"]["port"],
        "namespace": "fixture", "ca_secret": "fixture-ca",
        "haproxy_image": "registry.example.invalid/haproxy@sha256:" + HAPROXY_SHA,
    }
    @contextmanager
    def serve(config, log_path, bind_address):
        with log_path.open("xb") as log, subprocess.Popen(
            [str(binary), "-W", "-db", "-f", str(config)], cwd=root,
            user=65534, group=65534, extra_groups=[], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        ) as process:
            try:
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise ValueError("HAProxy exited before routed Git verification")
                    connection = http.client.HTTPConnection(bind_address, router.READINESS_PORT, timeout=2)
                    try:
                        connection.request("GET", "/readyz")
                        ready = connection.getresponse().status == 200
                    except ConnectionRefusedError:
                        ready = False
                    finally:
                        connection.close()
                    if ready:
                        yield process
                        if process.poll() is not None:
                            raise ValueError("The active HAProxy process stopped unexpectedly")
                        return
                    time.sleep(0.25)
                raise TimeoutError("HAProxy did not verify the Gerrit HTTPS and SSH health checks")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

    # Each process owns its own set of listeners and makes independent health
    # decisions. Relocate the binds to distinct loopback IPs for this fixture.
    count = 2 if two_routers else 1
    addresses = [f"127.0.0.{index}" for index in range(1, count + 1)]
    with ExitStack() as cleanup:
        listeners = [cleanup.enter_context(ExitStack()) for _ in addresses]

        def install(phase, index=0, **settings):
            candidate = {**plan, **settings}
            bundle = router.render(candidate, "raw")
            address = addresses[index]
            name = f"router-{index}-{phase}"
            config = root / (name + ".cfg")
            with config.open("x", encoding="utf-8") as stream:
                stream.write(bundle["haproxy.cfg"].replace(router.CA_FILE, str(ca)).replace("bind :", f"bind {address}:"))
            config.chmod(0o644)
            listeners[index].close()
            return candidate, listeners[index].enter_context(serve(config, root / (name + ".log"), address))

        originals = [install("normal", index)[1] for index in range(count)]
        yield root, ca, originals, addresses, install


def exercise(test, running, hostname, *, backup=None, automatic_reindex=False):
    api = running["api"]
    user = "fixture-admin"
    if not running["admin"].get("username"):
        api.call(user, "/a/accounts/self/username", {"username": user}, method="PUT")
    with routed(running, hostname, backup, two_routers=automatic_reindex) as (root, ca, originals, addresses, install):
        key = root / "client-key"
        generated = run_bounded(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                                timeout=10, output_max_bytes=16384)
        test.assertEqual(generated.returncode, 0, "SSH key generation failed")
        registered = api.call(user, "/a/accounts/self/sshkeys", key.with_suffix(".pub").read_bytes(),
                              method="POST", content_type="text/plain")
        test.assertIs(registered["valid"], True)
        keys = [path.read_text().split()[:2] for path in (running["site"] / "etc").glob("ssh_host_*_key.pub")]
        test.assertTrue(keys and all(len(key) == 2 for key in keys))
        known = root / "known-hosts"
        known.write_text("".join(f"[{address}]:29418 " + " ".join(key) + "\n"
                                 for address in ("127.0.0.1", hostname) for key in keys), encoding="ascii")

        authorization = base64.b64encode(f"{user}:{api.users[user]}".encode()).decode()
        git = git_client(test, root, user, api.users[user].encode("ascii"), ca, hostname, "127.0.0.1", key, known)
        urls = {
            "HTTPS": f"https://{hostname}:8443/a/plugin-canary",
            "SSH": f"ssh://{user}@127.0.0.1:29418/plugin-canary",
        }
        branch = "/a/projects/plugin-canary/branches/main"
        original = api.call(user, branch)["revision"]
        published = {}

        def await_member(member, address="127.0.0.1"):
            context = ssl.create_default_context(cafile=str(ca))
            certificate = ssl.PEM_cert_to_DER_cert((member["tls"] / "ca.crt").read_text(encoding="ascii"))
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if member["process"].poll() is not None:
                    raise RuntimeError("The expected Gerrit process stopped")
                request = http.client.HTTPSConnection(hostname, 8443, timeout=2, context=context)
                try:
                    with socket.create_connection((address, 8443), timeout=2) as transport:
                        with context.wrap_socket(transport, server_hostname=hostname) as session:
                            if session.getpeercert(binary_form=True) == certificate:
                                request.sock = session
                                request.request("GET", "/a/config/server/version",
                                                headers={"Authorization": "Basic " + authorization})
                                response = request.getresponse()
                                if (response.status == 200 and
                                        json.loads(response.read(8192).removeprefix(b")]}'\n")) == "3.13.10"):
                                    return
                except (OSError, http.client.HTTPException):
                    pass
                finally:
                    request.close()
                time.sleep(0.25)
            test.fail("The router did not select the expected Gerrit instance")

        def publish(protocol, api, phase, *, change_id=None):
            clone = root / (phase + "-" + protocol.lower())
            ref, revision, change_id = publish_review(test, git, api, user, urls[protocol], clone, original,
                                                       f"{phase} {protocol} review canary", change_id=change_id)
            print(json.dumps({"routed_git_clone_verified": protocol, "routed_review_revision_verified": True}), flush=True)
            return clone, ref, revision, change_id

        def outage_reviews_match():
            for _, _, expected, change_id in published.values():
                query = urlencode({"q": f"project:plugin-canary change:{change_id}", "o": "CURRENT_REVISION"})
                matches = api.call(user, "/a/changes/?" + query)
                if [(row["change_id"], row["current_revision"]) for row in matches] != [(change_id, expected)]:
                    return False
            return True

        await_member(running)
        if automatic_reindex:
            await_member(running, addresses[1])
        for protocol in urls:
            published[protocol] = publish(protocol, api, "primary")

        if backup is not None:
            if automatic_reindex:
                running["block_forwarding"].set()
                forwarded_before_outage = len(running["peer_requests"])
            stop_gerrit(running["process"])
            test.assertIsNotNone(running["process"].poll())
            await_member(backup)
            for protocol in urls:
                clone, ref, expected, _ = published[protocol]
                git("fetch", "--no-tags", urls[protocol], ref, cwd=clone, label=protocol + " pre-outage review fetch")
                test.assertEqual(git("rev-parse", "FETCH_HEAD", cwd=clone, label="Read surviving pre-outage review"), expected)
            _, _, old_revision, old_change_id = published["HTTPS"]
            for protocol in urls:
                existing_change = old_change_id if automatic_reindex and protocol == "HTTPS" else None
                published[protocol] = publish(protocol, backup["api"], "backup", change_id=existing_change)
            print(json.dumps({"post_failure_review_uploads_verified": ["HTTPS", "SSH"]}), flush=True)

        for upload, fetch in (("HTTPS", "SSH"), ("SSH", "HTTPS")):
            _, ref, expected, _ = published[upload]
            clone = published[fetch][0]
            git("fetch", "--no-tags", urls[fetch], ref, cwd=clone, label=fetch + " cross-protocol review fetch")
            test.assertEqual(git("rev-parse", "FETCH_HEAD", cwd=clone, label="Read fetched review revision"), expected)
        print(json.dumps({"cross_protocol_review_fetch_verified": True}), flush=True)

        if backup is not None:
            if automatic_reindex:
                recovery_plan, held_router = install("held", 0, hold_primary=True)
                inventory = [{"address": address, "port": router.READINESS_PORT} for address in addresses]
                test.assertIsNotNone(originals[0].poll())
                test.assertEqual(check_routers(recovery_plan, inventory)["routers_not_matching"], [2])
                _, second_hold = install("held", 1, hold_primary=True)
                test.assertIsNotNone(originals[1].poll())
                test.assertTrue(check_routers(recovery_plan, inventory)["router_rollout_verified"])
                await_member(backup)
            stopped = running["process"]
            running["process"] = running["restart_daemon"](offline_reindex=not automatic_reindex)
            test.assertIsNot(running["process"], stopped)
            test.assertIsNotNone(stopped.poll())
            if automatic_reindex:
                deadline = time.monotonic() + 75
                authority = urlsplit(running["origin"]).netloc
                while not backend_ready(running["backend_port"], authority):
                    if running["process"].poll() is not None or time.monotonic() >= deadline:
                        test.fail("The restarted Gerrit REST endpoint did not become ready")
                    time.sleep(0.25)
                await_member(backup)
                while not outage_reviews_match():
                    if running["process"].poll() is not None or time.monotonic() >= deadline:
                        test.fail("Automatic reindex did not recover both exact outage revisions")
                    time.sleep(0.5)
                reviewed = {"username": user, "changes": [
                    {"number": int(ref.split("/")[3]), "revision": revision}
                    for _, ref, revision, _ in published.values()]}
                auth_bytes = api.users[user].encode("ascii")
                # The router hold must permit both direct Gerrit health checks.
                ca_bytes = ca.read_bytes()
                verified = check_backends(recovery_plan, ca_bytes, evidence=reviewed, token=auth_bytes)
                test.assertTrue(verified["outage_changes_verified"], str(verified))
                reviewed["changes"][0]["revision"] = old_revision
                mismatched = check_backends(recovery_plan, ca_bytes, evidence=reviewed, token=auth_bytes)
                test.assertFalse(mismatched["outage_changes_verified"])
                test.assertEqual(mismatched["outage_changes_matched"], {"primary": 1, "backup": 1})
                pins = {role: hashlib.sha256(ssl.PEM_cert_to_DER_cert(
                    (member["tls"] / "ca.crt").read_text(encoding="ascii"))).hexdigest()
                    for role, member in (("primary", running), ("backup", backup))}
                _, ref, revision, _ = published["HTTPS"]
                _, _, _, number, patchset = ref.split("/")
                entry = {"address": addresses[0], "https_port": 8443, "ssh_port": 29418,
                         "peer_certificate_sha256": pins, "username": user, "project": "plugin-canary",
                         "number": int(number), "patchset": int(patchset), "revision": revision}
                token_file = root / "entry-reader.token"
                with os.fdopen(os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
                    stream.write(auth_bytes + b"\n")
                plan_file = root / "entry-router.json"
                entry_file = root / "entry-evidence.json"

                def entry_check(plan, evidence, exit_code):
                    plan_file.write_text(json.dumps(plan), encoding="ascii")
                    entry_file.write_text(json.dumps(evidence), encoding="ascii")
                    with redirect_stdout(io.StringIO()) as output:
                        test.assertEqual(check_main(["--plan", str(plan_file), "--backend-ca", str(ca),
                                                     "--entry-evidence", str(entry_file), "--reader-token-file",
                                                     str(token_file), "--ssh-identity", str(key),
                                                     "--known-hosts", str(known)]), exit_code)
                    return json.loads(output.getvalue())

                held_entry = entry_check(recovery_plan, entry, 0)
                test.assertTrue(held_entry["entry_verified"], str(held_entry))
                wrong_peer = entry_check({**recovery_plan, "hold_primary": False}, entry, 1)
                test.assertFalse(wrong_peer["https_peer_and_current_revision_verified"])
                test.assertTrue(wrong_peer["ssh_patchset_verified"])
                attempts = running["peer_requests"][forwarded_before_outage:]
                test.assertTrue(any(code == 503 and "/index/change/" in path for _, path, code in attempts))
                test.assertFalse(any(200 <= code < 300 for _, _, code in attempts))
                _, restarted_hold = install("held-restart", hold_primary=True)
                test.assertIsNotNone(held_router.poll())
                test.assertTrue(check_routers(recovery_plan, inventory)["router_rollout_verified"])
                await_member(backup)
                released_plan, released_router = install("released", 0, hold_primary=False)
                test.assertIsNotNone(restarted_hold.poll())
                test.assertIsNone(released_router.poll())
                await_member(running)
                test.assertEqual(check_routers(released_plan, inventory)["routers_not_matching"], [2])
                _, released_second = install("released", 1, hold_primary=False)
                test.assertIsNotNone(second_hold.poll())
                test.assertIsNone(released_second.poll())
                await_member(running, addresses[1])
                test.assertTrue(check_routers(released_plan, inventory)["router_rollout_verified"])
                for address in addresses:
                    observation = entry_check(released_plan, {**entry, "address": address}, 0)
                    test.assertTrue(observation["entry_verified"], str(observation))
                wrong_ref = entry_check(released_plan, {**entry, "patchset": entry["patchset"] + 1}, 1)
                test.assertTrue(wrong_ref["https_peer_and_current_revision_verified"])
                test.assertFalse(wrong_ref["ssh_patchset_verified"])
                print(json.dumps({"rendered_primary_hold_restart_verified": True,
                                  "entry_https_peer_and_ssh_ref_verified": True,
                                  "wrong_entry_peer_and_missing_ref_rejected": True,
                                  "two_router_rollout_convergence_verified": True,
                                  "mixed_router_rollout_rejected": True,
                                  "direct_outage_revision_check_verified": True,
                                  "old_patchset_rejected": True}), flush=True)
            await_member(running)
            test.assertTrue(outage_reviews_match(), "Recovered Gerrit did not index both exact outage revisions")
            for protocol, (clone, ref, expected, _) in published.items():
                git("fetch", "--no-tags", urls[protocol], ref, cwd=clone, label=protocol + " post-recovery review fetch")
                test.assertEqual(git("rev-parse", "FETCH_HEAD", cwd=clone, label="Read recovered review"), expected)
            for protocol in urls:
                publish(protocol, api, "recovered")
            test.assertIsNone(backup["process"].poll())
            recovery = "automatic_reindex_failback_verified" if automatic_reindex else "offline_reindex_failback_verified"
            print(json.dumps({recovery: True,
                              "outage_reviews_searchable_and_fetchable": len(published),
                              "post_failback_review_uploads_verified": ["HTTPS", "SSH"]}), flush=True)
