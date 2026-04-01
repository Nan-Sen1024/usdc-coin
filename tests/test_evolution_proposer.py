from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from src.evolution_models import ControllerState
from src.evolution_proposer import propose_next_candidate
from src.evolution_store import EvolutionStore


def _write_spec(tmp_path: Path, *, candidate_statuses: list[str], bottleneck: str = "entry_churn") -> Path:
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
        "diagnosis": {"primary_bottleneck": bottleneck},
        "mutation_surface": {"allowed": ["parameters", "filters", "execution_timing"]},
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
    for index, status in enumerate(candidate_statuses, start=1):
        payload["candidate_experiments"].append(
            {
                "id": f"candidate-{index}",
                "status": status,
                "hypothesis": f"hypothesis-{index}",
                "change_surface": ["parameters", "filters"],
                "validation": ["focused tests"],
            }
        )
    path = tmp_path / "strategy-evolution.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_proposer_reuses_existing_active_candidate(tmp_path):
    spec_path = _write_spec(tmp_path, candidate_statuses=["candidate"])
    state_dir = tmp_path / "evolution"

    result = propose_next_candidate(spec_path=spec_path, state_dir=state_dir)

    assert result.action == "reuse_active"
    assert result.candidate.id == "candidate-1"


def test_proposer_activates_existing_diagnosis_only_candidate(tmp_path):
    spec_path = _write_spec(tmp_path, candidate_statuses=["diagnosis_only"])
    state_dir = tmp_path / "evolution"

    result = propose_next_candidate(spec_path=spec_path, state_dir=state_dir)

    assert result.action == "activated_existing"
    payload = yaml.safe_load(Path(spec_path).read_text(encoding="utf-8"))
    assert payload["candidate_experiments"][0]["status"] == "candidate"
    assert result.candidate.id == "candidate-1"


def test_proposer_synthesizes_narrow_search_candidate(tmp_path):
    spec_path = _write_spec(tmp_path, candidate_statuses=["rejected"], bottleneck="sell_drought_release")
    state_dir = tmp_path / "evolution"
    store = EvolutionStore(state_dir)
    store.save_controller_state(
        ControllerState(
            phase="idle",
            failed_rounds=2,
            last_run_id="run-1",
            last_candidate_id="candidate-1",
            last_decision="reject",
            last_decision_reason="candidate failed",
            last_cycle_at="2026-04-01T00:00:00+00:00",
            next_action="narrow_search",
        )
    )

    result = propose_next_candidate(spec_path=spec_path, state_dir=state_dir)

    assert result.action == "synthesized_narrow_search"
    assert result.candidate.id.startswith("candidate-1-narrow")
    payload = yaml.safe_load(Path(spec_path).read_text(encoding="utf-8"))
    assert len(payload["candidate_experiments"]) == 2
    assert payload["candidate_experiments"][-1]["status"] == "candidate"
