.PHONY: setup build test lint dev-api dev-web openapi

setup:
	cd backend && uv venv -q .venv && uv pip install -q -e ".[dev]"
	cd frontend && npm ci

build:
	cd frontend && npm run build

test:
	cd backend && .venv/bin/pytest -q
	cd frontend && npm run build

lint:
	cd backend && .venv/bin/ruff check .
	cd frontend && npm run lint

dev-api:
	cd backend && .venv/bin/uvicorn filament.main:app --reload --port 8000

dev-web:
	cd frontend && npm run dev

openapi:
	cd backend && .venv/bin/python -c "import json; from filament.main import app; \
	print(json.dumps(app.openapi(), indent=2))" > ../docs/openapi.json
