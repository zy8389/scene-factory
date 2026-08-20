from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol


class SimulatorBackend(Protocol):
    def reset(self, scene: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]: ...

    def step(
        self, action: Any
    ) -> tuple[dict[str, Any], float, bool, bool, dict[str, Any]]: ...

    def render(self) -> Any: ...

    def close(self) -> None: ...


class DryRunBackend:
    """Gym-like backend for integration tests before a robot-specific simulator is attached."""

    def __init__(self, max_steps: int = 100) -> None:
        self.max_steps = max_steps
        self.scene: dict[str, Any] | None = None
        self.steps = 0

    def reset(self, scene: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        self.scene = scene
        self.steps = 0
        return self._observation(), {"scene_id": scene["scene_id"], "dry_run": True}

    def step(self, action: Any):
        if self.scene is None:
            raise RuntimeError("reset must be called before step")
        self.steps += 1
        truncated = self.steps >= self.max_steps
        return self._observation(), 0.0, False, truncated, {"dry_run": True, "action": action}

    def render(self) -> None:
        return None

    def close(self) -> None:
        self.scene = None

    def _observation(self) -> dict[str, Any]:
        if self.scene is None:
            return {}
        return {
            "language_instruction": self.scene.get("task", {}).get("instruction", ""),
            "scene_graph": self.scene["objects"],
            "proprioception": [],
        }


class SceneFactoryEnv:
    """Small Gymnasium-compatible facade with a pluggable simulator backend."""

    metadata = {"render_modes": ["rgb_array"]}

    def __init__(self, scene_path: str | Path, backend: SimulatorBackend | None = None) -> None:
        self.scene_path = Path(scene_path)
        self.backend = backend or DryRunBackend()
        self._scene: dict[str, Any] | None = None

    def reset(self, *, seed: int | None = None, options: dict[str, Any] | None = None):
        del seed, options
        with self.scene_path.open("r", encoding="utf-8") as handle:
            self._scene = json.load(handle)
        return self.backend.reset(self._scene)

    def step(self, action: Any):
        return self.backend.step(action)

    def render(self):
        return self.backend.render()

    def close(self) -> None:
        self.backend.close()

