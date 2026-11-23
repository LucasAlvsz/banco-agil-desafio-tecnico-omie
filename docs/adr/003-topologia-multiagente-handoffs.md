# ADR 003 — Topologia multiagente por handoffs com nós e ToolNodes isolados

- **Status:** Aceito
- **Data:** 2026-11-21
- **Feature:** Spike pré-F03 (aplicada a partir da F03)

## Contexto

O Banco Ágil possui quatro agentes especializados (`triagem`, `credito`, `entrevista`, `cambio`) que conversam diretamente com o cliente em múltiplos turnos, mantendo a experiência de uma única persona contínua (handoffs implícitos) e exigindo um gate inviolável de autenticação antes de qualquer operação de crédito ou câmbio.

Precisamos escolher a topologia de orquestração no LangGraph v1 que maximize segurança (menor privilégio de ferramentas e proteção contra prompt injection), baixa latência/custo e observabilidade por especialista.

## Decisão

Adotamos a topologia de **handoffs com 4 nós explícitos de agentes, `ToolNode` isolado por agente e um roteador condicional determinístico único**:

1. **Nós explícitos e isolamento físico de ferramentas:** cada especialista (`triagem`, `credito`, `entrevista`, `cambio`) é um nó próprio no `StateGraph` conectado exclusivamente ao seu `ToolNode` dedicado (`triagem_tools`, `credito_tools`, `entrevista_tools`, `cambio_tools`). Ferramentas fora do escopo de um agente não existem em seu `ToolNode`.
2. **Handoffs via estado (`Command(update=...)` sem `goto`):** ferramentas de transição atualizam `active_agent` (e emitem o respectivo `ToolMessage`) via `Command(update=...)`, sem utilizar `goto`.
3. **Roteador condicional determinístico único (zero tokens):** uma função Python pura governa tanto a entrada (`START`) quanto a saída de todos os `ToolNode`s:
   - Se `conversation_ended` for `True`: encerra na entrada (`END`) ou direciona ao nó ativo sem tools vinculadas para gerar a despedida final pós-ferramenta.
   - Se `not authenticated`: força roteamento para `triagem` (gate determinístico inviolável).
   - Caso contrário: direciona para `state["active_agent"]`, permitindo transições invisíveis dentro do mesmo turno.
4. **Injeção de estado confiável (`ToolRuntime`):** ferramentas leem estado confiável da sessão (`runtime.state`, ex.: `authenticated`, `client_cpf`, `auth_attempts`) e `runtime.tool_call_id` via `langgraph.prebuilt.ToolRuntime`, mantendo esses dados fora do schema exposto ao LLM.

## Alternativas Descartadas

- **Supervisor LLM central:** contra-indicado pela própria documentação do LangGraph/LangChain v1 quando os especialistas precisam conduzir diálogos multi-turno diretamente com o usuário. Além disso, adiciona uma chamada extra de LLM por turno (maior latência e custo), fragiliza o gate de autenticação caso dependa de prompt e dificulta a continuidade de persona única.
- **Nó único dinâmico (`Single Agent State Machine` com `ToolNode` único):** reduz o número de nós no grafo, mas concentra todas as ferramentas do sistema em um único `ToolNode` (perdendo o isolamento físico por escopo), mascara a identidade do especialista nos traces de observabilidade e acopla ferramentas locais a futuras ferramentas externas (MCP).
- **Roteamento via `Command(goto=...)` misturado com arestas estáticas:** na semântica do LangGraph v1, `Command(goto=...)` adiciona uma aresta dinâmica sem cancelar arestas estáticas (`add_edge`), podendo disparar dois nós em paralelo, além de complicar lotes com múltiplas chamadas de ferramentas no mesmo turno.

## Consequências

- **Positivas:**
  - Defesa em profundidade: mesmo em caso de alucinação ou prompt injection de nome de ferramenta, o `ToolNode` do agente ativo não possui ferramentas de outros domínios.
  - Zero custo/latência de LLM para roteamento de entrada e entre turnos de um mesmo especialista.
  - Diagrama gerado pelo grafo (`draw_mermaid()`) e traces de observabilidade identificam explicitamente cada agente executado.
  - Facilita evoluções independentes: o cliente MCP (F10) acopla-se apenas ao `cambio_tools`, e cada nó pode receber um `BaseChatModel` distinto caso haja otimização de custo/latência por papel.
- **Trade-offs / Riscos:**
  - Pequeno acréscimo de estrutura para registrar pares `(agente, agente_tools)` (mitigado por helper de registro no `graph.py`).
  - Se o número de agentes crescer para dezenas de domínios independentes, transições ponto a ponto aumentam a complexidade de manutenção.

## Evolução e Escala

- **Crescimento do número de especialistas:** acima de ~8–10 domínios independentes, evoluir a entrada pós-autenticação para um classificador/roteador dedicado (ou subgrafos por macro-domínio), mantendo o isolamento de `ToolNode`s e o gate determinístico de autenticação.
- **Otimização custo/latência por papel:** com nós separados, é possível injetar modelos menores/mais rápidos nos nós de triagem e câmbio e modelos de maior raciocínio em crédito e entrevista sem alterar a topologia.
- **Execução distribuída:** combinada a um checkpointer persistente (ADR 002) e a um `ToolRuntime` recebendo contexto por requisição, a topologia opera de forma stateless entre réplicas.

## Atualização (encerramento de tópico e retorno à Triagem)

- **Data:** 2026-11-23

Ao concluir um assunto específico (consulta de limite, aprovação/cancelamento de aumento ou cotação de câmbio), o especialista permanece como `active_agent`, responde diretamente ao cliente e encerra apenas o tópico, evitando uma chamada extra de LLM e o descarte do resultado na Triagem. `voltar_para_triagem` é acionada apenas quando o cliente muda de assunto, solicita outro serviço ou desiste da entrevista. Todo retorno por essa ferramenta limpa `pending_requested_limit`. Se a mudança for pedida para depois do resultado na mesma mensagem, o especialista executa sua ferramenta antes do handoff para que a Triagem inclua o resultado na resposta.

Em uma nova Entrevista de Crédito no mesmo Atendimento, `calcular_e_atualizar_score` usa o histórico de ferramentas para bloquear deterministicamente o recálculo no turno da reentrada. O prompt orienta o agente a coletar os cinco indicadores novamente e usar apenas os dados da entrevista atual.
