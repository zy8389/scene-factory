from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scene_factory.planning import (
    InteractionAction,
    InteractionPlan,
    PLAN_SCHEMA_VERSION,
    plan_interaction,
    replay_interaction_plan,
    synthesize_articulation_task,
    validate_interaction_plan,
)


class SymbolicPlanningTests(unittest.TestCase):
    def _drawer_scene(self, *, position: float = 0.0, regions: list[dict] | None = None) -> dict:
        return {
            "scene_id": "drawer-scene",
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
                        "regions": regions
                        if regions is not None
                        else [
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

    def _door_scene(self, *, position: float = 0.0) -> dict:
        scene = self._drawer_scene(position=position)
        scene["objects"][0]["interactions"] = {
            "joints": [
                {
                    "joint_id": "door_hinge",
                    "joint_type": "revolute",
                    "position": position,
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
                    "name": "closed",
                    "joint": "door_hinge",
                    "range": [0.0, 0.02],
                    "target_position": 0.01,
                },
                {
                    "name": "open",
                    "joint": "door_hinge",
                    "range": [1.3, 1.57],
                    "target_position": 1.4,
                },
            ],
        }
        return scene

    def test_drawer_open_plan_validate_replay_and_task_evaluator(self) -> None:
        scene = self._drawer_scene()
        result = plan_interaction(scene, object_id="drawer_1", state="open")
        self.assertTrue(result.valid, result.to_dict())
        self.assertEqual(
            [step.action for step in result.plan.steps],
            ["approach", "grasp", "pull", "release"],
        )
        validated = validate_interaction_plan(scene, result.plan.to_dict())
        replayed = replay_interaction_plan(scene, result.plan)
        self.assertTrue(validated.valid, validated.to_dict())
        self.assertTrue(replayed.valid, replayed.to_dict())
        self.assertTrue(replayed.task_oracle["task_success"])
        self.assertEqual(replayed.final_state["joint_positions"]["drawer_1"]["drawer_slide"], 0.4)
        self.assertEqual(result.plan.plan_sha256, validated.plan.plan_sha256)

    def test_drawer_close_uses_push(self) -> None:
        result = plan_interaction(self._drawer_scene(position=0.4), object_id="drawer_1", state="closed")
        self.assertTrue(result.valid, result.to_dict())
        self.assertEqual(result.plan.steps[2].action, "push")
        self.assertTrue(replay_interaction_plan(self._drawer_scene(position=0.4), result.plan).valid)

    def test_non_grasp_push_does_not_invent_grasp_or_release(self) -> None:
        scene = self._drawer_scene(
            position=0.4,
            regions=[
                {
                    "region_id": "drawer_front",
                    "kind": "push",
                    "link": "drawer",
                    "controlled_joint": "drawer_slide",
                    "allowed_actions": ["push"],
                }
            ]
        )
        result = plan_interaction(scene, object_id="drawer_1", state="closed")
        self.assertTrue(result.valid, result.to_dict())
        self.assertEqual([step.action for step in result.plan.steps], ["approach", "push"])
        self.assertTrue(validate_interaction_plan(scene, result.plan).valid)

    def test_revolute_door_uses_rotate(self) -> None:
        result = plan_interaction(self._door_scene(), object_id="drawer_1", state="open")
        self.assertTrue(result.valid, result.to_dict())
        self.assertEqual(result.plan.steps[2].action, "rotate")
        self.assertTrue(validate_interaction_plan(self._door_scene(), result.plan).valid)

    def test_already_satisfied_returns_zero_step_plan(self) -> None:
        scene = self._drawer_scene(position=0.4)
        result = plan_interaction(scene, object_id="drawer_1", state="open")
        self.assertTrue(result.valid, result.to_dict())
        self.assertTrue(result.goal_already_satisfied)
        self.assertEqual(result.plan.steps, ())
        self.assertTrue(replay_interaction_plan(scene, result.plan).valid)

    def test_task_synthesis_resolves_semantic_state(self) -> None:
        task = synthesize_articulation_task(self._drawer_scene(), object_id="drawer_1", state="open")
        self.assertEqual(
            task,
            {
                "success": {
                    "predicate": "articulation_state",
                    "object_id": "drawer_1",
                    "state": "open",
                    "joint": "drawer_slide",
                    "range": [0.35, 0.42],
                }
            },
        )

    def test_missing_region_and_wrong_allowed_action_fail_closed(self) -> None:
        scene = self._drawer_scene(regions=[])
        result = plan_interaction(scene, object_id="drawer_1", state="open")
        self.assertFalse(result.valid)
        self.assertEqual(result.failure_reason, "no_interaction_region")
        scene = self._drawer_scene(
            regions=[
                {
                    "region_id": "drawer_push_only",
                    "kind": "push",
                    "link": "drawer",
                    "controlled_joint": "drawer_slide",
                    "allowed_actions": ["push"],
                }
            ]
        )
        result = plan_interaction(scene, object_id="drawer_1", state="open")
        self.assertFalse(result.valid)
        self.assertEqual(result.failure_reason, "no_compatible_action")

    def test_validator_rejects_invalid_preconditions_and_targets(self) -> None:
        scene = self._drawer_scene()
        initial = plan_interaction(scene, object_id="drawer_1", state="open").plan.initial_state
        expected = plan_interaction(scene, object_id="drawer_1", state="open").plan.expected_final_state
        bad_steps = (
            InteractionAction(0, "grasp", "drawer_1", "drawer_handle"),
            InteractionAction(1, "pull", "drawer_1", "drawer_handle", "drawer_slide", 0.4),
        )
        bad_plan = InteractionPlan(PLAN_SCHEMA_VERSION, {"predicate": "articulation_state", "object_id": "drawer_1", "state": "open"}, initial, bad_steps, expected)
        result = validate_interaction_plan(scene, bad_plan)
        self.assertFalse(result.valid)
        self.assertEqual(result.failure_reason, "grasp_before_approach")
        malformed = bad_plan.to_dict()
        malformed["steps"][0]["extra"] = True
        result = validate_interaction_plan(scene, malformed)
        self.assertFalse(result.valid)
        self.assertEqual(result.failure_reason, "malformed_plan")

        out_of_range = copy.deepcopy(
            plan_interaction(scene, object_id="drawer_1", state="open").plan.to_dict()
        )
        out_of_range["steps"][2]["target_position"] = 0.2
        out_of_range_plan = InteractionPlan(
            PLAN_SCHEMA_VERSION,
            out_of_range["goal"],
            out_of_range["initial_state"],
            tuple(InteractionAction.from_dict(step) for step in out_of_range["steps"]),
            out_of_range["expected_final_state"],
        )
        result = validate_interaction_plan(scene, out_of_range_plan)
        self.assertFalse(result.valid)
        self.assertEqual(result.failure_reason, "target_outside_semantic_range")

    def test_validator_rejects_holding_conflicts_and_non_contiguous_ids(self) -> None:
        scene = self._drawer_scene()
        generated = plan_interaction(scene, object_id="drawer_1", state="open").plan
        steps = (
            InteractionAction(0, "approach", "drawer_1", "drawer_handle"),
            InteractionAction(1, "grasp", "drawer_1", "drawer_handle"),
            InteractionAction(2, "grasp", "drawer_1", "drawer_handle"),
        )
        plan = InteractionPlan(
            PLAN_SCHEMA_VERSION,
            generated.goal,
            generated.initial_state,
            steps,
            generated.expected_final_state,
        )
        result = validate_interaction_plan(scene, plan)
        self.assertFalse(result.valid)
        self.assertEqual(result.failure_reason, "holding_conflict")
        payload = generated.to_dict()
        payload["steps"][1]["step_id"] = 4
        result = validate_interaction_plan(scene, payload)
        self.assertFalse(result.valid)
        self.assertEqual(result.failure_reason, "non_contiguous_step_ids")

    def test_unknown_state_ambiguous_state_and_unsupported_goal(self) -> None:
        scene = self._drawer_scene()
        result = plan_interaction(scene, object_id="drawer_1", state="locked")
        self.assertFalse(result.valid)
        self.assertEqual(result.failure_reason, "unknown_state")
        scene["objects"][0]["interactions"]["semantic_states"].append(
            {"name": "open", "joint": "other_slide", "range": [0.1, 0.2], "target_position": 0.15}
        )
        scene["objects"][0]["interactions"]["joints"].append(
            {
                "joint_id": "other_slide",
                "joint_type": "prismatic",
                "position": 0.0,
                "lower_limit": 0.0,
                "upper_limit": 0.42,
            }
        )
        result = plan_interaction(scene, object_id="drawer_1", state="open")
        self.assertFalse(result.valid)
        self.assertEqual(result.failure_reason, "ambiguous_state")
        result = plan_interaction(scene, goal={"predicate": "lifted", "object_id": "drawer_1", "state": "open"})
        self.assertFalse(result.valid)
        self.assertEqual(result.failure_reason, "goal_not_supported")

    def test_metadata_order_does_not_change_plan_hash(self) -> None:
        scene = self._drawer_scene()
        first = plan_interaction(scene, object_id="drawer_1", state="open")
        reordered = copy.deepcopy(scene)
        interaction = reordered["objects"][0]["interactions"]
        interaction["joints"] = list(reversed(interaction["joints"]))
        interaction["regions"] = list(reversed(interaction["regions"]))
        interaction["semantic_states"] = list(reversed(interaction["semantic_states"]))
        second = plan_interaction(reordered, object_id="drawer_1", state="open")
        self.assertTrue(first.valid and second.valid)
        self.assertEqual(first.plan.plan_sha256, second.plan.plan_sha256)
        self.assertEqual(first.plan.to_dict(), second.plan.to_dict())

    def test_plan_round_trip_and_schema_rejection(self) -> None:
        scene = self._drawer_scene()
        plan = plan_interaction(scene, object_id="drawer_1", state="open").plan
        reloaded = InteractionPlan.from_dict(json.loads(json.dumps(plan.to_dict())))
        self.assertEqual(reloaded.to_dict(), plan.to_dict())
        payload = plan.to_dict()
        payload["schema_version"] = "scene_factory.interaction_plan.v0"
        result = validate_interaction_plan(scene, payload)
        self.assertFalse(result.valid)
        self.assertEqual(result.failure_reason, "unsupported_schema_version")

        missing_hash = plan.to_dict()
        missing_hash["plan_sha256"] = None
        result = validate_interaction_plan(scene, missing_hash)
        self.assertFalse(result.valid)
        self.assertEqual(result.failure_reason, "malformed_plan")

    def test_omitted_target_uses_midpoint_and_repeated_planning_is_deterministic(self) -> None:
        scene = self._drawer_scene()
        scene["objects"][0]["interactions"]["semantic_states"][1].pop("target_position")
        first = plan_interaction(scene, object_id="drawer_1", state="open")
        self.assertTrue(first.valid, first.to_dict())
        self.assertEqual(first.plan.steps[2].target_position, 0.385)
        hashes = {
            plan_interaction(scene, object_id="drawer_1", state="open").plan.plan_sha256
            for _ in range(100)
        }
        self.assertEqual(len(hashes), 1)
        for seed in range(10):
            seeded = copy.deepcopy(scene)
            seeded["seed"] = seed
            planned = plan_interaction(seeded, object_id="drawer_1", state="open")
            self.assertTrue(planned.valid, planned.to_dict())
            self.assertEqual(planned.plan.plan_sha256, first.plan.plan_sha256)

    def test_cli_plan_validate_replay_and_no_simulator_import(self) -> None:
        scene = self._drawer_scene()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scene_path = root / "layout.json"
            plan_path = root / "plan.json"
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
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)
            self.assertTrue(plan_path.is_file())
            for command in ("validate", "replay"):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "scene_factory",
                        "task",
                        command,
                        "--scene",
                        str(scene_path),
                        "--plan",
                        str(plan_path),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertTrue(json.loads(completed.stdout)["valid"])

        smoke = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import scene_factory.planning; assert not any(name in sys.modules for name in ('isaacsim', 'omni', 'pxr', 'carb', 'numpy'))",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(smoke.returncode, 0, smoke.stderr)


if __name__ == "__main__":
    unittest.main()
