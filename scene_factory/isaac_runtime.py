from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path

from .paths import project_root


_NATIVE_REALPATH = os.path.realpath
_ISAAC_ENVIRONMENT_NAME = "scene_factory_isaac_py312"


def _absolute_without_resolving_links(path: Path) -> Path:
    """Return an absolute path while preserving junction/symlink spelling.

    ``Path.resolve()`` is intentionally not used here. On Windows, Isaac's
    USD bindings can import successfully but fail to create a USD stage when
    the interpreter path contains non-ASCII characters. A junction with an
    ASCII path fixes that runtime issue, so resolving the junction back to its
    Chinese target would undo the fix.
    """

    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _same_real_path(left: Path, right: Path) -> bool:
    """Compare paths after resolving links, for junction validation only."""

    return os.path.normcase(_NATIVE_REALPATH(os.fspath(left))) == os.path.normcase(
        _NATIVE_REALPATH(os.fspath(right))
    )


def _ascii_junction_python(candidate: Path) -> Path | None:
    """Create/use an ASCII junction for an Isaac Python executable on Windows.

    This never copies, deletes, or recreates an environment. If the desired
    alias already exists and points elsewhere, it is left untouched and the
    caller falls back to the original path.
    """

    if os.name != "nt" or str(candidate).isascii():
        return candidate
    if not candidate.is_file():
        return None

    environment_root = candidate.parent.parent
    drive_root = Path(environment_root.anchor)
    if not drive_root:
        return None
    alias_root = drive_root / _ISAAC_ENVIRONMENT_NAME
    try:
        relative_python = candidate.relative_to(environment_root)
    except ValueError:
        return None

    if alias_root.exists() or os.path.lexists(os.fspath(alias_root)):
        if not alias_root.is_dir() or not _same_real_path(alias_root, environment_root):
            return None
    else:
        command = f'mklink /J "{alias_root}" "{environment_root}"'
        try:
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", command],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0 or not _same_real_path(alias_root, environment_root):
            return None

    alias_python = alias_root / relative_python
    return alias_python if alias_python.is_file() else None


def prepare_isaac_python(candidate: str | Path) -> Path:
    """Preserve an Isaac executable's ASCII alias when one is available."""

    original = _absolute_without_resolving_links(Path(candidate))
    alias = _ascii_junction_python(original)
    return alias or original


def _candidate_paths() -> list[Path]:
    configured = os.environ.get("SCENE_FACTORY_ISAAC_PYTHON", "").strip()
    local = (
        project_root()
        / "local_resources"
        / "environments"
        / _ISAAC_ENVIRONMENT_NAME
        / "Scripts"
        / "python.exe"
    )
    # Explicit/configured and bundled Isaac environments may need the ASCII
    # junction. Do not create an alias for the ordinary project .venv fallback.
    raw_candidates = [
        (Path(configured).expanduser(), True) if configured else None,
        (local, True),
        (Path(sys.executable), False),
    ]
    candidates: list[Path] = []
    for item in raw_candidates:
        if item is None:
            continue
        raw, allow_alias = item
        candidate = _absolute_without_resolving_links(raw)
        if allow_alias:
            candidate = prepare_isaac_python(candidate)
        if candidate.is_file() and _path_key(candidate) not in {
            _path_key(existing) for existing in candidates
        }:
            candidates.append(candidate)
    return candidates


def activate_isaac_kit_runtime() -> None:
    """Make Isaac Kit imports use the ASCII environment spelling on Windows.

    Isaac Sim's Python bootstrap calls ``realpath(__file__)`` and then builds
    ``ISAAC_PATH``/``EXP_PATH`` from that result.  With a Chinese checkout this
    puts non-ASCII paths back into Kit even when Python itself was launched via
    the ASCII junction.  Preserve the junction spelling during Kit bootstrap,
    remap an originally-launched environment's ``sys.path`` when possible.
    This does not copy or rebuild Isaac Sim or change the working directory.
    """

    if os.name != "nt":
        return

    original_executable = _absolute_without_resolving_links(Path(sys.executable))
    alias_executable = prepare_isaac_python(original_executable)
    original_root = original_executable.parent.parent
    alias_root = alias_executable.parent.parent

    if _path_key(original_root) != _path_key(alias_root):
        mapped_paths: list[str] = []
        original_root_text = os.path.normcase(os.fspath(original_root))
        for value in sys.path:
            if not value:
                mapped_paths.append(value)
                continue
            absolute_value = os.path.abspath(value)
            if os.path.normcase(absolute_value).startswith(original_root_text + os.sep):
                suffix = Path(absolute_value).relative_to(original_root)
                mapped_paths.append(os.fspath(alias_root / suffix))
            elif os.path.normcase(absolute_value) == original_root_text:
                mapped_paths.append(os.fspath(alias_root))
            else:
                mapped_paths.append(value)
        sys.path[:] = mapped_paths
        # These are writable Python attributes and keep libraries that consult
        # the active virtual environment from reconstructing the Chinese path.
        sys.prefix = os.fspath(alias_root)
        sys.exec_prefix = os.fspath(alias_root)

    # Do not change the caller's working directory here. This helper is also
    # used by the in-process backend and diagnostics; changing cwd would leak
    # into callers (and into pytest) after Kit shuts down. Standalone launcher
    # scripts already pass absolute ASCII USD/output paths.


