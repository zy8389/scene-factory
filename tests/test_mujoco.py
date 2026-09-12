from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from scene_factory.agent import SceneFactoryEnv
from scene_factory.backends import MujocoBackend
from scene_factory.exporters import MujocoMjcfExporter
from scene_factory.factory import SceneFactory
from scene_factory.webapp import SceneWebApplication


class MujocoIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with patch.dict("os.environ", {"SCENE_FACTORY_LLM_MODE": "off"}):
            cls.factory = SceneFactory()

    def test_mjcf_loads_and_steps_in_mujoco(self) -> None:
        import mujoco
        import numpy as np

        result = self.factory.build_from_recipe("kitchen_after_cooking", 42)
        xml = MujocoMjcfExporter(self.factory.registry).to_string(result.scene)
        root = ET.fromstring(xml)
        self.assertEqual(root.tag, "mujoco")
        self.assertEqual(len(root.findall("./worldbody/body")), len(result.scene.objects))

        model = mujoco.MjModel.from_xml_string(xml)
        data = mujoco.MjData(model)
        for _ in range(20):
            mujoco.mj_step(model, data)
        self.assertTrue(np.isfinite(data.qpos).all())

    def test_scene_factory_env_uses_mujoco_by_default(self) -> None:
        import numpy as np

        result = self.factory.build_from_recipe("living_room_returned_home", 19)
        with tempfile.TemporaryDirectory() as directory:
            files = self.factory.write_result(result, directory, export_mjcf=True)
            env = SceneFactoryEnv(files["layout"])
            self.assertIsInstance(env.backend, MujocoBackend)
            observation, info = env.reset()
            self.assertEqual(info["backend"], "mujoco")
            self.assertEqual(len(observation["object_poses"]), len(result.scene.objects))
            observation, _, terminated, truncated, _ = env.step(None)
            self.assertFalse(terminated)
            self.assertFalse(truncated)
            self.assertTrue(np.isfinite(observation["proprioception"]["qpos"]).all())
            env.close()

    def test_web_defaults_to_mjcf_and_exposes_whitelisted_visuals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"SCENE_FACTORY_LLM_MODE": "off"}):
                app = SceneWebApplication(directory)
            response = app.generate(
                {
                    "prompt": "刚做完饭，厨房里还有碗、盘子、杯子和刀。",
                    "seed": 42,
                    "count": 1,
                    "export_usd": False,
                }
            )
            item = response["items"][0]
            self.assertIn("mjcf", item["files"])
            self.assertIsNotNone(item["assets"]["mug_001"]["visual_url"])
            visual = app.resolve_asset("mug_001/visual")
            self.assertTrue(visual.is_file())
            with self.assertRaises(FileNotFoundError):
                app.resolve_asset("../registry.jsonl")


if __name__ == "__main__":
    unittest.main()
