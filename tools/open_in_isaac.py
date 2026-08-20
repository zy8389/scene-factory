from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open a SceneFactory USD in the Isaac Sim editor.")
    parser.add_argument("usd", type=Path, help="USD file to open")
    parser.add_argument(
        "--experience",
        type=Path,
        help="Optional Isaac Sim .kit experience (defaults to isaacsim.exp.full.kit)",
    )
    return parser.parse_args()


def _contains_non_ascii(value: str) -> bool:
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return True
    return False


def _default_experience() -> Path:
    candidates = [
        Path(sys.prefix) / "Lib" / "site-packages" / "isaacsim" / "apps",
        Path(sys.prefix) / "lib" / "python3.12" / "site-packages" / "isaacsim" / "apps",
    ]
    for apps_dir in candidates:
        experience = apps_dir / "isaacsim.exp.full.kit"
        if experience.is_file():
            return experience
    raise FileNotFoundError(
        "isaacsim.exp.full.kit was not found in this Python environment. "
        "Run this script with Isaac Sim's Python interpreter."
    )


def _wait_for_editor_ready(app, timeout_seconds: float = 120.0) -> None:
    """Wait until the full Isaac editor has finished its asynchronous setup."""
    import omni.kit.app

    kit_app = omni.kit.app.get_app()
    deadline = time.monotonic() + timeout_seconds
    ready_frames = 0
    while app.is_running() and time.monotonic() < deadline:
        app.update()
        ready_frames = ready_frames + 1 if kit_app.is_app_ready() else 0
        if ready_frames >= 5:
            return
        time.sleep(0.01)
    raise TimeoutError("Isaac Sim did not become ready before the startup timeout")


def _open_and_verify_scene(app, usd_path: Path) -> int:
    """Open the USD after full-app setup and verify its generated scene roots."""
    import omni.usd
    from isaacsim.core.utils.stage import open_stage

    expected = usd_path.as_posix().lower()
    stage = omni.usd.get_context().get_stage()
    current = ""
    if stage is not None:
        current = stage.GetRootLayer().realPath.replace("\\", "/").lower()
    if current != expected:
        print(f"Editor startup replaced the stage ({current or 'none'}); reopening USD.", flush=True)
        if open_stage(usd_path.as_posix()) is False:
            raise RuntimeError(f"Isaac Sim could not open {usd_path}")

    for _ in range(20):
        app.update()

    stage = omni.usd.get_context().get_stage()
    if stage is None:
        raise RuntimeError("Isaac Sim has no active USD stage after opening the scene")
    current = stage.GetRootLayer().realPath.replace("\\", "/").lower()
    required_prims = ("/World/Room", "/World/Objects")
    missing = [path for path in required_prims if not stage.GetPrimAtPath(path).IsValid()]
    if current != expected or missing:
        raise RuntimeError(
            f"USD verification failed: layer={current!r}, missing_prims={missing!r}"
        )
    return sum(1 for _ in stage.Traverse())


def _frame_scene(app) -> None:
    """Point the active viewport at the generated room."""

    import omni.kit.commands
    from omni.kit.viewport.utility import get_active_viewport
    from pxr import Usd

    viewport = get_active_viewport()
    if viewport is None:
        print("Warning: the active viewport is not ready; use F to frame the scene.", flush=True)
        return

    width, height = viewport.resolution
    omni.kit.commands.execute(
        "FramePrimsCommand",
        prim_to_move=viewport.camera_path,
        prims_to_frame=["/World/Room", "/World/Objects"],
        time_code=Usd.TimeCode.Default(),
        aspect_ratio=width / max(height, 1),
        zoom=0.72,
    )


def main() -> int:
    args = _parse_args()
    usd_path = args.usd.expanduser().resolve()
    if not usd_path.is_file():
        raise FileNotFoundError(usd_path)
    if sys.platform == "win32" and _contains_non_ascii(str(usd_path)):
        raise RuntimeError(
            "Isaac Sim/OpenUSD cannot reliably open non-ASCII USD paths on Windows. "
            "Export the scene to an ASCII-only path first."
        )

    experience = (args.experience or _default_experience()).expanduser().resolve()
    if not experience.is_file():
        raise FileNotFoundError(experience)

    # SimulationApp forwards process arguments to Kit, so keep this script's
    # arguments out of Kit's command-line parser.
    sys.argv = [sys.argv[0]]
    os.environ.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")

    # SimulationApp must be constructed before importing omni or pxr modules.
    from isaacsim import SimulationApp

    app = SimulationApp(
        {
            "headless": False,
            "hide_ui": False,
            "renderer": "RaytracedLighting",
            "anti_aliasing": 2,
            "multi_gpu": False,
            "max_gpu_count": 1,
            "width": 1280,
            "height": 720,
            "window_width": 1440,
            "window_height": 900,
            "display_options": 3286,
            "open_usd": usd_path.as_posix(),
            "fast_shutdown": True,
            "extra_args": [
                "--/app/renderer/skipWhileMinimized=true",
                # isaacsim.app.setup otherwise creates a New Stage asynchronously
                # after SimulationApp has already opened our USD.
                "--/isaac/startup/create_new_stage=false",
            ],
        },
        experience=experience.as_posix(),
    )

    try:
        _wait_for_editor_ready(app)
        prim_count = _open_and_verify_scene(app, usd_path)
        _frame_scene(app)
        print(f"Isaac Sim verified {prim_count} prims from: {usd_path}", flush=True)
        print("Close the Isaac Sim window to end this launcher.", flush=True)
        while app.is_running():
            app.update()
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
