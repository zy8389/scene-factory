"""Simulator-neutral execution contracts and a symbolic dry-run executor.

The execution layer consumes validated :class:`InteractionPlan` values but
does not know how a simulator or a robot implements an action.  The dry-run
executor exists to test this boundary and applies the same symbolic transition
engine used by the planner's replay path.  It is not a physics simulator.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .planning import (
    SUPPORTED_ACTIONS,
    InteractionAction,
    InteractionPlan,
    InteractionWorldState,
    PlanFormatError,
    PlanningError,
    _SceneModel,
    _ResolvedGoal,
    _canonical_json,
    _coerce_mapping,
    _resolve_goal,
    _task_for_goal,
    apply_symbolic_interaction_action,
    validate_interaction_plan,
)
from .tasks import TaskEvaluator


EXECUTION_COMMAND_SCHEMA_VERSION = "scene_factory.execution_command.v1"
EXECUTION_TRACE_SCHEMA_VERSION = "scene_factory.execution_trace.v1"
EXECUTION_STATUSES = frozenset({"succeeded", "failed", "not_supported"})
EXECUTION_RESULTS = frozenset({"passed", "failed"})
_PLAN_HASH_LENGTH = 64
_TRACE_FIELDS = {
    "schema_version",
    "plan_sha256",
    "scene_id",
    "executor",
    "result",
    "steps",
    "final_evidence",
    "goal_status",
    "failure_reason",
    "trace_sha256",
}


class ExecutionError(ValueError):
    """A structured execution contract or orchestration failure."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionError("invalid_execution_contract", f"{field_name} must be a non-empty string")
    return value.strip()


