"""Run deterministic, offline structural checks for the v0.1 candidate."""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "0.1.0"
REQUIRED_FILES = (
    "CHANGELOG.md",
    "RELEASE_CHECKLIST.md",
    "THIRD_PARTY_NOTICES.md",
    "docs/ARCHITECTURE.md",
    "docs/COMPATIBILITY.md",
    "docs/API_SURFACE_v0.1.md",
    "docs/CLI_REFERENCE.md",
    "docs/PUBLIC_API.md",
    "docs/RELEASE_NOTES_v0.1.0.md",
    "docs/SCHEMA_POLICY.md",
    "examples/basic_scene/README.md",
    "examples/external_intent/README.md",
    "examples/external_intent/scene.json",
    "examples/deterministic_dataset/README.md",
    "examples/articulated_drawer/README.md",
    "examples/articulated_drawer/scene.json",
    "tools/release_artifacts.py",
    "tools/release_smoke.py",
)
PERSONAL_PATH_PATTERNS = (
    re.compile(r"\bC:[\\/](?:Users|Documents and Settings)[\\/]", re.IGNORECASE),
    re.compile(r"\bF:[\\/]", re.IGNORECASE),
    re.compile(r"Desktop\\agent", re.IGNORECASE),
    re.compile(r"\b29497\b"),
)
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}\b", re.IGNORECASE),
)
LOCAL_LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def _tracked_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode(errors="replace"))
    return [ROOT / item for item in completed.stdout.decode().split("\0") if item]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _check_files(failures: list[str]) -> None:
    for relative in REQUIRED_FILES:
        if not (ROOT / relative).is_file():
            failures.append(f"missing required release file: {relative}")


def _check_metadata(failures: list[str]) -> None:
    try:
        metadata = tomllib.loads(_read_text(ROOT / "pyproject.toml"))
        project = metadata["project"]
    except (KeyError, OSError, tomllib.TOMLDecodeError) as exc:
        failures.append(f"invalid pyproject metadata: {exc}")
        return
    expected = {
        "name": "scene-factory",
        "version": EXPECTED_VERSION,
        "requires-python": ">=3.12",
        "license": "MIT",
    }
    for key, value in expected.items():
        if project.get(key) != value:
            failures.append(f"pyproject {key} is {project.get(key)!r}, expected {value!r}")
    if project.get("dependencies") != []:
        failures.append("core runtime dependencies must remain empty")
    scripts = project.get("scripts", {})
    for name in ("scene-factory", "scene-factory-web"):
        if name not in scripts:
            failures.append(f"missing console script: {name}")


def _check_schemas(failures: list[str]) -> None:
    for path in sorted((ROOT / "schemas").glob("*.json")):
        try:
            payload = json.loads(_read_text(path))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"invalid schema {path.relative_to(ROOT)}: {exc}")
            continue
        if not isinstance(payload, dict) or payload.get("$schema") is None:
            failures.append(f"schema lacks $schema: {path.relative_to(ROOT)}")


def _check_resources(failures: list[str]) -> None:
    resource_files = (
        "data/assets/registry.jsonl",
        "web/index.html",
        "recipes/kitchen_after_cooking.json",
        "recipes/kitchen_franka_mug_lift.json",
        "recipes/kitchen_franka_mug_pick_place.json",
        "schemas/executor_capabilities.schema.json",
        "schemas/executor_conformance.schema.json",
    )
    for relative in resource_files:
        if not (ROOT / relative).is_file():
            failures.append(f"missing packaged resource: {relative}")


def _check_attribution(failures: list[str]) -> None:
    notices = _read_text(ROOT / "THIRD_PARTY_NOTICES.md") if (ROOT / "THIRD_PARTY_NOTICES.md").is_file() else ""
    if "CC BY 4.0" not in notices:
        failures.append("third-party notices do not state the recorded CC BY 4.0 license")
    for manifest in sorted((ROOT / "data/assets/source").glob("*/SOURCE.json")):
        try:
            payload = json.loads(_read_text(manifest))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"invalid source manifest {manifest.relative_to(ROOT)}: {exc}")
            continue
        for key in ("asset_id", "source_url", "license", "source_revision"):
            if not payload.get(key):
                failures.append(f"source manifest lacks {key}: {manifest.relative_to(ROOT)}")
            if payload.get("asset_id") not in notices:
                failures.append(f"asset is missing from third-party notices: {payload.get('asset_id')}")


