#!/usr/bin/env bash
set -euo pipefail
cat <<'EOF'
Recommended bootstrap order

1. Prepare 3 Linux nodes.
2. Configure Rocky Linux 10 prerequisites and API VIP using kube-vip or HAProxy/Keepalived.
3. Run make rke2-registry-check to verify node egress to the RKE2 image registry path.
4. Run make rke2-install, optionally with RKE2_VERSION=vX.Y.Z+rke2rN.
   This runs preflight, node preparation, registry checks, and RKE2 install.
5. Run make platform-bootstrap.
   This verifies RKE2, deploys/verifies the API VIP, writes controller hosts,
   bootstraps Argo CD, verifies or repairs pod DNS, installs MetalLB and
   Traefik, binds the app VIP, publishes Argo CD on HTTPS 443, and prints
   the current API, service, ingress, and GUI URL report.
6. Run make platform-status whenever you want the same read-only report again.
7. To register GitOps applications, replace or privately render profile
   placeholders, verify them with:
   PLATFORM_PROFILE=premium-3node make platform-profile-check
   then run:
   PLATFORM_REPO_URL=<THIS_REPO_URL> PLATFORM_APPLY_GITOPS=true make platform-argocd
8. Use make platform-ingress to verify or repair pod DNS, then deploy or repair
   the MetalLB/Traefik/Argo CD ingress path separately.
9. Let Argo CD deploy Forgejo, Woodpecker, Harbor, monitoring, logging, and backups.
10. Configure off-cluster backups.
11. Run make platform-production-check before calling the platform production-ready.
12. Run restore test before production use.
EOF