def _strict_keys(raw: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ExecutionError(
            "invalid_execution_contract",
            f"{label} contains unsupported fields: {', '.join(unknown)}",
        )


def _json_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ExecutionError("invalid_executor_evidence", f"{label} must be a JSON object")
    try:
        encoded = _canonical_json(dict(value))
        normalized = json.loads(encoded.decode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ExecutionError("invalid_executor_evidence", f"{label} is not finite JSON: {exc}") from exc
    if not isinstance(normalized, dict):
        raise ExecutionError("invalid_executor_evidence", f"{label} must be a JSON object")
    return normalized


def _hash_text(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _plan_hash_is_valid(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _PLAN_HASH_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True)
class ExecutorCapabilities:
    """Stable, JSON-serializable declaration of executor support."""

    executor: str
    version: str
    physical: bool
    supported_actions: frozenset[str] = field(default_factory=frozenset)
    articulation_execution: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "executor", _text(self.executor, "executor"))
        object.__setattr__(self, "version", _text(self.version, "version"))
        if not isinstance(self.physical, bool):
            raise ExecutionError("invalid_executor_capabilities", "physical must be a boolean")
        if not isinstance(self.articulation_execution, bool):
            raise ExecutionError(
                "invalid_executor_capabilities", "articulation_execution must be a boolean"
            )
        if isinstance(self.supported_actions, str):
            raise ExecutionError(
                "invalid_executor_capabilities", "supported_actions must be a collection"
            )
        try:
            actions = frozenset(self.supported_actions)
        except TypeError as exc:
            raise ExecutionError(
                "invalid_executor_capabilities", "supported_actions must be a collection"
            ) from exc
        try:
            normalized_actions = frozenset(_text(action, "supported_actions item") for action in actions)
        except ExecutionError as exc:
            raise ExecutionError(
                "invalid_executor_capabilities", "supported_actions must contain non-empty strings"
            ) from exc
        object.__setattr__(self, "supported_actions", normalized_actions)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExecutorCapabilities":
        if not isinstance(raw, Mapping):
            raise ExecutionError("invalid_executor_capabilities", "executor must be an object")
        _strict_keys(
            raw,
            {"executor", "version", "physical", "supported_actions", "articulation_execution"},
            "executor capabilities",
        )
        required = {"executor", "version", "physical", "supported_actions", "articulation_execution"}
        missing = sorted(required - set(raw))
        if missing:
            raise ExecutionError(
                "invalid_executor_capabilities",
                f"executor capabilities missing: {', '.join(missing)}",
            )
        actions = raw["supported_actions"]
        if not isinstance(actions, list):
            raise ExecutionError(
                "invalid_executor_capabilities", "supported_actions must be an array"
            )
        try:
            return cls(
                executor=raw["executor"],
                version=raw["version"],
                physical=raw["physical"],
                supported_actions=frozenset(actions),
                articulation_execution=raw["articulation_execution"],
            )
        except ExecutionError:
            raise
        except (TypeError, ValueError) as exc:
            raise ExecutionError("invalid_executor_capabilities", str(exc)) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "executor": self.executor,
            "version": self.version,
            "physical": self.physical,
            "supported_actions": sorted(self.supported_actions),
            "articulation_execution": self.articulation_execution,
        }


def command_id_for(plan_sha256: str, step_id: int) -> str:
    """Return the deterministic ID for one plan step."""

    if not _plan_hash_is_valid(plan_sha256):
        raise ExecutionError("invalid_command", "plan_sha256 must be a lowercase SHA-256 string")
    if isinstance(step_id, bool) or not isinstance(step_id, int) or step_id < 0:
        raise ExecutionError("invalid_command", "step_id must be a non-negative integer")
    return f"{plan_sha256}:{step_id:06d}"


@dataclass(frozen=True)
class ExecutionCommand:
    """Versioned executor-facing envelope around one planner action."""

    schema_version: str
    command_id: str
    plan_sha256: str
    step_id: int
    action: InteractionAction

    def __post_init__(self) -> None:
        if self.schema_version != EXECUTION_COMMAND_SCHEMA_VERSION:
            raise ExecutionError(
                "unsupported_schema_version",
                f"unsupported command schema version: {self.schema_version!r}",
            )
        if not _plan_hash_is_valid(self.plan_sha256):
            raise ExecutionError("invalid_command", "plan_sha256 must be a lowercase SHA-256 string")
        if isinstance(self.step_id, bool) or not isinstance(self.step_id, int) or self.step_id < 0:
            raise ExecutionError("invalid_command", "step_id must be a non-negative integer")
        if not isinstance(self.action, InteractionAction):
            raise ExecutionError("invalid_command", "action must be an InteractionAction")
        if self.action.step_id != self.step_id:
            raise ExecutionError("invalid_command", "action.step_id must match command.step_id")
        expected_id = command_id_for(self.plan_sha256, self.step_id)
        if self.command_id != expected_id:
            raise ExecutionError("invalid_command_id", "command_id does not match plan and step")

    @classmethod
    def from_action(cls, plan_sha256: str, action: InteractionAction) -> "ExecutionCommand":
        return cls(
            schema_version=EXECUTION_COMMAND_SCHEMA_VERSION,
            command_id=command_id_for(plan_sha256, action.step_id),
            plan_sha256=plan_sha256,
            step_id=action.step_id,
            action=action,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExecutionCommand":
        if not isinstance(raw, Mapping):
            raise ExecutionError("invalid_command", "command must be an object")
        _strict_keys(raw, {"schema_version", "command_id", "plan_sha256", "step_id", "action"}, "command")
        try:
            return cls(
                schema_version=raw["schema_version"],
                command_id=raw["command_id"],
                plan_sha256=raw["plan_sha256"],
                step_id=raw["step_id"],
                action=InteractionAction.from_dict(raw["action"]),
            )
        except ExecutionError:
            raise
        except (KeyError, TypeError, ValueError, PlanFormatError) as exc:
            raise ExecutionError("invalid_command", str(exc)) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "command_id": self.command_id,
            "plan_sha256": self.plan_sha256,
            "step_id": self.step_id,
            "action": self.action.to_dict(),
        }


@dataclass(frozen=True)
class ExecutionStepResult:
    """Synchronous result correlated to one :class:`ExecutionCommand`."""

    command_id: str
    step_id: int
    status: str
    reason: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.command_id, str) or not self.command_id:
            raise ExecutionError("invalid_step_result", "command_id must be a non-empty string")
        if isinstance(self.step_id, bool) or not isinstance(self.step_id, int) or self.step_id < 0:
            raise ExecutionError("invalid_step_result", "step_id must be a non-negative integer")
        if self.status not in EXECUTION_STATUSES:
            raise ExecutionError("invalid_step_result", f"unsupported result status: {self.status!r}")
        if self.reason is not None and (not isinstance(self.reason, str) or not self.reason.strip()):
            raise ExecutionError("invalid_step_result", "reason must be null or a non-empty string")
        if self.status == "succeeded" and self.reason is not None:
            raise ExecutionError("invalid_step_result", "succeeded results cannot have a reason")
        if self.status != "succeeded" and self.reason is None:
            raise ExecutionError("invalid_step_result", "failed results require a reason")
        object.__setattr__(self, "evidence", _json_object(self.evidence, "step evidence"))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExecutionStepResult":
        if not isinstance(raw, Mapping):
            raise ExecutionError("invalid_step_result", "step result must be an object")
        _strict_keys(raw, {"command_id", "step_id", "status", "reason", "evidence"}, "step result")
        required = {"command_id", "step_id", "status", "reason", "evidence"}
        missing = sorted(required - set(raw))
        if missing:
            raise ExecutionError(
                "invalid_step_result", f"step result missing: {', '.join(missing)}"
            )
        try:
            return cls(
                command_id=raw["command_id"],
                step_id=raw["step_id"],
                status=raw["status"],
                reason=raw["reason"],
                evidence=raw["evidence"],
            )
        except ExecutionError:
            raise
        except (TypeError, ValueError) as exc:
            raise ExecutionError("invalid_step_result", str(exc)) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "step_id": self.step_id,
            "status": self.status,
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class ExecutionTraceStep:
    """One auditable command/result pair in an execution trace."""

    command: ExecutionCommand
    result: ExecutionStepResult

    def __post_init__(self) -> None:
        if not isinstance(self.command, ExecutionCommand):
            raise ExecutionError("invalid_execution_trace", "trace command is invalid")
        if not isinstance(self.result, ExecutionStepResult):
            raise ExecutionError("invalid_execution_trace", "trace result is invalid")
        if self.command.command_id != self.result.command_id or self.command.step_id != self.result.step_id:
            raise ExecutionError("executor_result_mismatch", "trace command/result correlation failed")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExecutionTraceStep":
        if not isinstance(raw, Mapping):
            raise ExecutionError("invalid_execution_trace", "trace step must be an object")
        _strict_keys(raw, {"command", "result"}, "trace step")
        try:
            return cls(
                command=ExecutionCommand.from_dict(raw["command"]),
                result=ExecutionStepResult.from_dict(raw["result"]),
            )
        except ExecutionError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutionError("invalid_execution_trace", str(exc)) from exc

    def to_dict(self) -> dict[str, Any]:
        return {"command": self.command.to_dict(), "result": self.result.to_dict()}


