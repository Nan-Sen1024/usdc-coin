import json
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evolution_models import ChampionState, ControllerState, DecisionRecord, ExperimentRecord, ObservationSummary
from src.evolution_store import EvolutionStore


def test_evolution_store_round_trips_state_and_logs(tmp_path):
    store = EvolutionStore(tmp_path)
    controller_state = ControllerState(
        phase="observing",
        failed_rounds=1,
        last_run_id="run-1",
        last_candidate_id="candidate-1",
        last_decision="continue_observing",
        last_decision_reason="waiting for more fills",
        last_cycle_at="2026-04-01T00:00:00+00:00",
        next_action="wait_for_more_samples",
    )
    champion_state = ChampionState(
        candidate_id="candidate-0",
        metric_name="realized_per_10k_turnover",
        metric_value=Decimal("0.42"),
        promoted_at="2026-03-31T00:00:00+00:00",
        run_id="run-0",
        observation_count=3,
    )
    observation = ObservationSummary(
        observed_at="2026-04-01T01:00:00+00:00",
        run_id="run-1",
        candidate_id="candidate-1",
        metric_name="realized_per_10k_turnover",
        metric_value=Decimal("0.38"),
        baseline_value=Decimal("0.42"),
        target_value=Decimal("2.0"),
        fill_count=12,
        turnover_quote=Decimal("1000"),
        realized_pnl_quote=Decimal("0.038"),
        realized_per_10k_turnover=Decimal("0.38"),
        rebalance_release_ratio=Decimal("0.30"),
        paused_runtime_share=Decimal("0.10"),
        runtime_state="QUOTING",
        runtime_reason="running",
        first_event_ts_ms=1,
        last_event_ts_ms=2,
    )
    decision = DecisionRecord(
        ts="2026-04-01T01:00:01+00:00",
        decision="continue_observing",
        reason="Need more paper-trade samples.",
        candidate_id="candidate-1",
        run_id="run-1",
        metric_name="realized_per_10k_turnover",
        metric_value=Decimal("0.38"),
        baseline_value=Decimal("0.42"),
        target_value=Decimal("2.0"),
        next_action="wait_for_more_samples",
        details={"fill_count": 12},
    )
    experiment = ExperimentRecord(
        ts="2026-04-01T01:00:02+00:00",
        action="codex_dispatch",
        status="skipped",
        reason="waiting for more fills",
        candidate_id="candidate-1",
        trigger_decision="continue_observing",
        next_action="wait_for_more_samples",
        command=["codex", "exec"],
        details={"mode": "dry-run"},
    )

    store.save_controller_state(controller_state)
    store.save_champion_state(champion_state)
    store.append_observation(observation)
    store.append_decision(decision)
    store.append_experiment(experiment)

    assert store.load_controller_state() == controller_state
    assert store.load_champion_state() == champion_state
    assert store.load_latest_observation() == observation
    assert store.load_latest_decision() == decision
    assert store.load_latest_experiment() == experiment

    controller_payload = json.loads((tmp_path / "controller_state.json").read_text(encoding="utf-8"))
    assert controller_payload["phase"] == "observing"
    assert controller_payload["failed_rounds"] == 1
