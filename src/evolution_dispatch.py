from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

import yaml

from .evolution_cycle import default_evolution_spec_path, default_evolution_state_dir
from .evolution_models import CandidateBundle
from .evolution_models import DecisionRecord, EvolutionSpec, ExperimentRecord, ObservationSummary, utc_now_iso
from .evolution_runner import apply_evaluation_report, seed_active_candidate_bundle
from .evolution_store import EvolutionStore


DispatchExecutor = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class DispatchPlan:
    should_dispatch: bool
    reason: str
    repo_root: str
    spec_path: str
    command: list[str]
    prompt: str
    prompt_path: str | None
    output_path: str | None
    output_schema_path: str | None
    evaluation_request_path: str | None
    evaluation_template_path: str | None
    evaluation_report_path: str | None
    candidate_id: str | None
    trigger_decision: str | None
    next_action: str | None


@dataclass(frozen=True)
class DispatchExecutionResult:
    plan: DispatchPlan
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    worker_response: dict | None
    applied_report_path: str | None
    applied_decision: str | None
    record: ExperimentRecord


def build_dispatch_plan(
    *,
    config_path: str | Path,
    spec_path: str | Path | None = None,
    state_dir: str | Path | None = None,
    codex_bin: str = "codex",
) -> DispatchPlan:
    config_path = Path(config_path).resolve()
    resolved_spec_path = Path(spec_path) if spec_path is not None else default_evolution_spec_path(config_path)
    resolved_state_dir = Path(state_dir) if state_dir is not None else default_evolution_state_dir(config_path)
    repo_root = resolved_spec_path.resolve().parent
    spec = EvolutionSpec.load(resolved_spec_path)
    store = EvolutionStore(resolved_state_dir)
    controller_state = store.load_controller_state()
    latest_decision = store.load_latest_decision()
    latest_observation = store.load_latest_observation()
    active_candidate = spec.active_candidate()

    should_dispatch, reason = _should_dispatch(
        spec=spec,
        controller_phase=controller_state.phase,
        next_action=controller_state.next_action or (latest_decision.next_action if latest_decision else None),
        latest_decision=latest_decision,
        latest_observation=latest_observation,
    )
    if not should_dispatch:
        return DispatchPlan(
            should_dispatch=False,
            reason=reason,
            repo_root=str(repo_root),
            spec_path=str(resolved_spec_path),
            command=[],
            prompt="",
            prompt_path=None,
            output_path=None,
            output_schema_path=None,
            evaluation_request_path=None,
            evaluation_template_path=None,
            evaluation_report_path=None,
            candidate_id=active_candidate.id if active_candidate else None,
            trigger_decision=latest_decision.decision if latest_decision else None,
            next_action=controller_state.next_action or (latest_decision.next_action if latest_decision else None),
        )

    dispatch_dir = resolved_state_dir / "dispatch"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    dispatch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    prompt_path = dispatch_dir / f"{dispatch_id}.prompt.txt"
    output_path = dispatch_dir / f"{dispatch_id}.last-message.txt"
    output_schema_path = dispatch_dir / f"{dispatch_id}.worker-output.schema.json"
    bundle = _load_or_seed_bundle(
        spec=spec,
        store=store,
        state_dir=resolved_state_dir,
    )
    evaluation_request_path = None
    evaluation_template_path = None
    evaluation_report_path = None
    if bundle is not None:
        evaluation_request_path, evaluation_template_path, evaluation_report_path = _write_evaluation_request(
            bundle=bundle,
            dispatch_id=dispatch_id,
            spec=spec,
            state_dir=resolved_state_dir,
            config_path=config_path,
        )
    output_schema_path.write_text(json.dumps(_worker_output_schema(), ensure_ascii=False, indent=2), encoding="utf-8")
    prompt = _build_prompt(
        repo_root=repo_root,
        config_path=config_path,
        spec=spec,
        state_dir=resolved_state_dir,
        latest_decision=latest_decision,
        latest_observation=latest_observation,
        next_action=controller_state.next_action or (latest_decision.next_action if latest_decision else None),
        evaluation_request_path=evaluation_request_path,
        evaluation_template_path=evaluation_template_path,
        evaluation_report_path=evaluation_report_path,
        output_schema_path=output_schema_path,
    )
    command = [
        codex_bin,
        "exec",
        "-C",
        str(repo_root),
        "--full-auto",
        "--output-schema",
        str(output_schema_path),
        "-o",
        str(output_path),
        "-",
    ]
    return DispatchPlan(
        should_dispatch=True,
        reason=reason,
        repo_root=str(repo_root),
        spec_path=str(resolved_spec_path),
        command=command,
        prompt=prompt,
        prompt_path=str(prompt_path),
        output_path=str(output_path),
        output_schema_path=str(output_schema_path),
        evaluation_request_path=str(evaluation_request_path) if evaluation_request_path is not None else None,
        evaluation_template_path=str(evaluation_template_path) if evaluation_template_path is not None else None,
        evaluation_report_path=str(evaluation_report_path) if evaluation_report_path is not None else None,
        candidate_id=active_candidate.id if active_candidate else None,
        trigger_decision=latest_decision.decision if latest_decision else None,
        next_action=controller_state.next_action or (latest_decision.next_action if latest_decision else None),
    )


