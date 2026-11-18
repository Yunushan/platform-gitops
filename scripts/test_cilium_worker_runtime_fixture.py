#!/usr/bin/env python3
"""Actual Cilium CNI, policy denial and TLS Gateway traffic on five KVM guests."""

import base64
import hashlib
from http.client import HTTPException, HTTPSConnection
import json
import os
from pathlib import Path
import shlex
import socket
import ssl
from uuid import uuid4

import deploy_cilium_gateway as routes
import deploy_cilium_installation as deploy
import render_cilium_gateway as gateway
import render_cilium_installation as renderer
from test_k3s_vm_fixture import PROBE_IMAGE, PROBE_INDEX, PROBE_MANIFEST, fingerprint
from test_tekton_worker_runtime_fixture import verify_archive


CHART_SHA = "b7252afca3413a33e2b987c954e52441e96979e411e1495ecc6aefe8cd258863"
GATEWAY_SHA = "73b91b77f6be023a8c92c969fc664e5bd3b1a28aea59eac9ebc904607354dad2"
TLSROUTE_SHA = "1dee8d7e1a56dadf0e88466bfef8a8afc5664a94e95d6b2fb812f03d0b198748"
HELM_SHA = "c16af303055cfe1c1e64652b547cfdbed924e89fd2a20d8632d797978c14def3"
IMAGES = {
    "agent": "quay.io/cilium/cilium@sha256:e86fb55fa703ada191fbd4489513daa19674952c8c3c4985dc3c77ce51ac4f27",
    "envoy": "quay.io/cilium/cilium-envoy@sha256:dfdee624883c419b253ecdecf9fb6a001c05900dda19914e241db053c94bd987",
    "operator": "quay.io/cilium/operator-generic@sha256:b3d1c26d4bc58e307282bad9639b4d598e73f91be1430028f36bc5fdce19a85c",
}
HOST = "gateway.example.invalid"
BODY = b"original Cilium backend\n"
PASSTHROUGH_HOST = "passthrough.example.invalid"
PASSTHROUGH_BODY = "Backend-owned TLS\n"


class DirectTLS(HTTPSConnection):
    def connect(self):
        self.sock = self._context.wrap_socket(socket.create_connection((self.address, self.port), self.timeout), server_hostname=self.host)


def stage(name): print(json.dumps({"cilium_vm_stage": name}), flush=True)


def package(root, name, files):
    output = root / name; output.mkdir(mode=0o700)
    for name, text in files.items():
        path = output / name; path.write_text(text, encoding="utf-8", newline="\n"); path.chmod(0o600)
    return output


