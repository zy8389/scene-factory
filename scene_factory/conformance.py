"""Offline conformance checks for :mod:`scene_factory.execution` backends.

The core suite is deliberately simulator-neutral.  It exercises the public
executor contract with planner-generated reference scenes, while keeping
physical manipulation and backend-specific performance outside this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .execution import (
    EXECUTION_RESULTS,
    DryRunInteractionExecutor,
    ExecutionCommand,
    ExecutionError,
    ExecutionStepResult,
    ExecutorCapabilities,
    InteractionExecutor,
    execute_interaction_plan,
    validate_execution_trace,
)
from .planning import (
    InteractionPlan,
    InteractionWorldState,
    SUPPORTED_ACTIONS,
    _canonical_json,
    _coerce_mapping,
    _read_json,
    plan_interaction,
)


CONFORMANCE_SCHEMA_VERSION = "scene_factory.executor_conformance.v1"
CORE_PROFILE = "core"
CONFORMANCE_RESULTS = frozenset({"passed", "failed", "not_applicable"})
EXECUTOR_NAMES = ("dry-run",)
MAX_CASE_DETAILS_BYTES = 64 * 1024
CORE_CONFORMANCE_CASES = (
    "capabilities.valid",
    "lifecycle.reset",
    "lifecycle.close",
    "lifecycle.pre_reset",
    "execution.zero_step",
    "execution.drawer_open",
    "execution.drawer_close",
    "execution.door_rotate",
    "execution.command_correlation",
    "execution.evidence",
    "execution.final_goal",
    "trace.valid",
)

_REPORT_FIELDS = {
    "schema_version",
    "profile",
    "executor",
    "capabilities",
    "capability_sha256",
    "result",
    "cases",
    "summary",
    "failure_reason",
}
_CASE_FIELDS = {"case_id", "result", "reason", "message", "details"}
_SUMMARY_FIELDS = {"total", "passed", "failed", "not_applicable"}
_REQUIRED_ACTIONS = {
    "execution.drawer_open": frozenset({"approach", "grasp", "pull", "release"}),
    "execution.drawer_close": frozenset({"approach", "grasp", "push", "release"}),
    "execution.door_rotate": frozenset({"approach", "grasp", "rotate", "release"}),
}


class ConformanceError(ValueError):
    """A malformed capability or conformance report contract."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _strict_keys(raw: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConformanceError(
            "invalid_conformance_report",
            f"{label} contains unsupported fields: {', '.join(unknown)}",
        )


