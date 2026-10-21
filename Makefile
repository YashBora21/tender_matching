# Tender Requirement Matching - developer commands.
# Run `make help` for the list.

VENV := venv
PY := $(VENV)/Scripts/python.exe
PIP := $(VENV)/Scripts/python.exe -m pip
COMPOSE := docker compose

.DEFAULT_GOAL := help
.PHONY: help install install-dev migrate downgrade run test test-unit test-integration \
        docker-build docker-up docker-down docker-logs clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install runtime dependencies
	$(PIP) install -r backend/requirements.txt

install-dev: ## Install runtime + dev dependencies
	$(PIP) install -r backend/requirements-dev.txt

migrate: ## Apply database migrations
	cd backend && ../$(VENV)/Scripts/alembic.exe upgrade head

downgrade: ## Roll back one migration
	cd backend && ../$(VENV)/Scripts/alembic.exe downgrade -1

run: ## Start the API locally
	cd backend && ../$(VENV)/Scripts/uvicorn.exe app.main:app --reload --port 8000

test: ## Run all tests (integration tests skip without TEST_DATABASE_URL)
	cd backend && ../$(VENV)/Scripts/python.exe -m pytest

test-unit: ## Run only tests that need no database
	cd backend && ../$(VENV)/Scripts/python.exe -m pytest -m "not integration"

test-integration: ## Run only database-backed tests (needs TEST_DATABASE_URL)
	cd backend && ../$(VENV)/Scripts/python.exe -m pytest -m integration

docker-build: ## Build the backend image
	$(COMPOSE) build backend

docker-up: ## Start pgvector + backend
	$(COMPOSE) up -d --build

docker-down: ## Stop the stack
	$(COMPOSE) down

docker-logs: ## Tail backend logs
	$(COMPOSE) logs -f backend

clean: ## Remove caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .coverage htmlcov
