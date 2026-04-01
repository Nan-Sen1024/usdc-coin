from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from .evolution_models import (
    CandidateBundle,
    CandidateExperiment,
    ChampionState,
    ControllerState,
    DecisionRecord,
    EvaluationReport,
    EvolutionSpec,
    ExperimentRecord,
    GateResult,
    utc_now_iso,
)
from .evolution_store import EvolutionStore


@dataclass(frozen=True)
class EvaluationApplyResult:
    spec: EvolutionSpec
    report: EvaluationReport
    report_store_path: str
    decision: DecisionRecord
    controller_state: ControllerState
    champion_state: ChampionState | None
    candidate_bundle: CandidateBundle | None


def seed_active_candidate_bundle(*, spec_path: str | Path, state_dir: str | Path) -> CandidateBundle:
    spec = EvolutionSpec.load(spec_path)
    store = EvolutionStore(state_dir)
    active_candidate = spec.active_candidate()
    if active_candidate is None:
        raise ValueError("No active candidate exists in the evolution spec.")

    existing_bundle = store.load_candidate_bundle(active_candidate.id)
    if existing_bundle is not None:
        store.append_experiment(
            ExperimentRecord(
                ts=utc_now_iso(),
                action="seed_candidate_bundle",
                status="reused",
                reason=f"Candidate bundle for {active_candidate.id} already exists.",
                candidate_id=active_candidate.id,
                trigger_decision=store.load_controller_state().last_decision,
                next_action=store.load_controller_state().next_action,
                details={"bundle_dir": existing_bundle.bundle_dir},
            )
        )
        return existing_bundle

    champion_state = store.load_champion_state()
    latest_decision = store.load_latest_decision()
    bundle_dir = store.candidate_bundle_dir(active_candidate.id)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    spec_snapshot_path = bundle_dir / "spec-snapshot.yaml"
    spec_snapshot_path.write_text(Path(spec.path).read_text(encoding="utf-8"), encoding="utf-8")

    bundle = CandidateBundle(
        candidate_id=active_candidate.id,
        created_at=utc_now_iso(),
        status="seeded",
        hypothesis=active_candidate.hypothesis,
        change_surface=list(active_candidate.change_surface),
        validation=list(active_candidate.validation),
        source_decision=latest_decision.decision if latest_decision else None,
        parent_champion_id=champion_state.candidate_id if champion_state else None,
        bundle_dir=str(bundle_dir),
        spec_snapshot_path=str(spec_snapshot_path),
        patch_paths=[],
        config_patch={},
        notes="Seeded from the active candidate in strategy-evolution.yaml.",
    )
    store.save_candidate_bundle(bundle)
    store.append_experiment(
        ExperimentRecord(
            ts=utc_now_iso(),
            action="seed_candidate_bundle",
            status="created",
            reason=f"Created candidate bundle for {active_candidate.id}.",
            candidate_id=active_candidate.id,
            trigger_decision=latest_decision.decision if latest_decision else None,
            next_action=store.load_controller_state().next_action,
            details={"bundle_dir": str(bundle_dir), "spec_snapshot_path": str(spec_snapshot_path)},
        )
    )
    return bundle


def apply_evaluation_report(
    *,
    spec_path: str | Path,
    state_dir: str | Path,
    report_path: str | Path,
) -> EvaluationApplyResult:
    spec = EvolutionSpec.load(spec_path)
    store = EvolutionStore(state_dir)
    report = store.load_evaluation_report(report_path)
    report_store_path = store.save_evaluation_report(report)
    controller_state = store.load_controller_state()
    champion_state = store.load_champion_state()
    candidate_bundle = store.load_candidate_bundle(report.candidate_id)
    previous_champion = _previous_champion(store=store, current=champion_state)

    decision, next_state, next_champion, next_bundle = _decide_report_outcome(
        spec=spec,
        report=report,
        controller_state=controller_state,
        champion_state=champion_state,
        previous_champion=previous_champion,
        candidate_bundle=candidate_bundle,
    )

    if next_bundle is not None:
        store.save_candidate_bundle(next_bundle)
    store.append_decision(decision)
    store.save_controller_state(next_state)
    if decision.decision == "promote" and next_champion is not None:
        store.save_champion_state(next_champion)
        store.append_champion_history(next_champion)
    elif decision.decision == "rollback" and next_champion is not None:
        store.save_champion_state(next_champion)
    store.append_experiment(
        ExperimentRecord(
            ts=utc_now_iso(),
            action="apply_evaluation_report",
            status=decision.decision,
            reason=decision.reason,
            candidate_id=report.candidate_id,
            trigger_decision=controller_state.last_decision,
            next_action=next_state.next_action,
            details={
                "stage": report.stage,
                "status": report.status,
                "report_path": str(report_store_path),
            },
        )
    )
    return EvaluationApplyResult(
        spec=spec,
        report=report,
        report_store_path=str(report_store_path),
        decision=decision,
        controller_state=next_state,
        champion_state=next_champion,
        candidate_bundle=next_bundle,
    )


