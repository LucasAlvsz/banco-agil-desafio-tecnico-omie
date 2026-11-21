# Plano — F02: Walking Skeleton (LangGraph + LiteLLM + Streamlit)

## Objetivo
Conectar de ponta a ponta a interface conversacional (**Streamlit**), a orquestração com estado persistente por sessão (**LangGraph** com 1 nó e `InMemorySaver`), a camada agnóstica de LLM (**LiteLLM** atrás de uma factory) e a ferramenta global de encerramento (`encerrar_atendimento`), validando na prática o suporte a `bind_tools()` com o modelo configurado na Groq antes de introduzir a topologia multiagente.

## Dependências a Adicionar (`pyproject.toml`)
- `pydantic-settings>=2.7.0`
- `langchain-core>=0.3.0`
- `langchain-litellm>=0.2.0`
- `langgraph>=1.0.0`
- `litellm>=1.60.0`
- `streamlit>=1.42.0`

*Tipagem estrita:* manter `strict = true` no `mypy`; se alguma biblioteca terceira (`langchain_litellm`, `litellm`, `streamlit`) não distribuir stubs completos, declarar `[[tool.mypy.overrides]]` restrito apenas a esses módulos (nunca `ignore_missing_imports = true` global).

## Escopo

### 1. Configuração Tipada (`src/banco_agil/config.py` e `.env.example`)
- `Settings(BaseSettings)` lendo `.env` (`extra="ignore"`):
  - `llm_model: str = "groq/openai/gpt-oss-120b"` — validar que contém `/` no formato explícito `provedor/modelo` (sem heurísticas implícitas de adivinhação).
  - `llm_temperature: float = Field(default=0.1, ge=0.0, le=2.0)`
  - `llm_timeout_seconds: float = Field(default=45.0, gt=0.0)`
  - `data_dir: Path = Path("data")`
  - Credenciais opcionais usando `SecretStr | None = None`: `groq_api_key`, `gemini_api_key`, `openai_api_key`, `openrouter_api_key`, `aws_access_key_id`, `aws_secret_access_key`, `aws_region_name` (garantindo que nenhum segredo vaze em `repr()` ou tracebacks).
- Atualizar `.env.example` documentando as variáveis acima e exemplos para Groq, Gemini, OpenAI e Amazon Bedrock.

### 2. Fábrica de LLM (`src/banco_agil/infrastructure/llm_factory.py`)
- `create_chat_model(settings: Settings) -> BaseChatModel`:
  - Exporta credenciais não vazias de `Settings` para `os.environ` apenas quando a variável ainda não estiver preenchida no ambiente do processo (sem sobrescrever env vars explícitas e sem logar chaves).
  - Instancia e retorna `ChatLiteLLM(model=..., temperature=..., request_timeout=..., max_retries=2)` tipado como `BaseChatModel`.
- O grafo recebe `BaseChatModel` por injeção de dependência, nunca importando `litellm` diretamente.

### 3. Grafo Mínimo e Ferramenta de Encerramento (`src/banco_agil/agents/`)
- `state.py`: `ConversationState(TypedDict)` mínimo:
  - `messages: Annotated[list[AnyMessage], add_messages]`
  - `conversation_ended: bool`
- `tools.py`:
  - `encerrar_atendimento(motivo: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command[Any]`: retorna `Command(update={"conversation_ended": True, "messages": [ToolMessage(content=..., tool_call_id=tool_call_id)]})`.
- `graph.py`:
  - `TurnResult(BaseModel)` (imutável): `reply: str`, `conversation_ended: bool`.
  - `build_graph(model: BaseChatModel, checkpointer: BaseCheckpointSaver[Any] | None = None)` usando `StateGraph(ConversationState)` + prebuilt `ToolNode([encerrar_atendimento])` + `InMemorySaver()` padrão:
    - **Roteamento na entrada (`START`):** se `state.get("conversation_ended", False)` for `True`, vai direto para `END` (sessão encerrada nunca invoca o LLM novamente).
    - **Nó `assistant`:** injeta o system prompt mínimo do Banco Ágil (persona única, cordial e objetiva, instruindo a acionar `encerrar_atendimento` quando o cliente pedir para encerrar).
      - Se `conversation_ended` já for `True` (ou seja, acabou de retornar da tool `encerrar_atendimento`), invoca o `model` **sem tools** para gerar apenas a mensagem final de despedida (evitando loop de re-invocação da tool).
      - Caso contrário, invoca `model.bind_tools([encerrar_atendimento])`.
    - **Aresta pós-`assistant`:** se a última `AIMessage` contiver `tool_calls` → `tools`; senão → `END`.
    - **Aresta pós-`tools`:** retorna para `assistant` para consolidar a resposta ao cliente.
  - `run_turn(graph, *, thread_id: str, user_message: str) -> TurnResult`: encapsula a invocação por `thread_id`, retorna mensagem cordial fixa se a sessão já estava encerrada e extrai com segurança o texto da última `AIMessage`.

