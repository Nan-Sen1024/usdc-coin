from __future__ import annotations

import json
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import BotConfig
from .evolution_models import (
    CandidateExperiment,
    ChampionState,
    ControllerState,
    DecisionRecord,
    EvolutionSpec,
    ObservationSummary,
    utc_now_iso,
)
from .evolution_store import EvolutionStore
from .order_reason_attribution import ReasonBucketSummary
from .reason_attribution import classify_reason_bucket, realized_per_10k_turnover


@dataclass(frozen=True)
class EvolutionCycleResult:
    spec: EvolutionSpec
    state_dir: str
    controller_state: ControllerState
    champion_state: ChampionState | None
    observation: ObservationSummary | None
    decision: DecisionRecord | None


def default_evolution_spec_path(config_path: str | Path) -> Path:
    return Path(config_path).resolve().parent.parent / "strategy-evolution.yaml"


def default_evolution_state_dir(config_path: str | Path) -> Path:
    return Path(config_path).resolve().parent.parent / "data" / "evolution"


def run_evolution_cycle(
    *,
    config: BotConfig,
    spec_path: str | Path,
    state_dir: str | Path,
    now: datetime | None = None,
) -> EvolutionCycleResult:
    cycle_now = now or datetime.now(timezone.utc)
    spec = EvolutionSpec.load(spec_path)
    store = EvolutionStore(state_dir)
    controller_state = store.load_controller_state()
    champion_state = store.load_champion_state()

    try:
        observation = collect_current_observation(
            config=config,
            spec=spec,
            champion_state=champion_state,
            observed_at=cycle_now,
        )
    except FileNotFoundError as exc:
        if spec.stop_conditions.interrupt_on_missing_infra:
            decision = _interrupt_record(
                spec=spec,
                candidate_id=spec.active_candidate().id if spec.active_candidate() else None,
                reason=f"Missing telemetry input: {exc}",
            )
            controller_state = ControllerState(
                phase="interrupted",
                failed_rounds=controller_state.failed_rounds,
                last_run_id=controller_state.last_run_id,
                last_candidate_id=decision.candidate_id,
                last_decision=decision.decision,
                last_decision_reason=decision.reason,
                last_cycle_at=decision.ts,
                next_action="restore_telemetry_inputs",
            )
            store.append_decision(decision)
            store.save_controller_state(controller_state)
            return EvolutionCycleResult(
                spec=spec,
                state_dir=str(state_dir),
                controller_state=controller_state,
                champion_state=champion_state,
                observation=None,
                decision=decision,
            )
        raise
    except Exception as exc:
        if spec.stop_conditions.interrupt_on_data_corruption:
            decision = _interrupt_record(
                spec=spec,
                candidate_id=spec.active_candidate().id if spec.active_candidate() else None,
                reason=f"Telemetry parse failure: {exc}",
            )
            controller_state = ControllerState(
                phase="interrupted",
                failed_rounds=controller_state.failed_rounds,
                last_run_id=controller_state.last_run_id,
                last_candidate_id=decision.candidate_id,
                last_decision=decision.decision,
                last_decision_reason=decision.reason,
                last_cycle_at=decision.ts,
                next_action="repair_state_or_journal_payloads",
            )
            store.append_decision(decision)
            store.save_controller_state(controller_state)
            return EvolutionCycleResult(
                spec=spec,
                state_dir=str(state_dir),
                controller_state=controller_state,
                champion_state=champion_state,
                observation=None,
                decision=decision,
            )
        raise

    active_candidate = spec.active_candidate()
    interrupt_reason = detect_interrupt_reason(
        runtime_state=observation.runtime_state,
        runtime_reason=observation.runtime_reason,
        stop_conditions=spec.stop_conditions,
    )
    if interrupt_reason:
        decision = _interrupt_record(
            spec=spec,
            candidate_id=active_candidate.id if active_candidate else observation.candidate_id,
            reason=interrupt_reason,
            observation=observation,
        )
        controller_state = ControllerState(
            phase="interrupted",
            failed_rounds=controller_state.failed_rounds,
            last_run_id=observation.run_id,
            last_candidate_id=decision.candidate_id,
            last_decision=decision.decision,
            last_decision_reason=decision.reason,
            last_cycle_at=decision.ts,
            next_action="investigate_hard_stop",
        )
    elif active_candidate is None:
        decision = _decision_record(
            spec=spec,
            decision="ready_for_optimization",
            reason="No active challenger in spec; the next automatic step is to generate the next bounded experiment.",
            candidate_id=None,
            next_action="generate_next_candidate",
            observation=observation,
        )
        controller_state = ControllerState(
            phase="idle",
            failed_rounds=controller_state.failed_rounds,
            last_run_id=observation.run_id,
            last_candidate_id=None,
            last_decision=decision.decision,
            last_decision_reason=decision.reason,
            last_cycle_at=decision.ts,
            next_action=decision.next_action,
        )
    else:
        decision, controller_state, champion_state = _decide_candidate_action(
            spec=spec,
            store=store,
            candidate=active_candidate,
            observation=observation,
            controller_state=controller_state,
            champion_state=champion_state,
            cycle_now=cycle_now,
        )

    store.append_observation(observation)
    store.append_decision(decision)
    store.save_controller_state(controller_state)
    if champion_state is not None:
        store.save_champion_state(champion_state)
        if decision.decision == "promote":
            store.append_champion_history(champion_state)

    return EvolutionCycleResult(
        spec=spec,
        state_dir=str(state_dir),
        controller_state=controller_state,
        champion_state=champion_state,
        observation=observation,
        decision=decision,
    )


