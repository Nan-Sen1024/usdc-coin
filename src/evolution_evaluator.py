from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

import yaml

from .audit_summary import render_audit_summary
from .config import BotConfig
from .evolution_cycle import collect_current_observation, detect_interrupt_reason
from .evolution_models import EvaluationReport, EvaluationRequest, EvolutionSpec, GateResult, ObservationSummary, utc_now_iso
from .evolution_store import EvolutionStore

REPO_ROOT = Path(__file__).resolve().parents[1]


CommandExecutor = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class EvaluationGenerationResult:
    request: EvaluationRequest
    report: EvaluationReport
    report_path: str
    commands_run: list[list[str]]
    summary_artifact_path: str | None


def generate_evaluation_report(
    *,
    config: BotConfig,
    spec_path: str | Path,
    state_dir: str | Path,
    request_path: str | Path,
    executor: CommandExecutor = subprocess.run,
    now: datetime | None = None,
) -> EvaluationGenerationResult:
    spec = EvolutionSpec.load(spec_path)
    request = _load_evaluation_request(request_path)
    store = EvolutionStore(state_dir)
    champion_state = store.load_champion_state()
    observed_at = now or datetime.now(timezone.utc)

    observation: ObservationSummary | None
    try:
        observation = collect_current_observation(
            config=config,
            spec=spec,
            champion_state=champion_state,
            observed_at=observed_at,
        )
    except Exception:
        observation = None

    commands = _select_test_commands(request=request)
    test_gate_results, test_results = _run_test_commands(
        commands=commands,
        repo_root=REPO_ROOT,
        executor=executor,
    )

    gate_results = list(test_gate_results)
    gate_results.extend(_observation_gate_results(request=request, spec=spec, observation=observation))
    report_status = _report_status(request=request, gate_results=gate_results, observation=observation)
    summary_artifact_path = _write_summary_artifact(
        config=config,
        request=request,
        observation=observation,
    )

    metrics = _build_metrics(spec=spec, observation=observation)
    artifacts: dict[str, object] = {
        "request_path": str(request_path),
        "test_commands": [" ".join(command) for command in commands],
        "test_results": test_results,
    }
    if summary_artifact_path is not None:
        artifacts["audit_summary_path"] = summary_artifact_path

    report = EvaluationReport(
        candidate_id=request.candidate_id,
        stage=request.requested_stage,
        status=report_status,
        evaluated_at=utc_now_iso(),
        summary=_report_summary(request=request, gate_results=gate_results, observation=observation),
        metrics=metrics,
        gate_results=gate_results,
        artifacts=artifacts,
        decision_hint=_decision_hint(spec=spec, request=request, gate_results=gate_results, observation=observation, report_status=report_status),
        run_id=observation.run_id if observation is not None else None,
        baseline_value=request.baseline_value if request.baseline_value is not None else spec.target.current_value,
        target_value=request.target_value if request.target_value is not None else spec.target.target_value,
    )

    output_path = Path(request.evaluation_report_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(_report_payload(report), sort_keys=False, allow_unicode=False), encoding="utf-8")

    return EvaluationGenerationResult(
        request=request,
        report=report,
        report_path=str(output_path),
        commands_run=commands,
        summary_artifact_path=summary_artifact_path,
    )


def render_evaluation_generation_result(result: EvaluationGenerationResult) -> str:
    lines = [
        "Evolution Evaluation Report",
        f"candidate_id: {result.report.candidate_id}",
        f"stage: {result.report.stage}",
        f"status: {result.report.status}",
        f"decision_hint: {result.report.decision_hint or 'none'}",
        f"report_path: {result.report_path}",
    ]
    if result.commands_run:
        lines.append(f"commands_run: {len(result.commands_run)}")
    if result.summary_artifact_path:
        lines.append(f"audit_summary_path: {result.summary_artifact_path}")
    return "\n".join(lines)


def _load_evaluation_request(path: str | Path) -> EvaluationRequest:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Evaluation request must be a YAML mapping: {path}")
    return EvaluationRequest.from_dict(payload)


def _select_test_commands(*, request: EvaluationRequest) -> list[list[str]]:
    validations = " ".join(item.lower() for item in request.validation)
    commands: list[list[str]] = []
    if "strategy" in validations or "focused tests" in validations or "focused strategy regression tests" in validations:
        commands.append([sys.executable, "-m", "pytest", "tests/test_strategy.py", "-q"])
    if "risk" in validations:
        commands.append([sys.executable, "-m", "pytest", "tests/test_risk.py", "-q"])
    # Offline validation should still run at least one focused regression if no more specific rule exists.
    if not commands and request.requested_stage == "offline":
        commands.append([sys.executable, "-m", "pytest", "tests/test_strategy.py", "-q"])
    return _dedupe_commands(commands)


def _dedupe_commands(commands: list[list[str]]) -> list[list[str]]:
    seen: set[tuple[str, ...]] = set()
    unique: list[list[str]] = []
    for command in commands:
        key = tuple(command)
        if key in seen:
            continue
        seen.add(key)
        unique.append(command)
    return unique


def _run_test_commands(
    *,
    commands: list[list[str]],
    repo_root: Path,
    executor: CommandExecutor,
) -> tuple[list[GateResult], list[dict[str, object]]]:
    gate_results: list[GateResult] = []
    results: list[dict[str, object]] = []
    if not commands:
        gate_results.append(GateResult(gate="focused_tests", passed=True, detail="No focused test command was required for this request."))
        return gate_results, results

    overall_pass = True
    details: list[str] = []
    for command in commands:
        completed = executor(
            command,
            cwd=str(repo_root),
            text=True,
            capture_output=True,
            check=False,
        )
        results.append(
            {
                "command": command,
                "exit_code": completed.returncode,
                "stdout_excerpt": completed.stdout[:800],
                "stderr_excerpt": completed.stderr[:800],
            }
        )
        if completed.returncode != 0:
            overall_pass = False
        details.append(f"{' '.join(command)} -> exit_code={completed.returncode}")
    gate_results.append(GateResult(gate="focused_tests", passed=overall_pass, detail="; ".join(details)))
    return gate_results, results


