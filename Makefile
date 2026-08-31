SHELL := bash
PYTHON ?= python3

.PHONY: platform-inventory-preflight
.PHONY: platform-forgejo-runtime-repair

.PHONY: help init-local validate no-secrets security-scan supply-chain-posture supply-chain-verify vendored-chart-provenance-verify github-governance-plan github-governance-security-apply github-governance-apply github-governance-verify rendered-schema-verify rendered-private-schema-verify policy-cel-verify forge-migration-validate forge-migration-run forge-migration-verify forge-migration-proof-verify forge-migration-live-plan forge-migration-live-run forge-workspace-validate forge-workspace-export forge-workspace-import forge-pipeline-convert forge-cutover-validate forge-cutover-discover forge-cutover-prepare forge-cutover-verify forge-cutover-activate forge-cutover-rollback forge-cutover-proof-verify forge-transition-validate forge-transition-discover forge-transition-prepare forge-transition-verify-shadow forge-transition-enter forge-transition-status forge-transition-reconcile forge-transition-relay forge-transition-fallback forge-transition-finalize forge-transition-failback forge-transition-rollback forge-transition-proof-verify bootstrap-plan platform-render-private-values platform-profile-check platform-bootstrap platform-first-deploy platform-first-deploy-auto platform-first-deploy-seed platform-seed-git platform-seed-git-sync platform-seed-git-remove platform-argocd platform-argocd-core platform-argocd-ha platform-argocd-expose platform-argocd-unexpose platform-argocd-diagnose platform-argocd-service-repair platform-app-secrets platform-app-health platform-ci-health platform-woodpecker-repair platform-monitoring-health platform-monitoring-repair platform-tls platform-tls-verify platform-data-protection platform-policy-readiness platform-network-isolation-verify platform-internal-tls-verify platform-openbao-status platform-openbao-verify platform-openbao-ceremony-digest platform-openbao-ceremony-evidence-verify platform-observability-verify platform-capacity-verify platform-image-inventory-verify platform-production-evidence platform-production-score platform-production-check platform-node-storage-diagnose platform-node-storage-cleanup platform-longhorn-bootstrap platform-longhorn-runtime-repair platform-longhorn-crd-repair platform-forgejo-diagnose platform-forgejo-repair platform-forgejo-storage-repair platform-forgejo-ingress platform-forgejo-recovery-drill platform-dns-repair platform-service-path-consumers-repair platform-service-path-repair platform-dns-repair-traefik platform-ingress platform-ingress-vip platform-ingress-diagnose platform-status rke2-preflight rke2-controller-hosts rke2-prepare rke2-registry-check rke2-api-vip rke2-install rke2-recover rke2-reset rke2-verify rke2-diagnose rke2-status rke2-cleanup-installers rke2-network-check rke2-ping docs-list ci-list

