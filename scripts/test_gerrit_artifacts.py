#!/usr/bin/env python3
"""Check archive identities without treating modeled classes as runnable plugins."""

import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import sys
import unittest
from unittest import mock
import zipfile
from urllib.parse import urlsplit

import gerrit_artifacts as artifacts


class LivePluginTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("GERRIT_PLUGIN_TEST_RUNTIME") == "1"
                         or os.environ.get("GERRIT_ROUTED_GIT_TEST") == "1"
                         or os.environ.get("GERRIT_PEER_INDEX_TEST") == "1"
                         or os.environ.get("GERRIT_VIP_TEST") == "1"
                         or os.environ.get("GERRIT_AUTO_REINDEX_TEST") == "1",
                         "Requires the isolated Gerrit plugin runtime fixture")
    def test_stable_gerrit_loads_reviewed_plugins(self):
        from activate_gerrit_host import healthy_body
        from check_gerrit_router_backends import parse_health
        from gerrit_storage import REQUIRED_HEALTHCHECKS, check_health_configuration
        from test_gerrit_review_runtime_fixture import artifact, server

        pins = {
            "high-availability.jar": ("4bb0362338ef1ee704f6ec83cec9617e2975bdd67c42989dfcbaa85d8ecf8622", "0da3ec4"),
            "healthcheck.jar": ("5b716890adc0beef87873c0422d45249f3d739a8f463f53fed6d8649ad876f78", "52ba0d2"),
            "global-refdb.jar": ("387baacf2645fd7101104dbf3735d1a9eae88d8c1a77d71bc4a967f15dc2d32d", "9d182b4"),
        }
        directory = Path(os.environ["GERRIT_PLUGIN_TEST_ARTIFACTS"]).resolve(strict=True)
        routed = os.environ.get("GERRIT_ROUTED_GIT_TEST") == "1"
        paired = os.environ.get("GERRIT_PEER_INDEX_TEST") == "1"
        automatic_reindex = os.environ.get("GERRIT_AUTO_REINDEX_TEST") == "1"
        vip_test = os.environ.get("GERRIT_VIP_TEST") == "1"
        if vip_test:
            self.assertTrue(routed and paired and not automatic_reindex,
                            "VIP verification requires the isolated routed peer fixture")
        if automatic_reindex:
            self.assertTrue(routed and paired, "Automatic reindex requires two routed Gerrit sites")

        def write_private(path, data):
            path.write_text(data, encoding="utf-8")
            path.chmod(0o600)
            os.chown(path, 65534, 65534)

        def prepare(site, probe_password, *, shared=None, peer=None, peer_password=None):
            if shared is None:
                shared = site.parent / "ha-state"
                shared.mkdir(mode=0o700)
                os.chown(shared, 65534, 65534)
            for relative, (identity, entry) in artifacts.PLUGINS.items():
                target = site / relative
                source = directory / target.name
                artifact(source, pins[target.name][0])
                artifacts.inspect_plugin(source, "3.13.10", identity, entry)
                shutil.copyfile(source, target)
                target.chmod(0o644)
            auto = " enabled = true\n delay = 10s\n" if automatic_reindex and peer == 17082 else " enabled = false\n"
            write_private(site / "etc/high-availability.config",
                          f"[main]\n sharedDirectory = {shared}\n"
                          f'[peerInfo]\n strategy = static\n[peerInfo "static"]\n url = http://127.0.0.1:{peer or 1}\n'
                          f"[autoReindex]\n{auto}[indexSync]\n enabled = false\n"
                          + (f"[http]\n user = fixture-admin\n password = {peer_password}\n maxTries = 5\n retryInterval = 1s\n" if peer_password else ""))
            health = "[healthcheck]\n timeout = 5000\n"
            for name in REQUIRED_HEALTHCHECKS:
                health += f'[healthcheck "{name}"]\n enabled = true\n'
                if name == "auth":
                    health += f" username = fixture-admin\n password = {probe_password}\n"
                if name == "gitspace":
                    health += " minFreeDiskPercent = 1\n"
                if name == "querychanges":
                    health += " query = project:plugin-canary status:open\n limit = 1\n"
            write_private(site / "etc/healthcheck.config", health)
            check_health_configuration(site)

        if paired:
            from test_gerrit_peer_index_fixture import exercise

            exercise(self, prepare, failover=routed, automatic_reindex=automatic_reindex, vip_test=vip_test)
            return
        router_hostname = "review.example.invalid" if routed else None
        with server(active_processors=2, prepare_site=prepare, ssh=routed, router_hostname=router_hostname) as running:
            api = running["api"]
            plugins = api.call("fixture-admin", "/a/plugins/?all")
            for identity in ("healthcheck", "high-availability"):
                item = plugins[identity]
                self.assertEqual(item["id"], identity)
                self.assertEqual(item["version"], pins[identity + ".jar"][1])
                self.assertIs(item.get("disabled", False), False)
            def health():
                connection = http.client.HTTPConnection("127.0.0.1", running["backend_port"], timeout=20)
                try:
                    authority = urlsplit(running["origin"]).netloc
                    connection.request("GET", "/config/server/healthcheck~status", headers={
                        "Host": authority, "X-Forwarded-Proto": "https", "X-Forwarded-Port": authority.rsplit(":", 1)[1],
                    })
                    response = connection.getresponse()
                    raw = response.read(65537)
                    self.assertLessEqual(len(raw), 65536)
                    return response.status, raw
                finally:
                    connection.close()

            code, raw = health()
            self.assertEqual(code, 500)
            status = json.loads(raw.removeprefix(b")]}'\n"))
            self.assertEqual(status["querychanges"]["result"], "failed")
            self.assertFalse(healthy_body(raw))
            self.assertEqual(parse_health(code, raw)["check_failures"], ["querychanges"])
            seed_review(api)
            code, raw = health()
            status = json.loads(raw.removeprefix(b")]}'\n"))
            results = {name: status.get(name, {}).get("result") for name in REQUIRED_HEALTHCHECKS}
            self.assertEqual(code, 200, str(results))
            self.assertTrue(healthy_body(raw), str(results))
            self.assertTrue(parse_health(code, raw)["health_verified"])
            if routed:
                from test_gerrit_routed_git_fixture import exercise

                exercise(self, running, router_hostname)


