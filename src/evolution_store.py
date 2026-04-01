from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, TypeVar

import yaml

from .evolution_models import (
    CandidateBundle,
    ChampionState,
    ControllerState,
    DecisionRecord,
    EvaluationReport,
    ExperimentRecord,
    ObservationSummary,
    to_jsonable,
)

T = TypeVar("T")


class EvolutionStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.controller_state_path = self.root / "controller_state.json"
        self.champion_path = self.root / "champion.yaml"
        self.champion_history_path = self.root / "champion_history.jsonl"
        self.observations_path = self.root / "observations.jsonl"
        self.decisions_path = self.root / "decisions.jsonl"
        self.experiments_path = self.root / "experiments.jsonl"
        self.candidates_dir = self.root / "candidates"
        self.evaluations_dir = self.root / "evaluations"
        self.candidates_dir.mkdir(parents=True, exist_ok=True)
        self.evaluations_dir.mkdir(parents=True, exist_ok=True)

    def load_controller_state(self) -> ControllerState:
        payload = self._load_json_file(self.controller_state_path)
        if payload is None:
            return ControllerState()
        if not isinstance(payload, dict):
            raise ValueError(f"Controller state must be a JSON object: {self.controller_state_path}")
        return ControllerState.from_dict(payload)

    def save_controller_state(self, state: ControllerState) -> None:
        self._write_text_atomic(
            self.controller_state_path,
            json.dumps(to_jsonable(state), ensure_ascii=False, indent=2),
        )

    def load_champion_state(self) -> ChampionState | None:
        if not self.champion_path.exists():
            return None
        payload = yaml.safe_load(self.champion_path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise ValueError(f"Champion state must be a YAML mapping: {self.champion_path}")
        return ChampionState.from_dict(payload)

    def save_champion_state(self, state: ChampionState) -> None:
        self._write_text_atomic(
            self.champion_path,
            yaml.safe_dump(to_jsonable(state), sort_keys=False, allow_unicode=False),
        )

    def append_champion_history(self, state: ChampionState) -> None:
        self._append_jsonl(self.champion_history_path, state)

    def load_champion_history(self) -> list[ChampionState]:
        return self._load_jsonl(self.champion_history_path, ChampionState.from_dict)

    def append_observation(self, observation: ObservationSummary) -> None:
        self._append_jsonl(self.observations_path, observation)

    def append_decision(self, decision: DecisionRecord) -> None:
        self._append_jsonl(self.decisions_path, decision)

    def load_observations(self, *, candidate_id: str | None = None) -> list[ObservationSummary]:
        items = self._load_jsonl(self.observations_path, ObservationSummary.from_dict)
        if candidate_id is None:
            return items
        return [item for item in items if item.candidate_id == candidate_id]

    def load_latest_observation(self) -> ObservationSummary | None:
        observations = self.load_observations()
        return observations[-1] if observations else None

    def load_decisions(self) -> list[DecisionRecord]:
        return self._load_jsonl(self.decisions_path, DecisionRecord.from_dict)

    def load_latest_decision(self) -> DecisionRecord | None:
        decisions = self.load_decisions()
        return decisions[-1] if decisions else None

    def append_experiment(self, record: ExperimentRecord) -> None:
        self._append_jsonl(self.experiments_path, record)

    def load_experiments(self) -> list[ExperimentRecord]:
        return self._load_jsonl(self.experiments_path, ExperimentRecord.from_dict)

    def load_latest_experiment(self) -> ExperimentRecord | None:
        experiments = self.load_experiments()
        return experiments[-1] if experiments else None

    def candidate_bundle_dir(self, candidate_id: str) -> Path:
        return self.candidates_dir / candidate_id

    def candidate_bundle_path(self, candidate_id: str) -> Path:
        return self.candidate_bundle_dir(candidate_id) / "candidate.yaml"

    def save_candidate_bundle(self, bundle: CandidateBundle) -> None:
        bundle_dir = self.candidate_bundle_dir(bundle.candidate_id)
        bundle_dir.mkdir(parents=True, exist_ok=True)
        self._write_text_atomic(
            self.candidate_bundle_path(bundle.candidate_id),
            yaml.safe_dump(to_jsonable(bundle), sort_keys=False, allow_unicode=False),
        )

    def load_candidate_bundle(self, candidate_id: str) -> CandidateBundle | None:
        path = self.candidate_bundle_path(candidate_id)
        if not path.exists():
            return None
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise ValueError(f"Candidate bundle must be a YAML mapping: {path}")
        return CandidateBundle.from_dict(payload)

    def evaluation_report_path(self, *, candidate_id: str, stage: str, evaluated_at: str) -> Path:
        safe_stamp = evaluated_at.replace(":", "").replace("+", "_").replace("/", "_")
        return self.evaluations_dir / candidate_id / f"{safe_stamp}.{stage}.yaml"

    def save_evaluation_report(self, report: EvaluationReport) -> Path:
        report_dir = self.evaluations_dir / report.candidate_id
        report_dir.mkdir(parents=True, exist_ok=True)
        path = self.evaluation_report_path(candidate_id=report.candidate_id, stage=report.stage, evaluated_at=report.evaluated_at)
        self._write_text_atomic(
            path,
            yaml.safe_dump(to_jsonable(report), sort_keys=False, allow_unicode=False),
        )
        return path

    def load_evaluation_report(self, path: str | Path) -> EvaluationReport:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise ValueError(f"Evaluation report must be a YAML mapping: {path}")
        return EvaluationReport.from_dict(payload)

    def load_evaluation_reports(self, *, candidate_id: str | None = None) -> list[EvaluationReport]:
        targets: list[Path]
        if candidate_id is None:
            targets = sorted(self.evaluations_dir.glob("*/*.yaml"))
        else:
            targets = sorted((self.evaluations_dir / candidate_id).glob("*.yaml"))
        reports: list[EvaluationReport] = []
        for path in targets:
            reports.append(self.load_evaluation_report(path))
        return reports

    def _append_jsonl(self, path: Path, payload: object) -> None:
        line = json.dumps(to_jsonable(payload), ensure_ascii=False) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def _load_jsonl(self, path: Path, factory: Callable[[dict], T]) -> list[T]:
        if not path.exists():
            return []
        items: list[T] = []
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError(f"JSONL entry must be an object: {path}")
                items.append(factory(payload))
        return items

    def _load_json_file(self, path: Path) -> dict | list | None:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _write_text_atomic(self, path: Path, content: str) -> None:
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(path)