def execute_dispatch_plan(
    plan: DispatchPlan,
    *,
    state_dir: str | Path,
    executor: DispatchExecutor = subprocess.run,
) -> DispatchExecutionResult:
    store = EvolutionStore(state_dir)
    if not plan.should_dispatch:
        record = ExperimentRecord(
            ts=utc_now_iso(),
            action="codex_dispatch",
            status="skipped",
            reason=plan.reason,
            candidate_id=plan.candidate_id,
            trigger_decision=plan.trigger_decision,
            next_action=plan.next_action,
            prompt_path=plan.prompt_path,
            output_path=plan.output_path,
            command=plan.command,
            exit_code=None,
            details={},
        )
        store.append_experiment(record)
        return DispatchExecutionResult(
            plan=plan,
            status="skipped",
            exit_code=None,
            stdout="",
            stderr="",
            worker_response=None,
            applied_report_path=None,
            applied_decision=None,
            record=record,
        )

    assert plan.prompt_path is not None
    Path(plan.prompt_path).write_text(plan.prompt, encoding="utf-8")
    completed = executor(
        plan.command,
        input=plan.prompt,
        text=True,
        capture_output=True,
        check=False,
    )
    status = "dispatched" if completed.returncode == 0 else "failed"
    worker_response = _load_worker_response(plan.output_path)
    applied_report_path = None
    applied_decision = None
    details = {
        "stdout_excerpt": completed.stdout[:800],
        "stderr_excerpt": completed.stderr[:800],
    }
    if completed.returncode == 0 and worker_response:
        details["worker_response"] = worker_response
        report_path = worker_response.get("evaluation_report_path")
        report_written = bool(worker_response.get("report_written"))
        if isinstance(report_path, str) and report_written and Path(report_path).exists():
            apply_result = apply_evaluation_report(
                spec_path=plan.spec_path,
                state_dir=state_dir,
                report_path=report_path,
            )
            applied_report_path = apply_result.report_store_path
            applied_decision = apply_result.decision.decision
            details["applied_decision"] = applied_decision
            details["applied_report_path"] = applied_report_path
    record = ExperimentRecord(
        ts=utc_now_iso(),
        action="codex_dispatch",
        status=status,
        reason=plan.reason,
        candidate_id=plan.candidate_id,
        trigger_decision=plan.trigger_decision,
        next_action=plan.next_action,
        prompt_path=plan.prompt_path,
        output_path=plan.output_path,
        command=plan.command,
        exit_code=completed.returncode,
        details=details,
    )
    store.append_experiment(record)
    return DispatchExecutionResult(
        plan=plan,
        status=status,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        worker_response=worker_response,
        applied_report_path=applied_report_path,
        applied_decision=applied_decision,
        record=record,
    )


