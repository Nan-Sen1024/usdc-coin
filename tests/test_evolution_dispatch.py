import subprocess
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from src.evolution_dispatch import build_dispatch_plan, execute_dispatch_plan
from src.evolution_models import ControllerState, DecisionRecord, ObservationSummary
from src.evolution_store import EvolutionStore


def _write_spec(tmp_path: Path, *, with_candidate: bool = False) -> Path:
    payload = {
        "target": {
            "strategy_scope": "test_scope",
            "primary_metric": "realized_per_10k_turnover",
            "current_value": "0.05",
            "target_value": "2.0",
            "optimize_direction": "maximize",
        },
        "constraints": {"min_trade_count": 5},
        "autonomy": {"mode": "auto_continue", "ask_each_round": False},
        "promotion": {"require_paper_or_shadow": True, "paper_days": 0, "require_canary": False, "canary_days": 0},
        "stop_conditions": {
            "interrupt_on_bias": True,
            "interrupt_on_data_corruption": True,
            "interrupt_on_risk_breach": True,
            "interrupt_on_missing_infra": True,
            "max_failed_rounds_before_narrowing": 2,
        },
        "candidate_experiments": [],
    }
    if with_candidate:
        payload["candidate_experiments"].append(
            {
                "id": "candidate-1",
                "status": "challenger_coded_locally_not_deployed",
                "hypothesis": "candidate hypothesis",
                "change_surface": ["parameters"],
                "validation": ["focused tests"],
            }
        )
    spec_path = tmp_path / "strategy-evolution.yaml"
    spec_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return spec_path


def test_dispatch_plan_skips_when_controller_is_still_observing(tmp_path):
    spec_path = _write_spec(tmp_path)
    state_dir = tmp_path / "evolution"
    store = EvolutionStore(state_dir)
    store.save_controller_state(
        ControllerState(
            phase="observing",
            failed_rounds=0,
            last_run_id="run-1",
            last_candidate_id="candidate-1",
            last_decision="continue_observing",
            last_decision_reason="need more fills",
            last_cycle_at="2026-04-01T00:00:00+00:00",
            next_action="wait_for_more_samples",
        )
    )
    store.append_decision(
        DecisionRecord(
            ts="2026-04-01T00:00:00+00:00",
            decision="continue_observing",
            reason="need more fills",
            candidate_id="candidate-1",
            run_id="run-1",
            metric_name="realized_per_10k_turnover",
            metric_value=None,
            baseline_value=Decimal("0.05"),
            target_value=Decimal("2.0"),
            next_action="wait_for_more_samples",
            details={},
        )
    )

    plan = build_dispatch_plan(
        config_path=tmp_path / "config.yaml",
        spec_path=spec_path,
        state_dir=state_dir,
    )

    assert plan.should_dispatch is False
    assert "observation mode" in plan.reason


def test_dispatch_plan_builds_codex_exec_request_when_ready_for_new_candidate(tmp_path):
    spec_path = _write_spec(tmp_path)
    state_dir = tmp_path / "evolution"
    store = EvolutionStore(state_dir)
    store.save_controller_state(
        ControllerState(
            phase="idle",
            failed_rounds=1,
            last_run_id="run-1",
            last_candidate_id=None,
            last_decision="ready_for_optimization",
            last_decision_reason="no active challenger",
            last_cycle_at="2026-04-01T00:00:00+00:00",
            next_action="generate_next_candidate",
        )
    )
    store.append_decision(
        DecisionRecord(
            ts="2026-04-01T00:00:00+00:00",
            decision="ready_for_optimization",
            reason="no active challenger",
            candidate_id=None,
            run_id="run-1",
            metric_name="realized_per_10k_turnover",
            metric_value=Decimal("0.08"),
            baseline_value=Decimal("0.05"),
            target_value=Decimal("2.0"),
            next_action="generate_next_candidate",
            details={},
        )
    )
    store.append_observation(
        ObservationSummary(
            observed_at="2026-04-01T00:00:00+00:00",
            run_id="run-1",
            candidate_id=None,
            metric_name="realized_per_10k_turnover",
            metric_value=Decimal("0.08"),
            baseline_value=Decimal("0.05"),
            target_value=Decimal("2.0"),
            fill_count=10,
            turnover_quote=Decimal("1000"),
            realized_pnl_quote=Decimal("0.008"),
            realized_per_10k_turnover=Decimal("0.08"),
            rebalance_release_ratio=Decimal("0.25"),
            paused_runtime_share=Decimal("0.1"),
            runtime_state="QUOTING",
            runtime_reason="running",
        )
    )

    plan = build_dispatch_plan(
        config_path=tmp_path / "config.yaml",
        spec_path=spec_path,
        state_dir=state_dir,
        codex_bin="codex",
    )

    assert plan.should_dispatch is True
    assert plan.command[:3] == ["codex", "exec", "-C"]
    assert "$quant-autonomous-evolution" in plan.prompt
    assert "generate_next_candidate" in plan.prompt
    assert plan.output_schema_path is not None
    assert plan.evaluation_request_path is None