help:
	@echo "Platform GitOps Workspace"
	@echo ""
	@echo "Targets:"
	@echo "  init-local      Create ignored local config files from examples"
	@echo "  validate        Run repository validation"
	@echo "  no-secrets      Scan repository for obvious secrets/private data"
	@echo "  security-scan   Run Trivy, Gitleaks, and Semgrep security scanners"
	@echo "  supply-chain-posture  Generate SBOM and optional Scorecard/Cosign evidence"
	@echo "  supply-chain-verify  Require scanners, SBOM, Scorecard threshold, and digest-bound Cosign proof"
	@echo "  vendored-chart-provenance-verify  Download pinned Helm packages and verify exact provenance"
	@echo "  github-governance-plan  Plan GitHub security, immutable-tag, and release-approval controls"
	@echo "  github-governance-security-apply  Enable GitHub scanner controls without changing release gates"
	@echo "  github-governance-apply  Apply GitHub release controls with an independent reviewer"
	@echo "  github-governance-verify  Verify live GitHub branch, tag, release, Actions, and security controls"
	@echo "  rendered-schema-verify  Render selected GitOps applications and validate Kubernetes object schemas"
	@echo "  rendered-private-schema-verify  Render a complete synthetic premium profile and validate every schema"
	@echo "  policy-cel-verify  Compile and behavior-test active policies with the pinned Kyverno CLI"
	@echo "  forge-migration-validate  Validate PLAN and its fail-closed migration surface policy"
	@echo "  forge-migration-run  Migrate PLAN and write optional PROOF using optional WORK_DIR"
	@echo "  forge-migration-verify  Re-read source/destination from PLAN and write optional PROOF"
	@echo "  forge-migration-proof-verify  Verify stored PROOF integrity and acceptance"
	@echo "  forge-migration-live-plan  Print the redacted four-direction live acceptance manifest"
	@echo "  forge-migration-live-run  Run opt-in GitHub/GitLab/Forgejo live migration acceptance and write LIVE_DIR proof"
	@echo "  forge-workspace-validate  Validate selective GitLab users/groups/projects/CI workspace PLAN"
	@echo "  forge-workspace-export  Export selected GitLab workspace surfaces to redacted SNAPSHOT"
	@echo "  forge-workspace-import  Import only PLAN surfaces marked managed into Forgejo/Woodpecker"
	@echo "  forge-pipeline-convert  Convert a supported GitLab/GitHub pipeline to Woodpecker or fail with a compatibility report"
	@echo "  forge-cutover-validate  Validate the opt-in GitLab-to-Forgejo cutover PLAN"
	@echo "  forge-cutover-discover  Inventory source/destination CI/CD surfaces into DISCOVERY proof"
	@echo "  forge-cutover-prepare  Prepare shadow Forgejo/Woodpecker/Harbor state from approved DISCOVERY"
	@echo "  forge-cutover-verify  Run shadow canary and write VERIFICATION proof from PREPARED proof"
	@echo "  forge-cutover-activate  Freeze GitLab and activate Woodpecker using approved VERIFICATION"
	@echo "  forge-cutover-rollback  Restore GitLab and disable destination authority from EVIDENCE"
	@echo "  forge-cutover-proof-verify  Verify stored cutover PROOF integrity and acceptance"
	@echo "  forge-transition-validate  Validate an optional GitLab/GitHub coexistence PLAN"
	@echo "  forge-transition-discover  Inventory every declared CI/CD surface into DISCOVERY proof"
	@echo "  forge-transition-prepare  Build the approved shadow destination and durable STATE"
	@echo "  forge-transition-verify-shadow  Reconcile and canary the shadow destination"
	@echo "  forge-transition-enter  Disable source CI while keeping source Git writable"
	@echo "  forge-transition-status  Prove relay lag, source authority, and destination health"
	@echo "  forge-transition-reconcile  Run one provider-neutral relay reconciliation"
	@echo "  forge-transition-relay  Run the supervised relay with automatic rollback"
	@echo "  forge-transition-fallback  Restore source CI temporarily while keeping relay synchronization"
	@echo "  forge-transition-finalize  Freeze source Git after a final zero-drift reconciliation"
	@echo "  forge-transition-failback  Reverse-sync finalized Forgejo data and restore source authority"
	@echo "  forge-transition-rollback  Stop transition and restore source CI from STATE"
	@echo "  forge-transition-proof-verify  Verify stored transition PROOF integrity and acceptance"
	@echo "  bootstrap-plan  Print recommended bootstrap order"
	@echo "  platform-render-private-values  Render first-deploy private values for platform apps from env/private env file or inventory"
	@echo "  platform-profile-check  Verify selected GitOps profile is structurally complete and has no unresolved placeholders"
	@echo "  platform-bootstrap  Verify RKE2/API VIP, bootstrap Argo CD, configure app VIP when ready, and print access report"
	@echo "  platform-first-deploy  First private GitOps deploy: bootstrap Argo CD, register repo credentials, publish ingress, and print status"
	@echo "  platform-first-deploy-auto  Non-interactive first private deploy using private/first-deploy.env or exported variables"
	@echo "  platform-first-deploy-seed  First deploy with no previous Git server by using temporary internal seed Git"
	@echo "  platform-seed-git  Create temporary internal read-only seed Git service on the first RKE2 node"
	@echo "  platform-seed-git-sync  Optionally pull source remote, then mirror the current branch to temporary seed Git"
	@echo "  platform-seed-git-remove  Remove temporary internal seed Git service"
	@echo "  platform-argocd  Bootstrap Argo CD; set PLATFORM_APPLY_GITOPS=true and PLATFORM_REPO_URL to register apps"
	@echo "  platform-argocd-core  Bootstrap standard Argo CD and clean stale HA Redis bootstrap resources"
	@echo "  platform-argocd-ha  Bootstrap Argo CD HA explicitly"
	@echo "  platform-argocd-expose  Expose Argo CD through a bootstrap NodePort, default HTTPS port 30443"
	@echo "  platform-argocd-unexpose  Remove the bootstrap Argo CD NodePort exposure"
	@echo "  platform-argocd-diagnose  Show Argo CD rollout, image, event, log, and registry diagnostics"
	@echo "  platform-argocd-service-repair  Repair Argo CD internal repo-server/Redis service reachability"
	@echo "  platform-app-secrets  Generate bootstrap Harbor, Woodpecker, Loki, and Velero secrets"
	@echo "  platform-app-health  Verify platform app sync, storage, pod readiness, ingress, and service paths"
	@echo "  platform-ci-health  Verify Argo CD runtime plus Woodpecker CI ingress, agents, and service paths"
	@echo "  platform-woodpecker-repair  Hard-refresh/sync Woodpecker, verify runtime image tags, and run focused CI health"
	@echo "  platform-monitoring-health  Verify focused Grafana and Prometheus readiness and ingress APIs"
	@echo "  platform-monitoring-repair  Repair monitoring reconciliation, storage prerequisites, and ready backends"
	@echo "  platform-tls  Validate and distribute a pre-issued wildcard TLS certificate without storing it in Git"
	@echo "  platform-tls-verify  Verify TLS Secrets, hostname coverage, expiry, and the certificate served by the ingress VIP"
	@echo "  platform-data-protection  Verify off-cluster backup freshness and approved restore-drill evidence"
	@echo "  platform-policy-readiness  Verify Kyverno CEL baselines and optional signed-image admission"
	@echo "  platform-network-isolation-verify  Prove premium default-deny policies allow trusted and block untrusted service paths"
	@echo "  platform-internal-tls-verify  Prove managed trust and verified OpenBao/PostgreSQL service TLS"
	@echo "  platform-openbao-status  Print sanitized OpenBao replica, seal, HA, and cluster-identity state"
	@echo "  platform-openbao-verify  Require three Ready, initialized, unsealed OpenBao HA replicas"
	@echo "  platform-openbao-ceremony-digest  Hash the profile-specific OpenBao application tree"
	@echo "  platform-openbao-ceremony-evidence-verify  Validate EVIDENCE=/private/openbao-ceremony.json"
	@echo "  platform-observability-verify  Prove authenticated Loki ingestion, retention, Alloy collection, and alert delivery"
	@echo "  platform-capacity-verify  Fail when node, scheduler, or Longhorn headroom is below production thresholds"
	@echo "  platform-image-inventory-verify  Reconcile exact rendered/live image digests with signatures and admission scope"
	@echo "  platform-inventory-preflight  Normalize and validate the private Ansible inventory before cluster targets"
	@echo "  platform-node-storage-diagnose  Report root, container runtime, journal, and Longhorn storage usage"
	@echo "  platform-node-storage-cleanup  Reclaim safe node caches without deleting Longhorn or PVC data"
	@echo "  platform-production-evidence  Run production gates and retain commit-bound, independently approved evidence"
	@echo "  platform-production-score  Require live, governance, approval, and signed-release evidence for exactly 100/100"
	@echo "  platform-production-check  Run repo, RKE2, app, backup, and restore-evidence readiness gates"
	@echo "  platform-longhorn-bootstrap  Load private settings, bootstrap Longhorn storage, and verify CSI/PVC readiness"
	@echo "  platform-longhorn-runtime-repair  Repair Longhorn manager/CSI registration without blocking on storage capacity"
	@echo "  platform-longhorn-crd-repair  Restore missing Longhorn CRDs and restart Longhorn manager"
	@echo "  platform-forgejo-diagnose  Show Forgejo init, logs, PVC/PV, Longhorn volume, service, and ingress diagnostics"
	@echo "  platform-forgejo-repair  Safely repair Longhorn/Forgejo runtime and verify the published ingress"
	@echo "  platform-forgejo-runtime-repair  Repair Forgejo PostgreSQL trust and runtime dependency drift without changing data volumes"
	@echo "  platform-forgejo-storage-repair  Repair first-deploy Forgejo PVC, Longhorn disk, and volume attach issues"
	@echo "  platform-forgejo-ingress  Publish and verify Forgejo through Traefik on the app VIP"
	@echo "  platform-forgejo-recovery-drill  Opt-in cross-node Forgejo failover drill with encrypted-storage proof"
	@echo "  platform-dns-repair  Verify pod DNS and repair CoreDNS upstreams for external chart repositories"
	@echo "  platform-service-path-consumers-repair  Refresh Woodpecker agents after ClusterIP service-path repair"
	@echo "  platform-service-path-repair  Repair ClusterIP/DNS service paths and refresh Woodpecker consumers"
	@echo "  platform-dns-repair-traefik  Optional external DNS diagnostic against the Traefik chart repository"
	@echo "  platform-ingress  Install MetalLB/Traefik, bind the app VIP, and publish Argo CD on 443"
	@echo "  platform-ingress-vip  Alias for platform-ingress"
	@echo "  platform-ingress-diagnose  Classify app VIP, Traefik, MetalLB, and controller reachability without redeploying"
	@echo "  platform-status  Show API, app VIP, Argo CD, ingress, service, and GUI URL status"
	@echo "  rke2-preflight  Check Ansible SSH/sudo and write node /etc/hosts"
	@echo "  rke2-controller-hosts  Write platform /etc/hosts on the Ansible controller"
	@echo "  rke2-prepare    Prepare Linux nodes through Ansible"
	@echo "  rke2-registry-check  Check Docker Hub/RKE2 image pull egress, optional HOST=node-1"
	@echo "  rke2-api-vip    Deploy kube-vip for the Kubernetes API VIP"
	@echo "  rke2-install    Install RKE2 through Ansible"
	@echo "  rke2-recover    Safely recover interrupted RKE2 bootstrap without deleting cluster data"
	@echo "  rke2-reset      Destructively reset failed RKE2 bootstrap state with confirmation"
	@echo "  rke2-verify     Verify RKE2 service, API, network, and Ready nodes"
	@echo "  rke2-diagnose   Collect RKE2 diagnostics, optional HOST=node-1"
	@echo "  rke2-status     Show RKE2 install/service diagnostics"
	@echo "  rke2-cleanup-installers  Stop stale installer jobs, optional HOST=node-1"
	@echo "  rke2-network-check  Check node-to-node RKE2 API/supervisor reachability"
	@echo "  rke2-ping       Check Ansible connectivity, optional HOST=node-1"
	@echo "  docs-list       List key docs"
	@echo "  ci-list         List included CI definitions"