def _trace_semantics(
    schema_version: str,
    plan_sha256: str,
    scene_id: str | None,
    executor: ExecutorCapabilities,
    result: str,
    steps: tuple[ExecutionTraceStep, ...],
    final_evidence: Mapping[str, Any],
    goal_status: Mapping[str, Any],
    failure_reason: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "plan_sha256": plan_sha256,
        "scene_id": scene_id,
        "executor": executor.to_dict(),
        "result": result,
        "steps": [step.to_dict() for step in steps],
        "final_evidence": dict(final_evidence),
        "goal_status": dict(goal_status),
        "failure_reason": failure_reason,
    }


@dataclass(frozen=True)
class ExecutionTrace:
    """Deterministic semantic record of one executor lifecycle."""

    schema_version: str
    plan_sha256: str
    scene_id: str | None
    executor: ExecutorCapabilities
    result: str
    steps: tuple[ExecutionTraceStep, ...] = ()
    final_evidence: Mapping[str, Any] = field(default_factory=dict)
    goal_status: Mapping[str, Any] = field(default_factory=dict)
    failure_reason: str | None = None
    trace_sha256: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != EXECUTION_TRACE_SCHEMA_VERSION:
            raise ExecutionError(
                "unsupported_schema_version",
                f"unsupported trace schema version: {self.schema_version!r}",
            )
        if not _plan_hash_is_valid(self.plan_sha256):
            raise ExecutionError("invalid_execution_trace", "trace plan_sha256 is invalid")
        if self.scene_id is not None and (
            not isinstance(self.scene_id, str) or not self.scene_id.strip()
        ):
            raise ExecutionError("invalid_execution_trace", "scene_id must be null or a non-empty string")
        if not isinstance(self.executor, ExecutorCapabilities):
            raise ExecutionError("invalid_execution_trace", "trace executor capabilities are invalid")
        if self.result not in EXECUTION_RESULTS:
            raise ExecutionError("invalid_execution_trace", f"unsupported trace result: {self.result!r}")
        if self.failure_reason is not None and (
            not isinstance(self.failure_reason, str) or not self.failure_reason.strip()
        ):
            raise ExecutionError("invalid_execution_trace", "failure_reason must be null or non-empty")
        if self.result == "passed" and self.failure_reason is not None:
            raise ExecutionError("invalid_execution_trace", "passed traces cannot have failure_reason")
        if self.result == "failed" and self.failure_reason is None:
            raise ExecutionError("invalid_execution_trace", "failed traces require failure_reason")
        if not isinstance(self.steps, tuple):
            object.__setattr__(self, "steps", tuple(self.steps))
        for expected_id, trace_step in enumerate(self.steps):
            if not isinstance(trace_step, ExecutionTraceStep):
                raise ExecutionError("invalid_execution_trace", "trace steps are invalid")
            if trace_step.command.step_id != expected_id:
                raise ExecutionError("invalid_execution_trace", "trace step IDs must be contiguous")
        final_evidence = _json_object(self.final_evidence, "final evidence")
        goal_status = _json_object(self.goal_status, "goal status")
        object.__setattr__(self, "final_evidence", final_evidence)
        object.__setattr__(self, "goal_status", goal_status)
        calculated = _hash_text(
            _trace_semantics(
                self.schema_version,
                self.plan_sha256,
                self.scene_id,
                self.executor,
                self.result,
                self.steps,
                final_evidence,
                goal_status,
                self.failure_reason,
            )
        )
        if self.trace_sha256 and self.trace_sha256 != calculated:
            raise ExecutionError("trace_hash_mismatch", "trace_sha256 does not match semantic content")
        object.__setattr__(self, "trace_sha256", calculated)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExecutionTrace":
        if not isinstance(raw, Mapping):
            raise ExecutionError("invalid_execution_trace", "trace must be an object")
        _strict_keys(raw, _TRACE_FIELDS, "execution trace")
        if set(raw) != _TRACE_FIELDS:
            raise ExecutionError(
                "invalid_execution_trace",
                "execution trace must contain exactly: " + ", ".join(sorted(_TRACE_FIELDS)),
            )
        try:
            steps_raw = raw["steps"]
            if not isinstance(steps_raw, list):
                raise ExecutionError("invalid_execution_trace", "trace.steps must be an array")
            if not isinstance(raw["trace_sha256"], str):
                raise ExecutionError(
                    "invalid_execution_trace", "trace_sha256 must be a lowercase SHA-256 string"
                )
            return cls(
                schema_version=raw["schema_version"],
                plan_sha256=raw["plan_sha256"],
                scene_id=raw["scene_id"],
                executor=ExecutorCapabilities.from_dict(raw["executor"]),
                result=raw["result"],
                steps=tuple(ExecutionTraceStep.from_dict(item) for item in steps_raw),
                final_evidence=raw["final_evidence"],
                goal_status=raw["goal_status"],
                failure_reason=raw["failure_reason"],
                trace_sha256=raw["trace_sha256"],
            )
        except ExecutionError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutionError("invalid_execution_trace", str(exc)) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_sha256": self.plan_sha256,
            "scene_id": self.scene_id,
            "executor": self.executor.to_dict(),
            "result": self.result,
            "steps": [step.to_dict() for step in self.steps],
            "final_evidence": dict(self.final_evidence),
            "goal_status": dict(self.goal_status),
            "failure_reason": self.failure_reason,
            "trace_sha256": self.trace_sha256,
        }


