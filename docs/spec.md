# Especificação Técnica — Banco Ágil

Fonte única da verdade para regras de negócio, contratos de dados e decisões de arquitetura do MVP do **Banco Ágil**.

---

## 1. Visão Geral

Sistema multiagente conversacional para atendimento digital ao cliente do **Banco Ágil**. Para o cliente, a experiência deve ser a de conversar com **um único assistente contínuo**, com transições implícitas entre agentes especializados.

**Princípio arquitetural:** regras de negócio, validações críticas e persistência residem em código determinístico; o LLM conduz a conversa e invoca ferramentas.

---

## 2. Agentes e Responsabilidades

1. **Agente de Triagem:** porta de entrada do atendimento. Recepciona o cliente, coleta CPF e data de nascimento, autentica contra `clientes.csv` e direciona para o especialista adequado **somente após autenticação bem-sucedida**. Após 3 falhas consecutivas de autenticação, informa de forma cordial e encerra o atendimento.
2. **Agente de Crédito:** consulta o limite de crédito disponível e processa solicitações de aumento de limite (registrando em `solicitacoes_aumento_limite.csv` e validando contra `score_limite.csv`). Em caso de rejeição, oferece redirecionamento ao Agente de Entrevista de Crédito.
3. **Agente de Entrevista de Crédito:** conduz entrevista conversacional estruturada coletando 5 indicadores financeiros, calcula o novo score (`0..1000`), atualiza `clientes.csv` e retorna ao Agente de Crédito.
4. **Agente de Câmbio:** consulta cotações de moedas em tempo real via API externa estruturada, apresenta a cotação ao cliente e encerra o tópico de cotação com mensagem amigável.

### Regras Globais
- **Encerramento global:** todos os agentes dispõem da ferramenta de encerramento para finalizar o loop de execução sempre que o cliente solicitar o fim da conversa.
- **Escopo estrito:** nenhum agente atua fora das suas responsabilidades definidas. Busca em base de conhecimento (RAG) está fora do escopo do MVP.
- **Transições invisíveis:** redirecionamentos entre agentes são implícitos, preservando uma persona única e o contexto da conversa.
- **Tratamento de erros:** falhas esperadas (leitura/escrita de CSV, indisponibilidade de API externa, entrada inválida) são tratadas de forma controlada, comunicadas com clareza ao cliente e registradas em log sem interromper abruptamente a interação.

---

## 3. Contratos de Dados (`data/`)

### Classificação de Dados Sensíveis (PII)
Os campos `cpf` e `data_nascimento` são tratados como dados pessoais sensíveis (LGPD) e **nunca** podem ser registrados em texto claro em logs ou traces de observabilidade.

### 3.1. `clientes.csv` *(fonte da verdade do cliente)*
| Coluna | Tipo | Exemplo | Regra |
| :--- | :--- | :--- | :--- |
| `cpf` | `str` (11 dígitos) | `12345678901` | Armazenado apenas com dígitos; entrada aceita com ou sem pontuação. |
| `nome` | `str` | `Ana Souza` | Utilizado na saudação contextual após autenticação. |
| `data_nascimento` | `str` (`YYYY-MM-DD`) | `1990-05-14` | Entrada aceita `DD/MM/AAAA` ou `YYYY-MM-DD`. |
| `limite_atual` | `float` | `2500.00` | Atualizado quando um pedido de aumento é `aprovado`. |
| `score` | `int` (`0..1000`) | `480` | Atualizado pelo Agente de Entrevista de Crédito. |

### 3.2. `score_limite.csv` *(tabela de faixas em degraus)*
| Coluna | Tipo | Exemplo | Regra |
| :--- | :--- | :--- | :--- |
| `score_minimo` | `int` | `0` | Degrau inferior inclusivo. Aplica-se a linha com o maior `score_minimo <= score`. |
| `limite_maximo` | `float` | `1000.00` | Teto de crédito permitido para a faixa. |

