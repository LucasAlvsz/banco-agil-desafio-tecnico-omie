# Notas — F08: Evals & Observabilidade

## Entregue
- Telemetria local sempre ativa por turno (`TurnTelemetry` e `TurnTelemetryCallback` em `src/banco_agil/infrastructure/observability.py`) integrada a `run_turn` e `resume_turn`, capturando rota de agentes, ferramentas acionadas (sem argumentos/PII), latência (`ms`), tokens (`usage_metadata` com fallback para `token_usage`) e custo estimado em USD via `litellm.cost_per_token`.
- Integração opcional ao Langfuse v4 (`langfuse>=4.0.0,<5`), habilitada somente quando `LANGFUSE_PUBLIC_KEY` e `LANGFUSE_SECRET_KEY` estiverem preenchidos (zero import/rede quando desativado), aplicando `mask` e `mask_otel_spans` com `privacy.py`, correlacionando via `langfuse_session_id` sem `langfuse_user_id` e degradando silenciosamente em falhas.
- Painel lateral **"Telemetria do turno"** na UI Streamlit (`src/banco_agil/ui/app.py`) exibindo status do Langfuse, agente ativo, rota, latência, tokens e custo estimado (resetado em *Nova conversa*, sem PII).
- Suíte declarativa de avaliação (`evals/scenarios.json` com 12 cenários, `src/banco_agil/evals/` e alvo `make evals`) com modo `offline` determinístico (CSVs isolados em diretório temporário, `httpx.MockTransport` para câmbio primário e fallback BCE, checagem automática de zero vazamento de PII e saída `--json`) e modo `live` opcional.
- Decisões e caminhos de escala registrados em [ADR 007](../../adr/007-evals-e-observabilidade.md).

## Revisão
- Cross-model review aprovado de primeira (LGTM), sem ajustes de código necessários.

## Verificação
- `make check`: `ruff`, `mypy --strict` (27 arquivos) e 366 testes passando (99% cobertura total; 100% em `observability.py`, `evals/models.py` e `graph.py`; 99% em `evals/runner.py` e `ui/app.py`; 97% em `evals/__main__.py`).
- `make evals`: 12/12 cenários aprovados (100%), rotas 100%, 0 vazamentos de PII.
