#!/usr/bin/env bash
set -euo pipefail
cat <<'EOF'
Recommended bootstrap order

1. Prepare 3 Linux nodes.
2. Configure API VIP using kube-vip or HAProxy/Keepalived.
3. Install RKE2 server on node-1.
4. Join node-2 and node-3 as RKE2 servers.
5. Install cert-manager, MetalLB, and ingress.
6. Install Longhorn or Rook/Ceph.
7. Install CloudNativePG.
8. Install Argo CD HA.
9. Bootstrap platform-gitops root application.
10. Let Argo CD deploy Forgejo, Woodpecker, Harbor, monitoring, logging, and backups.
11. Configure off-cluster backups.
12. Run restore test before production use.
EOF