O modelo em degraus elimina por construção lacunas ou sobreposições entre faixas. Os dados iniciais devem ser calibrados para permitir testar o fluxo completo (`rejeitado` → `entrevista` → `aprovado`).

### 3.3. `solicitacoes_aumento_limite.csv` *(registro append-only de pedidos)*
| Coluna | Tipo | Exemplo | Regra |
| :--- | :--- | :--- | :--- |
| `cpf_cliente` | `str` | `12345678901` | CPF autenticado na sessão (proveniente exclusivamente do estado confiável). |
| `data_hora_solicitacao` | `str` (ISO 8601) | `2026-11-21T03:00:00-03:00` | Timestamp com timezone. |
| `limite_atual` | `float` | `2500.00` | Limite vigente no momento do pedido. |
| `novo_limite_solicitado` | `float` | `6000.00` | Deve ser estritamente maior que `limite_atual`. |
| `status_pedido` | `str` | `aprovado` | Enum canônico: `'pendente'`, `'aprovado'`, `'rejeitado'`. |

---

## 4. Regras de Negócio

### Autenticação
- Conta como tentativa de autenticação apenas um par bem-formado (CPF com 11 dígitos e data válida) sem correspondência em `clientes.csv`.
- Entradas mal formatadas solicitam correção sem consumir o contador de tentativas.
- A mensagem de falha deve ser genérica (*"dados não conferem"*), sem indicar qual campo falhou (prevenção de enumeração de CPFs).
- Na 3ª falha consecutiva na sessão, o atendimento é encerrado automaticamente com mensagem cordial.

### Consulta e Aumento de Limite
- `clientes.csv` é a fonte da verdade de `limite_atual` e `score`; `solicitacoes_aumento_limite.csv` é um registro de pedidos.
- Valores solicitados menores ou iguais ao `limite_atual` (ou negativos/zero) são tratados como entrada inválida no domínio (não gravam no CSV); a ferramenta retorna erro estruturado informando o limite atual.
- No domínio, o pedido nasce com status `'pendente'` e é avaliado de forma síncrona contra `score_limite.csv`: se `novo_limite_solicitado <= limite_maximo` da faixa do cliente, transita para `'aprovado'`; caso contrário, para `'rejeitado'`.
- Persistência no CSV é append-only (1 linha por pedido gravada já com o status final).
- Quando `'aprovado'`, o `limite_atual` em `clientes.csv` é atualizado. Quando `'rejeitado'`, `clientes.csv` permanece inalterado e o agente oferece a entrevista de crédito.

### Fórmula de Score de Crédito
Executada estritamente em código Python com pesos definidos como constantes no módulo de domínio:

```text
score_bruto = (renda_mensal / (despesas_fixas + 1)) * 30
            + peso_emprego[tipo_emprego]
            + peso_dependentes[num_dependentes]
            + peso_dividas[tem_dividas]

score_final = max(0, min(1000, round(score_bruto)))
```

- `peso_emprego`: `{"formal": 300, "autônomo": 200, "desempregado": 0}`
- `peso_dependentes`: `{0: 100, 1: 80, 2: 60, "3+": 30}`
- `peso_dividas`: `{"sim": -100, "não": 100}`
- Casos de borda conhecidos e cobertos por testes: `despesas_fixas == 0` (valor bruto excede 1000 → clamp em `1000`); desempregado + 3+ dependentes + dívidas + renda `0` (valor bruto `-70` → clamp em `0`).

### Retorno da Entrevista de Crédito
- O valor de limite anteriormente desejado é preservado no estado da sessão durante a entrevista.
- Ao concluir a entrevista e atualizar o score em `clientes.csv`, o controle retorna ao Agente de Crédito, que informa o novo score e propõe reenviar o pedido com o valor anterior (permitindo ao cliente confirmar ou informar outro valor).
- Confirmada a submissão, um **novo** pedido é registrado em `solicitacoes_aumento_limite.csv`.