def render_candidate_bundle(bundle: CandidateBundle) -> str:
    lines = [
        "Evolution Candidate Bundle",
        f"candidate_id: {bundle.candidate_id}",
        f"status: {bundle.status}",
        f"created_at: {bundle.created_at}",
        f"source_decision: {bundle.source_decision or 'none'}",
        f"parent_champion_id: {bundle.parent_champion_id or 'none'}",
        f"bundle_dir: {bundle.bundle_dir or 'none'}",
        f"spec_snapshot_path: {bundle.spec_snapshot_path or 'none'}",
        f"change_surface: {', '.join(bundle.change_surface) if bundle.change_surface else 'none'}",
    ]
    return "\n".join(lines)


def render_evaluation_apply_result(result: EvaluationApplyResult) -> str:
    lines = [
        "Evolution Evaluation",
        f"candidate_id: {result.report.candidate_id}",
        f"stage: {result.report.stage}",
        f"report_status: {result.report.status}",
        f"decision: {result.decision.decision}",
        f"reason: {result.decision.reason}",
        f"next_action: {result.controller_state.next_action or 'none'}",
        f"report_store_path: {result.report_store_path}",
    ]
    if result.champion_state is not None:
        lines.append(f"champion_candidate: {result.champion_state.candidate_id}")
    if result.candidate_bundle is not None:
        lines.append(f"candidate_bundle_status: {result.candidate_bundle.status}")
    return "\n".join(lines)


