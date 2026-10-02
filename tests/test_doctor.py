from __future__ import annotations

import json
import subprocess

import pytest

from scene_factory import doctor
from scene_factory.cli import main


@pytest.fixture
def core_root(tmp_path, monkeypatch):
    assets = tmp_path / "data" / "assets"
    assets.mkdir(parents=True)
    (assets / "registry.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "recipes").mkdir()
    monkeypatch.setattr(doctor, "project_root", lambda: tmp_path)
    monkeypatch.setattr(doctor, "_isaac_candidate", lambda: None)
    monkeypatch.setattr(doctor, "_uv_command", lambda: None)
    monkeypatch.setattr(doctor, "_version", lambda package: None)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.delenv("BLENDER_EXE", raising=False)
    return tmp_path


def test_doctor_keeps_optional_runtimes_optional(core_root, capsys) -> None:
    assert main(["doctor", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["core_ready"] is True
    assert report["isaac"]["available"] is False
    assert report["blender"]["available"] is False
    assert report["mujoco"] is None
    assert "not installed" in doctor.format_report(report)


def test_doctor_requires_core_recipes(core_root, capsys) -> None:
    (core_root / "recipes").rmdir()
    assert main(["doctor"]) == 1
    assert "not ready" in capsys.readouterr().out


def test_doctor_resolves_configured_blender_command(core_root, monkeypatch) -> None:
    executable = core_root / "blender.exe"
    executable.write_bytes(b"")
    monkeypatch.setenv("BLENDER_EXE", "blender-command")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: str(executable))
    report = doctor.diagnose()
    assert report["blender"]["available"] is True
    assert report["blender"]["executable"] == str(executable)


@pytest.mark.parametrize(
    "metadata,stage_ok,available",
    [
        ({"isaac_sim": None, "torch": None}, True, False),
        ({"isaac_sim": "6.0.1", "torch": None}, True, True),
        ({"isaac_sim": "6.0.1", "torch": "2.8"}, False, False),
    ],
)
def test_doctor_distinguishes_usd_from_isaac(core_root, monkeypatch, metadata, stage_ok,
                                          available) -> None:
    monkeypatch.setattr(doctor, "_isaac_candidate", lambda: core_root / "isaac-python")

    def probe(command, **options):
        assert options["encoding"] == "utf-8"
        code = command[-1]
        if "Usd.Stage.CreateInMemory" in code:
            return subprocess.CompletedProcess(command, 0 if stage_ok else 1,
                                               "usd_stage_ok" if stage_ok else "", "")
        assert "import isaacsim" not in code
        return subprocess.CompletedProcess(command, 0, json.dumps(metadata), "")

    monkeypatch.setattr(doctor.subprocess, "run", probe)
    report = doctor.diagnose()
    assert report["core_ready"] is True
    assert report["isaac"]["pxr"] is True
    assert report["isaac"]["available"] is available
    assert report["isaac"]["usd_stage_usable"] is stage_ok
    assert report["isaac"]["cuda_available"] is False
    assert doctor.format_report(report)


@pytest.mark.parametrize("invalid", ["[]", "null", "not-json", ""])
def test_doctor_handles_invalid_metadata(core_root, monkeypatch, invalid) -> None:
    monkeypatch.setattr(doctor, "_isaac_candidate", lambda: core_root / "isaac-python")

    def probe(command, **options):
        stdout = "usd_stage_ok" if "Usd.Stage.CreateInMemory" in command[-1] else invalid
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(doctor.subprocess, "run", probe)
    report = doctor.diagnose()
    assert report["core_ready"] is True
    assert report["isaac"]["available"] is False
    assert "metadata probe failed" in report["isaac"]["warning"]


def test_doctor_handles_optional_probe_timeouts(core_root, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_isaac_candidate", lambda: core_root / "isaac-python")

    def timeout(command, **options):
        raise subprocess.TimeoutExpired(command, options["timeout"])

    monkeypatch.setattr(doctor.subprocess, "run", timeout)
    report = doctor.diagnose()
    assert report["core_ready"] is True
    assert report["isaac"]["available"] is False
    assert report["isaac"]["usd_stage_usable"] is False
    assert doctor.format_report(report)