@contextmanager
def isaac_kit_runtime():
    """Preserve only Isaac environment aliases during import, then restore realpath."""
    activate_isaac_kit_runtime()
    original_realpath = os.path.realpath
    executable = prepare_isaac_python(sys.executable) if os.name == "nt" else Path(sys.executable)
    alias_root = executable.parent.parent
    target_root = _NATIVE_REALPATH(os.fspath(alias_root))
    target_key = os.path.normcase(target_root)

    def isaac_realpath(path: str | bytes, *, strict: bool = False) -> str | bytes:
        resolved = original_realpath(path, strict=strict)
        decoded = os.fsdecode(resolved)
        normalized = os.path.normcase(decoded)
        if normalized == target_key or normalized.startswith(target_key + os.sep):
            relative = os.path.relpath(decoded, target_root)
            mapped = os.fspath(alias_root / relative)
            return os.fsencode(mapped) if isinstance(path, bytes) else mapped
        return resolved

    if os.name == "nt" and str(alias_root).isascii():
        os.path.realpath = isaac_realpath
    try:
        yield
    finally:
        os.path.realpath = original_realpath


def load_simulation_app():
    """Import Isaac's bootstrap without leaving its path compatibility hook active."""
    with isaac_kit_runtime():
        return import_module("isaacsim").SimulationApp


def find_isaac_python() -> Path:
    checked: set[str] = set()
    for candidate in _candidate_paths():
        key = _path_key(candidate)
        if key in checked:
            continue
        checked.add(key)
        probe = subprocess.run(
            [str(candidate), "-c", "import pxr"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    raise RuntimeError(
        "Isaac validation is optional but no Python with 'pxr' was found. "
        "Set SCENE_FACTORY_ISAAC_PYTHON to Isaac Sim's python executable. "
        "On Windows, use an ASCII-only path or allow SceneFactory to create "
        f"the {_ISAAC_ENVIRONMENT_NAME} junction."
    )


def export_usd_with_isaac(
    layout_path: str | Path,
    registry_path: str | Path,
    output_path: str | Path,
) -> Path:
    runtime = find_isaac_python()
    command = [
        str(runtime),
        "-m",
        "scene_factory.isaac_export",
        str(Path(layout_path).resolve()),
        "--registry",
        str(Path(registry_path).resolve()),
        "--output",
        str(Path(output_path).resolve()),
    ]
    result = subprocess.run(
        command,
        cwd=project_root(),
        env=isaac_process_environment(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Isaac USD export failed: {detail[-1000:]}")
    return Path(output_path).resolve()


def isaac_process_environment() -> dict[str, str]:
    """Make packaged worker modules and the caller's resources available to Isaac."""
    environment = os.environ.copy()
    python_path = environment.get("PYTHONPATH", "")
    code_root = Path(__file__).resolve().parent.parent
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(code_root), python_path) if value
    )
    environment["SCENE_FACTORY_HOME"] = str(project_root())
    return environment


def package_usd_with_isaac(source: str | Path) -> bytes:
    """Localize every USD dependency in an isolated worker and return its USDZ bytes."""
    runtime = find_isaac_python()
    with tempfile.TemporaryDirectory(prefix="scene-factory-usd-") as directory:
        output = Path(directory) / "scene.usdz"
        result = subprocess.run(
            [str(runtime), "-m", "scene_factory.isaac_export", str(Path(source).resolve()),
             "--package", "--output", str(output)],
            cwd=directory,
            env=isaac_process_environment(),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0 or not output.is_file():
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"Portable USD packaging failed: {detail[-1000:]}")
        return output.read_bytes()