def bootstrap(test, context, *, tls_passthrough=False):
    client, root = context["client"], context["root"]
    assets = Path(os.environ["CILIUM_VM_TEST_ARTIFACTS"]).resolve(strict=True)
    helm = Path(os.environ["CILIUM_VM_TEST_HELM"]).resolve(strict=True)
    fingerprint(assets / "cilium-1.19.10.tgz", CHART_SHA)
    definitions = assets / "gateway-api-v1.4.1-standard-install.yaml"
    fingerprint(definitions, GATEWAY_SHA); fingerprint(helm, HELM_SHA)
    crds = definitions.read_bytes()
    if tls_passthrough:
        tlsroute = assets / "gateway-api-v1.4.1-tlsroute.yaml"
        fingerprint(tlsroute, TLSROUTE_SHA)
        crds = crds.rstrip() + b"\n---\n" + tlsroute.read_bytes()
    verified = {image: verify_archive(assets / "images" / (image.rsplit(":", 1)[1] + ".tar"), image) for image in IMAGES.values()}
    try:
        stage("offline-image-import")
        nodes = [*context["control_nodes"], *context["nodes"][1:]]
        test.assertEqual(len(context["control_nodes"]), 3)
        test.assertIn(len(nodes), (5, 6))
        test.assertEqual(len({node["name"] for node in nodes}), len(nodes))
        for node in nodes:
            program = ('import json,tomllib; from pathlib import Path; '
                'd=tomllib.loads(Path("/var/lib/fixture-k3s/agent/etc/containerd/config.toml").read_text()); '
                'print(json.dumps(d["plugins"]["io.containerd.cri.v1.runtime"]["cni"]))')
            observed = json.loads(context["ssh"](node, "sudo python3 -c " + shlex.quote(program)).stdout)
            test.assertEqual(observed, {"bin_dirs": ["/var/lib/fixture-k3s/data/cni"], "conf_dir": "/var/lib/fixture-k3s/agent/etc/cni/net.d"})
            for image in IMAGES.values():
                digest = image.rsplit(":", 1)[1]; path = assets / "images" / (digest + ".tar")
                print(json.dumps({"cilium_image_import": {"guest": node["name"], "image_digest": digest}}), flush=True)
                destination = "/tmp/fixture-image-" + digest + ".tar"
                context["upload"](node, path, destination)
                context["ssh"](node, "sudo /mnt/fixtures/k3s ctr --address /run/k3s/containerd/containerd.sock --namespace k8s.io images import --digests " + destination, seconds=120)
                context["ssh"](node, "rm -- " + destination)
            test.assertEqual(context["ssh"](node, "test ! -e /var/lib/fixture-k3s/agent/etc/cni/net.d/10-flannel.conflist", check=False).returncode, 0)
        plan = {"schema_version": 1, "name": "fixture-cilium", "namespace": "kube-system", "management_target": context["target"],
            "cilium_version": "1.19.10", "gateway_api_version": "1.4.1", "kubernetes_version": "1.35.0", "chart_sha256": CHART_SHA,
            "helm_sha256": HELM_SHA, "gateway_crds_sha256": hashlib.sha256(crds).hexdigest(), "images": IMAGES,
            "bootstrap_api": {"host": "192.0.2.1", "port": 6443},
            "cni_paths": {"bin": "/var/lib/fixture-k3s/data/cni", "config": "/var/lib/fixture-k3s/agent/etc/cni/net.d"},
            "pod_cidr": "198.18.0.0/16", "node_cidr_mask": 24, "mtu": 1500,
            "serving_selector": {"platform.gitops/edge": "true"}, "failure_domain_key": "topology.kubernetes.io/zone"}
        if tls_passthrough:
            plan["tls_passthrough"] = True
        stage("reviewed-real-chart-package")
        files = renderer.render(plan, (assets / "cilium-1.19.10.tgz").read_bytes(), crds, "k3s", helm)
        bundle = package(root, "cilium-package", files)
        digest = json.loads(files["render-status.json"])["contract_sha256"]
        evidence = Path(__file__).resolve().parents[1] / "private/validation/cilium/v1.19.10" / ("runtime-" + uuid4().hex)
        stage("owned-cni-preview-and-installation")
        preview = deploy.operate(bundle, digest, check_server=True, output=evidence / "check")
        test.assertFalse(preview["bootstrap_ready"])
        test.assertEqual(preview["namespace_bootstrap_required"], ["fixture-cilium-secrets"])
        test.assertIsNone(client.get("namespace/fixture-cilium-secrets", optional=True))
        installed = deploy.operate(bundle, digest, apply=True, output=evidence / "install")
        test.assertTrue(installed["bootstrap_ready"]); test.assertFalse(installed["full_stack_verified"])
        checked = deploy.operate(bundle, digest, verify=True, output=evidence / "verify")
        test.assertTrue(checked["bootstrap_ready"])
        context["cilium_runtime"] = {"plan": plan, "bundle": bundle, "digest": digest, "evidence": evidence}
        stage("cni-bootstrap-verified")
        return {"real_cilium_cni_bootstrap_verified": True, "cilium_agent_node_count": len(nodes), "cilium_version": "1.19.10",
            "cilium_chart_sha256": CHART_SHA, "gateway_crds_sha256": plan["gateway_crds_sha256"], "cilium_oci_archives_sha256": verified,
            "cilium_namespace_bootstrap_preview_read_only_verified": True, "cilium_guest_containerd_cni_paths_observed": True,
            "publisher_image_signatures_verified": False}
    except Exception:
        diagnostics(client)
        raise


