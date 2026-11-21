# ADR 006 — Guardrails, robustez e LGPD

- **Status:** Aceito
- **Data:** 2026-11-21
- **Feature:** F07

## Contexto

O Banco Ágil recebe CPF e data de nascimento no chat para autenticar clientes. Mensagens livres podem tentar alterar o comportamento do assistente. Respostas do modelo e exceções de bibliotecas podem expor dados pessoais ou credenciais. O MVP precisa manter o atendimento disponível e preservar as decisões de autenticação e de confirmação de pedidos mesmo quando uma chamada falha.

## Decisão

Adotar três camadas de defesa:

1. **Entrada do turno.** Higienizar bytes nulos, limitar o texto a 2.000 caracteres e detectar instruções explícitas de manipulação antes de chamar o modelo. A sessão encerrada e a confirmação pendente têm prioridade. Uma recusa preserva o checkpoint e registra somente o motivo do bloqueio.
2. **Escopo e ferramentas.** Todos os prompts delimitam os serviços e orientam a recusa cordial de pedidos fora do escopo. Cada agente recebe apenas as suas ferramentas, conforme o ADR 003. O grafo valida a autenticação para acessar especialistas. A solicitação de aumento exige confirmação do cliente antes de qualquer gravação. Falhas de serviço retornam mensagens controladas e registram apenas a classe da exceção.
3. **Mascaramento compartilhado.** Uma única função mascara CPF, datas e credenciais conhecidas. Os handlers do logger raiz e do Banco Ágil aplicam o mascaramento estrito às mensagens, exceções e pilhas. Nas respostas de uma sessão autenticada com nascimento verificado, a comparação pela data normalizada permite preservar datas operacionais, inclusive a referência do BCE.

Cada execução do grafo tem um orçamento padrão de 16 supersteps. O valor par permite concluir um par agente–ferramentas em um turno iniciado pelo cliente. Ao atingir o limite, o grafo recupera o estado do checkpoint e pede ao cliente que reformule a solicitação. Uma retomada de confirmação ou um limite ímpar pode parar antes de executar um lote de ferramentas. Nesse caso, o grafo remove do histórico ativo apenas a mensagem do lote ainda por executar e preserva os resultados já registrados.

## Alternativas Descartadas

- **Classificador LLM a cada turno.** Acrescentaria custo e latência a todas as mensagens.
- **Framework completo de guardrails no MVP.** Adicionaria dependências e configuração antes de haver necessidade operacional comprovada.
- **Bloqueio por palavras-chave de serviços fora do escopo.** Poderia barrar frases legítimas como “tenho dívida no cartão”.
- **Filtros em loggers isolados.** Perderiam registros de loggers descendentes, que chegam aos handlers por propagação.

## Consequências

- **Positivas.** Reduzimos a exposição incidental de dados nos logs e nas respostas, limitamos a duração de loops e mantemos a autenticação e a confirmação sob controle determinístico.
- **Trade-offs e riscos.** As expressões reconhecem padrões conhecidos. Ataques com formulações novas podem passar pela primeira camada. Os handlers configurados cobrem os logs que chegam a eles. Exportadores e handlers independentes precisam aplicar o mesmo mascaramento. Os checkpoints em memória ainda contêm dados necessários à conversa.

## Evolução e Escala

- Avaliar guardrails gerenciados, como Amazon Bedrock Guardrails, ou um classificador dedicado se os testes adversariais e as ocorrências de produção mostrarem lacunas recorrentes.
- Aplicar DLP no pipeline centralizado de logs e traces antes de exportar dados. Usar métricas de bloqueio, falsos positivos e saturação de supersteps para calibrar as regras.
- Aplicar rate limiting distribuído por CPF e IP com WAF ou Redis quando o atendimento for exposto por API ou distribuído entre réplicas. O contador atual atua por sessão.
- Antes de adotar um checkpointer persistente, definir prazos de retenção, critérios de anonimização, controles de acesso e rotinas de exclusão para os dados das conversas.
