# ADR 007 — Evals e observabilidade

- **Status:** Aceito
- **Data:** 2026-11-21
- **Feature:** F08

## Contexto

A equipe do Banco Ágil precisa identificar aumentos de latência e custo, entender
handoffs e verificar se mudanças de modelo ou prompt preservam jornadas críticas.
O atendimento precisa funcionar com a coleta externa desativada. CPF, datas de
nascimento e credenciais conhecidas precisam de proteção também na exportação.

## Decisão

- Coletar a telemetria local a cada turno e retomada de confirmação no LangGraph
  com um `BaseCallbackHandler`. Mostrar agente atual, rota, latência, tokens e
  estimativa de custo na sidebar. A estimativa usa `litellm.cost_per_token`. A UI
  mostra `N/D` quando o preço do modelo não está disponível.
- Habilitar Langfuse v4 quando as duas chaves estiverem configuradas. Correlacionar
  traces pelo identificador de sessão gerado pela aplicação e omitir a
  identificação do usuário. Aplicar as funções de `privacy.py` no hook `mask` e
  em `mask_otel_spans`, na etapa de exportação dos atributos OpenTelemetry.
- Manter jornadas declarativas em `evals/scenarios.json`. `make evals` executa
  o grafo e os repositórios reais com cópias temporárias dos CSVs, respostas de
  modelo roteirizadas e endpoints de câmbio simulados. O modo `live` usa o
  provedor LLM configurado e verifica estado, rotas, CSVs e PII com as mesmas
  cotações simuladas, inclusive o cenário de falha do provedor primário.

## Alternativas Descartadas

- **Depender exclusivamente de SaaS para telemetria.** Tornaria o diagnóstico
  local dependente de credenciais e disponibilidade de um serviço externo.
- **Usar LLM como avaliador no CI.** Introduziria dependência de rede, custo e
  variação em verificações de regras determinísticas.
- **Manter uma tabela manual de preços.** Duplicaria o catálogo do LiteLLM e
  exigiria atualizações a cada mudança de preço ou de provedor.

## Consequências

- **Positivas.** A equipe consegue diagnosticar cada turno localmente. Os evals
  verificam regras de negócio, confirmação humana, transições e privacidade
  contra dados isolados.
- **Trade-offs e riscos.** O custo é uma estimativa com base no uso informado
  e no preço que o LiteLLM conhece. A suíte offline verifica jornadas
  roteirizadas. O modo live pode variar entre execuções e consumir créditos do
  provedor. O mascaramento reconhece os padrões de `privacy.py` e o hook
  OpenTelemetry atua nos atributos dos spans enviados pelo SDK.

## Evolução e Escala

- Centralizar coleta e controles de retenção em um OpenTelemetry Collector
  quando houver múltiplas réplicas ou exportadores.
- Agendar evals live para detectar regressões em prompts e modelos. Acrescentar
  avaliadores semânticos para avaliar a qualidade da linguagem e das recusas.
- Ajustar `sample_rate` conforme o volume de traces. Definir alertas de SLO para
  latência p95, custo por atendimento e taxas de falhas e bloqueios.

## Atualização (F12/F16)

- **Data:** 2026-11-22

Na F12, `TurnObservability` e `TurnScope` isolaram a instrumentação por turno sem
acoplamento circular com `agents`, garantindo um handler Langfuse independente por
turno e ownership explícito de `flush()` no `ConversationRuntime`. Na F16, o executor
de evals passou a verificar vazamentos via `contains_pii()` (`privacy.py`) e a ler o
estado dos CSVs via `list_all()` nos repositórios concretos.
