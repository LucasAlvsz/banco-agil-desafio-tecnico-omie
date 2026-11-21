# Plano — F03: Agente de Triagem e Gate de Autenticação

## Objetivo
Evoluir o grafo do walking skeleton para a topologia multiagente por handoffs definida no **ADR 003** (nós explícitos, `ToolNode` isolado por agente, roteador condicional determinístico único e `ToolRuntime`), entregando o **Agente de Triagem** completo com autenticação contra `clientes.csv`, bloqueio determinístico na 3ª falha consecutiva e redirecionamento implícito pós-autenticação para os nós de Crédito e Câmbio.

## Dependências
Nenhuma dependência nova em `pyproject.toml`.

## Escopo

### 1. Estado Tipado (`src/banco_agil/agents/state.py`)
- `AgentName = Literal["triagem", "credito", "entrevista", "cambio"]`
- `ConversationState(TypedDict)`:
  - `messages: Annotated[list[AnyMessage], add_messages]`
  - `active_agent: NotRequired[AgentName]` *(default lógico `"triagem"`)*
  - `authenticated: NotRequired[bool]` *(default lógico `False`)*
  - `client_cpf: NotRequired[str | None]` *(gravado exclusivamente por `autenticar_cliente`)*
  - `client_name: NotRequired[str | None]`
  - `auth_attempts: NotRequired[int]` *(default lógico `0`)*
  - `conversation_ended: NotRequired[bool]` *(default lógico `False`)*
- Todos os campos de sessão permanecem `NotRequired` para que `run_turn` continue enviando apenas `{"messages": [HumanMessage(...)]}` e nunca sobrescreva o checkpoint.

### 2. Prompts com Persona Única (`src/banco_agil/agents/prompts.py`)
- Diretriz base compartilhada: persona única do Banco Ágil, tom cordial e objetivo em português, proibição explícita de anunciar transferências/setores ao cliente (handoff invisível) e uso de `encerrar_atendimento` sempre que o cliente pedir para finalizar.
- `build_triage_prompt(state)`:
  - Pré-autenticação: recepciona o cliente, solicita CPF e data de nascimento (aceitando ambos juntos se já enviados), instrui a chamar `autenticar_cliente` primeiro antes de qualquer redirecionamento.
  - Pós-autenticação: cumprimenta pelo `client_name`, identifica o assunto (crédito ou câmbio) e aciona `redirecionar_para_credito` ou `redirecionar_para_cambio` sem mencionar mudança de agente.
- `build_credit_placeholder_prompt(state)` e `build_exchange_placeholder_prompt(state)`:
  - Prompts mínimos para os nós de destino nesta feature (mantendo persona única, informando o estado atual do canal quando acionados e permitindo retornar à triagem via `voltar_para_triagem` ou encerrar via `encerrar_atendimento`).

### 3. Ferramentas com `ToolRuntime` (`src/banco_agil/agents/tools.py`)
Migrar `encerrar_atendimento` para `langgraph.prebuilt.ToolRuntime[None, ConversationState]` e adicionar:
- `build_triage_tools(credit_service: CreditService)` retornando:
  - `autenticar_cliente(cpf: str, data_nascimento: str, runtime: ToolRuntime[None, ConversationState]) -> Command[Any]`:
    - Se `runtime.state.get("authenticated")` já for `True`: retorna `ToolMessage` informando que o cliente já está autenticado, sem alterar estado nem contador.
    - Invoca `credit_service.authenticate(cpf, data_nascimento)`:
      - `DomainValidationError` (formato inválido): retorna `ToolMessage` pedindo correção de formato; **não** incrementa `auth_attempts` e **não** ecoa PII.
      - `PersistenceError`: registra em `logging` apenas `type(exc).__name__`, retorna `ToolMessage` controlada de indisponibilidade temporária; **não** incrementa `auth_attempts`.
      - Retorno `None` (dados bem-formados sem correspondência): incrementa `auth_attempts = current + 1`; retorna `ToolMessage` genérica anti-enumeração (sem indicar se errou CPF ou data e sem ecoar os dados). Se `auth_attempts >= 3`, também define `conversation_ended = True` no `Command(update=...)`.
      - Retorno `Client`: atualiza `authenticated = True`, `client_cpf = client.cpf`, `client_name = client.nome`, `auth_attempts = 0`; `ToolMessage` informa apenas o nome do cliente (sem CPF nem data de nascimento).
  - `redirecionar_para_credito(runtime: ToolRuntime[None, ConversationState]) -> Command[Any]`:
    - Gate em código: se `not runtime.state.get("authenticated")` ou `not runtime.state.get("client_cpf")`, recusa a transição mantendo `active_agent = "triagem"`.
    - Se autenticado: retorna `Command(update={"active_agent": "credito", "messages": [ToolMessage(...)]})` (sem `goto`).
  - `redirecionar_para_cambio(runtime: ToolRuntime[None, ConversationState]) -> Command[Any]`:
    - Mesma validação de gate; se autenticado, atualiza `active_agent = "cambio"` (sem `goto`).
- `voltar_para_triagem(runtime: ToolRuntime[None, ConversationState]) -> Command[Any]`:
  - Atualiza `active_agent = "triagem"` preservando `authenticated`, `client_cpf` e `client_name`.
- Garantir via testes que `runtime` não aparece no schema JSON exposto ao LLM em nenhuma ferramenta.

