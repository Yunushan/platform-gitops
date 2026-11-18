"""Validate Gerrit HA plugin change-index forwarding over isolated loopback."""

from functools import partial
import json
import os
import re
import time
from urllib.parse import urlencode, unquote

from bounded_subprocess import run_bounded
from test_gerrit_review_runtime_fixture import server


def exercise(test, prepare_plugins, *, failover=False, automatic_reindex=False, vip_test=False):
    """Caller supplies the checksum-pinned plugin preparation used by the single-site test."""

    def configure(site, probe_password, *, peer, peer_password, shared=None, primary=None):
        first = primary is None
        if first:
            shared = site.parent / "ha-state"
            shared.mkdir(mode=0o700)
            os.chown(shared, 65534, 65534)
            git_data = shared / "git"
            (site / "git").rename(git_data)
        else:
            git_data = shared / "git"
        config = site / "etc/gerrit.config"
        args = ["git", "config", "--file", str(config)]
        if first:
            server_id = run_bounded([*args, "--get", "gerrit.serverId"],
                                    timeout=10, text=True, output_max_bytes=4096, check=True).stdout.strip()
            test.assertRegex(server_id, r"^[0-9a-fA-F-]{36}$")
        else:
            server_id = run_bounded(["git", "config", "--file", str(primary / "etc/gerrit.config"),
                                     "--get", "gerrit.serverId"],
                                    timeout=10, text=True, output_max_bytes=4096, check=True).stdout.strip()
        for key, value in (("gerrit.basePath", str(git_data)), ("gerrit.serverId", server_id),
                           ("gerrit.instanceId", "fixture-a" if first else "fixture-b")):
            run_bounded([*args, key, value], timeout=10, output_max_bytes=4096, check=True)
        os.chown(config, 65534, 65534)
        prepare_plugins(site, probe_password, shared=shared, peer=peer, peer_password=peer_password)

    def check_forward(source, receiver, tag):
        source_api, peer_api = source["api"], receiver["api"]
        subject = f"Peer index fixture {tag}"
        path = "/a/changes/?" + urlencode({"q": f'project:peer-canary message:"{subject}"'})
        test.assertEqual(peer_api.call("fixture-admin", path), [])
        write = source_api.call("fixture-admin", "/a/changes/", {
            "project": "peer-canary", "branch": "main", "subject": subject, "work_in_progress": False,
        })
        change = write["_number"]
        deadline = time.monotonic() + 35
        observed = []
        while time.monotonic() < deadline:
            for member in (source, receiver):
                if member["process"].poll() is not None:
                    raise RuntimeError("An original Gerrit peer stopped")
            observed = [code for method, target, code in receiver["peer_requests"] if method in {"POST", "PUT"}
                        and "/plugins/high-availability/index/change/" in target
                        and re.search(rf"[/~]{change}(?:$|\?)", unquote(target))]
            if any(200 <= code < 300 for code in observed):
                matches = peer_api.call("fixture-admin", path)
                if len(matches) == 1 and matches[0]["_number"] == change:
                    return
            time.sleep(0.25)
        summary = [(method, "index/change" in path, code) for method, path, code in receiver["peer_requests"]]
        test.fail(f"Peer did not receive and index the new change. Matching statuses {observed}; observed methods/statuses {summary}")

    router_hostname = "review.example.invalid" if failover else None
    if vip_test:
        from test_gateway_edge_vip_fixture import require_scope
        from test_gerrit_review_runtime_fixture import namespace

        require_scope(gerrit=True)
        namespace()
        for address in ("192.0.2.30", "192.0.2.31"):
            run_bounded(["ip", "addr", "add", address + "/32", "dev", "lo"], timeout=10, check=True)
    with server(active_processors=2, peer_port=17081, ssh=failover, router_hostname=router_hostname,
                prepare_site=partial(configure, peer=17082), vip_test=vip_test) as first:
        source = first["api"]
        if not first["admin"].get("username"):
            source.call("fixture-admin", "/a/accounts/self/username", {"username": "fixture-admin"}, method="PUT")
        source.call("fixture-admin", "/a/projects/peer-canary", {
            "create_empty_commit": True, "branches": ["main"],
        }, method="PUT")
        if failover:
            from test_gerrit_artifacts import seed_review

            seed_review(source)
        with server(active_processors=2, peer_port=17082, source_site=first, ssh=failover,
                    router_hostname=router_hostname, vip_test=vip_test,
                    prepare_site=partial(configure, peer=17081, shared=first["root"] / "ha-state",
                                         primary=first["site"])) as second:
            test.assertNotEqual(first["process"].pid, second["process"].pid)
            test.assertNotEqual((first["site"] / "index").stat().st_ino, (second["site"] / "index").stat().st_ino)
            check_forward(first, second, "alpha")
            check_forward(second, first, "beta")
            print(json.dumps({"bidirectional_gerrit_change_index_forwarding_verified": True,
                              "shared_local_storage_fixture": True}), flush=True)
            if vip_test:
                from test_gerrit_vip_fixture import exercise as exercise_vip

                exercise_vip(test, first, second, router_hostname)
            elif failover:
                from test_gerrit_routed_git_fixture import exercise as exercise_routed

                exercise_routed(test, first, router_hostname, backup=second, automatic_reindex=automatic_reindex)
