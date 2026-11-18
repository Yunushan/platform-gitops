"""Verify real Gerrit HTTPS and SSH through the isolated Keepalived VIP."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shlex
import ssl
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit

from bounded_subprocess import run_bounded
from check_gerrit_router_backends import check_entry
from test_gateway_edge_vip_fixture import HAPROXY_SHA, VIP, exercise as exercise_vrrp
from test_gerrit_review_runtime_fixture import stop_gerrit
from test_gerrit_routed_git_fixture import git_client, prepare_review, publish_review, verify_review


def exercise(test, primary, backup, hostname):
    user = "fixture-admin"
    parsed = urlsplit(primary["origin"])
    plan = {
        "schema_version": 1, "canonical_host": hostname, "primary_host": parsed.hostname,
        "backup_host": urlsplit(backup["origin"]).hostname, "https_port": parsed.port,
        "ssh_port": primary["ssh_endpoint"]["port"], "namespace": "fixture",
        "ca_secret": "fixture-ca", "haproxy_image": "registry.example.invalid/haproxy@sha256:" + HAPROXY_SHA,
    }
    certificates = {role: (member["tls"] / "ca.crt").read_text(encoding="ascii")
                    for role, member in (("primary", primary), ("backup", backup))}
    ca = "".join(certificates.values()).encode("ascii")
    pins = {role: hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate)).hexdigest()
            for role, certificate in certificates.items()}
    key, known = (primary["root"] / name for name in ("vip-client-key", "vip-known-hosts"))
    run_bounded(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                timeout=10, check=True, output_max_bytes=16384)
    api = primary["api"]
    registered = api.call(user, "/a/accounts/self/sshkeys", key.with_suffix(".pub").read_bytes(),
                          method="POST", content_type="text/plain")
    test.assertIs(registered["valid"], True)
    host_keys = [path.read_text(encoding="ascii").split()[:2]
                 for path in (primary["site"] / "etc").glob("ssh_host_*_key.pub")]
    test.assertTrue(host_keys and all(len(value) == 2 for value in host_keys))
    known.write_text("".join(f"[{host}]:29418 " + " ".join(value) + "\n"
                             for host in (hostname, VIP) for value in host_keys), encoding="ascii")
    rows = api.call(user, "/a/changes/?q=project:plugin-canary&o=CURRENT_REVISION")
    test.assertEqual(len(rows), 1)
    change = rows[0]
    number, revision = change["_number"], change["current_revision"]
    evidence = {"address": VIP, "https_port": 8443, "ssh_port": 29418, "peer_certificate_sha256": pins,
                "username": user, "project": "plugin-canary", "number": number,
                "patchset": change["revisions"][revision]["_number"], "revision": revision}
    auth_bytes = api.users[user].encode("ascii")
    trust = primary["root"] / "vip-ca.crt"
    trust.write_bytes(ca)
    git = git_client(test, primary["root"], user, auth_bytes, trust, hostname, VIP, key, known)
    urls = {"HTTPS": f"https://{hostname}:8443/a/plugin-canary",
            "SSH": f"ssh://{user}@{VIP}:29418/plugin-canary"}
    original = api.call(user, "/a/projects/plugin-canary/branches/main")["revision"]

    def verify(edges, owners, wait, stop, start):
        waits = {}

        def observe(phase, expected_owner=None, *, prefer_backup=False, patchset=evidence):
            started = time.monotonic()
            expected_plan = {**plan, "hold_primary": prefer_backup}

            def serving():
                current = owners()
                if not current or (expected_owner is not None and current != [expected_owner]):
                    return None
                report = check_entry(expected_plan, ca, patchset, auth_bytes, key, known)
                return current[0] if report["entry_verified"] and owners() == current else None

            owner = wait(serving, seconds=45)
            waits[phase] = round(time.monotonic() - started, 3)
            print(json.dumps({"gerrit_vip_phase": phase, "https_and_ssh_verified": True,
                              "elapsed_seconds": waits[phase]}), flush=True)
            return owner

        def write_and_verify(phase, owner, member, *, use_backup=False):
            for protocol, remote in urls.items():
                test.assertEqual(owners(), [owner])
                clone = primary["root"] / f"vip-{phase}-{protocol.lower()}"
                ref, revision, _ = publish_review(test, git, member["api"], user, remote, clone, original,
                                                   f"VIP {phase} {protocol} review canary")
                _, _, _, number, patchset = ref.split("/")
                current = {**evidence, "number": int(number), "patchset": int(patchset), "revision": revision}
                observe(f"{phase}/{protocol}", owner, prefer_backup=use_backup, patchset=current)
                other_remote = urls["SSH" if protocol == "HTTPS" else "HTTPS"]
                git("fetch", "--no-tags", other_remote, ref, cwd=clone, label="Fetch review over the other protocol")
                test.assertEqual(git("rev-parse", "FETCH_HEAD", cwd=clone, label="Read fetched revision"), revision)
            print(json.dumps({"gerrit_vip_write_phase": phase, "https_and_ssh_upload_verified": True,
                              "cross_protocol_fetch_verified": True}), flush=True)

        def interrupt(protocol, phase, owner):
            label = protocol.lower() + "-" + phase
            failed = next(edge for edge in edges if edge["name"] == owner)
            survivor = next(edge for edge in edges if edge is not failed)
            clone = primary["root"] / ("vip-interrupted-" + label)
            push = ("push", urls[protocol], "HEAD:refs/for/main%notify=NONE")
            change_id, revision = prepare_review(test, git, urls[protocol], clone, original,
                                                 f"VIP interrupted {label} review canary")
            path = f"/a/changes/plugin-canary~main~{change_id}/detail"
            with ThreadPoolExecutor(max_workers=1) as executor:
                if protocol == "HTTPS":
                    with primary["interrupt_push"](phase) as reached:
                        client = executor.submit(git, *push, cwd=clone, label="Interrupted HTTPS push", expect_failure=True)
                        test.assertTrue(reached.wait(10), "Git push did not reach its interruption point")
                        test.assertFalse(client.done(), "Git client exited before router loss")
                        test.assertEqual(owners(), [owner])
                        stop(failed["haproxy"])
                        client.result(timeout=10)
                else:
                    barrier = primary["root"] / ("ssh-" + phase)
                    barrier.mkdir(mode=0o700)
                    hook = barrier / "pre-push"
                    helper = Path(__file__).with_name("gerrit_ssh_vip_test_relay.py")
                    hook.write_text("#!/bin/sh\nexec " + shlex.join([
                        sys.executable, "-B", str(helper), "hook", str(barrier), phase]) + "\n", encoding="ascii")
                    hook.chmod(0o700)
                    ssh_git = git_client(test, primary["root"], user, auth_bytes, trust, hostname, VIP, key, known,
                                         proxy_command=shlex.join([sys.executable, "-B", str(helper),
                                                                   "relay", str(barrier), phase]), hooks=barrier)
                    client = executor.submit(ssh_git, *push, cwd=clone, label="Interrupted SSH push",
                                             expect_failure=True)
                    def stored():
                        if not (barrier / "reply-held").exists():
                            return False
                        try:
                            return api.call(user, path + "?o=CURRENT_REVISION")["current_revision"] == revision
                        except HTTPError as error:
                            if error.code != 404:
                                raise
                            return False

                    try:
                        wait(lambda: (barrier / "hook-ready").exists(), seconds=10)
                        if phase == "response":
                            wait(stored, seconds=10)
                        test.assertFalse(client.done(), "Git client exited before router loss")
                        test.assertEqual(owners(), [owner])
                        stop(failed["haproxy"])
                        wait(lambda: (barrier / "router-disconnected").exists(), seconds=10)
                    finally:
                        (barrier / "release").touch()
                    client.result(timeout=10)
            new_owner = observe("router-loss-" + label, survivor["name"])
            if phase == "request":
                for member in (primary, backup):
                    with test.assertRaises(HTTPError) as missing:
                        member["api"].call(user, path)
                    test.assertEqual(missing.exception.code, 404)
                git(*push, cwd=clone, label="Retry original commit after confirming it is absent")
            ref = verify_review(test, primary["api"], user, original, change_id, revision)
            _, _, _, number, patchset = ref.split("/")
            current = {**evidence, "number": int(number), "patchset": int(patchset), "revision": revision}
            observe("interrupted-" + label, new_owner, patchset=current)
            fetch = "SSH" if protocol == "HTTPS" else "HTTPS"
            git("fetch", "--no-tags", urls[fetch], ref, cwd=clone, label="Fetch interrupted push over other protocol")
            test.assertEqual(git("rev-parse", "FETCH_HEAD", cwd=clone, label="Read fetched revision"), revision)
            print(json.dumps({"gerrit_vip_interrupted_push": label,
                              "client_observed_failure": True, "exact_review_revision_verified": True,
                              "retried_after_confirming_absence": phase == "request"}), flush=True)
            start(failed, "-recovered-" + label)
            observe("router-recovery-" + label, new_owner)
            return new_owner

        owner = observe("initial")
        owner = interrupt("HTTPS", "request", owner)
        write_and_verify("router-loss", owner, primary)
        owner = interrupt("HTTPS", "response", owner)
        owner = interrupt("SSH", "request", owner)
        owner = interrupt("SSH", "response", owner)
        stop_gerrit(primary["process"])
        test.assertIsNotNone(primary["process"].poll())
        observe("gerrit-peer-loss", owner, prefer_backup=True)
        write_and_verify("gerrit-peer-loss", owner, backup, use_backup=True)
        stop_gerrit(backup["process"])
        test.assertIsNotNone(backup["process"].poll())
        wait(lambda: owners() == [], seconds=45)
        unavailable = check_entry(plan, ca, evidence, auth_bytes, key, known)
        test.assertFalse(unavailable["https_peer_and_current_revision_verified"])
        test.assertFalse(unavailable["ssh_patchset_verified"])
        return {"gerrit_vip_router_loss_verified": True, "gerrit_vip_peer_loss_verified": True,
                "gerrit_vip_review_writes_verified": True,
                "gerrit_vip_interrupted_https_push_reconciled": True,
                "gerrit_vip_interrupted_ssh_push_reconciled": True,
                "gerrit_vip_all_peers_down_withdrawal_verified": True,
                "shared_local_storage": True, "phase_observation_seconds": waits}

    result = exercise_vrrp(test, None, gerrit={"plan": plan, "ca": ca, "verify": verify})
    print(json.dumps({key: result[key] for key in ("gerrit_vip_router_loss_verified", "gerrit_vip_peer_loss_verified",
                                                  "gerrit_vip_review_writes_verified",
                                                  "gerrit_vip_interrupted_https_push_reconciled",
                                                  "gerrit_vip_interrupted_ssh_push_reconciled",
                                                  "gerrit_vip_all_peers_down_withdrawal_verified")}), flush=True)
