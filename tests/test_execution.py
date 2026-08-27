from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scene_factory.execution import (
    EXECUTION_COMMAND_SCHEMA_VERSION,
    EXECUTION_TRACE_SCHEMA_VERSION,
    DryRunInteractionExecutor,
    ExecutionCommand,
    ExecutionError,
    ExecutionStepResult,
    ExecutionTrace,
    ExecutorCapabilities,
    InteractionExecutor,
    execute_interaction_plan,
    validate_execution_trace,
    write_execution_trace_atomic,
)
from scene_factory.planning import (
    InteractionAction,
    InteractionPlan,
    InteractionWorldState,
    PLAN_SCHEMA_VERSION,
    plan_interaction,
    replay_interaction_plan,
)


class RecordingExecutor:
    def __init__(
        self,
        *,
        supported_actions: set[str] | None = None,
        fail_step: int | None = None,
        not_supported_step: int | None = None,
        raise_step: int | None = None,
        mismatch: str | None = None,
        invalid_evidence: bool = False,
        goal_false: bool = False,
        reset_failure: bool = False,
        close_failure: bool = False,
    ) -> None:
        self.inner = DryRunInteractionExecutor()
        self.supported_actions = supported_actions
        self.fail_step = fail_step
        self.not_supported_step = not_supported_step
        self.raise_step = raise_step
        self.mismatch = mismatch
        self.invalid_evidence = invalid_evidence
        self.goal_false = goal_false
        self.reset_failure = reset_failure
        self.close_failure = close_failure
        self.reset_count = 0
        self.execute_count = 0
        self.close_count = 0

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            executor="test_executor",
            version="1",
            physical=False,
            supported_actions=(
                self.supported_actions
                if self.supported_actions is not None
                else {"approach", "grasp", "pull", "push", "rotate", "release"}
            ),
            articulation_execution=True,
        )

    def reset(self, scene, initial_state) -> None:
        self.reset_count += 1
        if self.reset_failure:
            raise RuntimeError("injected reset failure")
        self.inner.reset(scene, initial_state)

    def execute(self, command: ExecutionCommand) -> ExecutionStepResult:
        self.execute_count += 1
        if self.raise_step == command.step_id:
            raise RuntimeError("injected step failure")
        if self.fail_step == command.step_id:
            return ExecutionStepResult(
                command.command_id,
                command.step_id,
                "failed",
                "injected_failure",
                {"step_id": command.step_id},
            )
        if self.not_supported_step == command.step_id:
            return ExecutionStepResult(
                command.command_id,
                command.step_id,
                "not_supported",
                "injected_not_supported",
                {},
            )
        if self.mismatch == "command_id":
            return ExecutionStepResult(
                "wrong-command-id",
                command.step_id,
                "succeeded",
                None,
                {},
            )
        if self.mismatch == "step_id":
            return ExecutionStepResult(
                command.command_id,
                command.step_id + 99,
                "succeeded",
                None,
                {},
            )
        if self.invalid_evidence:
            return {
                "command_id": command.command_id,
                "step_id": command.step_id,
                "status": "succeeded",
                "reason": None,
                "evidence": {"not_json": object()},
            }
        if self.goal_false:
            return ExecutionStepResult(command.command_id, command.step_id, "succeeded", None, {})
        return self.inner.execute(command)

    def snapshot(self):
        return self.inner.snapshot()

    def close(self) -> None:
        self.close_count += 1
        self.inner.close()
        if self.close_failure:
            raise RuntimeError("injected close failure")


