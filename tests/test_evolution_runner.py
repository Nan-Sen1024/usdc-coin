from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from src.evolution_models import ChampionState, ControllerState
from src.evolution_runner import (
    apply_evaluation_report,
    seed_active_candidate_bundle,
)
from src.evolution_store import EvolutionStore


def _write_spec(tmp_path: Path, *, candidate_status: str = "in_progress", require_paper: bool = True) -> Path:
    payload = {
        "target": {
            "strategy_scope": "test_scope",
            "primary_metric": "realized_per_10k_turnover",
            "current_value": "0.05",
            "target_value": "0.20",
            "optimize_direction": "maximize",
        },
        "constraints": {"min_trade_count": 5},
        "autonomy": {"mode": "auto_continue", "ask_each_round": False},
        "promotion": {
            "require_paper_or_shadow": require_paper,
            "paper_days": 0,
            "require_canary": False,
            "canary_days": 0,
        },
        "stop_conditions": {
            "interrupt_on_bias": True,
            "interrupt_on_data_corruption": True,
            "interrupt_on_risk_breach": True,
            "interrupt_on_missing_infra": True,
            "max_failed_rounds_before_narrowing": 2,
        },
        "candidate_experiments": [
            {
                "id": "candidate-1",
                "status": candidate_status,
                "hypothesis": "suppress low-edge entry overlay",
                "change_surface": ["parameters", "filters"],
                "validation": ["focused tests", "paper evidence"],
            }
        ],
    }
    path = tmp_path / "strategy-evolution.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _write_report(
    tmp_path: Path,
    *,
    candidate_id: str = "candidate-1",
    stage: str = "paper",
    status: str = "passed",
    metric_value: str = "0.25",
    gate_passed: bool = True,
) -> Path:
    payload = {
        "candidate_id": candidate_id,
        "stage": stage,
        "status": status,
        "evaluated_at": "2026-04-01T00:00:00+00:00",
        "summary": "evaluation summary",
        "metrics": {
            "realized_per_10k_turnover": metric_value,
            "realized_pnl_quote": "3.2",
        },
        "gate_results": [
            {"gate": "focused_tests", "passed": gate_passed, "detail": "pytest pass"},
            {"gate": "cost_realism", "passed": True, "detail": "fees enabled"},
        ],
        "artifacts": {"log": "artifacts/run.log"},
        "run_id": "run-1",
    }
    path = tmp_path / f"{candidate_id}-{stage}.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_seed_active_candidate_bundle_creates_bundle_and_snapshot(tmp_path):
    spec_path = _write_spec(tmp_path, candidate_status="challenger_coded_locally_not_deployed")
    state_dir = tmp_path / "evolution"
    store = EvolutionStore(state_dir)
    store.save_controller_state(
        ControllerState(
            phase="idle",
            failed_rounds=0,
            last_run_id="run-0",
            last_candidate_id="candidate-1",
            last_decision="ready_for_optimization",
            last_decision_reason="seed next candidate",
            last_cycle_at="2026-04-01T00:00:00+00:00",
            next_action="generate_next_candidate",
        )
    )

    bundle = seed_active_candidate_bundle(spec_path=spec_path, state_dir=state_dir)

    assert bundle.candidate_id == "candidate-1"
    assert bundle.status == "seeded"
    assert bundle.spec_snapshot_path is not None
    assert Path(bundle.spec_snapshot_path).exists()
    assert store.load_candidate_bundle("candidate-1") == bundle
    latest = store.load_latest_experiment()
    assert latest is not None
    assert latest.action == "seed_candidate_bundle"