def render_evolution_status(
    *,
    config: BotConfig,
    spec_path: str | Path,
    state_dir: str | Path,
) -> str:
    spec = EvolutionSpec.load(spec_path)
    store = EvolutionStore(state_dir)
    controller_state = store.load_controller_state()
    champion_state = store.load_champion_state()
    latest_decision = store.load_latest_decision()
    latest_observation = store.load_latest_observation()

    if latest_observation is None:
        try:
            latest_observation = collect_current_observation(config=config, spec=spec, champion_state=champion_state)
        except Exception:
            latest_observation = None

    active_candidate = spec.active_candidate()
    lines = [
        "Evolution Status",
        f"spec_path: {spec.path}",
        f"state_dir: {Path(state_dir)}",
        f"primary_metric: {spec.target.primary_metric}",
        f"baseline: {_format_decimal(champion_state.metric_value if champion_state else spec.target.current_value)}",
        f"target: {_format_decimal(spec.target.target_value)}",
        f"active_candidate: {active_candidate.id if active_candidate else 'none'}",
        f"controller_phase: {controller_state.phase}",
        f"failed_rounds: {controller_state.failed_rounds}",
        f"next_action: {controller_state.next_action or 'none'}",
    ]
    if latest_observation is not None:
        lines.extend(
            [
                f"latest_run_id: {latest_observation.run_id or 'none'}",
                f"latest_metric: {_format_decimal(latest_observation.metric_value)}",
                f"latest_fill_count: {latest_observation.fill_count}",
                f"runtime_state: {latest_observation.runtime_state or 'unknown'}",
                f"runtime_reason: {latest_observation.runtime_reason or 'none'}",
            ]
        )
    if champion_state is not None:
        lines.extend(
            [
                f"champion_candidate: {champion_state.candidate_id}",
                f"champion_metric: {_format_decimal(champion_state.metric_value)}",
                f"champion_promoted_at: {champion_state.promoted_at}",
            ]
        )
    if latest_decision is not None:
        lines.extend(
            [
                f"last_decision: {latest_decision.decision}",
                f"last_decision_reason: {latest_decision.reason}",
            ]
        )
    return "\n".join(lines)


def render_cycle_result(result: EvolutionCycleResult) -> str:
    decision = result.decision
    observation = result.observation
    lines = [
        "Evolution Cycle",
        f"decision: {decision.decision if decision else 'none'}",
        f"reason: {decision.reason if decision else 'none'}",
        f"next_action: {decision.next_action if decision and decision.next_action else 'none'}",
        f"failed_rounds: {result.controller_state.failed_rounds}",
    ]
    if observation is not None:
        lines.extend(
            [
                f"run_id: {observation.run_id or 'none'}",
                f"candidate_id: {observation.candidate_id or 'none'}",
                f"metric({observation.metric_name}): {_format_decimal(observation.metric_value)}",
                f"baseline: {_format_decimal(observation.baseline_value)}",
                f"fills: {observation.fill_count}",
                f"turnover_quote: {_format_decimal(observation.turnover_quote)}",
                f"runtime_state: {observation.runtime_state or 'unknown'}",
                f"runtime_reason: {observation.runtime_reason or 'none'}",
            ]
        )
    return "\n".join(lines)