class ExecutionContractTests(unittest.TestCase):
    @staticmethod
    def _drawer_scene(*, position: float = 0.0) -> dict:
        return {
            "scene_id": "drawer-execution-scene",
            "seed": 7,
            "recipe_name": "articulated_test",
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

    @classmethod
    def _door_scene(cls) -> dict:
        scene = cls._drawer_scene()
        scene["scene_id"] = "door-execution-scene"
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

    def _plan(self, scene: dict, state: str = "open") -> InteractionPlan:
        planned = plan_interaction(scene, object_id="drawer_1", state=state)
        self.assertTrue(planned.valid, planned.to_dict())
        self.assertIsNotNone(planned.plan)
        return planned.plan

    def test_protocol_and_capabilities_are_stable(self) -> None:
        executor = DryRunInteractionExecutor()
        self.assertIsInstance(executor, InteractionExecutor)
        capabilities = executor.capabilities()
        self.assertEqual(capabilities.to_dict()["executor"], "dry_run")
        self.assertFalse(capabilities.to_dict()["physical"])
        self.assertEqual(
            capabilities.to_dict()["supported_actions"],
            ["approach", "grasp", "pull", "push", "release", "rotate"],
        )

    def test_drawer_open_executes_shared_transition_and_validates_trace(self) -> None:
        scene = self._drawer_scene()
        plan = self._plan(scene)
        result = execute_interaction_plan(scene, plan, DryRunInteractionExecutor())
        self.assertTrue(result.valid, result.to_dict())
        self.assertFalse(result.physical_execution)
        self.assertEqual(result.task_status["task_success"], True)
        self.assertIsNotNone(result.trace)
        self.assertEqual(result.trace.schema_version, EXECUTION_TRACE_SCHEMA_VERSION)
        self.assertEqual(len(result.trace.steps), 4)
        self.assertEqual(result.trace.steps[2].command.action.action, "pull")
        self.assertEqual(result.trace.steps[2].result.evidence["symbolic_effect"], True)
        replayed = replay_interaction_plan(scene, plan)
        self.assertTrue(replayed.valid, replayed.to_dict())
        self.assertEqual(
            result.trace.final_evidence["joint_positions"],
            replayed.final_state["joint_positions"],
        )
        validated = validate_execution_trace(scene, plan, result.trace)
        self.assertTrue(validated.valid, validated.to_dict())
        self.assertEqual(validated.trace_result, "passed")
        self.assertEqual(validated.trace.trace_sha256, result.trace.trace_sha256)

    def test_drawer_close_door_and_zero_step(self) -> None:
        close_scene = self._drawer_scene(position=0.4)
        close_plan = self._plan(close_scene, "closed")
        close_result = execute_interaction_plan(close_scene, close_plan, DryRunInteractionExecutor())
        self.assertTrue(close_result.valid, close_result.to_dict())
        self.assertEqual([step.command.action.action for step in close_result.trace.steps], [
            "approach", "grasp", "push", "release"
        ])

        door_scene = self._door_scene()
        door_plan_result = plan_interaction(door_scene, object_id="drawer_1", state="open")
        self.assertTrue(door_plan_result.valid, door_plan_result.to_dict())
        door_result = execute_interaction_plan(door_scene, door_plan_result.plan, DryRunInteractionExecutor())
        self.assertTrue(door_result.valid, door_result.to_dict())
        self.assertEqual(door_result.trace.steps[2].command.action.action, "rotate")

        satisfied_scene = self._drawer_scene(position=0.4)
        satisfied_plan = self._plan(satisfied_scene)
        self.assertEqual(satisfied_plan.steps, ())
        satisfied_result = execute_interaction_plan(
            satisfied_scene, satisfied_plan, DryRunInteractionExecutor()
        )
        self.assertTrue(satisfied_result.valid, satisfied_result.to_dict())
        self.assertEqual(satisfied_result.trace.steps, ())

    def test_invalid_plan_is_rejected_before_reset_or_execute(self) -> None:
        scene = self._drawer_scene()
        generated = self._plan(scene)
        invalid = InteractionPlan(
            PLAN_SCHEMA_VERSION,
            generated.goal,
            generated.initial_state,
            (
                InteractionAction(0, "grasp", "drawer_1", "drawer_handle"),
                InteractionAction(1, "pull", "drawer_1", "drawer_handle", "drawer_slide", 0.4),
            ),
            generated.expected_final_state,
        )
        executor = RecordingExecutor()
        result = execute_interaction_plan(scene, invalid, executor)
        self.assertFalse(result.valid)
        self.assertEqual(result.failure_reason, "invalid_plan")
        self.assertEqual(executor.reset_count, 0)
        self.assertEqual(executor.execute_count, 0)
        self.assertEqual(executor.close_count, 0)

    def test_capability_gate_runs_before_reset(self) -> None:
        scene = self._drawer_scene()
        plan = self._plan(scene)
        executor = RecordingExecutor(supported_actions={"approach", "grasp"})
        result = execute_interaction_plan(scene, plan, executor)
        self.assertFalse(result.valid)
        self.assertEqual(result.failure_reason, "unsupported_executor_action")
        self.assertEqual(executor.reset_count, 0)
        self.assertEqual(executor.execute_count, 0)
        self.assertEqual(executor.close_count, 0)
        self.assertEqual(result.trace.steps, ())

    def test_step_failure_is_fail_fast_and_trace_is_valid(self) -> None:
        scene = self._drawer_scene()
        plan = self._plan(scene)
        executor = RecordingExecutor(fail_step=2)
        result = execute_interaction_plan(scene, plan, executor)
        self.assertFalse(result.valid)
        self.assertEqual(result.failure_reason, "executor_step_failed")
        self.assertEqual(executor.execute_count, 3)
        self.assertEqual(executor.close_count, 1)
        self.assertEqual(len(result.trace.steps), 3)
        self.assertEqual(result.trace.steps[-1].result.status, "failed")
        self.assertTrue(validate_execution_trace(scene, plan, result.trace).valid)

    def test_not_supported_step_is_a_terminal_failure(self) -> None:
        scene = self._drawer_scene()
        plan = self._plan(scene)
        result = execute_interaction_plan(scene, plan, RecordingExecutor(not_supported_step=2))
        self.assertFalse(result.valid)
        self.assertEqual(result.failure_reason, "unsupported_executor_action")
        self.assertEqual(len(result.trace.steps), 3)
        self.assertEqual(result.trace.steps[-1].result.status, "not_supported")
        self.assertTrue(validate_execution_trace(scene, plan, result.trace).valid)

    def test_reset_and_executor_exception_are_structured_and_closed(self) -> None:
        scene = self._drawer_scene()
        plan = self._plan(scene)
        reset_executor = RecordingExecutor(reset_failure=True)
        reset_result = execute_interaction_plan(scene, plan, reset_executor)
        self.assertFalse(reset_result.valid)
        self.assertEqual(reset_result.failure_reason, "executor_reset_failed")
        self.assertEqual(reset_executor.close_count, 1)
        self.assertTrue(validate_execution_trace(scene, plan, reset_result.trace).valid)

        exception_executor = RecordingExecutor(raise_step=1)
        exception_result = execute_interaction_plan(scene, plan, exception_executor)
        self.assertFalse(exception_result.valid)
        self.assertEqual(exception_result.failure_reason, "executor_exception")
        self.assertEqual(exception_executor.execute_count, 2)
        self.assertEqual(exception_executor.close_count, 1)
        evidence = exception_result.trace.steps[-1].result.evidence
        self.assertEqual(evidence["exception_type"], "RuntimeError")
        self.assertTrue(validate_execution_trace(scene, plan, exception_result.trace).valid)

    def test_result_correlation_and_evidence_are_fail_closed(self) -> None:
        scene = self._drawer_scene()
        plan = self._plan(scene)
        for mismatch in ("command_id", "step_id"):
            with self.subTest(mismatch=mismatch):
                result = execute_interaction_plan(
                    scene, plan, RecordingExecutor(mismatch=mismatch)
                )
                self.assertFalse(result.valid)
                self.assertEqual(result.failure_reason, "executor_result_mismatch")
                self.assertTrue(validate_execution_trace(scene, plan, result.trace).valid)

        invalid = execute_interaction_plan(
            scene, plan, RecordingExecutor(invalid_evidence=True)
        )
        self.assertFalse(invalid.valid)
        self.assertEqual(invalid.failure_reason, "invalid_executor_evidence")
        self.assertTrue(validate_execution_trace(scene, plan, invalid.trace).valid)
        with self.assertRaises(ExecutionError):
            ExecutionStepResult("command", 0, "succeeded", None, {"bad": object()})

    def test_all_step_success_without_goal_success_fails(self) -> None:
        scene = self._drawer_scene()
        plan = self._plan(scene)
        executor = RecordingExecutor(goal_false=True)
        result = execute_interaction_plan(scene, plan, executor)
        self.assertFalse(result.valid)
        self.assertEqual(result.failure_reason, "goal_not_satisfied")
        self.assertEqual(result.task_status["task_success"], False)
        self.assertTrue(all(step.result.status == "succeeded" for step in result.trace.steps))
        validated = validate_execution_trace(scene, plan, result.trace)
        self.assertTrue(validated.valid, validated.to_dict())
        self.assertEqual(validated.trace_result, "failed")

    def test_close_failure_cannot_leave_passed_result(self) -> None:
        scene = self._drawer_scene()
        plan = self._plan(scene)
        result = execute_interaction_plan(scene, plan, RecordingExecutor(close_failure=True))
        self.assertFalse(result.valid)
        self.assertEqual(result.failure_reason, "executor_exception")
        self.assertEqual(result.trace.result, "failed")
        self.assertEqual(result.trace.failure_reason, "executor_exception")

    def test_dry_run_close_clears_runtime_state(self) -> None:
        scene = self._drawer_scene()
        plan = self._plan(scene)
        executor = DryRunInteractionExecutor()
        executor.reset(scene, InteractionWorldState.from_dict(plan.initial_state))
        executor.close()
        with self.assertRaises(RuntimeError):
            executor.snapshot()
        executor.close()

    def test_trace_round_trip_mutation_and_plan_correlation(self) -> None:
        scene = self._drawer_scene()
        plan = self._plan(scene)
        result = execute_interaction_plan(scene, plan, DryRunInteractionExecutor())
        trace = result.trace
        self.assertIsInstance(trace, ExecutionTrace)
        reloaded = ExecutionTrace.from_dict(json.loads(json.dumps(trace.to_dict())))
        self.assertEqual(reloaded.to_dict(), trace.to_dict())
        self.assertEqual(reloaded.trace_sha256, trace.trace_sha256)

        for mutation in ("trace_sha256", "plan_sha256", "step_id", "task_status"):
            with self.subTest(mutation=mutation):
                payload = copy.deepcopy(trace.to_dict())
                if mutation == "trace_sha256":
                    payload["trace_sha256"] = "0" * 64
                elif mutation == "plan_sha256":
                    payload["plan_sha256"] = "0" * 64
                elif mutation == "step_id":
                    payload["steps"][1]["command"]["step_id"] = 99
                else:
                    payload["goal_status"]["task_success"] = False
                invalid = validate_execution_trace(scene, plan, payload)
                self.assertFalse(invalid.valid, invalid.to_dict())

        other_plan = self._plan(self._drawer_scene(position=0.4), "closed")
        plan_mismatch = validate_execution_trace(scene, other_plan, trace)
        self.assertFalse(plan_mismatch.valid)
        self.assertEqual(plan_mismatch.failure_reason, "plan_sha_mismatch")

    def test_trace_hash_is_deterministic_for_100_runs(self) -> None:
        scene = self._drawer_scene()
        plan = self._plan(scene)
        hashes = {
            execute_interaction_plan(scene, plan, DryRunInteractionExecutor()).trace.trace_sha256
            for _ in range(100)
        }
        self.assertEqual(len(hashes), 1)

    def test_atomic_trace_write_and_cli_execute_validate(self) -> None:
        scene = self._drawer_scene()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scene_path = root / "layout.json"
            plan_path = root / "plan.json"
            trace_path = root / "trace.json"
            scene_path.write_text(json.dumps(scene), encoding="utf-8")
            planned = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scene_factory",
                    "task",
                    "plan",
                    "--scene",
                    str(scene_path),
                    "--object",
                    "drawer_1",
                    "--state",
                    "open",
                    "--output",
                    str(plan_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)
            executed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scene_factory",
                    "task",
                    "execute",
                    "--scene",
                    str(scene_path),
                    "--plan",
                    str(plan_path),
                    "--executor",
                    "dry-run",
                    "--output",
                    str(trace_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(executed.returncode, 0, executed.stderr)
            self.assertTrue(trace_path.is_file())
            self.assertTrue(json.loads(executed.stdout)["physical_execution"] is False)
            validated = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scene_factory",
                    "task",
                    "execution-validate",
                    "--scene",
                    str(scene_path),
                    "--plan",
                    str(plan_path),
                    "--trace",
                    str(trace_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertTrue(json.loads(validated.stdout)["valid"])

            trace = ExecutionTrace.from_dict(json.loads(trace_path.read_text("utf-8")))
            another_path = root / "nested" / "trace.json"
            write_execution_trace_atomic(another_path, trace)
            self.assertEqual(json.loads(another_path.read_text("utf-8")), trace.to_dict())

    def test_schema_and_no_isaac_no_numpy_import_smoke(self) -> None:
        schema = json.loads(
            Path("schemas/execution_trace.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(schema["$id"], EXECUTION_TRACE_SCHEMA_VERSION)
        self.assertEqual(ExecutionCommand.from_action("0" * 64, self._plan(self._drawer_scene()).steps[0]).schema_version,
                         EXECUTION_COMMAND_SCHEMA_VERSION)
        smoke = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import scene_factory.execution; "
                    "assert not any(name in sys.modules for name in "
                    "('isaacsim', 'omni', 'pxr', 'carb', 'numpy'))"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(smoke.returncode, 0, smoke.stderr)


if __name__ == "__main__":
    unittest.main()