def diagnostics(client):
    try: pods = json.loads(client.call(["get", "pods", "-n", "kube-system", "-o", "json"]))["items"]
    except ValueError: return
    rows = []
    for pod in pods:
        if not pod["metadata"]["name"].startswith("cilium"): continue
        states = pod.get("status", {}).get("initContainerStatuses", []) + pod.get("status", {}).get("containerStatuses", [])
        row = {"pod": pod["metadata"]["name"], "phase": pod.get("status", {}).get("phase"),
            "waiting": [s["state"]["waiting"].get("reason") for s in states if "waiting" in s.get("state", {})],
            "terminated": [s["state"]["terminated"].get("reason") for s in states if "terminated" in s.get("state", {})]}
        try:
            text = client.call(["logs", "pod/" + pod["metadata"]["name"], "-n", "kube-system", "--all-containers=true", "--tail=30"]).casefold()
            row["log_signals"] = {key: key in text for key in ("permission denied", "bpf", "failed", "x509", "ipam", "out of memory", "connection refused")}
        except ValueError: pass
        rows.append(row)
    print(json.dumps({"actual_cilium_pod_signals": rows}), flush=True)


def certificates(context, *, hostname=HOST, directory_name="gateway-pki"):
    root, run = context["root"], context["run"]
    directory = root / directory_name; directory.mkdir(mode=0o700)
    run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=fixture-gateway-ca",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        "-keyout", directory / "ca.key", "-out", directory / "ca.crt"])
    run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=" + hostname, "-keyout", directory / "server.key", "-out", directory / "server.csr"])
    (directory / "extensions").write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\nsubjectAltName=DNS:" + hostname + "\nextendedKeyUsage=serverAuth\n", encoding="utf-8")
    run(["openssl", "x509", "-req", "-in", directory / "server.csr", "-CA", directory / "ca.crt", "-CAkey", directory / "ca.key", "-CAcreateserial",
        "-days", "1", "-extfile", directory / "extensions", "-out", directory / "server.crt"])
    for path in directory.iterdir(): path.chmod(0o600)
    return directory