init-local:
	@bash scripts/init-local-config.sh

validate:
	@make_python="$(PYTHON)"; make_python_origin="$(origin PYTHON)"; \
	env_file="$${PLATFORM_VALIDATION_ENV_FILE:-}"; \
	if [[ -z "$${env_file}" && -n "$${PLATFORM_SEED_DEPLOY_ENV_FILE:-}" ]]; then env_file="$${PLATFORM_SEED_DEPLOY_ENV_FILE}"; fi; \
	if [[ -z "$${env_file}" && -n "$${PLATFORM_FIRST_DEPLOY_ENV_FILE:-}" ]]; then env_file="$${PLATFORM_FIRST_DEPLOY_ENV_FILE}"; fi; \
	if [[ -z "$${env_file}" && -f private/seed-git.env ]]; then env_file=private/seed-git.env; fi; \
	if [[ -z "$${env_file}" && -f private/first-deploy.env ]]; then env_file=private/first-deploy.env; fi; \
	if [[ -n "$${env_file}" ]]; then . scripts/bootstrap/load-env-file.sh; load_env_file "$${env_file}" preserve-existing; fi; \
	python_bin="$${PYTHON:-$${make_python}}"; \
	if [[ "$${make_python_origin}" == "command line" ]]; then python_bin="$${make_python}"; fi; \
	exec "$${python_bin}" scripts/run_validation.py

github-governance-plan:
	@$(PYTHON) scripts/configure_github_governance.py plan

github-governance-security-apply:
	@$(PYTHON) scripts/configure_github_governance.py apply-security

github-governance-apply:
	@$(PYTHON) scripts/configure_github_governance.py apply

github-governance-verify:
	@$(PYTHON) scripts/verify_github_governance.py

