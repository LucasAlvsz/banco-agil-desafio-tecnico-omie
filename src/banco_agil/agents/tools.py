import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from math import isfinite
from typing import Any, Literal, cast

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command, interrupt
from pydantic import StrictBool, StrictFloat, StrictInt

from banco_agil.agents.state import ConversationState, has_authenticated_client
from banco_agil.domain.errors import (
    ClientNotFoundError,
    DomainError,
    DomainValidationError,
    PersistenceError,
)
from banco_agil.domain.models import ExchangeQuote
from banco_agil.domain.repositories import ExchangeRateProvider
from banco_agil.domain.services import CreditService

logger = logging.getLogger(__name__)
MAX_AUTH_ATTEMPTS = 3


def _tool_result(
    runtime: ToolRuntime[None, ConversationState], content: str, **updates: object
) -> Command[Any]:
    if runtime.tool_call_id is None:
        raise ValueError("Identificador da ferramenta ausente")
    return Command(
        update={
            **updates,
            "messages": [ToolMessage(content=content, tool_call_id=runtime.tool_call_id)],
        }
    )


@tool
def encerrar_atendimento(
    motivo: str, runtime: ToolRuntime[None, ConversationState]
) -> Command[Any]:
    """Encerra o atendimento a pedido do cliente. Informe o motivo sem dados pessoais."""
    return _tool_result(runtime, "Atendimento encerrado.", conversation_ended=True)


def build_triage_tools(credit_service: CreditService) -> list[BaseTool]:
    @tool
    def autenticar_cliente(
        cpf: str, data_nascimento: str, runtime: ToolRuntime[None, ConversationState]
    ) -> Command[Any]:
        """Verifica a identidade do cliente pelo CPF e pela data de nascimento."""
        if has_authenticated_client(runtime.state):
            return _tool_result(runtime, "O cliente já está autenticado.")
        try:
            client = credit_service.authenticate(cpf, data_nascimento)
        except DomainValidationError:
            return _tool_result(
                runtime, "Informe um CPF com 11 dígitos e uma data válida no formato DD/MM/AAAA."
            )
        except PersistenceError as exc:
            logger.error("%s", type(exc).__name__)
            return _tool_result(runtime, "A autenticação está temporariamente indisponível.")

        if client is None:
            attempts = runtime.state.get("auth_attempts", 0) + 1
            if attempts >= MAX_AUTH_ATTEMPTS:
                return _tool_result(
                    runtime,
                    "Os dados não conferem.",
                    auth_attempts=attempts,
                    conversation_ended=True,
                )
            return _tool_result(runtime, "Os dados não conferem.", auth_attempts=attempts)

        return _tool_result(
            runtime,
            f"Cliente autenticado: {client.nome}.",
            authenticated=True,
            client_cpf=client.cpf,
            client_name=client.nome,
            client_birth_date=client.data_nascimento,
            auth_attempts=0,
        )

    return [autenticar_cliente, redirecionar_para_credito, redirecionar_para_cambio]


def build_credit_tools(credit_service: CreditService) -> list[BaseTool]:
    @tool
    def consultar_limite(runtime: ToolRuntime[None, ConversationState]) -> Command[Any]:
        """Consulta o limite de crédito disponível do cliente autenticado."""
        if not has_authenticated_client(runtime.state):
            return _tool_result(runtime, "Autentique o cliente primeiro.", active_agent="triagem")
        cpf = cast(str, runtime.state["client_cpf"])
        try:
            client = credit_service.get_client(cpf)
        except (ClientNotFoundError, PersistenceError) as exc:
            logger.error("%s", type(exc).__name__)
            return _tool_result(runtime, "A consulta de limite está temporariamente indisponível.")
        return _tool_result(runtime, f"Limite atual: R$ {client.limite_atual:.2f}.")

    @tool
    def solicitar_aumento_limite(
        novo_limite: float, runtime: ToolRuntime[None, ConversationState]
    ) -> Command[Any]:
        """Solicita um novo limite em reais e abre a confirmação do cliente."""
        if not has_authenticated_client(runtime.state):
            return _tool_result(runtime, "Autentique o cliente primeiro.", active_agent="triagem")
        cpf = cast(str, runtime.state["client_cpf"])
        try:
            client = credit_service.get_client(cpf)
        except DomainError as exc:
            logger.error("%s", type(exc).__name__)
            return _tool_result(runtime, "O pedido de aumento está temporariamente indisponível.")

        if not isfinite(novo_limite) or novo_limite <= 0 or novo_limite <= client.limite_atual:
            return _tool_result(
                runtime,
                json.dumps(
                    {
                        "erro": "O novo limite precisa ser maior que o limite atual.",
                        "limite_atual": client.limite_atual,
                    },
                    ensure_ascii=False,
                ),
            )

        decision = interrupt(
            {
                "action": "confirm_limit_increase",
                "limite_atual": client.limite_atual,
                "novo_limite_solicitado": novo_limite,
                "message": "Você confirma o pedido de aumento de limite?",
            }
        )
        if not isinstance(decision, dict) or decision.get("approved") is not True:
            return _tool_result(runtime, "Pedido de aumento de limite cancelado.")

        try:
            result = credit_service.process_limit_increase(cpf, novo_limite)
        except DomainError as exc:
            logger.error("%s", type(exc).__name__)
            return _tool_result(runtime, "Não foi possível concluir o pedido de aumento de limite.")
        if result.aprovado:
            return _tool_result(
                runtime,
                f"Pedido aprovado. Novo limite vigente: R$ {novo_limite:.2f}.",
                pending_requested_limit=None,
            )
        return _tool_result(
            runtime,
            "Pedido registrado como rejeitado para o score atual. "
            "Ofereça a entrevista de crédito para reavaliar o score.",
            pending_requested_limit=novo_limite,
        )

    return [consultar_limite, solicitar_aumento_limite, redirecionar_para_entrevista]


