# ADR 005 — Integração de câmbio com fallback

- **Status:** Aceito
- **Data:** 2026-11-21
- **Feature:** F06

## Contexto

O Agente de Câmbio precisa consultar cotações em reais para clientes autenticados. A consulta deve continuar disponível durante falhas do provedor primário sempre que houver uma taxa de referência para a moeda solicitada.

## Decisão

Usar a AwesomeAPI como fonte primária de cotação em BRL e a Frankfurter v1 como fallback para a taxa diária de referência do Banco Central Europeu (BCE). Se a consulta primária falhar ou a resposta for inválida, tentar o fallback. Na resposta ao cliente, identificar a taxa de referência e informar sua data. Se ambas falharem, informar indisponibilidade temporária e manter o atendimento aberto. [Documentação da AwesomeAPI](https://docs.awesomeapi.com.br/api-de-moedas), [contrato da Frankfurter v1](https://api.frankfurter.dev/v1/openapi.json).

O cliente usa `httpx.AsyncClient` e implementa o contrato assíncrono `ExchangeRateProvider`. O `ToolNode` de câmbio recebe o provedor por injeção, verifica a autenticação antes da consulta e usa a cotação estruturada para alimentar a resposta. A UI configura o timeout por `EXCHANGE_TIMEOUT_SECONDS`, com padrão de 8 segundos.

## Alternativas Descartadas

- **Tavily ou SerpAPI.** A busca web exige uma chave adicional e extrair valores de texto livre com o LLM aumenta o risco de apresentar números financeiros incorretos.
- **Provedor único.** Uma falha temporária na fonte primária tornaria a consulta indisponível mesmo para moedas que têm uma taxa diária publicada.

## Consequências

- **Positivas.** As taxas são validadas em código e a origem de cada dado fica explícita. O contrato permite testar a ferramenta com um provedor em memória e o cliente HTTP com `httpx.MockTransport`.
- **Trade-offs e riscos.** O fallback adiciona uma segunda chamada quando a primeira falha. A taxa diária pode corresponder ao último dia útil e a Frankfurter v1 cobre apenas as moedas publicadas pelo BCE. A ferramenta aguarda a resposta do provedor durante o turno.

## Evolução e Escala

Com aumento de tráfego, considerar cache distribuído com TTL de 30–60 segundos e circuit breaker para evitar insistir em provedores indisponíveis. Em produção, avaliar um provedor contratado com SLA e cobertura compatível com as moedas oferecidas. Na F10, expor o cliente pelo servidor MCP por meio do mesmo contrato de domínio.

## Atualização (F10/F15)

- **Data:** 2026-11-22

O cliente HTTP de câmbio passou a ser exposto pelo servidor MCP stateless de câmbio ([ADR 009](009-servidor-mcp-cambio-e-fronteira-de-seguranca.md)) atrás do mesmo contrato `ExchangeRateProvider`. Na F15, o protocolo de fio, o servidor e o cliente foram colocalizados em `banco_agil.mcp` (`create_exchange_provider`), consumidos via `ConversationRuntime` com timeout configurado por `EXCHANGE_TIMEOUT_SECONDS`.
