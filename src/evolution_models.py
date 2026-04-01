from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_decimal(value: Any) -> Decimal | None:
    if value in (None, "", "null"):
        return None
    return Decimal(str(value))


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value


@dataclass(frozen=True)
class CandidateExperiment:
    id: str
    status: str = "pending"
    hypothesis: str = ""
    change_surface: list[str] = field(default_factory=list)
    validation: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CandidateExperiment":
        return cls(
            id=str(payload.get("id") or ""),
            status=str(payload.get("status") or "pending"),
            hypothesis=str(payload.get("hypothesis") or ""),
            change_surface=[str(item) for item in payload.get("change_surface") or []],
            validation=[str(item) for item in payload.get("validation") or []],
        )


@dataclass(frozen=True)
class EvolutionTarget:
    strategy_scope: str = ""
    primary_metric: str = "realized_per_10k_turnover"
    current_value: Decimal | None = None
    target_value: Decimal | None = None
    optimize_direction: str = "maximize"
    secondary_metrics: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EvolutionTarget":
        return cls(
            strategy_scope=str(payload.get("strategy_scope") or ""),
            primary_metric=str(payload.get("primary_metric") or "realized_per_10k_turnover"),
            current_value=parse_decimal(payload.get("current_value")),
            target_value=parse_decimal(payload.get("target_value")),
            optimize_direction=str(payload.get("optimize_direction") or "maximize"),
            secondary_metrics=[str(item) for item in payload.get("secondary_metrics") or []],
        )


@dataclass(frozen=True)
class EvolutionConstraints:
    min_trade_count: int = 0
    include_fees: bool = True
    include_slippage: bool = True
    include_funding: bool = False
    include_borrow: bool = False
    allow_leverage_change: bool = False
    allow_live_capital_increase: bool = False

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EvolutionConstraints":
        return cls(
            min_trade_count=int(payload.get("min_trade_count") or 0),
            include_fees=bool(payload.get("include_fees", True)),
            include_slippage=bool(payload.get("include_slippage", True)),
            include_funding=bool(payload.get("include_funding", False)),
            include_borrow=bool(payload.get("include_borrow", False)),
            allow_leverage_change=bool(payload.get("allow_leverage_change", False)),
            allow_live_capital_increase=bool(payload.get("allow_live_capital_increase", False)),
        )


@dataclass(frozen=True)
class EvolutionAutonomy:
    mode: str = "auto_continue"
    ask_each_round: bool = False

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EvolutionAutonomy":
        return cls(
            mode=str(payload.get("mode") or "auto_continue"),
            ask_each_round=bool(payload.get("ask_each_round", False)),
        )


@dataclass(frozen=True)
class EvolutionPromotion:
    require_paper_or_shadow: bool = True
    require_canary: bool = False
    paper_days: int = 0
    canary_days: int = 0

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EvolutionPromotion":
        return cls(
            require_paper_or_shadow=bool(payload.get("require_paper_or_shadow", True)),
            require_canary=bool(payload.get("require_canary", False)),
            paper_days=int(payload.get("paper_days") or 0),
            canary_days=int(payload.get("canary_days") or 0),
        )


@dataclass(frozen=True)
class EvolutionStopConditions:
    interrupt_on_bias: bool = True
    interrupt_on_data_corruption: bool = True
    interrupt_on_risk_breach: bool = True
    interrupt_on_missing_infra: bool = True
    max_failed_rounds_before_narrowing: int = 2

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EvolutionStopConditions":
        return cls(
            interrupt_on_bias=bool(payload.get("interrupt_on_bias", True)),
            interrupt_on_data_corruption=bool(payload.get("interrupt_on_data_corruption", True)),
            interrupt_on_risk_breach=bool(payload.get("interrupt_on_risk_breach", True)),
            interrupt_on_missing_infra=bool(payload.get("interrupt_on_missing_infra", True)),
            max_failed_rounds_before_narrowing=int(payload.get("max_failed_rounds_before_narrowing") or 2),
        )