def test_apply_evaluation_report_promotes_candidate_when_stage_is_ready(tmp_path):
    spec_path = _write_spec(tmp_path, require_paper=True)
    state_dir = tmp_path / "evolution"
    store = EvolutionStore(state_dir)
    store.save_controller_state(
        ControllerState(
            phase="observing",
            failed_rounds=0,
            last_run_id="run-0",
            last_candidate_id="candidate-1",
            last_decision="continue_observing",
            last_decision_reason="collect paper evidence",
            last_cycle_at="2026-04-01T00:00:00+00:00",
            next_action="advance_to_paper",
        )
    )
    seed_active_candidate_bundle(spec_path=spec_path, state_dir=state_dir)
    report_path = _write_report(tmp_path, stage="paper", status="passed", metric_value="0.25", gate_passed=True)

    result = apply_evaluation_report(spec_path=spec_path, state_dir=state_dir, report_path=report_path)

    assert result.decision.decision == "promote"
    assert result.champion_state is not None
    assert result.champion_state.candidate_id == "candidate-1"
    assert result.candidate_bundle is not None
    assert result.candidate_bundle.status == "promoted"
    assert store.load_champion_state() == result.champion_state
    assert len(store.load_champion_history()) == 1


def test_apply_evaluation_report_rolls_back_failed_champion_to_previous_one(tmp_path):
    spec_path = _write_spec(tmp_path, require_paper=False)
    state_dir = tmp_path / "evolution"
    store = EvolutionStore(state_dir)
    previous = ChampionState(
        candidate_id="candidate-0",
        metric_name="realized_per_10k_turnover",
        metric_value=Decimal("0.12"),
        promoted_at="2026-03-30T00:00:00+00:00",
        run_id="run-prev",
        observation_count=3,
    )
    current = ChampionState(
        candidate_id="candidate-1",
        metric_name="realized_per_10k_turnover",
        metric_value=Decimal("0.20"),
        promoted_at="2026-03-31T00:00:00+00:00",
        run_id="run-cur",
        observation_count=2,
    )
    store.append_champion_history(previous)
    store.append_champion_history(current)
    store.save_champion_state(current)
    store.save_controller_state(
        ControllerState(
            phase="idle",
            failed_rounds=0,
            last_run_id="run-cur",
            last_candidate_id="candidate-1",
            last_decision="promote",
            last_decision_reason="candidate promoted",
            last_cycle_at="2026-03-31T00:00:00+00:00",
            next_action="monitor_champion",
        )
    )
    seed_active_candidate_bundle(spec_path=spec_path, state_dir=state_dir)
    report_path = _write_report(tmp_path, candidate_id="candidate-1", stage="champion", status="failed", metric_value="0.08", gate_passed=False)

    result = apply_evaluation_report(spec_path=spec_path, state_dir=state_dir, report_path=report_path)

    assert result.decision.decision == "rollback"
    assert result.champion_state is not None
    assert result.champion_state.candidate_id == "candidate-0"
    assert store.load_champion_state().candidate_id == "candidate-0"
    bundle = store.load_candidate_bundle("candidate-1")
    assert bundle is not None
    assert bundle.status == "rolled_back"


def test_apply_evaluation_report_rejects_candidate_and_requests_narrow_search_after_limit(tmp_path):
    spec_path = _write_spec(tmp_path, require_paper=False)
    state_dir = tmp_path / "evolution"
    store = EvolutionStore(state_dir)
    store.save_controller_state(
        ControllerState(
            phase="idle",
            failed_rounds=1,
            last_run_id="run-0",
            last_candidate_id="candidate-1",
            last_decision="reject",
            last_decision_reason="previous candidate failed",
            last_cycle_at="2026-04-01T00:00:00+00:00",
            next_action="generate_next_candidate",
        )
    )
    seed_active_candidate_bundle(spec_path=spec_path, state_dir=state_dir)
    report_path = _write_report(tmp_path, stage="oos", status="passed", metric_value="0.01", gate_passed=True)

    result = apply_evaluation_report(spec_path=spec_path, state_dir=state_dir, report_path=report_path)

    assert result.decision.decision == "reject"
    assert result.controller_state.next_action == "narrow_search"
    assert result.candidate_bundle is not None
    assert result.candidate_bundle.status == "rejected"