def _observation_gate_results(
    *,
    request: EvaluationRequest,
    spec: EvolutionSpec,
    observation: ObservationSummary | None,
) -> list[GateResult]:
    gates: list[GateResult] = []
    gates.append(
        GateResult(
            gate="cost_realism",
            passed=bool(spec.constraints.include_fees and spec.constraints.include_slippage),
            detail=(
                f"include_fees={spec.constraints.include_fees}, "
                f"include_slippage={spec.constraints.include_slippage}"
            ),
        )
    )

    if request.requested_stage == "offline":
        return gates

    if observation is None:
        gates.append(GateResult(gate="observation_available", passed=False, detail="No runtime observation could be collected."))
        return gates

    gates.append(
        GateResult(
            gate="observation_available",
            passed=True,
            detail=f"run_id={observation.run_id or 'none'}",
        )
    )
    gates.append(
        GateResult(
            gate="min_trade_count",
            passed=observation.fill_count >= spec.constraints.min_trade_count,
            detail=f"fill_count={observation.fill_count}, min_trade_count={spec.constraints.min_trade_count}",
        )
    )
    interrupt_reason = detect_interrupt_reason(
        runtime_state=observation.runtime_state,
        runtime_reason=observation.runtime_reason,
        stop_conditions=spec.stop_conditions,
    )
    gates.append(
        GateResult(
            gate="hard_stop_clear",
            passed=interrupt_reason is None,
            detail=interrupt_reason or f"runtime_state={observation.runtime_state or 'unknown'}",
        )
    )
    return gates


def _report_status(
    *,
    request: EvaluationRequest,
    gate_results: list[GateResult],
    observation: ObservationSummary | None,
) -> str:
    if not all(gate.passed for gate in gate_results):
        if request.requested_stage != "offline" and observation is None:
            return "blocked"
        return "failed"
    return "passed"


def _build_metrics(*, spec: EvolutionSpec, observation: ObservationSummary | None) -> dict[str, Decimal | None]:
    if observation is None:
        return {
            spec.target.primary_metric: None,
        }
    return {
        spec.target.primary_metric: observation.metric_value,
        "realized_pnl_quote": observation.realized_pnl_quote,
        "turnover_quote": observation.turnover_quote,
        "realized_per_10k_turnover": observation.realized_per_10k_turnover,
        "rebalance_release_ratio": observation.rebalance_release_ratio,
        "paused_runtime_share": observation.paused_runtime_share,
        "fill_count": Decimal(str(observation.fill_count)),
    }


def _decision_hint(
    *,
    spec: EvolutionSpec,
    request: EvaluationRequest,
    gate_results: list[GateResult],
    observation: ObservationSummary | None,
    report_status: str,
) -> str:
    if report_status == "blocked":
        return "interrupt"
    if report_status == "failed":
        return "reject"
    if request.requested_stage != _required_stage(spec):
        return "continue_observing"
    if observation is None or observation.metric_value is None:
        return "continue_observing"
    baseline = request.baseline_value if request.baseline_value is not None else spec.target.current_value
    if baseline is None:
        return "promote" if observation.metric_value >= 0 else "reject"
    if spec.target.optimize_direction.lower().strip() == "minimize":
        return "promote" if observation.metric_value < baseline else "reject"
    return "promote" if observation.metric_value > baseline else "reject"


def _required_stage(spec: EvolutionSpec) -> str:
    if spec.promotion.require_canary:
        return "canary"
    if spec.promotion.require_paper_or_shadow:
        return "paper"
    return "oos"


def _report_summary(
    *,
    request: EvaluationRequest,
    gate_results: list[GateResult],
    observation: ObservationSummary | None,
) -> str:
    passing = sum(1 for gate in gate_results if gate.passed)
    summary = f"stage={request.requested_stage}, passed_gates={passing}/{len(gate_results)}"
    if observation is not None:
        summary += f", run_id={observation.run_id or 'none'}, fill_count={observation.fill_count}"
    return summary


def _write_summary_artifact(
    *,
    config: BotConfig,
    request: EvaluationRequest,
    observation: ObservationSummary | None,
) -> str | None:
    sqlite_path = Path(config.telemetry.sqlite_path)
    if not sqlite_path.exists():
        return None
    summary_dir = Path(request.evaluation_report_path).parent
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / f"{request.request_id}.audit-summary.txt"
    summary_path.write_text(
        render_audit_summary(config, run_id=observation.run_id if observation is not None else None),
        encoding="utf-8",
    )
    return str(summary_path)


def _report_payload(report: EvaluationReport) -> dict[str, object]:
    return {
        "candidate_id": report.candidate_id,
        "stage": report.stage,
        "status": report.status,
        "evaluated_at": report.evaluated_at,
        "summary": report.summary,
        "metrics": {key: (str(value) if value is not None else None) for key, value in report.metrics.items()},
        "gate_results": [
            {
                "gate": gate.gate,
                "passed": gate.passed,
                "detail": gate.detail,
            }
            for gate in report.gate_results
        ],
        "artifacts": report.artifacts,
        "decision_hint": report.decision_hint,
        "run_id": report.run_id,
        "baseline_value": str(report.baseline_value) if report.baseline_value is not None else None,
        "target_value": str(report.target_value) if report.target_value is not None else None,
    }
