# ---------------------------------------------------------------------------
# tradabot developer commands.
# Run `make help` for the list.
# ---------------------------------------------------------------------------
.DEFAULT_GOAL := help
.PHONY: help install dev test test-cov lint format format-check typecheck check \
        migrate migration downgrade seed seed-profiles demo-simulation signal \
        market-data-status market-data-import market-data-sync quote simulate \
        smoke-real-data notify-test notify-status daily-summary up down logs clean

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

market-data-status: ## Show provider configuration and stored-data freshness
	$(BIN)/python -m app.cli market-data status

market-data-import: ## Import a window: make market-data-import s=NVDA from=2024-01-01 to=2024-06-30 [tf=1d]
	@test -n "$(s)" -a -n "$(from)" -a -n "$(to)" || \
		(echo "usage: make market-data-import s=NVDA from=2024-01-01 to=2024-06-30 [tf=1d]" && exit 1)
	$(BIN)/python -m app.cli market-data import $(s) --start $(from) --end $(to) --timeframe $(or $(tf),1d)

market-data-sync: ## Update the watchlist (or make market-data-sync s=NVDA)
	$(BIN)/python -m app.cli market-data sync $(s)

quote: ## Fetch one latest quote: make quote s=NVDA
	@test -n "$(s)" || (echo "usage: make quote s=NVDA" && exit 1)
	$(BIN)/python -m app.cli market-data quote $(s)

simulate: ## Replay imported real candles: make simulate s=NVDA from=2024-01-01 to=2024-06-30
	@test -n "$(s)" -a -n "$(from)" -a -n "$(to)" || \
		(echo "usage: make simulate s=NVDA from=2024-01-01 to=2024-06-30" && exit 1)
	$(BIN)/python -m app.cli simulate --symbol $(s) --from $(from) --to $(to)

smoke-real-data: ## Opt-in smoke test against the live provider (needs credentials)
	TRADABOT_RUN_EXTERNAL_TESTS=1 $(BIN)/pytest tests/external -v

notify-test: ## Send a TEST notification to every configured channel
	$(BIN)/python -m app.cli notifications test

notify-status: ## Show notification configuration and delivery outcomes
	$(BIN)/python -m app.cli notifications status

daily-summary: ## Build and send the daily portfolio report
	$(BIN)/python -m app.cli notifications daily-summary

up: ## Start the Docker stack
	docker compose up --build -d

down: ## Stop the Docker stack
	docker compose down

logs: ## Follow API logs
	docker compose logs -f api

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage dist build
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
