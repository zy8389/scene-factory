"""Pure Python validation for optional scripted-controller settings."""
from __future__ import annotations

import math
from typing import Any


_DEFAULT_SETTINGS: dict[str, float | int] = {
    "pre_grasp_position_tolerance_m": 0.04,
    "approach_position_tolerance_m": 0.025,
    "max_reach_steps": 180,
    "grasp_hold_closing_margin_m": 0.007,
    "arm_stiffness": 1000.0,
    "arm_damping": 200.0,
    "arm_max_force": 2000.0,
}
_REACH_KEYS = (
    "pre_grasp_position_tolerance_m",
    "approach_position_tolerance_m",
    "max_reach_steps",
)


def normalize_controller_settings(raw: Any) -> dict[str, float | int]:
    """Return a fresh, complete configuration without changing historical defaults.

    Values must be finite Python numbers; booleans and numeric strings are not
    accepted. Reach steps must be an integer of at least 20. The closing margin
    can be zero; all other numeric settings must be strictly positive.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("controller settings must be an object or None")

    unknown = [key for key in raw if key not in _DEFAULT_SETTINGS]
    if unknown:
        names = ", ".join(sorted(repr(key) for key in unknown))
        raise ValueError(f"unknown controller settings: {names}")

    result = dict(_DEFAULT_SETTINGS)
    for key, value in raw.items():
        if key == "max_reach_steps":
            if isinstance(value, bool) or not isinstance(value, int) or value < 20:
                raise ValueError("controller.max_reach_steps must be an integer of at least 20")
            result[key] = value
            continue

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"controller.{key} must be a finite number")
        try:
            normalized = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError(f"controller.{key} must be a finite number") from exc
        if not math.isfinite(normalized):
            raise ValueError(f"controller.{key} must be a finite number")

        if key == "grasp_hold_closing_margin_m":
            if normalized < 0.0:
                raise ValueError(f"controller.{key} must be nonnegative")
        elif normalized <= 0.0:
            raise ValueError(f"controller.{key} must be positive")
        result[key] = normalized
    return result


def controller_reach_kwargs(settings: Any) -> dict[str, float | int]:
    """Validate the entire configuration, then return only controller reach options."""
    normalized = normalize_controller_settings(settings)
    return {key: normalized[key] for key in _REACH_KEYS}