### Encerramento de Tópico vs. Encerramento de Conversa
- Ao concluir um assunto específico (ex.: apresentar cotação de câmbio ou consulta de limite), o agente encerra apenas o tópico com mensagem amigável, pergunta se pode ajudar em algo mais e devolve o controle à Triagem (mantendo o cliente autenticado).
- A conversa inteira só é finalizada quando o cliente indica que deseja encerrar (acionando a ferramenta global de encerramento) ou após a 3ª falha consecutiva de autenticação.

### Consulta de Câmbio
- Provedor primário: **AwesomeAPI** (cotação estruturada em BRL, sem necessidade de chave).
- Fallback automático em caso de timeout ou indisponibilidade: **Frankfurter API (BCE)**, informando ao cliente a data de referência da taxa.

---

## 5. Arquitetura do MVP

- **Orquestração:** `LangGraph` com estado tipado, persistência de sessão via checkpointer (`thread_id`) e topologia de handoffs com 4 nós explícitos, `ToolNode` isolado por agente e roteador condicional determinístico único (ADR 003).
- **Segurança de ferramentas:** ferramentas pós-autenticação obtêm o estado confiável da sessão (`authenticated`, `client_cpf`) exclusivamente via `ToolRuntime`, nunca de argumentos livres gerados pelo LLM.
- **Human-in-the-Loop (HITL):** uso de `interrupt()` nativo do LangGraph antes de gravar qualquer pedido de aumento de limite (inicial ou pós-entrevista), retomando via `Command(resume=...)` (ADR 004).
- **Camada de LLM:** `langchain-litellm` isolado em uma factory retornando `BaseChatModel`, permitindo trocar de provedor (Groq, Gemini, OpenAI, Anthropic, Amazon Bedrock) via variáveis de ambiente e obter estimativa de custo por token.
- **Observabilidade:** telemetria local sempre ativa por turno (latência, tokens, custo estimado em USD, agente ativo) exibida na UI, combinada com integração opcional ao **Langfuse** via `.env` (com mascaramento de PII).
- **Exposição e UI:** interface conversacional em **Streamlit** (modo direto em memória e modo cliente HTTP), API REST assíncrona em **FastAPI** e empacotamento com **Docker**.
- **MCP:** servidor `FastMCP` para consulta de câmbio (stateless, sem PII) consumido pelo Agente de Câmbio via `langchain-mcp-adapters`; ferramentas autenticadas permanecem locais com `InjectedState`.

---

## 6. Limites do MVP e Caminhos de Escala

Escolhas intencionais para manter o MVP enxuto e seus caminhos naturais de evolução quando houver aumento de volume, concorrência ou criticidade:

| Dimensão | Abordagem no MVP | Evolução para Escala |
| :--- | :--- | :--- |
| **Persistência** | Arquivos CSV locais atrás de interfaces de repositório | Banco relacional transacional (ex.: PostgreSQL/RDS) com bloqueio otimista e auditoria |
| **Avaliação de crédito** | Síncrona e determinística na requisição (status `pendente` transitório em domínio) | Fila assíncrona (SQS/Kafka) + motor de crédito/antifraude com status `pendente` persistido e notificação posterior |
| **Proteção contra força bruta** | Contador de 3 tentativas no estado da sessão | Rate limit e bloqueio temporário distribuído por CPF/IP (Redis/WAF) |
| **Gateway de LLM** | LiteLLM embutido na aplicação | LiteLLM Proxy centralizado com gestão de chaves, rate limit por serviço, fallback multi-região e billing |
| **Memória de sessão** | Checkpointer em memória (`InMemorySaver`) | Checkpointer persistente (Postgres/DynamoDB) para múltiplas réplicas stateless |

---

## 7. Spikes em Aberto

Nenhum spike estrutural em aberto — decisões de domínio, LLM, topologia multiagente e pontos HITL registradas em `docs/adr/001` a `docs/adr/004`.
