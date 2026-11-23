import argparse
import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from banco_agil.domain.errors import DomainError
from banco_agil.evals.runner import DEFAULT_SCENARIOS_PATH, run_evals
from banco_agil.infrastructure.privacy import configure_logging

logger = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Avaliação das jornadas do Banco Ágil")
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_SCENARIOS_PATH)
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenarios",
        help="Filtra por ID de cenário (pode ser repetido)",
    )
    parser.add_argument("--runs", type=int, default=1, help="Execuções por cenário")
    parser.add_argument("--json", action="store_true", help="Emite o relatório em JSON")
    args = parser.parse_args(argv)
    mode: Literal["offline", "live"] = args.mode
    configure_logging()
    try:
        report = run_evals(
            mode=mode,
            scenarios_path=args.dataset,
            scenario_ids=args.scenarios,
            runs=args.runs,
        )
    except (OSError, ValueError, DomainError) as exc:
        message = f"Não foi possível executar a avaliação ({type(exc).__name__})."
        if args.json:
            print(json.dumps({"error": message}, ensure_ascii=False))
        else:
            logger.error("%s", message)
        return 1

    if args.json:
        print(report.model_dump_json())
    else:
        print(f"{'Cenário':48}  Resultado")
        for scenario in report.scenarios:
            status = "OK" if scenario.passed else "FALHOU"
            print(f"{scenario.id:48}  {status}")
            for failure in scenario.failures:
                print(f"  {failure}")
        cost = report.total_cost_usd
        estimated_cost = "N/D" if cost is None else f"US$ {cost:.6f}"
        print(
            f"Passaram {report.passed_scenarios}/{report.total_scenarios} "
            f"({report.pass_rate:.0%}) · Rotas {report.route_accuracy:.0%} "
            f"· Vazamentos de PII {report.pii_leaks}"
        )
        print(
            f"Latência média {report.avg_latency_ms:.1f} ms "
            f"· p95 {report.p95_latency_ms:.1f} ms "
            f"· Tokens {report.total_tokens} · Custo estimado {estimated_cost}"
        )
    return int(report.failed_scenarios > 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
