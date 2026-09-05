"""Physical Isaac Sim interaction primitives for the frozen P1-4 fixture.

The public boundary in this module is :class:`IsaacInteractionExecutor`.  Isaac
imports stay inside ``_IsaacInteractionRuntime`` so importing the executor is
safe in ordinary Python and test processes.  The executor deliberately owns no
drawer command API: the runtime can write the drawer only while establishing
the reset state, while every action uses Franka targets and physics steps.
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as distribution_version
from importlib import import_module
from pathlib import Path
from typing import Any

from ..execution import (
    ExecutionCommand,
    ExecutionError,
    ExecutionStepResult,
    ExecutorCapabilities,
)
from ..planning import InteractionWorldState
from ..robotics import quaternion_angular_distance
from .isaac import (
    _action_joint_positions,
    _configure_bundled_franka_drives,
    _configure_franka_runtime_drives,
    _empty_asset_root_diagnostics,
    _finger_gripper_is_open,
    _franka_kinematics_frame,
    _load_simulation_app,
    _read_finger_dof_diagnostics,
    _read_runtime_dof_limits,
    _resolve_finger_gripper_config,
    _resolve_franka_link_paths,
    _resolve_franka_usd,
    IsaacSimBackend,
)
from .isaac_binding import (
    SEKTION_TOP_DRAWER_BINDING,
    SEKTION_TOP_DRAWER_RUNTIME_ROOT,
    IsaacArticulationBinding,
    IsaacArticulationBindingResolution,
    resolve_isaac_articulation_binding,
)


_ISAAC_INTERACTION_ACTIONS = frozenset({"approach", "grasp", "pull", "release"})
_LEGACY_GRASP_ORIENTATION_WXYZ = (0.0, 1.0, 0.0, 0.0)
_LEGACY_GRASP_OFFSET_M = (0.0, 0.0, -0.005)
_LEGACY_PRE_GRASP_OFFSET_M = (-0.06, 0.0, 0.0)
_GRASP_TOPOLOGIES = frozenset({"front_face_clamp", "top_bottom_pinch", "side_pinch", "cage_hook"})
_FINAL_GRASP_TOPOLOGY = "top_bottom_pinch"
_PRE_GRASP_DISTANCE_M = 0.06
_PULL_DISTANCE_M = 0.05
_POSITION_TOLERANCE_M = 0.01
_ORIENTATION_TOLERANCE_RAD = 0.1
_PULL_TOLERANCE_M = 0.015
_CONTACT_STABLE_STEPS = 5
_RELEASE_OBSERVATION_STEPS = 20
_ORIENTATION_FAMILY_DEGREES = tuple(range(-90, 91, 15))
_ORIENTATION_REFINEMENT_DEGREES = tuple(range(-90, 91, 5))
_CURRENT_FIXTURE_ROBOT_BASE_POSITION_M = (0.7, 0.0, 0.78)
_FIXTURE_BASE_OFFSET_LIMIT_M = (0.08, 0.08, 0.10)
_FIXTURE_BASE_YAW_LIMIT_DEG = 10.0
_FRAME_TRANSFORM_TOLERANCE_M = 1.0e-5
_FRAME_TRANSFORM_TOLERANCE_RAD = 1.0e-5
_GRIPPER_CLEARANCE_BUFFER_M = 0.002

# Frozen from the successful G3 motion route, successful 1 mm physical pull,
# and the guarded reproduction of the failed 3 mm branch switch.  The guard is
# the geometric midpoint between the largest successful local transition and
# the smallest observed failed-branch transition, leaving equal multiplicative
# separation from both measured populations.
_MOTION_ROUTE_LOCAL_MAX_DQ_RAD = 0.06031647148119346
_MOTION_ROUTE_LOCAL_MEDIAN_DQ_RAD = 0.029276682732319814
_MOTION_ROUTE_LOCAL_PATH_LENGTH_RAD = 0.506623152918832
_PHYSICAL_1MM_MAX_DQ_RAD = 0.03198213249419557
_PHYSICAL_1MM_L2_DQ_RAD = 0.037897354518649055
_FAILED_BRANCH_MIN_MAX_DQ_RAD = 3.1470161321869194
_FAILED_BRANCH_OBSERVED_MAX_DQ_RAD = 3.152834388065739
_IK_BRANCH_GUARD_MAX_DQ_RAD = math.sqrt(
    _MOTION_ROUTE_LOCAL_MAX_DQ_RAD * _FAILED_BRANCH_MIN_MAX_DQ_RAD
)
_PULL_CONTINUATION_SPACING_M = 0.001
_PULL_MIN_CONTINUATION_SPACING_M = _PULL_CONTINUATION_SPACING_M / 4.0
_PULL_JOINT_CONVERGENCE_RAD = _PHYSICAL_1MM_MAX_DQ_RAD / 2.0
_PULL_MIN_EXPECTED_DQ_RAD = _PHYSICAL_1MM_MAX_DQ_RAD * (
    _PULL_MIN_CONTINUATION_SPACING_M / _PULL_CONTINUATION_SPACING_M
)
_PULL_IK_POSITION_TOLERANCE_M = 0.0005


@dataclass(frozen=True)
class GraspTopologyConfig:
    """Validated, simulator-independent policy for the physical grasp path.

    The orientation and position are intentionally absent.  Those values are
    observations derived from the loaded handle and Franka collision geometry
    by the Isaac runtime for each frozen fixture reset.
    """

    selected_topology: str = _FINAL_GRASP_TOPOLOGY
    pregrasp_distance_m: float = _PRE_GRASP_DISTANCE_M
    orientation_family_angle_deg: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.selected_topology, str) or self.selected_topology not in _GRASP_TOPOLOGIES:
            raise ValueError(f"unsupported grasp topology: {self.selected_topology}")
        distance = _finite(self.pregrasp_distance_m, "pregrasp_distance_m")
        if distance <= 0.0:
            raise ValueError("pregrasp_distance_m must be positive")
        object.__setattr__(self, "pregrasp_distance_m", distance)
        angle = self.orientation_family_angle_deg
        if angle is not None:
            if isinstance(angle, bool) or not isinstance(angle, int):
                raise ValueError("orientation_family_angle_deg must be an integer or None")
            if angle not in _ORIENTATION_REFINEMENT_DEGREES:
                raise ValueError(
                    "orientation_family_angle_deg must be a deterministic 5-degree "
                    "family member from -90 through 90"
                )


def _dot(first: tuple[float, ...], second: tuple[float, ...]) -> float:
    return sum(a * b for a, b in zip(first, second, strict=True))


def _cross(first: tuple[float, float, float], second: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )


def _subtract(first: tuple[float, ...], second: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(a - b for a, b in zip(first, second, strict=True))


def _cartesian_waypoints(
    start: tuple[float, ...],
    end: tuple[float, ...],
    spacing_m: float,
) -> list[tuple[float, ...]]:
    distance = _norm(_subtract(end, start))
    spacing = _finite(spacing_m, "Cartesian waypoint spacing")
    count = max(1, math.ceil(distance / spacing - 1.0e-12))
    delta = _subtract(end, start)
    return [_add(start, _scale(delta, index / count)) for index in range(1, count + 1)]


def _continuity_policy() -> dict[str, Any]:
    return {
        "successful_baseline": {
            "motion_route_local_max_dq_rad": _MOTION_ROUTE_LOCAL_MAX_DQ_RAD,
            "motion_route_local_median_dq_rad": _MOTION_ROUTE_LOCAL_MEDIAN_DQ_RAD,
            "motion_route_local_path_length_rad": _MOTION_ROUTE_LOCAL_PATH_LENGTH_RAD,
            "physical_1mm_max_dq_rad": _PHYSICAL_1MM_MAX_DQ_RAD,
            "physical_1mm_l2_dq_rad": _PHYSICAL_1MM_L2_DQ_RAD,
        },
        "failed_branch_baseline": {
            "minimum_max_dq_rad": _FAILED_BRANCH_MIN_MAX_DQ_RAD,
            "observed_q_max_dq_rad": _FAILED_BRANCH_OBSERVED_MAX_DQ_RAD,
        },
        "branch_guard_max_dq_rad": _IK_BRANCH_GUARD_MAX_DQ_RAD,
        "guard_derivation": "geometric_midpoint_successful_local_max_and_failed_branch_min",
        "success_to_guard_ratio": (
            _IK_BRANCH_GUARD_MAX_DQ_RAD / _MOTION_ROUTE_LOCAL_MAX_DQ_RAD
        ),
        "failure_to_guard_ratio": (
            _FAILED_BRANCH_MIN_MAX_DQ_RAD / _IK_BRANCH_GUARD_MAX_DQ_RAD
        ),
        "waypoint_spacing_m": _PULL_CONTINUATION_SPACING_M,
        "minimum_waypoint_spacing_m": _PULL_MIN_CONTINUATION_SPACING_M,
        "minimum_spacing_derivation": "successful_1mm_increment_divided_by_four",
        "joint_convergence_tolerance_rad": _PULL_JOINT_CONVERGENCE_RAD,
        "joint_convergence_derivation": "successful_1mm_max_dq_divided_by_two",
        "minimum_expected_waypoint_dq_rad": _PULL_MIN_EXPECTED_DQ_RAD,
        "minimum_expected_dq_derivation": "successful_1mm_max_dq_scaled_to_minimum_spacing",
        "solve_once_per_waypoint": True,
        "seed_hierarchy": ["physical_grasp_terminal_q", "previous_accepted_solution"],
    }


def _joint_delta_metrics(
    candidate: list[float] | tuple[float, ...],
    reference: list[float] | tuple[float, ...],
    joint_names: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    if len(candidate) != len(reference) or not candidate:
        raise ValueError("joint continuity vectors must have the same non-zero length")
    deltas = [
        _finite(value, "candidate joint") - _finite(origin, "reference joint")
        for value, origin in zip(candidate, reference, strict=True)
    ]
    absolute = [abs(value) for value in deltas]
    maximum = max(absolute)
    maximum_index = absolute.index(maximum)
    names = (
        [str(name) for name in joint_names]
        if joint_names is not None and len(joint_names) == len(deltas)
        else [f"joint_{index + 1}" for index in range(len(deltas))]
    )
    return {
        "per_joint_delta_rad": deltas,
        "l2_norm_rad": math.sqrt(sum(value * value for value in deltas)),
        "max_abs_delta_rad": maximum,
        "max_joint_index": maximum_index,
        "max_joint_name": names[maximum_index],
    }


def _joint_limit_diagnostic(
    joint_positions: list[float] | tuple[float, ...],
    joint_limits: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    joint_names: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    if len(joint_positions) != len(joint_limits) or not joint_positions:
        raise ValueError("joint positions and limits must have the same non-zero length")
    names = (
        [str(name) for name in joint_names]
        if joint_names is not None and len(joint_names) == len(joint_positions)
        else [f"joint_{index + 1}" for index in range(len(joint_positions))]
    )
    joints: list[dict[str, Any]] = []
    for index, (raw_q, raw_limit) in enumerate(
        zip(joint_positions, joint_limits, strict=True)
    ):
        if not isinstance(raw_limit, (list, tuple)) or len(raw_limit) != 2:
            raise ValueError(f"joint limit {index} must contain lower and upper bounds")
        q = _finite(raw_q, "arm joint position")
        lower = _finite(raw_limit[0], "arm lower limit")
        upper = _finite(raw_limit[1], "arm upper limit")
        if lower > upper:
            raise ValueError(f"joint limit {index} has lower bound above upper bound")
        lower_distance = q - lower
        upper_distance = upper - q
        margin = min(lower_distance, upper_distance)
        joints.append(
            {
                "joint_index": index,
                "joint_name": names[index],
                "joint_position_rad": q,
                "lower_limit_rad": lower,
                "upper_limit_rad": upper,
                "distance_to_lower_limit_rad": lower_distance,
                "distance_to_upper_limit_rad": upper_distance,
                "distance_to_nearest_limit_rad": margin,
                "nearest_limit": "lower" if lower_distance <= upper_distance else "upper",
                "within_limits": margin >= 0.0,
            }
        )
    limiting = min(joints, key=lambda item: item["distance_to_nearest_limit_rad"])
    return {
        "joint_count": len(joints),
        "all_within_limits": all(item["within_limits"] for item in joints),
        "minimum_joint_limit_margin_rad": limiting["distance_to_nearest_limit_rad"],
        "limiting_joint": dict(limiting),
        "joints": joints,
    }


def _displacement_accounting(
    commanded_eef_displacement_m: float,
    observed_right_gripper_displacement_m: float,
    observed_finger_midpoint_displacement_m: float,
    observed_handle_displacement_m: float,
    observed_drawer_displacement_m: float,
) -> dict[str, Any]:
    commanded = _finite(commanded_eef_displacement_m, "commanded EEF displacement")
    right_gripper = _finite(
        observed_right_gripper_displacement_m,
        "observed right_gripper displacement",
    )
    fingers = _finite(
        observed_finger_midpoint_displacement_m,
        "observed finger midpoint displacement",
    )
    handle = _finite(observed_handle_displacement_m, "observed handle displacement")
    drawer = _finite(observed_drawer_displacement_m, "observed drawer displacement")
    losses = {
        "controller_tracking_loss_m": commanded - right_gripper,
        "gripper_internal_loss_m": right_gripper - fingers,
        "grasp_relative_slip_m": fingers - handle,
        "handle_drawer_loss_m": handle - drawer,
    }
    reconstructed = drawer + sum(losses.values())
    magnitudes = {name: abs(value) for name, value in losses.items()}
    dominant_name = max(magnitudes, key=magnitudes.get)
    total_magnitude = sum(magnitudes.values())
    dominant_share = (
        magnitudes[dominant_name] / total_magnitude
        if total_magnitude > 1.0e-12
        else 0.0
    )
    classifications = {
        "controller_tracking_loss_m": "EEF_TRACKING_LIMIT",
        "gripper_internal_loss_m": "GRIPPER_INTERNAL_COMPLIANCE",
        "grasp_relative_slip_m": "GRASP_COMPLIANCE_OR_SLIP",
        "handle_drawer_loss_m": "HANDLE_DRAWER_ARTICULATION_RELATION",
    }
    return {
        "commanded_eef_displacement_m": commanded,
        "observed_right_gripper_displacement_m": right_gripper,
        "observed_finger_midpoint_displacement_m": fingers,
        "observed_handle_displacement_m": handle,
        "observed_drawer_displacement_m": drawer,
        **losses,
        "reconstructed_commanded_displacement_m": reconstructed,
        "accounting_residual_m": commanded - reconstructed,
        "dominant_loss_field": dominant_name,
        "dominant_loss_share": dominant_share,
        "primary_loss_source": (
            classifications[dominant_name]
            if dominant_share >= 2.0 / 3.0 and magnitudes[dominant_name] > 1.0e-6
            else "COMBINATION"
        ),
        "traction_efficiency_drawer_per_observed_right_gripper": (
            drawer / right_gripper if abs(right_gripper) > 1.0e-9 else None
        ),
    }


def _branch_continuity_diagnostic(
    candidate: list[float] | tuple[float, ...],
    observed: list[float] | tuple[float, ...],
    previous: list[float] | tuple[float, ...],
    *,
    joint_names: list[str] | tuple[str, ...] | None = None,
    cartesian_target_delta_m: float,
) -> dict[str, Any]:
    from_observed = _joint_delta_metrics(candidate, observed, joint_names)
    from_previous = _joint_delta_metrics(candidate, previous, joint_names)
    threshold = _IK_BRANCH_GUARD_MAX_DQ_RAD
    branch_jump = (
        from_observed["max_abs_delta_rad"] > threshold
        or from_previous["max_abs_delta_rad"] > threshold
    )
    return {
        "cartesian_target_delta_m": _finite(
            cartesian_target_delta_m,
            "Cartesian target delta",
        ),
        "dq_from_observed": from_observed,
        "dq_from_previous": from_previous,
        "branch_guard_max_dq_rad": threshold,
        "branch_jump_detected": branch_jump,
    }


def _projection_interval(
    points: list[tuple[float, float, float]],
    axis: tuple[float, float, float],
) -> tuple[float, float]:
    if not points:
        raise ValueError("collision projection requires at least one point")
    values = [_dot(point, axis) for point in points]
    return (min(values), max(values))


def _intervals_overlap(
    first: tuple[float, float],
    second: tuple[float, float],
) -> bool:
    return first[1] >= second[0] and first[0] <= second[1]


def _clearance_basis(
    opening_axis: tuple[float, float, float],
) -> tuple[tuple[float, float, float], ...]:
    opening = _unit(opening_axis, "clearance opening axis")
    vertical_reference = (0.0, 0.0, 1.0)
    if abs(_dot(opening, vertical_reference)) > 0.95:
        vertical_reference = (0.0, 1.0, 0.0)
    vertical = _orthogonalize(vertical_reference, opening, "clearance vertical axis")
    lateral = _unit(_cross(vertical, opening), "clearance lateral axis")
    return (opening, lateral, vertical)


def _projected_intervals(
    points: list[tuple[float, float, float]],
    basis: tuple[tuple[float, float, float], ...],
) -> tuple[tuple[float, float], ...]:
    return tuple(_projection_interval(points, axis) for axis in basis)


def _projected_overlap(
    first: tuple[tuple[float, float], ...],
    second: tuple[tuple[float, float], ...],
) -> bool:
    return all(_intervals_overlap(a, b) for a, b in zip(first, second, strict=True))


def _required_outward_clearance(
    component_points: Mapping[str, list[tuple[float, float, float]]],
    obstacle_points: list[tuple[float, float, float]],
    opening_axis: tuple[float, float, float],
    buffer_m: float,
) -> dict[str, Any]:
    basis = _clearance_basis(opening_axis)
    obstacle = _projected_intervals(obstacle_points, basis)
    required: dict[str, float] = {}
    component_intervals: dict[str, tuple[tuple[float, float], ...]] = {}
    for name, points in component_points.items():
        intervals = _projected_intervals(points, basis)
        component_intervals[name] = intervals
        orthogonal_overlap = all(
            _intervals_overlap(intervals[index], obstacle[index])
            for index in (1, 2)
        )
        required[name] = (
            max(0.0, obstacle[0][1] + buffer_m - intervals[0][0])
            if orthogonal_overlap
            else 0.0
        )
    shift = max(required.values(), default=0.0)
    return {
        "opening_axis_world": opening_axis,
        "basis_world": basis,
        "buffer_m": buffer_m,
        "obstacle_intervals": obstacle,
        "component_intervals_before": component_intervals,
        "component_required_outward_shift_m": required,
        "required_outward_shift_m": shift,
        "component_intervals_after": {
            name: (
                (intervals[0][0] + shift, intervals[0][1] + shift),
                intervals[1],
                intervals[2],
            )
            for name, intervals in component_intervals.items()
        },
    }


def _orthogonalize(
    value: tuple[float, float, float],
    against: tuple[float, float, float],
    label: str,
) -> tuple[float, float, float]:
    residual = _subtract(value, _scale(against, _dot(value, against)))
    return _unit(residual, label)  # type: ignore[return-value]


def _basis_rotation_quaternion(
    source_closing: tuple[float, float, float],
    source_approach: tuple[float, float, float],
    target_closing: tuple[float, float, float],
    target_approach: tuple[float, float, float],
) -> tuple[float, float, float, float]:
    """Construct WXYZ from two measured source and target axes.

    Each pair is completed to a right-handed orthonormal basis.  The returned
    rotation maps the actual hand closing/approach axes to the selected handle
    surface normal and drawer-opening axes; no quaternion search is involved.
    """

    source_c = _unit(source_closing, "source closing axis")
    source_a = _orthogonalize(source_approach, source_c, "source approach axis")
    source_p = _unit(_cross(source_c, source_a), "source palm axis")
    target_c = _unit(target_closing, "target closing axis")
    target_a = _orthogonalize(target_approach, target_c, "target approach axis")
    target_p = _unit(_cross(target_c, target_a), "target palm axis")

    source = (source_c, source_a, source_p)
    target = (target_c, target_a, target_p)
    rotation = tuple(
        tuple(sum(target[b][row] * source[b][column] for b in range(3)) for column in range(3))
        for row in range(3)
    )
    trace = rotation[0][0] + rotation[1][1] + rotation[2][2]
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        quaternion = (
            0.25 * scale,
            (rotation[2][1] - rotation[1][2]) / scale,
            (rotation[0][2] - rotation[2][0]) / scale,
            (rotation[1][0] - rotation[0][1]) / scale,
        )
    elif rotation[0][0] > rotation[1][1] and rotation[0][0] > rotation[2][2]:
        scale = math.sqrt(1.0 + rotation[0][0] - rotation[1][1] - rotation[2][2]) * 2.0
        quaternion = (
            (rotation[2][1] - rotation[1][2]) / scale,
            0.25 * scale,
            (rotation[0][1] + rotation[1][0]) / scale,
            (rotation[0][2] + rotation[2][0]) / scale,
        )
    elif rotation[1][1] > rotation[2][2]:
        scale = math.sqrt(1.0 + rotation[1][1] - rotation[0][0] - rotation[2][2]) * 2.0
        quaternion = (
            (rotation[0][2] - rotation[2][0]) / scale,
            (rotation[0][1] + rotation[1][0]) / scale,
            0.25 * scale,
            (rotation[1][2] + rotation[2][1]) / scale,
        )
    else:
        scale = math.sqrt(1.0 + rotation[2][2] - rotation[0][0] - rotation[1][1]) * 2.0
        quaternion = (
            (rotation[1][0] - rotation[0][1]) / scale,
            (rotation[0][2] + rotation[2][0]) / scale,
            (rotation[1][2] + rotation[2][1]) / scale,
            0.25 * scale,
        )
    return _unit(quaternion, "grasp orientation")  # type: ignore[return-value]


def _rotate_vector(
    orientation_wxyz: tuple[float, float, float, float],
    value: tuple[float, float, float],
) -> tuple[float, float, float]:
    w, x, y, z = orientation_wxyz
    qv = (x, y, z)
    t = _scale(_cross(qv, value), 2.0)
    return _add(value, _add(_scale(t, w), _cross(qv, t)))  # type: ignore[return-value]


def _axis_angle_quaternion(
    axis: tuple[float, float, float],
    angle_rad: float,
) -> tuple[float, float, float, float]:
    unit_axis = _unit(axis, "axis-angle axis")
    half = angle_rad * 0.5
    sine = math.sin(half)
    return _unit(
        (math.cos(half), unit_axis[0] * sine, unit_axis[1] * sine, unit_axis[2] * sine),
        "axis-angle quaternion",
    )  # type: ignore[return-value]


def _aabb(points: list[tuple[float, float, float]], label: str) -> dict[str, Any]:
    if not points:
        raise ValueError(f"{label} has no geometry points")
    minimum = tuple(min(point[index] for point in points) for index in range(3))
    maximum = tuple(max(point[index] for point in points) for index in range(3))
    center = tuple((minimum[index] + maximum[index]) * 0.5 for index in range(3))
    extents = tuple(maximum[index] - minimum[index] for index in range(3))
    return {"min": minimum, "max": maximum, "center": center, "extents": extents}


def _aabb_corners(bounds: Any) -> list[tuple[float, float, float]]:
    minimum = tuple(float(value) for value in bounds.GetMin())
    maximum = tuple(float(value) for value in bounds.GetMax())
    return [
        (x, y, z)
        for x in (minimum[0], maximum[0])
        for y in (minimum[1], maximum[1])
        for z in (minimum[2], maximum[2])
    ]


def _inverse_transform(matrix: tuple[float, ...], label: str) -> tuple[float, ...]:
    # The flattened USD transform uses the row-vector convention used above.
    a00, a01, a02 = matrix[0], matrix[4], matrix[8]
    a10, a11, a12 = matrix[1], matrix[5], matrix[9]
    a20, a21, a22 = matrix[2], matrix[6], matrix[10]
    determinant = (
        a00 * (a11 * a22 - a12 * a21)
        - a01 * (a10 * a22 - a12 * a20)
        + a02 * (a10 * a21 - a11 * a20)
    )
    if abs(determinant) <= 1.0e-12:
        raise ValueError(f"{label} is not invertible")
    inverse = (
        (a11 * a22 - a12 * a21) / determinant,
        (a02 * a21 - a01 * a22) / determinant,
        (a01 * a12 - a02 * a11) / determinant,
        0.0,
        (a12 * a20 - a10 * a22) / determinant,
        (a00 * a22 - a02 * a20) / determinant,
        (a02 * a10 - a00 * a12) / determinant,
        0.0,
        (a10 * a21 - a11 * a20) / determinant,
        (a01 * a20 - a00 * a21) / determinant,
        (a00 * a11 - a01 * a10) / determinant,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )
    translation = (matrix[12], matrix[13], matrix[14])
    inverse_translation = tuple(
        -sum(inverse[row * 4 + column] * translation[column] for column in range(3))
        for row in range(3)
    )
    return (
        inverse[0], inverse[4], inverse[8], 0.0,
        inverse[1], inverse[5], inverse[9], 0.0,
        inverse[2], inverse[6], inverse[10], 0.0,
        inverse_translation[0], inverse_translation[1], inverse_translation[2], 1.0,
    )


def _matrix_axes(matrix: tuple[float, ...]) -> tuple[tuple[float, float, float], ...]:
    return tuple(
        _transform_direction(
            matrix,
            tuple(1.0 if axis == index else 0.0 for axis in range(3)),
        )
        for index in range(3)
    )


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _vector(value: Any, length: int, label: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"{label} must contain {length} finite values")
    return tuple(_finite(item, f"{label}[{index}]") for index, item in enumerate(value))


def _matrix4(value: Any, label: str) -> tuple[float, ...]:
    matrix = _vector(value, 16, label)
    determinant = (
        matrix[0] * (matrix[5] * matrix[10] - matrix[6] * matrix[9])
        - matrix[1] * (matrix[4] * matrix[10] - matrix[6] * matrix[8])
        + matrix[2] * (matrix[4] * matrix[9] - matrix[5] * matrix[8])
    )
    if abs(determinant) <= 1.0e-9:
        raise ValueError(f"{label} must be invertible")
    return matrix


def _add(first: tuple[float, ...], second: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(a + b for a, b in zip(first, second, strict=True))


def _scale(value: tuple[float, ...], factor: float) -> tuple[float, ...]:
    return tuple(item * factor for item in value)


def _norm(value: tuple[float, ...]) -> float:
    return math.sqrt(sum(item * item for item in value))


def _unit(value: tuple[float, ...], label: str) -> tuple[float, ...]:
    magnitude = _norm(value)
    if magnitude <= 1.0e-12 or not math.isfinite(magnitude):
        raise ValueError(f"{label} must have non-zero finite length")
    return _scale(value, 1.0 / magnitude)


def _transform_point(matrix: tuple[float, ...], point: tuple[float, ...]) -> tuple[float, ...]:
    # Gf.Matrix4d uses row-vector transforms.  Translation is the last row.
    return (
        matrix[0] * point[0] + matrix[4] * point[1] + matrix[8] * point[2] + matrix[12],
        matrix[1] * point[0] + matrix[5] * point[1] + matrix[9] * point[2] + matrix[13],
        matrix[2] * point[0] + matrix[6] * point[1] + matrix[10] * point[2] + matrix[14],
    )


def _transform_direction(matrix: tuple[float, ...], direction: tuple[float, ...]) -> tuple[float, ...]:
    return _unit(
        (
            matrix[0] * direction[0] + matrix[4] * direction[1] + matrix[8] * direction[2],
            matrix[1] * direction[0] + matrix[5] * direction[1] + matrix[9] * direction[2],
            matrix[2] * direction[0] + matrix[6] * direction[1] + matrix[10] * direction[2],
        ),
        "transformed direction",
    )


def _rotation_matrix_from_quaternion(
    orientation: tuple[float, float, float, float],
) -> tuple[float, ...]:
    columns = tuple(
        _rotate_vector(
            orientation,
            tuple(1.0 if axis == index else 0.0 for axis in range(3)),
        )
        for index in range(3)
    )
    return (
        columns[0][0], columns[0][1], columns[0][2], 0.0,
        columns[1][0], columns[1][1], columns[1][2], 0.0,
        columns[2][0], columns[2][1], columns[2][2], 0.0,
        0.0, 0.0, 0.0, 1.0,
    )


def _matrix_to_quaternion(matrix: tuple[float, ...]) -> tuple[float, float, float, float]:
    r00, r01, r02 = matrix[0], matrix[4], matrix[8]
    r10, r11, r12 = matrix[1], matrix[5], matrix[9]
    r20, r21, r22 = matrix[2], matrix[6], matrix[10]
    trace = r00 + r11 + r22
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        result = (
            0.25 * scale,
            (r21 - r12) / scale,
            (r02 - r20) / scale,
            (r10 - r01) / scale,
        )
    elif r00 > r11 and r00 > r22:
        scale = math.sqrt(1.0 + r00 - r11 - r22) * 2.0
        result = (
            (r21 - r12) / scale,
            0.25 * scale,
            (r01 + r10) / scale,
            (r02 + r20) / scale,
        )
    elif r11 > r22:
        scale = math.sqrt(1.0 + r11 - r00 - r22) * 2.0
        result = (
            (r02 - r20) / scale,
            (r01 + r10) / scale,
            0.25 * scale,
            (r12 + r21) / scale,
        )
    else:
        scale = math.sqrt(1.0 + r22 - r00 - r11) * 2.0
        result = (
            (r10 - r01) / scale,
            (r02 + r20) / scale,
            (r12 + r21) / scale,
            0.25 * scale,
        )
    return _unit(result, "rotation quaternion")  # type: ignore[return-value]


def _pose_transform(
    position: tuple[float, float, float],
    orientation: tuple[float, float, float, float],
) -> tuple[float, ...]:
    rotation = _rotation_matrix_from_quaternion(orientation)
    return (
        rotation[0], rotation[1], rotation[2], 0.0,
        rotation[4], rotation[5], rotation[6], 0.0,
        rotation[8], rotation[9], rotation[10], 0.0,
        position[0], position[1], position[2], 1.0,
    )


def _compose_transform(first: tuple[float, ...], second: tuple[float, ...]) -> tuple[float, ...]:
    columns = tuple(
        _transform_direction(
            first,
            (second[index], second[index + 1], second[index + 2]),
        )
        for index in (0, 4, 8)
    )
    translation = _transform_point(
        first,
        (second[12], second[13], second[14]),
    )
    return (
        columns[0][0], columns[0][1], columns[0][2], 0.0,
        columns[1][0], columns[1][1], columns[1][2], 0.0,
        columns[2][0], columns[2][1], columns[2][2], 0.0,
        translation[0], translation[1], translation[2], 1.0,
    )


def _orientation_family_variants(
    geometry_orientation: tuple[float, float, float, float],
    closing_axis: tuple[float, float, float],
    approach_axis: tuple[float, float, float],
    degrees_family: tuple[int, ...] = _ORIENTATION_FAMILY_DEGREES,
) -> list[dict[str, Any]]:
    """Build the bounded wrist-roll family without changing grasp topology."""

    closing = _unit(closing_axis, "orientation-family closing axis")
    source_approach = _orthogonalize(
        approach_axis,
        closing,
        "orientation-family approach axis",
    )
    variants: list[dict[str, Any]] = []
    for degrees in degrees_family:
        delta = _axis_angle_quaternion(closing, math.radians(degrees))
        orientation = _matrix_to_quaternion(
            _compose_transform(
                _pose_transform((0.0, 0.0, 0.0), delta),
                _pose_transform((0.0, 0.0, 0.0), geometry_orientation),
            )
        )
        variants.append(
            {
                "degrees_about_closing_axis": degrees,
                "delta_orientation_wxyz": delta,
                "geometry_orientation_wxyz": orientation,
                "closing_axis_world": _rotate_vector(delta, closing),
                "approach_axis_world": _rotate_vector(delta, source_approach),
            }
        )
    return variants


def _long_horizon_orientation_score(candidate: Mapping[str, Any]) -> tuple[float, ...] | None:
    """Return the P1-4B-W ordering key for a fully evaluated wrist candidate."""

    if candidate.get("short_range_pass") is not True or candidate.get("long_range_pass") is not True:
        return None
    try:
        margin = _finite(
            candidate["minimum_joint_limit_margin_rad"],
            "long-horizon minimum joint-limit margin",
        )
        path_length = _finite(
            candidate["joint_space_path_length_rad"],
            "long-horizon joint-space path length",
        )
        alignment = _finite(
            candidate.get("approach_alignment_to_negative_opening", -1.0),
            "long-horizon approach alignment",
        )
        angle = _finite(candidate["orientation_family_angle_deg"], "orientation-family angle")
    except (KeyError, TypeError, ValueError):
        return None
    return (margin, -path_length, alignment, -abs(angle), angle)


def _rank_long_horizon_orientation_candidates(
    candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    """Rank all viable candidates; short-only and partial-path results are excluded."""

    scored = [
        (score, dict(candidate))
        for candidate in candidates
        if (score := _long_horizon_orientation_score(candidate)) is not None
    ]
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {**candidate, "long_horizon_score": list(score), "rank": index}
        for index, (score, candidate) in enumerate(scored, start=1)
    ]


def _fixture_candidate_validity(
    base_position_m: tuple[float, float, float] | list[float],
    base_yaw_deg: float,
    *,
    initial_robot_cabinet_contact_count: int = 0,
) -> dict[str, Any]:
    """Validate the bounded rigid-pedestal fixture envelope used by P1-4B-F."""

    position = _vector(base_position_m, 3, "fixture robot base position")
    yaw = _finite(base_yaw_deg, "fixture robot base yaw")
    if (
        isinstance(initial_robot_cabinet_contact_count, bool)
        or not isinstance(initial_robot_cabinet_contact_count, int)
        or initial_robot_cabinet_contact_count < 0
    ):
        raise ValueError("initial_robot_cabinet_contact_count must be a non-negative integer")
    offsets = tuple(
        position[index] - _CURRENT_FIXTURE_ROBOT_BASE_POSITION_M[index]
        for index in range(3)
    )
    bounds_valid = all(
        abs(offsets[index]) <= _FIXTURE_BASE_OFFSET_LIMIT_M[index] + 1.0e-12
        for index in range(3)
    ) and abs(yaw) <= _FIXTURE_BASE_YAW_LIMIT_DEG + 1.0e-12
    support_valid = abs(offsets[2]) <= _FIXTURE_BASE_OFFSET_LIMIT_M[2] + 1.0e-12
    contact_free = initial_robot_cabinet_contact_count == 0
    reasons: list[str] = []
    if not bounds_valid:
        reasons.append("candidate exceeds the bounded fixture search envelope")
    if not support_valid:
        reasons.append("candidate requires an unsupported base-height offset")
    if not contact_free:
        reasons.append("robot and cabinet contact in the initial configuration")
    return {
        "valid": bounds_valid and support_valid and contact_free,
        "base_position_m": position,
        "base_yaw_deg": yaw,
        "offset_from_baseline_m": offsets,
        "offset_limits_m": _FIXTURE_BASE_OFFSET_LIMIT_M,
        "yaw_limit_deg": _FIXTURE_BASE_YAW_LIMIT_DEG,
        "within_search_bounds": bounds_valid,
        "support_placement_valid": support_valid,
        "support_model": "rigid adjustable pedestal within 100 mm of baseline height",
        "initial_robot_cabinet_contact_count": initial_robot_cabinet_contact_count,
        "initial_robot_cabinet_contact_free": contact_free,
        "reasons": reasons,
    }


def _fixture_candidate_score(candidate: Mapping[str, Any]) -> tuple[float, ...] | None:
    """Rank only physically valid, approach-valid, 50 mm fixture candidates."""

    if (
        candidate.get("physically_valid") is not True
        or candidate.get("approach_ik_viable") is not True
        or candidate.get("grasp_ik_viable") is not True
        or candidate.get("reach_50mm") is not True
    ):
        return None
    try:
        margin = _finite(candidate["minimum_path_margin_rad"], "fixture path margin")
        path_length = _finite(candidate["joint_space_path_length_rad"], "fixture joint path")
        deviation = _finite(candidate["fixture_deviation_norm"], "fixture deviation")
    except (KeyError, TypeError, ValueError):
        return None
    if margin <= 0.0:
        return None
    return (margin, -path_length, -deviation)


def _rank_fixture_candidates(
    candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    scored = [
        (score, dict(candidate))
        for candidate in candidates
        if (score := _fixture_candidate_score(candidate)) is not None
    ]
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {**candidate, "fixture_score": list(score), "rank": index}
        for index, (score, candidate) in enumerate(scored, start=1)
    ]


def _json_safe(value: Any, label: str = "value") -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return _finite(value, label)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item, f"{label}.{key}") for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, f"{label}[{index}]") for index, item in enumerate(value)]
    if hasattr(value, "tolist") and callable(value.tolist):
        return _json_safe(value.tolist(), label)
    raise ValueError(f"{label} is not JSON serializable")


def _bounded_diagnostics(value: Any, key: str = "value") -> Any:
    """Keep geometry evidence inspectable without duplicating every mesh vertex."""

    if key in {"world_points", "local_points"} and isinstance(value, (list, tuple)):
        return {
            "point_count": len(value),
            "sample": [_bounded_diagnostics(item, "point") for item in value[:128]],
        }
    if isinstance(value, Mapping):
        return {
            str(item_key): _bounded_diagnostics(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_bounded_diagnostics(item, key) for item in value]
    return value


def _pose(raw: Any, label: str) -> tuple[tuple[float, ...], tuple[float, ...]]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{label} must be an object")
    position = _vector(raw.get("position"), 3, f"{label}.position")
    orientation = _vector(raw.get("orientation_wxyz"), 4, f"{label}.orientation_wxyz")
    if _norm(orientation) <= 1.0e-12:
        raise ValueError(f"{label}.orientation_wxyz must have non-zero length")
    return position, orientation


def _contact(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("contact observation must be an object")
    left = raw.get("left_contact") is True
    right = raw.get("right_contact") is True
    force_valid = raw.get("force_valid", raw.get("contact_force_valid")) is True
    nonzero = raw.get("nonzero_force", raw.get("nonzero_contact_force")) is True
    samples = raw.get("force_samples", [])
    if not isinstance(samples, (list, tuple)):
        raise ValueError("contact force samples must be an array")
    normalized_samples = [_json_safe(sample, "force_samples") for sample in samples]
    pairs = raw.get("contact_pairs", [])
    if not isinstance(pairs, (list, tuple)):
        raise ValueError("contact pairs must be an array")
    normalized = {
        "left_contact": left,
        "right_contact": right,
        "force_valid": force_valid,
        "nonzero_force": nonzero,
        "force_samples": normalized_samples,
        "contact_pairs": [_json_safe(pair, "contact_pairs") for pair in pairs],
    }
    for key in (
        "contact_detail_available",
        "surface_pair_valid",
        "normal_dot_left_right",
        "opposed_contact",
        "net_force_on_handle",
        "opening_axis_world",
        "axial_traction",
        "axial_traction_available",
        "force_convention",
    ):
        if key in raw:
            normalized[key] = _json_safe(raw[key], f"contact.{key}")
    return normalized


@dataclass(frozen=True)
class _IsaacInteractionConfig:
    """Simulator-only tuning values; semantic binding values stay elsewhere."""

    headless: bool = True
    physics_dt: float = 1.0 / 60.0
    approach_steps: int = 180
    grasp_motion_steps: int = 180
    grasp_contact_steps: int = 120
    pull_steps_per_waypoint: int = 30
    pull_max_steps: int = 1800
    release_max_steps: int = 80
    waypoint_spacing_m: float = 0.01
    contact_loss_grace_steps: int = 2
    grasp_topology: GraspTopologyConfig = GraspTopologyConfig()
    fixture_cabinet_position_m: tuple[float, float, float] = (0.0, 0.0, 0.78)
    fixture_cabinet_orientation_wxyz: tuple[float, float, float, float] = (
        1.0,
        0.0,
        0.0,
        0.0,
    )
    fixture_robot_base_position_m: tuple[float, float, float] = (
        _CURRENT_FIXTURE_ROBOT_BASE_POSITION_M
    )
    fixture_robot_base_orientation_wxyz: tuple[float, float, float, float] = (
        1.0,
        0.0,
        0.0,
        0.0,
    )
    home_joint_positions: tuple[float, ...] = (
        0.0,
        -0.3,
        0.0,
        -2.0,
        0.0,
        1.7,
        0.8,
        0.04,
        0.04,
    )


def _make_config(
    *,
    headless: bool,
    physics_dt: float,
    action_steps: Mapping[str, int] | None,
    fixture: Mapping[str, Any] | None,
) -> _IsaacInteractionConfig:
    values: dict[str, Any] = {
        "headless": bool(headless),
        "physics_dt": _finite(physics_dt, "physics_dt"),
    }
    if values["physics_dt"] <= 0.0:
        raise ValueError("physics_dt must be positive")
    if action_steps is not None:
        for key, value in action_steps.items():
            if key not in {
                "approach_steps",
                "grasp_motion_steps",
                "grasp_contact_steps",
                "pull_steps_per_waypoint",
                "pull_max_steps",
                "release_max_steps",
            }:
                raise ValueError(f"unsupported action step limit: {key}")
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{key} must be a positive integer")
            values[key] = value
    if fixture is not None:
        allowed = {
            "waypoint_spacing_m",
            "contact_loss_grace_steps",
            "grasp_topology",
            "pregrasp_distance_m",
            "orientation_family_angle_deg",
            "fixture_cabinet_position_m",
            "fixture_cabinet_orientation_wxyz",
            "fixture_robot_base_position_m",
            "fixture_robot_base_orientation_wxyz",
            "home_joint_positions",
        }
        unknown = sorted(set(fixture) - allowed)
        if unknown:
            raise ValueError(f"unsupported fixture config fields: {', '.join(unknown)}")
        values.update(dict(fixture))
    values["waypoint_spacing_m"] = _finite(values.get("waypoint_spacing_m", 0.01), "waypoint_spacing_m")
    if values["waypoint_spacing_m"] <= 0.0:
        raise ValueError("waypoint_spacing_m must be positive")
    grace = values.get("contact_loss_grace_steps", 2)
    if isinstance(grace, bool) or not isinstance(grace, int) or grace < 0:
        raise ValueError("contact_loss_grace_steps must be a non-negative integer")
    values["contact_loss_grace_steps"] = grace
    topology = values.pop("grasp_topology", None)
    pregrasp_distance = values.pop("pregrasp_distance_m", None)
    orientation_angle = values.pop("orientation_family_angle_deg", None)
    if topology is None:
        topology = (
            _IsaacInteractionConfig.grasp_topology.selected_topology
            if pregrasp_distance is not None or orientation_angle is not None
            else _IsaacInteractionConfig.grasp_topology
        )
    if isinstance(topology, GraspTopologyConfig):
        if pregrasp_distance is not None or orientation_angle is not None:
            raise ValueError(
                "pregrasp_distance_m and orientation_family_angle_deg are only valid "
                "with a topology string"
            )
        values["grasp_topology"] = topology
    elif isinstance(topology, str):
        values["grasp_topology"] = GraspTopologyConfig(
            selected_topology=topology,
            pregrasp_distance_m=(
                _IsaacInteractionConfig.grasp_topology.pregrasp_distance_m
                if pregrasp_distance is None
                else pregrasp_distance
            ),
            orientation_family_angle_deg=orientation_angle,
        )
    else:
        raise ValueError("grasp_topology must be a GraspTopologyConfig or topology string")
    values["fixture_cabinet_position_m"] = _vector(
        values.get("fixture_cabinet_position_m", (0.0, 0.0, 0.78)),
        3,
        "fixture_cabinet_position_m",
    )
    values["fixture_cabinet_orientation_wxyz"] = _vector(
        values.get("fixture_cabinet_orientation_wxyz", (1.0, 0.0, 0.0, 0.0)),
        4,
        "fixture_cabinet_orientation_wxyz",
    )
    values["fixture_robot_base_position_m"] = _vector(
        values.get("fixture_robot_base_position_m", (0.7, 0.0, 0.78)),
        3,
        "fixture_robot_base_position_m",
    )
    values["fixture_robot_base_orientation_wxyz"] = _vector(
        values.get("fixture_robot_base_orientation_wxyz", (1.0, 0.0, 0.0, 0.0)),
        4,
        "fixture_robot_base_orientation_wxyz",
    )
    home = values.get("home_joint_positions", _IsaacInteractionConfig.home_joint_positions)
    values["home_joint_positions"] = _vector(home, 9, "home_joint_positions")
    return _IsaacInteractionConfig(**values)


class IsaacInteractionExecutor:
    """Execute the P1-4B approach/grasp/pull/release primitives in Isaac Sim."""

    def __init__(
        self,
        *,
        headless: bool = True,
        binding: IsaacArticulationBinding = SEKTION_TOP_DRAWER_BINDING,
        physics_dt: float = 1.0 / 60.0,
        action_steps: Mapping[str, int] | None = None,
        fixture: Mapping[str, Any] | None = None,
        runtime_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        if not isinstance(binding, IsaacArticulationBinding):
            raise TypeError("binding must be an IsaacArticulationBinding")
        self.binding = binding
        self._config = _make_config(
            headless=headless,
            physics_dt=physics_dt,
            action_steps=action_steps,
            fixture=fixture,
        )
        self._runtime_factory = runtime_factory or _IsaacInteractionRuntime
        self._runtime: Any | None = None
        self._resolution: IsaacArticulationBindingResolution | Mapping[str, Any] | None = None
        self._closed = True
        self._approached_region: tuple[str, str] | None = None
        self._holding: tuple[str, str] | None = None
        self._physics_steps = 0
        self._reset_joint_write_count = 0
        self._execution_joint_write_count = 0
        self._last_snapshot: dict[str, Any] | None = None
        self._active_grasp_plan: dict[str, Any] | None = None

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            executor="isaac_interaction",
            version="1",
            physical=True,
            articulation_execution=True,
            supported_actions=_ISAAC_INTERACTION_ACTIONS,
        )

    def reset(self, scene: Mapping[str, Any], initial_state: InteractionWorldState) -> None:
        if not isinstance(initial_state, InteractionWorldState):
            raise ExecutionError("executor_reset_failed", "initial_state must be an InteractionWorldState")
        if not isinstance(scene, Mapping):
            raise ExecutionError("executor_reset_failed", "scene must be a mapping")
        if initial_state.holding is not None:
            raise ExecutionError("executor_reset_failed", "initial_state must not hold an interaction region")
        positions = initial_state.joint_positions.get(self.binding.semantic_asset_id)
        if not isinstance(positions, Mapping) or self.binding.semantic_joint_id not in positions:
            raise ExecutionError(
                "executor_reset_failed",
                "initial_state is missing the frozen Sektion drawer joint",
            )
        initial_position = _finite(
            positions[self.binding.semantic_joint_id],
            "initial_state drawer position",
        )
        if not self.binding.closed_range[0] - _POSITION_TOLERANCE_M <= initial_position <= self.binding.closed_range[1] + _POSITION_TOLERANCE_M:
            raise ExecutionError("executor_reset_failed", "initial drawer position is not closed")

        self.close()
        runtime = None
        try:
            runtime = self._runtime_factory(self._config)
            runtime.reset(dict(scene), initial_position)
            resolution = runtime.resolve_binding(self.binding)
            if isinstance(resolution, IsaacArticulationBindingResolution):
                resolution.require_valid()
                if resolution.binding_id != self.binding.binding_id:
                    raise ValueError("runtime binding id does not match the frozen binding")
            elif isinstance(resolution, Mapping):
                if resolution.get("valid") is not True:
                    raise ValueError("runtime binding resolution is invalid")
                if resolution.get("binding_id") not in (None, self.binding.binding_id):
                    raise ValueError("runtime binding id does not match the frozen binding")
            else:
                raise ValueError("runtime binding resolution is not JSON-like")
            observed = _finite(runtime.read_joint(), "reset drawer position")
            if not self.binding.closed_range[0] - _POSITION_TOLERANCE_M <= observed <= self.binding.closed_range[1] + _POSITION_TOLERANCE_M:
                raise ValueError(f"reset drawer position is not closed: {observed}")
            contacts = _contact(runtime.read_contacts())
            if contacts["left_contact"] or contacts["right_contact"]:
                raise ValueError("reset unexpectedly has finger-handle contact")
            gripper = runtime.read_gripper()
            if not isinstance(gripper, Mapping) or gripper.get("open") is not True:
                raise ValueError(f"reset gripper is not observed open: {gripper}")
            counters = self._read_counters(runtime)
            self._runtime = runtime
            self._resolution = resolution
            self._closed = False
            self._approached_region = None
            self._holding = None
            self._physics_steps = 0
            self._reset_joint_write_count = counters["reset_joint_write_count"]
            self._execution_joint_write_count = counters["execution_joint_write_count"]
            if self._execution_joint_write_count != 0:
                raise ValueError("runtime reported execution drawer writes during reset")
            self._last_snapshot = None
            self._active_grasp_plan = None
        except Exception:
            # Keep a failed runtime attached until the caller has persisted its
            # exception report.  Isaac Sim can terminate the process from
            # SimulationApp.close(), which would otherwise hide reset errors.
            self._runtime = runtime
            self._resolution = None
            self._closed = True
            raise

    def execute(self, command: ExecutionCommand) -> ExecutionStepResult:
        if self._closed or self._runtime is None:
            raise RuntimeError("Isaac interaction executor is not reset")
        if not isinstance(command, ExecutionCommand):
            raise ExecutionError("invalid_command", "executor command is invalid")
        start = self._physics_steps
        try:
            self._validate_action(command)
            if command.action.action == "approach":
                evidence = self._approach(command, start)
            elif command.action.action == "grasp":
                evidence = self._grasp(command, start)
            elif command.action.action == "pull":
                evidence = self._pull(command, start)
            else:
                evidence = self._release(command, start)
            return ExecutionStepResult(command.command_id, command.step_id, "succeeded", None, evidence)
        except _ActionFailure as failure:
            return ExecutionStepResult(
                command.command_id,
                command.step_id,
                "failed",
                failure.reason,
                self._evidence(command, start, self._physics_steps, failure.evidence),
            )
        except (AttributeError, RuntimeError, TypeError, ValueError, KeyError) as exc:
            return ExecutionStepResult(
                command.command_id,
                command.step_id,
                "failed",
                "physical_runtime_failure",
                self._evidence(
                    command,
                    start,
                    self._physics_steps,
                    {"message": " ".join(str(exc).split())[:500]},
                ),
            )

    def snapshot(self) -> Mapping[str, Any]:
        if self._closed or self._runtime is None:
            raise RuntimeError("Isaac interaction executor is closed or not reset")
        position = _finite(self._runtime.read_joint(), "snapshot drawer position")
        counters = self._read_counters(self._runtime)
        snapshot = {
            "joint_positions": {
                self.binding.semantic_asset_id: {
                    self.binding.semantic_joint_id: position,
                }
            },
            "articulation_positions": {
                self.binding.semantic_asset_id: {
                    self.binding.semantic_joint_id: position,
                }
            },
            "holding": self._holding_ref(),
            "approached_region": self._approached_ref(),
            "physical": True,
            "isaac_version": self._runtime.isaac_sim_version(),
            "binding_id": self.binding.binding_id,
            "runtime_diagnostics": self._runtime.diagnostics(self.binding),
            "reset_joint_write_count": counters["reset_joint_write_count"],
            "execution_joint_write_count": counters["execution_joint_write_count"],
            "physics_steps": self._physics_steps,
        }
        normalized = _json_safe(snapshot, "snapshot")
        self._last_snapshot = normalized
        return normalized

    def close(self) -> None:
        runtime, self._runtime = self._runtime, None
        self._resolution = None
        self._closed = True
        self._approached_region = None
        self._holding = None
        self._physics_steps = 0
        self._last_snapshot = None
        self._active_grasp_plan = None
        if runtime is not None:
            runtime.close()

    def _validate_action(self, command: ExecutionCommand) -> None:
        action = command.action
        if action.action not in _ISAAC_INTERACTION_ACTIONS:
            raise _ActionFailure("unsupported_action", {"action": action.action})
        if action.object_id != self.binding.semantic_asset_id:
            raise _ActionFailure("wrong_object", {"expected_object_id": self.binding.semantic_asset_id})
        if action.region_id != self.binding.semantic_region_id:
            raise _ActionFailure("wrong_region", {"expected_region_id": self.binding.semantic_region_id})
        if action.action == "pull" and action.joint_id != self.binding.semantic_joint_id:
            raise _ActionFailure("wrong_joint", {"expected_joint_id": self.binding.semantic_joint_id})
        if action.action != "pull" and (action.joint_id is not None or action.target_position is not None):
            raise _ActionFailure("malformed_action", {})

    def _approach(self, command: ExecutionCommand, start: int) -> dict[str, Any]:
        plan = self._grasp_plan()
        grasp_position = plan["grasp_position_world"]
        pre_position = plan["pregrasp_position_world"]
        entry_waypoints = [
            tuple(waypoint)
            for waypoint in plan.get("pregrasp_entry_waypoints_world", (pre_position,))
        ]
        approach_motions = [
            self._move_to_pose(
                waypoint,
                self._config.approach_steps,
                plan["orientation_wxyz"],
            )
            for waypoint in entry_waypoints
        ]
        motion = {
            **approach_motions[-1],
            "motion_waypoints": approach_motions,
            "motion_waypoint_count": len(approach_motions),
        }
        self._approached_region = (command.action.object_id, command.action.region_id or "")
        return self._evidence(
            command,
            start,
            self._physics_steps,
            {
                "target_pose": self._target_pose(pre_position),
                "grasp_pose": self._target_pose(grasp_position),
                "grasp_topology": plan["topology"],
                "grasp_candidate_id": plan["candidate_id"],
                "grasp_plan": plan,
                **motion,
                "handle_frame": plan["handle_frame"],
            },
        )

    def _grasp(self, command: ExecutionCommand, start: int) -> dict[str, Any]:
        if self._approached_region != (command.action.object_id, command.action.region_id or ""):
            raise _ActionFailure("grasp_before_approach", {})
        plan = self._grasp_plan()
        grasp_position = plan["grasp_position_world"]
        approach_waypoints = [
            tuple(waypoint)
            for waypoint in plan.get("approach_waypoints_world", (grasp_position,))
        ]
        pregrasp_position = tuple(plan["pregrasp_position_world"])
        if approach_waypoints and _norm(_subtract(approach_waypoints[0], pregrasp_position)) <= 1.0e-9:
            approach_waypoints = approach_waypoints[1:]
        if not approach_waypoints:
            approach_waypoints = [tuple(grasp_position)]
        motion_waypoints = [
            self._move_to_pose(
                waypoint,
                self._config.grasp_motion_steps,
                plan["orientation_wxyz"],
            )
            for waypoint in approach_waypoints
        ]
        motion = {
            **motion_waypoints[-1],
            "motion_waypoints": motion_waypoints,
            "motion_waypoint_count": len(motion_waypoints),
        }
        self._runtime.close_gripper()
        samples: list[dict[str, Any]] = []
        stable_steps = 0
        gripper = self._runtime.read_gripper()
        for _ in range(self._config.grasp_contact_steps):
            self._step()
            contacts = _contact(self._runtime.read_contacts())
            samples.append({"physics_step": self._physics_steps, **contacts})
            if (
                contacts["left_contact"]
                and contacts["right_contact"]
                and contacts["force_valid"]
                and contacts["nonzero_force"]
                and contacts.get("opposed_contact", True) is not False
            ):
                stable_steps += 1
                if stable_steps >= _CONTACT_STABLE_STEPS:
                    gripper = self._runtime.read_gripper()
                    if not isinstance(gripper, Mapping):
                        raise _ActionFailure("gripper_observation_invalid", {})
                    if gripper.get("open") is not False:
                        raise _ActionFailure(
                            "gripper_close_not_observed",
                            {"gripper_positions": gripper.get("positions", [])},
                        )
                    self._holding = (command.action.object_id, command.action.region_id or "")
                    return self._evidence(
                        command,
                        start,
                        self._physics_steps,
                        {
                            **motion,
                            "gripper_positions": gripper.get("positions", []),
                            "left_contact": True,
                            "right_contact": True,
                            "contact_pairs": contacts["contact_pairs"],
                            "contact_force_valid": True,
                            "force_samples": samples,
                            "stable_contact_steps": stable_steps,
                            "holding": self._holding_ref(),
                            "handle_relative_pose": self._handle_relative_pose(grasp_position),
                            "grasp_topology": plan["topology"],
                            "grasp_candidate_id": plan["candidate_id"],
                            "contact_diagnostics": contacts,
                        },
                    )
            else:
                stable_steps = 0
            gripper = self._runtime.read_gripper()
        self._holding = None
        raise _ActionFailure(
            "grasp_contact_gate_failed",
            {
                **motion,
                "gripper_positions": gripper.get("positions", []) if isinstance(gripper, Mapping) else [],
                "left_contact": bool(samples[-1]["left_contact"]) if samples else False,
                "right_contact": bool(samples[-1]["right_contact"]) if samples else False,
                "force_samples": samples,
                "stable_contact_steps": stable_steps,
                "holding": None,
            },
        )

    def _pull(self, command: ExecutionCommand, start: int) -> dict[str, Any]:
        if self._holding != (command.action.object_id, command.action.region_id or ""):
            raise _ActionFailure("pull_before_grasp", {})
        target = _finite(command.action.target_position, "pull target_position")
        before = _finite(self._runtime.read_joint(), "drawer position before pull")
        lower, upper = self._joint_limits()
        if not lower <= target <= upper:
            raise _ActionFailure("target_outside_limits", {"target_position": target})
        if target <= before:
            raise _ActionFailure("pull_target_not_ahead", {"drawer_joint_before": before, "target_position": target})
        requested_delta = target - before
        if abs(requested_delta - _PULL_DISTANCE_M) > 1.0e-6:
            raise _ActionFailure(
                "pull_target_distance_mismatch",
                {"drawer_joint_before": before, "target_position": target, "target_displacement": requested_delta},
            )
        plan = self._grasp_plan()
        axis = _unit(
            _vector(plan["opening_axis_world"], 3, "opening_axis_world"),
            "opening_axis_world",
        )
        eef_start, _ = _pose(self._runtime.read_eef_pose(), "pull eef_start")
        initial_contacts = _contact(self._runtime.read_contacts())
        if not (
            initial_contacts["left_contact"]
            and initial_contacts["right_contact"]
            and initial_contacts["force_valid"]
            and initial_contacts["nonzero_force"]
        ):
            raise _ActionFailure(
                "pull_contact_not_present",
                {"contact_before_pull": initial_contacts},
            )
        ik_path = self._precompute_pull_ik_path(
            eef_start,
            axis,
            _PULL_DISTANCE_M,
            tuple(plan["orientation_wxyz"]),
        )
        if ik_path.get("success") is not True:
            raise _ActionFailure("pull_ik_continuation_failed", ik_path)
        waypoints = list(ik_path["accepted_waypoints"])
        waypoint_count = len(waypoints)
        contact_samples: list[dict[str, Any]] = [
            {"physics_step": self._physics_steps, "phase": "before_pull", **initial_contacts}
        ]
        joint_samples: list[float] = [before]
        arm_joint_samples: list[dict[str, Any]] = []
        waypoints_completed = 0
        contact_loss_steps = 0
        pull_steps = 0
        last_eef = eef_start
        positive_axial_traction_observed = False
        for waypoint in waypoints:
            waypoint_index = int(waypoint["waypoint_id"])
            target_position = tuple(float(value) for value in waypoint["target_position"])
            joint_target = [float(value) for value in waypoint["joint_positions"]]
            self._runtime.apply_arm_targets(joint_target)
            waypoint_converged = False
            for _ in range(self._config.pull_steps_per_waypoint):
                if pull_steps >= self._config.pull_max_steps:
                    raise _ActionFailure(
                        "pull_step_limit_exceeded",
                        {"waypoint_index": waypoint_index, "waypoints_completed": waypoints_completed},
                    )
                self._runtime.apply_arm_targets(joint_target)
                self._step()
                pull_steps += 1
                observed_eef, observed_orientation = _pose(
                    self._runtime.read_eef_pose(),
                    "pull observed_eef",
                )
                last_eef = observed_eef
                position_error = _norm(_subtract(observed_eef, target_position))
                orientation_error = quaternion_angular_distance(
                    observed_orientation,
                    plan["orientation_wxyz"],
                )
                observed_arm = [
                    _finite(value, "pull observed arm joint")
                    for value in self._runtime.read_robot_joint_positions()
                ]
                joint_error = _joint_delta_metrics(
                    joint_target,
                    observed_arm,
                    ik_path["joint_names"],
                )
                arm_joint_samples.append(
                    {
                        "physics_step": self._physics_steps,
                        "waypoint_id": waypoint_index,
                        "target_position": list(target_position),
                        "observed_eef_position": list(observed_eef),
                        "eef_position_error_m": position_error,
                        "eef_orientation_error_rad": orientation_error,
                        "target_joint_positions": joint_target,
                        "observed_joint_positions": observed_arm,
                        "target_error": joint_error,
                    }
                )
                observed_joint = _finite(self._runtime.read_joint(), "pull drawer position")
                joint_samples.append(observed_joint)
                contacts = _contact(self._runtime.read_contacts())
                contact_samples.append(
                    {
                        "physics_step": self._physics_steps,
                        "phase": "pull",
                        "waypoint_id": waypoint_index,
                        **contacts,
                        "drawer_joint": observed_joint,
                    }
                )
                contact_ok = (
                    contacts["left_contact"]
                    and contacts["right_contact"]
                    and contacts["force_valid"]
                    and contacts["nonzero_force"]
                    and contacts.get("opposed_contact", True) is not False
                )
                if contacts.get("axial_traction_available") is True:
                    axial = _finite(
                        contacts.get("axial_traction"),
                        "contact axial traction",
                    )
                    positive_axial_traction_observed |= axial > 0.0
                contact_loss_steps = 0 if contact_ok else contact_loss_steps + 1
                if contact_loss_steps > self._config.contact_loss_grace_steps:
                    raise _ActionFailure(
                        "pull_contact_lost",
                        {
                            "drawer_joint_before": before,
                            "drawer_joint_after": observed_joint,
                            "contact_samples": contact_samples,
                        },
                    )
                if joint_error["max_abs_delta_rad"] <= _PULL_JOINT_CONVERGENCE_RAD:
                    waypoint_converged = True
                    break
            if not waypoint_converged:
                raise _ActionFailure(
                    "pull_joint_convergence_failed",
                    {
                        "waypoint_index": waypoint_index,
                        "waypoints_completed": waypoints_completed,
                        "ik_continuation": ik_path,
                        "arm_joint_samples": arm_joint_samples,
                        "contact_samples": contact_samples,
                    },
                )
            waypoints_completed += 1
        after = _finite(self._runtime.read_joint(), "drawer position after pull")
        delta = after - before
        if delta < 0.0:
            raise _ActionFailure("drawer_joint_moved_wrong_direction", {"drawer_joint_before": before, "drawer_joint_after": after, "drawer_joint_delta": delta})
        if delta <= 1.0e-5:
            raise _ActionFailure("drawer_joint_did_not_move", {"drawer_joint_before": before, "drawer_joint_after": after, "drawer_joint_delta": delta})
        if (
            any(sample.get("axial_traction_available") is True for sample in contact_samples)
            and not positive_axial_traction_observed
        ):
            raise _ActionFailure(
                "non_positive_drawer_axis_traction",
                {
                    "drawer_joint_before": before,
                    "drawer_joint_after": after,
                    "drawer_joint_delta": delta,
                    "opening_axis_world": axis,
                    "contact_samples": contact_samples,
                },
            )
        if not target - _PULL_TOLERANCE_M <= after <= target + _PULL_TOLERANCE_M:
            raise _ActionFailure("pull_target_not_reached", {"drawer_joint_before": before, "drawer_joint_after": after, "target_position": target, "target_tolerance": _PULL_TOLERANCE_M})
        return self._evidence(
            command,
            start,
            self._physics_steps,
            {
                "drawer_joint_before": before,
                "drawer_joint_after": after,
                "drawer_joint_delta": delta,
                "target_position": target,
                "target_tolerance": _PULL_TOLERANCE_M,
                "opening_axis_world": axis,
                "eef_start": eef_start,
                "eef_end": last_eef,
                "waypoint_count": waypoint_count,
                "waypoints_completed": waypoints_completed,
                "ik_continuation": ik_path,
                "arm_joint_samples": arm_joint_samples,
                "contact_maintained": contact_loss_steps <= self._config.contact_loss_grace_steps,
                "contact_samples": contact_samples,
                "execution_joint_write_count": self._execution_joint_write_count,
                "joint_samples": joint_samples,
                "grasp_topology": plan["topology"],
                "grasp_candidate_id": plan["candidate_id"],
                "positive_axial_traction_observed": positive_axial_traction_observed,
            },
        )

    def _release(self, command: ExecutionCommand, start: int) -> dict[str, Any]:
        if self._holding != (command.action.object_id, command.action.region_id or ""):
            raise _ActionFailure("release_before_grasp", {})
        drawer_at_release = _finite(self._runtime.read_joint(), "drawer position at release")
        self._runtime.open_gripper()
        separation_steps = 0
        contact_samples: list[dict[str, Any]] = []
        for _ in range(self._config.release_max_steps):
            self._step()
            contacts = _contact(self._runtime.read_contacts())
            contact_samples.append({"physics_step": self._physics_steps, **contacts})
            if not contacts["left_contact"] and not contacts["right_contact"]:
                separation_steps += 1
            else:
                separation_steps = 0
            if separation_steps >= _CONTACT_STABLE_STEPS:
                break
        if separation_steps < _CONTACT_STABLE_STEPS:
            raise _ActionFailure(
                "release_contact_separation_failed",
                {"contact_separated": False, "separation_stable_steps": separation_steps, "contact_samples": contact_samples},
            )
        while len(contact_samples) < _RELEASE_OBSERVATION_STEPS:
            self._step()
            contacts = _contact(self._runtime.read_contacts())
            contact_samples.append({"physics_step": self._physics_steps, **contacts})
        drawer_after = _finite(self._runtime.read_joint(), "drawer position after release observation")
        gripper = self._runtime.read_gripper()
        if not isinstance(gripper, Mapping) or gripper.get("open") is not True:
            raise _ActionFailure(
                "gripper_open_not_observed",
                {"gripper_positions": gripper.get("positions", []) if isinstance(gripper, Mapping) else []},
            )
        self._holding = None
        return self._evidence(
            command,
            start,
            self._physics_steps,
            {
                "gripper_positions": gripper.get("positions", []) if isinstance(gripper, Mapping) else [],
                "gripper_open": gripper.get("open") is True if isinstance(gripper, Mapping) else False,
                "contact_separated": True,
                "separation_stable_steps": separation_steps,
                "drawer_position_release": drawer_at_release,
                "drawer_position_after_observation": drawer_after,
                "contact_samples": contact_samples,
            },
        )

    def _move_to_pose(
        self,
        target: tuple[float, ...],
        max_steps: int,
        orientation: tuple[float, float, float, float] | None = None,
    ) -> dict[str, Any]:
        orientation = orientation or self._grasp_plan()["orientation_wxyz"]
        solve = self._solve_ik(target, orientation)
        if not solve["ik_success"]:
            raise _ActionFailure("ik_failed", solve)
        self._runtime.apply_arm_targets(solve["joint_positions"])
        observed_position = target
        observed_orientation = orientation
        position_error = math.inf
        orientation_error = math.inf
        converged = False
        motion_start = self._physics_steps
        for _ in range(max_steps):
            # Isaac articulation actions are control-tick commands. Re-submit
            # the bounded position target while the PhysX drive converges.
            self._runtime.apply_arm_targets(solve["joint_positions"])
            self._step()
            observed_position, observed_orientation = _pose(
                self._runtime.read_eef_pose(),
                "observed_eef_pose",
            )
            position_error = _norm(tuple(a - b for a, b in zip(observed_position, target, strict=True)))
            orientation_error = quaternion_angular_distance(observed_orientation, orientation)
            if position_error <= _POSITION_TOLERANCE_M and orientation_error <= _ORIENTATION_TOLERANCE_RAD:
                converged = True
                break
        if not converged:
            raise _ActionFailure(
                "motion_convergence_failed",
                {
                    "target_pose": self._target_pose(target),
                    "observed_eef_pose": self._target_pose(observed_position, observed_orientation),
                    "position_error_m": position_error,
                    "orientation_error_rad": orientation_error,
                    "ik_success": True,
                    "robot_joint_positions": self._runtime.read_robot_joint_positions(),
                    "applied_joint_position_targets": solve["joint_positions"],
                    "physics_step_start": motion_start,
                    "physics_step_end": self._physics_steps,
                },
            )
        return {
            "target_pose": self._target_pose(target),
            "observed_eef_pose": self._target_pose(observed_position, observed_orientation),
            "position_error_m": position_error,
            "orientation_error_rad": orientation_error,
            "ik_success": True,
            "robot_joint_positions": self._runtime.read_robot_joint_positions(),
            "applied_joint_position_targets": solve["joint_positions"],
            "physics_step_start": motion_start,
            "physics_step_end": self._physics_steps,
        }

    def _solve_ik(
        self,
        target: tuple[float, ...],
        orientation: tuple[float, float, float, float] | None = None,
        *,
        warm_start: list[float] | tuple[float, ...] | None = None,
        warm_start_source: str | None = None,
        position_tolerance_m: float = _POSITION_TOLERANCE_M,
    ) -> dict[str, Any]:
        orientation = orientation or self._grasp_plan()["orientation_wxyz"]
        raw = self._runtime.solve_ik(
            target,
            orientation,
            warm_start=warm_start,
            position_tolerance_m=position_tolerance_m,
        )
        if not isinstance(raw, Mapping):
            return {"ik_success": False, "reason": "ik_result_invalid"}
        success = raw.get("success", raw.get("ik_success")) is True
        diagnostic_fields = {
            key: raw[key]
            for key in (
                "target_position",
                "base_position",
                "base_orientation",
                "position_tolerance_m",
                "seed_source",
                "warm_start",
                "continuation_solver_profile",
            )
            if key in raw
        }
        if warm_start is not None:
            diagnostic_fields["continuation_seed_source"] = (
                warm_start_source or "previous_accepted_solution"
            )
        try:
            joints = [_finite(item, "IK joint position") for item in raw.get("joint_positions", [])]
        except (TypeError, ValueError):
            return {"ik_success": False, "reason": "ik_solution_non_finite", **diagnostic_fields}
        if not success or not joints:
            return {
                "ik_success": False,
                "reason": raw.get("reason", "ik_failed"),
                "joint_positions": joints,
                **diagnostic_fields,
            }
        limits = self._runtime.arm_joint_limits()
        if not isinstance(limits, (list, tuple)) or len(limits) != len(joints):
            return {"ik_success": False, "reason": "ik_joint_limits_invalid", "joint_positions": joints}
        names_reader = getattr(self._runtime, "arm_joint_names", None)
        joint_names = (
            [str(value) for value in names_reader()]
            if callable(names_reader)
            else [f"joint_{index + 1}" for index in range(len(joints))]
        )
        try:
            limit_diagnostic = _joint_limit_diagnostic(joints, limits, joint_names)
        except (TypeError, ValueError) as exc:
            return {
                "ik_success": False,
                "reason": "ik_joint_limits_invalid",
                "joint_positions": joints,
                "message": " ".join(str(exc).split())[:500],
                **diagnostic_fields,
            }
        if not limit_diagnostic["all_within_limits"]:
            return {
                "ik_success": False,
                "reason": "ik_solution_outside_limits",
                "joint_positions": joints,
                "joint_limit_diagnostic": limit_diagnostic,
                "limiting_joint": limit_diagnostic["limiting_joint"],
                **diagnostic_fields,
            }
        limit_margins = [
            float(item["distance_to_nearest_limit_rad"])
            for item in limit_diagnostic["joints"]
        ]
        return {
            "ik_success": True,
            "joint_positions": joints,
            "joint_limit_margins_rad": limit_margins,
            "minimum_joint_limit_margin_rad": min(limit_margins),
            "joint_limit_diagnostic": limit_diagnostic,
            "limiting_joint": limit_diagnostic["limiting_joint"],
            **diagnostic_fields,
        }

    def _precompute_pull_ik_path(
        self,
        start_position: tuple[float, ...],
        axis: tuple[float, ...],
        distance_m: float,
        orientation: tuple[float, float, float, float],
        *,
        initial_joint_positions: list[float] | tuple[float, ...] | None = None,
    ) -> dict[str, Any]:
        distance_m = _finite(distance_m, "pull continuation distance")
        if distance_m <= 0.0:
            raise ValueError("pull continuation distance must be positive")
        start = _vector(start_position, 3, "pull continuation start")
        direction = _unit(_vector(axis, 3, "pull continuation axis"), "pull continuation axis")
        end = _add(start, _scale(direction, distance_m))
        pending = _cartesian_waypoints(start, end, _PULL_CONTINUATION_SPACING_M)
        initial_q = [
            _finite(value, "pull initial arm joint")
            for value in (
                self._runtime.read_robot_joint_positions()
                if initial_joint_positions is None
                else initial_joint_positions
            )
        ]
        if len(initial_q) != 7:
            raise ValueError("pull continuation requires seven initial arm joints")
        names_reader = getattr(self._runtime, "arm_joint_names", None)
        joint_names = (
            [str(value) for value in names_reader()]
            if callable(names_reader)
            else [f"panda_joint{index}" for index in range(1, 8)]
        )
        initial_limit_diagnostic = _joint_limit_diagnostic(
            initial_q,
            self._runtime.arm_joint_limits(),
            joint_names,
        )
        previous_position = start
        previous_solution = initial_q
        accepted: list[dict[str, Any]] = []
        subdivisions: list[dict[str, Any]] = []
        attempts: list[dict[str, Any]] = []
        while pending:
            target = pending.pop(0)
            spacing = _norm(_subtract(target, previous_position))
            actual_observed_q = [
                _finite(value, "precompute observed arm joint")
                for value in self._runtime.read_robot_joint_positions()
            ]
            observed_eef_pose = dict(self._runtime.read_eef_pose())
            solve = self._solve_ik(
                target,
                orientation,
                warm_start=previous_solution,
                warm_start_source="previous_accepted_solution",
                position_tolerance_m=_PULL_IK_POSITION_TOLERANCE_M,
            )
            direct_solve = solve
            predictor_fallback: dict[str, Any] = {"attempted": False}
            if solve.get("ik_success") is not True:
                predictor = getattr(self._runtime, "predict_local_ik_seed", None)
                if callable(predictor):
                    prediction = dict(
                        predictor(
                            target,
                            orientation,
                            previous_solution,
                        )
                    )
                    predicted_seed = [
                        _finite(value, "predicted local IK seed")
                        for value in prediction.get("joint_positions", [])
                    ]
                    if len(predicted_seed) != len(previous_solution):
                        raise ValueError(
                            "local IK predictor must return one seed value per arm joint"
                        )
                    solve = self._solve_ik(
                        target,
                        orientation,
                        warm_start=predicted_seed,
                        warm_start_source="jacobian_predicted_local_seed",
                        position_tolerance_m=_PULL_IK_POSITION_TOLERANCE_M,
                    )
                    predictor_fallback = {
                        "attempted": True,
                        "trigger_reason": direct_solve.get("reason", "ik_failed"),
                        "prediction": prediction,
                        "retry_solve": solve,
                    }
            attempt: dict[str, Any] = {
                "target_position": list(target),
                "target_orientation_wxyz": list(orientation),
                "cartesian_progress_m": _dot(_subtract(target, start), direction),
                "cartesian_spacing_m": spacing,
                "physics_step": self._physics_steps,
                "observed_eef_pose": observed_eef_pose,
                "actual_observed_joint_positions": actual_observed_q,
                "continuation_observed_joint_positions": list(previous_solution),
                "previous_accepted_solution": list(previous_solution),
                "seed_joint_positions": list(previous_solution),
                "accepted_seed_source": solve.get("continuation_seed_source"),
                "ik_success": solve.get("ik_success") is True,
                "direct_solve": direct_solve,
                "predictor_fallback": predictor_fallback,
                "solve": solve,
            }
            branch_jump = False
            if solve.get("ik_success") is True:
                candidate = [float(value) for value in solve["joint_positions"]]
                continuity = _branch_continuity_diagnostic(
                    candidate,
                    previous_solution,
                    previous_solution,
                    joint_names=joint_names,
                    cartesian_target_delta_m=spacing,
                )
                branch_jump = continuity["branch_jump_detected"]
                attempt["continuity"] = continuity
                fk_reader = getattr(self._runtime, "compute_arm_fk", None)
                if callable(fk_reader):
                    fk = dict(fk_reader(candidate))
                    fk_position = _vector(fk.get("position"), 3, "pull continuation FK position")
                    fk_orientation = _vector(
                        fk.get("orientation_wxyz"),
                        4,
                        "pull continuation FK orientation",
                    )
                    attempt["forward_kinematics"] = {
                        **fk,
                        "position_error_m": _norm(_subtract(fk_position, target)),
                        "orientation_error_rad": quaternion_angular_distance(
                            fk_orientation,
                            orientation,
                        ),
                    }
            attempts.append(attempt)
            if solve.get("ik_success") is not True or branch_jump:
                half_spacing = spacing / 2.0
                if half_spacing < _PULL_MIN_CONTINUATION_SPACING_M - 1.0e-12:
                    return {
                        "success": False,
                        "reason": (
                            "ik_branch_discontinuity_at_minimum_spacing"
                            if branch_jump
                            else "ik_failed_at_minimum_spacing"
                        ),
                        "initial_joint_positions": initial_q,
                        "initial_joint_limit_diagnostic": initial_limit_diagnostic,
                        "joint_names": joint_names,
                        "accepted_waypoints": accepted,
                        "attempts": attempts,
                        "subdivisions": subdivisions,
                        "continuity_policy": _continuity_policy(),
                    }
                midpoint = tuple(
                    (origin + destination) / 2.0
                    for origin, destination in zip(previous_position, target, strict=True)
                )
                subdivisions.append(
                    {
                        "from_position": list(previous_position),
                        "rejected_target_position": list(target),
                        "midpoint_position": list(midpoint),
                        "rejected_spacing_m": spacing,
                        "subdivided_spacing_m": half_spacing,
                        "reason": (
                            "branch_jump_detected" if branch_jump else solve.get("reason", "ik_failed")
                        ),
                    }
                )
                pending.insert(0, target)
                pending.insert(0, midpoint)
                continue
            accepted_waypoint = {
                "waypoint_id": len(accepted) + 1,
                "target_position": list(target),
                "target_orientation_wxyz": list(orientation),
                "cartesian_progress_m": _dot(_subtract(target, start), direction),
                "joint_positions": list(solve["joint_positions"]),
                "seed_joint_positions": list(previous_solution),
                "accepted_seed_source": solve.get("continuation_seed_source"),
                "cartesian_spacing_m": spacing,
                "continuity": attempt["continuity"],
                "joint_limit_margins_rad": solve["joint_limit_margins_rad"],
                "minimum_joint_limit_margin_rad": solve["minimum_joint_limit_margin_rad"],
                "joint_limit_diagnostic": solve["joint_limit_diagnostic"],
                "limiting_joint": solve["limiting_joint"],
                "forward_kinematics": attempt.get("forward_kinematics"),
            }
            accepted.append(accepted_waypoint)
            previous_position = target
            previous_solution = list(solve["joint_positions"])
        return {
            "success": True,
            "reason": None,
            "initial_joint_positions": initial_q,
            "initial_joint_limit_diagnostic": initial_limit_diagnostic,
            "joint_names": joint_names,
            "target_position": list(end),
            "accepted_waypoints": accepted,
            "attempts": attempts,
            "subdivisions": subdivisions,
            "continuity_policy": _continuity_policy(),
        }

    def _grasp_plan(self) -> dict[str, Any]:
        if self._active_grasp_plan is not None:
            return self._active_grasp_plan
        reader = getattr(self._runtime, "read_grasp_plan", None)
        if callable(reader):
            raw = reader()
        else:
            # The fallback exists only for the dependency-free unit boundary.
            # The Isaac runtime implements read_grasp_plan and cannot use this
            # hand-authored pose.
            frame = self._handle_frame()
            grasp_position = _add(frame["position"], _LEGACY_GRASP_OFFSET_M)
            raw = {
                "candidate_id": "legacy_test_plan",
                "topology": "top_bottom_pinch",
                "orientation_wxyz": _LEGACY_GRASP_ORIENTATION_WXYZ,
                "grasp_position_world": grasp_position,
                "pregrasp_position_world": _add(grasp_position, _LEGACY_PRE_GRASP_OFFSET_M),
                "closing_axis_world": (0.0, 0.0, 1.0),
                "approach_axis_world": (1.0, 0.0, 0.0),
                "opening_axis_world": self._runtime.read_opening_axis_world(),
                "handle_surface_pair": ["negative", "positive"],
                "expected_contact_normals": [[0.0, 0.0, -1.0], [0.0, 0.0, 1.0]],
                "required_gripper_opening_m": 0.0,
                "handle_frame": frame,
            }
        self._active_grasp_plan = self._normalize_grasp_plan(raw)
        return self._active_grasp_plan

    def _normalize_grasp_plan(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise _ActionFailure("grasp_topology_observation_invalid", {})
        topology = raw.get("topology")
        if topology not in _GRASP_TOPOLOGIES:
            raise _ActionFailure("unsupported_grasp_topology", {"topology": topology})
        if topology != self._config.grasp_topology.selected_topology:
            raise _ActionFailure(
                "grasp_topology_policy_mismatch",
                {
                    "configured_topology": self._config.grasp_topology.selected_topology,
                    "observed_topology": topology,
                },
            )
        if topology == "front_face_clamp":
            raise _ActionFailure(
                "front_face_clamp_rejected",
                {"topology": topology, "reason": "front clamp is not an opposed retaining grasp"},
            )
        try:
            orientation = _unit(
                _vector(raw.get("orientation_wxyz"), 4, "grasp orientation"),
                "grasp orientation",
            )
            grasp_position = _vector(raw.get("grasp_position_world"), 3, "grasp_position_world")
            pregrasp_position = _vector(
                raw.get("pregrasp_position_world"),
                3,
                "pregrasp_position_world",
            )
            raw_waypoints = raw.get("approach_waypoints_world")
            if raw_waypoints is None:
                approach_waypoints = (grasp_position,)
            else:
                if not isinstance(raw_waypoints, (list, tuple)) or not raw_waypoints:
                    raise ValueError("approach_waypoints_world must be a non-empty sequence")
                approach_waypoints = tuple(
                    _vector(item, 3, "approach waypoint") for item in raw_waypoints
                )
                if _norm(_subtract(approach_waypoints[0], pregrasp_position)) > 1.0e-9:
                    raise ValueError("first approach waypoint must equal pregrasp_position_world")
                if _norm(_subtract(approach_waypoints[-1], grasp_position)) > 1.0e-9:
                    raise ValueError("last approach waypoint must equal grasp_position_world")
                previous = _vector(
                    raw.get("nominal_pregrasp_position_world", pregrasp_position),
                    3,
                    "nominal_pregrasp_position_world",
                )
                for waypoint in approach_waypoints:
                    if _norm(_subtract(waypoint, previous)) > self._config.waypoint_spacing_m + 1.0e-9:
                        raise ValueError("approach waypoint exceeds configured Cartesian spacing")
                    previous = waypoint
            raw_entry_waypoints = raw.get("pregrasp_entry_waypoints_world")
            if raw_entry_waypoints is None:
                pregrasp_entry_waypoints = (pregrasp_position,)
            else:
                if not isinstance(raw_entry_waypoints, (list, tuple)) or not raw_entry_waypoints:
                    raise ValueError("pregrasp_entry_waypoints_world must be a non-empty sequence")
                pregrasp_entry_waypoints = tuple(
                    _vector(item, 3, "pregrasp entry waypoint")
                    for item in raw_entry_waypoints
                )
                if _norm(_subtract(pregrasp_entry_waypoints[-1], pregrasp_position)) > 1.0e-9:
                    raise ValueError("last pregrasp entry waypoint must equal pregrasp_position_world")
            closing_axis = _unit(
                _vector(raw.get("closing_axis_world"), 3, "closing_axis_world"),
                "closing_axis_world",
            )
            approach_axis = _unit(
                _vector(raw.get("approach_axis_world"), 3, "approach_axis_world"),
                "approach_axis_world",
            )
            opening_axis = _unit(
                _vector(raw.get("opening_axis_world"), 3, "opening_axis_world"),
                "opening_axis_world",
            )
            pair = raw.get("handle_surface_pair")
            if not isinstance(pair, (list, tuple)) or len(pair) != 2 or pair[0] == pair[1]:
                raise ValueError("handle_surface_pair must contain two distinct surfaces")
            normals = raw.get("expected_contact_normals")
            if not isinstance(normals, (list, tuple)) or len(normals) != 2:
                raise ValueError("expected_contact_normals must contain two vectors")
            expected_normals = tuple(
                _unit(_vector(normal, 3, "expected contact normal"), "expected contact normal")
                for normal in normals
            )
            required_opening = _finite(
                raw.get("required_gripper_opening_m"),
                "required_gripper_opening_m",
            )
            if required_opening < 0.0:
                raise ValueError("required_gripper_opening_m must not be negative")
        except (TypeError, ValueError) as exc:
            raise _ActionFailure(
                "grasp_topology_observation_invalid",
                {"message": " ".join(str(exc).split())[:500]},
            ) from exc
        if abs(_dot(closing_axis, approach_axis)) > 1.0e-4:
            raise _ActionFailure(
                "grasp_axes_not_orthogonal",
                {"closing_axis_world": closing_axis, "approach_axis_world": approach_axis},
            )
        normalized = {
            "candidate_id": str(raw.get("candidate_id") or topology),
            "topology": topology,
            "orientation_wxyz": orientation,
            "grasp_position_world": grasp_position,
            "pregrasp_position_world": pregrasp_position,
            "approach_waypoints_world": approach_waypoints,
            "pregrasp_entry_waypoints_world": pregrasp_entry_waypoints,
            "closing_axis_world": closing_axis,
            "approach_axis_world": approach_axis,
            "opening_axis_world": opening_axis,
            "handle_surface_pair": (str(pair[0]), str(pair[1])),
            "expected_contact_normals": expected_normals,
            "required_gripper_opening_m": required_opening,
            "handle_frame": raw.get("handle_frame", {}),
        }
        for key in (
            "geometry_screen",
            "ik_screen",
            "gripper_geometry",
            "handle_geometry",
            "material_audit",
            "orientation_family",
            "frame_transform_geometry_to_controller",
            "endpoint_clearance",
        ):
            if key in raw:
                normalized[key] = _bounded_diagnostics(raw[key], key)
        for key in (
            "geometry_orientation_wxyz",
            "geometry_grasp_position_world",
            "geometry_pregrasp_position_world",
            "target_frame",
            "observation_frame",
            "orientation_family_angle_deg",
            "nominal_pregrasp_position_world",
            "geometry_nominal_pregrasp_position_world",
            "geometry_approach_waypoints_world",
            "geometry_clearance_entry_position_world",
            "endpoint_outward_shift_m",
            "endpoint_clearance_buffer_m",
            "entry_vertical_clearance_m",
            "entry_gripper_vertical_span_m",
            "uncorrected_grasp_position_world",
            "uncorrected_geometry_grasp_position_world",
        ):
            if key in raw:
                normalized[key] = _bounded_diagnostics(raw[key], key)
        return normalized

    def _handle_frame(self) -> dict[str, Any]:
        raw = self._runtime.read_handle_frame()
        if not isinstance(raw, Mapping):
            raise ValueError("handle frame observation must be an object")
        matrix = _matrix4(raw.get("transform"), "handle_frame.transform")
        position = _vector(
            raw.get("position", (matrix[12], matrix[13], matrix[14])),
            3,
            "handle_frame.position",
        )
        return {"position": position, "transform": matrix}

    def _handle_relative_pose(self, target: tuple[float, ...]) -> dict[str, Any]:
        frame = self._handle_frame()
        return {"eef_minus_handle_frame_m": _add(target, _scale(frame["position"], -1.0))}

    def _target_pose(
        self,
        position: tuple[float, ...],
        orientation: tuple[float, ...] | None = None,
    ) -> dict[str, Any]:
        if orientation is None:
            orientation = (
                self._active_grasp_plan["orientation_wxyz"]
                if self._active_grasp_plan is not None
                else _LEGACY_GRASP_ORIENTATION_WXYZ
            )
        pose = {"position": list(position), "orientation_wxyz": list(orientation)}
        if self._active_grasp_plan is not None:
            for key in ("target_frame", "observation_frame"):
                if key in self._active_grasp_plan:
                    pose[key] = self._active_grasp_plan[key]
        return pose

    def _step(self) -> None:
        self._runtime.step_physics()
        self._physics_steps += 1
        counters = self._read_counters(self._runtime)
        self._execution_joint_write_count = counters["execution_joint_write_count"]
        if self._execution_joint_write_count != 0:
            raise _ActionFailure(
                "forbidden_drawer_write",
                {"execution_joint_write_count": self._execution_joint_write_count},
            )

    def _joint_limits(self) -> tuple[float, float]:
        limits = self._runtime.joint_limits()
        if not isinstance(limits, (list, tuple)) or len(limits) != 2:
            raise ValueError("runtime drawer joint limits are invalid")
        lower, upper = (_finite(limits[0], "drawer lower limit"), _finite(limits[1], "drawer upper limit"))
        if lower >= upper:
            raise ValueError("runtime drawer joint limits are not ordered")
        if abs(lower - self.binding.expected_lower_limit) > 1.0e-4 or abs(upper - self.binding.expected_upper_limit) > 1.0e-4:
            raise ValueError("runtime drawer joint limits differ from the frozen binding")
        return lower, upper

    def _read_counters(self, runtime: Any) -> dict[str, int]:
        raw = runtime.write_counters()
        if not isinstance(raw, Mapping):
            raise ValueError("runtime write counters must be an object")
        counters = {}
        for name in ("reset_joint_write_count", "execution_joint_write_count"):
            value = raw.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"runtime {name} is invalid")
            counters[name] = value
        return counters

    def _evidence(self, command: ExecutionCommand, start: int, end: int, extra: Mapping[str, Any]) -> dict[str, Any]:
        counters = self._read_counters(self._runtime)
        self._execution_joint_write_count = counters["execution_joint_write_count"]
        common = {
            "physical": True,
            "isaac_version": self._runtime.isaac_sim_version(),
            "binding_id": self.binding.binding_id,
            "asset_relative_path": self.binding.asset_relative_path,
            "object_id": command.action.object_id,
            "region_id": command.action.region_id,
            "physics_step_start": start,
            "physics_step_end": end,
            "execution_joint_write_count": self._execution_joint_write_count,
        }
        return _json_safe({**common, **dict(extra)}, "step evidence")

    def _joint_ref(self, value: float) -> dict[str, dict[str, float]]:
        return {self.binding.semantic_asset_id: {self.binding.semantic_joint_id: value}}

    def _holding_ref(self) -> dict[str, str] | None:
        if self._holding is None:
            return None
        return {"object_id": self._holding[0], "region_id": self._holding[1]}

    def _approached_ref(self) -> dict[str, str] | None:
        if self._approached_region is None:
            return None
        return {"object_id": self._approached_region[0], "region_id": self._approached_region[1]}


class _ActionFailure(RuntimeError):
    def __init__(self, reason: str, evidence: Mapping[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.evidence = dict(evidence)


class _IsaacInteractionRuntime:
    """Isaac-only implementation of the narrow runtime used by the executor."""

    def __init__(self, config: _IsaacInteractionConfig) -> None:
        self.config = config
        self._app = None
        self._simulation = None
        self._stage = None
        self._cabinet = None
        self._robot = None
        self._gripper = None
        self._kinematics = None
        self._resolution = None
        self._asset_source = None
        self._robot_asset_source = None
        self._finger_gripper_config: dict[str, Any] = {}
        self._finger_root_paths: tuple[str, str] = ()
        self._hand_root_path: str | None = None
        self._eef_frame_path: str | None = None
        self._eef_kinematics_frame: str | None = None
        self._observation_frame: str | None = None
        self._lula_frame_names: tuple[str, ...] = ()
        self._geometry_to_controller_transform: tuple[float, ...] | None = None
        self._frame_transform_diagnostics: dict[str, Any] = {}
        self._lula_base_position: tuple[float, ...] | None = None
        self._lula_base_orientation: tuple[float, ...] | None = None
        self._contact_views: list[tuple[str, Any]] = []
        self._handle_contact_view = None
        self._contact_subscription = None
        self._contact_interface = None
        self._physics_steps = 0
        self._reset_joint_write_count = 0
        self._execution_joint_write_count = 0
        self._asset_diagnostics = _empty_asset_root_diagnostics()
        self._pre_robot_handle_frame_position: tuple[float, ...] | None = None
        self._post_robot_handle_frame_position: tuple[float, ...] | None = None
        self._handle_geometry: dict[str, Any] | None = None
        self._gripper_geometry: dict[str, Any] | None = None
        self._grasp_analysis: dict[str, Any] | None = None
        self._material_audit: dict[str, Any] | None = None

    def reset(self, scene: Mapping[str, Any], initial_position: float) -> None:
        del scene
        self.close()
        binding = SEKTION_TOP_DRAWER_BINDING
        asset_root = os.environ.get("ISAACSIM_ASSET_ROOT")
        self._asset_source = binding.resolve_asset_path(asset_root)
        if sys.platform == "win32" and not str(self._asset_source).isascii():
            raise RuntimeError("Isaac Sim requires an ASCII-only cabinet asset path on Windows")
        os.environ.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
        original_argv = sys.argv
        sys.argv = [sys.argv[0]]
        try:
            SimulationApp = _load_simulation_app()
            self._app = SimulationApp(
                {
                    "headless": self.config.headless,
                    "hide_ui": self.config.headless,
                    "renderer": "Minimal",
                    "minimal_shading_mode": 4,
                    "anti_aliasing": 0,
                    "multi_gpu": False,
                    "max_gpu_count": 1,
                    "width": 640,
                    "height": 480,
                    "disable_viewport_updates": self.config.headless,
                    "fast_shutdown": True,
                    "extra_args": [
                        "--/app/renderer/skipWhileMinimized=true",
                        "--/rtx-transient/resourcemanager/texturestreaming/enabled=false",
                        "--/isaac/startup/create_new_stage=false",
                    ],
                }
            )
        finally:
            sys.argv = original_argv

        try:
            import numpy as np
            import omni.usd
            from isaacsim.core.api import SimulationContext
            from isaacsim.core.prims import SingleArticulation
            from isaacsim.core.utils.stage import add_reference_to_stage
            from isaacsim.core.utils.types import ArticulationAction
            from isaacsim.robot.manipulators.grippers.parallel_gripper import ParallelGripper
            from isaacsim.robot_motion.motion_generation import (
                ArticulationKinematicsSolver,
                LulaKinematicsSolver,
                load_supported_lula_kinematics_solver_config,
            )
            from isaacsim.storage.native import get_assets_root_path
            from pxr import Gf, UsdGeom, UsdPhysics, UsdShade
        except (ImportError, ModuleNotFoundError) as exc:
            raise RuntimeError("Isaac Sim interaction APIs are unavailable") from exc

        context = omni.usd.get_context()
        context.new_stage()
        for _ in range(5):
            self._app.update()
        stage = context.get_stage()
        if stage is None:
            raise RuntimeError("Isaac returned no stage after creating a clean stage")
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
        UsdGeom.SetStageMetersPerUnit(stage, 1.0)
        physics_scene = UsdPhysics.Scene.Define(stage, "/World/PhysicsScene")
        physics_scene.CreateGravityDirectionAttr().Set(Gf.Vec3f(0.0, 0.0, -1.0))
        physics_scene.CreateGravityMagnitudeAttr().Set(9.81)
        cabinet_prim = stage.DefinePrim(SEKTION_TOP_DRAWER_RUNTIME_ROOT, "Xform")
        cabinet_transform = Gf.Matrix4d(1.0)
        cabinet_transform.SetRotateOnly(
            Gf.Quatd(
                float(self.config.fixture_cabinet_orientation_wxyz[0]),
                Gf.Vec3d(*self.config.fixture_cabinet_orientation_wxyz[1:]),
            )
        )
        cabinet_transform.SetTranslateOnly(Gf.Vec3d(*self.config.fixture_cabinet_position_m))
        UsdGeom.Xformable(cabinet_prim).AddTransformOp().Set(cabinet_transform)
        cabinet_prim.GetReferences().AddReference(self._asset_source.as_posix())
        stage.Load()
        for _ in range(10):
            self._app.update()
        if not cabinet_prim.IsValid():
            raise RuntimeError("failed to define the official Sektion Cabinet reference")
        self._stage = stage
        self._pre_robot_handle_frame_position = tuple(self.read_handle_frame()["position"])
        self._cabinet = SingleArticulation(SEKTION_TOP_DRAWER_RUNTIME_ROOT)
        self._asset_diagnostics = _empty_asset_root_diagnostics()
        robot_usd, self._robot_asset_source = _resolve_franka_usd(
            get_assets_root_path,
            Path(tempfile.gettempdir()) / "scene_factory_isaac_interaction",
            self._asset_diagnostics,
        )
        robot_root = "/World/Franka"
        add_reference_to_stage(usd_path=robot_usd, prim_path=robot_root)
        stage.Load(robot_root)
        self._app.update()
        self._finger_root_paths, self._hand_root_path = _resolve_franka_link_paths(stage, robot_root)
        self._eef_frame_path = self._resolve_eef_frame_path(
            stage,
            robot_root,
            _franka_kinematics_frame(self._robot_asset_source),
        )
        eef_prim = stage.GetPrimAtPath(self._eef_frame_path)
        if not eef_prim.IsValid():
            raise RuntimeError(f"resolved Franka EEF prim is unavailable: {self._eef_frame_path}")
        self._observation_frame = str(eef_prim.GetName())
        self._eef_kinematics_frame = _franka_kinematics_frame(self._robot_asset_source)
        if self._robot_asset_source == "isaacsim_bundled_franka_urdf":
            _configure_bundled_franka_drives(stage, robot_root, UsdPhysics)
        IsaacSimBackend._configure_gripper_material(
            stage,
            UsdPhysics,
            UsdShade,
            self._finger_root_paths,
        )
        self._post_robot_handle_frame_position = tuple(self.read_handle_frame()["position"])
        self._robot = SingleArticulation(
            robot_root,
            name="franka_interaction",
            position=np.asarray(self.config.fixture_robot_base_position_m, dtype=float),
            orientation=np.asarray(self.config.fixture_robot_base_orientation_wxyz, dtype=float),
        )
        self._simulation = SimulationContext(
            physics_dt=self.config.physics_dt,
            rendering_dt=0.0,
            stage_units_in_meters=1.0,
            physics_prim_path="/World/PhysicsScene",
            stage=stage,
        )
        physics_context = self._simulation.get_physics_context()
        physics_context.set_physx_update_transformations_settings(
            update_to_usd=True,
            update_velocities_to_usd=True,
        )
        self._simulation.initialize_physics()
        self._cabinet.initialize()
        self._robot.initialize()
        self._finger_gripper_config = _resolve_finger_gripper_config(self._robot)
        home = np.asarray(self.config.home_joint_positions, dtype=float)
        self._robot.set_world_pose(
            position=np.asarray(self.config.fixture_robot_base_position_m, dtype=float),
            orientation=np.asarray(self.config.fixture_robot_base_orientation_wxyz, dtype=float),
        )
        self._robot.set_joint_positions(home)
        self._robot.apply_action(ArticulationAction(joint_positions=home))
        self._gripper = ParallelGripper(
            end_effector_prim_path=self._hand_root_path,
            joint_prim_names=["panda_finger_joint1", "panda_finger_joint2"],
            joint_opened_positions=np.asarray(self._finger_gripper_config["open_positions"], dtype=float),
            joint_closed_positions=np.asarray(self._finger_gripper_config["closed_positions"], dtype=float),
            action_deltas=np.asarray(self._finger_gripper_config["action_deltas"], dtype=float),
        )
        self._gripper.initialize(
            articulation_apply_action_func=self._robot.apply_action,
            get_joint_positions_func=self._robot.get_joint_positions,
            set_joint_positions_func=self._robot.set_joint_positions,
            dof_names=self._robot.dof_names,
        )
        lula_config = load_supported_lula_kinematics_solver_config("Franka")
        if not lula_config:
            raise RuntimeError("Isaac Sim has no Lula Franka configuration")
        lula = LulaKinematicsSolver(**lula_config)
        self._lula_frame_names = tuple(str(name) for name in lula.get_all_frame_names())
        for frame_name in (self._eef_kinematics_frame, self._observation_frame):
            if frame_name not in self._lula_frame_names:
                raise RuntimeError(
                    f"required Lula frame {frame_name!r} is unavailable: "
                    f"{list(self._lula_frame_names)}"
                )
        self._kinematics = ArticulationKinematicsSolver(
            self._robot,
            lula,
            self._eef_kinematics_frame,
        )
        self._simulation.play()
        self._app.update()
        _configure_franka_runtime_drives(self._robot, self._finger_gripper_config["indices"])
        self._robot.set_world_pose(
            position=np.asarray(self.config.fixture_robot_base_position_m, dtype=float),
            orientation=np.asarray(self.config.fixture_robot_base_orientation_wxyz, dtype=float),
        )
        self._robot.set_joint_positions(home)
        self._robot.apply_action(ArticulationAction(joint_positions=home))
        self._sync_kinematics_base_pose()
        self._initialize_frame_reconciliation()
        self._apply_gripper(True)
        self._initialize_contact_reporting(stage)
        self.reset_articulation_to_initial_state(initial_position, ArticulationAction)
        self._settle(60)
        self._sync_kinematics_base_pose()
        if not self.binding_position_is_closed():
            observed = self.read_joint()
            velocities = self._cabinet.get_joint_velocities()
            raise RuntimeError(
                "drawer did not settle in the closed range: "
                f"position={observed}, velocities={velocities.tolist()}"
            )
        self._resolution = None

    def resolve_binding(self, binding: IsaacArticulationBinding) -> IsaacArticulationBindingResolution:
        if self._stage is None or self._cabinet is None:
            raise RuntimeError("runtime is not initialized")
        from .isaac_binding import IsaacUsdBindingInspector

        inspector = IsaacUsdBindingInspector(
            stage=self._stage,
            runtime_asset_root_prim=SEKTION_TOP_DRAWER_RUNTIME_ROOT,
            runtime_articulation=self._cabinet,
        )

        class _AuthoredDefaultInspector:
            def inspect(self, candidate: IsaacArticulationBinding, asset_source: Path) -> Mapping[str, Any]:
                observation = dict(inspector.inspect(candidate, asset_source))
                joint_path = candidate.runtime_paths(SEKTION_TOP_DRAWER_RUNTIME_ROOT)["joint_prim"]
                joint = self_stage.GetPrimAtPath(joint_path)
                attribute = joint.GetAttribute("physics:jointPosition")
                if attribute and attribute.IsValid():
                    authored = attribute.Get()
                    if authored is not None:
                        observation["runtime_default_position"] = float(authored)
                return observation

        self_stage = self._stage
        resolution = resolve_isaac_articulation_binding(
            binding,
            asset_root=os.environ.get("ISAACSIM_ASSET_ROOT"),
            runtime_asset_root_prim=SEKTION_TOP_DRAWER_RUNTIME_ROOT,
            inspector=_AuthoredDefaultInspector(),
        )
        if not resolution.valid:
            details = "; ".join(
                f"{issue.code} field={issue.field} expected={issue.expected} observed={issue.observed}"
                for issue in resolution.errors
            )
            raise ValueError(f"runtime binding resolution failed: {details}")
        resolution.require_valid()
        self._resolution = resolution
        return resolution

    def reset_articulation_to_initial_state(self, position: float, ArticulationAction: Any) -> None:
        if self._cabinet is None:
            raise RuntimeError("cabinet articulation is not initialized")
        import numpy as np

        names = [str(name) for name in self._cabinet.dof_names]
        index = names.index(SEKTION_TOP_DRAWER_BINDING.joint_name)
        positions = np.asarray(self._cabinet.get_joint_positions(), dtype=float)
        positions[index] = float(position)
        self._cabinet.set_joint_positions(positions)
        self._cabinet.apply_action(
            ArticulationAction(
                joint_positions=np.asarray([float(position)], dtype=float),
                joint_indices=np.asarray([index], dtype=int),
            )
        )
        self._reset_joint_write_count += 1

    def binding_position_is_closed(self) -> bool:
        value = self.read_joint()
        lower, upper = SEKTION_TOP_DRAWER_BINDING.closed_range
        return lower - _POSITION_TOLERANCE_M <= value <= upper + _POSITION_TOLERANCE_M

    def binding_closed_range(self) -> tuple[float, float]:
        return SEKTION_TOP_DRAWER_BINDING.closed_range

    def read_joint(self) -> float:
        if self._cabinet is None:
            raise RuntimeError("cabinet articulation is not initialized")
        import numpy as np

        names = [str(name) for name in self._cabinet.dof_names]
        index = names.index(SEKTION_TOP_DRAWER_BINDING.joint_name)
        values = np.asarray(self._cabinet.get_joint_positions(), dtype=float)
        return _finite(values[index], "runtime drawer position")

    def joint_limits(self) -> tuple[float, float]:
        if self._cabinet is None:
            raise RuntimeError("cabinet articulation is not initialized")
        import numpy as np

        names = [str(name) for name in self._cabinet.dof_names]
        index = names.index(SEKTION_TOP_DRAWER_BINDING.joint_name)
        limits = np.asarray(_read_runtime_dof_limits(self._cabinet), dtype=float)
        return (_finite(limits[index][0], "runtime drawer lower limit"), _finite(limits[index][1], "runtime drawer upper limit"))

    def read_handle_frame(self) -> Mapping[str, Any]:
        from pxr import Usd, UsdGeom

        if self._stage is None or self._resolution is None:
            path = self._resolution.handle_frame_prim if self._resolution else None
        else:
            path = self._resolution.handle_frame_prim
        if not path:
            path = SEKTION_TOP_DRAWER_RUNTIME_ROOT + "/drawer_handle_top/drawer_handle_frame"
        prim = self._stage.GetPrimAtPath(path)
        if not prim.IsValid():
            raise RuntimeError(f"handle frame prim is unavailable: {path}")
        matrix = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        translation = matrix.ExtractTranslation()
        flattened = tuple(float(matrix[row][column]) for row in range(4) for column in range(4))
        return {
            "position": [float(translation[index]) for index in range(3)],
            "transform": flattened,
        }

    def read_opening_axis_world(self) -> tuple[float, float, float]:
        from pxr import Usd, UsdGeom

        parent_path = (
            self._resolution.parent_link_prim
            if self._resolution is not None
            else SEKTION_TOP_DRAWER_RUNTIME_ROOT + "/sektion"
        )
        prim = self._stage.GetPrimAtPath(parent_path)
        matrix = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        direction = matrix.TransformDir((1.0, 0.0, 0.0))
        return _unit(
            tuple(float(direction[index]) for index in range(3)),
            "opening_axis_world",
        )

    def read_grasp_plan(self) -> Mapping[str, Any]:
        """Return the selected geometry-derived grasp plan for this reset."""

        analysis = self._ensure_grasp_analysis()
        selected = analysis.get("selected_candidate")
        if not isinstance(selected, Mapping):
            raise RuntimeError(
                "no physically feasible opposed grasp topology passed geometry and IK screening"
            )
        return selected

    def _ensure_grasp_analysis(self) -> dict[str, Any]:
        if self._grasp_analysis is not None:
            return self._grasp_analysis
        if self._stage is None or self._resolution is None:
            raise RuntimeError("binding must be resolved before grasp analysis")
        handle_path = self._resolution.handle_link_prim or (
            SEKTION_TOP_DRAWER_RUNTIME_ROOT + "/drawer_handle_top"
        )
        handle_frame = self._read_prim_frame(handle_path)
        handle_geometry = self._read_collision_geometry(
            handle_path,
            handle_frame["transform"],
            "handle",
        )
        drawer_geometry = None
        drawer_path = self._resolution.child_link_prim
        if drawer_path:
            try:
                drawer_frame = self._read_prim_frame(drawer_path)
                drawer_geometry = self._read_collision_geometry(
                    drawer_path,
                    drawer_frame["transform"],
                    "drawer body",
                )
            except (AttributeError, RuntimeError, TypeError, ValueError):
                drawer_geometry = None
        opening_axis = self.read_opening_axis_world()
        gripper_geometry = self._read_gripper_geometry()
        candidates = self._build_grasp_candidates(
            handle_geometry,
            handle_frame,
            gripper_geometry,
            opening_axis,
            drawer_geometry,
        )
        for index, candidate in enumerate(candidates):
            if candidate["topology"] == "top_bottom_pinch":
                candidate, family = self._select_orientation_family(candidate, opening_axis)
                candidate["orientation_family"] = family
                candidate = self._recover_top_bottom_endpoint(candidate, opening_axis)
                candidates[index] = candidate
            if candidate["topology"] != "cage_hook":
                candidate["ik_screen"] = self._screen_candidate_ik(candidate, opening_axis)
            else:
                candidate["ik_screen"] = {
                    "valid": False,
                    "reason": "geometry_screen_failed",
                    "probes": {},
                }
        selected = next(
            (
                candidate
                for candidate in candidates
                if candidate["geometry_screen"]["valid"]
                and candidate["ik_screen"]["valid"]
                and candidate["topology"] == self.config.grasp_topology.selected_topology
            ),
            None,
        )
        self._handle_geometry = handle_geometry
        self._gripper_geometry = gripper_geometry
        self._grasp_analysis = {
            "selected_candidate": selected,
            "candidates": candidates,
            "handle_geometry": handle_geometry,
            "drawer_geometry": drawer_geometry,
            "gripper_geometry": gripper_geometry,
            "opening_axis_world": opening_axis,
            "selection_policy": "geometry_validity_then_ik_reachability",
            "configured_topology": self.config.grasp_topology.selected_topology,
        }
        return self._grasp_analysis

    def _read_prim_frame(self, path: str) -> dict[str, Any]:
        from pxr import Usd, UsdGeom

        if self._stage is None:
            raise RuntimeError("USD stage is not initialized")
        prim = self._stage.GetPrimAtPath(path)
        if not prim.IsValid():
            raise RuntimeError(f"required grasp frame prim is unavailable: {path}")
        matrix = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        flattened = tuple(float(matrix[row][column]) for row in range(4) for column in range(4))
        translation = matrix.ExtractTranslation()
        return {
            "prim_path": path,
            "position": tuple(float(translation[index]) for index in range(3)),
            "transform": flattened,
            "axes_world": _matrix_axes(flattened),
        }

    def _collision_paths_for_root(self, root_path: str) -> tuple[list[str], list[str]]:
        from pxr import Usd, UsdGeom, UsdPhysics
        from .isaac_binding import _handle_collision_primitives, _has_schema

        if self._stage is None:
            raise RuntimeError("USD stage is not initialized")
        root = self._stage.GetPrimAtPath(root_path)
        if not root.IsValid():
            raise RuntimeError(f"geometry root is unavailable: {root_path}")
        paths: set[str] = set()
        apis: set[str] = set()
        try:
            paths, apis = _handle_collision_primitives(
                self._stage,
                root,
                Usd,
                UsdGeom,
                UsdPhysics,
            )
        except (AttributeError, RuntimeError, TypeError):
            paths, apis = set(), set()
        traverse = getattr(self._stage, "TraverseAll", self._stage.Traverse)
        prefix = root_path.rstrip("/") + "/"
        descendants = [
            prim
            for prim in traverse()
            if str(prim.GetPath()) == root_path or str(prim.GetPath()).startswith(prefix)
        ]
        if not paths:
            paths = {
                str(prim.GetPath())
                for prim in descendants
                if prim.IsA(UsdGeom.Gprim)
            }
        else:
            paths.update(
                str(prim.GetPath())
                for prim in descendants
                if prim.IsA(UsdGeom.Gprim) and _has_schema(prim, "PhysicsCollisionAPI", UsdPhysics.CollisionAPI)
            )
        return sorted(paths), sorted(apis)

    def _read_collision_geometry(
        self,
        root_path: str,
        reference_transform: tuple[float, ...],
        label: str,
    ) -> dict[str, Any]:
        from pxr import Usd, UsdGeom

        if self._stage is None:
            raise RuntimeError("USD stage is not initialized")
        paths, collision_apis = self._collision_paths_for_root(root_path)
        inverse_reference = _inverse_transform(reference_transform, f"{label} frame")
        bbox_cache = UsdGeom.BBoxCache(
            Usd.TimeCode.Default(),
            [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
            useExtentsHint=True,
        )
        world_points: list[tuple[float, float, float]] = []
        primitive_diagnostics: list[dict[str, Any]] = []
        root_prefix = root_path.rstrip("/") + "/"
        for path in paths:
            if path != root_path and not path.startswith(root_prefix):
                # Prototype-local geometry has no instance transform.  Do not
                # mistake prototype coordinates for world coordinates.
                continue
            prim = self._stage.GetPrimAtPath(path)
            if not prim.IsValid():
                continue
            primitive_points: list[tuple[float, float, float]] = []
            source = "world_bound_corners"
            try:
                if prim.IsA(UsdGeom.Mesh):
                    points = UsdGeom.Mesh(prim).GetPointsAttr().Get()
                    if points:
                        matrix = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(
                            Usd.TimeCode.Default()
                        )
                        transform = tuple(
                            float(matrix[row][column]) for row in range(4) for column in range(4)
                        )
                        primitive_points = [
                            _transform_point(
                                transform,
                                tuple(float(value) for value in point),
                            )
                            for point in points
                        ]
                        source = "mesh_points"
                if not primitive_points:
                    bounds = bbox_cache.ComputeWorldBound(prim).ComputeAlignedRange()
                    if not bounds.IsEmpty():
                        primitive_points = [
                            tuple(float(value) for value in point)
                            for point in _aabb_corners(bounds)
                        ]
            except (AttributeError, RuntimeError, TypeError, ValueError):
                primitive_points = []
            if not primitive_points:
                continue
            local_points = [_transform_point(inverse_reference, point) for point in primitive_points]
            world_points.extend(primitive_points)
            primitive_diagnostics.append(
                {
                    "path": path,
                    "type": str(prim.GetTypeName()),
                    "source": source,
                    "point_count": len(local_points),
                    "local_bounds": _aabb(local_points, f"{label} primitive"),
                }
            )
        if not world_points:
            root = self._stage.GetPrimAtPath(root_path)
            bounds = bbox_cache.ComputeWorldBound(root).ComputeAlignedRange()
            if bounds.IsEmpty():
                raise RuntimeError(f"{label} collision geometry has no readable points")
            world_points = _aabb_corners(bounds)
            primitive_diagnostics.append(
                {
                    "path": root_path,
                    "type": str(root.GetTypeName()),
                    "source": "root_world_bound_conservative",
                    "point_count": len(world_points),
                    "local_bounds": _aabb(
                        [_transform_point(inverse_reference, point) for point in world_points],
                        f"{label} conservative root bound",
                    ),
                }
            )
        local_points = [_transform_point(inverse_reference, point) for point in world_points]
        return {
            "root_path": root_path,
            "collision_apis": collision_apis,
            "collision_prim_paths": [item["path"] for item in primitive_diagnostics],
            "world_points": world_points,
            "local_points": local_points,
            "world_aabb": _aabb(world_points, f"{label} world geometry"),
            "local_aabb": _aabb(local_points, f"{label} local geometry"),
            "principal_frame": self._principal_frame(local_points, f"{label} principal frame"),
            "primitives": primitive_diagnostics,
        }

    @staticmethod
    def _principal_frame(
        points: list[tuple[float, float, float]],
        label: str,
    ) -> dict[str, Any]:
        import numpy as np

        values = np.asarray(points, dtype=float)
        center = values.mean(axis=0)
        if len(values) >= 3:
            covariance = np.cov(values - center, rowvar=False, bias=True)
            eigenvalues, eigenvectors = np.linalg.eigh(covariance)
            order = np.argsort(eigenvalues)[::-1]
            axes = [
                _unit(tuple(float(item) for item in eigenvectors[:, index]), label)
                for index in order
            ]
        else:
            axes = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
        extents = []
        for axis in axes:
            projection = values @ np.asarray(axis, dtype=float)
            extents.append(float(projection.max() - projection.min()))
        return {
            "center": tuple(float(item) for item in center),
            "axes": tuple(axes),
            "extents": tuple(extents),
            "eigenvalues": tuple(float(eigenvalues[index]) for index in order)
            if len(values) >= 3
            else (0.0, 0.0, 0.0),
        }

    def _read_gripper_geometry(self) -> dict[str, Any]:
        if self._stage is None or self._hand_root_path is None or self._eef_frame_path is None:
            raise RuntimeError("Franka geometry is not initialized")
        hand_frame = self._read_prim_frame(self._eef_frame_path)
        hand_geometry = self._read_collision_geometry(
            self._hand_root_path,
            hand_frame["transform"],
            "Franka hand",
        )
        finger_entries = []
        for label, path in zip(("left", "right"), self._finger_root_paths, strict=True):
            frame = self._read_prim_frame(path)
            geometry = self._read_collision_geometry(path, frame["transform"], f"{label} finger")
            finger_entries.append(
                {
                    "label": label,
                    "path": path,
                    "frame": frame,
                    "geometry": geometry,
                }
            )
        left, right = (entry["frame"]["position"] for entry in finger_entries)
        closing_axis = _unit(_subtract(right, left), "gripper closing axis")
        midpoint = _scale(_add(left, right), 0.5)
        approach_axis = _unit(
            _subtract(midpoint, hand_frame["position"]),
            "gripper approach axis",
        )
        approach_axis = _orthogonalize(
            approach_axis,
            closing_axis,
            "gripper approach axis",
        )
        palm_axis = _unit(_cross(closing_axis, approach_axis), "gripper palm normal")
        hand_inverse = _inverse_transform(hand_frame["transform"], "Franka hand frame")
        closing_local = _unit(
            _transform_direction(hand_inverse, closing_axis),
            "local gripper closing axis",
        )
        approach_local = _unit(
            _transform_direction(hand_inverse, approach_axis),
            "local gripper approach axis",
        )
        finger_mid_local = _transform_point(hand_inverse, midpoint)
        left_points = finger_entries[0]["geometry"]["world_points"]
        right_points = finger_entries[1]["geometry"]["world_points"]
        collision_components_local = {
            "panda_hand": [tuple(point) for point in hand_geometry["local_points"]],
            **{
                f"panda_{entry['label']}finger": [
                    _transform_point(hand_inverse, tuple(point))
                    for point in entry["geometry"]["world_points"]
                ]
                for entry in finger_entries
            },
        }
        left_inner = max(_dot(point, closing_axis) for point in left_points)
        right_inner = min(_dot(point, closing_axis) for point in right_points)
        open_width = right_inner - left_inner
        if open_width <= 0.0:
            open_width = _norm(_subtract(right, left))
        indices = tuple(int(value) for value in self._finger_gripper_config.get("indices", ()))
        limits = self.arm_and_finger_limits()
        return {
            "hand": hand_frame,
            "hand_geometry": hand_geometry,
            "fingers": finger_entries,
            "closing_axis_world": closing_axis,
            "approach_axis_world": approach_axis,
            "palm_normal_world": palm_axis,
            "tool_forward_axis_world": approach_axis,
            "closing_axis_local": closing_local,
            "approach_axis_local": approach_local,
            "finger_midpoint_local": finger_mid_local,
            "collision_components_local": collision_components_local,
            "opening_width_m": _finite(open_width, "Franka opening width"),
            "finger_joint_indices": indices,
            "finger_limits": limits,
            "opening_positions": tuple(self._finger_gripper_config.get("open_positions", ())),
            "closing_positions": tuple(self._finger_gripper_config.get("closed_positions", ())),
            "action_deltas": tuple(self._finger_gripper_config.get("action_deltas", ())),
        }

    @staticmethod
    def _resolve_eef_frame_path(stage: Any, robot_root: str, frame_name: str) -> str:
        def matches_for(name: str) -> list[str]:
            return [
                str(prim.GetPath())
                for prim in stage.Traverse()
                if str(prim.GetPath()).startswith(robot_root.rstrip("/") + "/")
                and str(prim.GetName()) == name
            ]

        matches = matches_for(frame_name)
        if len(matches) == 1:
            return matches[0]
        if not matches:
            # Isaac's Lula Franka model names the tool frame right_gripper,
            # while the official 6.0 Franka USD exposes the physical hand as
            # panda_hand.  Geometry diagnostics must use the loaded USD link.
            aliases = {"right_gripper": ("panda_hand",), "panda_hand": ("right_gripper",)}
            for alias in aliases.get(frame_name, ()):
                alias_matches = matches_for(alias)
                if len(alias_matches) == 1:
                    return alias_matches[0]
        if frame_name == "panda_hand" and len(matches) == 0:
            raise RuntimeError("Franka panda_hand kinematics frame is unavailable")
        raise RuntimeError(
            f"Franka kinematics frame {frame_name!r} is missing or ambiguous: {matches}"
        )

    def arm_and_finger_limits(self) -> list[tuple[float, float]]:
        if self._robot is None:
            raise RuntimeError("Franka articulation is not initialized")
        import numpy as np

        limits = np.asarray(_read_runtime_dof_limits(self._robot), dtype=float)
        indices = self._finger_gripper_config.get("indices", ())
        return [
            (_finite(limits[int(index)][0], "Franka finger lower limit"), _finite(limits[int(index)][1], "Franka finger upper limit"))
            for index in indices
        ]

    @staticmethod
    def _surface_pair(
        geometry: Mapping[str, Any],
        axis_local: tuple[float, float, float],
        axis_world: tuple[float, float, float],
        negative_name: str,
        positive_name: str,
    ) -> dict[str, Any]:
        points = geometry["local_points"]
        projections = [_dot(point, axis_local) for point in points]
        minimum, maximum = min(projections), max(projections)
        return {
            "axis_local": axis_local,
            "axis_world": axis_world,
            "negative": {
                "name": negative_name,
                "normal_world": _scale(axis_world, -1.0),
                "coordinate": minimum,
            },
            "positive": {
                "name": positive_name,
                "normal_world": axis_world,
                "coordinate": maximum,
            },
            "span_m": maximum - minimum,
        }

    def _geometry_screen(
        self,
        topology: str,
        required_opening: float,
        target_hand_points: list[tuple[float, float, float]],
        gripper_geometry: Mapping[str, Any],
        drawer_geometry: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        palm_penetration = None
        target_behind_cabinet = None
        clearance_available = drawer_geometry is not None and bool(target_hand_points)
        if clearance_available:
            drawer_bounds = drawer_geometry["world_aabb"]
            inside = [
                point
                for point in target_hand_points
                if all(
                    float(drawer_bounds["min"][axis]) <= point[axis] <= float(drawer_bounds["max"][axis])
                    for axis in range(3)
                )
            ]
            if inside:
                palm_penetration = max(
                    min(
                        min(point[axis] - drawer_bounds["min"][axis], drawer_bounds["max"][axis] - point[axis])
                        for axis in range(3)
                    )
                    for point in inside
                )
                target_behind_cabinet = True
            else:
                target_behind_cabinet = False
                palm_penetration = 0.0
        geometry_screen = {
            "valid": (
                topology != "front_face_clamp"
                and required_opening <= gripper_geometry["opening_width_m"] + 1.0e-4
                and palm_penetration is not None
                and palm_penetration <= 0.0
                and target_behind_cabinet is False
            ),
            "opening_feasible": required_opening <= gripper_geometry["opening_width_m"] + 1.0e-4,
            "required_jaw_span_m": required_opening,
            "actual_opening_width_m": gripper_geometry["opening_width_m"],
            "opposed_surface_pair": topology != "front_face_clamp",
            "palm_penetration_m": palm_penetration,
            "target_behind_solid_cabinet": target_behind_cabinet,
            "clearance_data_available": clearance_available,
            "reasons": [],
        }
        if topology == "front_face_clamp":
            geometry_screen["reasons"].append("front-face/front-clamp is a negative traction baseline")
        if not geometry_screen["opening_feasible"]:
            geometry_screen["reasons"].append("required jaw span exceeds observed opening")
        if not clearance_available:
            geometry_screen["reasons"].append("drawer/palm clearance geometry was unavailable")
        elif geometry_screen["palm_penetration_m"] > 0.0:
            geometry_screen["reasons"].append("palm collision geometry starts inside drawer body")
        return geometry_screen

    @staticmethod
    def _target_component_points(
        position: tuple[float, float, float],
        orientation: tuple[float, float, float, float],
        gripper_geometry: Mapping[str, Any],
    ) -> dict[str, list[tuple[float, float, float]]]:
        return {
            str(name): [
                _add(position, _rotate_vector(orientation, tuple(point)))
                for point in points
            ]
            for name, points in gripper_geometry.get("collision_components_local", {}).items()
        }

    def _full_geometry_screen(
        self,
        candidate: Mapping[str, Any],
        target_components: Mapping[str, list[tuple[float, float, float]]],
        clearance: Mapping[str, Any],
    ) -> dict[str, Any]:
        drawer_geometry = candidate.get("drawer_geometry")
        handle_geometry = candidate.get("handle_geometry")
        hand_points = target_components.get("panda_hand", [])
        base = self._geometry_screen(
            str(candidate["topology"]),
            float(candidate["required_gripper_opening_m"]),
            list(hand_points),
            candidate["gripper_geometry"],
            drawer_geometry if isinstance(drawer_geometry, Mapping) else None,
        )
        if not isinstance(drawer_geometry, Mapping) or not isinstance(handle_geometry, Mapping):
            base["valid"] = False
            base["reasons"].append("full drawer/handle collision geometry was unavailable")
            return base

        basis = tuple(tuple(axis) for axis in clearance["basis_world"])
        drawer_intervals = _projected_intervals(
            [tuple(point) for point in drawer_geometry["world_points"]],
            basis,
        )
        handle_intervals = _projected_intervals(
            [tuple(point) for point in handle_geometry["world_points"]],
            basis,
        )
        component_intervals = {
            name: _projected_intervals(points, basis)
            for name, points in target_components.items()
        }
        drawer_overlaps = {
            name: _projected_overlap(intervals, drawer_intervals)
            for name, intervals in component_intervals.items()
        }
        handle_overlaps = {
            name: _projected_overlap(intervals, handle_intervals)
            for name, intervals in component_intervals.items()
        }

        finger_intervals = [
            component_intervals[name]
            for name in ("panda_leftfinger", "panda_rightfinger")
            if name in component_intervals
        ]
        handle_alignment_valid = False
        if len(finger_intervals) == 2:
            lower, upper = sorted(finger_intervals, key=lambda intervals: intervals[2][0])
            handle_alignment_valid = (
                all(_intervals_overlap(intervals[0], handle_intervals[0]) for intervals in finger_intervals)
                and all(_intervals_overlap(intervals[1], handle_intervals[1]) for intervals in finger_intervals)
                and lower[2][1] <= handle_intervals[2][0]
                and upper[2][0] >= handle_intervals[2][1]
            )
        full_valid = (
            base["valid"]
            and not any(drawer_overlaps.values())
            and not any(handle_overlaps.values())
            and handle_alignment_valid
        )
        base.update(
            {
                "valid": full_valid,
                "component_intervals": component_intervals,
                "drawer_intervals": drawer_intervals,
                "handle_intervals": handle_intervals,
                "drawer_component_overlap": drawer_overlaps,
                "open_gripper_handle_overlap": handle_overlaps,
                "finger_handle_alignment_valid": handle_alignment_valid,
                "endpoint_clearance": dict(clearance),
            }
        )
        if any(drawer_overlaps.values()):
            base["reasons"].append("open hand/finger collision geometry overlaps the drawer body")
        if any(handle_overlaps.values()):
            base["reasons"].append("open hand/finger collision geometry overlaps the handle")
        if not handle_alignment_valid:
            base["reasons"].append("open fingers do not straddle the selected handle surfaces")
        return base

    def _recover_top_bottom_endpoint(
        self,
        candidate: dict[str, Any],
        opening_axis: tuple[float, float, float],
    ) -> dict[str, Any]:
        drawer_geometry = candidate.get("drawer_geometry")
        if not isinstance(drawer_geometry, Mapping):
            return candidate
        geometry_position = tuple(candidate["geometry_grasp_position_world"])
        geometry_orientation = tuple(candidate["geometry_orientation_wxyz"])
        original_components = self._target_component_points(
            geometry_position,
            geometry_orientation,
            candidate["gripper_geometry"],
        )
        clearance = _required_outward_clearance(
            original_components,
            [tuple(point) for point in drawer_geometry["world_points"]],
            opening_axis,
            _GRIPPER_CLEARANCE_BUFFER_M,
        )
        shift = _scale(opening_axis, float(clearance["required_outward_shift_m"]))
        recovered_geometry_grasp = _add(geometry_position, shift)
        recovered_geometry_nominal_pregrasp = _add(
            tuple(candidate["geometry_pregrasp_position_world"]),
            shift,
        )
        original_pregrasp_components = self._target_component_points(
            tuple(candidate["geometry_pregrasp_position_world"]),
            geometry_orientation,
            candidate["gripper_geometry"],
        )
        environment_points = [
            tuple(point)
            for geometry in (candidate["drawer_geometry"], candidate["handle_geometry"])
            for point in geometry["world_points"]
        ]
        environment_top = max(point[2] for point in environment_points)
        gripper_z_values = [
            point[2]
            for points in original_pregrasp_components.values()
            for point in points
        ]
        gripper_bottom = min(gripper_z_values)
        gripper_vertical_span = max(gripper_z_values) - gripper_bottom
        entry_vertical_clearance = max(
            gripper_vertical_span + _GRIPPER_CLEARANCE_BUFFER_M,
            environment_top + _GRIPPER_CLEARANCE_BUFFER_M - gripper_bottom,
        )
        geometry_clearance_entry = _add(
            tuple(candidate["geometry_pregrasp_position_world"]),
            (0.0, 0.0, entry_vertical_clearance),
        )
        recovered_grasp, recovered_orientation = self._geometry_pose_to_controller(
            recovered_geometry_grasp,
            geometry_orientation,
        )
        recovered_nominal_pregrasp, _ = self._geometry_pose_to_controller(
            recovered_geometry_nominal_pregrasp,
            geometry_orientation,
        )
        clearance_entry, _ = self._geometry_pose_to_controller(
            geometry_clearance_entry,
            geometry_orientation,
        )
        controller_waypoints = _cartesian_waypoints(
            recovered_nominal_pregrasp,
            recovered_grasp,
            self.config.waypoint_spacing_m,
        )
        geometry_waypoints = _cartesian_waypoints(
            recovered_geometry_nominal_pregrasp,
            recovered_geometry_grasp,
            self.config.waypoint_spacing_m,
        )
        target_components = self._target_component_points(
            recovered_geometry_grasp,
            geometry_orientation,
            candidate["gripper_geometry"],
        )
        recovered = dict(candidate)
        recovered.update(
            {
                "uncorrected_grasp_position_world": candidate["grasp_position_world"],
                "uncorrected_geometry_grasp_position_world": geometry_position,
                "orientation_wxyz": recovered_orientation,
                "grasp_position_world": recovered_grasp,
                "nominal_pregrasp_position_world": recovered_nominal_pregrasp,
                "pregrasp_position_world": controller_waypoints[0],
                "pregrasp_entry_waypoints_world": (
                    clearance_entry,
                    controller_waypoints[0],
                ),
                "approach_waypoints_world": controller_waypoints,
                "geometry_grasp_position_world": recovered_geometry_grasp,
                "geometry_nominal_pregrasp_position_world": recovered_geometry_nominal_pregrasp,
                "geometry_pregrasp_position_world": geometry_waypoints[0],
                "geometry_approach_waypoints_world": geometry_waypoints,
                "endpoint_outward_shift_m": clearance["required_outward_shift_m"],
                "endpoint_clearance_buffer_m": _GRIPPER_CLEARANCE_BUFFER_M,
                "endpoint_clearance": clearance,
                "entry_vertical_clearance_m": entry_vertical_clearance,
                "entry_gripper_vertical_span_m": gripper_vertical_span,
                "geometry_clearance_entry_position_world": geometry_clearance_entry,
            }
        )
        recovered["geometry_screen"] = self._full_geometry_screen(
            recovered,
            target_components,
            clearance,
        )
        return recovered

    def _build_grasp_candidates(
        self,
        handle_geometry: Mapping[str, Any],
        handle_frame: Mapping[str, Any],
        gripper_geometry: Mapping[str, Any],
        opening_axis: tuple[float, float, float],
        drawer_geometry: Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]:
        handle_transform = handle_frame["transform"]
        handle_inverse = _inverse_transform(handle_transform, "handle frame")
        opening_local = _unit(
            _transform_direction(handle_inverse, opening_axis),
            "handle-local opening axis",
        )
        up_local = _unit(
            _transform_direction(handle_inverse, (0.0, 0.0, 1.0)),
            "handle-local up axis",
        )
        principal = handle_geometry["principal_frame"]
        principal_axes = list(principal["axes"])
        opening_index = max(
            range(3),
            key=lambda index: abs(_dot(principal_axes[index], opening_local)),
        )
        remaining = [index for index in range(3) if index != opening_index]
        vertical_index = max(
            remaining,
            key=lambda index: abs(_dot(principal_axes[index], up_local)),
        )
        side_index = next(index for index in remaining if index != vertical_index)
        vertical_axis_local = principal_axes[vertical_index]
        if _dot(vertical_axis_local, up_local) < 0.0:
            vertical_axis_local = _scale(vertical_axis_local, -1.0)  # type: ignore[assignment]
        side_axis_local = principal_axes[side_index]
        side_reference = _cross(opening_local, vertical_axis_local)
        if _dot(side_axis_local, side_reference) < 0.0:
            side_axis_local = _scale(side_axis_local, -1.0)  # type: ignore[assignment]
        opening_axis_local = opening_local
        surface_axes = {
            "opening": opening_axis_local,
            "vertical": vertical_axis_local,
            "side": side_axis_local,
        }
        surface_world = {
            name: _unit(
                _transform_direction(handle_transform, axis),
                f"{name} surface axis world",
            )
            for name, axis in surface_axes.items()
        }
        surfaces = {
            "front_back": self._surface_pair(
                handle_geometry,
                opening_axis_local,
                surface_world["opening"],
                "back",
                "front",
            ),
            "top_bottom": self._surface_pair(
                handle_geometry,
                vertical_axis_local,
                surface_world["vertical"],
                "bottom",
                "top",
            ),
            "side": self._surface_pair(
                handle_geometry,
                side_axis_local,
                surface_world["side"],
                "left",
                "right",
            ),
        }
        handle_center_world = _transform_point(
            handle_transform,
            handle_geometry["local_aabb"]["center"],
        )
        source_closing = gripper_geometry["closing_axis_local"]
        source_approach = gripper_geometry["approach_axis_local"]
        hand_local_points = gripper_geometry.get("hand_geometry", {}).get("local_points", [])
        ordered = [
            ("front_face_clamp", "front_back", "back", "front"),
            ("top_bottom_pinch", "top_bottom", "bottom", "top"),
            ("side_pinch", "side", "left", "right"),
        ]
        candidates: list[dict[str, Any]] = []
        for topology, surface_name, negative_name, positive_name in ordered:
            surface = surfaces[surface_name]
            candidate_approach = (
                opening_axis
                if topology == "front_face_clamp"
                else _orthogonalize(
                    _scale(opening_axis, -1.0),
                    surface["axis_world"],
                    "candidate approach axis",
                )
            )
            orientation = (
                _LEGACY_GRASP_ORIENTATION_WXYZ
                if topology == "front_face_clamp"
                else _basis_rotation_quaternion(
                    source_closing,
                    source_approach,
                    surface["axis_world"],
                    candidate_approach,
                )
            )
            finger_mid_world = _rotate_vector(
                orientation,
                gripper_geometry["finger_midpoint_local"],
            )
            grasp_position = _subtract(handle_center_world, finger_mid_world)
            pregrasp_position = _subtract(
                grasp_position,
                _scale(candidate_approach, self.config.grasp_topology.pregrasp_distance_m),
            )
            required_opening = float(surface["span_m"])
            target_hand_points = [
                _add(
                    grasp_position,
                    _rotate_vector(orientation, tuple(point)),
                )
                for point in hand_local_points
            ]
            geometry_screen = self._geometry_screen(
                topology,
                required_opening,
                target_hand_points,
                gripper_geometry,
                drawer_geometry,
            )
            controller_grasp_position, controller_orientation = self._geometry_pose_to_controller(
                grasp_position,
                orientation,
            )
            controller_pregrasp_position, _ = self._geometry_pose_to_controller(
                pregrasp_position,
                orientation,
            )
            candidates.append(
                {
                    "candidate_id": topology,
                    "topology": topology,
                    "orientation_wxyz": controller_orientation,
                    "grasp_position_world": controller_grasp_position,
                    "pregrasp_position_world": controller_pregrasp_position,
                    "geometry_orientation_wxyz": orientation,
                    "geometry_grasp_position_world": grasp_position,
                    "geometry_pregrasp_position_world": pregrasp_position,
                    "target_frame": self._eef_kinematics_frame,
                    "observation_frame": self._observation_frame,
                    "frame_transform_geometry_to_controller": self._geometry_to_controller_transform,
                    "closing_axis_world": surface["axis_world"],
                    "approach_axis_world": candidate_approach,
                    "palm_normal_world": (
                        _unit(
                        _cross(surface["axis_world"], candidate_approach),
                            "candidate palm normal",
                        )
                        if topology != "front_face_clamp"
                        else gripper_geometry["palm_normal_world"]
                    ),
                    "opening_axis_world": opening_axis,
                    "handle_surface_pair": (negative_name, positive_name),
                    "expected_contact_normals": (
                        surface["negative"]["normal_world"],
                        surface["positive"]["normal_world"],
                    ),
                    "required_gripper_opening_m": required_opening,
                    "surface_geometry": surface,
                    "handle_frame": handle_frame,
                    "geometry_screen": geometry_screen,
                    "handle_geometry": handle_geometry,
                    "gripper_geometry": gripper_geometry,
                    "drawer_geometry": drawer_geometry,
                    "material_audit": self._read_material_audit(),
                }
            )
        candidates.append(
            {
                "candidate_id": "cage_hook",
                "topology": "cage_hook",
                "orientation_wxyz": _LEGACY_GRASP_ORIENTATION_WXYZ,
                "grasp_position_world": handle_center_world,
                "pregrasp_position_world": _subtract(
                    handle_center_world,
                    _scale(opening_axis, self.config.grasp_topology.pregrasp_distance_m),
                ),
                "closing_axis_world": gripper_geometry["closing_axis_world"],
                    "approach_axis_world": candidate_approach,
                "palm_normal_world": gripper_geometry["palm_normal_world"],
                "opening_axis_world": opening_axis,
                "handle_surface_pair": ("retaining", "retaining"),
                "expected_contact_normals": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
                "required_gripper_opening_m": 0.0,
                "handle_frame": handle_frame,
                "geometry_screen": {
                    "valid": False,
                    "opening_feasible": True,
                    "required_jaw_span_m": 0.0,
                    "actual_opening_width_m": gripper_geometry["opening_width_m"],
                    "opposed_surface_pair": False,
                    "palm_penetration_m": None,
                    "target_behind_solid_cabinet": None,
                    "reasons": ["no hook/cage geometry exists in the original Franka fingers"],
                },
                "ik_screen": {"valid": False, "reason": "not_a_parallel_surface_candidate", "probes": {}},
                "handle_geometry": handle_geometry,
                "gripper_geometry": gripper_geometry,
                "drawer_geometry": drawer_geometry,
                "material_audit": self._read_material_audit(),
            }
        )
        return candidates

    def _ik_quality(self, ik_screen: Mapping[str, Any]) -> dict[str, float] | None:
        if self._robot is None:
            return None
        probes = ik_screen.get("probes")
        if not isinstance(probes, Mapping) or not ik_screen.get("valid"):
            return None
        try:
            current = tuple(self.read_robot_joint_positions())
            limits = self.arm_joint_limits()
            solutions = []
            for probe in probes.values():
                if not isinstance(probe, Mapping) or probe.get("success") is not True:
                    return None
                solution = tuple(_finite(value, "orientation-family IK joint") for value in probe["joint_positions"])
                if len(solution) != len(current) or len(solution) != len(limits):
                    return None
                solutions.append(solution)
            margins = [
                min(_finite(solution[index] - limits[index][0], "joint-limit margin"), _finite(limits[index][1] - solution[index], "joint-limit margin"))
                for solution in solutions
                for index in range(len(solution))
            ]
            distances = [
                _norm(_subtract(solution, current))
                for solution in solutions
            ]
            return {
                "minimum_joint_limit_margin": min(margins),
                "maximum_joint_distance": max(distances),
            }
        except (TypeError, ValueError, KeyError):
            return None

    def _select_orientation_family(
        self,
        candidate: dict[str, Any],
        opening_axis: tuple[float, float, float],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        geometry_orientation = tuple(candidate["geometry_orientation_wxyz"])
        geometry_grasp = tuple(candidate["geometry_grasp_position_world"])
        finger_midpoint = tuple(candidate["gripper_geometry"]["finger_midpoint_local"])
        handle_center = _add(
            geometry_grasp,
            _rotate_vector(geometry_orientation, finger_midpoint),
        )
        closing_axis = tuple(candidate["closing_axis_world"])
        source_approach = tuple(candidate["approach_axis_world"])
        requested_angle = self.config.grasp_topology.orientation_family_angle_deg
        family: list[dict[str, Any]] = []
        selected: dict[str, Any] | None = None
        requested_variant: dict[str, Any] | None = None
        selected_score: tuple[float, ...] | None = None
        for family_variant in _orientation_family_variants(
            geometry_orientation,
            closing_axis,
            source_approach,
            (
                _ORIENTATION_FAMILY_DEGREES
                if requested_angle is None
                or requested_angle in _ORIENTATION_FAMILY_DEGREES
                else (requested_angle,)
            ),
        ):
            degrees = int(family_variant["degrees_about_closing_axis"])
            geometry_variant_orientation = tuple(
                family_variant["geometry_orientation_wxyz"]
            )
            approach_axis = tuple(family_variant["approach_axis_world"])
            grasp_position = _subtract(
                handle_center,
                _rotate_vector(geometry_variant_orientation, finger_midpoint),
            )
            pregrasp_position = _subtract(
                grasp_position,
                _scale(approach_axis, self.config.grasp_topology.pregrasp_distance_m),
            )
            target_hand_points = [
                _add(grasp_position, _rotate_vector(geometry_variant_orientation, tuple(point)))
                for point in candidate["gripper_geometry"].get("hand_geometry", {}).get("local_points", [])
            ]
            geometry_screen = self._geometry_screen(
                candidate["topology"],
                candidate["required_gripper_opening_m"],
                target_hand_points,
                candidate["gripper_geometry"],
                candidate.get("drawer_geometry"),
            )
            controller_grasp_position, controller_orientation = self._geometry_pose_to_controller(
                grasp_position,
                geometry_variant_orientation,
            )
            controller_pregrasp_position, _ = self._geometry_pose_to_controller(
                pregrasp_position,
                geometry_variant_orientation,
            )
            variant = dict(candidate)
            variant.update(
                {
                    "orientation_wxyz": controller_orientation,
                    "grasp_position_world": controller_grasp_position,
                    "pregrasp_position_world": controller_pregrasp_position,
                    "geometry_orientation_wxyz": geometry_variant_orientation,
                    "geometry_grasp_position_world": grasp_position,
                    "geometry_pregrasp_position_world": pregrasp_position,
                    "approach_axis_world": approach_axis,
                    "palm_normal_world": _unit(
                        _cross(closing_axis, approach_axis),
                        "orientation-family palm normal",
                    ),
                    "geometry_screen": geometry_screen,
                    "orientation_family_angle_deg": degrees,
                }
            )
            if requested_angle is not None and degrees == requested_angle:
                requested_variant = variant
            ik_screen = (
                self._screen_candidate_ik(variant, opening_axis)
                if geometry_screen["valid"]
                else {"valid": False, "reason": "geometry_screen_failed", "probes": {}}
            )
            quality = self._ik_quality(ik_screen)
            alignment = _dot(approach_axis, _scale(opening_axis, -1.0))
            gross_collision = geometry_screen.get("palm_penetration_m") is not None and geometry_screen.get("palm_penetration_m") > 0.0
            score = None
            if geometry_screen["valid"] and ik_screen["valid"] and not gross_collision and quality is not None:
                score = (
                    alignment,
                    quality["minimum_joint_limit_margin"],
                    -quality["maximum_joint_distance"],
                    -abs(float(degrees)),
                    float(degrees),
                )
                if (
                    (requested_angle is None or degrees == requested_angle)
                    and (selected_score is None or score > selected_score)
                ):
                    selected = variant
                    selected_score = score
            family.append(
                {
                    "degrees_about_closing_axis": degrees,
                    "constraint_axis_world": closing_axis,
                    "closing_axis_world": family_variant["closing_axis_world"],
                    "approach_axis_world": approach_axis,
                    "approach_alignment_to_negative_opening": alignment,
                    "geometry_valid": geometry_screen["valid"],
                    "gross_palm_cabinet_collision": gross_collision,
                    "ik_valid": ik_screen["valid"],
                    "ik_probes": {
                        name: probe.get("success") is True
                        for name, probe in ik_screen.get("probes", {}).items()
                        if isinstance(probe, Mapping)
                    },
                    "minimum_joint_limit_margin": (
                        None if quality is None else quality["minimum_joint_limit_margin"]
                    ),
                    "maximum_joint_distance": (
                        None if quality is None else quality["maximum_joint_distance"]
                    ),
                    "score": score,
                    "selected": False,
                }
            )
        if selected is None and requested_variant is not None:
            selected = requested_variant
        if selected is None:
            return candidate, family
        selected_angle = selected["orientation_family_angle_deg"]
        for item in family:
            item["selected"] = item["degrees_about_closing_axis"] == selected_angle
        selected["orientation_family"] = family
        return selected, family

    def _screen_candidate_ik(
        self,
        candidate: dict[str, Any],
        opening_axis: tuple[float, float, float],
    ) -> dict[str, Any]:
        orientation = candidate["orientation_wxyz"]
        grasp = candidate["grasp_position_world"]
        probes = {
            "pre_grasp": candidate["pregrasp_position_world"],
            "grasp": grasp,
            "grasp_plus_1mm": _add(grasp, _scale(opening_axis, 0.001)),
            "grasp_plus_3mm": _add(grasp, _scale(opening_axis, 0.003)),
            "grasp_plus_5mm": _add(grasp, _scale(opening_axis, 0.005)),
        }
        for index, waypoint in enumerate(candidate.get("approach_waypoints_world", ()), start=1):
            probes[f"approach_waypoint_{index}"] = waypoint
        for index, waypoint in enumerate(
            candidate.get("pregrasp_entry_waypoints_world", ()),
            start=1,
        ):
            probes[f"pregrasp_entry_waypoint_{index}"] = waypoint
        results = {}
        for name, target in probes.items():
            result = self.solve_ik(target, orientation)
            results[name] = result
        return {
            "valid": all(result.get("success") is True for result in results.values()),
            "probes": results,
        }

    def _read_material_audit(self) -> dict[str, Any]:
        if self._material_audit is not None:
            return self._material_audit
        from pxr import UsdPhysics, UsdShade

        if self._stage is None:
            raise RuntimeError("USD stage is not initialized")

        def material_for_path(path: str) -> dict[str, Any]:
            prim = self._stage.GetPrimAtPath(path)
            if not prim.IsValid():
                return {"prim_path": path, "material_path": None, "error": "prim_missing"}
            material = None
            try:
                physics_purpose = getattr(UsdShade.Tokens, "physics", "physics")
                try:
                    bound = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial(physics_purpose)
                except TypeError:
                    bound = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
                material = bound[0] if isinstance(bound, tuple) else bound
            except (AttributeError, RuntimeError, TypeError):
                material = None
            if material is None or not material.GetPrim().IsValid():
                return {"prim_path": path, "material_path": None, "error": "material_unresolved"}
            material_prim = material.GetPrim()
            api = UsdPhysics.MaterialAPI(material_prim)
            result: dict[str, Any] = {
                "prim_path": path,
                "material_path": str(material_prim.GetPath()),
                "static_friction": None,
                "dynamic_friction": None,
                "restitution": None,
                "combine_mode": None,
            }
            if api:
                for field, getter in (
                    ("static_friction", api.GetStaticFrictionAttr),
                    ("dynamic_friction", api.GetDynamicFrictionAttr),
                    ("restitution", api.GetRestitutionAttr),
                ):
                    try:
                        value = getter().Get()
                        result[field] = None if value is None else _finite(value, field)
                    except (AttributeError, TypeError, ValueError):
                        pass
            for attribute_name in (
                "physxMaterial:frictionCombineMode",
                "physics:frictionCombineMode",
                "frictionCombineMode",
            ):
                attribute = material_prim.GetAttribute(attribute_name)
                if attribute and attribute.IsValid():
                    value = attribute.Get()
                    if value is not None:
                        result["combine_mode"] = str(value)
                        break
            return result

        finger_materials = [
            material_for_path(path)
            for path in self._finger_root_paths
        ]
        handle_path = SEKTION_TOP_DRAWER_RUNTIME_ROOT + "/drawer_handle_top"
        handle_paths, _ = self._collision_paths_for_root(handle_path)
        handle_material = material_for_path(handle_paths[0] if handle_paths else handle_path)
        friction_values = [
            value["static_friction"]
            for value in (*finger_materials, handle_material)
            if value.get("static_friction") is not None
        ]
        dynamic_values = [
            value["dynamic_friction"]
            for value in (*finger_materials, handle_material)
            if value.get("dynamic_friction") is not None
        ]
        self._material_audit = {
            "left_finger": finger_materials[0],
            "right_finger": finger_materials[1],
            "handle": handle_material,
            "static_friction": friction_values,
            "dynamic_friction": dynamic_values,
            "combine_modes": [
                value.get("combine_mode")
                for value in (*finger_materials, handle_material)
            ],
            "effective_pair_policy": {
                "static_friction": min(friction_values) if friction_values else None,
                "dynamic_friction": min(dynamic_values) if dynamic_values else None,
                "combine_mode": "runtime_pair_policy_not_exposed"
                if not all(value.get("combine_mode") for value in (*finger_materials, handle_material))
                else "see_materials",
            },
            "override_required": False,
            "runtime_gripper_material_policy": "existing_runtime_fixture_material_only_not_changed",
            "runtime_gripper_material_override_active": True,
            "runtime_gripper_material_override": {
                "static_friction": 2.0,
                "dynamic_friction": 1.5,
                "restitution": 0.0,
                "scope": "Franka fingers only",
                "reason": "pre-existing fixture policy; no new friction tuning in topology recovery",
            },
        }
        return self._material_audit

    def _sync_kinematics_base_pose(self) -> None:
        if self._robot is None or self._kinematics is None:
            raise RuntimeError("Franka kinematics is not initialized")
        import numpy as np

        position, orientation = self._robot.get_world_pose()
        base_position = tuple(float(value) for value in np.asarray(position).reshape(-1)[:3])
        base_orientation = tuple(float(value) for value in np.asarray(orientation).reshape(-1)[:4])
        self._kinematics.get_kinematics_solver().set_robot_base_pose(
            np.asarray(base_position, dtype=float),
            np.asarray(base_orientation, dtype=float),
        )
        self._lula_base_position = base_position
        self._lula_base_orientation = base_orientation

    def _read_kinematic_frame_transform(
        self,
        frame_name: str,
        joint_positions: tuple[float, ...],
    ) -> tuple[float, ...]:
        if self._kinematics is None:
            raise RuntimeError("Franka kinematics is not initialized")
        import numpy as np

        position, rotation = self._kinematics.get_kinematics_solver().compute_forward_kinematics(
            frame_name,
            np.asarray(joint_positions, dtype=float),
        )
        position_values = tuple(
            _finite(value, f"{frame_name} FK position")
            for value in np.asarray(position, dtype=float).reshape(-1)[:3]
        )
        matrix = np.asarray(rotation, dtype=float).reshape(3, 3)
        if not np.isfinite(matrix).all():
            raise ValueError(f"{frame_name} FK orientation is non-finite")
        flat = (
            float(matrix[0, 0]), float(matrix[1, 0]), float(matrix[2, 0]), 0.0,
            float(matrix[0, 1]), float(matrix[1, 1]), float(matrix[2, 1]), 0.0,
            float(matrix[0, 2]), float(matrix[1, 2]), float(matrix[2, 2]), 0.0,
            *position_values,
            1.0,
        )
        return _matrix4(flat, f"{frame_name} FK transform")

    def _initialize_frame_reconciliation(self) -> None:
        if self._observation_frame is None or self._eef_kinematics_frame is None:
            raise RuntimeError("Franka frame mapping is not initialized")
        if self._observation_frame not in self._lula_frame_names:
            raise RuntimeError(f"USD observation frame is absent from Lula: {self._observation_frame}")
        if self._geometry_to_controller_transform is not None:
            return
        current = tuple(self.read_robot_joint_positions())
        sample_configs = (
            current,
            tuple(a + b for a, b in zip(current, (0.08, -0.04, 0.06, 0.05, -0.05, 0.04, -0.04), strict=True)),
            tuple(a + b for a, b in zip(current, (-0.06, 0.05, -0.04, -0.06, 0.04, -0.05, 0.05), strict=True)),
        )
        transforms = []
        for index, joints in enumerate(sample_configs, start=1):
            observation = self._read_kinematic_frame_transform(self._observation_frame, joints)
            controller = self._read_kinematic_frame_transform(self._eef_kinematics_frame, joints)
            transforms.append(
                {
                    "configuration": index,
                    "joint_positions": joints,
                    "transform": _compose_transform(_inverse_transform(observation, "observation FK"), controller),
                }
            )
        reference = transforms[0]["transform"]
        diagnostics = []
        max_translation_error = 0.0
        max_orientation_error = 0.0
        for item in transforms:
            transform = item["transform"]
            translation_error = _norm(
                _subtract(
                    (transform[12], transform[13], transform[14]),
                    (reference[12], reference[13], reference[14]),
                )
            )
            relative = _compose_transform(_inverse_transform(reference, "reference frame transform"), transform)
            trace = relative[0] + relative[5] + relative[10]
            orientation_error = math.acos(max(-1.0, min(1.0, (trace - 1.0) * 0.5)))
            max_translation_error = max(max_translation_error, translation_error)
            max_orientation_error = max(max_orientation_error, orientation_error)
            diagnostics.append(
                {
                    "configuration": item["configuration"],
                    "joint_positions": item["joint_positions"],
                    "transform": transform,
                    "translation_error_m": translation_error,
                    "orientation_error_rad": orientation_error,
                }
            )
        if max_translation_error > _FRAME_TRANSFORM_TOLERANCE_M or max_orientation_error > _FRAME_TRANSFORM_TOLERANCE_RAD:
            raise RuntimeError(
                "panda_hand to right_gripper transform is not constant: "
                f"translation_error={max_translation_error}, orientation_error={max_orientation_error}"
            )
        self._geometry_to_controller_transform = reference
        self._frame_transform_diagnostics = {
            "source_frame": self._observation_frame,
            "target_frame": self._eef_kinematics_frame,
            "configurations": diagnostics,
            "constant_transform": True,
            "max_translation_error_m": max_translation_error,
            "max_orientation_error_rad": max_orientation_error,
        }

    def _geometry_pose_to_controller(
        self,
        position: tuple[float, float, float],
        orientation: tuple[float, float, float, float],
    ) -> tuple[tuple[float, ...], tuple[float, float, float, float]]:
        if self._geometry_to_controller_transform is None:
            raise RuntimeError("frame transform is not initialized")
        controller_pose = _compose_transform(
            _pose_transform(position, orientation),
            self._geometry_to_controller_transform,
        )
        return (
            (controller_pose[12], controller_pose[13], controller_pose[14]),
            _matrix_to_quaternion(controller_pose),
        )

    def read_eef_pose(self) -> Mapping[str, Any]:
        import numpy as np
        from isaacsim.core.utils.numpy.rotations import rot_matrices_to_quats

        position, rotation = self._kinematics.compute_end_effector_pose()
        orientation = rot_matrices_to_quats(rotation)
        return {
            "position": [float(value) for value in np.asarray(position).reshape(-1)[:3]],
            "orientation_wxyz": [float(value) for value in np.asarray(orientation).reshape(-1)[:4]],
        }

    def solve_ik(
        self,
        position: tuple[float, ...],
        orientation: tuple[float, ...],
        *,
        warm_start: list[float] | tuple[float, ...] | None = None,
        position_tolerance_m: float = _POSITION_TOLERANCE_M,
    ) -> Mapping[str, Any]:
        import numpy as np

        position_tolerance_m = _finite(position_tolerance_m, "IK position tolerance")
        if position_tolerance_m <= 0.0:
            raise ValueError("IK position tolerance must be positive")
        self._sync_kinematics_base_pose()
        if warm_start is None:
            action, success = self._kinematics.compute_inverse_kinematics(
                np.asarray(position, dtype=float),
                np.asarray(orientation, dtype=float),
                position_tolerance=position_tolerance_m,
                orientation_tolerance=_ORIENTATION_TOLERANCE_RAD,
            )
            joints = _action_joint_positions(action) if success else []
            seed_source = "current_articulation_q"
        else:
            seed = np.asarray(warm_start, dtype=float).reshape(-1)
            if len(seed) != 7 or not np.all(np.isfinite(seed)):
                raise ValueError("explicit IK warm start must contain seven finite arm joints")
            lula_solver = self._kinematics.get_kinematics_solver()
            default_ccd_delta = float(lula_solver.ccd_descent_termination_delta)
            default_ccd_iterations = int(lula_solver.ccd_max_iterations)
            resolution_scale = max(
                1,
                math.ceil(default_ccd_delta / _PULL_MIN_EXPECTED_DQ_RAD),
            )
            continuation_profile = {
                "default_ccd_descent_termination_delta_rad": default_ccd_delta,
                "default_ccd_max_iterations": default_ccd_iterations,
                "ccd_descent_termination_delta_rad": _PULL_MIN_EXPECTED_DQ_RAD,
                "ccd_max_iterations": default_ccd_iterations * resolution_scale,
                "derivation": "minimum_waypoint_expected_dq_with_iteration_scaling",
            }
            lula_solver.ccd_descent_termination_delta = _PULL_MIN_EXPECTED_DQ_RAD
            lula_solver.ccd_max_iterations = continuation_profile["ccd_max_iterations"]
            try:
                joints_raw, success = lula_solver.compute_inverse_kinematics(
                    self._eef_kinematics_frame,
                    np.asarray(position, dtype=float),
                    np.asarray(orientation, dtype=float),
                    warm_start=seed,
                    position_tolerance=position_tolerance_m,
                    orientation_tolerance=_ORIENTATION_TOLERANCE_RAD,
                )
            finally:
                lula_solver.ccd_descent_termination_delta = default_ccd_delta
                lula_solver.ccd_max_iterations = default_ccd_iterations
            joints = (
                [float(value) for value in np.asarray(joints_raw, dtype=float).reshape(-1)]
                if success
                else []
            )
            seed_source = "explicit_previous_accepted_q"
        return {
            "success": bool(success),
            "joint_positions": joints or [],
            "reason": "ik_failed" if not success else None,
            "target_position": [float(value) for value in np.asarray(position).reshape(-1)[:3]],
            "base_position": [float(value) for value in self._lula_base_position or ()],
            "base_orientation": [float(value) for value in self._lula_base_orientation or ()],
            "position_tolerance_m": position_tolerance_m,
            "seed_source": seed_source,
            "warm_start": None if warm_start is None else [float(value) for value in warm_start],
            "continuation_solver_profile": (
                continuation_profile if warm_start is not None else None
            ),
        }

    def compute_arm_fk(self, joint_positions: list[float] | tuple[float, ...]) -> Mapping[str, Any]:
        import numpy as np
        from isaacsim.core.utils.numpy.rotations import rot_matrices_to_quats

        joints = np.asarray(joint_positions, dtype=float).reshape(-1)
        if len(joints) != 7 or not np.all(np.isfinite(joints)):
            raise ValueError("Franka FK requires seven finite arm joints")
        self._sync_kinematics_base_pose()
        position, rotation = self._kinematics.get_kinematics_solver().compute_forward_kinematics(
            self._eef_kinematics_frame,
            joints,
        )
        orientation = rot_matrices_to_quats(rotation)
        return {
            "frame": self._eef_kinematics_frame,
            "position": [float(value) for value in np.asarray(position).reshape(-1)[:3]],
            "orientation_wxyz": [
                float(value) for value in np.asarray(orientation).reshape(-1)[:4]
            ],
        }

    def predict_local_ik_seed(
        self,
        position: tuple[float, ...],
        orientation: tuple[float, ...],
        joint_positions: list[float] | tuple[float, ...],
    ) -> Mapping[str, Any]:
        import numpy as np

        joints = np.asarray(joint_positions, dtype=float).reshape(-1)
        if len(joints) != 7 or not np.all(np.isfinite(joints)):
            raise ValueError("local IK predictor requires seven finite arm joints")
        target = np.asarray(position, dtype=float).reshape(-1)[:3]
        self._sync_kinematics_base_pose()
        solver = self._kinematics.get_kinematics_solver()
        current_position, current_rotation = solver.compute_forward_kinematics(
            self._eef_kinematics_frame,
            joints,
        )
        current_position = np.asarray(current_position, dtype=float).reshape(-1)[:3]
        current_rotation = np.asarray(current_rotation, dtype=float).reshape(3, 3)
        finite_difference_rad = 1.0e-5
        jacobian = np.zeros((6, 7), dtype=float)
        for index in range(7):
            perturbed = joints.copy()
            perturbed[index] += finite_difference_rad
            perturbed_position, perturbed_rotation = solver.compute_forward_kinematics(
                self._eef_kinematics_frame,
                perturbed,
            )
            jacobian[:3, index] = (
                np.asarray(perturbed_position, dtype=float).reshape(-1)[:3]
                - current_position
            ) / finite_difference_rad
            relative_rotation = (
                np.asarray(perturbed_rotation, dtype=float).reshape(3, 3)
                @ current_rotation.T
            )
            jacobian[3:, index] = np.asarray(
                (
                    relative_rotation[2, 1] - relative_rotation[1, 2],
                    relative_rotation[0, 2] - relative_rotation[2, 0],
                    relative_rotation[1, 0] - relative_rotation[0, 1],
                ),
                dtype=float,
            ) / (2.0 * finite_difference_rad)
        desired_twist = np.concatenate((target - current_position, np.zeros(3, dtype=float)))
        predicted_delta, _, _, singular_values = np.linalg.lstsq(
            jacobian,
            desired_twist,
            rcond=None,
        )
        unscaled_max = float(np.max(np.abs(predicted_delta)))
        scale = (
            1.0
            if unscaled_max <= _MOTION_ROUTE_LOCAL_MAX_DQ_RAD
            else _MOTION_ROUTE_LOCAL_MAX_DQ_RAD / unscaled_max
        )
        predicted_delta *= scale
        predicted = joints + predicted_delta
        limits = self.arm_joint_limits()
        predicted = np.asarray(
            [
                min(max(value, lower), upper)
                for value, (lower, upper) in zip(predicted, limits, strict=True)
            ],
            dtype=float,
        )
        nonzero_singular = [float(value) for value in singular_values if value > 1.0e-12]
        condition = (
            None
            if not nonzero_singular
            else max(nonzero_singular) / min(nonzero_singular)
        )
        return {
            "joint_positions": [float(value) for value in predicted],
            "previous_joint_positions": [float(value) for value in joints],
            "predicted_joint_delta_rad": [float(value) for value in predicted - joints],
            "unscaled_max_abs_delta_rad": unscaled_max,
            "applied_scale": scale,
            "max_abs_delta_limit_rad": _MOTION_ROUTE_LOCAL_MAX_DQ_RAD,
            "finite_difference_rad": finite_difference_rad,
            "position_error_before_m": float(np.linalg.norm(target - current_position)),
            "orientation_target_wxyz": [float(value) for value in orientation],
            "singular_values": [float(value) for value in singular_values],
            "condition_number": condition,
            "method": "deterministic_finite_difference_lula_fk_jacobian_least_squares",
        }

    def arm_joint_names(self) -> list[str]:
        if self._robot is None:
            raise RuntimeError("Franka articulation is not initialized")
        names = [str(name) for name in getattr(self._robot, "dof_names", ())[:7]]
        if len(names) != 7:
            raise RuntimeError("Franka articulation does not expose seven arm joint names")
        return names

    def ik_api_diagnostics(self) -> Mapping[str, Any]:
        import inspect

        articulation_method = self._kinematics.compute_inverse_kinematics
        lula_solver = self._kinematics.get_kinematics_solver()
        lula_method = lula_solver.compute_inverse_kinematics
        articulation_help = inspect.getdoc(articulation_method) or ""
        lula_help = inspect.getdoc(lula_method) or ""
        default_ccd_delta = float(lula_solver.ccd_descent_termination_delta)
        default_ccd_iterations = int(lula_solver.ccd_max_iterations)
        resolution_scale = max(
            1,
            math.ceil(default_ccd_delta / _PULL_MIN_EXPECTED_DQ_RAD),
        )
        return {
            "articulation_solver": type(self._kinematics).__name__,
            "underlying_solver": type(self._kinematics.get_kinematics_solver()).__name__,
            "kinematic_frame": self._eef_kinematics_frame,
            "articulation_signature": str(inspect.signature(articulation_method)),
            "lula_signature": str(inspect.signature(lula_method)),
            "articulation_uses_current_q": "current robot position as a warm start"
            in articulation_help,
            "explicit_warm_start_supported": "warm_start" in inspect.signature(lula_method).parameters,
            "warm_start_priority_documented": "warm start will be given priority" in lula_help,
            "default_solver_profile": {
                "ccd_descent_termination_delta_rad": default_ccd_delta,
                "ccd_max_iterations": default_ccd_iterations,
                "bfgs_max_iterations": int(lula_solver.bfgs_max_iterations),
                "max_num_descents": int(lula_solver.max_num_descents),
            },
            "continuation_solver_profile": {
                "ccd_descent_termination_delta_rad": _PULL_MIN_EXPECTED_DQ_RAD,
                "ccd_max_iterations": default_ccd_iterations * resolution_scale,
                "derivation": "minimum_waypoint_expected_dq_with_iteration_scaling",
            },
        }

    def arm_joint_limits(self) -> list[tuple[float, float]]:
        import numpy as np

        limits = np.asarray(_read_runtime_dof_limits(self._robot), dtype=float)
        return [(_finite(row[0], "arm lower limit"), _finite(row[1], "arm upper limit")) for row in limits[:7]]

    def read_robot_joint_positions(self) -> list[float]:
        if self._robot is None:
            raise RuntimeError("Franka articulation is not initialized")
        import numpy as np

        values = np.asarray(self._robot.get_joint_positions(), dtype=float).reshape(-1)
        if len(values) < 7:
            raise RuntimeError("Franka articulation has fewer than seven arm joints")
        return [_finite(value, "Franka arm joint position") for value in values[:7]]

    def apply_arm_targets(self, positions: list[float]) -> None:
        import numpy as np
        from isaacsim.core.utils.types import ArticulationAction

        if len(positions) != 7:
            raise ValueError("Franka IK must provide seven arm joint targets")
        self._robot.apply_action(
            ArticulationAction(
                joint_positions=np.asarray(positions, dtype=float),
                joint_indices=np.arange(7, dtype=int),
            )
        )

    def _apply_gripper(self, opened: bool) -> None:
        import numpy as np
        from isaacsim.core.utils.types import ArticulationAction

        if opened:
            self._gripper.open()
            positions = self._finger_gripper_config["open_positions"]
        else:
            self._gripper.close()
            positions = self._finger_gripper_config["closed_positions"]
        self._robot.apply_action(
            ArticulationAction(
                joint_positions=np.asarray(positions, dtype=float),
                joint_indices=np.asarray(self._finger_gripper_config["indices"], dtype=int),
            )
        )

    def close_gripper(self) -> None:
        self._apply_gripper(False)

    def open_gripper(self) -> None:
        self._apply_gripper(True)

    def read_gripper(self) -> Mapping[str, Any]:
        import numpy as np

        positions = self._robot.get_joint_positions()
        diagnostics = _read_finger_dof_diagnostics(self._robot, positions)
        diagnostics["finger_gripper_config"] = self._finger_gripper_config
        open_observed = _finger_gripper_is_open(diagnostics)
        if not open_observed:
            entries = diagnostics.get("finger_dofs", [])
            open_positions = self._finger_gripper_config.get("open_positions", [])
            open_observed = len(entries) == 2 and all(
                float(entry["position"]) >= float(open_position) - 0.005
                for entry, open_position in zip(entries, open_positions, strict=True)
            )
        entries = diagnostics.get("finger_dofs", [])
        finger_positions = [entry["position"] for entry in entries]
        velocity_values = np.asarray(self._robot.get_joint_velocities(), dtype=float).reshape(-1)
        indices = tuple(int(value) for value in self._finger_gripper_config.get("indices", ()))
        finger_velocities = [
            _finite(velocity_values[index], "Franka finger joint velocity")
            for index in indices
        ]
        finger_frames = [self._read_prim_frame(path) for path in self._finger_root_paths]
        return {
            "positions": finger_positions,
            "velocities": finger_velocities,
            "joint_limits": self.arm_and_finger_limits(),
            "joint_separation_m": sum(finger_positions),
            "finger_root_distance_m": _norm(
                _subtract(finger_frames[1]["position"], finger_frames[0]["position"])
            ),
            "open": open_observed,
        }

    def _initialize_contact_reporting(self, stage: Any) -> None:
        try:
            from omni.physics.core import get_physics_simulation_interface
            from pxr import PhysxSchema
            from isaacsim.core.api.sensors import RigidContactView
        except (ImportError, ModuleNotFoundError) as exc:
            raise RuntimeError("Isaac contact reporting APIs are unavailable") from exc
        handle_path = SEKTION_TOP_DRAWER_RUNTIME_ROOT + "/drawer_handle_top"
        roots = [stage.GetPrimAtPath(path) for path in (*self._finger_root_paths, handle_path)]
        if any(not prim.IsValid() for prim in roots):
            raise RuntimeError("contact-report prims are missing")
        for prim in roots:
            PhysxSchema.PhysxContactReportAPI.Apply(prim).CreateThresholdAttr().Set(0.0)
        self._contact_interface = get_physics_simulation_interface()
        self._contact_subscription = self._contact_interface.subscribe_physics_contact_report_events(lambda *_args: None)
        self._contact_views = []
        for index, finger_path in enumerate(self._finger_root_paths):
            view = RigidContactView(
                prim_paths_expr=finger_path,
                filter_paths_expr=[handle_path],
                name=f"p1_4b_finger_handle_contact_{index}",
                max_contact_count=32,
            )
            view.initialize(self._simulation.physics_sim_view)
            self._contact_views.append((finger_path, view))
        self._handle_contact_view = RigidContactView(
            prim_paths_expr=handle_path,
            filter_paths_expr=list(self._finger_root_paths),
            name="p1_4b_handle_finger_reaction_contacts",
            max_contact_count=64,
        )
        self._handle_contact_view.initialize(self._simulation.physics_sim_view)

    def read_contacts(self) -> Mapping[str, Any]:
        import numpy as np
        from pxr import Usd, UsdGeom

        if len(self._contact_views) != 2 or self._handle_contact_view is None:
            raise RuntimeError("finger and handle contact views are required")
        handle_matrix = np.asarray(
            self._handle_contact_view.get_contact_force_matrix(dt=self.config.physics_dt),
            dtype=float,
        )
        if handle_matrix.shape != (1, 2, 3) or not np.isfinite(handle_matrix).all():
            raise RuntimeError("invalid handle-side contact force matrix")
        samples: list[dict[str, Any]] = []
        contact_flags = []
        for finger_index, (finger_path, view) in enumerate(self._contact_views):
            matrix = np.asarray(view.get_contact_force_matrix(dt=self.config.physics_dt), dtype=float)
            if matrix.ndim != 3 or matrix.shape[0] < 1 or matrix.shape[1] < 1 or matrix.shape[2] != 3:
                raise RuntimeError("invalid finger contact force matrix")
            forces = matrix[0]
            if not np.isfinite(forces).all():
                raise ValueError("non-finite finger contact force")
            magnitudes = np.linalg.norm(forces, axis=1)
            index = int(np.argmax(magnitudes))
            magnitude = float(magnitudes[index])
            force_on_finger = tuple(float(value) for value in forces[index])
            force_on_handle = tuple(float(value) for value in handle_matrix[0, finger_index])
            contact = magnitude > 1.0e-6
            contact_flags.append(contact)
            finger_prim = self._stage.GetPrimAtPath(finger_path)
            finger_matrix = UsdGeom.Xformable(finger_prim).ComputeLocalToWorldTransform(
                Usd.TimeCode.Default()
            )
            finger_position = finger_matrix.ExtractTranslation()
            manifold = self._contact_manifold(view)
            points = [tuple(item["point_world"]) for item in manifold]
            normals = [tuple(item["normal_world"]) for item in manifold]
            fallback_point = tuple(float(finger_position[item]) for item in range(3))
            point = next(
                (item for item in points if _norm(item) > 1.0e-12),
                fallback_point,
            ) if contact else fallback_point
            normal = next(
                (item for item in normals if _norm(item) > 1.0e-12),
                None,
            ) if contact else None
            if normal is not None:
                normal = _unit(normal, "contact normal")
                normal_scalar = _dot(force_on_finger, normal)
                normal_force = _scale(normal, normal_scalar)
                friction_force = _subtract(force_on_finger, normal_force)
            else:
                normal_force = None
                friction_force = None
            friction_capacity = None
            if normal_force is not None and self._material_audit is not None:
                coefficient = self._material_audit["effective_pair_policy"].get("static_friction")
                if coefficient is not None:
                    friction_capacity = float(coefficient) * _norm(normal_force)
            point_local = None
            surface_name = None
            expected_normal = None
            surface_classification = None
            if self._grasp_analysis is not None:
                point_local = self._contact_point_handle_local(point)
                surface_classification = self._classify_handle_surface(point_local)
                surface_name = surface_classification["surface"]
                expected_normal = surface_classification.get("normal_world")
            reaction_residual = _norm(_add(force_on_finger, force_on_handle))
            reaction_scale = max(_norm(force_on_finger), _norm(force_on_handle), 1.0e-12)
            samples.append(
                {
                    "finger": finger_path,
                    "handle": SEKTION_TOP_DRAWER_RUNTIME_ROOT + "/drawer_handle_top",
                    "finger_position": [
                        float(finger_position[index]) for index in range(3)
                    ],
                    "contact_point_world": point,
                    "contact_point_handle_local": point_local,
                    "contact_normal_world": normal,
                    "expected_contact_normal_world": expected_normal,
                    "surface": surface_name,
                    "surface_classification": surface_classification,
                    "reported_force_world": force_on_finger,
                    "reported_force_body": finger_path,
                    "reported_force_semantics": "force_on_view_prim_world",
                    "force": force_on_finger,
                    "total_contact_force": force_on_finger,
                    "normal_force_on_finger_world": normal_force,
                    "friction_force_on_finger_world": friction_force,
                    "normal_force": normal_force,
                    "friction_force": friction_force,
                    "friction_capacity": friction_capacity,
                    "force_on_finger_world": force_on_finger,
                    "force_on_handle_world": force_on_handle,
                    "force_on_handle": force_on_handle,
                    "newton_pair_residual_n": reaction_residual,
                    "newton_pair_relative_residual": reaction_residual / reaction_scale,
                    "contact_manifold": manifold,
                    "force_magnitude": magnitude,
                    "contact_detail_available": bool(contact and points and normals and normal is not None),
                }
            )
        handle_force = tuple(
            sum(sample["force_on_handle"][axis] for sample, contact in zip(samples, contact_flags, strict=True) if contact)
            for axis in range(3)
        )
        opening_axis = None
        axial_traction = None
        if self._grasp_analysis is not None:
            opening_axis = _unit(
                tuple(self._grasp_analysis["opening_axis_world"]),
                "opening axis",
            )
            axial_traction = _dot(handle_force, opening_axis)
        surfaces = [
            sample.get("surface")
            for sample, contact in zip(samples, contact_flags, strict=True)
            if contact and sample.get("surface") is not None
        ]
        selected = self._grasp_analysis.get("selected_candidate") if self._grasp_analysis else None
        required_surfaces = list(selected.get("handle_surface_pair", ())) if isinstance(selected, Mapping) else []
        surface_pair_valid = (
            len(surfaces) == 2
            and len(required_surfaces) == 2
            and sorted(str(item) for item in surfaces) == sorted(str(item) for item in required_surfaces)
        )
        normals_present = all(contact_flags) and all(
            sample.get("contact_normal_world") is not None
            for sample, contact in zip(samples, contact_flags, strict=True)
            if contact
        )
        normal_dot = None
        if all(sample.get("contact_normal_world") is not None for sample in samples):
            normal_dot = _dot(
                tuple(samples[0]["contact_normal_world"]),
                tuple(samples[1]["contact_normal_world"]),
            )
        opposed_contact = surface_pair_valid and normal_dot is not None and normal_dot < 0.0
        return {
            "left_contact": contact_flags[0],
            "right_contact": contact_flags[1],
            "force_valid": True,
            "nonzero_force": all(contact_flags),
            "force_samples": samples,
            "contact_detail_available": normals_present,
            "surface_pair_valid": surface_pair_valid,
            "normal_dot_left_right": normal_dot,
            "opposed_contact": opposed_contact if surfaces else None,
            "net_force_on_handle": handle_force,
            "opening_axis_world": opening_axis,
            "axial_traction": axial_traction,
            "axial_traction_available": axial_traction is not None,
            "force_convention": {
                "api": "RigidContactView.get_contact_force_matrix",
                "documented_semantics": "force_on_view_prim_world",
                "finger_view_body": "finger",
                "handle_view_body": "drawer_handle_top",
                "force_on_handle_source": "direct_handle_side_contact_view",
                "newton_pair_observed": any(contact_flags)
                and all(
                    sample["newton_pair_relative_residual"] <= 1.0e-3
                    for sample, contact in zip(samples, contact_flags, strict=True)
                    if contact
                ),
            },
            "contact_pairs": [
                {"finger": sample["finger"], "handle": sample["handle"]}
                for sample, contact in zip(samples, contact_flags, strict=True)
                if contact
            ],
        }

    def _contact_point_handle_local(
        self,
        point_world: tuple[float, float, float],
    ) -> tuple[float, float, float]:
        current_frame = self._current_handle_geometry_frame()
        handle_transform = current_frame.get("transform")
        if not isinstance(handle_transform, (list, tuple)) or len(handle_transform) != 16:
            raise RuntimeError("current handle geometry frame transform is invalid")
        return _transform_point(
            _inverse_transform(tuple(handle_transform), "current handle geometry frame"),
            point_world,
        )

    def _current_handle_geometry_frame(self) -> Mapping[str, Any]:
        handle_path = (
            self._resolution.handle_link_prim
            if self._resolution is not None and self._resolution.handle_link_prim
            else SEKTION_TOP_DRAWER_RUNTIME_ROOT + "/drawer_handle_top"
        )
        return self._read_prim_frame(handle_path)

    def _contact_manifold(self, view: Any) -> list[dict[str, Any]]:
        import numpy as np

        raw = view.get_contact_force_data(dt=self.config.physics_dt)
        if raw is None or len(raw) != 6:
            return []
        normal_forces, points, normals, separations, counts, starts = (
            np.asarray(value, dtype=float) for value in raw
        )
        if not all(
            np.isfinite(value).all()
            for value in (normal_forces, points, normals, separations, counts, starts)
        ):
            raise ValueError("non-finite contact manifold data")
        count = int(counts.reshape(-1)[0]) if counts.size else 0
        start = int(starts.reshape(-1)[0]) if starts.size else 0
        result = []
        for offset in range(count):
            index = start + offset
            result.append(
                {
                    "normal_force_n": float(normal_forces.reshape(-1)[index]),
                    "point_world": tuple(float(value) for value in points.reshape((-1, 3))[index]),
                    "normal_world": tuple(float(value) for value in normals.reshape((-1, 3))[index]),
                    "separation_m": float(separations.reshape(-1)[index]),
                }
            )
        return result

    def _classify_handle_surface(
        self,
        point_local: tuple[float, float, float],
    ) -> dict[str, Any]:
        if self._grasp_analysis is None:
            return {"surface": "edge/ambiguous", "reason": "grasp_analysis_unavailable"}
        definitions: dict[str, dict[str, Any]] = {}
        for candidate in self._grasp_analysis.get("candidates", ()):
            if not isinstance(candidate, Mapping):
                continue
            surface = candidate.get("surface_geometry")
            if not isinstance(surface, Mapping):
                continue
            axis = surface.get("axis_local")
            if not isinstance(axis, (list, tuple)) or len(axis) != 3:
                continue
            for side in ("negative", "positive"):
                face = surface.get(side)
                if not isinstance(face, Mapping) or not face.get("name"):
                    continue
                definitions[str(face["name"])] = {
                    "axis_local": tuple(axis),
                    "coordinate": float(face["coordinate"]),
                    "normal_world": face["normal_world"],
                }
        spans = [
            float(candidate["surface_geometry"]["span_m"])
            for candidate in self._grasp_analysis.get("candidates", ())
            if isinstance(candidate, Mapping)
            and isinstance(candidate.get("surface_geometry"), Mapping)
            and float(candidate["surface_geometry"].get("span_m", 0.0)) > 0.0
        ]
        if len(definitions) != 6 or not spans:
            return {"surface": "edge/ambiguous", "reason": "surface_geometry_unavailable"}
        tolerance = min(spans) * 0.1
        distances = {
            name: abs(_dot(point_local, definition["axis_local"]) - definition["coordinate"])
            for name, definition in definitions.items()
        }
        nearby = sorted(name for name, distance in distances.items() if distance <= tolerance)
        if len(nearby) != 1:
            return {
                "surface": "edge/ambiguous",
                "reason": "multiple_nearby_surfaces" if nearby else "not_near_collision_surface",
                "nearby_surfaces": nearby,
                "distances_m": distances,
                "tolerance_m": tolerance,
            }
        surface_name = nearby[0]
        return {
            "surface": surface_name,
            "normal_world": definitions[surface_name]["normal_world"],
            "distance_m": distances[surface_name],
            "distances_m": distances,
            "tolerance_m": tolerance,
        }

    def step_physics(self) -> None:
        if self._simulation is None:
            raise RuntimeError("simulation is not initialized")
        self._simulation.step(render=False)
        self._physics_steps += 1

    def write_counters(self) -> Mapping[str, int]:
        return {
            "reset_joint_write_count": self._reset_joint_write_count,
            "execution_joint_write_count": self._execution_joint_write_count,
        }

    def isaac_sim_version(self) -> str:
        module = None
        try:
            module = import_module("isaacsim")
            version = getattr(module, "__version__", None)
            if version:
                return str(version)
        except (ImportError, ModuleNotFoundError):
            module = None
        try:
            version = distribution_version("isaacsim")
            if version:
                return str(version)
        except PackageNotFoundError:
            pass
        if module is not None:
            module_file = getattr(module, "__file__", None)
            if module_file:
                version_path = Path(module_file).with_name("VERSION")
                try:
                    version = version_path.read_text(encoding="utf-8").strip()
                except (OSError, UnicodeError):
                    pass
                else:
                    if version:
                        return version
        return "unknown"

    def diagnostics(self, binding: IsaacArticulationBinding) -> Mapping[str, Any]:
        frame = self.read_handle_frame()
        robot_position, robot_orientation = self._robot.get_world_pose()
        analysis = self._ensure_grasp_analysis()
        selected = analysis.get("selected_candidate")
        return {
            "binding_id": binding.binding_id,
            "runtime_asset_root_prim": SEKTION_TOP_DRAWER_RUNTIME_ROOT,
            "asset_relative_path": binding.asset_relative_path,
            "fixture_cabinet_position_m": self.config.fixture_cabinet_position_m,
            "fixture_robot_base_position_m": self.config.fixture_robot_base_position_m,
            "handle_frame_position_m": frame["position"],
            "pre_robot_handle_frame_position_m": self._pre_robot_handle_frame_position,
            "post_robot_handle_frame_position_m": self._post_robot_handle_frame_position,
            "robot_world_position_m": [float(value) for value in robot_position],
            "robot_world_orientation_wxyz": [float(value) for value in robot_orientation],
            "drawer_joint_position": self.read_joint(),
            "grasp_analysis": _bounded_diagnostics(analysis, "grasp_analysis"),
            "selected_grasp": _bounded_diagnostics(selected, "selected_grasp"),
            "eef_frame_path": self._eef_frame_path,
            "eef_kinematics_frame": self._eef_kinematics_frame,
            "configured_kinematic_frame": self._eef_kinematics_frame,
            "controller_target_frame": self._eef_kinematics_frame,
            "observed_usd_frame": self._eef_frame_path,
            "observation_frame": self._observation_frame,
            "lula_frame_names": self._lula_frame_names,
            "lula_base_position_m": self._lula_base_position,
            "lula_base_orientation_wxyz": self._lula_base_orientation,
            "base_pose_synchronized": (
                self._lula_base_position == tuple(float(value) for value in robot_position)
                and self._lula_base_orientation == tuple(float(value) for value in robot_orientation)
            ),
            "frame_transform_geometry_to_controller": self._geometry_to_controller_transform,
            "frame_transform_diagnostics": _bounded_diagnostics(
                self._frame_transform_diagnostics,
                "frame_transform_diagnostics",
            ),
            "ik_api": self.ik_api_diagnostics(),
            "ik_continuity_policy": _continuity_policy(),
            "material_audit": self._read_material_audit(),
            "robot_asset_source": self._robot_asset_source,
            "official_franka_asset": self._robot_asset_source == "nucleus_franka_usd",
            "contact_views": len(self._contact_views),
            "handle_contact_view": self._handle_contact_view is not None,
            "reset_joint_write_count": self._reset_joint_write_count,
            "execution_joint_write_count": self._execution_joint_write_count,
            "runtime_physics_steps": self._physics_steps,
            "asset_root_resolution_status": self._asset_diagnostics.get("asset_root_resolution_status"),
        }

    def _settle(self, steps: int) -> None:
        for _ in range(steps):
            self.step_physics()

    def close(self) -> None:
        subscription, interface = self._contact_subscription, self._contact_interface
        simulation, app = self._simulation, self._app
        self._contact_subscription = None
        self._contact_interface = None
        self._contact_views = []
        self._handle_contact_view = None
        self._simulation = None
        self._app = None
        if subscription is not None:
            try:
                if hasattr(subscription, "unsubscribe"):
                    subscription.unsubscribe()
                elif interface is not None:
                    interface.unsubscribe_physics_contact_report_events(subscription)
            except Exception:
                pass
        if simulation is not None:
            try:
                simulation.stop()
            except Exception:
                pass
        if app is not None:
            app.close()
        self._stage = None
        self._cabinet = None
        self._robot = None
        self._gripper = None
        self._kinematics = None
        self._resolution = None
        self._eef_frame_path = None
        self._eef_kinematics_frame = None
        self._observation_frame = None
        self._lula_frame_names = ()
        self._geometry_to_controller_transform = None
        self._frame_transform_diagnostics = {}
        self._lula_base_position = None
        self._lula_base_orientation = None
        self._handle_geometry = None
        self._gripper_geometry = None
        self._grasp_analysis = None
        self._material_audit = None


__all__ = ["GraspTopologyConfig", "IsaacInteractionExecutor"]
