#!/usr/bin/env bash
set -euo pipefail
cat <<'EOF'
Recommended bootstrap order

1. Prepare 3 Linux nodes.
2. Configure Rocky Linux 10 prerequisites and API VIP using kube-vip or HAProxy/Keepalived.
3. Run make rke2-preflight to verify SSH, sudo, inventory, and node host resolution.
4. Run make rke2-prepare.
5. Run make rke2-install, optionally with RKE2_VERSION=vX.Y.Z+rke2rN.
6. Use Cilium CNI and install cert-manager, MetalLB, and Traefik ingress.
7. Install Longhorn or Rook/Ceph.
8. Install CloudNativePG.
9. Install Argo CD HA.
10. Bootstrap platform-gitops root application, using the default or premium 3-node root app.
11. Let Argo CD deploy Forgejo, Woodpecker, Harbor, monitoring, logging, and backups.
12. Configure off-cluster backups.
13. Run restore test before production use.
EOF
