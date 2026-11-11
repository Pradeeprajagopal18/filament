.PHONY: setup check-node build test lint dev-api dev-web openapi

setup: check-node
	cd backend && uv venv -q --allow-existing .venv && uv pip install -q -e ".[dev]"
	cd frontend && npm ci

check-node:
	@node frontend/scripts/check-node.mjs

build:
	cd frontend && npm run build

test:
	cd backend && .venv/bin/pytest -q
	cd frontend && npm run build

lint:
	cd backend && .venv/bin/ruff check .
	cd frontend && npm run lint

API_PORT ?= 8000

dev-api:
	cd backend && .venv/bin/uvicorn igniteai.main:app --reload --host 127.0.0.1 --port $(API_PORT)

dev-web: check-node
	cd frontend && API_PORT=$(API_PORT) npm run dev

openapi:
	cd backend && .venv/bin/python -c "import json; from igniteai.main import app; \
	print(json.dumps(app.openapi(), indent=2))" > ../docs/openapi.json
