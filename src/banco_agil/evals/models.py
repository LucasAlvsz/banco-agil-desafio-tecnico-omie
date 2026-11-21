from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from banco_agil.agents.state import AgentName
from banco_agil.domain.models import LimitIncreaseStatus

type SeedFile = Literal["clientes.csv", "score_limite.csv", "solicitacoes_aumento_limite.csv"]


class EvalModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class EvalToolCall(EvalModel):
    tool: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)


class EvalLimitRequest(EvalModel):
    cpf_cliente: str
    limite_atual: float
    novo_limite_solicitado: float
    status_pedido: LimitIncreaseStatus


class EvalExpectations(EvalModel):
    active_agent: AgentName
    route: tuple[AgentName, ...] = Field(min_length=1)
    authenticated: bool
    ended: bool = False
    auth_attempts: int | None = Field(default=None, ge=0)
    confirmation_amount: float | None = Field(default=None, gt=0)
    pending_requested_limit: float | None = Field(default=None, gt=0)
    guardrail_blocked: bool = False
    total_tokens: int | None = Field(default=None, ge=0)
    tools: tuple[str, ...] | None = None
    tool_contains: tuple[str, ...] = ()
    reply_contains: tuple[str, ...] = ()
    requests: tuple[EvalLimitRequest, ...] = ()
    unchanged: tuple[SeedFile, ...] = ()
    client_limit: float | None = Field(default=None, ge=0)
    client_score: int | None = Field(default=None, ge=0, le=1000)


class EvalTurn(EvalModel):
    message: str | None = None
    approve: bool | None = None
    script: tuple[str | EvalToolCall, ...] = ()
    expect: EvalExpectations

    @model_validator(mode="after")
    def validate_input(self) -> Self:
        if (self.message is None) == (self.approve is None):
            raise ValueError("Informe exatamente um dos campos message ou approve")
        return self


class EvalScenario(EvalModel):
    id: str = Field(min_length=1)
    exchange: Literal["primary", "fallback"] = "primary"
    turns: tuple[EvalTurn, ...] = Field(min_length=1)


class ScenarioEvalResult(EvalModel):
    id: str
    passed: bool
    failures: tuple[str, ...]
    route_matches: int
    turns: int
    pii_leaks: int
    latencies_ms: tuple[float, ...]
    total_tokens: int
    total_cost_usd: float | None


class EvalReport(EvalModel):
    scenarios: tuple[ScenarioEvalResult, ...]
    total_scenarios: int
    passed_scenarios: int
    failed_scenarios: int
    pass_rate: float
    route_accuracy: float
    pii_leaks: int
    avg_latency_ms: float
    p95_latency_ms: float
    total_tokens: int
    total_cost_usd: float | None