def passthrough_backend(create, context):
    namespace, name = "cilium-probe", "tls-backend"
    pki = certificates(context, hostname=PASSTHROUGH_HOST, directory_name="backend-pki")
    cert = {f"tls.{suffix}": base64.b64encode((pki / f"server.{suffix}").read_bytes()).decode() for suffix in ("crt", "key")}
    create({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": namespace}, "type": "kubernetes.io/tls", "data": cert})
    http = {"@type": "type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager",
            "stat_prefix": "fixture", "route_config": {"name": "fixture", "virtual_hosts": [{"name": "fixture",
                "domains": ["*"], "routes": [{"match": {"prefix": "/"}, "direct_response": {
                    "status": 200, "body": {"inline_string": PASSTHROUGH_BODY}}}]}]},
            "http_filters": [{"name": "envoy.filters.http.router", "typed_config": {
                "@type": "type.googleapis.com/envoy.extensions.filters.http.router.v3.Router"}}]}
    transport = {"name": "envoy.transport_sockets.tls", "typed_config": {
        "@type": "type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.DownstreamTlsContext",
        "common_tls_context": {"tls_certificates": [{"certificate_chain": {"filename": "/cert/tls.crt"},
                                                    "private_key": {"filename": "/cert/tls.key"}}]}}}
    config = {"static_resources": {"listeners": [{"name": "backend-https",
        "address": {"socket_address": {"address": "0.0.0.0", "port_value": 8443}},
        "filter_chains": [{"transport_socket": transport, "filters": [
            {"name": "envoy.filters.network.http_connection_manager", "typed_config": http}]}]}]}}
    create({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": namespace},
            "data": {"envoy.json": json.dumps(config)}})
    selector = {"role": name}
    create({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": namespace, "labels": selector},
            "spec": {"automountServiceAccountToken": False, "nodeSelector": {"kubernetes.io/hostname": context["nodes"][1]["name"]},
                "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
                "volumes": [{"name": "cert", "secret": {"secretName": name}}, {"name": "config", "configMap": {"name": name}}],
                "containers": [{"name": "https", "image": IMAGES["envoy"], "imagePullPolicy": "Never",
                    "command": ["/usr/bin/cilium-envoy", "--disable-hot-restart", "-c", "/config/envoy.json"],
                    "ports": [{"name": "https", "containerPort": 8443}],
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                    "readinessProbe": {"tcpSocket": {"port": "https"}, "periodSeconds": 1},
                    "volumeMounts": [{"name": "cert", "mountPath": "/cert", "readOnly": True},
                                     {"name": "config", "mountPath": "/config", "readOnly": True}]}]}})
    create({"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": namespace},
            "spec": {"selector": selector, "ports": [{"name": "https", "port": 443, "targetPort": "https"}]}})
    context["wait"](lambda: any(c.get("type") == "Ready" and c.get("status") == "True" for c in
                                context["client"].get("pod/" + name, namespace)["status"]["conditions"]), seconds=60)
    return pki, {"name": name, "namespace": namespace, "hostname": PASSTHROUGH_HOST, "protocol": "tls", "service": name,
                 "port": 443, "target_port": 8443, "pod_selector": selector}


def verify_passthrough(test, context, pki):
    tls = ssl.create_default_context(cafile=str(pki / "ca.crt"))
    expected_der = ssl.PEM_cert_to_DER_cert((pki / "server.crt").read_text(encoding="ascii"))
    for node in context["nodes"][1:]:
        connection = DirectTLS(PASSTHROUGH_HOST, 9443, context=tls, timeout=3)
        connection.address = node["address"]
        try:
            connection.connect()
            test.assertEqual(connection.sock.getpeercert(binary_form=True), expected_der)
            connection.request("GET", "/")
            response = connection.getresponse()
            test.assertEqual((response.status, response.read(1024)), (200, PASSTHROUGH_BODY.encode()))
        finally:
            connection.close()
    stage("backend-certificate-and-https-response-verified-on-both-gateways")


def verify_cluster_dns(test, context, service, request, success):
    client, wait = context["client"], context["wait"]
    namespace = service["metadata"]["namespace"]
    name = service["metadata"]["name"]
    application_ip = service["spec"]["clusterIP"]
    fqdn = f"{name}.{namespace}.svc.cluster.local"
    dns_service = client.get("service/kube-dns", "kube-system")
    dns_ip = dns_service["spec"]["clusterIP"]
    test.assertNotEqual(application_ip, dns_ip)
    clients = [f"allowed-{index}" for index in range(2)]

    def endpoints():
        slices = json.loads(client.call([
            "get", "endpointslices", "-n", "kube-system", "-o", "json",
            "-l", "kubernetes.io/service-name=kube-dns",
        ]))
        return {endpoint["targetRef"]["uid"] for item in slices["items"]
                for endpoint in item["endpoints"] if endpoint["conditions"].get("ready") is True
                and endpoint.get("targetRef", {}).get("kind") == "Pod"}

    def resolves():
        for pod in clients:
            # Query through the pod's default resolver. Match the answer line
            # exactly so the resolver address in the header cannot count.
            output = client.call([
                "exec", "pod/" + pod, "-n", namespace, "--", "sh", "-c",
                'timeout 5 nslookup -type=A "$1"; printf "\\ndns-exit:%s\\n" "$?"',
                "probe", fqdn + ".",
            ], seconds=10)
            lines = [line.split() for line in output.splitlines()]
            if ["Server:", dns_ip] not in lines or ["Address:", application_ip] not in lines or not output.endswith("\ndns-exit:0\n"):
                return False
            if request(pod, "http://" + fqdn + "/") != success:
                return False
        return True

    stage("worker-service-dns-after-quorum-recovery")
    wait(resolves, seconds=90)
    original_endpoints = endpoints()
    test.assertTrue(original_endpoints)
    stage("replace-disposable-coredns-and-verify-service-dns")
    client.call(["rollout", "restart", "deployment/coredns", "-n", "kube-system"])
    client.call(["rollout", "status", "deployment/coredns", "-n", "kube-system", "--timeout=120s"], seconds=130)

    def replacement_works():
        current_endpoints = endpoints()
        return bool(current_endpoints and current_endpoints.isdisjoint(original_endpoints) and resolves())

    wait(replacement_works, seconds=90)
    current_dns_service = client.get("service/kube-dns", "kube-system")
    test.assertEqual(current_dns_service["metadata"]["uid"], dns_service["metadata"]["uid"])
    test.assertEqual(current_dns_service["spec"]["clusterIP"], dns_ip)
    return {"cilium_service_dns_worker_count": len(clients), "cilium_service_dns_after_quorum_verified": True,
            "cilium_coredns_replacement_and_service_dns_verified": True}


def verify_datapath(test, context, *, check_dns=False, tls_passthrough=False):
    client, root, wait = context["client"], context["root"], context["wait"]
    chosen = context["cilium_runtime"]; plan = chosen["plan"]
    def create(obj): return json.loads(client.call(["create", "-f", "-", "-o", "json"], payload=json.dumps(obj).encode()))
    stage("cross-worker-policy-traffic")
    create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "cilium-probe", "labels": {"pod-security.kubernetes.io/enforce": "restricted", gateway.ACCESS: "fixture-edge"}}})
    create({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "content", "namespace": "cilium-probe"}, "data": {"index.html": BODY.decode()}})
    originals = {}
    for index, node in enumerate(context["nodes"][1:]):
        for role in ("server", "allowed", "denied"):
            server = role == "server"; name = role + "-" + str(index)
            spec = {"automountServiceAccountToken": False, "nodeSelector": {"kubernetes.io/hostname": node["name"]},
                "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{"name": role, "image": PROBE_IMAGE, "imagePullPolicy": "IfNotPresent", "command": ["httpd", "-f", "-p", "8080", "-h", "/www"] if server else ["sleep", "900"],
                    "resources": {"requests": {"cpu": "25m", "memory": "32Mi"}, "limits": {"cpu": "250m", "memory": "64Mi"}},
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}}]}
            if server:
                spec["volumes"] = [{"name": "content", "configMap": {"name": "content"}}]
                spec["containers"][0].update(volumeMounts=[{"name": "content", "mountPath": "/www", "readOnly": True}], readinessProbe={"httpGet": {"path": "/", "port": 8080}, "periodSeconds": 1})
            obj = create({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": "cilium-probe", "labels": {"role": role}}, "spec": spec})
            originals[name] = {"uid": obj["metadata"]["uid"], "node": node["name"]}
    def ready():
        for name, original in originals.items():
            obj = client.get("pod/" + name, "cilium-probe"); test.assertEqual(obj["metadata"]["uid"], original["uid"])
            if not any(c.get("type") == "Ready" and c.get("status") == "True" for c in obj.get("status", {}).get("conditions", [])): return False
            test.assertEqual(obj["spec"]["nodeName"], original["node"]); test.assertFalse(obj["spec"].get("hostNetwork", False))
            image = obj["status"]["containerStatuses"][0]["imageID"]
            test.assertTrue(any(digest in image for digest in (PROBE_INDEX, PROBE_MANIFEST)))
            original["ip"] = obj["status"]["podIP"]
        return True
    wait(ready, seconds=120)
    service = create({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "backend", "namespace": "cilium-probe"},
        "spec": {"selector": {"role": "server"}, "ports": [{"port": 80, "targetPort": 8080}]}})
    urls = ["http://" + service["spec"]["clusterIP"] + "/", *["http://" + originals["server-" + str(index)]["ip"] + ":8080/" for index in range(2)]]
    command = 'wget -T 2 -qO- "$1"; code=$?; printf "\\nprobe-exit:%s\\n" "$code"'
    def request(pod, url): return client.call(["exec", "pod/" + pod, "-n", "cilium-probe", "--", "sh", "-c", command, "probe", url], seconds=10)
    success = BODY.decode() + "\nprobe-exit:0\n"
    baseline = []
    def accessible():
        baseline.clear()
        for role in ("allowed", "denied"):
            for index in range(2):
                for target, url in enumerate(urls):
                    text = request(role + "-" + str(index), url)
                    baseline.append({"client": role + "-" + str(index), "target": "cluster-ip" if target == 0 else "pod-ip-" + str(target - 1),
                        "matched": text == success, "response": text[:160]})
        return all(item["matched"] for item in baseline)
    try: wait(accessible, seconds=60)
    except Exception:
        # Only this fixture's fixed body and completion marker can occur here;
        # no user workload, private endpoint, credential or Secret is queried.
        print(json.dumps({"cilium_initial_http_matrix": baseline}), flush=True)
        diagnostics(client)
        raise
    create({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "reviewed-client-only", "namespace": "cilium-probe"},
        "spec": {"podSelector": {"matchLabels": {"role": "server"}}, "policyTypes": ["Ingress"], "ingress": [{"from": [{"podSelector": {"matchLabels": {"role": "allowed"}}}], "ports": [{"protocol": "TCP", "port": 8080}]}]}})
    def enforced():
        if not all(request("allowed-" + str(index), url) == success for index in range(2) for url in urls): return False
        for index in range(2):
            for url in urls:
                text = request("denied-" + str(index), url)
                if text != "\nprobe-exit:1\n": return False
        return ready() and all(request("allowed-" + str(index), urls[0]) == success for index in range(2))
    wait(enforced, seconds=90)
    dns_evidence = verify_cluster_dns(test, context, service, request, success) if check_dns else {}
    stage("verified-gateway-tls-traffic")
    create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "cilium-gateway"}})
    pki = certificates(context)
    create({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "gateway-tls", "namespace": "cilium-gateway"}, "type": "kubernetes.io/tls",
        "data": {"tls.crt": base64.b64encode((pki / "server.crt").read_bytes()).decode(), "tls.key": base64.b64encode((pki / "server.key").read_bytes()).decode()}})
    route_plan = {"schema_version": 1, "name": "fixture-edge", "namespace": "cilium-gateway", "class_name": "fixture-cilium", "management_target": context["target"],
        "cilium": {"version": "1.19.10", "gateway_api_version": "1.4.1", "namespace": "kube-system", "images": IMAGES},
        "serving_selector": plan["serving_selector"], "failure_domain_key": plan["failure_domain_key"], "https_port": 8443, "tls_secret": "gateway-tls",
        "routes": [{"name": "backend", "namespace": "cilium-probe", "hostname": HOST, "protocol": "http", "service": "backend", "port": 80, "target_port": 8080, "pod_selector": {"role": "server"}}]}
    if tls_passthrough:
        stage("prepare-passthrough-backend")
        backend_pki, route = passthrough_backend(create, context)
        route_plan["tls_passthrough_port"] = 9443
        route_plan["routes"].append(route)
    files = gateway.render(route_plan, "k3s"); bundle = package(root, "gateway-package", files)
    digest = json.loads(files["render-status.json"])["contract_sha256"]
    result = routes.operate(bundle, digest, apply=True, output=chosen["evidence"] / "gateway-install")
    test.assertTrue(result["gateway_programmed"])
    tls = ssl.create_default_context(cafile=str(pki / "ca.crt"))
    tls_outcomes = []
    def gateway_traffic():
        tls_outcomes.clear()
        for node in context["nodes"][1:]:
            row = {"node": node["name"], "matched": False}
            connection = DirectTLS(HOST, 8443, context=tls, timeout=3); connection.address = node["address"]
            try:
                connection.request("GET", "/"); response = connection.getresponse(); body = response.read(1024)
                row.update(tls_verified=True, http_status=response.status, body=body.decode("utf-8", errors="replace")[:160],
                    matched=response.status == 200 and body == BODY)
            except (OSError, HTTPException) as error:
                row.update(error=type(error).__name__, certificate_verify_code=getattr(error, "verify_code", None))
            finally: connection.close()
            tls_outcomes.append(row)
        return all(row["matched"] for row in tls_outcomes)
    try: wait(gateway_traffic, seconds=60)
    except Exception:
        print(json.dumps({"gateway_tls_probe_outcomes": tls_outcomes}), flush=True)
        listeners = {}
        for node in context["nodes"][1:]:
            result = context["ssh"](node, "sudo ss -H -lnt '( sport = :8443 )'", check=False, seconds=8)
            listeners[node["name"]] = {"read_succeeded": result.returncode == 0, "tcp_listener_present": bool(result.stdout.strip())}
        print(json.dumps({"gateway_guest_socket_observations": listeners}), flush=True)
        diagnostics(client)
        raise
    checked = routes.operate(bundle, digest, verify=True, output=chosen["evidence"] / "gateway-verify")
    test.assertTrue(checked["gateway_programmed"]); test.assertTrue(ready()); test.assertTrue(enforced())
    if tls_passthrough:
        verify_passthrough(test, context, backend_pki)
    stage("actual-policy-and-tls-gateway-verified")
    return {**dns_evidence, "real_cilium_datapath_verified": True, "real_cilium_network_policy_verified": True, "real_cilium_gateway_tls_serving_verified": True,
        "cilium_application_pod_uids": {name: value["uid"] for name, value in originals.items()}, "cilium_gateway_serving_nodes": 2,
        "cilium_policy_denied_sources": 2, "cilium_policy_backend_targets": 3, "cilium_gateway_tls_verified": True,
        "cilium_gateway_tls_passthrough_verified": tls_passthrough,
        "cilium_gateway_physical_ha_verified": False, "full_stack_verified": False}