@dataclass(frozen=True)
class EvolutionSpec:
    path: str
    target: EvolutionTarget
    constraints: EvolutionConstraints
    autonomy: EvolutionAutonomy
    promotion: EvolutionPromotion
    stop_conditions: EvolutionStopConditions
    candidate_experiments: list[CandidateExperiment] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "EvolutionSpec":
        spec_path = Path(path)
        payload = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise ValueError(f"Evolution spec must be a mapping: {spec_path}")
        return cls(
            path=str(spec_path),
            target=EvolutionTarget.from_dict(payload.get("target") or {}),
            constraints=EvolutionConstraints.from_dict(payload.get("constraints") or {}),
            autonomy=EvolutionAutonomy.from_dict(payload.get("autonomy") or {}),
            promotion=EvolutionPromotion.from_dict(payload.get("promotion") or {}),
            stop_conditions=EvolutionStopConditions.from_dict(payload.get("stop_conditions") or {}),
            candidate_experiments=[
                CandidateExperiment.from_dict(item)
                for item in payload.get("candidate_experiments") or []
                if isinstance(item, dict)
            ],
        )

    def active_candidate(self) -> CandidateExperiment | None:
        active_statuses = {
            "candidate",
            "in_progress",
            "shadow",
            "paper",
            "live_candidate",
            "challenger_coded_locally_not_deployed",
            "challenger_seeded",
            "ready_for_shadow",
            "ready_for_paper",
            "deployed_shadow",
        }
        for candidate in self.candidate_experiments:
            if candidate.id and candidate.status in active_statuses:
                return candidate
        return None


@dataclass(frozen=True)
class ObservationSummary:
    observed_at: str
    run_id: str | None
    candidate_id: str | None
    metric_name: str
    metric_value: Decimal | None
    baseline_value: Decimal | None
    target_value: Decimal | None
    fill_count: int
    turnover_quote: Decimal
    realized_pnl_quote: Decimal
    realized_per_10k_turnover: Decimal | None
    rebalance_release_ratio: Decimal | None
    paused_runtime_share: Decimal | None
    runtime_state: str | None
    runtime_reason: str | None
    first_event_ts_ms: int | None = None
    last_event_ts_ms: int | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ObservationSummary":
        return cls(
            observed_at=str(payload.get("observed_at") or ""),
            run_id=str(payload["run_id"]) if payload.get("run_id") is not None else None,
            candidate_id=str(payload["candidate_id"]) if payload.get("candidate_id") is not None else None,
            metric_name=str(payload.get("metric_name") or ""),
            metric_value=parse_decimal(payload.get("metric_value")),
            baseline_value=parse_decimal(payload.get("baseline_value")),
            target_value=parse_decimal(payload.get("target_value")),
            fill_count=int(payload.get("fill_count") or 0),
            turnover_quote=parse_decimal(payload.get("turnover_quote")) or Decimal("0"),
            realized_pnl_quote=parse_decimal(payload.get("realized_pnl_quote")) or Decimal("0"),
            realized_per_10k_turnover=parse_decimal(payload.get("realized_per_10k_turnover")),
            rebalance_release_ratio=parse_decimal(payload.get("rebalance_release_ratio")),
            paused_runtime_share=parse_decimal(payload.get("paused_runtime_share")),
            runtime_state=str(payload["runtime_state"]) if payload.get("runtime_state") is not None else None,
            runtime_reason=str(payload["runtime_reason"]) if payload.get("runtime_reason") is not None else None,
            first_event_ts_ms=int(payload["first_event_ts_ms"]) if payload.get("first_event_ts_ms") is not None else None,
            last_event_ts_ms=int(payload["last_event_ts_ms"]) if payload.get("last_event_ts_ms") is not None else None,
        )


