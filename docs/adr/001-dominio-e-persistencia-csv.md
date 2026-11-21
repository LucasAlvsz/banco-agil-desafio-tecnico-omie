# ADR 001 — Domínio determinístico e persistência CSV

- **Status:** Aceito
- **Data:** 2026-11-21
- **Feature:** F01

## Contexto

As próximas features precisam de contratos de crédito independentes de LLM e rede,
com validação determinística, dados iniciais reproduzíveis e persistência local.

## Decisão

Modelos Pydantic v2 imutáveis representam os contratos em português. Normalizadores,
fórmula e avaliação ficam no domínio; interfaces `Protocol` desacoplam o serviço dos
repositórios CSV, implementados com a biblioteca padrão. Erros de persistência usam
mensagens genéricas, sem CPF ou nascimento, e suprimem a cadeia da exceção original.

`clientes.csv` é a fonte da verdade de score e limite. Atualizações reescrevem os
clientes em arquivo temporário no mesmo diretório, com flush/fsync e `os.replace`.
A tabela de crédito usa degraus: maior `score_minimo` menor ou igual ao score,
independentemente da ordem das linhas. Ausência de faixa compatível gera erro.

A fórmula usa os pesos da spec, arredondamento Python e clamp entre 0 e 1000 antes
do arredondamento (equivalente nos limites inteiros, evitando arredondar infinito
produzido por overflow com entradas finitas). CPF valida formato, sem dígito verificador.

Pedidos nascem pendentes no domínio e são avaliados sincronamente. O histórico é
append-only, com cinco colunas e timestamp ISO 8601 com timezone. O serviço registra
primeiro o resultado final e só então atualiza o limite do cliente quando aprovado.
A entrevista recalcula e persiste o score; uma nova avaliação gera outro pedido.

## Alternativas Descartadas

- **Regras no LLM:** não garantem validação e decisão reproduzíveis.
- **Banco relacional imediato:** infraestrutura adicional desnecessária para o MVP local.
- **Intervalos com mínimo e máximo:** permitem lacunas e sobreposições evitadas pelos degraus.

## Consequências

- **Positivas:** testes isolados sem rede; contratos tipados; substituição da persistência
  sem mudar regras; falha na substituição preserva o cadastro original.
- **Trade-offs / Riscos:** não há bloqueio entre escritores nem transação entre arquivos.
  Se o append funcionar e a atualização do cadastro falhar, o histórico terá um pedido
  aprovado cujo limite não foi atualizado; o serviço propaga o erro de persistência.
  CSVs são revalidados integralmente; seu custo cresce com o histórico. Escrita atômica
  do cadastro não torna o append imune a interrupção durante a gravação.

## Evolução e Escala

O MVP pressupõe um único escritor local. Antes de múltiplos processos ou maior
criticidade financeira, migrar as implementações dos Protocols para banco relacional
transacional, com controle de concorrência, idempotência e auditoria. Histórico e
cadastro devem ser gravados na mesma transação. Volume crescente exige índices e
consulta incremental; avaliação assíncrona futura pode persistir o estado pendente.
