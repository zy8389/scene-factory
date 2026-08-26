from __future__ import annotations

import json
import tempfile
from pathlib import Path

from scene_factory.factory import SceneFactory
from scene_factory.paths import default_recipes_dir, default_registry_path, default_web_dir
from scene_factory.trajectory import EpisodeRecorder, load_episode


def main() -> int:
    registry = default_registry_path()
    recipes = default_recipes_dir()
    web = default_web_dir()
    required = [
        registry,
        recipes / "kitchen_after_cooking.json",
        recipes / "kitchen_franka_mug_lift.json",
        recipes / "kitchen_franka_mug_pick_place.json",
        web / "index.html",
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
    with tempfile.TemporaryDirectory() as directory:
        episode_path = Path(directory) / "episode_000000"
        recorder = EpisodeRecorder(
            episode_path,
            {
                "schema_version": "scene_factory.trajectory.v1",
                "scene_id": "installed-smoke",
                "recipe": "installed-smoke",
                "seed": 1,
                "robot_asset_source": "not_applicable",
                "isaac_sim_version": "not_applicable",
                "camera": {"resolution": [1, 1]},
                "intrinsics": None,
                "extrinsics": None,
                "control_frequency_hz": 60.0,
                "sensor_frequency_hz": 60.0,
            },
        )
        recorder.append(
            episode_id="installed-smoke",
            frame_id=0,
            sim_step=0,
            timestamp=0.0,
            sensor={
                "rgb": [[[0, 0, 0]]],
                "depth": [[1.0]],
                "intrinsics": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                "extrinsics": [
                    [1.0, 0.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0, 0.0],
                    [0.0, 0.0, 1.0, 0.0],
                    [0.0, 0.0, 0.0, 1.0],
                ],
            },
            robot_state={},
            object_state={},
            action=None,
            oracle={},
            phase="PRE_GRASP",
            contact_diagnostics={},
            reward=0.0,
            terminated=False,
            truncated=False,
        )
        recorder.finalize({"result": "passed"})
        if len(load_episode(episode_path)) != 1:
            raise RuntimeError("installed trajectory reader smoke test failed")
    print(
        json.dumps(
            {
                "result": "passed",
                "registry": str(registry),
                "recipes": str(recipes),
                "web": str(web),
                "scene_id": result.scene.scene_id,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