def _check_release_documents(failures: list[str]) -> None:
    notes_path = ROOT / "docs" / "RELEASE_NOTES_v0.1.0.md"
    changelog_path = ROOT / "CHANGELOG.md"
    try:
        notes = _read_text(notes_path)
        changelog = _read_text(changelog_path)
    except (OSError, UnicodeDecodeError) as exc:
        failures.append(f"cannot read release document: {exc}")
        return
    normalized_notes = " ".join(notes.split())
    required_notes = (
        "early developer release",
        "Real P1-3 Isaac RGB-D acceptance remains environment-blocked.",
        "Real articulated execution has not been validated.",
        "Real robot execution has not been run.",
        "Isaac Lab integration has not started.",
    )
    for phrase in required_notes:
        if phrase not in normalized_notes:
            failures.append(f"release notes lack required disclosure: {phrase}")
    for phrase in ("production ready", "fully validated Isaac manipulation", "real robot ready"):
        if phrase.lower() in notes.lower():
            failures.append(f"release notes overclaim unsupported status: {phrase}")
    if "## 0.1.0 - Unreleased" not in changelog:
        failures.append("changelog must keep 0.1.0 explicitly unreleased")


def _check_links(failures: list[str], paths: list[Path]) -> None:
    for path in paths:
        if path.suffix.lower() not in {".md", ".markdown"}:
            continue
        try:
            text = _read_text(path)
        except (OSError, UnicodeDecodeError) as exc:
            failures.append(f"cannot read documentation {path.relative_to(ROOT)}: {exc}")
            continue
        for raw_target in LOCAL_LINK_PATTERN.findall(text):
            target = raw_target.strip().strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or target.startswith("#"):
                continue
            relative = parsed.path.strip("`")
            if not relative:
                continue
            target_path = (path.parent / relative).resolve()
            if not target_path.is_file() and not target_path.is_dir():
                failures.append(f"broken documentation link in {path.relative_to(ROOT)}: {target}")


def _check_hygiene(failures: list[str], tracked: list[Path]) -> None:
    forbidden_prefixes = ("outputs/", "dist/", "build/", ".venv/")
    for path in tracked:
        if path.resolve() == Path(__file__).resolve():
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(forbidden_prefixes) or relative.endswith((".log", ".tmp", ".part", ".whl")):
            failures.append(f"generated artifact is tracked: {relative}")
        if path.suffix.lower() not in {".md", ".json", ".toml", ".py", ".ps1", ".cmd", ".yml", ".yaml", ".css", ".js"}:
            continue
        try:
            text = _read_text(path)
        except (OSError, UnicodeDecodeError):
            continue
        for pattern in PERSONAL_PATH_PATTERNS:
            if pattern.search(text):
                failures.append(f"personal or absolute machine path in {relative}")
                break
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                failures.append(f"possible secret in {relative}")
                break


def run() -> dict[str, object]:
    failures: list[str] = []
    tracked = _tracked_files()
    _check_files(failures)
    _check_metadata(failures)
    _check_schemas(failures)
    _check_resources(failures)
    _check_attribution(failures)
    _check_release_documents(failures)
    _check_links(failures, [ROOT / "README.md", *(ROOT / "docs").glob("*.md"), *(ROOT / "examples").glob("**/*.md")])
    _check_hygiene(failures, tracked)
    checks = {
        "required_files": "passed",
        "metadata": "passed",
        "schemas": "passed",
        "resources": "passed",
        "attribution": "passed",
        "documentation_links": "passed",
        "tracked_file_hygiene": "passed",
    }
    if failures:
        checks = {key: "failed" for key in checks}
    return {"result": "passed" if not failures else "failed", "checks": checks, "failures": failures}


def main() -> int:
    report = run()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["result"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