def collect_current_observation(
    *,
    config: BotConfig,
    spec: EvolutionSpec,
    champion_state: ChampionState | None,
    observed_at: datetime | None = None,
) -> ObservationSummary:
    journal_path = Path(config.telemetry.journal_path)
    state_path = Path(config.telemetry.state_path)
    if not journal_path.exists():
        raise FileNotFoundError(journal_path)
    if not state_path.exists():
        raise FileNotFoundError(state_path)

    snapshot = _load_snapshot(state_path)
    run_id, run_records = _load_target_run_records(
        journal_path,
        preferred_run_id=str(snapshot.get("run_id")) if snapshot.get("run_id") else None,
    )
    summaries = _summarize_reason_attribution_for_records(run_records=run_records, state_path=state_path)
    aggregates = _aggregate_reason_summaries(summaries)
    paused_runtime_share = _paused_runtime_share(run_records)
    first_event_ts_ms = min((record.get("ts_ms") or 0) for record in run_records) if run_records else None
    last_event_ts_ms = max((record.get("ts_ms") or 0) for record in run_records) if run_records else None
    baseline_value = champion_state.metric_value if champion_state and champion_state.metric_name == spec.target.primary_metric else spec.target.current_value
    metric_value = _select_metric_value(
        metric_name=spec.target.primary_metric,
        aggregates=aggregates,
        paused_runtime_share=paused_runtime_share,
    )
    return ObservationSummary(
        observed_at=(observed_at or datetime.now(timezone.utc)).isoformat(),
        run_id=run_id,
        candidate_id=spec.active_candidate().id if spec.active_candidate() else None,
        metric_name=spec.target.primary_metric,
        metric_value=metric_value,
        baseline_value=baseline_value,
        target_value=spec.target.target_value,
        fill_count=aggregates["fill_count"],
        turnover_quote=aggregates["turnover_quote"],
        realized_pnl_quote=aggregates["realized_pnl_quote"],
        realized_per_10k_turnover=aggregates["realized_per_10k_turnover"],
        rebalance_release_ratio=aggregates["rebalance_release_ratio"],
        paused_runtime_share=paused_runtime_share,
        runtime_state=str(snapshot.get("runtime_state")) if snapshot.get("runtime_state") is not None else None,
        runtime_reason=str(snapshot.get("runtime_reason")) if snapshot.get("runtime_reason") is not None else None,
        first_event_ts_ms=first_event_ts_ms,
        last_event_ts_ms=last_event_ts_ms,
    )


def detect_interrupt_reason(*, runtime_state: str | None, runtime_reason: str | None, stop_conditions: Any) -> str | None:
    reason = str(runtime_reason or "").strip().lower()
    state = str(runtime_state or "").strip().upper()

    risk_keywords = ("loss limit", "drawdown", "risk breach", "inventory breach", "exposure breach")
    infra_keywords = ("stream", "reconnect", "stale", "resync", "instrument not approved", "not ready", "missing")
    bias_keywords = ("lookahead", "recursive bias", "evaluator bias", "validation leakage", "data leakage", "label leakage")

    if stop_conditions.interrupt_on_bias and any(keyword in reason for keyword in bias_keywords):
        return f"Bias or leakage indicator detected in runtime_reason={runtime_reason!r}."
    if stop_conditions.interrupt_on_risk_breach and any(keyword in reason for keyword in risk_keywords):
        return f"Risk guardrail breach detected in runtime_reason={runtime_reason!r}."
    if stop_conditions.interrupt_on_missing_infra and state in {"PAUSED", "STOPPED"} and any(
        keyword in reason for keyword in infra_keywords
    ):
        return f"Infrastructure stop detected in runtime_reason={runtime_reason!r}."
    return None


