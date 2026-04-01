from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .evolution_models import CandidateExperiment, EvolutionSpec, ExperimentRecord, utc_now_iso
from .evolution_store import EvolutionStore


@dataclass(frozen=True)
class CandidateProposalResult:
    spec_path: str
    candidate: CandidateExperiment
    action: str
    reason: str


def propose_next_candidate(*, spec_path: str | Path, state_dir: str | Path) -> CandidateProposalResult:
    spec = EvolutionSpec.load(spec_path)
    store = EvolutionStore(state_dir)
    active_candidate = spec.active_candidate()
    if active_candidate is not None:
        result = CandidateProposalResult(
            spec_path=str(spec_path),
            candidate=active_candidate,
            action="reuse_active",
            reason=f"Active candidate {active_candidate.id} already exists; no new proposal was required.",
        )
        store.append_experiment(
            ExperimentRecord(
                ts=utc_now_iso(),
                action="propose_candidate",
                status="reused",
                reason=result.reason,
                candidate_id=active_candidate.id,
                trigger_decision=store.load_controller_state().last_decision,
                next_action=store.load_controller_state().next_action,
                details={},
            )
        )
        return result

    payload = _load_spec_payload(spec_path)
    candidates = payload.setdefault("candidate_experiments", [])
    if not isinstance(candidates, list):
        raise ValueError("candidate_experiments must be a list in strategy-evolution.yaml")

    controller_state = store.load_controller_state()
    if controller_state.next_action == "narrow_search":
        candidate_payload = _build_narrow_search_candidate(payload=payload, candidates=candidates, controller_state=controller_state)
        action = "synthesized_narrow_search"
    else:
        candidate_payload = _activate_existing_diagnosis_only(candidates)
        if candidate_payload is None:
            candidate_payload = _build_bottleneck_candidate(payload=payload, candidates=candidates)
            action = "synthesized_from_bottleneck"
        else:
            action = "activated_existing"

    candidate_payload["status"] = "candidate"
    _write_spec_payload(spec_path, payload)
    candidate = CandidateExperiment.from_dict(candidate_payload)
    result = CandidateProposalResult(
        spec_path=str(spec_path),
        candidate=candidate,
        action=action,
        reason=f"Prepared candidate {candidate.id} with status={candidate.status}.",
    )
    store.append_experiment(
        ExperimentRecord(
            ts=utc_now_iso(),
            action="propose_candidate",
            status=action,
            reason=result.reason,
            candidate_id=candidate.id,
            trigger_decision=controller_state.last_decision,
            next_action=controller_state.next_action,
            details={"spec_path": str(spec_path)},
        )
    )
    return result


def render_candidate_proposal(result: CandidateProposalResult) -> str:
    lines = [
        "Evolution Candidate Proposal",
        f"action: {result.action}",
        f"reason: {result.reason}",
        f"candidate_id: {result.candidate.id}",
        f"status: {result.candidate.status}",
        f"change_surface: {', '.join(result.candidate.change_surface) if result.candidate.change_surface else 'none'}",
    ]
    return "\n".join(lines)


def _load_spec_payload(path: str | Path) -> dict[str, Any]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Evolution spec must be a YAML mapping: {path}")
    return payload


def _write_spec_payload(path: str | Path, payload: dict[str, Any]) -> None:
    spec_path = Path(path)
    temp_path = spec_path.with_suffix(f"{spec_path.suffix}.tmp")
    temp_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False), encoding="utf-8")
    temp_path.replace(spec_path)


def _activate_existing_diagnosis_only(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        status = str(candidate.get("status") or "")
        if status in {"diagnosis_only", "pending", "ready"}:
            return candidate
    return None


def _build_narrow_search_candidate(
    *,
    payload: dict[str, Any],
    candidates: list[dict[str, Any]],
    controller_state: Any,
) -> dict[str, Any]:
    source = None
    if controller_state.last_candidate_id:
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("id") == controller_state.last_candidate_id:
                source = candidate
                break
    if source is None and candidates:
        source = next((item for item in reversed(candidates) if isinstance(item, dict) and item.get("id")), None)

    source_id = str(source.get("id") or "candidate") if source else "candidate"
    new_id = _next_candidate_id(base=f"{source_id}-narrow", candidates=candidates)
    base_surface = []
    if source and source.get("change_surface"):
        base_surface = [str(source["change_surface"][0])]
    if not base_surface:
        base_surface = _default_change_surface(payload)
    new_candidate = {
        "id": new_id,
        "status": "candidate",
        "hypothesis": (
            f"Narrow the search around {source_id} by isolating the smallest remaining lever "
            "instead of broadening the mutation surface."
        ),
        "change_surface": base_surface,
        "validation": [
            "focused strategy regression tests",
            "prove the narrowed lever changes the target metric without widening risk",
        ],
    }
    candidates.append(new_candidate)
    return new_candidate


def _build_bottleneck_candidate(*, payload: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    diagnosis = payload.get("diagnosis") or {}
    bottleneck = str((diagnosis or {}).get("primary_bottleneck") or "unclassified_bottleneck")
    hypothesis = _default_hypothesis_for_bottleneck(bottleneck)
    candidate_id = _next_candidate_id(base=_slugify(bottleneck), candidates=candidates)
    candidate = {
        "id": candidate_id,
        "status": "candidate",
        "hypothesis": hypothesis,
        "change_surface": _default_change_surface(payload),
        "validation": [
            "focused strategy regression tests",
            "shadow or paper evidence before promotion",
        ],
    }
    candidates.append(candidate)
    return candidate


def _default_hypothesis_for_bottleneck(bottleneck: str) -> str:
    value = bottleneck.lower()
    if "sell_drought" in value or "release" in value:
        return (
            "If release-side participation remains stalled under drought guard conditions, "
            "narrowing the sell-side release control surface should restore turnover before adding new complexity."
        )
    if "entry" in value or "churn" in value:
        return (
            "If low-edge entry churn is still the dominant drag, tightening entry gating should raise realized profit density."
        )
    if "stream" in value or "quote_visibility" in value:
        return (
            "If unstable market visibility is the bottleneck, keep the strategy surface fixed and isolate the smallest execution-timing lever that reduces downtime."
        )
    return "Isolate the smallest allowed lever tied to the current primary bottleneck and validate it before broadening the search."


def _default_change_surface(payload: dict[str, Any]) -> list[str]:
    mutation_surface = payload.get("mutation_surface") or {}
    allowed = mutation_surface.get("allowed") or []
    values = [str(item) for item in allowed if item]
    return values[:2] if values else ["parameters"]


def _next_candidate_id(*, base: str, candidates: list[dict[str, Any]]) -> str:
    existing = {str(item.get("id")) for item in candidates if isinstance(item, dict) and item.get("id")}
    if base not in existing:
        return base
    index = 2
    while f"{base}-{index}" in existing:
        index += 1
    return f"{base}-{index}"


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "candidate"
