# ADR 004 — Agente de Crédito e confirmação antes de gravar pedidos

- **Status:** Aceito
- **Data:** 2026-11-21
- **Feature:** F04

## Contexto

O Agente de Crédito consulta o cadastro e usa o `CreditService` para avaliar pedidos de
aumento. O envio de um pedido gera um registro formal e, se aprovado para o score atual,
altera o limite do cliente. A autorização do cliente precisa anteceder esses efeitos.

## Decisão

A ferramenta `solicitar_aumento_limite` usa `interrupt()` do LangGraph para pausar o grafo
antes de persistir qualquer pedido inicial ou após a entrevista. A interface apresenta
o limite atual e o valor desejado. Os botões de confirmação e cancelamento retomam a
mesma sessão por `Command(resume=...)`, usando o mesmo `thread_id`. Durante a pausa,
o chat fica desabilitado e novos turnos de texto preservam o checkpoint.

A execução anterior ao `interrupt()` é idempotente. A ferramenta lê o CPF autenticado
via `ToolRuntime`, consulta o limite atual e valida o valor desejado. O payload
contém a ação, os dois valores e uma mensagem, todos serializáveis em JSON.

Na retomada, o LangGraph reexecuta a etapa anterior à pausa. A persistência via
`CreditService` ocorre após uma resposta estrutural com `approved` igual ao booleano
`True`. O cancelamento encerra o pedido sem gravação. Um pedido confirmado e rejeitado
preserva o valor desejado na sessão para uma possível reavaliação após a entrevista.

### Pontos de confirmação no atendimento

| Fluxo | Tratamento no MVP |
| --- | --- |
| Envio de aumento de limite, inicial ou após entrevista | Confirmação explícita por `interrupt()` antes do registro formal e de uma eventual alteração do limite. |
| Consulta de limite e consulta de câmbio | Resposta direta de leitura. Uma pausa acrescentaria atrito a consultas de baixo impacto. |
| Autenticação | Coleta e verificação determinística dos dados, com limite de tentativas. Uma confirmação adicional repetiria a verificação de identidade. |
| Revisão de dados da entrevista de crédito | Uma recapitulação em conversa permite corrigir as respostas antes do cálculo. Uma pausa estruturada adicional facilitaria a revisão, ao custo de prolongar a entrevista e gerar fadiga antes da confirmação do pedido. |
| Handoffs e encerramento de tópico ou conversa | A conversa conduz essas transições conforme a intenção do cliente. |

## Alternativas Descartadas

- **Confirmação apenas no prompt:** a interpretação de uma resposta livre ficaria
  a cargo do modelo e poderia acionar a gravação sem uma autorização estruturada.
- **Máquina de estados própria para confirmação:** duplicaria a pausa e a retomada
  que o checkpointer do LangGraph já oferece.

## Consequências

- **Positivas:** o cliente revisa os valores antes do envio, os CSVs ficam intactos
  durante a pausa e o histórico continua no mesmo atendimento após a decisão.
- **Trade-offs / Riscos:** a pausa vive no checkpointer em memória e se perde quando
  a sessão em memória é descartada. A gravação dos CSVs segue a premissa de um único
  escritor local do [ADR 001](001-dominio-e-persistencia-csv.md). Uma falha entre o
  registro de um pedido aprovado e a atualização do cadastro pode deixar o limite
  anterior em vigor.

## Evolução e Escala

Separar a autorização do cliente da deliberação de um analista de risco. Aumentos
acima da alçada automática, sinais de fraude ou divergências cadastrais são candidatos
à revisão assíncrona pelo backoffice. Nessa evolução, um pedido pendente e seu
checkpoint ficam em armazenamento durável. Uma fila encaminha a revisão, e um webhook
autenticado ou um worker retoma o fluxo pela decisão vinculada ao identificador do
pedido. Persistência transacional, chaves de idempotência e trilha de auditoria
permitem processar reentregas e manter o histórico e o cadastro coerentes.
