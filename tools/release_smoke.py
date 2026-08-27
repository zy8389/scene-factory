"""Run the pure-Python v0.1 release smoke from an installed environment.

This script intentionally imports only standard-library modules before checking
that the installed package is outside the repository. All CLI commands run in
a temporary working directory with repository and user-site paths removed.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


EXPECTED_VERSION = "0.1.0"
RECIPE = "living_room_recent_snacking"
REQUIRED_RECIPES = {
    "kitchen_after_cooking.json",
    "kitchen_franka_mug_lift.json",
    "kitchen_franka_mug_pick_place.json",
    "living_room_recent_snacking.json",
    "living_room_returned_home.json",
}
HELP_COMMANDS = (
    (),
    ("list-recipes",),
    ("build",),
    ("batch",),
    ("llm-status",),
    ("llm-test",),
    ("asset",),
    ("asset", "inspect"),
    ("asset", "normalize"),
    ("asset", "collision"),
    ("intent",),
    ("intent", "validate"),
    ("intent", "inspect"),
    ("intent", "schema"),
    ("dataset",),
    ("dataset", "inspect"),
    ("dataset", "validate"),
    ("dataset", "reproduce"),
    ("task",),
    ("task", "plan"),
    ("task", "validate"),
    ("task", "replay"),
    ("task", "execute"),
    ("task", "execution-validate"),
    ("executor",),
    ("executor", "inspect"),
    ("executor", "conformance"),
    ("executor", "validate-report"),
)


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        rendered = " ".join(command)
        raise RuntimeError(
            f"command failed with exit code {completed.returncode}: {rendered}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed.stdout


def _json_command(command: list[str], *, cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    output = _run(command, cwd=cwd, env=env)
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"command did not produce JSON: {' '.join(command)}\n{output}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"command JSON result is not an object: {' '.join(command)}")
    return value


def _assert_valid(report: dict[str, Any], label: str) -> None:
    if report.get("valid") is not True and report.get("result") != "passed":
        raise RuntimeError(f"{label} did not pass: {json.dumps(report, sort_keys=True)}")


def _intent_payload() -> dict[str, Any]:
    return {
        "room_type": "living_room",
        "event": "recent_snacking",
        "description": "A mug on the coffee table after a snack.",
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
            {"subject": "mug_1", "predicate": "on", "target": "coffee_table_1"}
        ],
        "room_dimensions_m": None,
        "clutter_level": 0.5,
        "layout_style": "casual",
    }


def _articulated_scene() -> dict[str, Any]:
    return {
        "scene_id": "release-smoke-articulated-drawer",
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
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _installed_package_check(source_root: Path) -> dict[str, str]:
    distribution = importlib.metadata.distribution("scene-factory")
    version = distribution.version
    if version != EXPECTED_VERSION:
        raise RuntimeError(f"installed distribution version is {version!r}, expected {EXPECTED_VERSION!r}")

    entry_points = {
        entry_point.name
        for entry_point in distribution.entry_points
        if entry_point.group == "console_scripts"
    }
    required_entry_points = {"scene-factory", "scene-factory-web"}
    if not required_entry_points <= entry_points:
        missing = sorted(required_entry_points - entry_points)
        raise RuntimeError(f"installed entry points are missing: {missing}")

    import scene_factory

    module_path = Path(scene_factory.__file__).resolve()
    if module_path.is_relative_to(source_root):
        raise RuntimeError(f"release smoke imported repository source: {module_path}")
    if scene_factory.__version__ != version:
        raise RuntimeError(
            f"package version {scene_factory.__version__!r} does not match distribution {version!r}"
        )

    from scene_factory.paths import (
        default_recipes_dir,
        default_registry_path,
        default_schemas_dir,
        default_web_dir,
    )

    required_resources = [
        default_registry_path(),
        default_web_dir() / "index.html",
        default_schemas_dir() / "scene_intent.schema.json",
        default_schemas_dir() / "interaction_plan.schema.json",
        default_schemas_dir() / "execution_trace.schema.json",
        default_schemas_dir() / "executor_conformance.schema.json",
    ]
    recipes = default_recipes_dir()
    missing = [str(path) for path in required_resources if not path.is_file()]
    missing.extend(str(recipes / name) for name in sorted(REQUIRED_RECIPES) if not (recipes / name).is_file())
    if missing:
        raise RuntimeError(f"installed resources are missing: {missing}")
    return {"version": version, "module": str(module_path)}


def _find_cli() -> str:
    cli = shutil.which("scene-factory")
    if cli is not None:
        return cli
    candidate = Path(sys.executable).with_name("scene-factory")
    if os.name == "nt":
        candidate = candidate.with_suffix(".exe")
    if candidate.is_file():
        return str(candidate)
    raise RuntimeError("installed scene-factory command is not on PATH or next to Python")


def run() -> dict[str, Any]:
    source_root = Path(__file__).resolve().parents[1]
    package = _installed_package_check(source_root)
    cli = _find_cli()

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("SCENE_FACTORY_HOME", None)
    environment["PYTHONNOUSERSITE"] = "1"

    checks: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="scene_factory_release_smoke_") as directory:
        cwd = Path(directory).resolve()
        if cwd.is_relative_to(source_root):
            raise RuntimeError(f"release smoke cwd is inside repository: {cwd}")
        module_probe = _run([sys.executable, "-c", "import scene_factory; print(scene_factory.__file__)"], cwd=cwd, env=environment)
        if Path(module_probe.strip()).resolve().is_relative_to(source_root):
            raise RuntimeError("release smoke subprocess resolved the repository source")
        checks["cwd_outside_repository"] = "passed"
        checks["installed_import"] = "passed"
        checks["package_metadata"] = "passed"

        _run([sys.executable, "-m", "pip", "check"], cwd=cwd, env=environment)
        checks["pip_check"] = "passed"

        for command in HELP_COMMANDS:
            _run([cli, *command, "--help"], cwd=cwd, env=environment)
        checks["cli_help"] = "passed"

        recipes = _run([cli, "list-recipes"], cwd=cwd, env=environment).splitlines()
        if RECIPE not in recipes:
            raise RuntimeError(f"packaged recipe is missing from list-recipes: {RECIPE}")
        checks["list_recipes"] = "passed"

        intent_path = cwd / "intent.json"
        _write_json(intent_path, _intent_payload())
        intent_report = _json_command([cli, "intent", "validate", str(intent_path)], cwd=cwd, env=environment)
        _assert_valid(intent_report, "intent validation")
        checks["intent"] = "passed"

        build_root = cwd / "build"
        build_report = _json_command(
            [cli, "build", "--intent", str(intent_path), "--seed", "42", "--output", str(build_root)],
            cwd=cwd,
            env=environment,
        )
        _assert_valid(build_report, "intent build")
        checks["build"] = "passed"

        dataset_root = cwd / "dataset"
        batch_report = _json_command(
            [
                cli,
                "batch",
                "--intent",
                str(intent_path),
                "--count",
                "3",
                "--seed-start",
                "100",
                "--output",
                str(dataset_root),
            ],
            cwd=cwd,
            env=environment,
        )
        if batch_report.get("generated") != 3 or batch_report.get("valid") != 3:
            raise RuntimeError(f"batch smoke did not produce three valid scenes: {batch_report}")
        checks["batch"] = "passed"

        _assert_valid(_json_command([cli, "dataset", "inspect", str(dataset_root)], cwd=cwd, env=environment), "dataset inspect")
        _assert_valid(_json_command([cli, "dataset", "validate", str(dataset_root)], cwd=cwd, env=environment), "dataset validation")
        checks["dataset_validate"] = "passed"
        _assert_valid(_json_command([cli, "dataset", "reproduce", str(dataset_root)], cwd=cwd, env=environment), "dataset reproduction")
        checks["dataset_reproduce"] = "passed"

        scene_path = cwd / "articulated-scene.json"
        plan_path = cwd / "interaction-plan.json"
        trace_path = cwd / "execution-trace.json"
        conformance_path = cwd / "executor-conformance.json"
        _write_json(scene_path, _articulated_scene())
        _assert_valid(
            _json_command(
                [cli, "task", "plan", "--scene", str(scene_path), "--object", "drawer_1", "--state", "open", "--output", str(plan_path)],
                cwd=cwd,
                env=environment,
            ),
            "task planning",
        )
        checks["task_plan"] = "passed"
        _assert_valid(
            _json_command([cli, "task", "validate", "--scene", str(scene_path), "--plan", str(plan_path)], cwd=cwd, env=environment),
            "task validation",
        )
        checks["task_validate"] = "passed"
        _assert_valid(
            _json_command(
                [cli, "task", "execute", "--scene", str(scene_path), "--plan", str(plan_path), "--executor", "dry-run", "--output", str(trace_path)],
                cwd=cwd,
                env=environment,
            ),
            "task execution",
        )
        checks["task_execute"] = "passed"
        _assert_valid(
            _json_command(
                [cli, "task", "execution-validate", "--scene", str(scene_path), "--plan", str(plan_path), "--trace", str(trace_path)],
                cwd=cwd,
                env=environment,
            ),
            "execution trace validation",
        )
        checks["trace_validation"] = "passed"

        inspect = _json_command([cli, "executor", "inspect", "--executor", "dry-run"], cwd=cwd, env=environment)
        _assert_valid(inspect, "executor inspection")
        conformance = _json_command(
            [cli, "executor", "conformance", "--executor", "dry-run", "--output", str(conformance_path)],
            cwd=cwd,
            env=environment,
        )
        if conformance.get("result") != "passed":
            raise RuntimeError(f"executor conformance did not pass: {conformance}")
        checks["executor_conformance"] = "passed"
        _assert_valid(
            _json_command([cli, "executor", "validate-report", str(conformance_path)], cwd=cwd, env=environment),
            "executor report validation",
        )
        checks["executor_report"] = "passed"

    return {
        "version": package["version"],
        "result": "passed",
        "checks": checks,
    }


def main() -> int:
    try:
        print(json.dumps(run(), indent=2, sort_keys=True))
    except (OSError, RuntimeError, ValueError, importlib.metadata.PackageNotFoundError) as exc:
        print(json.dumps({"result": "failed", "error": str(exc)}, indent=2, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