def _decide_candidate_action(
    *,
    spec: EvolutionSpec,
    store: EvolutionStore,
    candidate: CandidateExperiment,
    observation: ObservationSummary,
    controller_state: ControllerState,
    champion_state: ChampionState | None,
    cycle_now: datetime,
) -> tuple[DecisionRecord, ControllerState, ChampionState | None]:
    if observation.fill_count < spec.constraints.min_trade_count:
        decision = _decision_record(
            spec=spec,
            decision="continue_observing",
            reason=(
                f"Observation fill_count={observation.fill_count} is below min_trade_count="
                f"{spec.constraints.min_trade_count}."
            ),
            candidate_id=candidate.id,
            next_action="wait_for_more_samples",
            observation=observation,
        )
        next_state = ControllerState(
            phase="observing",
            failed_rounds=controller_state.failed_rounds,
            last_run_id=observation.run_id,
            last_candidate_id=candidate.id,
            last_decision=decision.decision,
            last_decision_reason=decision.reason,
            last_cycle_at=decision.ts,
            next_action=decision.next_action,
        )
        return decision, next_state, champion_state

    candidate_observations = store.load_observations(candidate_id=candidate.id) + [observation]
    if not _promotion_window_satisfied(candidate_observations, spec=spec, cycle_now=cycle_now):
        decision = _decision_record(
            spec=spec,
            decision="continue_observing",
            reason=(
                f"Candidate {candidate.id} has not satisfied the observation window "
                f"required by promotion.paper_days={spec.promotion.paper_days}."
            ),
            candidate_id=candidate.id,
            next_action="keep_collecting_shadow_or_paper_results",
            observation=observation,
        )
        next_state = ControllerState(
            phase="observing",
            failed_rounds=controller_state.failed_rounds,
            last_run_id=observation.run_id,
            last_candidate_id=candidate.id,
            last_decision=decision.decision,
            last_decision_reason=decision.reason,
            last_cycle_at=decision.ts,
            next_action=decision.next_action,
        )
        return decision, next_state, champion_state

    if _is_improvement(
        metric_value=observation.metric_value,
        baseline_value=observation.baseline_value,
        target_value=observation.target_value,
        optimize_direction=spec.target.optimize_direction,
    ):
        decision = _decision_record(
            spec=spec,
            decision="promote",
            reason=f"Candidate {candidate.id} improved {spec.target.primary_metric} against the current baseline.",
            candidate_id=candidate.id,
            next_action="mark_candidate_as_champion",
            observation=observation,
        )
        next_state = ControllerState(
            phase="idle",
            failed_rounds=0,
            last_run_id=observation.run_id,
            last_candidate_id=candidate.id,
            last_decision=decision.decision,
            last_decision_reason=decision.reason,
            last_cycle_at=decision.ts,
            next_action="prepare_next_challenger",
        )
        next_champion = ChampionState(
            candidate_id=candidate.id,
            metric_name=observation.metric_name,
            metric_value=observation.metric_value,
            promoted_at=decision.ts,
            run_id=observation.run_id,
            observation_count=len(candidate_observations),
        )
        return decision, next_state, next_champion

    failed_rounds = controller_state.failed_rounds + 1
    next_action = "narrow_search" if failed_rounds >= spec.stop_conditions.max_failed_rounds_before_narrowing else "generate_next_candidate"
    decision = _decision_record(
        spec=spec,
        decision="reject",
        reason=f"Candidate {candidate.id} did not beat the baseline after the required observation window.",
        candidate_id=candidate.id,
        next_action=next_action,
        observation=observation,
    )
    next_state = ControllerState(
        phase="idle",
        failed_rounds=failed_rounds,
        last_run_id=observation.run_id,
        last_candidate_id=candidate.id,
        last_decision=decision.decision,
        last_decision_reason=decision.reason,
        last_cycle_at=decision.ts,
        next_action=next_action,
    )
    return decision, next_state, champion_state


