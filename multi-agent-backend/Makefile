SHELL := /bin/bash
.DEFAULT_GOAL := help
COMPOSE := docker compose
BACKEND := backend

.PHONY: help dev down reset logs ps migrate migration-autogenerate test test-local \
        eval eval-local lint lint-local fmt shell clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'

dev: ## Build and start the full local stack (detached)
	$(COMPOSE) up -d --build
	@echo ""
	@echo "  Open WebUI   -> http://localhost:3000   (pick a model: analytics / monitor / research / simulate)"
	@echo "  Backend API  -> http://localhost:8000/docs"
	@echo "  LiteLLM      -> http://localhost:4000"
	@echo "  n8n          -> http://localhost:5678"

down: ## Stop the stack
	$(COMPOSE) down

reset: ## Stop the stack and delete all volumes (destructive)
	$(COMPOSE) down -v

logs: ## Tail backend logs
	$(COMPOSE) logs -f backend

ps: ## Show service status
	$(COMPOSE) ps

migrate: ## Apply Alembic migrations inside the backend container
	$(COMPOSE) exec -T backend uv run --no-sync alembic upgrade head

migration-autogenerate: ## Autogenerate a migration: make migration-autogenerate m="add table"
	$(COMPOSE) exec -T backend uv run --no-sync alembic revision --autogenerate -m "$(m)"

test: ## Run the test suite inside the backend container
	$(COMPOSE) exec -T backend uv run --no-sync python -m pytest -q

test-local: ## Run the test suite locally with uv (no Docker / DB required)
	cd $(BACKEND) && uv run python -m pytest

eval: ## Run all evaluation suites inside the backend container
	$(COMPOSE) exec -T backend uv run --no-sync python eval_harness.py --all

eval-local: ## Run all evaluation suites locally with uv
	cd $(BACKEND) && uv run python eval_harness.py --all

lint: ## Lint + format check (ruff)
	cd $(BACKEND) && uv run ruff check . && uv run ruff format --check .

lint-local: ## Alias of lint (runs locally by design)
	cd $(BACKEND) && uv run ruff check . && uv run ruff format --check .

fmt: ## Auto-fix and format the backend (ruff)
	cd $(BACKEND) && uv run ruff check --fix . && uv run ruff format .

shell: ## Open a shell in the backend container
	$(COMPOSE) exec backend bash

clean: ## Remove local build artifacts and caches
	cd $(BACKEND) && rm -rf .pytest_cache .ruff_cache data .venv