forge-migration-validate:
	@test -n "$(PLAN)" || (echo "PLAN=/path/to/migration-plan.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_migration.py validate-plan "$(PLAN)" $(if $(PROOF),--proof "$(PROOF)",)

forge-migration-run:
	@test -n "$(PLAN)" || (echo "PLAN=/path/to/migration-plan.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_migration.py migrate "$(PLAN)" $(if $(WORK_DIR),--work-dir "$(WORK_DIR)",) $(if $(PROOF),--proof "$(PROOF)",)

forge-migration-verify:
	@test -n "$(PLAN)" || (echo "PLAN=/path/to/migration-plan.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_migration.py verify "$(PLAN)" $(if $(PROOF),--proof "$(PROOF)",)

forge-migration-proof-verify:
	@test -n "$(PROOF)" || (echo "PROOF=/path/to/migration-proof.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_migration.py verify-proof "$(PROOF)"

forge-migration-live-plan:
	@$(PYTHON) scripts/forge_migration_live.py --dry-run

forge-migration-live-run:
	@test "$(FORGE_MIGRATION_LIVE)" = "1" || (echo "FORGE_MIGRATION_LIVE=1 is required for a live acceptance run" >&2; exit 2)
	@test -n "$(LIVE_DIR)" || (echo "LIVE_DIR=/private/evidence/directory is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_migration_live.py --run --output-dir "$(LIVE_DIR)" $(if $(filter 1 true yes,$(LIVE_CLEANUP)),--cleanup,)

forge-workspace-validate:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/gitlab-to-forgejo.workspace.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_workspace.py validate-plan "$(PLAN)" $(if $(PROOF),--proof "$(PROOF)",)

forge-workspace-export:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/gitlab-to-forgejo.workspace.json is required" >&2; exit 2)
	@test -n "$(SNAPSHOT)" || (echo "SNAPSHOT=private/migrations/proof/workspace-snapshot.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_workspace.py export "$(PLAN)" --snapshot "$(SNAPSHOT)" $(if $(PROOF),--proof "$(PROOF)",)

forge-workspace-import:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/gitlab-to-forgejo.workspace.json is required" >&2; exit 2)
	@test -n "$(SNAPSHOT)" || (echo "SNAPSHOT=private/migrations/proof/workspace-snapshot.json is required" >&2; exit 2)
	@test -n "$(WORK_DIR)" || (echo "WORK_DIR=private/migrations/workspace is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_workspace.py import "$(PLAN)" --snapshot "$(SNAPSHOT)" --work-dir "$(WORK_DIR)" $(if $(PROOF),--proof "$(PROOF)",)

forge-pipeline-convert:
	@test -n "$(PROVIDER)" || (echo "PROVIDER=gitlab or github is required" >&2; exit 2)
	@test -n "$(SOURCE)" || (echo "SOURCE=/path/to/.gitlab-ci.yml or workflow.yml is required" >&2; exit 2)
	@test -n "$(OUTPUT)" || (echo "OUTPUT=/path/to/.woodpecker.yml is required" >&2; exit 2)
	@test -n "$(REPORT)" || (echo "REPORT=/path/to/conversion-report.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_pipeline.py "$(PROVIDER)" "$(SOURCE)" --output "$(OUTPUT)" --report "$(REPORT)" $(if $(GATE_MARKER),--deployment-gate-marker "$(GATE_MARKER)",) $(if $(DEFAULT_IMAGE),--default-image "$(DEFAULT_IMAGE)",) $(foreach secret,$(SECRET),--secret-name "$(secret)") $(foreach job,$(DEPLOYMENT_JOB),--deployment-job "$(job)") $(foreach mapping,$(RUNNER_LABEL),--runner-label "$(mapping)") $(if $(SCHEDULE_MAPPING),--schedule-mapping "$(SCHEDULE_MAPPING)",)

forge-cutover-validate:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/cutover.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_cutover.py validate-plan "$(PLAN)" $(if $(PROOF),--proof "$(PROOF)",)

forge-cutover-discover:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/cutover.json is required" >&2; exit 2)
	@test -n "$(DISCOVERY)" || (echo "DISCOVERY=private/migrations/proof/discovery.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_cutover.py discover "$(PLAN)" --proof "$(DISCOVERY)"

forge-cutover-prepare:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/cutover.json is required" >&2; exit 2)
	@test -n "$(DISCOVERY)" || (echo "DISCOVERY=private/migrations/proof/discovery.json is required" >&2; exit 2)
	@test -n "$(PREPARED)" || (echo "PREPARED=private/migrations/proof/prepared.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_cutover.py prepare "$(PLAN)" --discovery "$(DISCOVERY)" --proof "$(PREPARED)"

forge-cutover-verify:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/cutover.json is required" >&2; exit 2)
	@test -n "$(PREPARED)" || (echo "PREPARED=private/migrations/proof/prepared.json is required" >&2; exit 2)
	@test -n "$(VERIFICATION)" || (echo "VERIFICATION=private/migrations/proof/verification.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_cutover.py verify "$(PLAN)" --prepared "$(PREPARED)" --proof "$(VERIFICATION)"

forge-cutover-activate:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/cutover.json is required" >&2; exit 2)
	@test -n "$(VERIFICATION)" || (echo "VERIFICATION=private/migrations/proof/verification.json is required" >&2; exit 2)
	@test -n "$(ACTIVATION)" || (echo "ACTIVATION=private/migrations/proof/activation.json is required" >&2; exit 2)
	@test -n "$(CHECKPOINT)" || (echo "CHECKPOINT=private/migrations/proof/activation-checkpoint.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_cutover.py activate "$(PLAN)" --verification "$(VERIFICATION)" --proof "$(ACTIVATION)" --checkpoint "$(CHECKPOINT)" $(if $(WORK_DIR),--work-dir "$(WORK_DIR)",)

forge-cutover-rollback:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/cutover.json is required" >&2; exit 2)
	@test -n "$(EVIDENCE)" || (echo "EVIDENCE=private/migrations/proof/activation-or-checkpoint.json is required" >&2; exit 2)
	@test -n "$(ROLLBACK)" || (echo "ROLLBACK=private/migrations/proof/rollback.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_cutover.py rollback "$(PLAN)" --activation "$(EVIDENCE)" --proof "$(ROLLBACK)"

forge-cutover-proof-verify:
	@test -n "$(PROOF)" || (echo "PROOF=private/migrations/proof/cutover-proof.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_cutover.py verify-proof "$(PROOF)"

forge-transition-validate:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/transition.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_transition.py validate-plan "$(PLAN)" $(if $(PROOF),--proof "$(PROOF)",)

forge-transition-discover:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/transition.json is required" >&2; exit 2)
	@test -n "$(DISCOVERY)" || (echo "DISCOVERY=private/migrations/proof/discovery.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_transition.py discover "$(PLAN)" --proof "$(DISCOVERY)"

forge-transition-prepare:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/transition.json is required" >&2; exit 2)
	@test -n "$(DISCOVERY)" || (echo "DISCOVERY=private/migrations/proof/discovery.json is required" >&2; exit 2)
	@test -n "$(STATE)" || (echo "STATE=private/migrations/state/transition.json is required" >&2; exit 2)
	@test -n "$(PREPARED)" || (echo "PREPARED=private/migrations/proof/prepared.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_transition.py prepare "$(PLAN)" --discovery "$(DISCOVERY)" --state "$(STATE)" --proof "$(PREPARED)"

forge-transition-verify-shadow:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/transition.json is required" >&2; exit 2)
	@test -n "$(PREPARED)" || (echo "PREPARED=private/migrations/proof/prepared.json is required" >&2; exit 2)
	@test -n "$(STATE)" || (echo "STATE=private/migrations/state/transition.json is required" >&2; exit 2)
	@test -n "$(VERIFICATION)" || (echo "VERIFICATION=private/migrations/proof/shadow-verification.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_transition.py verify-shadow "$(PLAN)" --prepared "$(PREPARED)" --state "$(STATE)" --proof "$(VERIFICATION)" $(if $(WORK_DIR),--work-dir "$(WORK_DIR)",)

forge-transition-enter:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/transition.json is required" >&2; exit 2)
	@test -n "$(VERIFICATION)" || (echo "VERIFICATION=private/migrations/proof/shadow-verification.json is required" >&2; exit 2)
	@test -n "$(STATE)" || (echo "STATE=private/migrations/state/transition.json is required" >&2; exit 2)
	@test -n "$(HANDOVER)" || (echo "HANDOVER=private/migrations/proof/handover.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_transition.py enter "$(PLAN)" --verification "$(VERIFICATION)" --state "$(STATE)" --proof "$(HANDOVER)" $(if $(WORK_DIR),--work-dir "$(WORK_DIR)",)

forge-transition-status:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/transition.json is required" >&2; exit 2)
	@test -n "$(STATE)" || (echo "STATE=private/migrations/state/transition.json is required" >&2; exit 2)
	@test -n "$(PROOF)" || (echo "PROOF=private/migrations/proof/status.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_transition.py status "$(PLAN)" --state "$(STATE)" --proof "$(PROOF)"

forge-transition-reconcile:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/transition.json is required" >&2; exit 2)
	@test -n "$(STATE)" || (echo "STATE=private/migrations/state/transition.json is required" >&2; exit 2)
	@test -n "$(PROOF)" || (echo "PROOF=private/migrations/proof/reconcile.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_transition.py reconcile "$(PLAN)" --state "$(STATE)" --proof "$(PROOF)" $(if $(WORK_DIR),--work-dir "$(WORK_DIR)",)

forge-transition-relay:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/transition.json is required" >&2; exit 2)
	@test -n "$(STATE)" || (echo "STATE=private/migrations/state/transition.json is required" >&2; exit 2)
	@test -n "$(PROOF_DIR)" || (echo "PROOF_DIR=private/migrations/proof/relay is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_transition.py run-relay "$(PLAN)" --state "$(STATE)" --proof-dir "$(PROOF_DIR)" $(if $(INTERVAL),--interval "$(INTERVAL)",) $(if $(filter 1 true yes,$(ONCE)),--once,)

forge-transition-fallback:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/transition.json is required" >&2; exit 2)
	@test -n "$(STATE)" || (echo "STATE=private/migrations/state/transition.json is required" >&2; exit 2)
	@test -n "$(EVIDENCE)" || (echo "EVIDENCE=private/migrations/proof/status-or-handover.json is required" >&2; exit 2)
	@test -n "$(FALLBACK)" || (echo "FALLBACK=private/migrations/proof/fallback.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_transition.py fallback "$(PLAN)" --state "$(STATE)" --evidence "$(EVIDENCE)" --proof "$(FALLBACK)"

forge-transition-finalize:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/transition.json is required" >&2; exit 2)
	@test -n "$(STATE)" || (echo "STATE=private/migrations/state/transition.json is required" >&2; exit 2)
	@test -n "$(EVIDENCE)" || (echo "EVIDENCE=private/migrations/proof/status.json is required" >&2; exit 2)
	@test -n "$(FINALIZATION)" || (echo "FINALIZATION=private/migrations/proof/finalization.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_transition.py finalize "$(PLAN)" --state "$(STATE)" --evidence "$(EVIDENCE)" --proof "$(FINALIZATION)" $(if $(WORK_DIR),--work-dir "$(WORK_DIR)",)

forge-transition-failback:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/transition.json is required" >&2; exit 2)
	@test -n "$(STATE)" || (echo "STATE=private/migrations/state/transition.json is required" >&2; exit 2)
	@test -n "$(EVIDENCE)" || (echo "EVIDENCE=private/migrations/proof/finalization-or-status.json is required" >&2; exit 2)
	@test -n "$(FAILBACK)" || (echo "FAILBACK=private/migrations/proof/failback.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_transition.py failback "$(PLAN)" --state "$(STATE)" --evidence "$(EVIDENCE)" --proof "$(FAILBACK)" $(if $(WORK_DIR),--work-dir "$(WORK_DIR)",)

forge-transition-rollback:
	@test -n "$(PLAN)" || (echo "PLAN=private/migrations/transition.json is required" >&2; exit 2)
	@test -n "$(STATE)" || (echo "STATE=private/migrations/state/transition.json is required" >&2; exit 2)
	@test -n "$(EVIDENCE)" || (echo "EVIDENCE=private/migrations/proof/status-or-handover.json is required" >&2; exit 2)
	@test -n "$(ROLLBACK)" || (echo "ROLLBACK=private/migrations/proof/rollback.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_transition.py rollback "$(PLAN)" --state "$(STATE)" --evidence "$(EVIDENCE)" --proof "$(ROLLBACK)"

forge-transition-proof-verify:
	@test -n "$(PROOF)" || (echo "PROOF=private/migrations/proof/transition-proof.json is required" >&2; exit 2)
	@$(PYTHON) scripts/forge_transition.py verify-proof "$(PROOF)"

no-secrets:
	@$(PYTHON) scripts/validate_no_secrets.py

security-scan:
	@bash scripts/security-scan.sh

supply-chain-posture:
	@bash scripts/supply-chain-posture.sh

supply-chain-verify: security-scan
	@SUPPLY_CHAIN_STRICT=true bash scripts/supply-chain-posture.sh

vendored-chart-provenance-verify:
	@$(PYTHON) scripts/vendored_chart_inventory.py --verify-upstream

rendered-schema-verify:
	@$(PYTHON) scripts/validate_rendered_manifests.py

rendered-private-schema-verify:
	@$(PYTHON) scripts/validate_synthetic_private_profile.py

policy-cel-verify:
	@$(PYTHON) scripts/verify_active_kyverno_policies.py

bootstrap-plan:
	@bash scripts/bootstrap-plan.sh

platform-render-private-values:
	@make_python="$(PYTHON)"; \
		env_file="$${PLATFORM_RENDER_ENV_FILE:-}"; \
		if [[ -z "$${env_file}" && -n "$${PLATFORM_SEED_DEPLOY_ENV_FILE:-}" ]]; then env_file="$${PLATFORM_SEED_DEPLOY_ENV_FILE}"; fi; \
		if [[ -z "$${env_file}" && -n "$${PLATFORM_FIRST_DEPLOY_ENV_FILE:-}" ]]; then env_file="$${PLATFORM_FIRST_DEPLOY_ENV_FILE}"; fi; \
		if [[ -z "$${env_file}" && -f private/seed-git.env ]]; then env_file=private/seed-git.env; fi; \
		if [[ -z "$${env_file}" && -f private/first-deploy.env ]]; then env_file=private/first-deploy.env; fi; \
		if [[ -n "$${env_file}" ]]; then . scripts/bootstrap/load-env-file.sh; load_env_file "$${env_file}" preserve-existing; fi; \
		exec "$${make_python}" scripts/render_private_platform_values.py --inventory inventory/hosts.local.ini

platform-profile-check:
	@$(PYTHON) scripts/check_gitops_profile.py --repo-root . --profile "$${PLATFORM_PROFILE:-premium-3node}" --require-structure

platform-bootstrap:
	@RKE2_VERIFY_API_VIP=false $(MAKE) rke2-verify
	@$(MAKE) rke2-api-vip
	@$(MAKE) rke2-controller-hosts
	@$(MAKE) rke2-verify
	@$(MAKE) platform-argocd
	@$(MAKE) platform-ingress
	@$(MAKE) platform-status

platform-first-deploy:
	@test -n "$${PLATFORM_REPO_URL:-}" || (echo "Set PLATFORM_REPO_URL to the private Git repository URL first." >&2; exit 1)
	@if [ "$${PLATFORM_FIRST_DEPLOY_DNS_REPAIR:-true}" = "true" ]; then $(MAKE) platform-dns-repair; fi
	@if ! PLATFORM_APPLY_GITOPS=true PLATFORM_PROFILE=$${PLATFORM_PROFILE:-premium-3node} PLATFORM_GITOPS_PLACEHOLDER_MODE=$${PLATFORM_GITOPS_PLACEHOLDER_MODE:-skip-incomplete} ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/bootstrap-argocd.yml; then \
		if [ "$${PLATFORM_FIRST_DEPLOY_ARGOCD_REPAIR_RETRY:-true}" = "true" ]; then \
			echo "Argo CD bootstrap failed; running automatic DNS/ClusterIP service-path repair and retrying once."; \
			$(MAKE) platform-dns-repair; \
			$(MAKE) platform-argocd-service-repair || true; \
			PLATFORM_APPLY_GITOPS=true PLATFORM_PROFILE=$${PLATFORM_PROFILE:-premium-3node} PLATFORM_GITOPS_PLACEHOLDER_MODE=$${PLATFORM_GITOPS_PLACEHOLDER_MODE:-skip-incomplete} ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/bootstrap-argocd.yml; \
		else \
			exit 1; \
		fi; \
	fi
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/expose-argocd-bootstrap.yml
	@$(MAKE) platform-ingress
	@$(MAKE) platform-status

platform-first-deploy-auto:
	@bash scripts/bootstrap/private-first-deploy.sh

platform-first-deploy-seed:
	@bash scripts/bootstrap/seed-first-deploy.sh

platform-seed-git:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/deploy-seed-git.yml

platform-seed-git-sync:
	@bash scripts/bootstrap/sync-seed-git.sh

platform-seed-git-remove:
	@PLATFORM_SEED_GIT_STATE=absent ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/deploy-seed-git.yml

platform-argocd:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/bootstrap-argocd.yml
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/expose-argocd-bootstrap.yml

platform-argocd-core:
	@PLATFORM_ARGOCD_BOOTSTRAP_MODE=core ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/bootstrap-argocd.yml
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/expose-argocd-bootstrap.yml

platform-argocd-ha:
	@PLATFORM_ARGOCD_BOOTSTRAP_MODE=ha PLATFORM_ARGOCD_AUTO_CORE_FALLBACK=false ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/bootstrap-argocd.yml
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/expose-argocd-bootstrap.yml

platform-argocd-expose:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/expose-argocd-bootstrap.yml

platform-argocd-unexpose:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/expose-argocd-bootstrap.yml -e platform_argocd_bootstrap_exposure_state=absent

platform-argocd-diagnose:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/diagnose-argocd.yml

platform-argocd-service-repair:
	@$(MAKE) platform-dns-repair
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/repair-argocd-service-path.yml

platform-app-secrets:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/configure-platform-app-secrets.yml

platform-app-health:
	@bash scripts/bootstrap/run-platform-app-health.sh

platform-ci-health:
	@PLATFORM_APP_HEALTH_REQUIRED_APPS="traefik woodpecker" \
	PLATFORM_APP_HEALTH_INCLUDE_EXISTING_APPS=false \
	PLATFORM_APP_HEALTH_FORBID_TEMPORARY_REPO=false \
	PLATFORM_APP_HEALTH_NAMESPACES="argocd traefik woodpecker" \
	PLATFORM_APP_HEALTH_GUI_APPS="argocd woodpecker" \
	PLATFORM_APP_HEALTH_DISCOVER_LIVE_HOSTS=true \
	PLATFORM_APP_HEALTH_STORAGE_CLASSES=skip \
	PLATFORM_APP_HEALTH_ARGOCD_RUNTIME=true \
	PLATFORM_APP_HEALTH_LONGHORN_RUNTIME=false \
	PLATFORM_APP_HEALTH_CNPG_CLUSTERS=skip \
	PLATFORM_APP_HEALTH_SSO=false \
	PLATFORM_APP_HEALTH_STEP_CA_API=false \
	PLATFORM_APP_HEALTH_REGISTRY_API=false \
	PLATFORM_APP_HEALTH_MONITORING_API=false \
	PLATFORM_APP_HEALTH_LOKI_API=false \
	PLATFORM_APP_HEALTH_VELERO_BACKUP_STORAGE=false \
	PLATFORM_APP_HEALTH_VELERO_SCHEDULES=false \
	ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} \
	bash scripts/bootstrap/run-platform-app-health.sh

platform-woodpecker-repair:
	@PLATFORM_NODE_STORAGE_PRESSURE_ONLY=true \
		PLATFORM_NODE_STORAGE_WAIT_FOR_PRESSURE_CLEAR=true \
		PLATFORM_NODE_STORAGE_CRI_PRUNE=true \
		PLATFORM_NODE_STORAGE_DOCKER_PRUNE=true \
		PLATFORM_NODE_STORAGE_GITLAB_RUNNER_CACHE_PRUNE=true \
		PLATFORM_NODE_STORAGE_LONGHORN_TRIM=true \
		PLATFORM_NODE_STORAGE_LONGHORN_PRESSURE_EVICTION=true \
		$(MAKE) platform-node-storage-cleanup
	@bash scripts/bootstrap/run-woodpecker-secret-reconcile.sh
	@bash scripts/bootstrap/reconcile-woodpecker-gitops-source.sh
	@$(MAKE) platform-argocd-service-repair
	@set -o pipefail; \
		repair_log="$$(mktemp)"; \
		trap 'rm -f "$$repair_log"' EXIT; \
		set +e; \
		ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/repair-woodpecker.yml 2>&1 | tee "$$repair_log"; \
		repair_rc="$${PIPESTATUS[0]}"; \
		set -e; \
		if [ "$$repair_rc" -eq 0 ]; then \
			exit 0; \
		fi; \
		service_path_repair=false; \
		longhorn_runtime_repair=false; \
		application_config_repair=false; \
		forgejo_ingress_repair=false; \
		forgejo_tls_self_signed=false; \
		woodpecker_forgejo_url_repair=false; \
		scheduling_capacity=false; \
		longhorn_bootstrap_ran=false; \
		if grep -Eq 'reason=postgres-endpoint-path-unreachable|cnpg-webhook-service.*(i/o timeout|context deadline exceeded|connection refused)|failed calling webhook.*(cnpg|mcluster)|mcluster\.cnpg\.io.*context deadline exceeded|Instance Status Extraction Error: HTTP communication issue|:8000/(readyz|healthz|startupz).*(i/o timeout|context deadline exceeded|Client\.Timeout exceeded)' "$$repair_log"; then \
			service_path_repair=true; \
		fi; \
		if grep -Fq 'reason=woodpecker-server-replica-volume-not-ready' "$$repair_log" || \
			grep -Eq 'driver name driver\.longhorn\.io not found in the list of registered CSI drivers|CSINode .* does not contain driver driver\.longhorn\.io|MountVolume\.(MountDevice|SetUp) failed.*driver\.longhorn\.io|AttachVolume\.Attach failed.*volume .*not ready for workloads|FailedAttachVolume.*unable to attach volume .*node .* is not ready|DeadlineExceeded desc = volume .* failed to attach|VolumeBinding.*binding volumes: context deadline exceeded|reason=longhorn-csi-(plugin|registration)|DiskFilesystemChanged' "$$repair_log"; then \
			longhorn_runtime_repair=true; \
		fi; \
		if grep -Eq 'reason=woodpecker-postgres-ca-(bundle|mount|file|controller|container|source)-missing|open /etc/ssl/platform-postgres/ca-certificates\\.crt: no such file or directory' "$$repair_log"; then \
			application_config_repair=true; \
		fi; \
		if grep -Fq 'reason=forgejo-oauth-tls-chain-self-signed' "$$repair_log"; then \
			forgejo_tls_self_signed=true; \
		fi; \
		if grep -Eq 'reason=forgejo-ingress-tls-(secret|material)-missing|reason=forgejo-route-hosts-ambiguous|forgejo-oauth-tls-chain-(self-signed|untrusted|did-not-converge)' "$$repair_log"; then \
			forgejo_ingress_repair=true; \
		fi; \
		if grep -Fq 'reason=woodpecker-forgejo-url-route-drift' "$$repair_log"; then \
			application_config_repair=true; \
			woodpecker_forgejo_url_repair=true; \
		fi; \
		if grep -Eq 'reason=woodpecker-scheduling-capacity-insufficient|reason=woodpecker-scheduling-blocked-by-node-taint' "$$repair_log"; then \
			scheduling_capacity=true; \
		fi; \
		echo "Woodpecker prerequisite classification: service_path=$$service_path_repair longhorn_runtime=$$longhorn_runtime_repair application_config=$$application_config_repair forgejo_ingress=$$forgejo_ingress_repair forgejo_tls_self_signed=$$forgejo_tls_self_signed scheduling_capacity=$$scheduling_capacity"; \
		if [ "$$service_path_repair" != "true" ] && [ "$$longhorn_runtime_repair" != "true" ] && [ "$$application_config_repair" != "true" ] && [ "$$forgejo_ingress_repair" != "true" ] && [ "$$scheduling_capacity" != "true" ]; then \
			echo "Woodpecker repair failed without a recognized PostgreSQL service-path, Longhorn CSI, application configuration, Forgejo ingress, or scheduling-capacity classification; automatic fallback skipped." >&2; \
			exit "$$repair_rc"; \
		fi; \
		if [ "$$scheduling_capacity" = "true" ] && [ "$$service_path_repair" != "true" ] && [ "$$longhorn_runtime_repair" != "true" ] && [ "$$application_config_repair" != "true" ] && [ "$$forgejo_ingress_repair" != "true" ]; then \
			echo "Woodpecker remains unschedulable after guarded disk-pressure cleanup. Add allocatable CPU or remove the reported non-DiskPressure taint; automatic repair will not reduce three-node HA or weaken hard topology spread." >&2; \
			exit "$$repair_rc"; \
		fi; \
		if [ "$$woodpecker_forgejo_url_repair" = "true" ]; then \
			echo "Woodpecker's Forgejo OAuth URL drifted from the GitOps-owned route; re-rendering the private Woodpecker source and waiting for Argo CD reconciliation."; \
			bash scripts/bootstrap/reconcile-woodpecker-gitops-source.sh; \
			$(MAKE) platform-argocd-service-repair; \
		elif [ "$$application_config_repair" = "true" ]; then \
			echo "Woodpecker PostgreSQL trust-bundle configuration failed; refreshing the bundle and repairing the server mount before retry."; \
		fi; \
		if [ "$$forgejo_ingress_repair" = "true" ]; then \
			if [ "$$forgejo_tls_self_signed" = "true" ]; then \
				echo "Forgejo is serving a self-signed wildcard certificate; repairing the backend, but the public certificate must be replaced before Woodpecker OAuth can pass."; \
			else \
				echo "Forgejo's HTTPS route or TLS binding failed; repairing Forgejo's backend and trust contract before applying the canonical Forgejo ingress contract."; \
			fi; \
			$(MAKE) platform-forgejo-runtime-repair; \
			if [ "$$forgejo_tls_self_signed" = "true" ]; then \
				echo "Install a CA-signed wildcard certificate with PLATFORM_WILDCARD_TLS_CERT_FILE and PLATFORM_WILDCARD_TLS_KEY_FILE, run make platform-tls, then rerun make platform-woodpecker-repair." >&2; \
				exit "$$repair_rc"; \
			fi; \
			$(MAKE) platform-forgejo-ingress; \
		fi; \
		if [ "$$service_path_repair" = "true" ]; then \
			echo "Woodpecker PostgreSQL or CloudNativePG service path failed; applying all-node CNI/firewalld recovery."; \
			PLATFORM_DNS_SERVICE_PATH_REPAIR=true PLATFORM_DNS_FORCE_SERVICE_PATH_REPAIR=true $(MAKE) platform-dns-repair; \
		fi; \
		if [ "$$longhorn_runtime_repair" = "true" ]; then \
			echo "Longhorn CSI registration, attach readiness, or duplicate-disk state failed; applying focused Longhorn runtime recovery."; \
			set +e; \
			PLATFORM_LONGHORN_RUNTIME_FORCE_RESTART=true $(MAKE) platform-longhorn-runtime-repair; \
			longhorn_runtime_rc="$$?"; \
			set -e; \
			if [ "$$longhorn_runtime_rc" -ne 0 ]; then \
				echo "Focused Longhorn runtime recovery failed; escalating to guarded Longhorn disk bootstrap."; \
				$(MAKE) platform-longhorn-bootstrap; \
				longhorn_bootstrap_ran=true; \
			fi; \
		fi; \
		echo "Retrying Woodpecker repair once after classified prerequisite recovery."; \
		: > "$$repair_log"; \
		set +e; \
		ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/repair-woodpecker.yml 2>&1 | tee "$$repair_log"; \
		retry_rc="$${PIPESTATUS[0]}"; \
		set -e; \
		if [ "$$retry_rc" -eq 0 ]; then \
			exit 0; \
		fi; \
		if [ "$$longhorn_runtime_repair" = "true" ] && \
			grep -Eq 'reason=woodpecker-server-replica-volume-not-ready|AttachVolume\.Attach failed.*volume .*not ready for workloads|CSINode .* does not contain driver driver\.longhorn\.io|FailedAttachVolume.*unable to attach volume .*node .* is not ready|DeadlineExceeded desc = volume .* failed to attach|actualSize=0.*robustness=faulted' "$$repair_log"; then \
			if [ "$$longhorn_bootstrap_ran" != "true" ]; then \
				echo "A replacement zero-byte Woodpecker volume is still faulted after runtime recovery; applying guarded Longhorn disk bootstrap."; \
				$(MAKE) platform-longhorn-bootstrap; \
				longhorn_bootstrap_ran=true; \
			fi; \
			echo "Retrying Woodpecker repair after Longhorn disk bootstrap."; \
			ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/repair-woodpecker.yml; \
			exit $$?; \
		fi; \
		if [ "$$service_path_repair" = "true" ] && grep -q 'reason=postgres-endpoint-path-unreachable' "$$repair_log"; then \
			echo "Cross-node PostgreSQL endpoint access still fails after CNI/firewalld recovery; checking for the documented Cilium VXLAN remote-ICMP-success/TCP-timeout condition."; \
			ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/repair-cilium-vxlan-overlay.yml; \
			echo "Retrying Woodpecker repair after guarded Cilium overlay recovery."; \
			: > "$$repair_log"; \
			set +e; \
			ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/repair-woodpecker.yml 2>&1 | tee "$$repair_log"; \
			overlay_retry_rc="$${PIPESTATUS[0]}"; \
			set -e; \
			if [ "$$overlay_retry_rc" -eq 0 ]; then \
				exit 0; \
			fi; \
			if ! grep -q 'reason=postgres-endpoint-path-unreachable' "$$repair_log"; then \
				exit "$$overlay_retry_rc"; \
			fi; \
			echo "Cross-node PostgreSQL endpoint access still fails after Cilium overlay recovery; applying guarded rolling RKE2 restart on only the source and endpoint nodes."; \
			ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/repair-woodpecker-service-path-nodes.yml; \
			echo "Retrying Woodpecker repair after guarded rolling node recovery."; \
			ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/repair-woodpecker.yml; \
			exit $$?; \
		fi; \
		if grep -Eq 'reason=woodpecker-scheduling-capacity-insufficient|reason=woodpecker-scheduling-blocked-by-node-taint' "$$repair_log"; then \
			echo "Woodpecker is still unschedulable after classified prerequisite recovery. Add node capacity or remove the reported non-DiskPressure taint; three-node HA and hard topology spread were preserved." >&2; \
		fi; \
		exit "$$retry_rc"
	@$(MAKE) platform-service-path-consumers-repair
	@$(MAKE) platform-ci-health

platform-monitoring-health:
	@PLATFORM_APP_HEALTH_REQUIRED_APPS="traefik monitoring" \
	PLATFORM_APP_HEALTH_INCLUDE_EXISTING_APPS=false \
	PLATFORM_APP_HEALTH_FORBID_TEMPORARY_REPO=false \
	PLATFORM_APP_HEALTH_NAMESPACES="argocd traefik monitoring" \
	PLATFORM_APP_HEALTH_GUI_APPS="grafana prometheus" \
	PLATFORM_APP_HEALTH_STORAGE_CLASSES=skip \
	PLATFORM_APP_HEALTH_LONGHORN_RUNTIME=false \
	PLATFORM_APP_HEALTH_CNPG_CLUSTERS=skip \
	PLATFORM_APP_HEALTH_STEP_CA_API=false \
	PLATFORM_APP_HEALTH_REGISTRY_API=false \
	PLATFORM_APP_HEALTH_LOKI_API=false \
	PLATFORM_APP_HEALTH_VELERO_BACKUP_STORAGE=false \
	PLATFORM_APP_HEALTH_VELERO_SCHEDULES=false \
	ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} \
	ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/verify-platform-app-health.yml

platform-monitoring-repair:
	@$(MAKE) platform-dns-repair
	@$(MAKE) platform-argocd-service-repair
	@$(MAKE) platform-longhorn-bootstrap
	@$(MAKE) platform-app-secrets
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/repair-monitoring.yml
	@$(MAKE) platform-monitoring-health

platform-tls:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/manage-platform-tls.yml

platform-tls-verify:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/verify-platform-tls.yml

platform-data-protection:
	@bash scripts/bootstrap/run-platform-data-protection.sh

platform-policy-readiness:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/verify-platform-policy-readiness.yml

platform-network-isolation-verify:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/verify-platform-network-isolation.yml

platform-internal-tls-verify:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/verify-platform-internal-tls.yml

platform-openbao-status:
	@PLATFORM_OPENBAO_VERIFY_STRICT=false ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/verify-openbao.yml

platform-openbao-verify:
	@PLATFORM_OPENBAO_VERIFY_STRICT=true ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/verify-openbao.yml

platform-openbao-ceremony-digest:
	@$(PYTHON) scripts/verify_openbao_ceremony_evidence.py --print-configuration-sha256 --expected-profile "$${PLATFORM_PROFILE:-premium-3node}"

platform-openbao-ceremony-evidence-verify:
	@test -n "$(EVIDENCE)" || (echo "EVIDENCE=/path/to/openbao-ceremony.json is required" >&2; exit 2)
	@$(PYTHON) scripts/verify_openbao_ceremony_evidence.py "$(EVIDENCE)"

platform-observability-verify:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/verify-platform-observability.yml

platform-capacity-verify:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/verify-platform-capacity.yml

platform-image-inventory-verify: rendered-schema-verify rendered-private-schema-verify supply-chain-verify
	@bash scripts/bootstrap/run-platform-image-inventory.sh

platform-production-evidence:
	@bash scripts/bootstrap/run-platform-production-evidence.sh

platform-production-score:
	@bash scripts/bootstrap/run-platform-production-score.sh

platform-production-check: validate
	@bash scripts/bootstrap/run-platform-production-check.sh

platform-longhorn-bootstrap:
	@bash scripts/bootstrap/run-longhorn-bootstrap.sh

platform-longhorn-runtime-repair:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/repair-longhorn-runtime.yml

platform-longhorn-crd-repair:
	@bash scripts/bootstrap/run-longhorn-crd-repair.sh

platform-forgejo-diagnose: platform-inventory-preflight
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/diagnose-forgejo.yml

platform-forgejo-repair: platform-inventory-preflight
	@PLATFORM_NODE_STORAGE_PRESSURE_ONLY=true \
		PLATFORM_NODE_STORAGE_WAIT_FOR_PRESSURE_CLEAR=true \
		PLATFORM_NODE_STORAGE_CRI_PRUNE=true \
		PLATFORM_NODE_STORAGE_DOCKER_PRUNE=true \
		PLATFORM_NODE_STORAGE_GITLAB_RUNNER_CACHE_PRUNE=true \
		PLATFORM_NODE_STORAGE_LONGHORN_TRIM=true \
		PLATFORM_NODE_STORAGE_LONGHORN_PRESSURE_EVICTION=true \
		$(MAKE) platform-node-storage-cleanup
	@$(MAKE) platform-longhorn-runtime-repair
	@$(MAKE) platform-forgejo-storage-repair
	@$(MAKE) platform-forgejo-runtime-repair
	@$(MAKE) platform-forgejo-ingress

platform-forgejo-runtime-repair: platform-inventory-preflight
	@bash scripts/bootstrap/run-forgejo-runtime-repair.sh

platform-forgejo-storage-repair: platform-inventory-preflight
	@bash scripts/bootstrap/run-forgejo-storage-repair.sh

platform-forgejo-ingress: platform-inventory-preflight
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/publish-forgejo-ingress.yml

platform-forgejo-recovery-drill: platform-inventory-preflight
	@bash scripts/bootstrap/run-forgejo-recovery-drill.sh

platform-dns-repair:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/repair-cluster-dns.yml

platform-service-path-consumers-repair:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/repair-platform-service-path-consumers.yml

platform-service-path-repair:
	@$(MAKE) platform-dns-repair
	@$(MAKE) platform-service-path-consumers-repair

platform-dns-repair-traefik:
	@PLATFORM_DNS_CHECK_REPO=$${PLATFORM_TRAEFIK_CHART_REPO:-https://traefik.github.io/charts} ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/repair-cluster-dns.yml

platform-ingress:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/deploy-platform-ingress.yml

platform-ingress-vip: platform-ingress

platform-ingress-diagnose:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/diagnose-platform-ingress.yml

platform-status:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/platform-status.yml

rke2-preflight: platform-inventory-preflight
	@ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/preflight.yml

rke2-controller-hosts:
	@ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/preflight.yml -e manage_controller_hosts=true

rke2-prepare: rke2-preflight
	@ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/prepare-nodes.yml

rke2-registry-check:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/rke2-registry-check.yml $(if $(HOST),--limit $(HOST),)

rke2-api-vip:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/deploy-kube-vip.yml

rke2-install: rke2-prepare
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/install-rke2.yml

rke2-recover: rke2-prepare
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/recover-rke2.yml
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/verify-rke2.yml

rke2-reset:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/reset-rke2.yml

rke2-verify:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/verify-rke2.yml

rke2-diagnose:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/diagnose-rke2.yml $(if $(HOST),--limit $(HOST),)

rke2-status:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/rke2-status.yml $(if $(HOST),--limit $(HOST),)

rke2-cleanup-installers:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/rke2-cleanup-installers.yml $(if $(HOST),--limit $(HOST),)

platform-inventory-preflight:
	@$(PYTHON) scripts/prepare_local_inventory.py --inventory inventory/hosts.local.ini

platform-node-storage-diagnose: platform-inventory-preflight
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-60} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/cleanup-node-storage.yml $(if $(HOST),--limit $(HOST),)

platform-node-storage-cleanup: platform-inventory-preflight
	@PLATFORM_NODE_STORAGE_CLEANUP=true ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-60} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/cleanup-node-storage.yml $(if $(HOST),--limit $(HOST),)

rke2-network-check:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/rke2-network-check.yml

rke2-ping:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-10} ansible -i inventory/hosts.local.ini $(if $(HOST),$(HOST),all) -m ping -T $${ANSIBLE_TIMEOUT:-10}

docs-list:
	@find docs -maxdepth 2 -type f | sort

ci-list:
	@printf '%s\n' ".github/workflows/validate.yml" ".gitea/workflows/validate.yml" ".forgejo/workflows/validate.yml" ".gitlab-ci.yml" ".woodpecker/validate.yml"
