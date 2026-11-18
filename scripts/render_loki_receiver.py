#!/usr/bin/env python3
"""Render the vendored Loki chart values for an independent replicated receiver."""

import argparse
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import IMAGE, dns_name, write_bundle
from render_gerrit_sites import label
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
CHART = "gitops/clusters/rke2-main/premium-3node/apps/loki/charts/loki"
NAME = "platform-loki"
RUNTIMES = ("rke2", "k3s", "k8s")
BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")


def validate(plan):
    fields = {"schema_version", "namespace", "dns_domain", "dns_namespace", "dns_service",
              "loki_image", "gateway_image", "gateway_secret", "tenant", "s3", "wal_storage_class",
              "wal_size_gib", "retention_days", "ingress"}
    if (not isinstance(plan, dict) or set(plan) != fields or type(plan["schema_version"]) is not int
            or plan["schema_version"] != 1 or not dns_name(plan["dns_domain"])
            or any(not label(plan[key]) for key in ("namespace", "dns_namespace", "dns_service",
                                                      "gateway_secret", "tenant", "wal_storage_class"))):
        raise ValueError("Declare the namespace, DNS service, tenant, and WAL storage class")
    if plan["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}:
        raise ValueError("Use a dedicated receiver namespace")
    for key in ("loki_image", "gateway_image"):
        if not isinstance(plan[key], str) or not IMAGE.fullmatch(plan[key]) or "/" not in plan[key].split("@")[0]:
            raise ValueError("Pin the Loki and NGINX images by digest")
    for key, minimum, maximum in (("wal_size_gib", 1, 1024), ("retention_days", 1, 365)):
        if type(plan[key]) is not int or not minimum <= plan[key] <= maximum:
            raise ValueError("Size the WAL and retention explicitly")
    store = plan["s3"]
    if (not isinstance(store, dict) or set(store) != {"endpoint", "bucket", "region", "credentials", "ca_secret"}
            or any(not label(store[key]) for key in ("region", "credentials", "ca_secret"))
            or not isinstance(store["bucket"], str) or not BUCKET.fullmatch(store["bucket"])):
        raise ValueError("Declare the isolated S3 bucket and existing credentials and CA Secrets")
    endpoint = store["endpoint"]
    if not isinstance(endpoint, str) or not re.fullmatch(r"https://[a-z0-9.-]+(?::[0-9]{1,5})?", endpoint):
        raise ValueError("Use a credential-free HTTPS S3 origin")
    target = urlsplit(endpoint)
    if not dns_name(target.hostname) or target.port == 0:
        raise ValueError("Use a valid S3 hostname and optional port")
    ingress = plan["ingress"]
    if (not isinstance(ingress, dict) or set(ingress) != {"host", "class", "namespace", "tls_secret"}
            or not dns_name(ingress["host"]) or "." not in ingress["host"]
            or any(not label(ingress[key]) for key in ("class", "namespace", "tls_secret"))):
        raise ValueError("Declare the HTTPS gateway hostname and ingress controller")


def image_values(image):
    path, digest = image.split("@", 1)
    registry, repository = path.split("/", 1)
    if ":" in repository.rsplit("/", 1)[-1] or not ("." in registry or ":" in registry):
        raise ValueError("Use registry/repository@sha256 image names without tags")
    return {"registry": registry, "repository": repository, "digest": digest}


def gateway_config(plan):
    service = f"{NAME}.{plan['namespace']}.svc.{plan['dns_domain']}"
    dns = f"{plan['dns_service']}.{plan['dns_namespace']}.svc.{plan['dns_domain']}."
    # Only collector and query routes go through the authenticated gateway.
    return f"""worker_processes 2;
pid /tmp/nginx.pid;
error_log /dev/stderr;
events {{ worker_connections 1024; }}
http {{
  access_log off;
  client_max_body_size 4m;
  client_body_temp_path /tmp/client_temp;
  proxy_temp_path /tmp/proxy_temp;
  fastcgi_temp_path /tmp/fastcgi_temp;
  uwsgi_temp_path /tmp/uwsgi_temp;
  scgi_temp_path /tmp/scgi_temp;
  resolver {dns} valid=5s;
  proxy_connect_timeout 3s;
  proxy_read_timeout 60s;
  proxy_set_header X-Scope-OrgID {plan['tenant']};
  proxy_set_header Authorization "";
  server {{
    listen 8080;
    auth_basic "Loki";
    auth_basic_user_file /etc/nginx/secrets/.htpasswd;
    set $loki http://{service}:3100;
    location = / {{
      auth_basic off;
      return 200;
    }}
    location = /loki/api/v1/push {{
      limit_except POST {{ deny all; }}
      proxy_pass $loki;
    }}
    location ~ ^/loki/api/v1/(query|query_range|labels|label/[A-Za-z_][A-Za-z0-9_]*/values|series|index/stats|index/volume|index/volume_range)$ {{
      limit_except GET {{ deny all; }}
      proxy_pass $loki;
    }}
    location / {{ return 404; }}
  }}
}}
"""


def backend_policy(plan):
    service_labels = {"app.kubernetes.io/instance": NAME, "app.kubernetes.io/component": "single-binary"}
    gateway_labels = {"app.kubernetes.io/instance": NAME, "app.kubernetes.io/component": "gateway"}
    return [
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
         "metadata": {"name": f"{NAME}-backend-ingress", "namespace": plan["namespace"]},
         "spec": {"podSelector": {"matchLabels": service_labels}, "policyTypes": ["Ingress"], "ingress": [
             {"from": [{"podSelector": {"matchLabels": gateway_labels}}],
              "ports": [{"port": 3100, "protocol": "TCP"}]},
             {"from": [{"podSelector": {"matchLabels": service_labels}}],
              "ports": [{"port": 3100, "protocol": "TCP"}, {"port": 9095, "protocol": "TCP"},
                        {"port": 7946, "protocol": "TCP"}, {"port": 7946, "protocol": "UDP"}]},
         ]}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
         "metadata": {"name": f"{NAME}-gateway-ingress", "namespace": plan["namespace"]},
         "spec": {"podSelector": {"matchLabels": gateway_labels}, "policyTypes": ["Ingress"], "ingress": [
             {"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": plan["ingress"]["namespace"]}}}],
              "ports": [{"port": 8080, "protocol": "TCP"}]},
         ]}},
    ]


