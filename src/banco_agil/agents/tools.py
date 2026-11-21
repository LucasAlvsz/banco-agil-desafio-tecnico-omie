import logging
from typing import Any, Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from banco_agil.agents.state import ConversationState
from banco_agil.domain.errors import DomainValidationError, PersistenceError
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
        if runtime.state.get("authenticated"):
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
            auth_attempts=0,
        )

    return [autenticar_cliente, redirecionar_para_credito, redirecionar_para_cambio]


def _redirect(
    runtime: ToolRuntime[None, ConversationState], agent: Literal["credito", "cambio"]
) -> Command[Any]:
    if not runtime.state.get("authenticated") or not runtime.state.get("client_cpf"):
        return _tool_result(runtime, "Autentique o cliente primeiro.", active_agent="triagem")
    return _tool_result(runtime, "Continue o atendimento.", active_agent=agent)


@tool
def redirecionar_para_credito(runtime: ToolRuntime[None, ConversationState]) -> Command[Any]:
    """Continua o atendimento sobre crédito após a autenticação do cliente."""
    return _redirect(runtime, "credito")


@tool
def redirecionar_para_cambio(runtime: ToolRuntime[None, ConversationState]) -> Command[Any]:
    """Continua o atendimento sobre câmbio após a autenticação do cliente."""
    return _redirect(runtime, "cambio")


@tool
def voltar_para_triagem(runtime: ToolRuntime[None, ConversationState]) -> Command[Any]:
    """Retoma a triagem quando o cliente muda de assunto ou conclui o tópico atual."""
    return _tool_result(runtime, "Continue o atendimento.", active_agent="triagem")
