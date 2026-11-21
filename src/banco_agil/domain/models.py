from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from banco_agil.domain.enums import EmploymentType as EmploymentType
from banco_agil.domain.rules import normalize_birth_date, normalize_cpf


class LimitIncreaseStatus(StrEnum):
    PENDENTE = "pendente"
    APROVADO = "aprovado"
    REJEITADO = "rejeitado"


class DomainModel(BaseModel):
    model_config = ConfigDict(
        frozen=True, hide_input_in_errors=True, allow_inf_nan=False, extra="forbid"
    )


class Client(DomainModel):
    cpf: str = Field(repr=False)
    nome: str = Field(min_length=1)
    data_nascimento: str = Field(repr=False)
    limite_atual: float = Field(ge=0)
    score: int = Field(ge=0, le=1000)

    _cpf = field_validator("cpf", mode="before")(normalize_cpf)
    _birth_date = field_validator("data_nascimento", mode="before")(normalize_birth_date)


class ScoreLimitTier(DomainModel):
    score_minimo: int = Field(ge=0, le=1000)
    limite_maximo: float = Field(ge=0)


class LimitIncreaseRequest(DomainModel):
    cpf_cliente: str = Field(repr=False)
    data_hora_solicitacao: str
    limite_atual: float = Field(ge=0)
    novo_limite_solicitado: float = Field(gt=0)
    status_pedido: LimitIncreaseStatus = LimitIncreaseStatus.PENDENTE

    _cpf = field_validator("cpf_cliente", mode="before")(normalize_cpf)

    @field_validator("data_hora_solicitacao")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Timestamp deve incluir timezone")
        return value


class LimitEvaluationResult(DomainModel):
    pedido: LimitIncreaseRequest
    limite_maximo: float = Field(ge=0)

    @property
    def aprovado(self) -> bool:
        return self.pedido.status_pedido == LimitIncreaseStatus.APROVADO
