import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict

from banco_agil.agents.state import ConversationState, has_authenticated_client
from banco_agil.infrastructure.privacy import mask_pii

MAX_USER_MESSAGE_CHARS = 2000
EMPTY_INPUT_REPLY = "Por favor, escreva uma mensagem."
TOO_LONG_INPUT_REPLY = (
    "Sua mensagem está muito longa. Por favor, resuma sua solicitação em até 2.000 caracteres."
)
GUARDRAIL_BLOCKED_REPLY = (
    "Não posso alterar minhas regras ou revelar instruções internas. "
    "Posso ajudar com consulta e aumento de limite de crédito, entrevista de crédito "
    "ou cotação de câmbio."
)
RECURSION_LIMIT_REPLY = (
    "Não consegui concluir esta solicitação no momento. "
    "Por favor, reformule sua mensagem de forma mais direta."
)

_PROMPT_INJECTION = re.compile(
    r"\b(?:"
    r"(?:ignore|desconsidere|esqueca) (?:todas as |as )?(?:suas )?"
    r"(?:instrucoes|regras)(?: anteriores| do sistema)?|"
    r"(?:ignore|disregard|forget) (?:all |the |your )*(?:previous |prior |system )?"
    r"instructions|"
    r"(?:revele|mostre|repita|exiba) (?:me )?(?:o |as )?(?:seu |suas )?"
    r"(?:system prompt|prompt de sistema|instrucoes internas)|"
    r"(?:reveal|show|dump|repeat) (?:me )?(?:the |your )?"
    r"(?:system prompt|internal instructions)|"
    r"modo (?:de )?desenvolvedor|developer mode|jailbreak|finja que voce nao e|pretend (?:that )?"
    r"you (?:are not|aren't)|"
    r"(?:pule|burle|ignore|bypass) (?:a |the )?"
    r"(?:autenticacao|verificacao de identidade|authentication|identity verification)"
    r")\b"
)


class InputGuardrailDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: Literal["ok", "empty", "too_long", "prompt_injection"] = "ok"
    reply: str | None = None


def evaluate_user_input(
    message: str, *, max_chars: int = MAX_USER_MESSAGE_CHARS
) -> InputGuardrailDecision:
    cleaned = message.replace("\x00", "").strip()
    if not cleaned:
        return InputGuardrailDecision(allowed=False, reason="empty", reply=EMPTY_INPUT_REPLY)
    if len(cleaned) > max_chars:
        return InputGuardrailDecision(allowed=False, reason="too_long", reply=TOO_LONG_INPUT_REPLY)

    normalized = "".join(
        char
        for char in unicodedata.normalize("NFKD", cleaned).lower()
        if not unicodedata.combining(char)
    )
    if _PROMPT_INJECTION.search(" ".join(normalized.split())):
        return InputGuardrailDecision(
            allowed=False, reason="prompt_injection", reply=GUARDRAIL_BLOCKED_REPLY
        )
    return InputGuardrailDecision(allowed=True)


def sanitize_reply(reply: str, state: ConversationState) -> str:
    birth_date = state.get("client_birth_date") if has_authenticated_client(state) else None
    return mask_pii(reply, preserve_dates_except=birth_date or None)