@dataclass(frozen=True)
class ExecutionResult:
    """Overall task execution result and optional semantic trace."""

    result: str
    valid: bool
    failure_reason: str | None = None
    message: str | None = None
    trace: ExecutionTrace | None = None
    task_status: Mapping[str, Any] = field(default_factory=dict)
    physical_execution: bool = False

    def __post_init__(self) -> None:
        if self.result not in EXECUTION_RESULTS:
            raise ExecutionError("invalid_execution_result", f"unsupported result: {self.result!r}")
        if not isinstance(self.valid, bool):
            raise ExecutionError("invalid_execution_result", "valid must be a boolean")
        if self.result == "passed" and not self.valid:
            raise ExecutionError("invalid_execution_result", "passed result must be valid")
        if self.result == "failed" and self.valid:
            raise ExecutionError("invalid_execution_result", "failed result cannot be valid")
        if self.result == "failed" and self.failure_reason is None:
            raise ExecutionError("invalid_execution_result", "failed results require failure_reason")
        if self.failure_reason is not None and (
            not isinstance(self.failure_reason, str) or not self.failure_reason.strip()
        ):
            raise ExecutionError("invalid_execution_result", "failure_reason must be null or non-empty")
        if not isinstance(self.physical_execution, bool):
            raise ExecutionError("invalid_execution_result", "physical_execution must be a boolean")
        object.__setattr__(self, "task_status", _json_object(self.task_status, "task status"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "valid": self.valid,
            "failure_reason": self.failure_reason,
            "message": self.message,
            "trace": self.trace.to_dict() if self.trace is not None else None,
            "task_status": dict(self.task_status),
            "physical_execution": self.physical_execution,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


@dataclass(frozen=True)
class ExecutionTraceValidationResult:
    """Result of validating a serialized trace without executing it."""

    result: str
    valid: bool
    failure_reason: str | None = None
    message: str | None = None
    trace: ExecutionTrace | None = None
    trace_result: str | None = None
    task_status: Mapping[str, Any] = field(default_factory=dict)
    physical_execution: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "valid": self.valid,
            "failure_reason": self.failure_reason,
            "message": self.message,
            "trace": self.trace.to_dict() if self.trace is not None else None,
            "trace_result": self.trace_result,
            "task_status": dict(self.task_status),
            "physical_execution": self.physical_execution,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


@runtime_checkable
class InteractionExecutor(Protocol):
    """Narrow synchronous boundary for future physical executors."""

    def capabilities(self) -> ExecutorCapabilities: ...

    def reset(self, scene: Mapping[str, Any], initial_state: InteractionWorldState) -> None: ...

    def execute(self, command: ExecutionCommand) -> ExecutionStepResult: ...

    def snapshot(self) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


class DryRunInteractionExecutor:
    """Apply symbolic effects without claiming physical execution."""

    def __init__(self) -> None:
        self._scene_model: _SceneModel | None = None
        self._world: InteractionWorldState | None = None
        self._closed = True

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            executor="dry_run",
            version="1",
            physical=False,
            supported_actions=frozenset(SUPPORTED_ACTIONS),
            articulation_execution=True,
        )

    def reset(self, scene: Mapping[str, Any], initial_state: InteractionWorldState) -> None:
        if not isinstance(initial_state, InteractionWorldState):
            raise ExecutionError("executor_reset_failed", "initial_state must be an InteractionWorldState")
        scene_payload = _coerce_mapping(scene, "scene")
        self._scene_model = _SceneModel.from_payload(scene_payload)
        self._world = initial_state
        self._closed = False

    def execute(self, command: ExecutionCommand) -> ExecutionStepResult:
        if self._closed or self._scene_model is None or self._world is None:
            raise RuntimeError("dry-run executor is not reset")
        if not isinstance(command, ExecutionCommand):
            raise ExecutionError("invalid_command", "executor command is invalid")
        self._world = apply_symbolic_interaction_action(
            self._scene_model,
            self._world,
            command.action,
        )
        return ExecutionStepResult(
            command_id=command.command_id,
            step_id=command.step_id,
            status="succeeded",
            evidence={
                "articulation_positions": self._world.to_dict()["joint_positions"],
                "holding": self._world.to_dict()["holding"],
                "approached_region": self._world.to_dict()["approached_region"],
                "symbolic_effect": True,
            },
        )

    def snapshot(self) -> Mapping[str, Any]:
        if self._closed or self._world is None:
            raise RuntimeError("dry-run executor is closed or not reset")
        return self._world.to_dict()

    def close(self) -> None:
        self._scene_model = None
        self._world = None
        self._closed = True


def _safe_exception(exc: BaseException) -> dict[str, str]:
    message = " ".join(str(exc).split())[:500] or "executor raised an exception"
    return {"exception_type": type(exc).__name__, "message": message}


def _trace_for(
    plan: InteractionPlan,
    scene_id: str | None,
    capabilities: ExecutorCapabilities,
    records: list[ExecutionTraceStep],
    *,
    result: str,
    final_evidence: Mapping[str, Any] | None = None,
    goal_status: Mapping[str, Any] | None = None,
    failure_reason: str | None = None,
) -> ExecutionTrace:
    return ExecutionTrace(
        schema_version=EXECUTION_TRACE_SCHEMA_VERSION,
        plan_sha256=plan.plan_sha256,
        scene_id=scene_id,
        executor=capabilities,
        result=result,
        steps=tuple(records),
        final_evidence=final_evidence or {},
        goal_status=goal_status or {},
        failure_reason=failure_reason,
    )


def _result(
    reason: str,
    message: str,
    *,
    trace: ExecutionTrace | None = None,
    task_status: Mapping[str, Any] | None = None,
    physical_execution: bool = False,
) -> ExecutionResult:
    return ExecutionResult(
        result="failed",
        valid=False,
        failure_reason=reason,
        message=message,
        trace=trace,
        task_status=task_status or {},
        physical_execution=physical_execution,
    )


def _load_plan(plan: InteractionPlan | Mapping[str, Any] | str | Path) -> InteractionPlan:
    if isinstance(plan, InteractionPlan):
        return plan
    try:
        return InteractionPlan.from_dict(_coerce_mapping(plan, "plan"))
    except PlanningError as exc:
        raise ExecutionError("invalid_plan", str(exc)) from exc
    except (TypeError, ValueError, OSError) as exc:
        raise ExecutionError("invalid_plan", str(exc)) from exc


def _load_scene(scene: Mapping[str, Any] | str | Path | Any) -> dict[str, Any]:
    try:
        return _coerce_mapping(scene, "scene")
    except PlanningError as exc:
        raise ExecutionError("invalid_scene", str(exc)) from exc
    except (TypeError, ValueError, OSError) as exc:
        raise ExecutionError("invalid_scene", str(exc)) from exc


def _capabilities(executor: InteractionExecutor) -> ExecutorCapabilities:
    if not isinstance(executor, InteractionExecutor):
        raise ExecutionError("invalid_executor", "executor does not implement InteractionExecutor")
    try:
        raw = executor.capabilities()
        return raw if isinstance(raw, ExecutorCapabilities) else ExecutorCapabilities.from_dict(raw)
    except ExecutionError:
        raise
    except Exception as exc:
        raise ExecutionError("executor_exception", str(exc)) from exc


def _step_failure_result(
    command: ExecutionCommand,
    reason: str,
    evidence: Mapping[str, Any] | None = None,
) -> ExecutionStepResult:
    return ExecutionStepResult(
        command_id=command.command_id,
        step_id=command.step_id,
        status="failed",
        reason=reason,
        evidence=evidence or {},
    )


def _coerce_step_result(
    command: ExecutionCommand,
    raw: Any,
) -> tuple[ExecutionStepResult, str | None, str | None]:
    """Normalize a returned result and classify correlation/evidence failures."""

    if isinstance(raw, ExecutionStepResult):
        step_result = raw
    elif isinstance(raw, Mapping):
        try:
            step_result = ExecutionStepResult.from_dict(raw)
        except ExecutionError as exc:
            reason = exc.reason
            if reason in {"invalid_executor_evidence", "invalid_step_result"}:
                return (
                    _step_failure_result(command, "invalid_executor_evidence", {"message": str(exc)}),
                    "invalid_executor_evidence",
                    str(exc),
                )
            return (
                _step_failure_result(command, "executor_result_mismatch", {"message": str(exc)}),
                "executor_result_mismatch",
                str(exc),
            )
    else:
        message = "executor returned a non-JSON step result"
        return (
            _step_failure_result(command, "invalid_executor_evidence", {"message": message}),
            "invalid_executor_evidence",
            message,
        )

    if step_result.command_id != command.command_id or step_result.step_id != command.step_id:
        return (
            _step_failure_result(
                command,
                "executor_result_mismatch",
                {
                    "returned_command_id": step_result.command_id,
                    "returned_step_id": step_result.step_id,
                },
            ),
            "executor_result_mismatch",
            "executor result does not match command correlation",
        )
    return step_result, None, None


def _joint_positions(snapshot: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for key in ("joint_positions", "articulation_positions"):
        candidate = snapshot.get(key)
        if isinstance(candidate, Mapping):
            return candidate
    return None


def _evaluate_final_goal(
    scene_model: _SceneModel,
    goal: _ResolvedGoal,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    positions = _joint_positions(snapshot)
    if positions is None:
        return {"task_success": False, "reason": "missing_articulation_positions"}
    evaluator = TaskEvaluator(_task_for_goal(goal), {})
    return evaluator.status({}, {"articulation_positions": positions})


def execute_interaction_plan(
    scene: Mapping[str, Any] | str | Path | Any,
    plan: InteractionPlan | Mapping[str, Any] | str | Path,
    executor: InteractionExecutor,
) -> ExecutionResult:
    """Validate, execute, and finally verify one symbolic interaction plan."""

    try:
        scene_payload = _load_scene(scene)
        plan_model = _load_plan(plan)
        scene_model = _SceneModel.from_payload(scene_payload)
        validation = validate_interaction_plan(scene_payload, plan_model)
        if not validation.valid:
            return _result(
                "invalid_plan",
                validation.message or validation.failure_reason or "plan validation failed",
            )
        resolved_goal = _resolve_goal(scene_model, plan_model.goal)
        capabilities = _capabilities(executor)
    except ExecutionError as exc:
        return _result(exc.reason, str(exc))
    except PlanningError as exc:
        return _result(exc.reason, str(exc))
    except (TypeError, ValueError, OSError) as exc:
        return _result("invalid_input", str(exc))

    unsupported = sorted(
        {action.action for action in plan_model.steps} - capabilities.supported_actions
    )
    scene_id = scene_payload.get("scene_id")
    if scene_id is not None and not isinstance(scene_id, str):
        scene_id = None
    if unsupported:
        trace = _trace_for(
            plan_model,
            scene_id,
            capabilities,
            [],
            result="failed",
            goal_status={"task_success": False},
            failure_reason="unsupported_executor_action",
        )
        return _result(
            "unsupported_executor_action",
            f"executor does not support actions: {', '.join(unsupported)}",
            trace=trace,
            physical_execution=capabilities.physical,
        )

    try:
        initial_state = InteractionWorldState.from_dict(plan_model.initial_state)
    except PlanningError as exc:
        return _result("invalid_plan", str(exc), physical_execution=capabilities.physical)

    records: list[ExecutionTraceStep] = []
    outcome: ExecutionResult | None = None
    close_error: BaseException | None = None
    lifecycle_started = False
    try:
        lifecycle_started = True
        try:
            executor.reset(scene_payload, initial_state)
        except Exception:
            trace = _trace_for(
                plan_model,
                scene_id,
                capabilities,
                records,
                result="failed",
                goal_status={"task_success": False},
                failure_reason="executor_reset_failed",
            )
            outcome = _result(
                "executor_reset_failed",
                "executor reset failed",
                trace=trace,
                physical_execution=capabilities.physical,
            )

        if outcome is None:
            for action in plan_model.steps:
                command = ExecutionCommand.from_action(plan_model.plan_sha256, action)
                try:
                    raw_result = executor.execute(command)
                    step_result, failure_reason, failure_message = _coerce_step_result(
                        command, raw_result
                    )
                except Exception as exc:
                    details = _safe_exception(exc)
                    step_result = _step_failure_result(command, "executor_exception", details)
                    failure_reason = "executor_exception"
                    failure_message = "executor raised an exception"
                records.append(ExecutionTraceStep(command, step_result))
                if failure_reason is not None:
                    trace = _trace_for(
                        plan_model,
                        scene_id,
                        capabilities,
                        records,
                        result="failed",
                        goal_status={"task_success": False},
                        failure_reason=failure_reason,
                    )
                    outcome = _result(
                        failure_reason,
                        failure_message or failure_reason,
                        trace=trace,
                        physical_execution=capabilities.physical,
                    )
                    break
                if step_result.status != "succeeded":
                    failure_reason = (
                        "unsupported_executor_action"
                        if step_result.status == "not_supported"
                        else "executor_step_failed"
                    )
                    trace = _trace_for(
                        plan_model,
                        scene_id,
                        capabilities,
                        records,
                        result="failed",
                        goal_status={"task_success": False},
                        failure_reason=failure_reason,
                    )
                    outcome = _result(
                        failure_reason,
                        step_result.reason or failure_reason,
                        trace=trace,
                        physical_execution=capabilities.physical,
                    )
                    break

        if outcome is None:
            try:
                final_snapshot = _json_object(executor.snapshot(), "final executor snapshot")
                task_status = _evaluate_final_goal(scene_model, resolved_goal, final_snapshot)
                task_success = task_status.get("task_success") is True
                failure_reason = None if task_success else "goal_not_satisfied"
                trace = _trace_for(
                    plan_model,
                    scene_id,
                    capabilities,
                    records,
                    result="passed" if task_success else "failed",
                    final_evidence=final_snapshot,
                    goal_status=task_status,
                    failure_reason=failure_reason,
                )
                if task_success:
                    outcome = ExecutionResult(
                        result="passed",
                        valid=True,
                        trace=trace,
                        task_status=task_status,
                        physical_execution=capabilities.physical,
                    )
                else:
                    outcome = _result(
                        "goal_not_satisfied",
                        "final TaskEvaluator did not satisfy the plan goal",
                        trace=trace,
                        task_status=task_status,
                        physical_execution=capabilities.physical,
                    )
            except Exception as exc:
                reason = "invalid_executor_evidence" if isinstance(exc, ExecutionError) else "executor_exception"
                trace = _trace_for(
                    plan_model,
                    scene_id,
                    capabilities,
                    records,
                    result="failed",
                    goal_status={"task_success": False},
                    failure_reason=reason,
                )
                outcome = _result(
                    reason,
                    str(exc),
                    trace=trace,
                    physical_execution=capabilities.physical,
                )
    finally:
        if lifecycle_started:
            try:
                executor.close()
            except BaseException as exc:
                close_error = exc

    if outcome is None:
        return _result("executor_exception", "executor lifecycle produced no result")
    if close_error is not None and outcome.result == "passed":
        trace = _trace_for(
            plan_model,
            scene_id,
            capabilities,
            records,
            result="failed",
            final_evidence=outcome.trace.final_evidence if outcome.trace else {},
            goal_status=outcome.task_status,
            failure_reason="executor_exception",
        )
        return _result(
            "executor_exception",
            "executor close failed",
            trace=trace,
            task_status=outcome.task_status,
            physical_execution=capabilities.physical,
        )
    return outcome


def _trace_failure(reason: str, message: str) -> ExecutionTraceValidationResult:
    return ExecutionTraceValidationResult(
        result="failed",
        valid=False,
        failure_reason=reason,
        message=message,
    )


def _trace_positions(trace: ExecutionTrace) -> Mapping[str, Any] | None:
    return _joint_positions(trace.final_evidence)


def validate_execution_trace(
    scene: Mapping[str, Any] | str | Path | Any,
    plan: InteractionPlan | Mapping[str, Any] | str | Path,
    trace: ExecutionTrace | Mapping[str, Any] | str | Path,
) -> ExecutionTraceValidationResult:
    """Validate a serialized trace without calling any executor."""

    try:
        scene_payload = _load_scene(scene)
        plan_model = _load_plan(plan)
        scene_model = _SceneModel.from_payload(scene_payload)
        if isinstance(trace, ExecutionTrace):
            trace_model = ExecutionTrace.from_dict(trace.to_dict())
        else:
            trace_model = ExecutionTrace.from_dict(_coerce_mapping(trace, "trace"))
        if trace_model.plan_sha256 != plan_model.plan_sha256:
            raise ExecutionError("plan_sha_mismatch", "trace plan_sha256 does not match the supplied plan")
        expected_scene_id = scene_payload.get("scene_id")
        if expected_scene_id is not None and trace_model.scene_id != expected_scene_id:
            raise ExecutionError("scene_id_mismatch", "trace scene_id does not match the scene")
        plan_validation = validate_interaction_plan(scene_payload, plan_model)
        if not plan_validation.valid:
            raise ExecutionError("invalid_plan", plan_validation.message or "plan validation failed")
        resolved_goal = _resolve_goal(scene_model, plan_model.goal)
        expected_commands = [
            ExecutionCommand.from_action(plan_model.plan_sha256, action)
            for action in plan_model.steps
        ]
        if trace_model.result == "passed":
            unsupported = sorted(
                {command.action.action for command in expected_commands}
                - trace_model.executor.supported_actions
            )
            if unsupported:
                raise ExecutionError(
                    "unsupported_executor_action",
                    f"trace executor does not support actions: {', '.join(unsupported)}",
                )
        if len(trace_model.steps) > len(expected_commands):
            raise ExecutionError("invalid_execution_trace", "trace contains too many executed steps")
        for index, trace_step in enumerate(trace_model.steps):
            expected = expected_commands[index]
            if trace_step.command.to_dict() != expected.to_dict():
                raise ExecutionError(
                    "command_mismatch",
                    f"trace command does not match plan step {index}",
                )
            if trace_step.result.command_id != expected.command_id or trace_step.result.step_id != expected.step_id:
                raise ExecutionError("executor_result_mismatch", "trace result correlation failed")
        statuses = [step.result.status for step in trace_model.steps]
        failed_indices = [index for index, status in enumerate(statuses) if status != "succeeded"]
        if failed_indices and failed_indices[-1] != len(statuses) - 1:
            raise ExecutionError("terminal_failure_mismatch", "trace contains steps after a failed result")
        if trace_model.result == "passed":
            if len(trace_model.steps) != len(expected_commands) or any(
                status != "succeeded" for status in statuses
            ):
                raise ExecutionError("terminal_result_mismatch", "passed trace did not execute every step")
            if trace_model.goal_status.get("task_success") is not True:
                raise ExecutionError("goal_status_mismatch", "passed trace must have task_success=true")
            if _trace_positions(trace_model) != plan_model.expected_final_state.get("joint_positions"):
                raise ExecutionError("final_evidence_mismatch", "passed final evidence differs from expected state")
        elif trace_model.failure_reason == "goal_not_satisfied":
            if failed_indices:
                raise ExecutionError("terminal_result_mismatch", "goal failure cannot contain a failed step")
            if trace_model.goal_status.get("task_success") is True:
                raise ExecutionError("goal_status_mismatch", "goal_not_satisfied trace cannot have task_success=true")
        elif trace_model.failure_reason == "executor_reset_failed":
            if trace_model.steps:
                raise ExecutionError(
                    "terminal_failure_mismatch",
                    "reset failure cannot contain executed steps",
                )
        elif trace_model.failure_reason == "unsupported_executor_action":
            if not trace_model.steps:
                pass
            elif not failed_indices or statuses[-1] != "not_supported":
                raise ExecutionError(
                    "terminal_failure_mismatch",
                    "unsupported action must be a preflight or terminal not_supported result",
                )
        elif failed_indices:
            if trace_model.failure_reason not in {
                "executor_step_failed",
                "executor_result_mismatch",
                "invalid_executor_evidence",
                "executor_exception",
                "unsupported_executor_action",
            }:
                raise ExecutionError("terminal_failure_mismatch", "trace failure reason is inconsistent with steps")
        elif trace_model.failure_reason not in {
            "executor_reset_failed",
            "executor_exception",
            "invalid_executor_evidence",
            "unsupported_executor_action",
        }:
            raise ExecutionError("terminal_failure_mismatch", "trace failure has no valid terminal condition")

        positions = _trace_positions(trace_model)
        task_status = dict(trace_model.goal_status)
        if positions is not None:
            calculated_status = _evaluate_final_goal(scene_model, resolved_goal, trace_model.final_evidence)
            if calculated_status.get("task_success") is not task_status.get("task_success"):
                raise ExecutionError("goal_status_mismatch", "trace goal_status disagrees with TaskEvaluator")
        return ExecutionTraceValidationResult(
            result="passed",
            valid=True,
            trace=trace_model,
            trace_result=trace_model.result,
            task_status=task_status,
            physical_execution=trace_model.executor.physical,
        )
    except ExecutionError as exc:
        return _trace_failure(exc.reason, str(exc))
    except PlanningError as exc:
        return _trace_failure(exc.reason, str(exc))
    except (TypeError, ValueError, OSError) as exc:
        return _trace_failure("invalid_input", str(exc))


def write_execution_trace_atomic(path: str | Path, trace: ExecutionTrace) -> None:
    """Write a stable execution trace with an atomic replace."""

    if not isinstance(trace, ExecutionTrace):
        raise ExecutionError("invalid_execution_trace", "trace must be an ExecutionTrace")
    destination = Path(path)
    encoded = json.dumps(
        trace.to_dict(), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
    ).encode("utf-8") + b"\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass
