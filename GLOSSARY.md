# Banco Ágil

Atendimento digital conversacional do Banco Ágil para triagem e autenticação de clientes, consulta e aumento de limite de crédito, entrevista de recálculo de score e consulta de cotações de câmbio.

## Atendimento

**Atendimento**:
Interação contínua de um Cliente com o Banco Ágil, do contato inicial até o encerramento definitivo, percebida como uma conversa única mesmo quando envolve diferentes especialistas.
_Avoid_: sessão, chamado, ticket, chat

**Turno**:
Uma rodada de interação dentro de um Atendimento, composta por uma mensagem ou decisão do Cliente seguida pela resposta do assistente.
_Avoid_: requisição, chamada, passo

**Cliente**:
Titular cadastrado no Banco Ágil, identificado por CPF e data de nascimento, com Limite de Crédito vigente e Score de Crédito associado.
_Avoid_: usuário, conta, correntista

**Triagem**:
Etapa inicial do Atendimento que recepciona o Cliente, conduz a Autenticação e encaminha a conversa para o especialista adequado.
_Avoid_: recepção, roteador

**Autenticação**:
Verificação da identidade do Cliente com CPF e data de nascimento válidos contra o cadastro, limitada a três falhas consecutivas por Atendimento.
_Avoid_: login, validação de cadastro

**Encerramento de Tópico**:
Conclusão amigável de um assunto específico que mantém o Atendimento aberto e o Cliente autenticado para novas solicitações.
_Avoid_: fim de atendimento, logout

**Encerramento de Atendimento**:
Finalização definitiva do Atendimento, a pedido do Cliente ou automaticamente após a terceira falha consecutiva de Autenticação.
_Avoid_: cancelamento, saída

## Crédito

**Limite de Crédito**:
Valor máximo de crédito atualmente disponível para o Cliente.
_Avoid_: saldo, teto da faixa

**Score de Crédito**:
Pontuação numérica inteira de `0` a `1000` atribuída ao Cliente a partir dos seus indicadores financeiros.
_Avoid_: nota, rating, pontuação genérica

**Faixa de Score**:
Degrau mínimo de Score de Crédito que define o limite máximo elegível para aprovação de aumento.
_Avoid_: categoria, tier, tabela de limite

**Solicitação de Aumento de Limite**:
Pedido formal de elevação do Limite de Crédito para um valor estritamente superior ao vigente, registrado com status `pendente`, `aprovado` ou `rejeitado`.
_Avoid_: proposta, requisição de crédito

**Confirmação de Aumento**:
Aprovação ou cancelamento explícito do Cliente antes que uma Solicitação de Aumento de Limite seja registrada e avaliada.
_Avoid_: aceite, aprovação bancária

**Entrevista de Crédito**:
Coleta estruturada de cinco indicadores financeiros do Cliente (renda mensal, tipo de emprego, despesas fixas, número de dependentes e existência de dívidas) para recalcular o Score de Crédito.
_Avoid_: questionário, formulário

## Câmbio e Governança

**Cotação de Câmbio**:
Taxa de conversão de uma moeda estrangeira para real brasileiro (BRL) obtida de fonte externa de mercado ou de taxa de referência.
_Avoid_: preço da moeda, conversão

**Dados Pessoais Sensíveis (PII)**:
Informações que identificam o Cliente — especificamente CPF e data de nascimento — protegidas contra exposição em registros e telemetria.
_Avoid_: metadados, credenciais

**Telemetria de Turno**:
Resumo operacional de um Turno (latência, consumo de tokens, custo estimado, rota de especialistas e ferramentas acionadas), sempre livre de Dados Pessoais Sensíveis.
_Avoid_: log, trace bruto