def build_interview_tools(credit_service: CreditService) -> list[BaseTool]:
    @tool
    def calcular_e_atualizar_score(
        renda_mensal: StrictFloat,
        tipo_emprego: str,
        despesas_fixas: StrictFloat,
        num_dependentes: StrictInt | str,
        tem_dividas: str | StrictBool,
        runtime: ToolRuntime[None, ConversationState],
    ) -> Command[Any]:
        """Recalcula e atualiza o score do cliente autenticado com os cinco dados da entrevista."""
        if not has_authenticated_client(runtime.state):
            return _tool_result(runtime, "Autentique o cliente primeiro.", active_agent="triagem")
        cpf = cast(str, runtime.state["client_cpf"])
        try:
            client = credit_service.recalculate_score(
                cpf,
                renda_mensal=renda_mensal,
                despesas_fixas=despesas_fixas,
                tipo_emprego=tipo_emprego,
                num_dependentes=num_dependentes,
                tem_dividas=tem_dividas,
            )
        except DomainValidationError:
            return _tool_result(
                runtime,
                "Revise os dados da entrevista: renda e despesas devem ser valores finitos "
                "e não negativos, o emprego deve ser formal, autônomo ou desempregado, "
                "os dependentes devem ser 0, 1, 2 ou 3+ e as dívidas devem ser sim ou não.",
                active_agent="entrevista",
            )
        except (ClientNotFoundError, PersistenceError) as exc:
            logger.error("%s", type(exc).__name__)
            return _tool_result(
                runtime,
                "A atualização do score está temporariamente indisponível.",
                active_agent="entrevista",
            )
        return _tool_result(
            runtime,
            f"Novo score: {client.score}. Informe o novo score e retome o atendimento de crédito.",
            active_agent="credito",
        )

    return [calcular_e_atualizar_score]


def _get_exchange_quote(provider: ExchangeRateProvider, currency: str) -> ExchangeQuote:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(provider.get_quote(currency))

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, provider.get_quote(currency)).result()


def build_exchange_tools(exchange_provider: ExchangeRateProvider) -> list[BaseTool]:
    @tool
    def consultar_cotacao(
        moeda: str = "USD", *, runtime: ToolRuntime[None, ConversationState]
    ) -> Command[Any]:
        """Consulta em reais a cotação da moeda informada. O padrão é USD."""
        if not has_authenticated_client(runtime.state):
            return _tool_result(runtime, "Autentique o cliente primeiro.", active_agent="triagem")
        try:
            quote = _get_exchange_quote(exchange_provider, moeda)
        except DomainValidationError as exc:
            return _tool_result(runtime, str(exc), active_agent="cambio")
        except Exception as exc:
            logger.error("%s", type(exc).__name__)
            return _tool_result(runtime, "A consulta de câmbio está temporariamente indisponível.")

        currency = quote.currency_code
        if quote.currency_name != currency:
            currency = f"{quote.currency_name} ({currency})"
        bid = str(quote.bid).replace(".", ",")
        if quote.is_reference_rate:
            details = f"Taxa de referência diária: R$ {bid}. Data de referência: {quote.timestamp}."
        else:
            details = f"Compra: R$ {bid}."
            if quote.ask is not None:
                ask = str(quote.ask).replace(".", ",")
                details += f" Venda: R$ {ask}."
            details += f" Data e hora da cotação: {quote.timestamp}."
        return _tool_result(runtime, f"{currency} em BRL. {details} Fonte: {quote.source}.")

    return [consultar_cotacao]


def _redirect(
    runtime: ToolRuntime[None, ConversationState], agent: Literal["credito", "entrevista", "cambio"]
) -> Command[Any]:
    if not has_authenticated_client(runtime.state):
        return _tool_result(runtime, "Autentique o cliente primeiro.", active_agent="triagem")
    return _tool_result(runtime, "Continue o atendimento.", active_agent=agent)


@tool
def redirecionar_para_credito(runtime: ToolRuntime[None, ConversationState]) -> Command[Any]:
    """Continua o atendimento sobre crédito após a autenticação do cliente."""
    return _redirect(runtime, "credito")


@tool
def redirecionar_para_entrevista(runtime: ToolRuntime[None, ConversationState]) -> Command[Any]:
    """Continua para a entrevista de crédito se o cliente autenticado aceitar a oferta."""
    return _redirect(runtime, "entrevista")


@tool
def redirecionar_para_cambio(runtime: ToolRuntime[None, ConversationState]) -> Command[Any]:
    """Continua o atendimento sobre câmbio após a autenticação do cliente."""
    return _redirect(runtime, "cambio")


@tool
def voltar_para_triagem(runtime: ToolRuntime[None, ConversationState]) -> Command[Any]:
    """Retoma a triagem quando o cliente muda de assunto ou conclui o tópico atual."""
    return _tool_result(runtime, "Continue o atendimento.", active_agent="triagem")
