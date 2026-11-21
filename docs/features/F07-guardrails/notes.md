# Notas — F07: Guardrails, robustez e LGPD

## Entregue

- Mascaramento compartilhado de CPF, datas e credenciais nos handlers de logging configurados e nas respostas do chat. Sessões autenticadas com data de nascimento verificada preservam datas operacionais, incluindo a referência de câmbio do BCE.
- Validação de tamanho e bloqueio de tentativas explícitas de manipulação antes da chamada ao modelo. O bloqueio preserva o estado da sessão. Os quatro agentes recebem instruções de escopo estrito.
- Limite padrão de 16 supersteps, recuperação pelo checkpoint e remoção do lote de ferramentas ainda por executar quando o limite interrompe o turno nesse ponto. A recuperação preserva os resultados de ferramentas já executadas.
- Tratamento de falhas inesperadas nas ferramentas de autenticação e crédito e na inicialização da UI. Os logs registram a classe da falha.
- Estratégia e caminhos de escala registrados no [ADR 006](../../adr/006-guardrails-robustez-e-lgpd.md).

## Verificação

- `make check` aprovado com lint, formatação, mypy estrito e 348 testes determinísticos.
- Cobertura geral de 99% conforme o relatório do pytest. `privacy.py` e `guardrails.py` atingiram 100%.
