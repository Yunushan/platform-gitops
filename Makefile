SHELL := /usr/bin/env bash

.PHONY: help init-local validate no-secrets bootstrap-plan platform-render-private-values platform-bootstrap platform-first-deploy platform-first-deploy-auto platform-first-deploy-seed platform-seed-git platform-seed-git-remove platform-argocd platform-argocd-core platform-argocd-ha platform-argocd-expose platform-argocd-unexpose platform-argocd-diagnose platform-argocd-service-repair platform-longhorn-bootstrap platform-forgejo-diagnose platform-dns-repair platform-dns-repair-traefik platform-ingress platform-ingress-vip platform-ingress-diagnose platform-status rke2-preflight rke2-controller-hosts rke2-prepare rke2-registry-check rke2-api-vip rke2-install rke2-recover rke2-reset rke2-verify rke2-diagnose rke2-status rke2-cleanup-installers rke2-network-check rke2-ping docs-list ci-list

help:
	@echo "Platform GitOps Workspace"
	@echo ""
	@echo "Targets:"
	@echo "  init-local      Create ignored local config files from examples"
	@echo "  validate        Run repository validation"
	@echo "  no-secrets      Scan repository for obvious secrets/private data"
	@echo "  bootstrap-plan  Print recommended bootstrap order"
	@echo "  platform-render-private-values  Render first-deploy private values for Forgejo/Longhorn from env or inventory"
	@echo "  platform-bootstrap  Verify RKE2/API VIP, bootstrap Argo CD, configure app VIP when ready, and print access report"
	@echo "  platform-first-deploy  First private GitOps deploy: bootstrap Argo CD, register repo credentials, publish ingress, and print status"
	@echo "  platform-first-deploy-auto  Non-interactive first private deploy using private/first-deploy.env or exported variables"
	@echo "  platform-first-deploy-seed  First deploy with no previous Git server by using temporary internal seed Git"
	@echo "  platform-seed-git  Create temporary internal read-only seed Git service on the first RKE2 node"
	@echo "  platform-seed-git-remove  Remove temporary internal seed Git service"
	@echo "  platform-argocd  Bootstrap Argo CD; set PLATFORM_APPLY_GITOPS=true and PLATFORM_REPO_URL to register apps"
	@echo "  platform-argocd-core  Bootstrap standard Argo CD and clean stale HA Redis bootstrap resources"
	@echo "  platform-argocd-ha  Bootstrap Argo CD HA explicitly"
	@echo "  platform-argocd-expose  Expose Argo CD through a bootstrap NodePort, default HTTPS port 30443"
	@echo "  platform-argocd-unexpose  Remove the bootstrap Argo CD NodePort exposure"
	@echo "  platform-argocd-diagnose  Show Argo CD rollout, image, event, log, and registry diagnostics"
	@echo "  platform-argocd-service-repair  Repair Argo CD internal repo-server/Redis service reachability"
	@echo "  platform-longhorn-bootstrap  Bootstrap Longhorn storage, pre-pull images on nodes, and verify CSI/PVC readiness"
	@echo "  platform-forgejo-diagnose  Show Forgejo init, logs, PVC/PV, Longhorn volume, service, and ingress diagnostics"
	@echo "  platform-dns-repair  Verify pod DNS and repair CoreDNS upstreams for external chart repositories"
	@echo "  platform-dns-repair-traefik  Verify pod DNS against the Traefik chart repository"
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
	@python3 scripts/validate_project.py
	@python3 scripts/validate_no_secrets.py

no-secrets:
	@python3 scripts/validate_no_secrets.py

bootstrap-plan:
	@bash scripts/bootstrap-plan.sh

platform-render-private-values:
	@python3 scripts/render_private_platform_values.py --inventory inventory/hosts.local.ini

platform-bootstrap: rke2-verify rke2-api-vip rke2-controller-hosts platform-argocd platform-ingress platform-status

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
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/repair-argocd-service-path.yml

platform-longhorn-bootstrap:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/bootstrap-longhorn.yml

platform-forgejo-diagnose:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/diagnose-forgejo.yml

platform-dns-repair:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/repair-cluster-dns.yml

platform-dns-repair-traefik:
	@PLATFORM_DNS_CHECK_REPO=$${PLATFORM_TRAEFIK_CHART_REPO:-https://traefik.github.io/charts} ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/repair-cluster-dns.yml

platform-ingress: platform-dns-repair platform-dns-repair-traefik
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/deploy-platform-ingress.yml

platform-ingress-vip: platform-ingress

platform-ingress-diagnose:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/diagnose-platform-ingress.yml

platform-status:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/platform-status.yml

rke2-preflight:
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

rke2-network-check:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-20} ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/rke2-network-check.yml

rke2-ping:
	@ANSIBLE_TIMEOUT=$${ANSIBLE_TIMEOUT:-10} ansible -i inventory/hosts.local.ini $(if $(HOST),$(HOST),all) -m ping -T $${ANSIBLE_TIMEOUT:-10}

docs-list:
	@find docs -maxdepth 2 -type f | sort

ci-list:
	@printf '%s\n' ".github/workflows/validate.yml" ".gitea/workflows/validate.yml" ".forgejo/workflows/validate.yml" ".gitlab-ci.yml" ".woodpecker/validate.yml"
