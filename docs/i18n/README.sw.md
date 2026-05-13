# Nafasi ya kazi ya Platform GitOps

**Language:** Kiswahili

Kiolezo cha faragha kwa jukwaa la CI/CD na GitOps lenye nodi tatu.

## Default stack

```text
RKE2 Kubernetes, 3 server nodes
Forgejo
Woodpecker CI
Argo CD HA
Harbor
CloudNativePG PostgreSQL
Longhorn or Rook/Ceph
MetalLB + ingress-nginx or Traefik
Prometheus + Grafana + Loki
Velero + off-cluster backups
Virtual IP / VIP
```

## Security reminder

Do not commit real IP addresses, domains, passwords, tokens, private keys, kubeconfigs, customer data, or company secrets.

## Main documentation

Read the English master documentation first:

```text
docs/QUICK_START.md
docs/ARCHITECTURE.md
docs/INSTALLATION.md
docs/SECRETS_AND_PRIVACY.md
```
