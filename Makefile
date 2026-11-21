.PHONY: setup format test check ui api evals mcp-exchange

setup:
	uv sync
	uv run pre-commit install

format:
	uv run ruff check --fix .
	uv run ruff format .

test:
	uv run pytest --cov=banco_agil --cov-report=term-missing

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src
	uv run pytest --cov=banco_agil --cov-report=term-missing

ui:
	uv run streamlit run src/banco_agil/ui/app.py

api:
	uv run uvicorn banco_agil.api.app:app --host 0.0.0.0 --port 8000

evals:
	uv run python -m banco_agil.evals

mcp-exchange:
	uv run python -m banco_agil.mcp.exchange_server --transport streamable-http --host 127.0.0.1 --port 8001
