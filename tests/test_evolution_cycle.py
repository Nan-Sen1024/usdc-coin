import json
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from src.config import BotConfig
from src.evolution_cycle import render_evolution_status, run_evolution_cycle
from src.evolution_store import EvolutionStore


def _write_spec(tmp_path: Path, *, candidate: bool, min_trade_count: int, current_value: str, paper_days: int = 0) -> Path:
    payload = {
        "target": {
            "strategy_scope": "test_scope",
            "primary_metric": "realized_per_10k_turnover",
            "current_value": current_value,
            "target_value": "2.0",
            "optimize_direction": "maximize",
        },
        "constraints": {
            "min_trade_count": min_trade_count,
            "include_fees": True,
            "include_slippage": True,
        },
        "autonomy": {
            "mode": "auto_continue",
            "ask_each_round": False,
        },
        "promotion": {
            "require_paper_or_shadow": True,
            "paper_days": paper_days,
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
        "candidate_experiments": [],
    }
    if candidate:
        payload["candidate_experiments"].append(
            {
                "id": "candidate-1",
                "status": "in_progress",
                "hypothesis": "test candidate",
                "change_surface": ["parameters"],
                "validation": ["unit tests"],
            }
        )
    spec_path = tmp_path / "strategy-evolution.yaml"
    spec_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return spec_path


def _write_runtime_inputs(tmp_path: Path, *, runtime_state: str = "QUOTING", runtime_reason: str = "running") -> BotConfig:
    journal_path = tmp_path / "journal.jsonl"
    state_path = tmp_path / "state.json"
    records = [
        {
            "ts_ms": 1,
            "run_id": "run1",
            "event": "decision",
            "runtime_state": "QUOTING",
            "payload": {
                "decision": {
                    "bid_layers": [{"reason": "join_best_bid", "price": "1", "base_size": "10"}],
                    "ask_layers": [],
                }
            },
        },
        {
            "ts_ms": 2,
            "run_id": "run1",
            "event": "place_order",
            "runtime_state": "QUOTING",
            "payload": {"clOrdId": "buy1", "side": "buy", "px": "1", "sz": "10"},
        },
        {
            "ts_ms": 3,
            "run_id": "run1",
            "event": "order_update",
            "runtime_state": "QUOTING",
            "payload": {"order": {"cl_ord_id": "buy1", "side": "buy", "price": "1", "filled_size": "10"}, "raw": {"fillPx": "1"}},
        },
        {
            "ts_ms": 4,
            "run_id": "run1",
            "event": "amend_order_submitted",
            "runtime_state": "QUOTING",
            "payload": {"cl_ord_id": "sell1", "reason": "rebalance_open_long"},
        },
        {
            "ts_ms": 5,
            "run_id": "run1",
            "event": "order_update",
            "runtime_state": "QUOTING",
            "payload": {"order": {"cl_ord_id": "sell1", "side": "sell", "price": "1.0002", "filled_size": "10"}, "raw": {"fillPx": "1.0002"}},
        },
    ]
    with journal_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")

    state_path.write_text(
        json.dumps(
            {
                "runtime_state": runtime_state,
                "runtime_reason": runtime_reason,
                "fill_markout_summary_by_reason": {
                    "entry": {"300": {"avg_adverse_ticks": "0.1"}},
                    "rebalance": {"300": {"avg_adverse_ticks": "0.2"}},
                },
            }
        ),
        encoding="utf-8",
    )

    config = BotConfig()
    config.telemetry.journal_path = str(journal_path)
    config.telemetry.state_path = str(state_path)
    return config


def _write_runtime_inputs_without_fills(tmp_path: Path, *, runtime_state: str = "QUOTING", runtime_reason: str = "running") -> BotConfig:
    journal_path = tmp_path / "journal.jsonl"
    state_path = tmp_path / "state.json"
    records = [
        {"ts_ms": 1, "run_id": "run1", "event": "stream_status", "runtime_state": runtime_state, "payload": {"stream": "public_books5", "connected": True}},
        {"ts_ms": 2, "run_id": "run1", "event": "stream_status", "runtime_state": runtime_state, "payload": {"stream": "private_user", "connected": True}},
    ]
    with journal_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")

    state_path.write_text(
        json.dumps({"runtime_state": runtime_state, "runtime_reason": runtime_reason, "fill_markout_summary_by_reason": {}}),
        encoding="utf-8",
    )

    config = BotConfig()
    config.telemetry.journal_path = str(journal_path)
    config.telemetry.state_path = str(state_path)
    return config


def test_cycle_reports_ready_for_optimization_when_no_candidate(tmp_path):
    config = _write_runtime_inputs(tmp_path)
    spec_path = _write_spec(tmp_path, candidate=False, min_trade_count=1, current_value="0.05")
    state_dir = tmp_path / "evolution"

    result = run_evolution_cycle(
        config=config,
        spec_path=spec_path,
        state_dir=state_dir,
        now=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )

    assert result.decision is not None
    assert result.decision.decision == "ready_for_optimization"
    assert result.controller_state.next_action == "generate_next_candidate"
    assert EvolutionStore(state_dir).load_latest_observation() is not None


def test_cycle_continues_observing_when_trade_count_is_too_low(tmp_path):
    config = _write_runtime_inputs(tmp_path)
    spec_path = _write_spec(tmp_path, candidate=True, min_trade_count=3, current_value="0.05")
    state_dir = tmp_path / "evolution"

    result = run_evolution_cycle(
        config=config,
        spec_path=spec_path,
        state_dir=state_dir,
        now=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )

    assert result.decision is not None
    assert result.decision.decision == "continue_observing"
    assert result.controller_state.phase == "observing"


def test_cycle_promotes_candidate_when_metric_improves(tmp_path):
    config = _write_runtime_inputs(tmp_path)
    spec_path = _write_spec(tmp_path, candidate=True, min_trade_count=2, current_value="0.05")
    state_dir = tmp_path / "evolution"

    result = run_evolution_cycle(
        config=config,
        spec_path=spec_path,
        state_dir=state_dir,
        now=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )

    assert result.decision is not None
    assert result.decision.decision == "promote"
    assert result.champion_state is not None
    assert result.champion_state.candidate_id == "candidate-1"
    assert render_evolution_status(config=config, spec_path=spec_path, state_dir=state_dir).startswith("Evolution Status")


def test_cycle_interrupts_on_runtime_risk_reason(tmp_path):
    config = _write_runtime_inputs(tmp_path, runtime_state="STOPPED", runtime_reason="realized loss limit hit")
    spec_path = _write_spec(tmp_path, candidate=True, min_trade_count=1, current_value="0.05")
    state_dir = tmp_path / "evolution"

    result = run_evolution_cycle(
        config=config,
        spec_path=spec_path,
        state_dir=state_dir,
        now=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )

    assert result.decision is not None
    assert result.decision.decision == "interrupt"
    assert result.controller_state.phase == "interrupted"


def test_cycle_keeps_observing_when_latest_run_has_no_fills(tmp_path):
    config = _write_runtime_inputs_without_fills(tmp_path)
    spec_path = _write_spec(tmp_path, candidate=True, min_trade_count=1, current_value="0.05")
    state_dir = tmp_path / "evolution"

    result = run_evolution_cycle(
        config=config,
        spec_path=spec_path,
        state_dir=state_dir,
        now=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )

    assert result.decision is not None
    assert result.decision.decision == "continue_observing"
    assert result.observation is not None
    assert result.observation.metric_value is None


def test_fill_bias_runtime_reason_does_not_trigger_bias_interrupt(tmp_path):
    config = _write_runtime_inputs_without_fills(tmp_path, runtime_state="QUOTING", runtime_reason="fill_rebalance_sell_biased")
    spec_path = _write_spec(tmp_path, candidate=True, min_trade_count=1, current_value="0.05")
    state_dir = tmp_path / "evolution"

    result = run_evolution_cycle(
        config=config,
        spec_path=spec_path,
        state_dir=state_dir,
        now=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )

    assert result.decision is not None
    assert result.decision.decision == "continue_observing"
