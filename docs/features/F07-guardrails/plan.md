# Plano — F07: Guardrails, Robustez e LGPD

## Objetivo
Consolidar a camada de **guardrails, robustez operacional e privacidade (LGPD)** do Banco Ágil em defesa em profundidade, garantindo:
1. Mascaramento transversal de PII (`cpf`, `data_nascimento`) e credenciais em **todos os logs** (via `logging.Filter` global e engine única reutilizável pela redação de respostas e, na F08, pelos traces de observabilidade).
2. Guardrails determinísticos de entrada (limites de tamanho, higienização básica e bloqueio de alta precisão contra *prompt injection*, extração de *system prompt* e tentativas de *bypass* de autenticação, sem custo de LLM) combinados a políticas explícitas de **escopo estrito** nos prompts de todos os agentes.
3. Robustez de execução no grafo e nas ferramentas: `recursion_limit` explícito no LangGraph v1 com tratamento controlado de `GraphRecursionError`, proteção defensiva contra exceções inesperadas nas ferramentas sem interferir em `GraphInterrupt`, e inicialização padronizada de logging na UI.

## Dependências (`pyproject.toml`)
- Nenhuma dependência nova (somente biblioteca padrão do Python — `logging`, `re`, `traceback`, `unicodedata` — e `langgraph.errors.GraphRecursionError` já instalado em `langgraph>=1.0.0`).

## Escopo