def _aggregate_reason_summaries(summaries: list[ReasonBucketSummary]) -> dict[str, Any]:
    turnover_quote = Decimal("0")
    realized_pnl_quote = Decimal("0")
    fill_count = 0
    rebalance_release_turnover = Decimal("0")
    for summary in summaries:
        fill_count += int(summary.fill_count)
        turnover_quote += Decimal(summary.turnover_quote)
        realized_pnl_quote += Decimal(summary.realized_pnl_quote)
        if summary.bucket in {"rebalance", "release"}:
            rebalance_release_turnover += Decimal(summary.turnover_quote)
    rebalance_release_ratio = None
    if turnover_quote > 0:
        rebalance_release_ratio = rebalance_release_turnover / turnover_quote
    return {
        "fill_count": fill_count,
        "turnover_quote": turnover_quote,
        "realized_pnl_quote": realized_pnl_quote,
        "realized_per_10k_turnover": realized_per_10k_turnover(
            realized_pnl_quote=realized_pnl_quote,
            turnover_quote=turnover_quote,
        ),
        "rebalance_release_ratio": rebalance_release_ratio,
    }


def _select_metric_value(
    *,
    metric_name: str,
    aggregates: dict[str, Any],
    paused_runtime_share: Decimal | None,
) -> Decimal | None:
    if metric_name == "paused_runtime_share":
        return paused_runtime_share
    if metric_name == "fill_count":
        return Decimal(str(aggregates["fill_count"]))
    if metric_name in {"realized_per_10k_turnover", "realized_pnl_quote", "turnover_quote", "rebalance_release_ratio"}:
        return aggregates.get(metric_name)
    raise ValueError(f"Unsupported evolution primary_metric={metric_name!r}")


def _promotion_window_satisfied(
    observations: list[ObservationSummary],
    *,
    spec: EvolutionSpec,
    cycle_now: datetime,
) -> bool:
    if not spec.promotion.require_paper_or_shadow:
        return True
    if spec.promotion.paper_days <= 0:
        return True
    timestamps = [datetime.fromisoformat(item.observed_at) for item in observations if item.observed_at]
    if not timestamps:
        return False
    return cycle_now - min(timestamps) >= timedelta(days=spec.promotion.paper_days)


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
        if direction == "minimize":
            return metric_value < baseline_value
        return metric_value > baseline_value
    if target_value is not None:
        if direction == "minimize":
            return metric_value <= target_value
        return metric_value >= target_value
    return False


def _load_snapshot(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"State snapshot must be a JSON object: {path}")
    return payload


def _load_target_run_records(path: Path, preferred_run_id: str | None = None) -> tuple[str | None, list[dict[str, Any]]]:
    target_run_id = preferred_run_id
    collected_reversed: list[dict[str, Any]] = []
    buffer = b""
    chunk_size = 1024 * 1024

    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            buffer = handle.read(read_size) + buffer
            lines = buffer.splitlines()
            if position > 0 and lines:
                buffer = lines[0]
                lines = lines[1:]
            else:
                buffer = b""
            for raw_line in reversed(lines):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    payload = json.loads(raw_line)
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                run_id = payload.get("run_id")
                if not run_id:
                    continue
                run_id = str(run_id)
                if target_run_id is None:
                    target_run_id = run_id
                if run_id == target_run_id:
                    collected_reversed.append(payload)
                    continue
                if collected_reversed:
                    collected_reversed.reverse()
                    return target_run_id, collected_reversed
        if buffer.strip():
            try:
                payload = json.loads(buffer)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                run_id = payload.get("run_id")
                if run_id:
                    run_id = str(run_id)
                    if target_run_id is None:
                        target_run_id = run_id
                    if run_id == target_run_id:
                        collected_reversed.append(payload)

    collected_reversed.reverse()
    return target_run_id, collected_reversed


