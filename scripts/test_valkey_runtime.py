#!/usr/bin/env python3
"""Check pinned runtime observations and bounded RESP without a company endpoint."""

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import ssl
import tempfile
import time
import unittest
from unittest import mock

import render_valkey_ha as renderer
import verify_valkey_runtime as verifier
from valkey_protocol import Connection,ReplyError


ROOT=Path(__file__).resolve().parents[1]


def fixture(runtime="raw"):
    selected=json.loads((ROOT/"examples/valkey-ha.json").read_text()); files=renderer.render(selected,runtime)
    trust={}
    for i,kind in enumerate(("ca","client_cert","client_key")):
        trust[kind+"_file"]=str((ROOT/"private/valkey-test"/(kind+".pem")).resolve()); trust[kind+"_sha256"]=str(i+1)*64
    pins={}
    for i,m in enumerate(selected["members"]):
        pins[m["name"]]={"connect_address":m["address"],"addresses":list(dict.fromkeys([m["address"],renderer.address(selected,m,runtime)])),
            "server_run_id":str(i+1)*40,"sentinel_id":str(i+4)*40,"server_cert_sha256":str(i+7)*64}
    plan={"schema_version":1,"contract_sha256":hashlib.sha256(files["valkey-contract.json"].encode()).hexdigest(),"trust":trust,
        "credentials":{"probe_env":"VALKEY_TEST_PROBE","discovery_env":"VALKEY_TEST_DISCOVERY"},"members":pins,
        "limits":{"request_seconds":5,"operation_seconds":60,"max_response_bytes":524288,"canary_ttl_seconds":180,"replication_wait_ms":3000}}
    return selected,files,plan


def write_bundle(root,files):
    for name,value in files.items(): (root/name).write_text(value,encoding="utf-8",newline="\n")


def flat(row): return [v for pair in row.items() for v in pair]
def info(row): return "# Fixture\r\n"+"\r\n".join(k+":"+str(v) for k,v in row.items())+"\r\n"


class Network:
    """Synthetic observation transport; real sockets have a separate opt-in fixture."""
    def __init__(self,plan,selected):
        self.plan=plan; self.selected=selected; self.names=list(plan["members"]); self.primary=self.names[0]; self.calls=[]; self.values={}; self.mutation=None; self.wait_count=2
    def connection(self,name,kind,*,authenticate=True,certificate=True):
        network=self
        class Client:
            written=None
            def __enter__(self):
                if not certificate: raise ssl.SSLError("fixture missing certificate")
                return self
            def __exit__(self,*args): pass
            def command(self,*args):
                network.calls.append((name,kind,args))
                if not authenticate: raise ReplyError(b"NOAUTH fixture_private_payload")
                if args[0]=="CONFIG" or args[:2]==("SENTINEL","CONFIG"): raise ReplyError(b"NOPERM fixture_private_payload")
                if args[0]=="GET":
                    if not args[1].startswith("platform-probe:"): raise ReplyError(b"NOPERM fixture_private_payload")
                    return network.values.get(args[1])
                if args[0]=="SET":
                    if name!=network.primary or args[3:5]!=("NX","PX"): raise AssertionError("Canary must be an expiring NX write to the primary")
                    network.values[args[1]]=args[2]; self.written=args[1]; return "OK"
                if args[0]=="WAIT":
                    if self.written is None: raise AssertionError("WAIT must follow SET on the same connection")
                    return network.wait_count
                if args[0]=="TTL": return 179
                value=network.response(name,kind,args)
                return network.mutation(name,kind,args,value) if network.mutation else value
        return Client()
    def response(self,name,kind,args):
        pins=self.plan["members"]; replicas=self.names[1:]; primary=self.primary; settings=self.selected["settings"]
        if args[0]=="PING": return "PONG"
        if args[:2]==("INFO","server"): return info({"valkey_version":"8.1.11","run_id":pins[name]["server_run_id"]})
        if kind=="server":
            if args[:2]==("INFO","replication"):
                return info({"role":"master" if name==primary else "slave","master_replid":"a"*40,"connected_slaves":"2",
                    "master_host":pins[primary]["connect_address"],"master_port":"6380","master_link_status":"up","master_sync_in_progress":"0","master_last_io_seconds_ago":"0"})
            if args[:2]==("INFO","persistence"): return info({"loading":"0","aof_enabled":"1","aof_last_write_status":"ok","aof_last_bgrewrite_status":"ok"})
            if args[:2]==("INFO","memory"): return info({"maxmemory_policy":"noeviction","maxmemory":settings["maxmemory_mib"]*1024**2})
            if args[0]=="ROLE": return ["master",100,[[pins[n]["connect_address"],6380,100] for n in replicas]] if name==primary else ["slave",pins[primary]["connect_address"],6380,"connected",100]
        if args[:2]==("SENTINEL","MYID"): return pins[name]["sentinel_id"]
        if args[:2]==("SENTINEL","GET-MASTER-ADDR-BY-NAME"): return [pins[primary]["connect_address"],"6380"]
        if args[:2]==("SENTINEL","CKQUORUM"): return "OK 3 usable Sentinels. Quorum and failover authorization can be reached"
        if args[:2]==("SENTINEL","MASTER"):
            return flat({"name":self.selected["cache_name"],"flags":"master","ip":pins[primary]["connect_address"],"port":"6380","num-slaves":"2","num-other-sentinels":"2","quorum":"2",
                "down-after-milliseconds":str(settings["down_after_ms"]),"failover-timeout":str(settings["failover_timeout_ms"]),"parallel-syncs":"1","config-epoch":"1"})
        if args[:2]==("SENTINEL","SENTINELS"):
            return [flat({"flags":"sentinel","ip":pins[n]["connect_address"],"port":"26380","runid":pins[n]["sentinel_id"]}) for n in self.names if n!=name]
        if args[:2]==("SENTINEL","REPLICAS"):
            return [flat({"flags":"slave","ip":pins[n]["connect_address"],"port":"6380","runid":pins[n]["server_run_id"],"master-link-status":"ok"}) for n in replicas]
        raise AssertionError("An unexpected command reached the observation fixture")


