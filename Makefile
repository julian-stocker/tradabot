# ---------------------------------------------------------------------------
# tradabot developer commands.
# Run `make help` for the list.
# ---------------------------------------------------------------------------
.DEFAULT_GOAL := help
.PHONY: help install dev test test-cov lint format format-check typecheck check \
        migrate migration downgrade seed seed-profiles demo-simulation signal up down logs clean

PYTHON ?= python3.12
VENV   := .venv
BIN    := $(VENV)/bin

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

$(VENV): ## Create the virtualenv
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip

install: $(VENV) ## Install the project with dev dependencies
	$(BIN)/pip install -e ".[dev]"

dev: ## Run the API with autoreload on :8000
	$(BIN)/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test: ## Run the test suite
	$(BIN)/pytest

test-cov: ## Run tests with a coverage report
	$(BIN)/pytest --cov=app --cov-report=term-missing --cov-report=html

lint: ## Lint with ruff
	$(BIN)/ruff check app tests

format: ## Auto-format and sort imports
	$(BIN)/ruff format app tests
	$(BIN)/ruff check --fix app tests

format-check: ## Verify formatting without changing files
	$(BIN)/ruff format --check app tests

typecheck: ## Type-check with mypy (strict)
	$(BIN)/mypy app

check: format-check lint typecheck test ## Everything CI runs

migrate: ## Apply all migrations
	$(BIN)/alembic upgrade head

migration: ## Autogenerate a migration: make migration m="add x"
	@test -n "$(m)" || (echo "usage: make migration m=\"describe the change\"" && exit 1)
	$(BIN)/alembic revision --autogenerate -m "$(m)"

downgrade: ## Roll back one migration
	$(BIN)/alembic downgrade -1

seed: ## Ingest deterministic synthetic market data
	$(BIN)/python -m app.cli seed --days 400

seed-profiles: ## Install the default simulation-profile catalogue
	$(BIN)/python -m app.cli seed-profiles

demo-simulation: ## Run the deterministic multi-profile paper-trading demo
	$(BIN)/python -m app.cli demo-simulation

signal: ## Print a signal: make signal s=NVDA
	@test -n "$(s)" || (echo "usage: make signal s=NVDA" && exit 1)
	$(BIN)/python -m app.cli signal $(s)

up: ## Start the Docker stack
	docker compose up --build -d

down: ## Stop the Docker stack
	docker compose down

logs: ## Follow API logs
	docker compose logs -f api

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage dist build
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
