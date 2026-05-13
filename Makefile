SHELL := /usr/bin/env bash

.PHONY: help init-local validate no-secrets bootstrap-plan docs-list ci-list

help:
	@echo "Platform GitOps Workspace"
	@echo ""
	@echo "Targets:"
	@echo "  init-local      Create ignored local config files from examples"
	@echo "  validate        Run repository validation"
	@echo "  no-secrets      Scan repository for obvious secrets/private data"
	@echo "  bootstrap-plan  Print recommended bootstrap order"
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

docs-list:
	@find docs -maxdepth 2 -type f | sort

ci-list:
	@printf '%s\n' ".github/workflows/validate.yml" ".gitea/workflows/validate.yml" ".forgejo/workflows/validate.yml" ".gitlab-ci.yml" ".woodpecker/validate.yml"
