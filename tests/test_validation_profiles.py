from __future__ import annotations

import unittest

from scene_factory.asset_profiles import sanity_check_bbox, validation_profile


class ValidationProfileTests(unittest.TestCase):
    def test_drop_profiles_are_distinct(self) -> None:
        self.assertEqual(validation_profile("drop").steps, 360)
        self.assertTrue(validation_profile("drop_thin_object").thin_object)
        self.assertGreater(validation_profile("drop_thin_object").steps, validation_profile("drop").steps)

    def test_category_specific_bbox_policy(self) -> None:
        self.assertEqual(sanity_check_bbox("mug", [0.1, 0.1, 0.1]), [])
        self.assertTrue(sanity_check_bbox("kitchen_knife", [0.01, 0.01, 0.01]))
        self.assertTrue(sanity_check_bbox("keys", [2.0, 0.1, 0.1]))


if __name__ == "__main__":
    unittest.main()
