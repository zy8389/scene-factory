from __future__ import annotations

import json
import math
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from scene_factory.backends.isaac_binding import (
    IsaacArticulationBinding,
    IsaacBindingError,
    LIMIT_TOLERANCE,
    POSITION_TOLERANCE,
    SEKTION_TOP_DRAWER_BINDING,
    SEKTION_TOP_DRAWER_SEMANTICS,
    resolve_binding_observation,
    resolve_isaac_articulation_binding,
    validate_semantic_binding,
)


def _observation(binding: IsaacArticulationBinding = SEKTION_TOP_DRAWER_BINDING) -> dict:
    paths = binding.runtime_paths("/World/Cabinet")
    return {
        **paths,
        "joint_name": binding.joint_name,
        "runtime_joint_type": binding.joint_type,
        "runtime_axis": binding.joint_axis,
        "runtime_lower_limit": binding.expected_lower_limit,
        "runtime_upper_limit": binding.expected_upper_limit,
        "runtime_default_position": binding.expected_default_position,
        "runtime_current_position": binding.expected_default_position,
        "handle_frame_transform": (
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
        ),
        "collision_available": True,
        "collision_apis": ("PhysicsCollisionAPI", "PhysicsMeshCollisionAPI"),
        "collision_prim_paths": ("/World/Cabinet/drawer_handle_top/mesh",),
    }


def _asset_root(tmp_path: Path) -> Path:
    asset = tmp_path / SEKTION_TOP_DRAWER_BINDING.asset_relative_path
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"#usda 1.0\n")
    return tmp_path


class _FakeInspector:
    def __init__(self, observation: dict) -> None:
        self.observation = observation

    def inspect(self, binding: IsaacArticulationBinding, asset_source: Path) -> dict:
        assert asset_source.is_file()
        return self.observation


def test_frozen_binding_and_semantics_are_valid() -> None:
    binding = SEKTION_TOP_DRAWER_BINDING

    assert binding.asset_relative_path == (
        "Isaac/Props/Sektion_Cabinet/sektion_cabinet_instanceable.usd"
    )
    assert binding.articulation_root_prim == "/cabinet"
    assert binding.joint_prim == "/cabinet/sektion/drawer_top_joint"
    assert binding.joint_type == "prismatic"
    assert binding.joint_axis == "X"
    assert binding.expected_lower_limit == 0.0
    assert binding.expected_upper_limit == 0.40000000596
    assert binding.expected_default_position == 0.0
    assert binding.handle_frame_prim.endswith("/drawer_handle_frame")
    assert binding.closed_range == (0.0, 0.02)
    assert binding.open_range == (0.32, 0.38)
    assert binding.target_position == 0.35
    assert validate_semantic_binding(binding) == ()
    assert SEKTION_TOP_DRAWER_SEMANTICS.region_link == "drawer_handle_top"