### 4. Interface Conversacional (`src/banco_agil/ui/app.py`) e `Makefile`
- Chat simples em Streamlit (`st.chat_message`, `st.chat_input`) mantendo `thread_id`, histórico de mensagens e flag `conversation_ended` em `st.session_state`.
- Sidebar com botão **"Nova conversa"** (gera novo `thread_id` e limpa o histórico).
- Quando `conversation_ended == True`, desabilita o `st.chat_input` e informa que o atendimento foi encerrado com opção de iniciar nova conversa.
- Tratamento controlado de falhas de configuração ou provedor LLM exibindo erro amigável na UI sem stack trace cru.
- Adicionar alvo `make ui` no `Makefile` (`uv run streamlit run src/banco_agil/ui/app.py`).

### 5. Documentação Arquitetural
- `docs/adr/002-camada-llm-litellm-e-walking-skeleton.md` (seguindo `000-template.md`): registra o uso de `ChatLiteLLM` atrás de `BaseChatModel`, formato explícito `provedor/modelo`, `ToolNode` + `Command` nativo do LangGraph v1, `SecretStr` para segredos e caminhos de evolução para escala (LiteLLM Proxy e checkpointer persistente).

## Fora de Escopo
- Os 4 agentes especializados (Triagem, Crédito, Entrevista, Câmbio) e integração com `CreditService` (`F03`–`F06`).
- Topologia multiagente (definida no Spike seguinte antes da `F03`).
- FastAPI, Docker, Langfuse, telemetria detalhada e MCP (`F08`–`F10`).
- Não editar `docs/backlog.md` nem `AGENTS.md` dentro do worktree.

## Estratégia de Testes
- `tests/test_config_and_llm_factory.py` (zero rede):
  - Validação de `Settings` (formato `provedor/modelo` válido vs. inválido sem `/`, faixa de temperatura/timeout).
  - `SecretStr` oculta chaves em `repr(settings)` e `str(settings)`.
  - `create_chat_model` retorna instância de `BaseChatModel` (`ChatLiteLLM`) com modelo/temperatura/timeout esperados e propaga chaves para `os.environ` sem sobrescrever variáveis já existentes.
- `tests/test_walking_skeleton.py` (zero rede, usando Fake `BaseChatModel` determinístico):
  - Conversa multi-turno preserva histórico no mesmo `thread_id` via `InMemorySaver`.
  - Sessões com `thread_id`s diferentes mantêm contextos isolados.
  - Quando o modelo aciona `encerrar_atendimento`, `ToolNode` propaga `Command(update={"conversation_ended": True, ...})`, o nó `assistant` gera a despedida **sem** re-vincular tools, `TurnResult.conversation_ended` retorna `True` e qualquer turno subsequente no mesmo `thread_id` não invoca o modelo novamente.
  - Entrada vazia ou erro inesperado do provedor são tratados de forma controlada.
- **Validação prática pós-implementação (Groq real, fora da suíte `pytest`):**
  - Configurar `.env` local (ignorado pelo Git) com `GROQ_API_KEY` e `LLM_MODEL=groq/openai/gpt-oss-120b`.
  - Executar um smoke check manual (via script ou `make ui`) confirmando 1 turno conversacional + 1 turno pedindo para encerrar que acione `encerrar_atendimento` via `bind_tools()`.

## Critérios de Aceite
- [ ] `make check` 100% verde (`ruff check`, `ruff format --check`, `mypy src` strict, `pytest --cov=banco_agil`).
- [ ] Cobertura ≥ 85% nos novos módulos (`config.py`, `llm_factory.py`, `agents/`).
- [ ] `make ui` sobe o Streamlit localmente e conversa ponta a ponta com Groq, encerrando a sessão corretamente via tool calling.
- [ ] `docs/adr/002-camada-llm-litellm-e-walking-skeleton.md` criado e revisado.
- [ ] Commits pequenos em Conventional Commits dentro da branch `feat/f02-walking-skeleton`.