def render_dispatch_plan(plan: DispatchPlan) -> str:
    lines = [
        "Evolution Dispatch Plan",
        f"should_dispatch: {'yes' if plan.should_dispatch else 'no'}",
        f"reason: {plan.reason}",
        f"candidate_id: {plan.candidate_id or 'none'}",
        f"trigger_decision: {plan.trigger_decision or 'none'}",
        f"next_action: {plan.next_action or 'none'}",
    ]
    if plan.should_dispatch:
        lines.extend(
            [
                f"repo_root: {plan.repo_root}",
                f"prompt_path: {plan.prompt_path}",
                f"output_path: {plan.output_path}",
                f"output_schema_path: {plan.output_schema_path}",
                f"evaluation_request_path: {plan.evaluation_request_path or 'none'}",
                f"evaluation_template_path: {plan.evaluation_template_path or 'none'}",
                f"evaluation_report_path: {plan.evaluation_report_path or 'none'}",
                f"command: {shlex.join(plan.command)}",
            ]
        )
    return "\n".join(lines)


def render_dispatch_result(result: DispatchExecutionResult) -> str:
    lines = [
        "Evolution Dispatch",
        f"status: {result.status}",
        f"reason: {result.plan.reason}",
        f"candidate_id: {result.plan.candidate_id or 'none'}",
        f"trigger_decision: {result.plan.trigger_decision or 'none'}",
        f"next_action: {result.plan.next_action or 'none'}",
    ]
    if result.exit_code is not None:
        lines.append(f"exit_code: {result.exit_code}")
    if result.plan.prompt_path:
        lines.append(f"prompt_path: {result.plan.prompt_path}")
    if result.plan.output_path:
        lines.append(f"output_path: {result.plan.output_path}")
    if result.applied_report_path:
        lines.append(f"applied_report_path: {result.applied_report_path}")
    if result.applied_decision:
        lines.append(f"applied_decision: {result.applied_decision}")
    if result.stderr:
        lines.append(f"stderr_excerpt: {result.stderr[:240]}")
    return "\n".join(lines)


def _should_dispatch(
    *,
    spec: EvolutionSpec,
    controller_phase: str,
    next_action: str | None,
    latest_decision: DecisionRecord | None,
    latest_observation: ObservationSummary | None,
) -> tuple[bool, str]:
    if controller_phase == "interrupted":
        return False, "Controller is interrupted; fix the hard-stop condition before dispatching a new worker."
    if latest_decision is None:
        return False, "No controller decision has been recorded yet; run one evolution cycle first."
    if next_action in {None, "", "none"}:
        return False, "No next_action is recorded, so there is nothing deterministic to dispatch."
    if next_action in {"wait_for_more_samples", "keep_collecting_shadow_or_paper_results"}:
        return False, "Current challenger is still in observation mode."
    if next_action in {"restore_telemetry_inputs", "investigate_hard_stop", "await_operator_or_data_fix"}:
        return False, "The latest controller state requires intervention, not a new optimization worker."
    if next_action == "mark_candidate_as_champion":
        return False, "Promotion bookkeeping should complete before a new optimization worker is started."
    if next_action == "prepare_next_challenger" and _target_reached(spec=spec, observation=latest_observation):
        return False, "Primary target is already satisfied, so the controller should stay in monitoring mode."
    if next_action in {"generate_next_candidate", "narrow_search", "prepare_next_challenger"}:
        return True, f"Controller requested {next_action}, so a new bounded optimization worker should start."
    return False, f"Unsupported next_action={next_action!r}; do not dispatch automatically."


def _target_reached(*, spec: EvolutionSpec, observation: ObservationSummary | None) -> bool:
    if observation is None or observation.metric_value is None or spec.target.target_value is None:
        return False
    direction = spec.target.optimize_direction.lower().strip()
    if direction == "minimize":
        return observation.metric_value <= spec.target.target_value
    return observation.metric_value >= spec.target.target_value


