from __future__ import annotations

import importlib
import math
from collections.abc import Mapping, Sequence
from typing import Any

from ..exporters.mujoco_mjcf import MujocoMjcfExporter
from ..paths import default_registry_path
from ..registry import AssetRegistry
from ..tasks import TaskEvaluator


class MujocoBackend:
    """Gym-like MuJoCo backend using deterministic MJCF collision proxies."""

    def __init__(
        self,
        *,
        registry: AssetRegistry | None = None,
        max_steps: int = 1000,
        frame_skip: int = 5,
        render_width: int = 960,
        render_height: int = 720,
    ) -> None:
        if max_steps < 1 or frame_skip < 1:
            raise ValueError("max_steps and frame_skip must be positive")
        self.registry = registry or AssetRegistry.load(default_registry_path())
        self.max_steps = max_steps
        self.frame_skip = frame_skip
        self.render_width = render_width
        self.render_height = render_height
        self.scene: dict[str, Any] | None = None
        self.steps = 0
        self._mujoco: Any = None
        self._model: Any = None
        self._data: Any = None
        self._renderer: Any = None
        self._body_ids: dict[str, int] = {}
        self._task_evaluator: TaskEvaluator | None = None

    def reset(self, scene: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        self.close()
        self._mujoco = self._load_mujoco()
        xml = MujocoMjcfExporter(
            self.registry,
            offscreen_width=self.render_width,
            offscreen_height=self.render_height,
        ).to_string(scene)
        self._model = self._mujoco.MjModel.from_xml_string(xml)
        self._data = self._mujoco.MjData(self._model)
        self._mujoco.mj_forward(self._model, self._data)
        self.scene = scene
        self.steps = 0
        self._body_ids = {
            str(item["object_id"]): self._body_id(str(item["object_id"]))
            for item in scene.get("objects", [])
        }
        initial_state = self._object_positions()
        self._task_evaluator = TaskEvaluator(dict(scene.get("task", {})), initial_state)
        return self._observation(), {
            "scene_id": scene["scene_id"],
            "backend": "mujoco",
            "timestep": float(self._model.opt.timestep) * self.frame_skip,
            "body_count": len(self._body_ids),
        }

    def step(
        self, action: Any
    ) -> tuple[dict[str, Any], float, bool, bool, dict[str, Any]]:
        if self._model is None or self._data is None or self.scene is None:
            raise RuntimeError("reset must be called before step")
        self._apply_action(action)
        for _ in range(self.frame_skip):
            self._mujoco.mj_step(self._model, self._data)
        self.steps += 1
        positions = self._object_positions()
        task_status = (
            self._task_evaluator.status(positions)
            if self._task_evaluator is not None
            else {"task_success": False}
        )
        terminated = bool(task_status.get("task_success"))
        truncated = self.steps >= self.max_steps and not terminated
        return self._observation(), float(terminated), terminated, truncated, {
            "backend": "mujoco",
            "step": self.steps,
            "task": task_status,
        }

    def render(self) -> Any:
        if self._model is None or self._data is None:
            raise RuntimeError("reset must be called before render")
        if self._renderer is None:
            self._renderer = self._mujoco.Renderer(
                self._model,
                height=self.render_height,
                width=self.render_width,
            )
        self._renderer.update_scene(self._data)
        return self._renderer.render()

    def close(self) -> None:
        if self._renderer is not None:
            self._renderer.close()
        self._renderer = None
        self._model = None
        self._data = None
        self._body_ids = {}
        self._task_evaluator = None
        self.scene = None
        self.steps = 0

    @staticmethod
    def _load_mujoco() -> Any:
        try:
            return importlib.import_module("mujoco")
        except ImportError as exc:
            raise RuntimeError(
                "MuJoCo is the default backend but the 'mujoco' package is missing. "
                "Install the project again or run: python -m pip install mujoco"
            ) from exc

    def _body_id(self, object_id: str) -> int:
        body_name = MujocoMjcfExporter.body_name(object_id)
        body_id = int(
            self._mujoco.mj_name2id(
                self._model,
                self._mujoco.mjtObj.mjOBJ_BODY,
                body_name,
            )
        )
        if body_id < 0:
            raise RuntimeError(f"MuJoCo body was not created for object {object_id!r}")
        return body_id

    def _object_positions(self) -> dict[str, tuple[float, float, float]]:
        return {
            object_id: tuple(float(value) for value in self._data.xpos[body_id])
            for object_id, body_id in self._body_ids.items()
        }

    def _observation(self) -> dict[str, Any]:
        if self.scene is None or self._data is None:
            return {}
        return {
            "language_instruction": self.scene.get("task", {}).get("instruction", ""),
            "scene_graph": self.scene.get("objects", []),
            "object_poses": {
                object_id: list(position)
                for object_id, position in self._object_positions().items()
            },
            "proprioception": {
                "qpos": [float(value) for value in self._data.qpos],
                "qvel": [float(value) for value in self._data.qvel],
            },
        }

    def _apply_action(self, action: Any) -> None:
        self._data.xfrc_applied[:] = 0.0
        if action is None:
            return
        if isinstance(action, Mapping):
            controls = action.get("ctrl")
            if controls is not None:
                self._set_controls(controls)
            forces = action.get("object_forces", {})
            if not isinstance(forces, Mapping):
                raise ValueError("object_forces must map object IDs to 3D or 6D forces")
            for object_id, values in forces.items():
                if object_id not in self._body_ids:
                    raise ValueError(f"unknown object force target: {object_id}")
                vector = self._finite_vector(values, (3, 6), "object force")
                self._data.xfrc_applied[self._body_ids[object_id], : len(vector)] = vector
            return
        self._set_controls(action)

    def _set_controls(self, values: Any) -> None:
        vector = self._finite_vector(values, (int(self._model.nu),), "control")
        if self._model.nu:
            self._data.ctrl[:] = vector

    @staticmethod
    def _finite_vector(values: Any, lengths: tuple[int, ...], label: str) -> list[float]:
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise ValueError(f"{label} must be a numeric sequence")
        vector = [float(value) for value in values]
        if len(vector) not in lengths or any(not math.isfinite(value) for value in vector):
            expected = " or ".join(str(value) for value in lengths)
            raise ValueError(f"{label} must contain {expected} finite values")
        return vector
