.PHONY: help setup db-up db-down migrate api worker sim demo test lint fmt clean

VENV := .venv/bin

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## create the venv, install the package, copy .env
	uv venv --python 3.12
	uv pip install -e '.[ml,dev]'
	@test -f .env || cp .env.example .env

db-up: ## start PostgreSQL
	docker compose up -d db
	@until docker compose exec -T db pg_isready -U mgs -d mgs >/dev/null 2>&1; do sleep 1; done
	@echo "postgres ready on localhost:5433"

db-down: ## stop PostgreSQL (keeps the volume)
	docker compose down

migrate: ## apply migrations
	$(VENV)/alembic upgrade head

api: ## run the ingestion API
	$(VENV)/mgs-api

worker: ## run the anomaly worker
	$(VENV)/mgs-worker

sim: ## fly the simulated satellite
	$(VENV)/mgs-sim

demo: db-up migrate ## end-to-end: 3 orbits ingested, screened, and summarised
	@set -e; \
	$(VENV)/mgs-api >/tmp/mgs-api.log 2>&1 & api=$$!; \
	trap "kill $$api 2>/dev/null || true" EXIT; \
	until curl -sf localhost:8000/health >/dev/null; do sleep 1; done; \
	$(VENV)/mgs-sim --orbits 3 --orbit-seconds 24 --frame-interval 0.05 --seed 7; \
	$(VENV)/mgs-worker --once; \
	docker compose exec -T db psql -U mgs -d mgs \
		-c "SELECT count(*) AS passes FROM passes" \
		-c "SELECT count(*) AS frames FROM telemetry" \
		-c "SELECT rule, severity, count(*), count(*) FILTER (WHERE resolved_at IS NOT NULL) AS resolved FROM alerts GROUP BY 1,2 ORDER BY 3 DESC"

test: ## run the test suite (integration tests need db-up)
	$(VENV)/python -m pytest -q

lint: ## ruff check
	$(VENV)/ruff check src tests
	$(VENV)/ruff format --check src tests

fmt: ## ruff format
	$(VENV)/ruff format src tests
	$(VENV)/ruff check --fix src tests

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__
