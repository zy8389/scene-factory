from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from scene_factory.factory import SceneFactory
from scene_factory.paths import default_recipes_dir, default_registry_path, default_web_dir


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
        commands = [
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
            [cli, "dataset", "inspect", dataset],
            [cli, "dataset", "validate", dataset],
            [cli, "dataset", "reproduce", dataset],
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
