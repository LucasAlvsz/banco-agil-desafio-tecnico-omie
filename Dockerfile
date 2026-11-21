FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY src/ src/
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY --from=builder /app/.venv .venv
COPY src/banco_agil/ui/app.py src/banco_agil/ui/app.py
RUN useradd --create-home --uid 1000 appuser
COPY --chown=appuser:appuser data/ data/
USER appuser
EXPOSE 8000
CMD ["uvicorn", "banco_agil.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
