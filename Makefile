PYTHON ?= python3
BASH ?= bash
BASE ?= origin/main
HEAD ?= HEAD
CONFIRM_TEXT ?=

.DEFAULT_GOAL := help

.PHONY: help setup doctor up status smoke down verify lint verify-local images push scan \
        configure bootstrap gitops-status gitops-wait gitops-uninstall drift-test \
        promote rollback promote-status promote-gate

help: ## Show the available targets.
	@echo "EKS GitOps Reliability Platform"
	@echo "  make setup         Install pinned development checks"
	@echo "  make doctor        Check local runtime prerequisites"
	@echo "  make up            Create the registry and both kind clusters"
	@echo "  make status        Report the local platform state"
	@echo "  make smoke         Check registry and cluster readiness"
	@echo "  make down          Remove only project-owned local resources"
	@echo "  make verify        Run credential-free repository checks"
	@echo "  make lint          Run all pinned pre-commit hooks"
	@echo "  make verify-local  Exercise idempotent up/down behavior"
	@echo "  make images        Build the four service images locally"
	@echo "  make push          Build and push images with SBOM and provenance"
	@echo "  make scan          Scan the built images for vulnerabilities"
	@echo "  make configure     Set the Git repository URL (REPO_URL=...)"
	@echo "  make bootstrap     Install Argo CD and apply the root Application"
	@echo "  make gitops-status Report Argo CD Application sync and health"
	@echo "  make drift-test    Prove out-of-band changes are reverted"
	@echo "  make promote       Write digest pins (ENV=dev|production)"
	@echo "  make rollback      Restore the previous digest set (ENV=...)"
	@echo "  make promote-status Show the current overlay pins (ENV=...)"

setup: ## Install pinned developer dependencies into the active Python environment.
	$(PYTHON) -m pip install --requirement requirements-dev.txt

doctor: ## Check Docker, kind, and kubectl without changing state.
	$(BASH) scripts/platform.sh doctor

up: ## Idempotently create the local registry and two kind clusters.
	$(BASH) scripts/platform.sh up

status: ## Display project-owned local resources.
	$(BASH) scripts/platform.sh status

smoke: ## Verify that the registry and both cluster nodes are ready.
	$(BASH) scripts/platform.sh smoke

down: ## Idempotently remove only project-owned local resources.
	$(BASH) scripts/platform.sh down

verify: ## Run static checks and unit tests; Docker and cloud credentials are not required.
	$(PYTHON) scripts/verify.py

lint: ## Run pinned formatting, linting, and secret-detection hooks.
	$(PYTHON) -m pre_commit run --all-files --show-diff-on-failure

configure: ## Set the Git repository URL Argo CD reconciles from (REPO_URL=...).
	@test -n "$(REPO_URL)" || { echo "usage: make configure REPO_URL=<git url>" >&2; exit 1; }
	$(PYTHON) scripts/gitops.py configure --repo-url "$(REPO_URL)"

bootstrap: ## Install Argo CD, register the production cluster, apply the root app.
	$(PYTHON) scripts/gitops.py bootstrap

gitops-status: ## Report Argo CD Application sync and health.
	$(PYTHON) scripts/gitops.py status

gitops-wait: ## Block until every Application is Synced and Healthy.
	$(PYTHON) scripts/gitops.py wait

gitops-uninstall: ## Remove Argo CD from the control-plane cluster.
	$(PYTHON) scripts/gitops.py uninstall

drift-test: ## Inject drift into the dev environment and require Argo CD to revert it.
	$(PYTHON) scripts/drift.py

promote: ## Write digest pins into ENV overlay. Production needs CONFIRM=production FROM_ENV=dev.
	@test -n "$(ENV)" || { echo "usage: make promote ENV=dev|production [FROM=artifacts/images.json] [FROM_ENV=dev] [CONFIRM=production]" >&2; exit 1; }
	$(PYTHON) scripts/promote.py apply --environment "$(ENV)" \
		$(if $(FROM),--from "$(FROM)") \
		$(if $(FROM_ENV),--from-environment "$(FROM_ENV)") \
		$(if $(CONFIRM),--confirm "$(CONFIRM)")

rollback: ## Restore the previous recorded digest set for ENV.
	@test -n "$(ENV)" || { echo "usage: make rollback ENV=dev|production [CONFIRM=production]" >&2; exit 1; }
	$(PYTHON) scripts/promote.py rollback --environment "$(ENV)" \
		$(if $(CONFIRM),--confirm "$(CONFIRM)")

promote-status: ## Show overlay pins without changing them.
	@test -n "$(ENV)" || { echo "usage: make promote-status ENV=dev|production" >&2; exit 1; }
	$(PYTHON) scripts/promote.py status --environment "$(ENV)"

promote-gate: ## Require PROMOTION-CONFIRM when production pins change (used by CI).
	$(PYTHON) scripts/promote.py gate --base "$(BASE)" --head "$(HEAD)" --confirm-text "$(CONFIRM_TEXT)"

images: ## Build all service images into the local Docker image store.
	$(PYTHON) scripts/build.py

push: ## Build and push images to the local registry with SBOM and provenance.
	$(PYTHON) scripts/build.py --push

scan: ## Fail on fixable HIGH or CRITICAL vulnerabilities in the built images.
	$(PYTHON) scripts/scan.py

verify-local: ## Prove repeated up/down calls are safe; always attempt cleanup.
	@set -eu; \
	trap '$(BASH) scripts/platform.sh down' EXIT INT TERM; \
	$(BASH) scripts/platform.sh up; \
	$(BASH) scripts/platform.sh up; \
	$(BASH) scripts/platform.sh smoke; \
	$(BASH) scripts/platform.sh down; \
	$(BASH) scripts/platform.sh down; \
	trap - EXIT INT TERM