@dataclass(frozen=True)
class ControllerState:
    phase: str = "idle"
    failed_rounds: int = 0
    last_run_id: str | None = None
    last_candidate_id: str | None = None
    last_decision: str | None = None
    last_decision_reason: str | None = None
    last_cycle_at: str | None = None
    next_action: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ControllerState":
        known = {field_info.name for field_info in fields(cls)}
        kwargs = {key: payload.get(key) for key in known if key in payload}
        return cls(**kwargs)


@dataclass(frozen=True)
class ChampionState:
    candidate_id: str
    metric_name: str
    metric_value: Decimal | None
    promoted_at: str
    run_id: str | None = None
    observation_count: int = 0

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ChampionState":
        return cls(
            candidate_id=str(payload.get("candidate_id") or ""),
            metric_name=str(payload.get("metric_name") or ""),
            metric_value=parse_decimal(payload.get("metric_value")),
            promoted_at=str(payload.get("promoted_at") or ""),
            run_id=str(payload["run_id"]) if payload.get("run_id") is not None else None,
            observation_count=int(payload.get("observation_count") or 0),
        )


@dataclass(frozen=True)
class DecisionRecord:
    ts: str
    decision: str
    reason: str
    candidate_id: str | None
    run_id: str | None
    metric_name: str
    metric_value: Decimal | None
    baseline_value: Decimal | None
    target_value: Decimal | None
    next_action: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DecisionRecord":
        return cls(
            ts=str(payload.get("ts") or ""),
            decision=str(payload.get("decision") or ""),
            reason=str(payload.get("reason") or ""),
            candidate_id=str(payload["candidate_id"]) if payload.get("candidate_id") is not None else None,
            run_id=str(payload["run_id"]) if payload.get("run_id") is not None else None,
            metric_name=str(payload.get("metric_name") or ""),
            metric_value=parse_decimal(payload.get("metric_value")),
            baseline_value=parse_decimal(payload.get("baseline_value")),
            target_value=parse_decimal(payload.get("target_value")),
            next_action=str(payload["next_action"]) if payload.get("next_action") is not None else None,
            details=dict(payload.get("details") or {}),
        )


@dataclass(frozen=True)
class ExperimentRecord:
    ts: str
    action: str
    status: str
    reason: str
    candidate_id: str | None
    trigger_decision: str | None
    next_action: str | None
    prompt_path: str | None = None
    output_path: str | None = None
    command: list[str] = field(default_factory=list)
    exit_code: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ExperimentRecord":
        return cls(
            ts=str(payload.get("ts") or ""),
            action=str(payload.get("action") or ""),
            status=str(payload.get("status") or ""),
            reason=str(payload.get("reason") or ""),
            candidate_id=str(payload["candidate_id"]) if payload.get("candidate_id") is not None else None,
            trigger_decision=str(payload["trigger_decision"]) if payload.get("trigger_decision") is not None else None,
            next_action=str(payload["next_action"]) if payload.get("next_action") is not None else None,
            prompt_path=str(payload["prompt_path"]) if payload.get("prompt_path") is not None else None,
            output_path=str(payload["output_path"]) if payload.get("output_path") is not None else None,
            command=[str(item) for item in payload.get("command") or []],
            exit_code=int(payload["exit_code"]) if payload.get("exit_code") is not None else None,
            details=dict(payload.get("details") or {}),
        )


