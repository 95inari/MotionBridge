from __future__ import annotations

import unittest

from motion_bridge.retargeting.presets import infer_humanoid_mapping_from_names


class RetargetPresetTests(unittest.TestCase):
    def test_exact_unity_names_map_directly(self) -> None:
        names = (
            "Hips",
            "Spine",
            "Chest",
            "Neck",
            "Head",
            "LeftUpperArm",
            "LeftLowerArm",
            "RightUpperArm",
            "RightLowerArm",
        )
        mapping = infer_humanoid_mapping_from_names(names, "EXACT")

        self.assertEqual(mapping["LeftUpperArm"], "LeftUpperArm")
        self.assertEqual(mapping["RightLowerArm"], "RightLowerArm")

    def test_mixamo_names_accept_namespace_and_map_semantics(self) -> None:
        names = (
            "Armature|mixamorig:Hips",
            "mixamorig:Spine",
            "mixamorig:Spine1",
            "mixamorig:Spine2",
            "mixamorig:LeftArm",
            "mixamorig:LeftForeArm",
            "mixamorig:LeftUpLeg",
            "mixamorig:LeftLeg",
            "mixamorig:LeftToeBase",
        )
        mapping = infer_humanoid_mapping_from_names(names, "MIXAMO")

        self.assertEqual(mapping["Hips"], "Armature|mixamorig:Hips")
        self.assertEqual(mapping["Chest"], "mixamorig:Spine1")
        self.assertEqual(mapping["UpperChest"], "mixamorig:Spine2")
        self.assertEqual(mapping["LeftUpperArm"], "mixamorig:LeftArm")
        self.assertEqual(mapping["LeftLowerArm"], "mixamorig:LeftForeArm")
        self.assertEqual(mapping["LeftUpperLeg"], "mixamorig:LeftUpLeg")

    def test_ambiguous_normalized_name_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Ambiguous source bones for Hips"):
            infer_humanoid_mapping_from_names(
                ("Hips", "mixamorig:Hips"),
                "AUTO",
            )

    def test_generic_names_map_shifted_spine_thighs_and_shins(self) -> None:
        names = (
            "Hips",
            "Spine1",
            "Spine2",
            "Spine3",
            "Spine4",
            "Neck",
            "Head",
            "LeftThigh",
            "LeftShin",
            "LeftFoot",
            "LeftToe",
            "RightThigh",
            "RightShin",
            "RightFoot",
            "RightToe",
        )

        mapping = infer_humanoid_mapping_from_names(names, "AUTO")

        self.assertEqual(mapping["Spine"], "Spine1")
        self.assertEqual(mapping["Chest"], "Spine2")
        self.assertEqual(mapping["UpperChest"], "Spine3")
        self.assertEqual(mapping["LeftUpperLeg"], "LeftThigh")
        self.assertEqual(mapping["LeftLowerLeg"], "LeftShin")
        self.assertEqual(mapping["LeftToes"], "LeftToe")
        self.assertEqual(mapping["RightUpperLeg"], "RightThigh")
        self.assertEqual(mapping["RightLowerLeg"], "RightShin")
        self.assertEqual(mapping["RightToes"], "RightToe")

    def test_unknown_preset_and_invalid_names_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            infer_humanoid_mapping_from_names(("Hips",), "FUZZY")
        with self.assertRaises(ValueError):
            infer_humanoid_mapping_from_names(("",), "AUTO")


if __name__ == "__main__":
    unittest.main()