def render(plan, runtime):
    if runtime not in RUNTIMES:
        raise ValueError("Select a Kubernetes receiver runtime")
    validate(plan)
    s3 = plan["s3"]
    ingress = plan["ingress"]
    ca_path = "/etc/loki/s3-ca/ca.crt"
    values = {
        "deploymentMode": "SingleBinary", "fullnameOverride": NAME,
        "global": {"clusterDomain": plan["dns_domain"], "dnsNamespace": plan["dns_namespace"], "dnsService": plan["dns_service"]},
        "loki": {
            "image": image_values(plan["loki_image"]), "auth_enabled": True,
            "podSecurityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
            "commonConfig": {"replication_factor": 3},
            "extraMemberlistConfig": {"rejoin_interval": "15s"},
            "storage": {"type": "s3", "bucketNames": {"chunks": s3["bucket"]},
                        "s3": {"endpoint": s3["endpoint"], "region": s3["region"], "s3ForcePathStyle": True,
                               "insecure": False, "http_config": {"ca_file": ca_path}}},
            "schemaConfig": {"configs": [{"from": "2024-01-01", "store": "tsdb", "object_store": "s3", "schema": "v13",
                                          "index": {"prefix": "loki_index_", "period": "24h"}}]},
            "limits_config": {"retention_period": f"{plan['retention_days'] * 24}h"},
            "compactor": {"retention_enabled": True, "delete_request_store": "s3"},
            "ingester": {
                # Keep healthy Service endpoints available when a peer misses its heartbeats.
                "lifecycler": {"readiness_check_ring_health": False},
                "wal": {"enabled": True, "dir": "/var/loki/wal", "flush_on_shutdown": True,
                        "replay_memory_ceiling": "512MB"},
            },
            "structuredConfig": {"analytics": {"reporting_enabled": False}},
        },
        "singleBinary": {
            "replicas": 3, "terminationGracePeriodSeconds": 120,
            "nodeSelector": {"kubernetes.io/os": "linux"},
            "persistence": {"enabled": True, "enableStatefulSetAutoDeletePVC": False,
                            "storageClass": plan["wal_storage_class"], "size": f"{plan['wal_size_gib']}Gi"},
            "resources": {"requests": {"cpu": "250m", "memory": "768Mi"}, "limits": {"memory": "1536Mi"}},
            "extraEnv": [{"name": key, "valueFrom": {"secretKeyRef": {"name": s3["credentials"], "key": key}}}
                         for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")],
            "extraVolumeMounts": [{"name": "s3-ca", "mountPath": "/etc/loki/s3-ca", "readOnly": True}],
            "extraVolumes": [{"name": "s3-ca", "secret": {"secretName": s3["ca_secret"],
                                                        "items": [{"key": "ca.crt", "path": "ca.crt"}]}}],
        },
        "podDisruptionBudget": {"minAvailable": 2},
        "memberlist": {"service": {"publishNotReadyAddresses": True}},
        "serviceAccount": {"automountServiceAccountToken": False},
        "rbac": {"namespaced": True},
        "gateway": {
            "image": image_values(plan["gateway_image"]), "enabled": True, "replicas": 2,
            "nodeSelector": {"kubernetes.io/os": "linux"},
            "podSecurityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
            "deploymentStrategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}},
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            "basicAuth": {"enabled": True, "existingSecret": plan["gateway_secret"]},
            "nginxConfig": {"file": gateway_config(plan)},
            "ingress": {"enabled": True, "ingressClassName": ingress["class"],
                        "hosts": [{"host": ingress["host"], "paths": [{"path": "/", "pathType": "Prefix"}]}],
                        "tls": [{"hosts": [ingress["host"]], "secretName": ingress["tls_secret"]}]},
        },
        "write": {"replicas": 0}, "read": {"replicas": 0}, "backend": {"replicas": 0},
        "chunksCache": {"enabled": False}, "resultsCache": {"enabled": False}, "test": {"enabled": False},
        "lokiCanary": {"enabled": False}, "sidecar": {"rules": {"enabled": False}}, "minio": {"enabled": False},
        "ruler": {"enabled": False},
        "extraObjects": backend_policy(plan),
    }
    return {"loki-values.yaml": yaml.safe_dump(values, sort_keys=False)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, max_bytes=65536))
        write_bundle(render(plan, args.runtime), args.output_dir, private_root=ROOT / "private")
    except (ValueError, OSError):
        print("Loki receiver render failed. Review the private plan and output path.", file=sys.stderr)
        return 1
    print(json.dumps({"rendered": True, "deployed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
