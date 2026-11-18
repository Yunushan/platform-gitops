#!/usr/bin/env python3
"""Actual Argo CD server cache traffic across an isolated TLS/Sentinel promotion."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import pwd
import secrets
import shutil
import socket
import ssl
import time
from unittest import mock
from urllib.error import HTTPError
from urllib.request import HTTPSHandler,HTTPRedirectHandler,ProxyHandler,Request,build_opener

import yaml

import argocd_valkey as integration
import render_valkey_ha as renderer
import kubernetes_management as management
from test_argocd_identity_api_fixture import prepare
from test_argocd_identity_runtime_fixture import ARTIFACTS
from test_rook_api_fixture import exercise as api_fixture
import test_valkey_runtime_fixture as cache_fixture
from valkey_protocol import Connection,ReplyError


class Application:
    def __init__(self):
        self.extra_refs={"argocd_data":"ARGOCD_FIXTURE_CACHE_DATA","argocd_sentinel":"ARGOCD_FIXTURE_CACHE_SENTINEL"}
        self.extra_values={v:secrets.token_urlsafe(36) for v in self.extra_refs.values()}; self.client_files=None
    def configure(self,plan): plan["network"]["client_namespaces"]=["argocd"]
    def auth_files(self,plan,runtime,member,refs,*,environment):
        self.contract=json.loads(renderer.render(plan,runtime)["valkey-contract.json"])
        self.plan={"schema_version":1,"cache_contract_sha256":hashlib.sha256(renderer.render(plan,runtime)["valkey-contract.json"].encode()).hexdigest(),
            "application":{"kind":"argocd","version":"3.5.0","namespace":"argocd","private_dir":str(Path(plan["private_dir"]).parent/"argocd-cache"),"credential_secret":"argocd-cache-credentials","tls_secret":"argocd-cache-tls"}}
        files=integration.private_files(self.contract,self.plan,member,{**refs,**self.extra_refs},environment={**environment,**self.extra_values})
        self.client_files={k:v for k,v in files.items() if k.startswith("argocd-")}
        return {k:v for k,v in files.items() if not k.startswith("argocd-")}
    def exercise(self,context):
        test=context["test"]; root=context["root"]; private_values=context["private_values"]; private_values.extend(self.extra_values.values()); stage="source-and-input-preparation"
        artifacts=Path(os.environ["ARGOCD_TEST_ARTIFACTS"])
        for name,digest in ARTIFACTS.items():
            path=artifacts/name
            if path.is_symlink() or not 0<path.stat().st_size<512*1024**2: raise ValueError("Select reviewed bounded Argo CD artifacts")
            with path.open("rb") as stream:
                if hashlib.file_digest(stream,"sha256").hexdigest()!=digest: raise ValueError("The original application artifact changed")
        shutil.copyfile(artifacts/"argocd-linux-amd64",root/"argocd-server"); (root/"argocd-server").chmod(0o755); (root/"argocd").symlink_to("argocd-server")
        client_dir=Path(self.plan["application"]["private_dir"])
        for key,filename in integration.CLIENT_FILES.items():
            path=context["write"](str((client_dir/"credentials"/key).relative_to(root)),self.client_files[filename],owner=True,mode=0o600)
            if path.stat().st_uid!=65534: raise ValueError("The application credential owner differs")
        for name in ("ca.crt","tls.crt","tls.key"):
            path=context["write"](str((client_dir/"tls"/name).relative_to(root)),(context["tls"]/name).read_text(),owner=True,mode=0o600)
        files=integration.render(self.contract,self.plan)
        if json.loads(files["argocd-valkey-arguments.json"])!=integration.arguments(self.contract,self.plan): raise ValueError("Use the production client arguments")
        admin_value=secrets.token_urlsafe(36); private_values.append(admin_value)
        login={"good":admin_value}
        bcrypt=context["run"]([root/"argocd","account","bcrypt","--password",admin_value]).strip()
        if not bcrypt.startswith(("$2a$","$2b$","$2y$")) or len(bcrypt)!=60: raise ValueError("Application credential hashing failed")
        private_values.append(bcrypt)
        reservations=[socket.socket() for _ in range(4)]
        for handle in reservations: handle.bind(("127.0.0.1",0))
        port,metrics,reader_port,reader_metrics=[handle.getsockname()[1] for handle in reservations]; origin="https://127.0.0.1:"+str(port); reader_origin="https://127.0.0.1:"+str(reader_port)
        context["run"](["/usr/bin/openssl","req","-new","-newkey","ec","-pkeyopt","ec_paramgen_curve:P-256","-nodes","-subj","/CN=fixture-argocd-cache","-keyout",root/"argo.key","-out",root/"argo.csr"])
        extension=context["write"]("argo.ext","subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n")
        context["run"](["/usr/bin/openssl","x509","-req","-in",root/"argo.csr","-CA",root/"ca.crt","-CAkey",root/"ca.key","-CAcreateserial","-days","1","-extfile",extension,"-out",root/"argo.crt"])
        for name,source in (("tls.crt",root/"argo.crt"),("tls.key",root/"argo.key")): context["write"]("argocd-tls/"+name,source.read_text(),owner=True,mode=0o600)
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self,*args): return None
        trust=ssl.create_default_context(cafile=str(root/"ca.crt")); opener=build_opener(ProxyHandler({}),HTTPSHandler(context=trust),NoRedirect()); app=reader=None
        def request(path,*,payload=None,expected=(200,),bearer=None,cookie=None,base=origin):
            headers={"Content-Type":"application/json"}
            if bearer is not None: headers["Authorization"]="Bearer "+bearer
            if cookie is not None: headers["Cookie"]="argocd.token="+cookie
            req=Request(base+path,data=None if payload is None else json.dumps(payload).encode(),headers=headers)
            try:
                with opener.open(req,timeout=5) as response: status=response.status; raw=response.read(512*1024+1)
            except HTTPError as error:
                status=error.code; raw=error.read(512*1024+1); error.close()
            if status not in expected or len(raw)>512*1024: raise ValueError("The application cache request did not complete as expected")
            if status==303: return {"redirected":True}
            return json.loads(raw)
        def connect(member,*,diagnostic=False,binary=False,kind="server"):
            certificate=(root/"inputs"/member["name"]/"tls/tls.crt").read_text(); role="fixture_diagnostics" if diagnostic else ("argocd_cache" if kind=="server" else "argocd_discovery")
            value=context["diagnostic"] if diagnostic else self.extra_values[self.extra_refs["argocd_data" if kind=="server" else "argocd_sentinel"]]
            return Connection(member["address"],member["address"],6380 if kind=="server" else 26380,context["tls_context"],peer_sha256=hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate)).hexdigest(),
                seconds=3,deadline=time.monotonic()+10,max_bytes=512*1024,username=role,password=value,bulk_bytes=binary)
        def app_keys(member):
            cursor="0"; found=set()
            with connect(member,diagnostic=True) as client:
                for _ in range(20):
                    result=client.command("SCAN",cursor,"COUNT",200)
                    if not isinstance(result,list) or len(result)!=2 or not isinstance(result[1],list): raise ValueError("Unexpected bounded cache inventory")
                    cursor=result[0]; found.update(result[1])
                    if len(found)>4096: raise ValueError("Excessive application fixture cache inventory")
                    if cursor=="0": return {k for k in found if not k.startswith("platform-probe:")}
            raise ValueError("Application fixture cache inventory did not finish")
        first=context["plan"]["members"][0]
        def raw_value(member,key):
            with connect(member,binary=True) as client: return client.command("GET",key)
        def check(client,api_root,kubectl):
            nonlocal app,reader,stage
            crds=[o for o in yaml.safe_load_all((artifacts/"install.yaml").read_text()) if isinstance(o,dict) and o.get("kind")=="CustomResourceDefinition"]
            if {o["metadata"]["name"] for o in crds}!={"applications.argoproj.io","applicationsets.argoproj.io","appprojects.argoproj.io"}: raise ValueError("The application schema inventory changed")
            stage="application-schema-preparation"
            original_run=management.run_bounded
            def command_diagnostic(args,**kwargs):
                result=original_run(args,**kwargs)
                if result.returncode:
                    text=(result.stdout+result.stderr).casefold()
                    patterns=(("timeout","timed out"),("deadline","deadline"),("context","context"),("forbidden","forbidden"),("certificate","x509"),("connection_refused","connection refused"),("not_found","not found"),("unknown_flag","unknown flag"),("resource_type","resource type"),("too_many_requests","too many requests"),
                        ("thread_limit","new os thread"),("resource_limit","resource temporarily unavailable"),("panic","panic"),("killed","killed"),("eof","eof"),("no_objects","no matching resources"),("no_space","no space left"),("connection_reset","connection reset"),("no_file","no such file"))
                    diagnostic={"condition_wait":"wait" in args,"return_code":result.returncode,"stdout_bytes":len(result.stdout.encode()),"stderr_bytes":len(result.stderr.encode()),"signals":{name:pattern in text for name,pattern in patterns}}
                    if "wait" in args and result.stderr.startswith("error: ") and len(result.stderr)<=512:
                        safe=result.stderr
                        for value in private_values: safe=safe.replace(value,"<private>")
                        safe=re.sub(r"https?://\S+","<endpoint>",safe)
                        safe=re.sub(r"\b(?:[0-9]{1,3}[.]){3}[0-9]{1,3}(?::[0-9]+)?","<address>",safe)
                        safe=re.sub(r"/(?:[^\s/]+/)+[^\s/]*","<path>",safe)
                        safe=re.sub(r"[A-Za-z0-9_-]{32,}","<opaque>",safe)
                        diagnostic["redacted_condition_error"]=safe.strip()
                    print(json.dumps({"argocd_cache_api_command_failed":diagnostic}),flush=True)
                return result
            with mock.patch.object(management,"run_bounded",side_effect=command_diagnostic): prepare(client,kubectl,crds=crds)
            kubectl(["create","-f","-","-o","json"],{"apiVersion":"argoproj.io/v1alpha1","kind":"AppProject","metadata":{"name":"default","namespace":"argocd"},"spec":{"sourceRepos":["https://repo.example.invalid/fixture.git"],"destinations":[{"server":"https://kubernetes.default.svc","namespace":"argocd"}]}})
            kubectl(["patch","configmap/argocd-cm","-n","argocd","--type=merge","-p",json.dumps({"data":{"url":origin}}),"-o","json"])
            for name in ("argocd-cmd-params-cm","argocd-tls-certs-cm","argocd-ssh-known-hosts-cm"):
                kubectl(["create","-f","-","-o","json"],{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":name,"namespace":"argocd"},"data":{}})
            secret_key=secrets.token_urlsafe(48); private_values.append(secret_key)
            values={"server.secretkey":secret_key,"admin.password":bcrypt,"admin.passwordMtime":"2020-01-01T00:00:00Z"}
            kubectl(["create","-f","-","-o","json"],{"apiVersion":"v1","kind":"Secret","metadata":{"name":"argocd-secret","namespace":"argocd","labels":{"app.kubernetes.io/part-of":"argocd"}},"type":"Opaque","data":{k:base64.b64encode(v.encode()).decode() for k,v in values.items()}})
            kubectl(["create","-f","-","-o","json"],{"apiVersion":"v1","kind":"Secret","metadata":{"name":"argocd-server-tls","namespace":"argocd","labels":{"app.kubernetes.io/part-of":"argocd"}},"type":"kubernetes.io/tls","data":{name:base64.b64encode((root/source).read_bytes()).decode() for name,source in (("tls.crt","argo.crt"),("tls.key","argo.key"))}})
            for obj in ({"apiVersion":"v1","kind":"ServiceAccount","metadata":{"name":"fixture-argocd-cache","namespace":"argocd"}},
                {"apiVersion":"rbac.authorization.k8s.io/v1","kind":"Role","metadata":{"name":"fixture-argocd-cache","namespace":"argocd"},"rules":[{"apiGroups":[""],"resources":["configmaps","secrets","pods","services","endpoints"],"verbs":["get","list","watch"]},
                    {"apiGroups":[""],"resources":["events"],"verbs":["get","list","watch","create","patch"]},{"apiGroups":["argoproj.io"],"resources":["applications","appprojects","applicationsets"],"verbs":["get","list","watch"]}]},
                {"apiVersion":"rbac.authorization.k8s.io/v1","kind":"RoleBinding","metadata":{"name":"fixture-argocd-cache","namespace":"argocd"},"roleRef":{"apiGroup":"rbac.authorization.k8s.io","kind":"Role","name":"fixture-argocd-cache"},"subjects":[{"kind":"ServiceAccount","name":"fixture-argocd-cache","namespace":"argocd"}]}): kubectl(["create","-f","-","-o","json"],obj)
            token=client.call(["create","token","fixture-argocd-cache","-n","argocd","--duration=10m"]).strip(); private_values.append(token)
            config=json.loads((api_root/"kubeconfig.json").read_text()); config["users"]=[{"name":"fixture","user":{"token":token}}]; config["contexts"][0]["context"]["namespace"]="argocd"
            kubeconfig=context["write"]("argocd-kubeconfig.json",json.dumps(config),owner=True,mode=0o600)
            for handle in reservations: handle.close()
            home=root/"argocd-home"; home.mkdir(mode=0o700); account=pwd.getpwnam("nobody"); os.chown(home,account.pw_uid,account.pw_gid)
            environment={**cache_fixture.CLEAN,**json.loads(files["argocd-valkey-environment.json"]),"HOME":str(home),"ARGOCD_TLS_DATA_PATH":str(root/"argocd-tls"),"GOMAXPROCS":"2","GRPC_ENABLE_TXT_SERVICE_CONFIG":"false"}
            args=[root/"argocd-server","--kubeconfig="+str(kubeconfig),"--context=storage","--namespace=argocd","--address=127.0.0.1","--port="+str(port),"--metrics-address=127.0.0.1","--metrics-port="+str(metrics),"--loglevel=warn",*json.loads(files["argocd-valkey-arguments.json"])]
            stage="application-start"; app=context["spawn"]("argocd-cache-server",args,environment); original_pid=app.pid
            def ready():
                try: return str(request("/api/version").get("Version","")).startswith("v3.5.0")
                except (OSError,ValueError): return False
            context["wait"](ready,seconds=45)
            stage="initial-application-cache-traffic"
            valid=request("/api/v1/session",payload={"username":"admin","password":login["good"]}); test.assertTrue(valid.get("token")); private_values.append(valid["token"])
            original_session=valid["token"]; request("/api/v1/projects/default",bearer=original_session)
            request("/auth/logout",cookie=original_session,expected=(303,))
            keys=context["wait"](lambda:app_keys(first),seconds=20)
            if not keys: raise ValueError("The actual application did not create cache state")
            # Revocation markers have empty values. Missing keys return None;
            # exact bytes are compared privately, including valid empty bytes.
            captured={key:raw_value(first,key) for key in keys}
            if any(not isinstance(value,bytes) for value in captured.values()): raise ValueError("Application cache state was absent")
            request("/api/v1/projects/default",bearer=original_session,expected=(401,))
            for member in context["plan"]["members"]:
                context["wait"](lambda:all(raw_value(member,k)==v for k,v in captured.items()))
            for denied_args in (("CONFIG","GET","protected-mode"),("ACL","USERS")):
                with connect(first) as data,test.assertRaises(ReplyError) as denied: data.command(*denied_args)
                test.assertEqual(denied.exception.code,"NOPERM")
            with connect(first,kind="sentinel") as data,test.assertRaises(ReplyError) as denied: data.command("SENTINEL","CONFIG","GET","announce-ip")
            test.assertEqual(denied.exception.code,"NOPERM")
            stage="application-cache-primary-loss"; promoted=context["primary_loss"]()
            test.assertTrue(all(raw_value(promoted,k)==v for k,v in captured.items()))
            stage="application-cache-client-recovery"
            def second_session():
                try:
                    return request("/api/v1/session",payload={"username":"admin","password":login["good"]})
                except (OSError,ValueError): return False
            valid=context["wait"](second_session,seconds=30); test.assertTrue(valid.get("token")); private_values.append(valid["token"]); new_session=valid["token"]
            request("/api/v1/projects/default",bearer=new_session)
            stage="fresh-reader-after-promotion"
            reader_home=root/"argocd-reader-home"; reader_home.mkdir(mode=0o700); os.chown(reader_home,account.pw_uid,account.pw_gid)
            reader_args=[("--port="+str(reader_port) if v=="--port="+str(port) else "--metrics-port="+str(reader_metrics) if v=="--metrics-port="+str(metrics) else v) for v in args]
            reader=context["spawn"]("argocd-cache-reader",reader_args,{**environment,"HOME":str(reader_home)})
            def reader_ready():
                try: return str(request("/api/version",base=reader_origin).get("Version","")).startswith("v3.5.0")
                except (OSError,ValueError): return False
            context["wait"](reader_ready,seconds=45)
            request("/api/v1/projects/default",bearer=new_session,base=reader_origin)
            def refused(session_value):
                try: request("/api/v1/projects/default",bearer=session_value,base=reader_origin,expected=(401,)); return True
                except (OSError,ValueError): return False
            context["wait"](lambda:refused(original_session),seconds=30)
            stage="post-promotion-revocation-write"
            request("/auth/logout",cookie=new_session,expected=(303,))
            added=context["wait"](lambda:app_keys(promoted)-keys,seconds=30)
            test.assertTrue(all(isinstance(raw_value(promoted,key),bytes) for key in added))
            context["wait"](lambda:refused(new_session),seconds=30)
            test.assertIsNone(app.poll()); test.assertEqual(app.pid,original_pid)
            return {"argocd_server_revocation_cache_verified":True,"argocd_server_cache_compatibility_verified":False,"argocd_sentinel_tls_client_verified":True,"argocd_cache_state_preserved_after_promotion":True,"argocd_cache_reconnected_without_server_restart":True,
                "argocd_cache_admin_reads_denied":True,"argocd_fresh_server_revocation_read_verified":True,"argocd_post_promotion_revocation_verified":True,"argocd_controller_cache_verified":False,"argocd_repo_server_cache_verified":False,
                "argocd_cache_artifacts_sha256":{**ARTIFACTS,"client_preparation_toolset":integration.observer.sources("argocd_valkey"),"client_arguments":hashlib.sha256(files["argocd-valkey-arguments.json"].encode()).hexdigest()}}
        try: return api_fixture(test,management_action=check)
        except BaseException:
            path=root/"argocd-cache-server.log"; text=""
            if path.exists():
                with path.open("rb") as stream: text=stream.read(2*1024**2).decode(errors="replace").casefold()
            patterns=(("permission_denied","permission denied"),("forbidden","forbidden"),("missing_file","no such file"),("unknown_flag","unknown flag"),("certificate","x509"),("noauth","noauth"),("wrongpass","wrongpass"),("noperm","noperm"),("sentinel","sentinel"),("fatal","fatal"),("panic","panic"),("missing_user","user: unknown"))
            forbidden=re.findall(r'cannot ([a-z]{1,20}) resource "([a-z-]{1,63})"',text)
            print(json.dumps({"argocd_cache_fixture_stage":stage,"private_details_suppressed":True,"application_log_signals":{k:v in text for k,v in patterns},"forbidden_operations":[{"verb":v,"resource":r} for v,r in sorted(set(forbidden))]}),flush=True); raise
        finally:
            for handle in reservations: handle.close()
            if reader is not None: context["stop"](reader)
            if app is not None: context["stop"](app)


def exercise(test): return cache_fixture.exercise(test,application=Application())