### 1. Privacidade, Mascaramento de PII e Logging LGPD (`src/banco_agil/infrastructure/privacy.py`)
Centralizar a lógica de mascaramento LGPD em um módulo único de infraestrutura (reutilizável por `agents/graph.py`, `logging`, `ui/app.py`, `F08` Langfuse e `F09` FastAPI):
- **Constantes e padrões:**
  - `REDACTED_PLACEHOLDER = "[dado omitido]"` (preserva compatibilidade integral com os contratos existentes).
  - `CPF_PATTERN`: regex equivalente ao padrão atual de CPF (`(?<!\d)[0-9](?:[.\-\s]*[0-9]){10}(?!\d)`), cobrindo 11 dígitos com ou sem pontuação/espaços.
  - `DATE_PATTERN`: regex equivalente ao padrão atual de datas (`YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `DD.MM.YYYY`).
  - `SECRET_PATTERN`: mascara vazamento acidental de chaves/tokens comuns (`Bearer <token>`, `gsk_...`, `sk-...`, `AIza...`, `AKIA...`, `pk-lf-...`, `sk-lf-...`) por `REDACTED_PLACEHOLDER`.
- **Funções puras:**
  - `mask_pii(text: str, *, preserve_dates_except: str | None = None) -> str`:
    - Mascara sempre `SECRET_PATTERN` e `CPF_PATTERN`.
    - Quando `preserve_dates_except is None` (modo estrito, padrão para logs, traces e sessões não autenticadas/sem data verificada): mascara todas as ocorrências de `DATE_PATTERN`.
    - Quando `preserve_dates_except` recebe uma data ISO verificada (ex.: `client_birth_date` do cliente autenticado em `TurnResult.reply`): mascara apenas datas que normalizam via `normalize_birth_date` para o mesmo valor, preservando datas operacionais e datas de referência de câmbio (Frankfurter/BCE).
  - `mask_pii_data(value: Any) -> Any`:
    - Helper recursivo (sem mutar o objeto original) para mascarar strings em estruturas `dict`, `list` e `tuple` (preparado para dados estruturados em logs e callbacks da F08).
- **Filtro global de logging (`PiiMaskingFilter(logging.Filter)`):**
  - `filter(self, record: logging.LogRecord) -> bool`:
    - Formata a mensagem com segurança (`record.getMessage()`, com fallback controlado em `TypeError`/`ValueError`), aplica `mask_pii(...)`, grava o texto mascarado em `record.msg` e limpa `record.args = None` (evitando dupla formatação ou erro com `%`).
    - Se `record.exc_text` estiver preenchido, aplica `mask_pii(record.exc_text)`.
    - Se `record.exc_info` for uma tupla de exceção válida (`exc_info[0] is not None`), formata via `traceback.format_exception(*record.exc_info)`, aplica `mask_pii(...)`, grava em `record.exc_text` e limpa `record.exc_info = None` para impedir emissão ou serialização da exceção bruta contendo PII.
    - Se `record.stack_info` estiver preenchido, aplica `mask_pii(record.stack_info)`.
    - Retorna sempre `True` (sanitiza sem descartar eventos).
- **Configuração idempotente (`install_pii_filter` e `configure_logging`):**
  - `install_pii_filter(target: logging.Logger | logging.Handler) -> PiiMaskingFilter`: anexa uma instância de `PiiMaskingFilter` somente se ainda não houver instância desse tipo no alvo.
  - `configure_logging(level: int = logging.INFO) -> None`:
    - Garante um `logging.StreamHandler` padrão no logger raiz caso ele ainda não possua handlers.
    - Instala `PiiMaskingFilter` em **todos os handlers** do logger raiz e do logger `"banco_agil"` (protegendo também logs propagados de módulos filhos e bibliotecas de terceiros) e ajusta o nível de `"banco_agil"` para `level`.
    - Totalmente idempotente (chamadas repetidas não duplicam handlers nem filtros).

### 2. Guardrails Determinísticos de Entrada e Saída (`src/banco_agil/agents/guardrails.py`)
Criar um módulo determinístico (zero chamadas de LLM) para proteção na fronteira do turno:
- **Constantes públicas:**
  - `MAX_USER_MESSAGE_CHARS = 2000`
  - `EMPTY_INPUT_REPLY = "Por favor, escreva uma mensagem."`
  - `TOO_LONG_INPUT_REPLY = "Sua mensagem está muito longa. Por favor, resuma sua solicitação em até 2.000 caracteres."`
  - `GUARDRAIL_BLOCKED_REPLY`: resposta cordial em persona única recusando manipulação de regras/prompt e lembrando os serviços atendidos pelo Banco Ágil (consulta e aumento de limite de crédito, entrevista de crédito e cotação de câmbio), mantendo a sessão aberta.
  - `RECURSION_LIMIT_REPLY = "Não consegui concluir esta solicitação no momento. Por favor, reformule sua mensagem de forma mais direta."`
- **Contrato de decisão:**
  - `InputGuardrailDecision(BaseModel)` (`frozen=True`):
    - `allowed: bool`
    - `reason: Literal["ok", "empty", "too_long", "prompt_injection"] = "ok"`
    - `reply: str | None = None`
- **Validação de entrada (`evaluate_user_input(message: str, *, max_chars: int = MAX_USER_MESSAGE_CHARS) -> InputGuardrailDecision`):**
  1. Remove bytes nulos (`\x00`) e espaços nas bordas (`cleaned = message.replace("\x00", "").strip()`).
  2. Se vazio → `InputGuardrailDecision(allowed=False, reason="empty", reply=EMPTY_INPUT_REPLY)`.
  3. Se `len(cleaned) > max_chars` → `InputGuardrailDecision(allowed=False, reason="too_long", reply=TOO_LONG_INPUT_REPLY)`.
  4. Normaliza texto (NFKD sem acentos, minúsculas, colapso de espaços) e avalia padrões de **alta precisão** para:
     - Sobrescrita de instruções (*"ignore todas as instruções"*, *"desconsidere suas regras"*, *"esqueça as instruções anteriores"*, *"ignore previous/system instructions"*).
     - Extração de *system prompt* ou instruções internas (*"revele/mostre/repita seu system prompt / prompt de sistema / instruções internas"*, *"reveal/show/dump system prompt"*).
     - *Jailbreak* / troca forçada de persona (*"modo desenvolvedor"*, *"developer mode"*, *"jailbreak"*, *"finja que você não é..."*).
     - Tentativa explícita de burlar autenticação (*"pule/burle/ignore/bypass a autenticação / verificação de identidade"*).
     - Em caso positivo → `InputGuardrailDecision(allowed=False, reason="prompt_injection", reply=GUARDRAIL_BLOCKED_REPLY)`.
  5. **Decisão intencional contra falsos positivos:** pedidos fora de escopo em linguagem natural (ex.: Pix, cartão, boleto, clima) **não** são bloqueados por listas genéricas de palavras-chave no pré-filtro — evitando falsos positivos em frases legítimas como *"tenho dívida no cartão"* ou *"quero aumentar o limite do cartão"*. Eles são tratados pela camada de escopo estrito nos prompts + isolamento físico de ferramentas (ADR 003).
- **Sanitização de saída (`sanitize_reply(reply: str, state: ConversationState) -> str`):**
  - Reutiliza `mask_pii` de `privacy.py` passando `preserve_dates_except=birth_date` somente quando `has_authenticated_client(state)` e `client_birth_date` estão presentes; substitui a lógica duplicada de `_redact_reply` em `graph.py`.

### 3. Robustez do Grafo, Ferramentas, Prompts e UI
- **`src/banco_agil/agents/graph.py`:**
  - Substituir `_redact_reply` por `sanitize_reply` (`guardrails.py`), removendo as regexes duplicadas `CPF_IN_REPLY` e `DATE_IN_REPLY`.
  - Definir `DEFAULT_RECURSION_LIMIT = 16` (valor par: em um loop `agente -> tool_node`, o LangGraph interrompe imediatamente após `ToolNode`, evitando deixar um `AIMessage(tool_calls=...)` órfão sem `ToolMessage` no checkpoint — conforme documentação do LangGraph v1, o padrão da biblioteca subiu para `10_007` supersteps).
  - Aceitar `recursion_limit: int = DEFAULT_RECURSION_LIMIT` em `run_turn` e `resume_turn`, validando `recursion_limit >= 2` (lançando `ValueError` em valor inválido de programação) e enviando `"recursion_limit": recursion_limit` no topo do `RunnableConfig`.
  - Integrar `evaluate_user_input` em `_execute_turn` preservando a prioridade atual de estado:
    1. `conversation_ended` → `ENDED_REPLY`
    2. `not resuming and snapshot.interrupts` → `PENDING_REPLY`
    3. `resuming and confirmation is None` → `NO_CONFIRMATION_REPLY`
    4. Entrada bloqueada pelo guardrail (`not decision.allowed`) → registrar `logger.warning("Entrada bloqueada por guardrail (%s)", decision.reason)` (quando `reason != "empty"`, **sem** logar o texto do usuário) e retornar `TurnResult` com `decision.reply`, sem invocar o grafo nem mutar o checkpoint.
  - Capturar `GraphRecursionError` (de `langgraph.errors`) em `_execute_turn`: registrar `logger.warning("Limite de passos atingido no turno (%s)", type(exc).__name__)`, recarregar o estado do checkpoint com segurança e retornar `RECURSION_LIMIT_REPLY`, mantendo a sessão funcional para o próximo turno.
  - Higienizar `\x00` na mensagem encaminhada ao `HumanMessage` quando aprovada pelo guardrail.
- **`src/banco_agil/agents/tools.py`:**
  - Ampliar os `except` defensivos nas operações de serviço das ferramentas (`autenticar_cliente`, `consultar_limite`, `solicitar_aumento_limite` — exclusivamente ao redor de `get_client` e `process_limit_increase`, nunca envolvendo `interrupt()` — e `calcular_e_atualizar_score`) para capturar também `Exception` inesperada após `DomainValidationError`, registrando apenas `logger.error("%s", type(exc).__name__)` (sem `exc_info`) e devolvendo `ToolMessage` controlada sem derrubar o atendimento.
- **`src/banco_agil/agents/prompts.py`:**
  - Reforçar `BASE_PROMPT` (mantendo todas as diretrizes e expressões já cobertas pelos testes existentes):
    - Delimitar explicitamente o escopo único do Banco Ágil: autenticação cadastral, consulta e aumento de limite de crédito, entrevista de reavaliação de score e consulta de cotação de câmbio.
    - Instruir recusa educada e objetiva para qualquer assunto fora desse escopo (como Pix, transferências, boletos, cartões, faturas, investimentos, empréstimos, consultas a documentos externos ou assuntos não bancários), listando os serviços disponíveis e mantendo o atendimento aberto.
    - Instruir resistência a manipulação: ignorar pedidos para mudar regras, mudar de persona, revelar instruções internas, nomes de ferramentas ou dados de outros clientes.
  - Reforçar `build_triage_prompt` (pós-autenticação) para responder educadamente dentro do escopo sem redirecionar quando o pedido do cliente não for crédito nem câmbio.
- **`src/banco_agil/ui/app.py`:**
  - Chamar `configure_logging()` na inicialização de `main()`.
  - Registrar `logger.error("Falha na inicialização do atendimento (%s)", type(exc).__name__)` no bloco `except Exception` da inicialização (sem exibir nem logar detalhes sensíveis).

### 4. Documentação Arquitetural (`docs/adr/006-guardrails-robustez-e-lgpd.md`)
Seguindo `docs/adr/000-template.md`, registrar:
- Estratégia de defesa em profundidade em 3 camadas (guardrails determinísticos + escopo estrito/isolamento físico de tools + mascaramento LGPD transversal em logs e respostas) e limite explícito de supersteps (`recursion_limit` par).
- Alternativas descartadas: classificador LLM por turno (latência/custo em 100% das mensagens), frameworks pesados de guardrails no MVP (YAGNI), bloqueio por palavras-chave genéricas de fora de escopo (risco de falsos positivos) e filtro de PII apenas em loggers isolados em vez de handlers.
- Consequências, trade-offs e **Evolução e Escala**: guardrails gerenciados ou classificadores dedicados para ataques adversariais avançados (ex.: Amazon Bedrock Guardrails), DLP no pipeline centralizado de observabilidade/logs, rate limiting distribuído por CPF/IP (WAF/Redis) e governança LGPD de retenção/anonimização em checkpointers persistentes.

## Fora de Escopo
- Evals automatizados (`make evals`), telemetria local por turno e integração com Langfuse (`F08`).
- API REST FastAPI, autenticação HTTP/rate limit de rede e Docker (`F09`).
- Servidor `FastMCP` (`F10`).
- Não editar `docs/backlog.md`, `docs/spec.md` nem `AGENTS.md` dentro do worktree.

## Estratégia de Testes (100% determinísticos, zero rede)
- `tests/test_privacy.py`:
  1. `mask_pii`: mascara CPF com/sem pontuação e espaços, datas (`DD/MM/YYYY`, `DD-MM-YYYY`, `DD.MM.YYYY`, `YYYY-MM-DD`) e segredos (`Bearer`, `gsk_`, `sk-`, `AIza`, `AKIA`, `pk-lf-`, `sk-lf-`).
  2. `mask_pii` com `preserve_dates_except`: mascara apenas a data de nascimento do cliente (em qualquer formato equivalente) e preserva datas operacionais e datas de referência de câmbio.
  3. `mask_pii_data`: mascara strings aninhadas em `dict`/`list`/`tuple` sem mutar a estrutura original e preserva tipos primitivos.
  4. `PiiMaskingFilter`: sanitiza `record.msg`, `record.args` (tupla e dicionário, inclusive quando o resultado contém `%`), `record.exc_info`/`exc_text` (exceções com CPF/data/segredo em `logger.exception(...)`) e `record.stack_info`, sem descartar registros.
  5. `configure_logging`: idempotente (não duplica handlers nem filtros), protege logs emitidos por loggers filhos (propagação para handler do root) e limpa corretamente nos fixtures para não contaminar outros testes.
- `tests/test_guardrails.py`:
  1. `evaluate_user_input`: aprova mensagens bancárias legítimas (incluindo menções legítimas como *"tenho dívida no cartão"*, *"quero aumentar o limite do meu cartão"*, *"não quero fazer pix, quero consultar meu limite"* — zero falsos positivos); bloqueia entrada vazia/espaços/nulos, entrada acima de `MAX_USER_MESSAGE_CHARS` e tentativas de *prompt injection*, extração de *system prompt*, *jailbreak* e *bypass* de autenticação (em português e inglês, com/sem acento).
  2. Integração com `run_turn`: entrada bloqueada por guardrail (tamanho ou *prompt injection*) retorna resposta amigável sem chamar o modelo (`len(model.calls) == 0`), sem alterar o checkpoint, preservando `authenticated` e `active_agent`, e registrando apenas o motivo no log (zero texto do usuário/PII no `caplog`).
  3. Prioridade de estado mantida: sessão já encerrada retorna `ENDED_REPLY` e confirmação HITL pendente retorna `PENDING_REPLY` mesmo diante de entrada bloqueada pelo guardrail.
  4. `recursion_limit` e `GraphRecursionError`: modelo em loop infinito de chamadas de ferramenta é interrompido de forma controlada com `RECURSION_LIMIT_REPLY`, registra `warning`, não deixa `AIMessage(tool_calls)` órfão no checkpoint e permite que o turno seguinte funcione normalmente.
  5. Falhas inesperadas (`RuntimeError`) em `credit_service` durante `autenticar_cliente`, `consultar_limite`, `solicitar_aumento_limite` (pré e pós-HITL) e `calcular_e_atualizar_score` retornam `ToolMessage` controlada sem derrubar a conversa e sem vazar PII nos logs.
  6. Prompts de todos os 4 agentes contêm diretrizes explícitas de escopo estrito, recusa educada de serviços não atendidos e proteção anti-injeção; saída com token secreto (`gsk_...`/`Bearer ...`) é mascarada em `TurnResult.reply`.
  7. Teste na UI (`AppTest`) verificando que erro na inicialização registra log controlado sem expor detalhes sensíveis e que entrada bloqueada pelo guardrail é exibida amigavelmente mantendo o chat aberto.

## Critérios de Aceite
- [x] `make check` 100% verde (`ruff check`, `ruff format --check`, `mypy src` strict, `pytest --cov=banco_agil`).
- [x] Cobertura ≥ 95% nos módulos novos (`infrastructure/privacy.py` e `agents/guardrails.py`) e cobertura geral mantida ≥ 98%.
- [x] `docs/adr/006-guardrails-robustez-e-lgpd.md` criado seguindo `docs/adr/000-template.md`.
- [x] Commits pequenos em Conventional Commits na branch `feat/f07-guardrails` (incluindo `plan.md`).