def _safe_json_object(value: Any, label: str, *, max_bytes: int = MAX_CASE_DETAILS_BYTES) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConformanceError("invalid_case_details", f"{label} must be an object")
    try:
        encoded = _canonical_json(dict(value))
        normalized = json.loads(encoded.decode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ConformanceError("invalid_case_details", f"{label} is not finite JSON: {exc}") from exc
    if len(encoded) > max_bytes:
        raise ConformanceError("case_details_too_large", f"{label} exceeds {max_bytes} bytes")
    if not isinstance(normalized, dict):
        raise ConformanceError("invalid_case_details", f"{label} must be an object")
    return normalized


def _safe_message(value: Any) -> str:
    message = " ".join(str(value).split())
    return message[:500] or "conformance case failed"


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _hash_is_valid(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def normalize_executor_capabilities(
    raw: ExecutorCapabilities | Mapping[str, Any],
) -> ExecutorCapabilities:
    """Validate and normalize one executor's capability declaration."""

    if isinstance(raw, ExecutorCapabilities):
        capabilities = raw
    elif isinstance(raw, Mapping):
        actions = raw.get("supported_actions")
        if isinstance(actions, list):
            try:
                if len(actions) != len(set(actions)):
                    raise ConformanceError(
                        "invalid_capabilities", "supported_actions contains duplicate actions"
                    )
            except TypeError as exc:
                raise ConformanceError(
                    "invalid_capabilities", "supported_actions contains an unhashable action"
                ) from exc
        try:
            capabilities = ExecutorCapabilities.from_dict(raw)
        except ExecutionError as exc:
            raise ConformanceError("invalid_capabilities", str(exc)) from exc
    else:
        raise ConformanceError("invalid_capabilities", "capabilities must be an object")

    unsupported = sorted(set(capabilities.supported_actions) - SUPPORTED_ACTIONS)
    if unsupported:
        raise ConformanceError(
            "invalid_capabilities",
            f"supported_actions contains unsupported actions: {', '.join(unsupported)}",
        )
    try:
        _canonical_json(capabilities.to_dict())
    except ValueError as exc:
        raise ConformanceError("invalid_capabilities", str(exc)) from exc
    return capabilities


def capability_sha256(
    capabilities: ExecutorCapabilities | Mapping[str, Any],
) -> str:
    """Return the stable SHA-256 signature of normalized capabilities."""

    normalized = normalize_executor_capabilities(capabilities)
    return _sha256(normalized.to_dict())


@dataclass(frozen=True)
class ConformanceCaseResult:
    """One deterministic result in a conformance report."""

    case_id: str
    result: str
    reason: str | None = None
    message: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.case_id not in CORE_CONFORMANCE_CASES:
            raise ConformanceError("invalid_case_id", f"unknown conformance case: {self.case_id!r}")
        if self.result not in CONFORMANCE_RESULTS:
            raise ConformanceError("invalid_case_result", f"unsupported case result: {self.result!r}")
        if self.result == "passed" and self.reason is not None:
            raise ConformanceError("invalid_case_result", "passed cases cannot have a reason")
        if self.result != "passed" and not isinstance(self.reason, str):
            raise ConformanceError("invalid_case_result", "failed cases require a reason")
        if self.message is not None and not isinstance(self.message, str):
            raise ConformanceError("invalid_case_result", "case message must be a string or null")
        object.__setattr__(self, "details", _safe_json_object(self.details, "case details"))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ConformanceCaseResult":
        if not isinstance(raw, Mapping):
            raise ConformanceError("invalid_case_result", "case must be an object")
        _strict_keys(raw, _CASE_FIELDS, "conformance case")
        if set(raw) != _CASE_FIELDS:
            raise ConformanceError(
                "invalid_case_result",
                "conformance case must contain exactly: " + ", ".join(sorted(_CASE_FIELDS)),
            )
        return cls(
            case_id=raw["case_id"],
            result=raw["result"],
            reason=raw["reason"],
            message=raw["message"],
            details=raw["details"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "result": self.result,
            "reason": self.reason,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class ExecutorConformanceReport:
    """Serialized result of one executor's core compatibility suite."""

    schema_version: str
    profile: str
    executor: Mapping[str, Any] | None
    capabilities: Mapping[str, Any] | None
    capability_sha256: str | None
    result: str
    cases: tuple[ConformanceCaseResult, ...]
    summary: Mapping[str, int]
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != CONFORMANCE_SCHEMA_VERSION:
            raise ConformanceError(
                "unsupported_schema_version",
                f"unsupported conformance schema version: {self.schema_version!r}",
            )
        if self.profile != CORE_PROFILE:
            raise ConformanceError("unsupported_profile", f"unsupported conformance profile: {self.profile!r}")
        if self.result not in EXECUTION_RESULTS:
            raise ConformanceError("invalid_conformance_report", f"unsupported report result: {self.result!r}")
        if not isinstance(self.cases, tuple):
            object.__setattr__(self, "cases", tuple(self.cases))
        case_ids = [case.case_id for case in self.cases]
        if case_ids != list(CORE_CONFORMANCE_CASES):
            raise ConformanceError("invalid_case_order", "conformance cases must use the core case order")
        if len(set(case_ids)) != len(case_ids):
            raise ConformanceError("duplicate_case_id", "conformance cases contain duplicate IDs")
        for case in self.cases:
            if not isinstance(case, ConformanceCaseResult):
                raise ConformanceError("invalid_case_result", "conformance cases are invalid")
        if self.executor is not None:
            executor = _safe_json_object(self.executor, "executor metadata")
            required = {"name", "version", "physical"}
            if set(executor) != required:
                raise ConformanceError("invalid_conformance_report", "executor metadata fields are invalid")
            if not isinstance(executor["name"], str) or not executor["name"].strip():
                raise ConformanceError("invalid_conformance_report", "executor name must be non-empty")
            if not isinstance(executor["version"], str) or not executor["version"].strip():
                raise ConformanceError("invalid_conformance_report", "executor version must be non-empty")
            if not isinstance(executor["physical"], bool):
                raise ConformanceError("invalid_conformance_report", "executor physical must be boolean")
            object.__setattr__(self, "executor", executor)
        if self.capabilities is not None:
            normalized_capabilities = normalize_executor_capabilities(self.capabilities)
            object.__setattr__(self, "capabilities", normalized_capabilities.to_dict())
            if self.executor is not None:
                expected_executor = {
                    "name": normalized_capabilities.executor,
                    "version": normalized_capabilities.version,
                    "physical": normalized_capabilities.physical,
                }
                if dict(self.executor) != expected_executor:
                    raise ConformanceError("capability_metadata_mismatch", "executor metadata disagrees with capabilities")
            if not _hash_is_valid(self.capability_sha256):
                raise ConformanceError("invalid_capability_hash", "capability_sha256 is invalid")
            expected_hash = capability_sha256(normalized_capabilities)
            if self.capability_sha256 != expected_hash:
                raise ConformanceError("capability_hash_mismatch", "capability_sha256 does not match capabilities")
        elif self.capability_sha256 is not None:
            raise ConformanceError("invalid_capability_hash", "capability hash requires capabilities")
        elif self.executor is not None:
            raise ConformanceError("invalid_conformance_report", "executor metadata requires capabilities")

        summary = _safe_json_object(self.summary, "summary")
        if set(summary) != _SUMMARY_FIELDS:
            raise ConformanceError("invalid_summary", "summary fields are invalid")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in summary.values()):
            raise ConformanceError("invalid_summary", "summary counts must be non-negative integers")
        expected_summary = {
            "total": len(self.cases),
            "passed": sum(case.result == "passed" for case in self.cases),
            "failed": sum(case.result == "failed" for case in self.cases),
            "not_applicable": sum(case.result == "not_applicable" for case in self.cases),
        }
        if summary != expected_summary:
            raise ConformanceError("summary_mismatch", "summary counts do not match cases")
        object.__setattr__(self, "summary", summary)
        if self.result == "passed":
            if self.failure_reason is not None or summary["failed"] or summary["not_applicable"]:
                raise ConformanceError("result_mismatch", "passed report must have all cases passed")
        elif not isinstance(self.failure_reason, str) or not self.failure_reason.strip():
            raise ConformanceError("result_mismatch", "failed report requires failure_reason")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExecutorConformanceReport":
        if not isinstance(raw, Mapping):
            raise ConformanceError("invalid_conformance_report", "report must be an object")
        _strict_keys(raw, _REPORT_FIELDS, "conformance report")
        if set(raw) != _REPORT_FIELDS:
            raise ConformanceError(
                "invalid_conformance_report",
                "report must contain exactly: " + ", ".join(sorted(_REPORT_FIELDS)),
            )
        cases = raw["cases"]
        if not isinstance(cases, list):
            raise ConformanceError("invalid_case_result", "report cases must be an array")
        return cls(
            schema_version=raw["schema_version"],
            profile=raw["profile"],
            executor=raw["executor"],
            capabilities=raw["capabilities"],
            capability_sha256=raw["capability_sha256"],
            result=raw["result"],
            cases=tuple(ConformanceCaseResult.from_dict(case) for case in cases),
            summary=raw["summary"],
            failure_reason=raw["failure_reason"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile": self.profile,
            "executor": dict(self.executor) if self.executor is not None else None,
            "capabilities": dict(self.capabilities) if self.capabilities is not None else None,
            "capability_sha256": self.capability_sha256,
            "result": self.result,
            "cases": [case.to_dict() for case in self.cases],
            "summary": dict(self.summary),
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True)
class ConformanceValidationResult:
    """Pure-data result from validating a serialized conformance report."""

    result: str
    valid: bool
    failure_reason: str | None = None
    message: str | None = None
    report: ExecutorConformanceReport | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "valid": self.valid,
            "failure_reason": self.failure_reason,
            "message": self.message,
            "report": self.report.to_dict() if self.report is not None else None,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


class _ExecutorProxy:
    """Track calls without changing the executor contract seen by the suite."""

    def __init__(self, delegate: InteractionExecutor) -> None:
        if not isinstance(delegate, InteractionExecutor):
            raise ConformanceError("invalid_executor", "factory result does not implement InteractionExecutor")
        self.delegate = delegate
        self.execute_count = 0
        self.reset_count = 0
        self.close_count = 0

    def capabilities(self) -> ExecutorCapabilities | Mapping[str, Any]:
        return self.delegate.capabilities()

    def reset(self, scene: Mapping[str, Any], initial_state: InteractionWorldState) -> None:
        self.reset_count += 1
        self.delegate.reset(scene, initial_state)

    def execute(self, command: ExecutionCommand) -> ExecutionStepResult:
        self.execute_count += 1
        return self.delegate.execute(command)

    def snapshot(self) -> Mapping[str, Any]:
        return self.delegate.snapshot()

    def close(self) -> None:
        self.close_count += 1
        self.delegate.close()


def _drawer_scene(*, position: float = 0.0) -> dict[str, Any]:
    return {
        "scene_id": "conformance-drawer-scene",
        "seed": 7,
        "recipe_name": "executor_conformance",
        "objects": [
            {
                "object_id": "drawer_1",
                "asset_id": "drawer_asset",
                "interactions": {
                    "joints": [
                        {
                            "joint_id": "drawer_slide",
                            "joint_type": "prismatic",
                            "position": position,
                            "lower_limit": 0.0,
                            "upper_limit": 0.42,
                        }
                    ],
                    "regions": [
                        {
                            "region_id": "drawer_handle",
                            "kind": "handle",
                            "link": "drawer",
                            "controlled_joint": "drawer_slide",
                            "allowed_actions": ["grasp", "pull", "push"],
                        }
                    ],
                    "semantic_states": [
                        {
                            "name": "closed",
                            "joint": "drawer_slide",
                            "range": [0.0, 0.02],
                            "target_position": 0.01,
                        },
                        {
                            "name": "open",
                            "joint": "drawer_slide",
                            "range": [0.35, 0.42],
                            "target_position": 0.4,
                        },
                    ],
                },
            }
        ],
    }


def _door_scene() -> dict[str, Any]:
    scene = _drawer_scene()
    scene["scene_id"] = "conformance-door-scene"
    scene["objects"][0]["interactions"] = {
        "joints": [
            {
                "joint_id": "door_hinge",
                "joint_type": "revolute",
                "position": 0.0,
                "lower_limit": 0.0,
                "upper_limit": 1.57,
            }
        ],
        "regions": [
            {
                "region_id": "door_handle",
                "kind": "handle",
                "link": "door",
                "controlled_joint": "door_hinge",
                "allowed_actions": ["grasp", "rotate"],
            }
        ],
        "semantic_states": [
            {
                "name": "open",
                "joint": "door_hinge",
                "range": [1.3, 1.57],
                "target_position": 1.4,
            }
        ],
    }
    return scene


def _plan(scene: Mapping[str, Any], state: str) -> InteractionPlan:
    planned = plan_interaction(scene, object_id="drawer_1", state=state)
    if not planned.valid or planned.plan is None:
        raise ConformanceError("fixture_invalid", planned.message or "reference plan generation failed")
    return planned.plan


def _case_pass(case_id: str, details: Mapping[str, Any] | None = None) -> ConformanceCaseResult:
    return ConformanceCaseResult(case_id, "passed", details=details or {})


def _case_failure(
    case_id: str,
    reason: str,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> ConformanceCaseResult:
    return ConformanceCaseResult(
        case_id,
        "failed",
        reason=reason,
        message=_safe_message(message),
        details=details or {},
    )


def _case_not_applicable(case_id: str, reason: str) -> ConformanceCaseResult:
    return ConformanceCaseResult(case_id, "not_applicable", reason=reason, message=reason, details={})


def _new_executor(executor_factory: Callable[[], InteractionExecutor]) -> _ExecutorProxy:
    if not callable(executor_factory):
        raise ConformanceError("executor_factory_failed", "executor_factory must be callable")
    try:
        return _ExecutorProxy(executor_factory())
    except ConformanceError:
        raise
    except Exception as exc:
        raise ConformanceError("executor_factory_failed", _safe_message(exc)) from exc


def _capabilities_for(proxy: _ExecutorProxy) -> ExecutorCapabilities:
    try:
        return normalize_executor_capabilities(proxy.capabilities())
    except ConformanceError:
        raise
    except Exception as exc:
        raise ConformanceError("invalid_capabilities", _safe_message(exc)) from exc


def _close_quietly(proxy: _ExecutorProxy) -> None:
    try:
        proxy.close()
    except Exception:
        pass


def _case_capabilities(executor_factory: Callable[[], InteractionExecutor]) -> ConformanceCaseResult:
    try:
        proxy = _new_executor(executor_factory)
    except ConformanceError as exc:
        return _case_failure("capabilities.valid", exc.reason, str(exc))
    try:
        capabilities = _capabilities_for(proxy)
        return _case_pass(
            "capabilities.valid",
            {"capabilities": capabilities.to_dict(), "capability_sha256": capability_sha256(capabilities)},
        )
    except ConformanceError as exc:
        return _case_failure("capabilities.valid", exc.reason, str(exc))
    finally:
        _close_quietly(proxy)


def _case_reset(executor_factory: Callable[[], InteractionExecutor]) -> ConformanceCaseResult:
    case_id = "lifecycle.reset"
    scene = _drawer_scene()
    plan = _plan(scene, "open")
    try:
        proxy = _new_executor(executor_factory)
        _capabilities_for(proxy)
        initial = InteractionWorldState.from_dict(plan.initial_state)
        proxy.reset(scene, initial)
        snapshot = _safe_json_object(proxy.snapshot(), "snapshot")
        if snapshot != initial.to_dict():
            return _case_failure(case_id, "invalid_snapshot", "reset snapshot differs from initial state")
        return _case_pass(case_id, {"reset_count": proxy.reset_count})
    except ConformanceError as exc:
        return _case_failure(case_id, exc.reason, str(exc))
    except Exception as exc:
        return _case_failure(case_id, "reset_failed", str(exc))
    finally:
        if "proxy" in locals():
            _close_quietly(proxy)


def _case_close(executor_factory: Callable[[], InteractionExecutor]) -> ConformanceCaseResult:
    case_id = "lifecycle.close"
    scene = _drawer_scene()
    plan = _plan(scene, "open")
    try:
        proxy = _new_executor(executor_factory)
        _capabilities_for(proxy)
        proxy.reset(scene, InteractionWorldState.from_dict(plan.initial_state))
        proxy.close()
        proxy.close()
        return _case_pass(case_id, {"close_count": proxy.close_count, "idempotent": True})
    except ConformanceError as exc:
        return _case_failure(case_id, exc.reason, str(exc))
    except Exception as exc:
        return _case_failure(case_id, "close_failed", str(exc))
    finally:
        if "proxy" in locals():
            _close_quietly(proxy)


def _case_pre_reset(executor_factory: Callable[[], InteractionExecutor]) -> ConformanceCaseResult:
    case_id = "lifecycle.pre_reset"
    scene = _drawer_scene()
    plan = _plan(scene, "open")
    try:
        proxy = _new_executor(executor_factory)
        _capabilities_for(proxy)
        command = ExecutionCommand.from_action(plan.plan_sha256, plan.steps[0])
        try:
            result = proxy.execute(command)
        except Exception:
            result = None
        if result is not None:
            if isinstance(result, Mapping):
                result = ExecutionStepResult.from_dict(result)
            if isinstance(result, ExecutionStepResult) and result.status != "succeeded":
                return _case_pass(case_id, {"rejected": True})
            return _case_failure(case_id, "execute_before_reset_succeeded", "execute succeeded before reset")
        try:
            proxy.snapshot()
        except Exception:
            return _case_pass(case_id, {"rejected": True})
        return _case_failure(case_id, "snapshot_before_reset_succeeded", "snapshot succeeded before reset")
    except ConformanceError as exc:
        return _case_failure(case_id, exc.reason, str(exc))
    except Exception as exc:
        return _case_failure(case_id, "execute_failed", str(exc))
    finally:
        if "proxy" in locals():
            _close_quietly(proxy)


def _execution_case(
    executor_factory: Callable[[], InteractionExecutor],
    case_id: str,
    scene: Mapping[str, Any],
    plan: InteractionPlan,
) -> ConformanceCaseResult:
    try:
        proxy = _new_executor(executor_factory)
        capabilities = _capabilities_for(proxy)
        required = _REQUIRED_ACTIONS.get(case_id, frozenset())
        missing = sorted(required - capabilities.supported_actions)
        if missing:
            return _case_failure(
                case_id,
                "missing_required_capability",
                f"executor does not declare required actions: {', '.join(missing)}",
                {"missing_actions": missing},
            )
        result = execute_interaction_plan(scene, plan, proxy)
        if not result.valid or result.trace is None:
            reason = result.failure_reason or "execute_failed"
            if reason == "unsupported_executor_action":
                reason = "capability_execution_mismatch"
            elif reason == "executor_result_mismatch":
                reason = "correlation_mismatch"
            elif reason == "invalid_executor_evidence":
                reason = "invalid_evidence"
            elif reason == "executor_exception" and result.message == "executor close failed":
                reason = "close_failed"
            return _case_failure(case_id, reason, result.message or reason, {"execution": result.to_dict()})
        return _case_pass(
            case_id,
            {
                "step_count": len(result.trace.steps),
                "task_success": result.task_status.get("task_success") is True,
                "execute_count": proxy.execute_count,
            },
        )
    except ConformanceError as exc:
        return _case_failure(case_id, exc.reason, str(exc))
    except Exception as exc:
        return _case_failure(case_id, "execute_failed", str(exc))


def _case_zero_step(executor_factory: Callable[[], InteractionExecutor]) -> ConformanceCaseResult:
    scene = _drawer_scene(position=0.4)
    plan = _plan(scene, "open")
    result = _execution_case(executor_factory, "execution.zero_step", scene, plan)
    if result.result == "passed" and result.details.get("execute_count") != 0:
        return _case_failure("execution.zero_step", "unexpected_command_dispatch", "zero-step plan dispatched a command")
    return result


def _case_correlation(executor_factory: Callable[[], InteractionExecutor]) -> ConformanceCaseResult:
    scene = _drawer_scene()
    plan = _plan(scene, "open")
    return _execution_case(executor_factory, "execution.command_correlation", scene, plan)


def _case_evidence(executor_factory: Callable[[], InteractionExecutor]) -> ConformanceCaseResult:
    scene = _drawer_scene()
    plan = _plan(scene, "open")
    return _execution_case(executor_factory, "execution.evidence", scene, plan)


def _case_final_goal(executor_factory: Callable[[], InteractionExecutor]) -> ConformanceCaseResult:
    scene = _drawer_scene()
    plan = _plan(scene, "open")
    result = _execution_case(executor_factory, "execution.final_goal", scene, plan)
    return result


def _case_trace(executor_factory: Callable[[], InteractionExecutor]) -> ConformanceCaseResult:
    case_id = "trace.valid"
    scene = _drawer_scene()
    plan = _plan(scene, "open")
    try:
        proxy = _new_executor(executor_factory)
        _capabilities_for(proxy)
        execution = execute_interaction_plan(scene, plan, proxy)
        if not execution.valid or execution.trace is None:
            return _case_failure(case_id, "invalid_trace", "executor did not produce a passing trace")
        validated = validate_execution_trace(scene, plan, execution.trace)
        if not validated.valid:
            return _case_failure(case_id, "invalid_trace", validated.message or "trace validation failed")
        return _case_pass(case_id, {"trace_sha256": execution.trace.trace_sha256})
    except ConformanceError as exc:
        return _case_failure(case_id, exc.reason, str(exc))
    except Exception as exc:
        return _case_failure(case_id, "invalid_trace", str(exc))


def _run_case(
    executor_factory: Callable[[], InteractionExecutor],
    case_id: str,
) -> ConformanceCaseResult:
    handlers: dict[str, Callable[[], ConformanceCaseResult]] = {
        "capabilities.valid": lambda: _case_capabilities(executor_factory),
        "lifecycle.reset": lambda: _case_reset(executor_factory),
        "lifecycle.close": lambda: _case_close(executor_factory),
        "lifecycle.pre_reset": lambda: _case_pre_reset(executor_factory),
        "execution.zero_step": lambda: _case_zero_step(executor_factory),
        "execution.drawer_open": lambda: _execution_case(
            executor_factory, "execution.drawer_open", _drawer_scene(), _plan(_drawer_scene(), "open")
        ),
        "execution.drawer_close": lambda: _execution_case(
            executor_factory,
            "execution.drawer_close",
            _drawer_scene(position=0.4),
            _plan(_drawer_scene(position=0.4), "closed"),
        ),
        "execution.door_rotate": lambda: _execution_case(
            executor_factory, "execution.door_rotate", _door_scene(), _plan(_door_scene(), "open")
        ),
        "execution.command_correlation": lambda: _case_correlation(executor_factory),
        "execution.evidence": lambda: _case_evidence(executor_factory),
        "execution.final_goal": lambda: _case_final_goal(executor_factory),
        "trace.valid": lambda: _case_trace(executor_factory),
    }
    try:
        return handlers[case_id]()
    except ConformanceError as exc:
        return _case_failure(case_id, exc.reason, str(exc))
    except Exception as exc:
        return _case_failure(case_id, "case_error", str(exc))


def _report(
    *,
    capabilities: ExecutorCapabilities | None,
    cases: tuple[ConformanceCaseResult, ...],
    failure_reason: str | None,
) -> ExecutorConformanceReport:
    summary = {
        "total": len(cases),
        "passed": sum(case.result == "passed" for case in cases),
        "failed": sum(case.result == "failed" for case in cases),
        "not_applicable": sum(case.result == "not_applicable" for case in cases),
    }
    result = "passed" if summary["failed"] == 0 and summary["not_applicable"] == 0 else "failed"
    if result == "passed":
        failure_reason = None
    elif failure_reason is None:
        failure_reason = "conformance_failed"
    executor = (
        {
            "name": capabilities.executor,
            "version": capabilities.version,
            "physical": capabilities.physical,
        }
        if capabilities is not None
        else None
    )
    declarations = capabilities.to_dict() if capabilities is not None else None
    return ExecutorConformanceReport(
        schema_version=CONFORMANCE_SCHEMA_VERSION,
        profile=CORE_PROFILE,
        executor=executor,
        capabilities=declarations,
        capability_sha256=capability_sha256(capabilities) if capabilities is not None else None,
        result=result,
        cases=cases,
        summary=summary,
        failure_reason=failure_reason,
    )


def run_executor_conformance(
    executor_factory: Callable[[], InteractionExecutor],
    *,
    profile: str = CORE_PROFILE,
) -> ExecutorConformanceReport:
    """Run the isolated core conformance suite for an executor factory."""

    if profile != CORE_PROFILE:
        raise ConformanceError("unsupported_profile", f"unsupported conformance profile: {profile!r}")
    try:
        probe = _new_executor(executor_factory)
    except ConformanceError as exc:
        cases = (
            ConformanceCaseResult("capabilities.valid", "failed", exc.reason, _safe_message(exc), {}),
            *tuple(_case_not_applicable(case_id, exc.reason) for case_id in CORE_CONFORMANCE_CASES[1:]),
        )
        return _report(capabilities=None, cases=cases, failure_reason=exc.reason)
    try:
        capabilities = _capabilities_for(probe)
    except ConformanceError as exc:
        cases = (
            ConformanceCaseResult("capabilities.valid", "failed", exc.reason, _safe_message(exc), {}),
            *tuple(_case_not_applicable(case_id, exc.reason) for case_id in CORE_CONFORMANCE_CASES[1:]),
        )
        _close_quietly(probe)
        return _report(capabilities=None, cases=cases, failure_reason=exc.reason)
    _close_quietly(probe)
    cases = tuple(_run_case(executor_factory, case_id) for case_id in CORE_CONFORMANCE_CASES)
    first_failure = next((case.reason for case in cases if case.result == "failed"), None)
    return _report(capabilities=capabilities, cases=cases, failure_reason=first_failure)


def _validation_failure(reason: str, message: str) -> ConformanceValidationResult:
    return ConformanceValidationResult(
        result="failed",
        valid=False,
        failure_reason=reason,
        message=_safe_message(message),
    )


def validate_conformance_report(
    report: ExecutorConformanceReport | Mapping[str, Any] | str | Path,
) -> ConformanceValidationResult:
    """Validate a serialized report without constructing or executing a backend."""

    try:
        if isinstance(report, ExecutorConformanceReport):
            report_model = ExecutorConformanceReport.from_dict(report.to_dict())
        elif isinstance(report, (str, Path)):
            report_model = ExecutorConformanceReport.from_dict(_read_json(report, "conformance report"))
        else:
            report_model = ExecutorConformanceReport.from_dict(_coerce_mapping(report, "conformance report"))
        return ConformanceValidationResult(
            result="passed",
            valid=True,
            report=report_model,
        )
    except ConformanceError as exc:
        return _validation_failure(exc.reason, str(exc))
    except Exception as exc:
        return _validation_failure("invalid_conformance_report", str(exc))


def write_conformance_report_atomic(
    path: str | Path,
    report: ExecutorConformanceReport,
) -> None:
    """Write a stable conformance report with an atomic replace."""

    if not isinstance(report, ExecutorConformanceReport):
        raise ConformanceError("invalid_conformance_report", "report must be an ExecutorConformanceReport")
    destination = Path(path)
    encoded = json.dumps(
        report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
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


def create_executor(name: str) -> InteractionExecutor:
    """Create one registered executor for the offline CLI."""

    if name == "dry-run":
        return DryRunInteractionExecutor()
    raise ConformanceError("unknown_executor", f"unsupported executor {name!r}; supported: dry-run")


__all__ = [
    "CONFORMANCE_SCHEMA_VERSION",
    "CORE_CONFORMANCE_CASES",
    "CORE_PROFILE",
    "EXECUTOR_NAMES",
    "ConformanceCaseResult",
    "ConformanceError",
    "ConformanceValidationResult",
    "ExecutorConformanceReport",
    "capability_sha256",
    "create_executor",
    "normalize_executor_capabilities",
    "run_executor_conformance",
    "validate_conformance_report",
    "write_conformance_report_atomic",
]