def test_execute_dispatch_plan_records_result(tmp_path):
    spec_path = _write_spec(tmp_path)
    state_dir = tmp_path / "evolution"
    store = EvolutionStore(state_dir)
    store.save_controller_state(
        ControllerState(
            phase="idle",
            failed_rounds=0,
            last_run_id="run-1",
            last_candidate_id=None,
            last_decision="ready_for_optimization",
            last_decision_reason="no active challenger",
            last_cycle_at="2026-04-01T00:00:00+00:00",
            next_action="generate_next_candidate",
        )
    )
    store.append_decision(
        DecisionRecord(
            ts="2026-04-01T00:00:00+00:00",
            decision="ready_for_optimization",
            reason="no active challenger",
            candidate_id=None,
            run_id="run-1",
            metric_name="realized_per_10k_turnover",
            metric_value=Decimal("0.08"),
            baseline_value=Decimal("0.05"),
            target_value=Decimal("2.0"),
            next_action="generate_next_candidate",
            details={},
        )
    )
    plan = build_dispatch_plan(config_path=tmp_path / "config.yaml", spec_path=spec_path, state_dir=state_dir)

    def fake_executor(command, *, input, text, capture_output, check):
        assert command[0] == "codex"
        assert "$quant-autonomous-evolution" in input
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    result = execute_dispatch_plan(plan, state_dir=state_dir, executor=fake_executor)

    assert result.status == "dispatched"
    assert result.exit_code == 0
    latest = store.load_latest_experiment()
    assert latest is not None
    assert latest.status == "dispatched"
    assert Path(plan.prompt_path).exists()


def test_dispatch_plan_with_active_candidate_writes_request_and_schema(tmp_path):
    spec_path = _write_spec(tmp_path, with_candidate=True)
    state_dir = tmp_path / "evolution"
    store = EvolutionStore(state_dir)
    store.save_controller_state(
        ControllerState(
            phase="idle",
            failed_rounds=1,
            last_run_id="run-1",
            last_candidate_id="candidate-1",
            last_decision="reject",
            last_decision_reason="need narrower search",
            last_cycle_at="2026-04-01T00:00:00+00:00",
            next_action="narrow_search",
        )
    )
    store.append_decision(
        DecisionRecord(
            ts="2026-04-01T00:00:00+00:00",
            decision="reject",
            reason="need narrower search",
            candidate_id="candidate-1",
            run_id="run-1",
            metric_name="realized_per_10k_turnover",
            metric_value=Decimal("0.03"),
            baseline_value=Decimal("0.05"),
            target_value=Decimal("2.0"),
            next_action="narrow_search",
            details={},
        )
    )

    plan = build_dispatch_plan(
        config_path=tmp_path / "config.yaml",
        spec_path=spec_path,
        state_dir=state_dir,
    )

    assert plan.should_dispatch is True
    assert plan.evaluation_request_path is not None
    assert plan.evaluation_template_path is not None
    assert plan.evaluation_report_path is not None
    assert Path(plan.output_schema_path).exists()
    assert Path(plan.evaluation_request_path).exists()
    assert Path(plan.evaluation_template_path).exists()


def test_execute_dispatch_plan_auto_applies_written_report(tmp_path):
    spec_path = _write_spec(tmp_path, with_candidate=True)
    state_dir = tmp_path / "evolution"
    store = EvolutionStore(state_dir)
    store.save_controller_state(
        ControllerState(
            phase="idle",
            failed_rounds=0,
            last_run_id="run-1",
            last_candidate_id="candidate-1",
            last_decision="reject",
            last_decision_reason="retry candidate",
            last_cycle_at="2026-04-01T00:00:00+00:00",
            next_action="narrow_search",
        )
    )
    store.append_decision(
        DecisionRecord(
            ts="2026-04-01T00:00:00+00:00",
            decision="reject",
            reason="retry candidate",
            candidate_id="candidate-1",
            run_id="run-1",
            metric_name="realized_per_10k_turnover",
            metric_value=Decimal("0.03"),
            baseline_value=Decimal("0.05"),
            target_value=Decimal("2.0"),
            next_action="narrow_search",
            details={},
        )
    )
    plan = build_dispatch_plan(config_path=tmp_path / "config.yaml", spec_path=spec_path, state_dir=state_dir)

    def fake_executor(command, *, input, text, capture_output, check):
        output_path = Path(command[command.index("-o") + 1])
        response = {
            "status": "completed",
            "summary": "wrote evaluation report",
            "candidate_id": "candidate-1",
            "report_written": True,
            "evaluation_report_path": plan.evaluation_report_path,
            "decision_hint": "promote",
            "next_action": "prepare_next_challenger",
        }
        output_path.write_text(yaml.safe_dump(response, sort_keys=False), encoding="utf-8")
        Path(plan.evaluation_report_path).write_text(
            yaml.safe_dump(
                {
                    "candidate_id": "candidate-1",
                    "stage": "paper",
                    "status": "passed",
                    "evaluated_at": "2026-04-01T00:00:00+00:00",
                    "summary": "paper pass",
                    "metrics": {"realized_per_10k_turnover": "0.25"},
                    "gate_results": [{"gate": "focused_tests", "passed": True, "detail": "ok"}],
                    "run_id": "run-2",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    result = execute_dispatch_plan(plan, state_dir=state_dir, executor=fake_executor)

    assert result.status == "dispatched"
    assert result.applied_decision == "promote"
    assert result.applied_report_path is not None
    assert store.load_champion_state() is not None
    assert store.load_champion_state().candidate_id == "candidate-1"
