from __future__ import annotations

import unittest

from scene_factory.asset_profiles import collision_profile, category_profile


class CollisionProfileTests(unittest.TestCase):
    def test_p0_profiles_declare_scope(self) -> None:
        self.assertEqual(collision_profile("concave_container_l1").strategy, "authored_convex_decomposition_mesh")
        self.assertIn("containment", collision_profile("concave_container_l1").unsupported_use_cases)
        self.assertIn("deformable_simulation", collision_profile("irregular_soft_object_proxy_l1").unsupported_use_cases)
        self.assertEqual(category_profile("kitchen_knife")["validation"], "drop_thin_object")

    def test_unknown_profile_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            collision_profile("unknown")


if __name__ == "__main__":
    unittest.main()
