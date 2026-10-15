SHELL := /bin/bash
DATABASE_URL ?= postgresql+psycopg://coop:coop-local-only@127.0.0.1:5432/coop

.PHONY: setup db-up db-wait migrate seed dev api web test lint format-check typecheck build e2e down reset-db openapi
setup:
	corepack pnpm install --frozen-lockfile
	uv sync --project apps/api --all-groups --frozen

db-up:
	docker compose up -d db

db-wait:
	@until docker compose exec -T db pg_isready -U $${POSTGRES_USER:-coop} -d $${POSTGRES_DB:-coop}; do sleep 1; done

migrate:
	DATABASE_URL=$(DATABASE_URL) uv run --project apps/api alembic upgrade head

seed:
	DATABASE_URL=$(DATABASE_URL) uv run --project apps/api python -m app.seed

api:
	DATABASE_URL=$(DATABASE_URL) uv run --project apps/api uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

web:
	corepack pnpm --filter web dev

dev:
	@echo "In zwei Terminals ausführen: make api und make web"

test:
	cd apps/api && DATABASE_URL=$(DATABASE_URL) uv run pytest
	corepack pnpm --filter web test

lint:
	cd apps/api && uv run ruff check app tests alembic
	corepack pnpm --filter web lint

format-check:
	cd apps/api && uv run ruff format --check app tests alembic
	corepack pnpm format:check

typecheck:
	cd apps/api && uv run mypy app
	corepack pnpm --filter web typecheck

build:
	corepack pnpm --filter web build

e2e:
	corepack pnpm exec playwright test

openapi:
	mkdir -p packages/api-client
	cd apps/api && .venv/bin/python -c 'import json; from app.main import app; print(json.dumps(app.openapi(), indent=2))' > ../../packages/api-client/openapi.json

down:
	docker compose down

reset-db:
	docker compose down -v