def _decide_report_outcome(
    *,
    spec: EvolutionSpec,
    report: EvaluationReport,
    controller_state: ControllerState,
    champion_state: ChampionState | None,
    previous_champion: ChampionState | None,
    candidate_bundle: CandidateBundle | None,
) -> tuple[DecisionRecord, ControllerState, ChampionState | None, CandidateBundle | None]:
    primary_metric = spec.target.primary_metric
    metric_value = report.metrics.get(primary_metric)
    baseline_value = report.baseline_value
    if baseline_value is None:
        if champion_state is not None:
            baseline_value = champion_state.metric_value
        else:
            baseline_value = spec.target.current_value
    target_value = report.target_value if report.target_value is not None else spec.target.target_value
    all_gates_passed = _all_gates_passed(report.gate_results)
    required_stage = _required_stage(spec)
    stage_ready = _stage_rank(report.stage) >= _stage_rank(required_stage)
    is_current_champion = champion_state is not None and report.candidate_id == champion_state.candidate_id

    if report.status in {"invalid", "blocked"}:
        decision = _decision(
            spec=spec,
            decision="interrupt",
            reason=f"Evaluation report is {report.status}; runner needs intervention before proceeding.",
            candidate_id=report.candidate_id,
            run_id=report.run_id,
            metric_value=metric_value,
            baseline_value=baseline_value,
            target_value=target_value,
            next_action="repair_evaluator_or_inputs",
            details={"stage": report.stage, "report_status": report.status},
        )
        next_state = ControllerState(
            phase="interrupted",
            failed_rounds=controller_state.failed_rounds,
            last_run_id=report.run_id,
            last_candidate_id=report.candidate_id,
            last_decision=decision.decision,
            last_decision_reason=decision.reason,
            last_cycle_at=decision.ts,
            next_action=decision.next_action,
        )
        return decision, next_state, champion_state, _update_bundle_status(candidate_bundle, "blocked")

    if is_current_champion and (report.status == "failed" or not all_gates_passed or not _metric_is_healthy(metric_value, baseline_value)):
        if previous_champion is not None:
            decision = _decision(
                spec=spec,
                decision="rollback",
                reason=f"Current champion {report.candidate_id} degraded or failed gates at stage {report.stage}; rolling back to {previous_champion.candidate_id}.",
                candidate_id=report.candidate_id,
                run_id=report.run_id,
                metric_value=metric_value,
                baseline_value=baseline_value,
                target_value=target_value,
                next_action="stabilize_after_rollback",
                details={"rollback_to": previous_champion.candidate_id, "stage": report.stage},
            )
            next_state = ControllerState(
                phase="idle",
                failed_rounds=0,
                last_run_id=report.run_id,
                last_candidate_id=previous_champion.candidate_id,
                last_decision=decision.decision,
                last_decision_reason=decision.reason,
                last_cycle_at=decision.ts,
                next_action=decision.next_action,
            )
            return decision, next_state, previous_champion, _update_bundle_status(candidate_bundle, "rolled_back")
        decision = _decision(
            spec=spec,
            decision="interrupt",
            reason=f"Champion {report.candidate_id} failed but no previous champion is available for rollback.",
            candidate_id=report.candidate_id,
            run_id=report.run_id,
            metric_value=metric_value,
            baseline_value=baseline_value,
            target_value=target_value,
            next_action="manual_rollback_required",
            details={"stage": report.stage},
        )
        next_state = ControllerState(
            phase="interrupted",
            failed_rounds=controller_state.failed_rounds,
            last_run_id=report.run_id,
            last_candidate_id=report.candidate_id,
            last_decision=decision.decision,
            last_decision_reason=decision.reason,
            last_cycle_at=decision.ts,
            next_action=decision.next_action,
        )
        return decision, next_state, champion_state, _update_bundle_status(candidate_bundle, "failed")

    if report.status != "passed" or not all_gates_passed or not _is_improvement(metric_value=metric_value, baseline_value=baseline_value, target_value=target_value, optimize_direction=spec.target.optimize_direction):
        failed_rounds = controller_state.failed_rounds + 1
        next_action = "narrow_search" if failed_rounds >= spec.stop_conditions.max_failed_rounds_before_narrowing else "generate_next_candidate"
        decision = _decision(
            spec=spec,
            decision="reject",
            reason=f"Candidate {report.candidate_id} did not satisfy evaluation gates or beat the baseline at stage {report.stage}.",
            candidate_id=report.candidate_id,
            run_id=report.run_id,
            metric_value=metric_value,
            baseline_value=baseline_value,
            target_value=target_value,
            next_action=next_action,
            details={"stage": report.stage, "report_status": report.status, "all_gates_passed": all_gates_passed},
        )
        next_state = ControllerState(
            phase="idle",
            failed_rounds=failed_rounds,
            last_run_id=report.run_id,
            last_candidate_id=report.candidate_id,
            last_decision=decision.decision,
            last_decision_reason=decision.reason,
            last_cycle_at=decision.ts,
            next_action=next_action,
        )
        return decision, next_state, champion_state, _update_bundle_status(candidate_bundle, "rejected")

    if not stage_ready:
        next_action = f"advance_to_{required_stage}"
        decision = _decision(
            spec=spec,
            decision="continue_observing",
            reason=f"Candidate {report.candidate_id} passed stage {report.stage} but still requires {required_stage} before promotion.",
            candidate_id=report.candidate_id,
            run_id=report.run_id,
            metric_value=metric_value,
            baseline_value=baseline_value,
            target_value=target_value,
            next_action=next_action,
            details={"stage": report.stage, "required_stage": required_stage},
        )
        next_state = ControllerState(
            phase="observing",
            failed_rounds=controller_state.failed_rounds,
            last_run_id=report.run_id,
            last_candidate_id=report.candidate_id,
            last_decision=decision.decision,
            last_decision_reason=decision.reason,
            last_cycle_at=decision.ts,
            next_action=next_action,
        )
        return decision, next_state, champion_state, _update_bundle_status(candidate_bundle, f"passed_{report.stage}")

    promoted = ChampionState(
        candidate_id=report.candidate_id,
        metric_name=primary_metric,
        metric_value=metric_value,
        promoted_at=utc_now_iso(),
        run_id=report.run_id,
        observation_count=1,
    )
    decision = _decision(
        spec=spec,
        decision="promote",
        reason=f"Candidate {report.candidate_id} passed all gates and reached the required stage {report.stage}.",
        candidate_id=report.candidate_id,
        run_id=report.run_id,
        metric_value=metric_value,
        baseline_value=baseline_value,
        target_value=target_value,
        next_action="prepare_next_challenger",
        details={"stage": report.stage},
    )
    next_state = ControllerState(
        phase="idle",
        failed_rounds=0,
        last_run_id=report.run_id,
        last_candidate_id=report.candidate_id,
        last_decision=decision.decision,
        last_decision_reason=decision.reason,
        last_cycle_at=decision.ts,
        next_action=decision.next_action,
    )
    return decision, next_state, promoted, _update_bundle_status(candidate_bundle, "promoted")