class Socket:
    def __init__(self,chunks,certificate=b"fixture-leaf"):
        self.chunks=list(chunks); self.sent=[]; self.closed=False; self.certificate=certificate
    def settimeout(self,value): pass
    def recv(self,count):
        if not self.chunks: return b""
        chunk=self.chunks.pop(0); self.chunks[:0]=[chunk[count:]] if len(chunk)>count else []; return chunk[:count]
    def sendall(self,value): self.sent.append(value)
    def getpeercert(self,*,binary_form): return self.certificate
    def close(self): self.closed=True


class RuntimeTests(unittest.TestCase):
    def test_local_mode_checks_all_five_exact_packages_without_network_or_tls_reads(self):
        for runtime in renderer.RUNTIMES:
            selected,files,plan=fixture(runtime)
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); write_bundle(root,files)
                with mock.patch.object(verifier,"Network",side_effect=AssertionError("No live mode")):
                    result=verifier.operate(root,plan)
                self.assertTrue(result["local_inputs_verified"]); self.assertFalse(result["live_observations_verified"])
                for key in verifier.FALSE_FLAGS: self.assertFalse(result[key])
                (root/next(n for n in files if n not in {"valkey-contract.json","render-status.json"})).write_text("changed")
                with self.assertRaises(ValueError): verifier.operate(root,plan)
    def test_invalid_member_aliases_pins_limits_and_private_references_refuse(self):
        selected,files,original=fixture(); contract=json.loads(files["valkey-contract.json"])
        mutations=[lambda p:p["members"]["cache-a"].update(server_run_id="latest"),lambda p:p["members"]["cache-a"].update(connect_address="127.0.0.1"),
            lambda p:p["members"]["cache-b"].update(server_cert_sha256=p["members"]["cache-a"]["server_cert_sha256"]),lambda p:p["members"]["cache-a"].update(addresses=["other.invalid"]),
            lambda p:p["credentials"].update(discovery_env=p["credentials"]["probe_env"]),lambda p:p["limits"].update(replication_wait_ms=5000),
            lambda p:p["limits"].update(operation_seconds=True),lambda p:p["limits"].update(canary_ttl_seconds=60),lambda p:p["trust"].update(client_key_file="relative.key"),lambda p:p.update(password="<fixture-only>")]
        for i,change in enumerate(mutations):
            plan=copy.deepcopy(original); change(plan)
            with self.subTest(case=i),self.assertRaises((ValueError,TypeError)): verifier.validate(plan,contract)
    def test_readonly_live_observations_have_no_data_writes_and_keep_scope_false(self):
        selected,files,plan=fixture(); network=Network(plan,selected)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); write_bundle(root,files); result=verifier.operate(root,plan,live=True,network=network)
        self.assertTrue(result["live_observations_verified"]); self.assertTrue(result["read_only"])
        self.assertFalse(result["canary_replication_verified"])
        self.assertFalse(any(args[0] in {"SET","DEL","EXPIRE","WAIT"} for _,_,args in network.calls))
        for key in verifier.FALSE_FLAGS: self.assertFalse(result[key])
        receipt=result.pop("receipt_sha256"); self.assertEqual(receipt,verifier.fingerprint(result))
        for pin in plan["members"].values():
            self.assertNotIn(pin["server_run_id"],json.dumps(result)); self.assertNotIn(pin["connect_address"],json.dumps(result))
    def test_canary_checks_same_connection_wait_exact_replicas_and_expiry(self):
        selected,files,plan=fixture(); network=Network(plan,selected)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); write_bundle(root,files); result=verifier.operate(root,plan,live=True,canary=True,network=network)
            self.assertTrue(result["canary_replication_verified"]); self.assertFalse(result["read_only"])
            self.assertEqual(len(network.values),1)
            network.wait_count=1
            with self.assertRaisesRegex(ValueError,"acknowledged"): verifier.operate(root,plan,live=True,canary=True,network=network)
            with self.assertRaisesRegex(ValueError,"explicit live"): verifier.operate(root,plan,canary=True)
    def test_foreign_or_changed_invocations_and_sentinel_policy_refuse(self):
        selected,files,plan=fixture()
        def mutations(name,kind,args,value,case):
            if case==0 and args[:2]==("INFO","server") and kind=="server": return value.replace(plan["members"][name]["server_run_id"],"f"*40)
            if case==1 and args[:2]==("SENTINEL","GET-MASTER-ADDR-BY-NAME"): return ["198.51.100.9","6380"]
            if case==2 and args[:2]==("SENTINEL","MASTER"):
                row=verifier.pairs(value); row["quorum"]="1"; return flat(row)
            if case==3 and args[:2]==("SENTINEL","SENTINELS"): return value[:1]
            if case==4 and args[:2]==("INFO","replication"): return value.replace("master_link_status:up","master_link_status:down")
            return value
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); write_bundle(root,files)
            for case in range(5):
                network=Network(plan,selected); network.mutation=lambda n,k,a,v:mutations(n,k,a,v,case)
                with self.subTest(case=case),self.assertRaises(ValueError): verifier.operate(root,plan,live=True,network=network)
    def test_failed_cli_keeps_existing_proof_and_suppresses_private_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); proof=root/"proof.json"; proof.write_text("original"); plan=root/"plan.json"; plan.write_text("{}")
            output=io.StringIO()
            with mock.patch.object(verifier,"operate",side_effect=ValueError("fixture_private_exception")),contextlib.redirect_stderr(output):
                self.assertEqual(verifier.main(["--bundle-dir",str(root),"--runtime-plan",str(plan),"--verify","--proof",str(proof)]),1)
            self.assertEqual(proof.read_text(),"original"); self.assertNotIn("fixture_private_exception",output.getvalue())
    def test_ambiguous_info_and_sentinel_metadata_refuse(self):
        for data in ("role:master\r\nrole:slave", "not-info"):
            with self.assertRaises(ValueError): verifier.info(data)
        for data in (["ip","one","ip","two"],["ip"],["port",6380]):
            with self.assertRaises(ValueError): verifier.pairs(data)
    def connect(self,socket,**kwargs):
        context=mock.Mock(verify_mode=ssl.CERT_REQUIRED,check_hostname=True); context.wrap_socket.return_value=socket
        with mock.patch("valkey_protocol.socket.create_connection",return_value=socket):
            return Connection("192.0.2.51","cache.invalid",6380,context,peer_sha256=kwargs.pop("peer_sha256",hashlib.sha256(b"fixture-leaf").hexdigest()),seconds=2,deadline=time.monotonic()+10,max_bytes=65536,**kwargs)
    def test_fragmented_resp_auth_and_nested_response_are_bounded(self):
        wire=Socket([b"+O",b"K\r\n",b"*3\r\n$3\r\nfoo\r",b"\n:2\r\n$-1\r\n"])
        with self.connect(wire,username="probe",password="<fixture-private>") as client: self.assertEqual(client.command("ROLE"),["foo",2,None])
        self.assertTrue(wire.closed); self.assertEqual(len(wire.sent),2)
    def test_tls_leaf_pin_is_checked_before_authentication(self):
        wire=Socket([])
        with self.assertRaisesRegex(ValueError,"certificate"): self.connect(wire,username="probe",password="<fixture-private>",peer_sha256="0"*64)
        self.assertEqual(wire.sent,[]); self.assertTrue(wire.closed)
    def test_malformed_excessive_and_unsolicited_resp_fail(self):
        for frame in (b"$999999\r\n",b"*513\r\n",b"*1\r\n"*8+b"+x\r\n",b"$2\r\nabxx",b":01\r\n",b"+OK\r\n+EXTRA\r\n",b"+"+b"x"*4100+b"\r\n"):
            with self.subTest(frame=frame[:20]),self.connect(Socket([frame])) as client,self.assertRaises(ValueError): client.command("PING")
        with self.connect(Socket([b"+"+b"x"*65536])) as client,self.assertRaises(ValueError): client.command("PING")
    def test_reply_error_payload_is_never_exposed(self):
        with self.connect(Socket([b"-WRONGPASS fixture-private\r\n"])) as client,self.assertRaises(ReplyError) as raised: client.command("PING")
        self.assertEqual(raised.exception.code,"WRONGPASS"); self.assertNotIn("fixture-private",str(raised.exception))
    def test_expired_operation_and_oversized_command_do_not_send(self):
        wire=Socket([])
        with self.connect(wire) as client:
            client.deadline=time.monotonic()-1
            with self.assertRaises(TimeoutError): client.command("PING")
        self.assertEqual(wire.sent,[])
        wire=Socket([])
        with self.connect(wire) as client,self.assertRaises(ValueError): client.command("SET","x","y"*8193)
        self.assertEqual(wire.sent,[])


if __name__=="__main__": unittest.main()
