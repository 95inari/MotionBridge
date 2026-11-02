import unittest

from motion_bridge.canonical.rig_spec import JOINTS, JointSpec, joint_path, validate_rig_spec


class CanonicalRigSpecTests(unittest.TestCase):
    def test_spec_is_valid_and_fits_pose_packet(self) -> None:
        validate_rig_spec()
        self.assertLessEqual(len(JOINTS), 60)
        self.assertEqual("", joint_path(0))
        self.assertEqual("Hips/Spine/Chest/UpperChest/Neck/Head", joint_path(6))

    def test_required_unity_humanoid_bones_are_present(self) -> None:
        human_bones = {joint.human_bone for joint in JOINTS if joint.human_bone}
        required = {
            "Hips",
            "Spine",
            "Chest",
            "Head",
            "LeftUpperArm",
            "LeftLowerArm",
            "LeftHand",
            "RightUpperArm",
            "RightLowerArm",
            "RightHand",
            "LeftUpperLeg",
            "LeftLowerLeg",
            "LeftFoot",
            "RightUpperLeg",
            "RightLowerLeg",
            "RightFoot",
        }
        self.assertTrue(required.issubset(human_bones))

    def test_limbs_are_mirrored(self) -> None:
        by_name = {joint.name: joint for joint in JOINTS}
        for left_name in ("LeftUpperArm", "LeftLowerArm", "LeftHand", "LeftUpperLeg", "LeftLowerLeg", "LeftFoot"):
            right_name = left_name.replace("Left", "Right", 1)
            left = by_name[left_name]
            right = by_name[right_name]
            self.assertAlmostEqual(left.head[0], -right.head[0])  # type: ignore[index]
            self.assertAlmostEqual(left.head[1], right.head[1])  # type: ignore[index]
            self.assertAlmostEqual(left.head[2], right.head[2])  # type: ignore[index]

    def test_invalid_parent_order_is_rejected(self) -> None:
        invalid = (
            JOINTS[0],
            JointSpec(1, "Hips", "Hips", 1, (0.0, 0.0, 1.0), (0.0, 0.0, 1.1), True),
        )
        with self.assertRaises(ValueError):
            validate_rig_spec(invalid)


if __name__ == "__main__":
    unittest.main()
