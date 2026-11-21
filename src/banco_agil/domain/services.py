from threading import Lock

from banco_agil.domain.errors import ClientNotFoundError
from banco_agil.domain.models import Client, LimitEvaluationResult
from banco_agil.domain.repositories import (
    ClientRepository,
    LimitRequestRepository,
    ScoreLimitRepository,
)
from banco_agil.domain.rules import (
    calculate_credit_score,
    evaluate_limit_increase,
    normalize_birth_date,
    normalize_cpf,
)


class CreditService:
    def __init__(
        self,
        clients: ClientRepository,
        tiers: ScoreLimitRepository,
        requests: LimitRequestRepository,
    ) -> None:
        self.clients = clients
        self.tiers = tiers
        self.requests = requests
        self._write_lock = Lock()

    def authenticate(self, cpf: str, birth_date: str) -> Client | None:
        cpf = normalize_cpf(cpf)
        birth_date = normalize_birth_date(birth_date)
        try:
            client = self.clients.get(cpf)
        except ClientNotFoundError:
            return None
        return client if client.data_nascimento == birth_date else None

    def get_client(self, cpf: str) -> Client:
        return self.clients.get(normalize_cpf(cpf))

    def process_limit_increase(self, cpf: str, requested_limit: float) -> LimitEvaluationResult:
        with self._write_lock:
            client = self.get_client(cpf)
            result = evaluate_limit_increase(client, requested_limit, self.tiers.list_all())
            self.requests.append(result.pedido)
            if result.aprovado:
                self.clients.update(client.model_copy(update={"limite_atual": requested_limit}))
            return result

    def recalculate_score(
        self,
        cpf: str,
        *,
        renda_mensal: float,
        despesas_fixas: float,
        tipo_emprego: object,
        num_dependentes: object,
        tem_dividas: object,
    ) -> Client:
        with self._write_lock:
            client = self.get_client(cpf)
            score = calculate_credit_score(
                renda_mensal=renda_mensal,
                despesas_fixas=despesas_fixas,
                tipo_emprego=tipo_emprego,
                num_dependentes=num_dependentes,
                tem_dividas=tem_dividas,
            )
            updated = client.model_copy(update={"score": score})
            self.clients.update(updated)
            return updated
