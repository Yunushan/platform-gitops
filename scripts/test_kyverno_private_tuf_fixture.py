#!/usr/bin/env python3
"""Real Kyverno image verification using an isolated threshold-signed HTTPS TUF mirror."""

import base64
import hashlib
import json
import os
import ssl
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

import create_sigstore_tuf as tuf
from bounded_subprocess import run_bounded
from strict_json import loads_strict_json
from test_sigstore_image_runtime_fixture import SignatureChecks, pin, roles

KYVERNO_SHA = "dd492a230e3f7c68235efbf925932cdb34cefbabbd4436d932c3287d9450eea8"
ADDRESS = "192.0.2.101"
ORIGIN = f"https://{ADDRESS}:8443"


class PrivateTUFChecks(SignatureChecks):
    def signature_setup(self, context, **kwargs):
        super().signature_setup(context, **kwargs)
        self.private_tuf(context)

    def private_tuf(self, context, *, address=ADDRESS, directory_name="private-tuf"):
        test = context["test"]; root = context["root"]
        origin = f"https://{address}:8443"
        binary = pin(os.environ["KYVERNO_TUF_TEST_BINARY"], KYVERNO_SHA)
        signer_uid = self.signature_uids["signer"]; reader_uid = self.signature_uids["reader"]
        mirror_uid = next(v for v in roles() if v not in self.signature_uids.values())
        base = root / directory_name; base.mkdir(mode=0o711); base.chmod(0o711)
        authority = base / "authority"; authority.mkdir(mode=0o700); os.chown(authority, signer_uid, signer_uid)
        server = base / "server"; server.mkdir(mode=0o700); os.chown(server, mirror_uid, mirror_uid)
        def write(path, raw, uid, mode=0o400):
            path.write_bytes(raw if isinstance(raw, bytes) else raw.encode()); path.chmod(mode); os.chown(path, uid, uid)
            return path
        def ref(path): return {"path":str(path),"sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
        plan = {"schema_version":1,"keys":{},"expires":{},"targets":{}}
        now = datetime.now(UTC)
        for role, (number, _, _) in tuf.ROLES.items():
            refs=[]
            for i in range(number):
                path=authority/(role+str(i)+".key")
                context["run"](["/usr/bin/openssl","genpkey","-algorithm","ED25519","-out",path])
                path.chmod(0o400);os.chown(path,signer_uid,signer_uid);refs.append(ref(path))
            plan["keys"][role]=refs
            delay={"root":30*86400,"targets":7*86400,"snapshot":2*86400,"timestamp":12*3600}[role]
            plan["expires"][role]=(now+timedelta(seconds=delay)).strftime("%Y-%m-%dT%H:%M:%SZ")
        trusted = write(authority/"trusted_root.json",self.signature_public_inputs["trusted_root"],signer_uid)
        trust = loads_strict_json(trusted.read_bytes())
        log_der=base64.b64decode(trust["tlogs"][0]["publicKey"]["rawBytes"],validate=True)
        write(authority/"log.der",log_der,signer_uid)
        context["run"](["/usr/bin/openssl","pkey","-pubin","-inform","DER","-in",authority/"log.der","-out",authority/"rekor.pub"])
        # These distinct supplemental public keys/certificates satisfy strict
        # infrastructure initialization. This key-based case does not establish
        # a running Fulcio or certificate-transparency service.
        context["run"](["/usr/bin/openssl","genpkey","-algorithm","EC","-pkeyopt","ec_paramgen_curve:P-256","-out",authority/"ct.key"])
        context["run"](["/usr/bin/openssl","pkey","-in",authority/"ct.key","-pubout","-out",authority/"ctfe.pub"])
        context["run"](["/usr/bin/openssl","req","-new","-x509","-newkey","ec","-pkeyopt","ec_paramgen_curve:P-256","-nodes","-days","1",
            "-subj","/CN=unused-private-keyless-test-ca","-addext","basicConstraints=critical,CA:TRUE","-addext","keyUsage=critical,keyCertSign,cRLSign",
            "-keyout",authority/"fulcio.key","-out",authority/"fulcio.crt.pem"])
        for path in authority.iterdir():path.chmod(0o400);os.chown(path,signer_uid,signer_uid)
        plan["targets"]={name:ref(authority/name) for name in tuf.TARGETS}
        plan_file=write(authority/"plan.json",tuf.canonical(plan),signer_uid)
        code="import json,sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from create_sigstore_tuf import generate;print(json.dumps(generate(json.loads(Path(sys.argv[2]).read_bytes()))))"
        self.signature_stage="private-tuf-threshold-signing"
        value=self.signature_command(signer_uid,authority,["/usr/bin/python3","-I","-B","-c",code,Path(__file__).resolve().parent,plan_file])
        files=loads_strict_json(value.stdout)
        status=loads_strict_json(files.pop("tuf-bootstrap-status.json"))
        test.assertEqual(status["role_keyids"]["root"]["threshold"],2)
        test.assertEqual(status["role_keyids"]["targets"]["threshold"],2)
        for name,raw in files.items():write(server/name,raw,mirror_uid)
        context["run"](["/usr/sbin/ip","address","add",address+"/32","dev","lo"])
        ext=base/"tls.ext";ext.write_text(f"subjectAltName=IP:{address}\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n")
        context["run"](["/usr/bin/openssl","req","-new","-newkey","ec","-pkeyopt","ec_paramgen_curve:P-256","-nodes","-subj","/CN=private-tuf-fixture","-keyout",server/"tls.key","-out",server/"tls.csr"])
        context["run"](["/usr/bin/openssl","x509","-req","-in",server/"tls.csr","-CA",root/"ca.crt","-CAkey",root/"ca.key","-CAcreateserial","-days","1","-extfile",ext,"-out",server/"tls.crt"])
        for path in (server/"tls.key",server/"tls.crt",server/"tls.csr"):path.chmod(0o400);os.chown(path,mirror_uid,mirror_uid)
        base_mirror={"schema_version":1,"address":address,"port":8443,"certificate":ref(server/"tls.crt"),"private_key":ref(server/"tls.key"),"files":{name:ref(server/name) for name in files}}
        reader=self.signature_reader
        credentials=loads_strict_json((reader/"credential").read_bytes())
        image=self.signature_plan["image"]
        def policy(mirror_url,root_metadata):
            return {"apiVersion":"policies.kyverno.io/v1","kind":"ImageValidatingPolicy","metadata":{"name":"verify-private-release"},"spec":{
                "evaluation":{"admission":{"enabled":True},"background":{"enabled":False}},"validationActions":["Deny"],"failurePolicy":"Fail",
                "matchConstraints":{"resourceRules":[{"apiGroups":[""],"apiVersions":["v1"],"operations":["CREATE","UPDATE"],"resources":["pods"]}]},
                "images":[{"name":"selected","expression":"(object.spec.containers + object.spec.?initContainers.orValue([]) + object.spec.?ephemeralContainers.orValue([])).map(c, c.image)"}],
                "matchImageReferences":[{"glob":"*"}],"attestors":[{"name":"release","cosign":{"key":{"data":self.signature_public_inputs["public_key"]},
                    "annotations":self.signature_plan["annotations"],"tuf":{"mirror":mirror_url,"root":{"data":base64.b64encode(root_metadata.encode()).decode()}},
                    "trustedRoot":{"value":self.signature_public_inputs["trusted_root"]}}}],
                "validationConfigurations":{"mutateDigest":False,"required":True,"verifyDigest":True},
                "validations":[{"expression":"images.selected.map(image, verifyImageSignatures(image, [attestors.release])).all(result, result > 0)","message":"Release signature and signed bindings must verify."}]}}
        pod={"apiVersion":"v1","kind":"Pod","metadata":{"name":"private-release","namespace":"synthetic-application"},"spec":{"containers":[{"name":"app","image":image}]}}
        self.tuf_attempts=[];self.tuf_processes=[]
        def evaluate(case, selected, expected):
            self.signature_stage="kyverno-private-tuf-"+case
            home=reader/("kyverno-"+case);home.mkdir(mode=0o700);os.chown(home,reader_uid,reader_uid)
            (home/"empty-certs").mkdir();os.chown(home/"empty-certs",reader_uid,reader_uid)
            config={"auths":{credentials["registry"]:{"registrytoken":credentials["token"]}}}
            write(home/"config.json",json.dumps(config),reader_uid)
            pp=write(home/"policy.yaml",json.dumps(selected),reader_uid);rr=write(home/"pod.json",json.dumps(pod),reader_uid)
            args=["/usr/bin/setpriv","--reuid="+str(reader_uid),"--regid="+str(reader_uid),"--clear-groups","--no-new-privs","--",str(binary),"apply",str(pp),"--resource",str(rr),"--registry","--detailed-results","--remove-color","--warn-no-pass","--warn-exit-code=2"]
            env={"PATH":"/usr/bin:/bin","HOME":str(home),"TMPDIR":str(home),"TUF_ROOT":str(home/"cache"),"DOCKER_CONFIG":str(home),"SSL_CERT_FILE":str(root/"ca.crt"),"SSL_CERT_DIR":str(home/"empty-certs"),"LANG":"C","LC_ALL":"C","GOMAXPROCS":"2"}
            result=run_bounded(args,env=env,cwd=home,text=True,timeout=90,output_max_bytes=1024*1024,check=False)
            logs=(result.stdout+result.stderr).encode()
            test.assertFalse(any(value in logs for value in self.signature_private_values))
            attempt={"case":case,"returncode":result.returncode,"expected_pass":expected,"cache_was_fresh":True,"policy_sha256":hashlib.sha256(tuf.canonical(selected)).hexdigest(),"one_policy_loaded":"Applying 1 policy rule(s)" in result.stdout}
            self.tuf_attempts.append(attempt)
            if (result.returncode==0)!=expected or not attempt["one_policy_loaded"] or expected and "pass: 1" not in result.stdout:
                target=Path(os.environ["KYVERNO_TUF_TEST_REPORT"]).parent/"kyverno-last-private-output.log";target.write_bytes(logs);target.chmod(0o600)
                print(json.dumps({"kyverno_tuf_case":case,"returncode":result.returncode,"expected_pass":expected}),flush=True)
                raise AssertionError("Actual Kyverno private TUF verification returned an unexpected result")
        def mirror_case(name, selected_mirror, chosen_policy, expected):
            file=write(server/("plan-"+name+".json"),tuf.canonical(selected_mirror),mirror_uid)
            log=base/(name+".log")
            argv=["/usr/bin/setpriv","--reuid="+str(mirror_uid),"--regid="+str(mirror_uid),"--clear-groups","--no-new-privs","--","/usr/bin/python3","-I","-B","-c",
                "import sys;sys.path.insert(0,sys.argv[1]);from sigstore_tuf_mirror import main;raise SystemExit(main(sys.argv[2:]))",str(Path(__file__).resolve().parent),"--plan",str(file),"--serve"]
            with log.open("xb") as output:child=subprocess.Popen(argv,cwd=server,env={"PATH":"/usr/bin:/bin","HOME":str(server)},stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
            original={}
            def finish():
                if child.poll() is None:
                    child.terminate()
                    try:child.wait(timeout=10)
                    except subprocess.TimeoutExpired:child.kill();child.wait(timeout=10)
                test.assertFalse(Path('/proc',str(child.pid)).exists())
                if original:original['original_process_removed']=True;self.tuf_processes.append(original)
            lifetime=getattr(self,'private_tuf_lifetime',None) if expected else None
            if lifetime is not None:lifetime.callback(finish)
            try:
                opener=build_opener(ProxyHandler({}),HTTPSHandler(context=ssl.create_default_context(cafile=str(root/"ca.crt"))))
                def ready():
                    if child.poll() is not None:raise ValueError("Original TLS mirror exited")
                    try:
                        with opener.open(Request(origin+"/root.json"),timeout=2) as response:return response.read()==(server/"root.json").read_bytes()
                    except OSError:return False
                context["wait"](ready,seconds=20)
                proc=Path('/proc')/str(child.pid);info=dict(line.split(':',1) for line in (proc/'status').read_text().splitlines() if ':' in line)
                test.assertEqual([int(v) for v in info['Uid'].split()],[mirror_uid]*4);test.assertEqual(info['NoNewPrivs'].strip(),'1');test.assertEqual(int(info['CapEff'].strip(),16),0)
                original={"pid":child.pid,"start_ticks":int((proc/'stat').read_text().rsplit(') ',1)[1].split()[19]),"uid":mirror_uid,
                    "boot_id":Path('/proc/sys/kernel/random/boot_id').read_text().strip(),"cgroup":(proc/'cgroup').read_text().strip(),"case":name}
                evaluate(name,chosen_policy,expected)
                if expected:
                    changed=loads_strict_json(tuf.canonical(chosen_policy));first=next(iter(changed['spec']['attestors'][0]['cosign']['annotations']))
                    changed['spec']['attestors'][0]['cosign']['annotations'][first]='0'*64
                    evaluate('wrong-binding',changed,False)
                    if hasattr(self, "private_tuf_live"):
                        self.private_tuf_active_process=original
                        self.private_tuf_live(context, files, status, selected)
            finally:
                if lifetime is None:finish()
        selected=policy(origin,files['root.json'])
        bad_target=write(server/'tampered-trust.json',files['trusted_root.json']+' ',mirror_uid)
        bad_mirror={**base_mirror,'files':{**base_mirror['files'],'trusted_root.json':ref(bad_target)}}
        mirror_case('tampered-target',bad_mirror,selected,False)
        bad_root=loads_strict_json(files['root.json']);bad_root['signatures']=bad_root['signatures'][:1]
        mirror_case('insufficient-root-threshold',base_mirror,policy(origin,json.dumps(bad_root)),False)
        mirror_case('valid',base_mirror,selected,True)
        check="import os,sys\ntry:os.open(sys.argv[1],os.O_RDONLY)\nexcept PermissionError:sys.exit(0)\nraise SystemExit(1)"
        self.signature_command(mirror_uid,server,['/usr/bin/python3','-I','-B','-c',check,plan['keys']['root'][0]['path']])
        self.private_tuf_proof={"attempts":self.tuf_attempts,"mirror_processes":self.tuf_processes,"mirror_uid":mirror_uid,"signer_uid":signer_uid,
            "bootstrap_status":status,"public_files":files,"policy":selected,"kyverno_binary_sha256":KYVERNO_SHA,
            "private_tuf_client_verified":True,"image_signature_verified":True,"tls_verified":True,"admission_verified":False,"ha_verified":False,"full_stack_verified":False}

    def exercise(self,context):
        result=super().exercise(context)
        target=Path(os.environ['KYVERNO_TUF_TEST_REPORT']);descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'wb') as stream:stream.write(tuf.canonical(self.private_tuf_proof)+b'\n')
        result.update(valkey_gateway_harbor_kyverno_private_tuf_signature_verified=True,valkey_gateway_harbor_kyverno_tuf_tampered_target_refused=True,
            valkey_gateway_harbor_kyverno_tuf_insufficient_root_threshold_refused=True,valkey_gateway_harbor_kyverno_wrong_signed_binding_refused=True,
            valkey_gateway_harbor_kyverno_private_tuf_proof_sha256={'proof':hashlib.sha256(target.read_bytes()).hexdigest()})
        return result


def exercise(test):
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    from test_harbor_core_replica_fixture import CoreReplicaChecks
    from test_harbor_database_promotion_fixture import DatabasePromotionChecks
    from test_harbor_jobservice_replica_fixture import JobReplicaChecks
    from test_harbor_jwt_runtime_fixture import JWTChecks
    from test_harbor_pool_health_fixture import PoolHealthChecks
    from test_harbor_registry_runtime_fixture import RegistryChecks
    from test_harbor_service_runtime_fixture import ServiceChecks
    class Application(PrivateTUFChecks,PoolHealthChecks,JobReplicaChecks,CoreReplicaChecks,JWTChecks,DatabasePromotionChecks,ServiceChecks,RegistryChecks,gateway.Application):pass
    return cache.exercise(test,application=Application())
