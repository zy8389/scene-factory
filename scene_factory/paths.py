from __future__ import annotations

import os
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent


def project_root() -> Path:
    configured = os.environ.get("SCENE_FACTORY_HOME")
    return Path(configured).expanduser().resolve() if configured else PROJECT_ROOT


def default_registry_path() -> Path:
    return project_root() / "data" / "assets" / "registry.jsonl"


def default_recipes_dir() -> Path:
    return project_root() / "recipes"