def seed_review(api):
    api.call("fixture-admin", "/a/projects/plugin-canary", {
        "create_empty_commit": True, "branches": ["main"],
    }, method="PUT")
    api.call("fixture-admin", "/a/changes/", {
        "project": "plugin-canary", "branch": "main", "subject": "Healthcheck canary",
        "work_in_progress": False,
    })


def zipped(entries):
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', compression=zipfile.ZIP_DEFLATED) as selected:
        for name, value in entries.items():
            entry = zipfile.ZipInfo(name, (2000, 1, 1, 0, 0, 0)); entry.compress_type = zipfile.ZIP_DEFLATED
            selected.writestr(entry, value)
    return result.getvalue()


def populate_artifacts(site, version='3.13.1', *, api_version=None):
    """Synthetic metadata only; these marker classes must never execute."""
    files = {'bin/gerrit.war': zipped({'META-INF/MANIFEST.MF': b'Manifest-Version: 1.0\r\nMain-Class: Main\r\n\r\n',
        artifacts.VERSION_JAR: zipped({artifacts.VERSION_RESOURCE: ('v' + version + '\n').encode()})})}
    for path, (identity, entry) in artifacts.PLUGINS.items():
        fields = 'Manifest-Version: 1.0\r\nGerrit-ApiType: plugin\r\nGerrit-ApiVersion: ' + (version if api_version is None else api_version) + '\r\n'
        if identity is not None: fields += 'Gerrit-PluginName: ' + identity + '\r\nGerrit-Module: ' + entry + '\r\n'
        files[path] = zipped({'META-INF/MANIFEST.MF': (fields + '\r\n').encode(), entry.replace('.', '/') + '.class': b'\xca\xfe\xba\xbeMODELED-NOT-RUNNABLE'})
    for relative, content in files.items():
        path = site / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
    return {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        populate_artifacts(self.root)

    def test_exact_release_metadata_is_separate_from_live_compatibility_and_provenance(self):
        result = artifacts.verify(self.root, '3.13.1')
        self.assertTrue(result['artifact_metadata_verified'])
        for name in ('live_plugin_compatibility_verified', 'publisher_signature_verified', 'ha_verified'): self.assertFalse(result[name])

    def test_snapshot_wrong_release_and_wrong_role_refuse_without_echoing_metadata(self):
        for version in ('3.13.2-SNAPSHOT', '3.13.2', '3.14.0', 'private-marker'):
            with self.subTest(version=version):
                populate_artifacts(self.root, api_version=version)
                with self.assertRaises(artifacts.ArtifactError) as caught: artifacts.verify(self.root, '3.13.1')
                self.assertNotIn(version, str(caught.exception))
        populate_artifacts(self.root)
        (self.root / 'plugins/high-availability.jar').write_bytes((self.root / 'plugins/healthcheck.jar').read_bytes())
        with self.assertRaises(artifacts.ArtifactError): artifacts.verify(self.root, '3.13.1')
        populate_artifacts(self.root)
        with self.assertRaises(artifacts.ArtifactError): artifacts.inspect_war(self.root / 'bin/gerrit.war', '3.13.2')

    def test_manifest_continuations_are_parsed_and_duplicate_headers_cannot_hide_api_drift(self):
        path = self.root / 'plugins/high-availability.jar'
        with zipfile.ZipFile(path) as selected: entries = {name: selected.read(name) for name in selected.namelist()}
        original = entries['META-INF/MANIFEST.MF']
        entries['META-INF/MANIFEST.MF'] = original.replace(b'highavailability.Module', b'highavailability.Mod\r\n ule')
        path.write_bytes(zipped(entries)); artifacts.verify(self.root, '3.13.1')
        entries['META-INF/MANIFEST.MF'] = original.replace(b'\r\n\r\n', b'\r\ngerrit-apiversion: 3.13.2-SNAPSHOT\r\n\r\n')
        path.write_bytes(zipped(entries))
        with self.assertRaises(artifacts.ArtifactError): artifacts.verify(self.root, '3.13.1')

    def test_archive_directory_limits_are_enforced_before_zipfile_allocation(self):
        data = bytearray((self.root / 'bin/gerrit.war').read_bytes()); offset = data.rfind(b'PK\x05\x06')
        struct.pack_into('<HH', data, offset + 8, 8193, 8193)
        path = self.root / 'oversized.jar'; path.write_bytes(data)
        with mock.patch.object(artifacts.zipfile, 'ZipFile') as parser:
            with self.assertRaises(artifacts.ArtifactError):
                with artifacts.archive(path): pass
        parser.assert_not_called()
        data = (self.root / 'bin/gerrit.war').read_bytes(); offset = data.rfind(b'PK\x05\x06')
        path.write_bytes(data[:offset] + b'PK\x06\x07' + b'\0' * 16 + data[offset:])
        with mock.patch.object(artifacts.zipfile, 'ZipFile') as parser:
            with self.assertRaises(artifacts.ArtifactError):
                with artifacts.archive(path): pass
        parser.assert_not_called()

    def test_missing_excessive_and_invalid_entries_refuse(self):
        path = self.root / 'bin/gerrit.war'
        for entries in ({'META-INF/MANIFEST.MF': b'Manifest-Version: 1.0\r\nMain-Class: Main\r\n\r\n'},
                {'META-INF/MANIFEST.MF': b'x' * 65537}, {'not-a-manifest': b'value'}):
            path.write_bytes(zipped(entries))
            with self.assertRaises(artifacts.ArtifactError): artifacts.inspect_war(path, '3.13.1')
        path.write_bytes(b'not-a-zip')
        with self.assertRaises(artifacts.ArtifactError): artifacts.inspect_war(path, '3.13.1')
        for version in ('3.13.1-SNAPSHOT', 'latest', '../private', True):
            with self.assertRaises(artifacts.ArtifactError): artifacts.verify(self.root, version)

    def test_copied_runtime_helper_runs_in_isolated_python_without_cwd_imports(self):
        import json
        from bounded_subprocess import run_bounded
        package = self.root / 'runtime'; package.mkdir()
        helper = package / 'gerrit_artifacts.py'; helper.write_bytes(Path(artifacts.__file__).read_bytes())
        hostile = self.root / 'hostile'; hostile.mkdir()
        (hostile / 'zipfile.py').write_text('raise RuntimeError("Untrusted cwd module loaded")\n')
        program = 'import importlib.util,json,sys; s=importlib.util.spec_from_file_location("checked",sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(json.dumps(m.verify(sys.argv[2],"3.13.1")))'
        result = run_bounded([sys.executable, '-I', '-B', '-c', program, str(helper), str(self.root)], cwd=hostile,
            timeout=20, output_max_bytes=16384, text=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(json.loads(result.stdout)['artifact_metadata_verified'])

    @unittest.skipUnless(os.name == 'posix', 'Linux no-follow artifact inspection')
    def test_symlink_is_not_followed(self):
        target = self.root / 'link.jar'; target.symlink_to(self.root / 'bin/gerrit.war')
        with self.assertRaises(artifacts.ArtifactError): artifacts.inspect_war(target, '3.13.1')


if __name__ == '__main__': unittest.main()