def test_binding_serialization_round_trip_is_json_safe() -> None:
    binding = SEKTION_TOP_DRAWER_BINDING
    encoded = json.dumps(binding.to_dict(), allow_nan=False, sort_keys=True)
    restored = IsaacArticulationBinding.from_dict(json.loads(encoded))

    assert restored == binding
    assert restored.runtime_paths("/World/Cabinet")["joint_prim"] == (
        "/World/Cabinet/sektion/drawer_top_joint"
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("asset_relative_path", r"C:\assets\cabinet.usd", "relative"),
        ("asset_relative_path", "/assets/cabinet.usd", "relative"),
        ("asset_relative_path", "Isaac/../cabinet.usd", "traversal"),
        ("joint_prim", "cabinet/sektion/drawer_top_joint", "absolute USD"),
        ("joint_prim", "/cabinet/sektion/../drawer_top_joint", "normalized"),
        ("joint_type", "fixed", "prismatic or revolute"),
    ],
)
def test_malformed_path_and_type_inputs_fail_closed(field: str, value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        replace(SEKTION_TOP_DRAWER_BINDING, **{field: value})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"expected_lower_limit": math.inf}, "finite"),
        ({"expected_lower_limit": 0.4, "expected_upper_limit": 0.2}, "less than"),
        ({"expected_default_position": 0.5}, "within expected limits"),
        ({"handle_frame_prim": ""}, "non-empty"),
    ],
)
def test_malformed_numeric_and_handle_inputs_fail_closed(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(SEKTION_TOP_DRAWER_BINDING, **changes)


def test_semantic_mismatch_is_reported_without_changing_canonical_models() -> None:
    mismatched = replace(SEKTION_TOP_DRAWER_BINDING, semantic_joint_id="other_joint")

    issues = validate_semantic_binding(mismatched)

    assert any(issue.code == "semantic_joint_mismatch" for issue in issues)
    assert SEKTION_TOP_DRAWER_SEMANTICS.joint.joint_id == "drawer_top_joint"


def test_valid_fake_observation_resolves_and_serializes(tmp_path: Path) -> None:
    binding = SEKTION_TOP_DRAWER_BINDING
    resolution = resolve_isaac_articulation_binding(
        binding,
        asset_root=_asset_root(tmp_path),
        inspector=_FakeInspector(_observation()),
    )

    assert resolution.valid
    assert resolution.errors == ()
    assert resolution.runtime_current_position == 0.0
    assert json.dumps(resolution.to_dict(), allow_nan=False)


@pytest.mark.parametrize(
    ("name", "mutate", "expected_code"),
    [
        ("missing root", lambda value: value.pop("articulation_root_prim"), "articulation_root_prim_mismatch"),
        ("wrong root", lambda value: value.__setitem__("articulation_root_prim", "/World/Other"), "articulation_root_prim_mismatch"),
        ("wrong joint type", lambda value: value.__setitem__("runtime_joint_type", "revolute"), "wrong_joint_type"),
        ("wrong axis", lambda value: value.__setitem__("runtime_axis", "Y"), "wrong_joint_axis"),
        ("limit mismatch", lambda value: value.__setitem__("runtime_upper_limit", 0.25), "joint_limits_mismatch"),
        ("fixed-joint mismatch", lambda value: value.__setitem__("handle_fixed_joint_body1", "/World/Cabinet/wrong"), "handle_fixed_joint_body1_mismatch"),
        ("missing collision", lambda value: value.update({"collision_available": False, "collision_apis": ()}), "collision_missing"),
    ],
)
def test_invalid_fake_observations_fail_closed(
    tmp_path: Path, name: str, mutate, expected_code: str
) -> None:
    observation = _observation()
    mutate(observation)

    resolution = resolve_isaac_articulation_binding(
        SEKTION_TOP_DRAWER_BINDING,
        asset_root=_asset_root(tmp_path),
        inspector=_FakeInspector(observation),
    )

    assert not resolution.valid, name
    assert expected_code in {issue.code for issue in resolution.errors}
    with pytest.raises(IsaacBindingError):
        resolution.require_valid()


def test_current_position_must_be_finite_and_within_runtime_limits() -> None:
    missing = _observation()
    missing["runtime_current_position"] = None
    outside = _observation()
    outside["runtime_current_position"] = 0.5

    for observation, code in ((missing, "joint_position_invalid"), (outside, "joint_position_out_of_limits")):
        resolution = resolve_binding_observation(SEKTION_TOP_DRAWER_BINDING, observation)
        assert code in {issue.code for issue in resolution.errors}


def test_handle_frame_must_be_a_finite_invertible_matrix() -> None:
    singular = _observation()
    singular["handle_frame_transform"] = (0.0,) * 16
    non_finite = _observation()
    non_finite["handle_frame_transform"] = (math.nan,) + (0.0,) * 15

    singular_result = resolve_binding_observation(SEKTION_TOP_DRAWER_BINDING, singular)
    non_finite_result = resolve_binding_observation(SEKTION_TOP_DRAWER_BINDING, non_finite)

    assert "handle_frame_singular" in {issue.code for issue in singular_result.errors}
    assert "handle_frame_invalid" in {issue.code for issue in non_finite_result.errors}


def test_resolution_tolerates_small_runtime_float_drift() -> None:
    observation = _observation()
    observation["runtime_upper_limit"] += LIMIT_TOLERANCE / 2
    observation["runtime_current_position"] = POSITION_TOLERANCE / 2

    resolution = resolve_binding_observation(SEKTION_TOP_DRAWER_BINDING, observation)

    assert resolution.valid


def test_missing_asset_root_and_asset_fail_closed(tmp_path: Path) -> None:
    missing_root = resolve_isaac_articulation_binding(
        SEKTION_TOP_DRAWER_BINDING,
        asset_root=tmp_path / "does-not-exist",
        inspector=_FakeInspector(_observation()),
    )
    empty_root = resolve_isaac_articulation_binding(
        SEKTION_TOP_DRAWER_BINDING,
        asset_root=tmp_path,
        inspector=_FakeInspector(_observation()),
    )

    assert missing_root.errors[0].code == "asset_root_missing"
    assert empty_root.errors[0].code == "asset_missing"


def test_binding_import_does_not_load_optional_isaac_modules() -> None:
    script = (
        "import sys; import scene_factory.backends.isaac_binding; "
        "names = ('isaacsim', 'omni', 'pxr', 'carb', 'numpy'); "
        "assert not any(name in sys.modules for name in names), "
        "sorted(name for name in names if name in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