@dataclass(frozen=True)
class CandidateBundle:
    candidate_id: str
    created_at: str
    status: str
    hypothesis: str
    change_surface: list[str] = field(default_factory=list)
    validation: list[str] = field(default_factory=list)
    source_decision: str | None = None
    parent_champion_id: str | None = None
    bundle_dir: str | None = None
    spec_snapshot_path: str | None = None
    patch_paths: list[str] = field(default_factory=list)
    config_patch: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CandidateBundle":
        return cls(
            candidate_id=str(payload.get("candidate_id") or ""),
            created_at=str(payload.get("created_at") or ""),
            status=str(payload.get("status") or ""),
            hypothesis=str(payload.get("hypothesis") or ""),
            change_surface=[str(item) for item in payload.get("change_surface") or []],
            validation=[str(item) for item in payload.get("validation") or []],
            source_decision=str(payload["source_decision"]) if payload.get("source_decision") is not None else None,
            parent_champion_id=str(payload["parent_champion_id"]) if payload.get("parent_champion_id") is not None else None,
            bundle_dir=str(payload["bundle_dir"]) if payload.get("bundle_dir") is not None else None,
            spec_snapshot_path=str(payload["spec_snapshot_path"]) if payload.get("spec_snapshot_path") is not None else None,
            patch_paths=[str(item) for item in payload.get("patch_paths") or []],
            config_patch=dict(payload.get("config_patch") or {}),
            notes=str(payload.get("notes") or ""),
        )


@dataclass(frozen=True)
class GateResult:
    gate: str
    passed: bool
    detail: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GateResult":
        return cls(
            gate=str(payload.get("gate") or ""),
            passed=bool(payload.get("passed", False)),
            detail=str(payload.get("detail") or ""),
        )


@dataclass(frozen=True)
class EvaluationReport:
    candidate_id: str
    stage: str
    status: str
    evaluated_at: str
    summary: str = ""
    metrics: dict[str, Decimal | None] = field(default_factory=dict)
    gate_results: list[GateResult] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    decision_hint: str | None = None
    run_id: str | None = None
    baseline_value: Decimal | None = None
    target_value: Decimal | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EvaluationReport":
        metrics_payload = payload.get("metrics") or {}
        metrics: dict[str, Decimal | None] = {}
        if isinstance(metrics_payload, dict):
            for key, value in metrics_payload.items():
                metrics[str(key)] = parse_decimal(value)
        return cls(
            candidate_id=str(payload.get("candidate_id") or ""),
            stage=str(payload.get("stage") or ""),
            status=str(payload.get("status") or ""),
            evaluated_at=str(payload.get("evaluated_at") or ""),
            summary=str(payload.get("summary") or ""),
            metrics=metrics,
            gate_results=[
                GateResult.from_dict(item)
                for item in payload.get("gate_results") or []
                if isinstance(item, dict)
            ],
            artifacts=dict(payload.get("artifacts") or {}),
            decision_hint=str(payload["decision_hint"]) if payload.get("decision_hint") is not None else None,
            run_id=str(payload["run_id"]) if payload.get("run_id") is not None else None,
            baseline_value=parse_decimal(payload.get("baseline_value")),
            target_value=parse_decimal(payload.get("target_value")),
        )


@dataclass(frozen=True)
class EvaluationRequest:
    request_id: str
    candidate_id: str
    requested_stage: str
    config_path: str
    spec_path: str
    state_dir: str
    primary_metric: str
    target_value: Decimal | None = None
    baseline_value: Decimal | None = None
    change_surface: list[str] = field(default_factory=list)
    validation: list[str] = field(default_factory=list)
    evaluation_report_path: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EvaluationRequest":
        return cls(
            request_id=str(payload.get("request_id") or ""),
            candidate_id=str(payload.get("candidate_id") or ""),
            requested_stage=str(payload.get("requested_stage") or ""),
            config_path=str(payload.get("config_path") or ""),
            spec_path=str(payload.get("spec_path") or ""),
            state_dir=str(payload.get("state_dir") or ""),
            primary_metric=str(payload.get("primary_metric") or ""),
            target_value=parse_decimal(payload.get("target_value")),
            baseline_value=parse_decimal(payload.get("baseline_value")),
            change_surface=[str(item) for item in payload.get("change_surface") or []],
            validation=[str(item) for item in payload.get("validation") or []],
            evaluation_report_path=str(payload.get("evaluation_report_path") or ""),
        )
