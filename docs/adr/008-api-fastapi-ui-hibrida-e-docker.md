# ADR 008 — API FastAPI, UI híbrida e Docker

- **Status:** Aceito
- **Data:** 2026-11-21
- **Feature:** F09

## Contexto

O Banco Ágil precisa oferecer o mesmo atendimento em uma interface web e por
integração HTTP. Ambos os canais precisam aplicar as regras de autenticação,
confirmação explícita de aumento de limite e privacidade do MVP. A equipe precisa
subir os dois serviços com a mesma versão do código e persistir os CSVs entre
reinícios de contêiner.

## Decisão

- Expor `GET /health` e `POST /api/v1/chat` via FastAPI. O `lifespan` inicializa o
  grafo, o serviço de crédito, a configuração e a observabilidade e descarrega os
  traces no encerramento. A API gera um UUID v4 para novas conversas sem identificador.
- Executar o grafo síncrono com `asyncio.to_thread`. Um lock por `thread_id` serializa
  turnos e retomadas da mesma conversa. Sessões distintas podem progredir em
  paralelo. O serviço de crédito compartilha um lock para serializar alterações
  nos CSVs dentro do processo.
- Exigir um booleano estrito na retomada. Respostas de validação incluem o tipo e a
  localização conhecida do erro, com mensagens controladas. Falhas inesperadas
  registram somente a classe da exceção.
- Com `API_BASE_URL` vazia, o Streamlit executa o grafo em memória. Com a URL
  configurada, a interface chama a API e atualiza a sessão a partir da resposta.
  Uma falha de comunicação preserva a confirmação exibida para permitir nova
  tentativa.
- Reutilizar uma imagem Docker multi-stage nos dois serviços, executados pelo usuário
  `appuser`. O Compose monta `./data` na API e inicia a UI depois que `/health`
  responde. O diretório montado precisa permitir gravação pelo usuário do contêiner
  (UID 1000 em Linux).
- Rodar a API com `--workers 1` no MVP. O `InMemorySaver` pertence a um único
  processo e a persistência em arquivos locais pressupõe um único escritor,
  conforme os ADRs 001 e 002.

## Alternativas Descartadas

- **Atendimento acoplado ao Streamlit.** Uma integração externa precisaria acionar
  a interface ou repetir a lógica do atendimento.
- **Múltiplos workers com estado em memória.** Retomadas poderiam chegar a um
  processo sem o checkpoint da conversa. Os processos também disputariam os CSVs.
- **Duas imagens Docker.** Uma imagem por serviço adicionaria builds e versões para
  coordenar enquanto os dois usam o mesmo pacote.

## Consequências

- **Positivas.** A API e a interface compartilham as regras de negócio. A UI pode
  ser executada localmente ou conectada ao serviço HTTP já em execução. O volume
  preserva os pedidos registrados quando o contêiner é recriado.
- **Trade-offs e riscos.** Checkpoints e locks por conversa permanecem em memória
  até o processo ser encerrado. O HTTP do MVP pressupõe um ambiente de acesso
  controlado. A autenticação no chat associa o cliente à conversa, e quem tiver
  acesso ao identificador pode retomar a sessão. Uma falha após o registro do pedido
  e antes de atualizar o cadastro continua sujeita ao limite descrito no ADR 004.

## Evolução e Escala

- Adotar um checkpointer persistente em PostgreSQL ou DynamoDB e migrar os CSVs
  para um banco relacional transacional que confirme o pedido e a mudança do
  cadastro em uma única transação.
- Rodar múltiplas réplicas stateless em ECS Fargate ou EKS atrás de um ALB quando
  houver necessidade de maior disponibilidade ou capacidade. Coordenar turnos
  concorrentes de uma sessão no armazenamento compartilhado.
- Vincular a sessão a uma identidade autenticada por OAuth2/JWT e aplicar rate
  limiting distribuído com WAF ou Redis antes da exposição pública.
- Adotar SSE para respostas parciais ou WebSocket para interação bidirecional
  quando a latência percebida justificar streaming.

## Atualização (F12/F13)

- **Data:** 2026-11-22

O `lifespan` da API passou a delegar a composição do grafo, serviço de crédito,
câmbio e observabilidade ao `ConversationRuntime.from_settings` (F12), mantendo o
controle de concorrência assíncrona em `ApiRuntimeState`. Na UI Streamlit (F13), os
modos direto e HTTP foram unificados atrás do protocolo `ChatClient`
(`LocalChatClient` e `HttpChatClient`), deixando `ui/app.py` como camada de
apresentação pura.
