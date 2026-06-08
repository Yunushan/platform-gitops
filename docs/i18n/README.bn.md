# Platform GitOps ওয়ার্কস্পেস

**Language:** বাংলা

গোপনীয়তা-প্রথম তিন-নোড CI/CD ও GitOps প্ল্যাটফর্ম টেমপ্লেট।

## Default stack

```text
RKE2 Kubernetes, 3 server nodes
Forgejo
Woodpecker CI
Argo CD HA
Harbor
CloudNativePG PostgreSQL
Longhorn or Rook/Ceph
MetalLB + Traefik, ingress-nginx optional
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
