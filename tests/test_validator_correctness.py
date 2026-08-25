from __future__ import annotations

import unittest

from scene_factory.models import (
    AssetRecord,
    CompiledScene,
    PlacedObject,
    Pose,
    SupportSurface,
)
from scene_factory.registry import AssetRegistry
from scene_factory.validation import SceneValidator


def _scene(*objects: PlacedObject, room=(4.0, 4.0, 3.0)) -> CompiledScene:
    return CompiledScene(
        scene_id="validator-test",
        seed=1,
        recipe_name="validator-test",
        room_type="test",
        room_dimensions_m=room,
        event="test",
        description="",
        objects=objects,
        task={},
    )


class ValidatorCorrectnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.support_asset = AssetRecord(
            asset_id="support",
            category="support",
            bbox_m=(1.2, 0.8, 1.0),
            support_surfaces=(
                SupportSurface(name="top", center=(0.2, 0.0, 0.5), size=(1.0, 0.4)),
            ),
        )
        self.subject_asset = AssetRecord(
            asset_id="subject",
            category="subject",
            bbox_m=(0.35, 0.15, 0.2),
        )
        self.validator = SceneValidator(
            AssetRegistry([self.support_asset, self.subject_asset]), tolerance_m=0.001
        )

    def test_rotated_subject_footprint_must_fit_offset_support_surface(self) -> None:
        support = PlacedObject(
            "table",
            "support",
            "support",
            self.support_asset.bbox_m,
            Pose((0.0, 0.0, 0.5), 90.0),
            False,
            "floor",
        )
        # Local support coordinates are (0.0, 0.05), but the subject is rotated
        # 90 degrees relative to the surface, so its long half extent crosses Y.
        subject = PlacedObject(
            "item",
            "subject",
            "subject",
            self.subject_asset.bbox_m,
            Pose((-0.05, 0.2, 1.1), 180.0),
            True,
            "table:top",
        )
        report = self.validator.validate(_scene(support, subject))
        self.assertIn("outside_support_surface", {issue.code for issue in report.issues})

    def test_rotated_long_object_is_checked_against_room_bounds(self) -> None:
        item = PlacedObject(
            "long_item",
            "subject",
            "subject",
            (0.2, 1.5, 0.2),
            Pose((0.4, 0.0, 0.1), 90.0),
            True,
            "floor",
        )
        report = self.validator.validate(_scene(item, room=(2.0, 2.0, 2.0)))
        self.assertIn("out_of_bounds_x", {issue.code for issue in report.issues})


if __name__ == "__main__":
    unittest.main()
