from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from scene_factory import ArticulationJoint
from scene_factory.factory import SceneFactory
from scene_factory.paths import (
    default_recipes_dir,
    default_registry_path,
    default_schemas_dir,
    default_web_dir,
)


def main() -> int:
    registry = default_registry_path()
    recipes = default_recipes_dir()
    web = default_web_dir()
    schemas = default_schemas_dir()
    required = [
        registry,
        recipes / "kitchen_after_cooking.json",
        recipes / "kitchen_franka_mug_lift.json",
        recipes / "kitchen_franka_mug_pick_place.json",
        web / "index.html",
        schemas / "interaction_plan.schema.json",
        schemas / "execution_trace.schema.json",
        schemas / "executor_capabilities.schema.json",
        schemas / "executor_conformance.schema.json",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"installed SceneFactory resources are missing: {missing}")

    factory = SceneFactory()
    result = factory.build_from_recipe("kitchen_franka_mug_lift", 77)
    mug = next(item for item in result.scene.objects if item.object_id == "mug_1")
    if not result.valid or mug.asset_id != "mug_001":
        raise RuntimeError("installed SceneFactory failed the real-mug recipe smoke test")
    pick_place = factory.build_from_recipe("kitchen_franka_mug_pick_place", 77)
    if not pick_place.valid:
        raise RuntimeError("installed SceneFactory failed the pick-and-place recipe smoke test")
    joint = ArticulationJoint(
        joint_id="smoke_slide",
        joint_type="prismatic",
        parent="body",
        child="drawer",
        axis=(2.0, 0.0, 0.0),
        lower_limit=0.0,
        upper_limit=0.4,
        default_position=0.0,
    )
    if joint.axis != (1.0, 0.0, 0.0):
        raise RuntimeError("installed articulation metadata API failed axis normalization")
    cli = shutil.which("scene-factory")
    if cli is None:
        candidate = Path(sys.executable).with_name("scene-factory")
        if os.name == "nt":
            candidate = candidate.with_suffix(".exe")
        if candidate.is_file():
            cli = str(candidate)
    if cli is None:
        raise FileNotFoundError("installed scene-factory CLI wrapper is not available")
    with tempfile.TemporaryDirectory(prefix="scene_factory_installed_dataset_") as directory:
        dataset = str(Path(directory) / "dataset")
        intent_path = Path(directory) / "intent.json"
        intent_path.write_text(
            json.dumps(
                {
                    "room_type": "living_room",
                    "event": "recent_snacking",
                    "description": "A mug on the coffee table.",
                    "objects": [
                        {
                            "object_id": "sofa_1",
                            "category": "sofa",
                            "dynamic": False,
                            "support_hint": None,
                            "attributes": [],
                            "state": [],
                        },
                        {
                            "object_id": "coffee_table_1",
                            "category": "coffee_table",
                            "dynamic": False,
                            "support_hint": None,
                            "attributes": [],
                            "state": [],
                        },
                        {
                            "object_id": "mug_1",
                            "category": "mug",
                            "dynamic": True,
                            "support_hint": "coffee_table_1",
                            "attributes": [],
                            "state": [],
                        },
                    ],
                    "relations": [
                        {
                            "subject": "mug_1",
                            "predicate": "on",
                            "target": "coffee_table_1",
                        }
                    ],
                    "room_dimensions_m": None,
                    "clutter_level": 0.5,
                    "layout_style": "casual",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        planning_scene_path = Path(directory) / "articulated-layout.json"
        planning_plan_path = Path(directory) / "articulated-plan.json"
        execution_trace_path = Path(directory) / "execution-trace.json"
        conformance_report_path = Path(directory) / "executor-conformance.json"
        planning_scene_path.write_text(
            json.dumps(
                {
                    "scene_id": "installed-articulated-smoke",
                    "objects": [
                        {
                            "object_id": "drawer_1",
                            "asset_id": "drawer_asset",
                            "interactions": {
                                "joints": [
                                    {
                                        "joint_id": "drawer_slide",
                                        "joint_type": "prismatic",
                                        "position": 0.0,
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
                                        "allowed_actions": ["grasp", "pull"],
                                    }
                                ],
                                "semantic_states": [
                                    {
                                        "name": "open",
                                        "joint": "drawer_slide",
                                        "range": [0.35, 0.42],
                                        "target_position": 0.4,
                                    }
                                ],
                            },
                        }
                    ],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        commands = [
            [cli, "intent", "schema"],
            [cli, "intent", "validate", str(intent_path)],
            [cli, "asset", "inspect", "--asset-id", "mug_blue"],
            [
                cli,
                "build",
                "--intent",
                str(intent_path),
                "--seed",
                "77",
                "--output",
                str(Path(directory) / "intent-scene"),
            ],
            [
                cli,
                "batch",
                "--recipe",
                "living_room_recent_snacking",
                "--count",
                "2",
                "--seed-start",
                "77",
                "--output",
                dataset,
            ],
            [
                cli,
                "batch",
                "--intent",
                str(intent_path),
                "--count",
                "2",
                "--seed-start",
                "77",
                "--output",
                str(Path(directory) / "intent-dataset"),
            ],
            [cli, "dataset", "inspect", dataset],
            [cli, "dataset", "validate", dataset],
            [cli, "dataset", "reproduce", dataset],
            [cli, "dataset", "validate", str(Path(directory) / "intent-dataset")],
            [cli, "dataset", "reproduce", str(Path(directory) / "intent-dataset")],
            [
                cli,
                "task",
                "plan",
                "--scene",
                str(planning_scene_path),
                "--object",
                "drawer_1",
                "--state",
                "open",
                "--output",
                str(planning_plan_path),
            ],
            [
                cli,
                "task",
                "validate",
                "--scene",
                str(planning_scene_path),
                "--plan",
                str(planning_plan_path),
            ],
            [
                cli,
                "task",
                "replay",
                "--scene",
                str(planning_scene_path),
                "--plan",
                str(planning_plan_path),
            ],
            [
                cli,
                "task",
                "execute",
                "--scene",
                str(planning_scene_path),
                "--plan",
                str(planning_plan_path),
                "--executor",
                "dry-run",
                "--output",
                str(execution_trace_path),
            ],
            [
                cli,
                "task",
                "execution-validate",
                "--scene",
                str(planning_scene_path),
                "--plan",
                str(planning_plan_path),
                "--trace",
                str(execution_trace_path),
            ],
            [cli, "executor", "inspect", "--executor", "dry-run"],
            [
                cli,
                "executor",
                "conformance",
                "--executor",
                "dry-run",
                "--output",
                str(conformance_report_path),
            ],
            [cli, "executor", "validate-report", str(conformance_report_path)],
        ]
        for command in commands:
            completed = subprocess.run(
                command,
                cwd=directory,
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"installed CLI failed ({completed.returncode}): {' '.join(command)}\n"
                    f"stdout={completed.stdout}\nstderr={completed.stderr}"
                )
        if not execution_trace_path.is_file():
            raise RuntimeError("installed execution CLI did not write its trace output")
        if not conformance_report_path.is_file():
            raise RuntimeError("installed executor CLI did not write its conformance report")
    print(
        json.dumps(
            {
                "result": "passed",
                "registry": str(registry),
                "recipes": str(recipes),
                "web": str(web),
                "schemas": str(schemas),
                "scene_id": result.scene.scene_id,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
