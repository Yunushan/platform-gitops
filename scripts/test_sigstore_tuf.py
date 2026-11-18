#!/usr/bin/env python3
"""Offline trust bootstrap boundaries; real Kyverno/TUF crypto is separately gated."""

import copy
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import HTTPServer
import io
import json
import tempfile
from pathlib import Path
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler, Request

import create_sigstore_tuf as tuf
import sigstore_tuf_mirror as mirror


def fixture(root):
    def ref(name, value):
        path = root / name; path.write_bytes(value); path.chmod(0o600)
        return {"path": str(path), "sha256": hashlib.sha256(value).hexdigest()}
    now = datetime.now(timezone.utc)
    plan = {"schema_version": 1, "keys": {}, "expires": {}, "targets": {}}
    for role, (number, _, _) in tuf.ROLES.items():
        plan["keys"][role] = [ref(role + str(i), tuf.KEY_HEADER + (role + str(i)).encode()) for i in range(number)]
        delay = {"root": 30, "targets": 7, "snapshot": 2, "timestamp": 1}[role]
        plan["expires"][role] = (now + timedelta(days=delay)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for name in tuf.TARGETS:
        value = b"-----BEGIN PUBLIC KEY-----\nsynthetic" if name.endswith(".pub") else b"-----BEGIN CERTIFICATE-----\nsynthetic"
        if name.endswith(".json"): value = tuf.canonical({"mediaType": "application/vnd.dev.sigstore.trustedroot+json;version=0.1", "tlogs": [{"publicKey": {}}]})
        plan["targets"][name] = ref(name, value)
    return plan


def fake_command(arguments, root):
    if arguments[0] == "pkey": return bytes.fromhex("302a300506032b6570032100") + hashlib.sha256(Path(arguments[2]).read_bytes()).digest()
    if arguments[0] == "pkeyutl": return b"s" * 64
    raise AssertionError("Unexpected command")


class TUFTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name); self.plan = fixture(self.root)

    def test_roles_expiry_and_input_identity_are_explicit(self):
        tuf.validate(self.plan)
        for change in (lambda p:p.update(skip_tuf=True), lambda p:p["keys"].update(root=p["keys"]["root"][:1]),
                lambda p:p["expires"].update(timestamp="2000-01-01T00:00:00Z"), lambda p:p["expires"].update(root=p["expires"]["timestamp"]),
                lambda p:p["keys"]["timestamp"].__setitem__(0,p["keys"]["snapshot"][0])):
            selected = copy.deepcopy(self.plan); change(selected)
            with self.assertRaises(ValueError): tuf.validate(selected)

    def test_threshold_metadata_links_and_public_only_repository(self):
        with mock.patch.object(tuf,"require_signer"), mock.patch.object(tuf,"command",side_effect=fake_command): files = tuf.generate(self.plan)
        self.assertEqual(set(files), {"root.json","1.root.json","targets.json","snapshot.json","timestamp.json",*tuf.TARGETS,"tuf-bootstrap-status.json"})
        self.assertEqual(files["root.json"],files["1.root.json"])
        for role, (_, threshold, _) in tuf.ROLES.items():
            value = json.loads(files[role+".json"])
            self.assertEqual(len(value["signatures"]),threshold)
            self.assertEqual(value["signed"]["version"],1)
        root = json.loads(files["root.json"])["signed"]
        self.assertEqual(len(root["keys"]),8); self.assertEqual(root["roles"]["root"]["threshold"],2)
        for role,target in (("timestamp","snapshot.json"),("snapshot","targets.json")):
            value = json.loads(files[role+".json"])["signed"]["meta"][target]
            self.assertEqual(value["length"],len(files[target].encode()))
            self.assertEqual(value["hashes"]["sha256"],hashlib.sha256(files[target].encode()).hexdigest())
        self.assertFalse(any("PRIVATE KEY" in text for text in files.values()))
        status = json.loads(files["tuf-bootstrap-status.json"])
        self.assertFalse(status["admission_verified"]); self.assertFalse(status["tuf_client_acceptance_verified"])

    def test_same_key_under_different_paths_cannot_satisfy_separate_roles(self):
        a = self.plan["keys"]["root"][0]; b = self.plan["keys"]["timestamp"][0]
        Path(b["path"]).write_bytes(Path(a["path"]).read_bytes()); b["sha256"] = a["sha256"]
        with mock.patch.object(tuf,"require_signer"), mock.patch.object(tuf,"command",side_effect=fake_command), self.assertRaisesRegex(ValueError,"overlap"):
            tuf.generate(self.plan)

    def test_changed_target_or_offline_signer_failure_never_publishes(self):
        Path(self.plan["targets"]["rekor.pub"]["path"]).write_bytes(b"changed")
        with mock.patch.object(tuf,"require_signer"), mock.patch.object(tuf,"command") as command, self.assertRaises(ValueError): tuf.generate(self.plan)
        command.assert_not_called()
        with mock.patch.object(tuf,"read_bootstrap_bytes",return_value=tuf.canonical(self.plan)), mock.patch.object(tuf,"generate",side_effect=ValueError("private signing details")), mock.patch.object(tuf,"write_bundle") as write, mock.patch("sys.stderr",new_callable=io.StringIO) as output:
            self.assertEqual(tuf.main(["--plan",str(self.root/"plan"),"--output-dir",str(self.root/"output"),"--create"]),1)
            write.assert_not_called(); self.assertNotIn("private signing details",output.getvalue())

    def test_mirror_routes_are_frozen_exact_and_never_list_or_decode_paths(self):
        content = {name:(name+"-public").encode() for name in mirror.FILES}
        original = content["root.json"]
        server = HTTPServer(("127.0.0.1",0),mirror.handler(content)); thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        self.addCleanup(server.server_close)
        opener = build_opener(ProxyHandler({})); url = "http://127.0.0.1:"+str(server.server_port)
        try:
            content["root.json"] = b"changed after handler snapshot"
            with opener.open(url+"/root.json",timeout=3) as response: self.assertEqual(response.read(),original)
            with opener.open(Request(url+"/targets/trusted_root.json",method="HEAD"),timeout=3) as response:
                self.assertEqual(response.read(),b""); self.assertEqual(int(response.headers["Content-Length"]),len(content["trusted_root.json"]))
            for path in ("/", "/../root.json", "/%72oot.json", "/root.json?x=1", "/key", "/tuf-bootstrap-status.json"):
                with self.subTest(path=path), self.assertRaises(HTTPError) as failed: opener.open(url+path,timeout=3)
                self.assertEqual(failed.exception.code,404); failed.exception.close()
        finally: server.shutdown(); thread.join(timeout=5); self.assertFalse(thread.is_alive())

    def test_mirror_refuses_extra_data_unknown_listener_and_changed_content(self):
        files = {name:self.plan["targets"].get(name,self.plan["targets"]["rekor.pub"]) for name in mirror.FILES}
        key = self.plan["keys"]["timestamp"][0]
        certificate = self.root / "mirror-ca"; certificate.write_bytes(Path(self.plan["targets"]["fulcio.crt.pem"]["path"]).read_bytes())
        certref = {"path":str(certificate),"sha256":hashlib.sha256(certificate.read_bytes()).hexdigest()}
        selected = {"schema_version":1,"address":"127.0.0.1","port":8443,"certificate":certref,"private_key":key,"files":files}
        mirror.inspect(selected)
        for change in (lambda p:p.update(address="0x7f000001"),lambda p:p.update(port=True),lambda p:p["files"].update(signing_key=key),lambda p:p["files"]["root.json"].update(sha256="0"*64)):
            item=copy.deepcopy(selected);change(item)
            with self.assertRaises(ValueError):mirror.inspect(item)


if __name__ == "__main__": unittest.main()
