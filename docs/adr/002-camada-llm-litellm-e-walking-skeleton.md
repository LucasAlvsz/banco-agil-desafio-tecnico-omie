# ADR 002 — Camada LLM com LiteLLM e walking skeleton

- **Status:** Aceito
- **Data:** 2026-11-21
- **Feature:** F02

## Contexto

Precisamos conectar a interface conversacional ao modelo e validar ferramentas antes
de introduzir os agentes especializados. A troca de provedor não deve alterar o grafo,
e regras de negócio e encerramento precisam continuar sob controle determinístico.

## Decisão

- `Settings` usa `pydantic-settings`, lê `.env` com `extra="ignore"` e exige o
  formato explícito `provedor/modelo` (padrão `groq/openai/gpt-oss-120b`). Temperatura
  e timeout são validados. Credenciais opcionais usam `SecretStr`, e erros de validação
  omitem os valores de entrada. Não exibimos exceções do provedor na interface.
- `create_chat_model` concentra `ChatLiteLLM` e retorna `BaseChatModel`, com timeout
  configurável e duas retentativas. Exporta credenciais não vazias para o ambiente
  somente quando a variável correspondente não está preenchida. O grafo recebe o
  modelo por injeção e não importa LiteLLM.
- Usamos `StateGraph` do LangGraph v1 com um nó conversacional `assistant`,
  `ToolNode` para executar `encerrar_atendimento` e `Command(update=...)` para
  atualizar a flag de encerramento e inserir o `ToolMessage` com o ID injetado.
  O modelo fornece apenas o motivo; o ID da chamada não faz parte do schema público.
- Após executar a ferramenta, `assistant` chama o modelo sem tools para produzir a
  despedida. A entrada do grafo e `run_turn` bloqueiam novas chamadas ao modelo em
  sessões encerradas. Mesmo se a despedida falhar, a flag persistida mantém a sessão
  encerrada. `TurnResult` é imutável e expõe apenas texto e a flag de encerramento.
- `InMemorySaver` mantém o histórico por `thread_id`. Cada sessão Streamlit possui
  seu próprio grafo no `session_state`; **Nova conversa** troca o UUID e descarta
  o histórico e o grafo anteriores. A entrada fica desabilitada após o encerramento.
- Os testes usam um `BaseChatModel` fake determinístico, mas executam o grafo e o
  `ToolNode` reais. Bloqueamos sockets e DNS desde a coleta do pytest, desabilitamos
  tracing e usamos o mapa local de custos do LiteLLM para evitar consultas na
  importação. `AppTest` verifica o fluxo da UI sem subir um servidor externo.

## Alternativas Descartadas

- **SDK específico de cada provedor:** acoplaria a orquestração ao fornecedor e
  exigiria adaptar chamadas e ferramentas a cada troca.
- **Inferir o provedor pelo nome do modelo:** torna a configuração ambígua;
  exigimos o prefixo explícito suportado pelo LiteLLM.
- **Executar ferramentas e atualizar o estado manualmente:** duplicaria o protocolo
  de tool calling; usamos `ToolNode` e `Command` nativos.
- **Topologia multiagente, API HTTP e observabilidade detalhada nesta feature:**
  dependem de features posteriores e não são necessárias ao walking skeleton.

## Consequências

- **Positivas:** o mesmo grafo pode usar diferentes provedores ou um fake; o
  encerramento é verificável sem rede e não depende apenas do prompt. Segredos ficam
  mascarados nas representações de configuração e detalhes de falhas não chegam à UI.
- **Trade-offs / Riscos:** suporte a tools e comportamento dependem do modelo e do
  provedor, exigindo um smoke real separado. As duas retentativas podem ampliar a
  latência total além do timeout individual. Mensagens e checkpoints residem em
  memória, são perdidos ao reiniciar o processo e crescem com a conversa; este MVP
  não oferece recuperação após reinício nem sincronização entre réplicas.
- **Credenciais:** o ambiente tem escopo de processo; `.env` representa configuração
  do servidor, não credenciais individuais por usuário. `SecretStr` mascara a
  representação, mas não criptografa valores em memória. Não registrar prompts,
  mensagens, chaves nem exceções brutas com possíveis dados pessoais.

## Evolução e Escala

Quando houver múltiplas réplicas ou necessidade de recuperação, substituir
`InMemorySaver` por checkpointer persistente (Postgres/DynamoDB), com isolamento por
usuário autenticado, política de retenção e proteção de dados. Para conversas longas,
adotar limites e compactação do histórico conforme a janela de contexto do modelo.

Migrar o LiteLLM embutido para **LiteLLM Proxy** quando gestão centralizada de chaves,
rate limits, fallback entre provedores e controle de custos justificarem um gateway.
O contrato `BaseChatModel` permanece na fronteira do grafo. Concorrência por sessão
exigirá serialização dos turnos e armazenamento compartilhado; não fazemos essa
promessa com o estado local do Streamlit.

### Validação prática com Groq (fora do pytest)

1. Copiar `.env.example` para `.env` (ignorado pelo Git), preencher `GROQ_API_KEY` e
   manter `LLM_MODEL=groq/openai/gpt-oss-120b`.
2. Executar `make ui`, enviar uma saudação e confirmar a resposta conversacional.
3. Pedir explicitamente para encerrar. Confirmar a despedida, o aviso de atendimento
   encerrado e a entrada desabilitada; o encerramento ocorre pela ferramenta ligada
   com `bind_tools()`, não pela interpretação de texto da resposta na UI.
4. Clicar em **Nova conversa** e confirmar que o histórico foi limpo e a entrada
   voltou a ficar disponível.

A suíte determinística não comprova disponibilidade, credenciais nem compatibilidade
real de tool calling na Groq. Essa verificação requer uma chave local válida e não
deve ser adicionada ao pytest.

### Evidência local desta implementação

- `make check`: Ruff, formatação e mypy strict aprovados; 137 testes passaram.
- Cobertura: 100% em `config.py`, `llm_factory.py` e `agents/`; 92% no pacote.
  A UI foi exercitada por quatro testes `AppTest`, embora a execução do script pelo
  Streamlit não tenha sido contabilizada no relatório de cobertura de linhas.
- `make ui`: servidor iniciado e `/_stcore/health` respondeu HTTP 200 (`ok`).
- Smoke com Groq real **pendente**: não havia `GROQ_API_KEY` configurada nem `.env`
  neste worktree. O encerramento via provedor real ainda precisa ser confirmado
  seguindo o procedimento acima; não é inferido a partir dos testes fake.