def _build_prompt(
    *,
    repo_root: Path,
    config_path: Path,
    spec: EvolutionSpec,
    state_dir: Path,
    latest_decision: DecisionRecord | None,
    latest_observation: ObservationSummary | None,
    next_action: str | None,
    evaluation_request_path: Path | None,
    evaluation_template_path: Path | None,
    evaluation_report_path: Path | None,
    output_schema_path: Path,
) -> str:
    spec_rel = _rel_path(Path(spec.path), repo_root)
    state_rel = _rel_path(state_dir, repo_root)
    config_rel = _rel_path(config_path, repo_root)
    observation_lines = []
    if latest_observation is not None:
        observation_lines = [
            f"- latest_run_id: {latest_observation.run_id or 'none'}",
            f"- latest_metric ({latest_observation.metric_name}): {_fmt_decimal(latest_observation.metric_value)}",
            f"- baseline: {_fmt_decimal(latest_observation.baseline_value)}",
            f"- target: {_fmt_decimal(latest_observation.target_value)}",
            f"- fill_count: {latest_observation.fill_count}",
            f"- runtime_state: {latest_observation.runtime_state or 'unknown'}",
            f"- runtime_reason: {latest_observation.runtime_reason or 'none'}",
        ]
    decision_lines = []
    if latest_decision is not None:
        decision_lines = [
            f"- latest_decision: {latest_decision.decision}",
            f"- latest_reason: {latest_decision.reason}",
            f"- next_action: {next_action or latest_decision.next_action or 'none'}",
        ]
    return "\n".join(
        [
            "Use $quant-autonomous-evolution on the current quant repo.",
            "",
            "Autonomy requirements:",
            "- Default to auto-continue.",
            "- Do not ask me every round whether to continue.",
            "- Interrupt only on hard-risk, data corruption, evaluator bias, or missing infrastructure.",
            "",
            "Execution contract:",
            f"- Config path: `{config_rel}`",
            f"- Objective spec: `{spec_rel}`",
            f"- Controller state dir: `{state_rel}`",
            "- Read the spec and current evidence before changing anything.",
            "- Keep changes inside the allowed mutation surface from the spec.",
            "- Update the spec only when new evidence materially changes the bottleneck, candidate queue, or validation gates.",
            f"- Your final response must satisfy the JSON schema at `{_rel_path(output_schema_path, repo_root)}`.",
            "",
            "Current controller snapshot:",
            *decision_lines,
            *observation_lines,
            "",
            "Required next step:",
            f"- Perform `{next_action or 'none'}`.",
            "- If there is no active challenger, diagnose the current bottleneck and create the next bounded candidate.",
            "- If the controller asked for narrow_search, shrink the hypothesis space instead of broadening it.",
            "- Keep evidence-driven summaries concise and milestone-based.",
            "- If you perform validation or produce a measurable result, write an evaluation report YAML to the exact path provided below.",
            *(
                [f"- Prefer running `bash scripts/run_evolution_evaluator.sh REQUEST_PATH={_rel_path(evaluation_request_path, repo_root)}` so the report is generated by the repo's evaluator."]
                if evaluation_request_path is not None
                else []
            ),
            "",
            "Start by reading these files:",
            f"- `{spec_rel}`",
            f"- `{state_rel}/controller_state.json`",
            f"- `{state_rel}/observations.jsonl`",
            f"- `{state_rel}/decisions.jsonl`",
            *( [f"- `{_rel_path(evaluation_request_path, repo_root)}`"] if evaluation_request_path is not None else [] ),
            *( [f"- `{_rel_path(evaluation_template_path, repo_root)}`"] if evaluation_template_path is not None else [] ),
            "",
            *(
                [
                    "Evaluation report contract:",
                    f"- evaluation_request_path: `{_rel_path(evaluation_request_path, repo_root)}`",
                    f"- evaluation_template_path: `{_rel_path(evaluation_template_path, repo_root) if evaluation_template_path is not None else 'none'}`",
                    f"- evaluation_report_path: `{_rel_path(evaluation_report_path, repo_root) if evaluation_report_path is not None else 'none'}`",
                    "- Set `report_written=true` in the final JSON only if that file was actually written.",
                    "",
                ]
                if evaluation_request_path is not None
                else []
            ),
            "Then continue the optimization loop autonomously.",
        ]
    )


