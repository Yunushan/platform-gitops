# Network Security

The premium profile enforces namespace-level east-west isolation for platform
workloads and stateful services. NetworkPolicy is a runtime security boundary,
not a substitute for application authentication, TLS, or Kubernetes RBAC.

## Enforced Namespaces

The shared Kustomize component at
`gitops/clusters/rke2-main/premium-3node/components/network-isolation` is
included by these applications:

- Argo CD, Forgejo, Woodpecker, and Harbor.
- Keycloak, OpenBao, and step-ca.
- Monitoring and logging.
- Platform PostgreSQL, Platform Valkey, and object storage.

The component installs five policies in each namespace:

1. `platform-default-deny` selects every pod and denies ingress and egress.
2. `platform-allow-same-namespace` preserves StatefulSet clustering and
   same-namespace service traffic.
3. `platform-allow-dns` permits TCP and UDP DNS only to CoreDNS.
4. `platform-allow-ingress` permits Traefik, monitoring, declared platform
   consumers, and required platform controllers on explicit application and
   metrics ports.
5. `platform-allow-egress` permits only declared in-cluster platform and
   controller service ports. It grants no destination-unrestricted egress.

Outbound access is composed per application from seven explicit roles:

- `external-web-egress`: HTTP, HTTPS, and alternate HTTPS.
- `external-git-egress`: SSH Git and the native Git protocol.
- `external-smtp-egress`: SMTP, implicit TLS, and submission.
- `external-database-egress`: external MySQL/MariaDB and PostgreSQL modes.
- `external-cache-egress`: opt-in external Valkey/Redis and Sentinel modes.
- `external-object-storage-egress`: non-HTTPS S3-compatible endpoints.
- `kubernetes-api-egress`: direct RKE2 API access.

The profile contract defines the exact applications assigned to each role and
rejects both missing assignments and accidental additions. Platform Valkey has
no destination-unrestricted egress role.

Sensitive shared data ports are deliberately absent from those generic
policies. Separate client and server policies enforce these paths:

- PostgreSQL `5432` accepts Forgejo, Woodpecker, Harbor, Keycloak, Grafana,
  and the CloudNativePG operator. Only those five workload namespaces receive
  PostgreSQL client egress.
- Valkey TLS `6379` accepts Forgejo and Harbor. Only those two workload
  namespaces receive Valkey client egress; Valkey peers additionally receive
  `6379` and Sentinel `26379`, while monitoring receives exporter `9121` only.
- PostgreSQL TLS and Valkey authentication remain mandatory application-layer
  controls; NetworkPolicy reduces reachability but does not replace them.

Infrastructure namespaces such as `kube-system`, `longhorn-system`, `traefik`,
`cert-manager`, `kyverno`, and `cnpg-system` are not selected by the shared
default-deny component. Their host networking, admission webhooks, storage data
paths, and control-plane callbacks require component-specific policies and live
failure testing before isolation can be promoted safely.

## Production Verification

Run the live proof after Argo CD has synchronized the premium applications:

```bash
make platform-network-isolation-verify
```

The verifier fails unless all five baseline policies exist in all twelve
namespaces and every specialized data-service policy exists. It then proves
four service paths:

- A pod in `woodpecker` must resolve DNS and connect to PostgreSQL.
- Pods in `forgejo` and `harbor` must resolve DNS and connect to Valkey.
- A pod in isolated `argocd` must be unable to connect to either data service.

This positive-and-negative test prevents an applied but unenforced
NetworkPolicy from being accepted as production evidence. The full
`platform-production-check` runs this proof automatically.

## Custom Endpoints

Do not remove `platform-default-deny` to support a private dependency. Add the
narrowest required namespace selector, pod selector, protocol, and port to a
private overlay. The external role components constrain ports but intentionally
remain endpoint-agnostic so the public profile can support private Git, mail,
identity, database, cache, and object-storage systems. The production profile
does not assign `external-cache-egress` to Forgejo or Harbor when they use the
managed Valkey service. Production private overlays should replace applicable
external role policies with Cilium FQDN or CIDR policies bound to approved
endpoints.

Record every production exception with an owner, purpose, source, destination,
port, review date, and expiry. Re-run the allowed and denied probes after any
CNI, NetworkPolicy, ingress, service-mesh, or application-port change.
