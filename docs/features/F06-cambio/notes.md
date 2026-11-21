# Notas — F06: Agente de Câmbio (AwesomeAPI + Fallback Frankfurter)

## Entregue
- Contratos de domínio para câmbio (`ExchangeQuote`, `ExchangeServiceError`, `ExchangeRateProvider` e `normalize_currency_code` com aliases em português e rejeição segura de `BRL`/entradas inválidas).
- Cliente HTTP assíncrono `ExchangeRateClient` (`httpx.AsyncClient`) consultando AwesomeAPI como fonte primária e acionando fallback automático para Frankfurter API (BCE) com validação de taxa e data de referência ISO.
- Agente de Câmbio completo (`build_exchange_prompt`, `consultar_cotacao` via `ToolRuntime` com ponte segura sync→async, injeção de `ExchangeRateProvider` em `build_graph` e `ui/app.py`, timeout configurável via `EXCHANGE_TIMEOUT_SECONDS`).
- Suítes `tests/test_exchange_client.py` (via `httpx.MockTransport`) e `tests/test_exchange_agent.py` (incluindo preservação da data de referência pós-redação e invocação sob event loop ativo).
- Decisões e caminhos de escala registrados em `docs/adr/005-integracao-cambio-com-fallback.md`.

## Revisão
- Cross-model review aprovado de primeira, sem ajustes de código necessários.

## Verificação
- `make check`: `ruff`, `mypy --strict` (20 arquivos) e 274 testes passando (99% de cobertura total; 100% em `exchange_client.py`, `graph.py`, `prompts.py`, `rules.py`, `models.py`).