def _decision(
    *,
    spec: EvolutionSpec,
    decision: str,
    reason: str,
    candidate_id: str | None,
    run_id: str | None,
    metric_value: Decimal | None,
    baseline_value: Decimal | None,
    target_value: Decimal | None,
    next_action: str | None,
    details: dict[str, object],
) -> DecisionRecord:
    return DecisionRecord(
        ts=utc_now_iso(),
        decision=decision,
        reason=reason,
        candidate_id=candidate_id,
        run_id=run_id,
        metric_name=spec.target.primary_metric,
        metric_value=metric_value,
        baseline_value=baseline_value,
        target_value=target_value,
        next_action=next_action,
        details=details,
    )


def _update_bundle_status(bundle: CandidateBundle | None, status: str) -> CandidateBundle | None:
    if bundle is None:
        return None
    return CandidateBundle(
        candidate_id=bundle.candidate_id,
        created_at=bundle.created_at,
        status=status,
        hypothesis=bundle.hypothesis,
        change_surface=list(bundle.change_surface),
        validation=list(bundle.validation),
        source_decision=bundle.source_decision,
        parent_champion_id=bundle.parent_champion_id,
        bundle_dir=bundle.bundle_dir,
        spec_snapshot_path=bundle.spec_snapshot_path,
        patch_paths=list(bundle.patch_paths),
        config_patch=dict(bundle.config_patch),
        notes=bundle.notes,
    )


def _all_gates_passed(gates: Iterable[GateResult]) -> bool:
    gate_list = list(gates)
    if not gate_list:
        return True
    return all(gate.passed for gate in gate_list)


def _required_stage(spec: EvolutionSpec) -> str:
    if spec.promotion.require_canary:
        return "canary"
    if spec.promotion.require_paper_or_shadow:
        return "paper"
    return "oos"


def _stage_rank(stage: str) -> int:
    order = {"offline": 1, "oos": 2, "paper": 3, "canary": 4, "champion": 5}
    return order.get(stage, 0)


def _is_improvement(
    *,
    metric_value: Decimal | None,
    baseline_value: Decimal | None,
    target_value: Decimal | None,
    optimize_direction: str,
) -> bool:
    if metric_value is None:
        return False
    direction = optimize_direction.lower().strip()
    if baseline_value is not None:
        return metric_value < baseline_value if direction == "minimize" else metric_value > baseline_value
    if target_value is not None:
        return metric_value <= target_value if direction == "minimize" else metric_value >= target_value
    return metric_value >= 0


def _metric_is_healthy(metric_value: Decimal | None, baseline_value: Decimal | None) -> bool:
    if metric_value is None:
        return False
    if baseline_value is None:
        return metric_value >= 0
    return metric_value >= baseline_value


def _previous_champion(*, store: EvolutionStore, current: ChampionState | None) -> ChampionState | None:
    if current is None:
        return None
    history = store.load_champion_history()
    for champion in reversed(history):
        if champion.candidate_id != current.candidate_id or champion.promoted_at != current.promoted_at:
            return champion
    return None