def _load_or_seed_bundle(*, spec: EvolutionSpec, store: EvolutionStore, state_dir: Path) -> CandidateBundle | None:
    active_candidate = spec.active_candidate()
    if active_candidate is None:
        return None
    bundle = store.load_candidate_bundle(active_candidate.id)
    if bundle is not None:
        return bundle
    return seed_active_candidate_bundle(spec_path=spec.path, state_dir=state_dir)


def _write_evaluation_request(
    *,
    bundle: CandidateBundle,
    dispatch_id: str,
    spec: EvolutionSpec,
    state_dir: Path,
    config_path: Path,
) -> tuple[Path, Path, Path]:
    bundle_dir = Path(bundle.bundle_dir or state_dir / "candidates" / bundle.candidate_id)
    request_dir = bundle_dir / "requests"
    request_dir.mkdir(parents=True, exist_ok=True)
    report_dir = state_dir / "inbox" / bundle.candidate_id
    report_dir.mkdir(parents=True, exist_ok=True)
    requested_stage = _requested_stage(bundle=bundle, spec=spec)
    report_path = report_dir / f"{dispatch_id}.{requested_stage}.yaml"
    request_path = request_dir / f"{dispatch_id}.evaluation-request.yaml"
    template_path = request_dir / f"{dispatch_id}.evaluation-report.template.yaml"
    payload = {
        "request_id": dispatch_id,
        "candidate_id": bundle.candidate_id,
        "requested_stage": requested_stage,
        "config_path": str(config_path),
        "spec_path": str(spec.path),
        "state_dir": str(state_dir),
        "primary_metric": spec.target.primary_metric,
        "target_value": str(spec.target.target_value) if spec.target.target_value is not None else None,
        "baseline_value": str(spec.target.current_value) if spec.target.current_value is not None else None,
        "change_surface": list(bundle.change_surface),
        "validation": list(bundle.validation),
        "evaluation_report_path": str(report_path),
    }
    request_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False), encoding="utf-8")
    template_payload = {
        "candidate_id": bundle.candidate_id,
        "stage": requested_stage,
        "status": "passed",
        "evaluated_at": "REPLACE_WITH_ISO8601",
        "summary": "Replace with the evaluation summary.",
        "metrics": {
            spec.target.primary_metric: "REPLACE_WITH_NUMERIC_VALUE",
        },
        "gate_results": [
            {"gate": "focused_tests", "passed": True, "detail": "Replace with evidence."},
        ],
        "artifacts": {
            "request_path": str(request_path),
        },
        "decision_hint": "promote",
        "run_id": "REPLACE_WITH_RUN_ID_OR_NULL",
        "baseline_value": str(spec.target.current_value) if spec.target.current_value is not None else None,
        "target_value": str(spec.target.target_value) if spec.target.target_value is not None else None,
    }
    template_path.write_text(yaml.safe_dump(template_payload, sort_keys=False, allow_unicode=False), encoding="utf-8")
    return request_path, template_path, report_path


def _requested_stage(*, bundle: CandidateBundle, spec: EvolutionSpec) -> str:
    status = bundle.status.lower()
    if "canary" in status:
        return "canary"
    if "paper" in status or "shadow" in status:
        return "paper"
    if "promoted" in status:
        return "champion"
    return "offline"


def _worker_output_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string"},
            "summary": {"type": "string"},
            "candidate_id": {"type": ["string", "null"]},
            "report_written": {"type": "boolean"},
            "evaluation_report_path": {"type": ["string", "null"]},
            "decision_hint": {"type": ["string", "null"]},
            "next_action": {"type": ["string", "null"]},
        },
        "required": [
            "status",
            "summary",
            "candidate_id",
            "report_written",
            "evaluation_report_path",
            "decision_hint",
            "next_action",
        ],
    }


def _load_worker_response(path: str | None) -> dict | None:
    if not path:
        return None
    file_path = Path(path)
    if not file_path.exists():
        return None
    raw = file_path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        try:
            payload = yaml.safe_load(raw)
        except Exception:
            return None
    return payload if isinstance(payload, dict) else None


def _rel_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return str(path)


def _fmt_decimal(value: Decimal | None) -> str:
    if value is None:
        return "none"
    return format(value.normalize(), "f") if value != 0 else "0"
