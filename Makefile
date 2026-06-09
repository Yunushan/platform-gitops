SHELL := /usr/bin/env bash

.PHONY: help init-local validate no-secrets bootstrap-plan rke2-preflight rke2-prepare rke2-install rke2-recover rke2-reset rke2-verify rke2-diagnose rke2-status rke2-cleanup-installers rke2-network-check rke2-ping docs-list ci-list

help:
	@echo "Platform GitOps Workspace"
	@echo ""
	@echo "Targets:"
	@echo "  init-local      Create ignored local config files from examples"
	@echo "  validate        Run repository validation"
	@echo "  no-secrets      Scan repository for obvious secrets/private data"
	@echo "  bootstrap-plan  Print recommended bootstrap order"
	@echo "  rke2-preflight  Check Ansible SSH/sudo and write node /etc/hosts"
	@echo "  rke2-prepare    Prepare Linux nodes through Ansible"
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

rke2-preflight:
	@ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/preflight.yml

rke2-prepare: rke2-preflight
	@ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/prepare-nodes.yml

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
