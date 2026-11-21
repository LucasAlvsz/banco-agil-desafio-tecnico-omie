import json
import logging
from math import isfinite
from typing import Any, Literal, cast

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command, interrupt

from banco_agil.agents.state import ConversationState, has_authenticated_client
from banco_agil.domain.errors import (
    ClientNotFoundError,
    DomainError,
    DomainValidationError,
    PersistenceError,
)
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