def _summarize_reason_attribution_for_records(
    *,
    run_records: list[dict[str, Any]],
    state_path: Path,
) -> list[ReasonBucketSummary]:
    latest_intents_by_side: dict[str, list[dict[str, Any]]] = {"buy": [], "sell": []}
    reason_by_order: dict[str, str] = {}
    prev_filled_by_order: dict[str, Decimal] = {}
    lots: deque[dict[str, Any]] = deque()
    fill_count_by_bucket: Counter[str] = Counter()
    turnover_by_bucket: dict[str, Decimal] = Counter()  # type: ignore[assignment]
    pnl_by_bucket: dict[str, Decimal] = Counter()  # type: ignore[assignment]

    for record in run_records:
        event = str(record.get("event") or "")
        payload = record.get("payload") or {}

        if event == "decision":
            latest_intents_by_side = _extract_decision_intents(payload)
            continue

        if event in {"place_order", "shadow_quote"}:
            cl_ord_id = str(payload.get("clOrdId") or payload.get("cl_ord_id") or "")
            if not cl_ord_id:
                continue
            reason = str(payload.get("reason") or "")
            if not reason:
                side = str(payload.get("side") or "")
                price = _parse_decimal(payload.get("px") or payload.get("price") or "0")
                size = _parse_decimal(payload.get("sz") or payload.get("base_size") or "0")
                reason = _infer_reason_from_decision(
                    side=side,
                    price=price,
                    size=size,
                    intents=latest_intents_by_side.get(side, []),
                )
            if reason:
                reason_by_order[cl_ord_id] = reason
            continue

        if event in {"amend_order_submitted", "shadow_amend_order"}:
            cl_ord_id = str(payload.get("cl_ord_id") or payload.get("clOrdId") or "")
            reason = str(payload.get("reason") or "")
            if cl_ord_id and reason:
                reason_by_order[cl_ord_id] = reason
            continue

        if event != "order_update":
            continue

        order = payload.get("order") or {}
        cl_ord_id = str(order.get("cl_ord_id") or order.get("clOrdId") or "")
        if not cl_ord_id:
            continue
        reason = str(payload.get("reason") or reason_by_order.get(cl_ord_id) or "")
        if reason:
            reason_by_order[cl_ord_id] = reason
        bucket = classify_reason_bucket(reason)

        filled_size = _parse_decimal(order.get("filled_size") or "0")
        prev_filled = prev_filled_by_order.get(cl_ord_id, Decimal("0"))
        fill_delta = filled_size - prev_filled
        prev_filled_by_order[cl_ord_id] = filled_size
        if fill_delta <= 0:
            continue

        raw = payload.get("raw") or {}
        fill_price = _parse_decimal(raw.get("fillPx") or order.get("price") or "0")
        side = str(order.get("side") or "")
        fill_count_by_bucket[bucket] += 1
        turnover_by_bucket[bucket] += fill_delta * fill_price

        remaining = fill_delta
        if side == "buy":
            while remaining > 0 and lots and lots[0]["qty"] < 0:
                lot = lots[0]
                matched = min(remaining, -lot["qty"])
                pnl_by_bucket[bucket] += matched * (lot["price"] - fill_price)
                lot["qty"] += matched
                remaining -= matched
                if lot["qty"] == 0:
                    lots.popleft()
            if remaining > 0:
                lots.append({"qty": remaining, "price": fill_price})
        elif side == "sell":
            while remaining > 0 and lots and lots[0]["qty"] > 0:
                lot = lots[0]
                matched = min(remaining, lot["qty"])
                pnl_by_bucket[bucket] += matched * (fill_price - lot["price"])
                lot["qty"] -= matched
                remaining -= matched
                if lot["qty"] == 0:
                    lots.popleft()
            if remaining > 0:
                lots.append({"qty": -remaining, "price": fill_price})

    markout_by_reason = _load_state_markout_by_reason(state_path)
    buckets = sorted(set(fill_count_by_bucket) | set(turnover_by_bucket) | set(pnl_by_bucket) | set(markout_by_reason))
    summaries: list[ReasonBucketSummary] = []
    for bucket in buckets:
        markout = markout_by_reason.get(bucket) or {}
        summaries.append(
            ReasonBucketSummary(
                bucket=bucket,
                fill_count=int(fill_count_by_bucket.get(bucket, 0)),
                turnover_quote=Decimal(turnover_by_bucket.get(bucket, Decimal("0"))),
                realized_pnl_quote=Decimal(pnl_by_bucket.get(bucket, Decimal("0"))),
                realized_per_10k_turnover=realized_per_10k_turnover(
                    realized_pnl_quote=Decimal(pnl_by_bucket.get(bucket, Decimal("0"))),
                    turnover_quote=Decimal(turnover_by_bucket.get(bucket, Decimal("0"))),
                ),
                avg_adverse_ticks_300ms=_parse_decimal((markout.get("300") or {}).get("avg_adverse_ticks")) if markout.get("300") else None,
                avg_adverse_ticks_1000ms=_parse_decimal((markout.get("1000") or {}).get("avg_adverse_ticks")) if markout.get("1000") else None,
                avg_adverse_ticks_2000ms=_parse_decimal((markout.get("2000") or {}).get("avg_adverse_ticks")) if markout.get("2000") else None,
            )
        )
    summaries.sort(key=lambda item: item.turnover_quote, reverse=True)
    return summaries


