#!/usr/bin/env bash
set -euo pipefail
cat <<'EOF'
Recommended bootstrap order

1. Prepare 3 Linux nodes.
2. Configure Rocky Linux 10 prerequisites and API VIP using kube-vip or HAProxy/Keepalived.
3. Run make rke2-registry-check to verify node egress to the RKE2 image registry path.
4. Run make rke2-install, optionally with RKE2_VERSION=vX.Y.Z+rke2rN.
   This runs preflight, node preparation, registry checks, and RKE2 install.
5. Verify all three nodes are Ready with make rke2-verify.
6. Deploy and verify the API VIP with make rke2-api-vip and make rke2-controller-hosts.
7. Use Cilium CNI and install cert-manager, MetalLB, and Traefik ingress.
8. Install Longhorn or Rook/Ceph.
9. Install CloudNativePG.
10. Install Argo CD HA.
11. Bootstrap platform-gitops root application, using the default or premium 3-node root app.
12. Let Argo CD deploy Forgejo, Woodpecker, Harbor, monitoring, logging, and backups.
13. Configure off-cluster backups.
14. Run restore test before production use.
EOF
