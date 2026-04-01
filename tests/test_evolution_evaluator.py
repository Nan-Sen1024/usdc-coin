import json
import subprocess
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from src.config import BotConfig
from src.evolution_evaluator import generate_evaluation_report


def _write_spec(tmp_path: Path) -> Path:
    payload = {
        "target": {
            "strategy_scope": "test_scope",
            "primary_metric": "realized_per_10k_turnover",
            "current_value": "0.05",
            "target_value": "0.20",
            "optimize_direction": "maximize",
        },
        "constraints": {
            "min_trade_count": 3,
            "include_fees": True,
            "include_slippage": True,
        },
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
    path = tmp_path / "strategy-evolution.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _write_request(tmp_path: Path, *, stage: str, validation: list[str], report_name: str = "report.yaml") -> Path:
    path = tmp_path / "request.yaml"
    payload = {
        "request_id": "req-1",
        "candidate_id": "candidate-1",
        "requested_stage": stage,
        "config_path": str(tmp_path / "config.yaml"),
        "spec_path": str(tmp_path / "strategy-evolution.yaml"),
        "state_dir": str(tmp_path / "evolution"),
        "primary_metric": "realized_per_10k_turnover",
        "target_value": "0.20",
        "baseline_value": "0.05",
        "change_surface": ["parameters"],
        "validation": validation,
        "evaluation_report_path": str(tmp_path / report_name),
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _write_runtime_inputs(tmp_path: Path, *, fills: bool) -> BotConfig:
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
    ]
    if fills:
        records.extend(
            [
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
        )
    with journal_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")

    state_path.write_text(
        json.dumps(
            {
                "runtime_state": "QUOTING",
                "runtime_reason": "running",
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
    config.telemetry.sqlite_path = str(tmp_path / "missing.db")
    return config


def test_generate_offline_evaluation_report_runs_focused_tests_and_writes_report(tmp_path):
    spec_path = _write_spec(tmp_path)
    request_path = _write_request(tmp_path, stage="offline", validation=["focused strategy regression tests"])
    config = _write_runtime_inputs(tmp_path, fills=False)

    def fake_executor(command, *, cwd, text, capture_output, check):
        assert "tests/test_strategy.py" in command
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    result = generate_evaluation_report(
        config=config,
        spec_path=spec_path,
        state_dir=tmp_path / "evolution",
        request_path=request_path,
        executor=fake_executor,
    )

    assert result.report.status == "passed"
    assert result.report.decision_hint == "continue_observing"
    assert Path(result.report_path).exists()
    payload = yaml.safe_load(Path(result.report_path).read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    assert payload["gate_results"][0]["gate"] == "focused_tests"


def test_generate_paper_evaluation_report_fails_when_trade_count_is_too_low(tmp_path):
    spec_path = _write_spec(tmp_path)
    request_path = _write_request(tmp_path, stage="paper", validation=[])
    config = _write_runtime_inputs(tmp_path, fills=False)

    result = generate_evaluation_report(
        config=config,
        spec_path=spec_path,
        state_dir=tmp_path / "evolution",
        request_path=request_path,
    )

    assert result.report.status == "failed"
    min_trade_gate = next(g for g in result.report.gate_results if g.gate == "min_trade_count")
    assert min_trade_gate.passed is False
    assert result.report.decision_hint == "reject"
