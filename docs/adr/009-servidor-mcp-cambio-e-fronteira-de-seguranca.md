# ADR 009 — Servidor MCP de câmbio e fronteira de segurança

- **Status:** Aceito
- **Data:** 2026-11-21
- **Feature:** F10

## Contexto

O Banco Ágil oferece cotações em reais a clientes autenticados. O cálculo e
a origem da cotação vêm de provedores externos, conforme o ADR 005. A consulta
pode ser operada em um serviço próprio com um contrato menor que o do
atendimento bancário.

## Decisão

| Fronteira | Ferramentas e dados |
| --- | --- |
| Servidor MCP | `consultar_cotacao_cambio` recebe o campo opcional `moeda` e devolve uma cotação estruturada em BRL. A AwesomeAPI fornece a cotação primária e a Frankfurter fornece a referência do BCE no fallback. |
| Grafo local com `ToolRuntime` | `autenticar_cliente`, `consultar_limite`, `solicitar_aumento_limite` com `interrupt()`, `calcular_e_atualizar_score`, handoffs e `encerrar_atendimento`. A ferramenta local `consultar_cotacao` também verifica a autenticação antes de consultar o serviço. |

O servidor usa `MCPServer` do SDK MCP 2.3.0. A API e a UI local usam o
`Client` do mesmo SDK por trás de `ExchangeRateProvider`. O cliente descobre
a ferramenta por sessão, revalida a resposta como `ExchangeQuote` e converte
erros MCP em erros de domínio. O transporte padrão usa o protocolo em memória.
`stdio` está disponível para uso local e o Docker Compose usa Streamable HTTP
com respostas JSON e tratamento stateless.

O estado confiável, incluindo a identidade ligada à sessão, fica em
`ToolRuntime` conforme o ADR 003. CPF, data de nascimento e identificadores
de sessão ficam no atendimento local. O cliente substitui entradas que contêm
dados sensíveis detectados pelo mascaramento antes de enviar o campo `moeda`.
A confirmação de crédito e sua retomada ficam sob controle do mesmo grafo.

A imagem compartilhada exclui os CSVs do contexto de build. O Compose monta
`./data` na API e fornece ao serviço de câmbio apenas a configuração de
timeout. O serviço usa a rede do Compose sem publicar uma porta no host.
A lista explícita de hosts permitidos inclui o nome e a porta do serviço e
mantém ativa a proteção contra DNS rebinding.

## Alternativas Descartadas

- **Ferramentas bancárias remotas sem OAuth e escopos.** Exigiriam propagar identidade ao servidor, ampliariam o acesso a dados bancários e distribuiriam o controle da confirmação de crédito.
- **Ferramenta remota vinculada diretamente ao agente.** O grafo perderia a verificação de autenticação na ferramenta e a aplicação passaria a depender do formato bruto para informar a cotação.
- **`langchain-mcp-adapters` 0.3.x.** A versão 0.3.1 importa símbolos removidos na v2 do SDK MCP e a 0.3.2 declara `mcp<2`. O `Client` oficial cobre os três transportes no mesmo processo da aplicação. [Incompatibilidade de importação](https://github.com/langchain-ai/langchain-mcp-adapters/issues/589) · [Dependências da versão 0.3.2](https://pypi.org/pypi/langchain-mcp-adapters/0.3.2/json).
- **Transporte SSE legado.** Streamable HTTP oferece o endpoint único usado pelo Compose e atende uma consulta com resposta JSON.

## Consequências

- **Positivas.** O serviço de câmbio pode reiniciar e escalar separadamente da API. Testes e avaliações exercitam o protocolo em memória com respostas simuladas dos provedores.
- **Trade-offs e riscos.** Cada consulta abre uma sessão MCP e descobre a ferramenta. A API depende da disponibilidade do serviço de câmbio no Compose. O HTTP pressupõe uma rede controlada, como no ADR 008. Ao executar a API diretamente com `docker run`, forneça o diretório de dados por volume.

## Evolução e Escala

- Quando o volume justificar, reutilizar conexões HTTP e avaliar cache distribuído por moeda, com TTL de 30–60 segundos, e circuit breaker para provedores indisponíveis, conforme o ADR 005.
- Quando outros domínios precisarem de ferramentas bancárias remotas, adotar MCP Auth com OAuth 2.1 e escopos por ferramenta. Validar emissor, audiência e escopos dos tokens e vincular o cliente à identidade verificada no servidor. Projetar a retomada do HITL junto com o armazenamento persistente do grafo.
- Avaliar autenticação entre serviços e mTLS quando o tráfego cruzar redes de confiança distintas.

## Atualização (F15)

- **Data:** 2026-11-22

O protocolo de fio, o servidor MCP, o cliente `McpExchangeRateProvider` e a factory `create_exchange_provider` foram colocalizados em `src/banco_agil/mcp/exchange.py` com fachada estreita em `banco_agil.mcp`, reduzindo `exchange_server.py` a entrypoint CLI fino e centralizando a validação de `MCP_EXCHANGE_URL` em `Settings.resolve_mcp_exchange_url()`.
