import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


# Import this pure boundary without the editing package's Blender-only exports.
spec = importlib.util.spec_from_file_location(
    "pose_api", Path(__file__).resolve().parents[1] / "motion_bridge/editing/pose_api.py"
)
assert spec is not None and spec.loader is not None
pose_api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pose_api)
pose_bone_selected = pose_api.pose_bone_selected
set_pose_bone_selected = pose_api.set_pose_bone_selected


class PoseSelectionCompatibilityTests(unittest.TestCase):
    def test_blender_45_uses_bone_selection(self):
        pose_bone = SimpleNamespace(bone=SimpleNamespace(select=False))
        self.assertFalse(pose_bone_selected(pose_bone))
        set_pose_bone_selected(pose_bone, True)
        self.assertTrue(pose_bone_selected(pose_bone))
        self.assertTrue(pose_bone.bone.select)
        self.assertFalse(hasattr(pose_bone, "select"))
        set_pose_bone_selected(pose_bone, False)
        self.assertFalse(pose_bone_selected(pose_bone))

    def test_blender_5_uses_pose_selection(self):
        pose_bone = SimpleNamespace(select=False, bone=SimpleNamespace())
        self.assertFalse(pose_bone_selected(pose_bone))
        set_pose_bone_selected(pose_bone, True)
        self.assertTrue(pose_bone_selected(pose_bone))
        self.assertTrue(pose_bone.select)
        self.assertFalse(hasattr(pose_bone.bone, "select"))
        set_pose_bone_selected(pose_bone, False)
        self.assertFalse(pose_bone_selected(pose_bone))

    def test_pose_selection_wins_when_both_properties_exist(self):
        pose_bone = SimpleNamespace(select=False, bone=SimpleNamespace(select=True))
        self.assertFalse(pose_bone_selected(pose_bone))
        set_pose_bone_selected(pose_bone, True)
        set_pose_bone_selected(pose_bone, False)
        self.assertFalse(pose_bone.select)
        self.assertTrue(pose_bone.bone.select)


if __name__ == "__main__":
    unittest.main()