### 4. Grafo Multiagente (`src/banco_agil/agents/graph.py`)
Seguir estritamente o **ADR 003**:
- `MAX_AUTH_ATTEMPTS = 3` (constante única no módulo de tools/regras de sessão).
- `TurnResult(BaseModel)` (imutável): `reply: str`, `conversation_ended: bool`, `active_agent: AgentName`, `authenticated: bool` (nunca expõe `client_cpf`).
- `build_graph(model: BaseChatModel, credit_service: CreditService, checkpointer: BaseCheckpointSaver[Any] | None = None) -> ConversationGraph`:
  - Helper de registro `_add_agent_nodes(...)` criando pares isolados:
    - `triagem` ↔ `triagem_tools = ToolNode([autenticar_cliente, redirecionar_para_credito, redirecionar_para_cambio, encerrar_atendimento])`
    - `credito` ↔ `credito_tools = ToolNode([voltar_para_triagem, encerrar_atendimento])`
    - `cambio` ↔ `cambio_tools = ToolNode([voltar_para_triagem, encerrar_atendimento])`
  - **Roteador condicional determinístico único:**
    - Na entrada (`START`): se `conversation_ended` → `END`; se `not authenticated` → `"triagem"`; senão → `state.get("active_agent", "triagem")` (fazendo fallback seguro para `"triagem"` se o valor não estiver registrado).
    - Após qualquer `*_tools`: se `conversation_ended` → nó ativo atual (ou `"triagem"` se não autenticado), que invocará o `model` **sem tools** para produzir despedida cordial; se `not authenticated` → `"triagem"`; senão → `state.get("active_agent", "triagem")`.
  - Nunca misturar `add_edge` estática com `Command(goto=...)`.
- `run_turn(...)`: preserva o comportamento seguro de logging (`type(exc).__name__`) e retorna `TurnResult` atualizado (incluindo `active_agent` e `authenticated` lidos do estado final).

### 5. Integração na UI (`src/banco_agil/ui/app.py`)
- Montar `CreditService` usando os repositórios CSV (`CsvClientRepository`, `CsvScoreLimitRepository`, `CsvLimitRequestRepository`) apontando para `Settings().data_dir` e injetar em `build_graph(model, credit_service)`.
- Manter a experiência visual de persona única (sem anunciar "transferência" ao cliente).

## Fora de Escopo
- Regras e tools de negócio do Agente de Crédito + HITL `interrupt()` (`F04`), nó e tools do Agente de Entrevista (`F05`) e cliente HTTP/tools de Câmbio (`F06`).
- Filtro global de mascaramento LGPD em handlers de logging (`F07`).
- Telemetria/Langfuse (`F08`), FastAPI (`F09`) e MCP (`F10`).
- Não editar `docs/backlog.md` nem `AGENTS.md` dentro do worktree.

## Estratégia de Testes (100% determinísticos, zero rede)
- Atualizar `tests/test_walking_skeleton.py` e `tests/test_ui.py` para injetar `CreditService` sobre CSVs temporários (`tmp_path`, sem tocar `data/`).
- Criar `tests/test_triage_agent.py` cobrindo:
  1. **Schema seguro (`ToolRuntime`):** `runtime` / `tool_call_id` / `client_cpf` não aparecem nos parâmetros JSON das tools expostas ao modelo.
  2. **Autenticação com sucesso:** CPF com pontuação + data `DD/MM/AAAA` → `authenticated=True`, `client_cpf="12345678901"`, `client_name="Ana Souza"`, `auth_attempts=0`, sem PII na `ToolMessage`.
  3. **Formato inválido não consome tentativa:** 5 entradas com CPF/data mal formatados não incrementam `auth_attempts` nem encerram a sessão.
  4. **3 falhas consecutivas encerram a sessão:** 1ª e 2ª falhas retornam mensagem genérica anti-enumeração e mantêm sessão aberta (`auth_attempts` 1 → 2); 3ª falha seta `conversation_ended=True`, gera despedida **sem tools** e bloqueia turnos futuros sem chamar o LLM.
  5. **Erro de persistência (`PersistenceError`):** loga apenas `PersistenceError` (sem PII), responde de forma controlada e não consome tentativa.
  6. **Gate de autenticação inviolável (defense in depth):**
     - Tentativa de chamar `redirecionar_para_credito` ou `redirecionar_para_cambio` antes de autenticar é recusada e mantém `active_agent="triagem"`.
     - Mesmo se o estado inicial/manipulado tentar apontar `active_agent="credito"` com `authenticated=False`, o roteador força `"triagem"`.
  7. **Handoff implícito no mesmo turno (`triagem → credito` e `triagem → cambio`):** após autenticar, o redirecionamento entrega diretamente ao nó especialista no mesmo turno e persiste `active_agent` no checkpoint para o turno seguinte.
  8. **Chamada paralela `autenticar_cliente` + `redirecionar_para_credito` no mesmo lote:** não gera erro de `tool_call_id` órfão; autentica primeiro, recusa o redirecionamento no snapshot pré-auth, retorna à triagem já autenticada e conclui o handoff com segurança.
  9. **Retorno de tópico (`voltar_para_triagem`):** devolve `active_agent="triagem"` preservando `authenticated=True` e `client_cpf` (sem pedir CPF novamente).
  10. **Isolamento físico de `ToolNode`s:** validar que o nó `triagem` só vincula tools de triagem e os nós `credito`/`cambio` não possuem `autenticar_cliente` nem `redirecionar_*`.

## Critérios de Aceite
- [ ] `make check` 100% verde (`ruff check`, `ruff format --check`, `mypy src` strict, `pytest --cov=banco_agil`).
- [ ] Cobertura ≥ 90% em `src/banco_agil/agents/`.
- [ ] Nenhum CPF ou data de nascimento aparece em `ToolMessage`, `TurnResult` ou logs.
- [ ] Commits pequenos em Conventional Commits na branch `feat/f03-triagem`.