def _paused_runtime_share(run_records: list[dict[str, Any]]) -> Decimal | None:
    runtime_states = [str(record.get("runtime_state") or "").upper() for record in run_records if record.get("runtime_state")]
    if not runtime_states:
        return None
    paused_count = sum(1 for item in runtime_states if item == "PAUSED")
    return Decimal(paused_count) / Decimal(len(runtime_states))


def _decision_record(
    *,
    spec: EvolutionSpec,
    decision: str,
    reason: str,
    candidate_id: str | None,
    next_action: str | None,
    observation: ObservationSummary,
) -> DecisionRecord:
    return DecisionRecord(
        ts=utc_now_iso(),
        decision=decision,
        reason=reason,
        candidate_id=candidate_id,
        run_id=observation.run_id,
        metric_name=spec.target.primary_metric,
        metric_value=observation.metric_value,
        baseline_value=observation.baseline_value,
        target_value=observation.target_value,
        next_action=next_action,
        details={
            "fill_count": observation.fill_count,
            "turnover_quote": str(observation.turnover_quote),
            "runtime_state": observation.runtime_state,
            "runtime_reason": observation.runtime_reason,
        },
    )


def _interrupt_record(
    *,
    spec: EvolutionSpec,
    candidate_id: str | None,
    reason: str,
    observation: ObservationSummary | None = None,
) -> DecisionRecord:
    metric_value = observation.metric_value if observation else None
    baseline_value = observation.baseline_value if observation else spec.target.current_value
    target_value = observation.target_value if observation else spec.target.target_value
    run_id = observation.run_id if observation else None
    return DecisionRecord(
        ts=utc_now_iso(),
        decision="interrupt",
        reason=reason,
        candidate_id=candidate_id,
        run_id=run_id,
        metric_name=spec.target.primary_metric,
        metric_value=metric_value,
        baseline_value=baseline_value,
        target_value=target_value,
        next_action="await_operator_or_data_fix",
        details={},
    )


def _format_decimal(value: Decimal | None) -> str:
    if value is None:
        return "none"
    return format(value.normalize(), "f") if value != 0 else "0"


def _parse_decimal(value: Any) -> Decimal:
    if value in (None, "", "null"):
        return Decimal("0")
    return Decimal(str(value))


def _extract_decision_intents(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    decision = (payload or {}).get("decision") or {}
    result: dict[str, list[dict[str, Any]]] = {"buy": [], "sell": []}
    for side_key, normalized_side in (("bid_layers", "buy"), ("ask_layers", "sell")):
        for layer in decision.get(side_key) or []:
            if not isinstance(layer, dict):
                continue
            result[normalized_side].append(
                {
                    "reason": str(layer.get("reason") or ""),
                    "price": _parse_decimal(layer.get("price") or "0"),
                    "base_size": _parse_decimal(layer.get("base_size") or "0"),
                }
            )
    return result


def _infer_reason_from_decision(*, side: str, price: Decimal, size: Decimal, intents: list[dict[str, Any]]) -> str:
    for intent in intents:
        if intent["price"] == price and intent["base_size"] == size:
            return str(intent["reason"] or "")
    for intent in intents:
        if intent["price"] == price:
            return str(intent["reason"] or "")
    return ""


def _load_state_markout_by_reason(state_path: Path) -> dict[str, dict[str, dict[str, Any]]]:
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    data = payload.get("fill_markout_summary_by_reason")
    return data if isinstance(data, dict) else {}
