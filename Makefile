.PHONY: setup format test check ui

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
