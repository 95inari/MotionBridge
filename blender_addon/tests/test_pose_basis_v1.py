import math
import unittest

from motion_bridge.live.basis_v1 import (
    BLENDER_TO_UNITY,
    FBX_LOCAL_HANDEDNESS,
    FBX_ROOT_TO_UNITY,
    blender_to_unity_vector,
    global_rest_relative_delta,
    hips_local_delta_to_unity,
    matrix3_multiply,
    quaternion_to_matrix3,
    trajectory_rotation_to_unity,
)


def _assert_quaternion_equivalent(
    case: unittest.TestCase,
    actual: tuple[float, float, float, float],
    expected: tuple[float, float, float, float],
    places: int = 7,
) -> None:
    dot = sum(left * right for left, right in zip(actual, expected))
    case.assertAlmostEqual(1.0, abs(dot), places=places)


class PoseBasisV1Tests(unittest.TestCase):
    def test_fixed_basis_is_r_times_f(self) -> None:
        self.assertEqual(
            BLENDER_TO_UNITY,
            matrix3_multiply(FBX_ROOT_TO_UNITY, FBX_LOCAL_HANDEDNESS),
        )

    def test_trajectory_and_hips_vectors_use_distinct_bases(self) -> None:
        self.assertEqual((-1.0, 3.0, -2.0), blender_to_unity_vector((1.0, 2.0, 3.0)))
        self.assertEqual((-1.0, 2.0, 3.0), hips_local_delta_to_unity((1.0, 2.0, 3.0)))

    def test_blender_z_yaw_becomes_negative_unity_y_yaw(self) -> None:
        half = math.sqrt(0.5)
        blender_yaw = quaternion_to_matrix3((0.0, 0.0, half, half))
        actual = trajectory_rotation_to_unity(blender_yaw)
        _assert_quaternion_equivalent(self, actual, (0.0, -half, 0.0, half))

    def test_global_delta_removes_nonidentity_rest_before_basis_conversion(self) -> None:
        sine_15 = math.sin(math.radians(15.0))
        cosine_15 = math.cos(math.radians(15.0))
        rest = quaternion_to_matrix3((sine_15, 0.0, 0.0, cosine_15))
        half = math.sqrt(0.5)
        delta = quaternion_to_matrix3((0.0, 0.0, half, half))
        pose = matrix3_multiply(delta, rest)

        actual = global_rest_relative_delta(pose, rest)

        _assert_quaternion_equivalent(self, actual, (0.0, -half, 0.0, half))

    def test_rest_pose_delta_is_identity(self) -> None:
        rest = quaternion_to_matrix3((0.2, -0.3, 0.1, 0.9))
        _assert_quaternion_equivalent(
            self,
            global_rest_relative_delta(rest, rest),
            (0.0, 0.0, 0.0, 1.0),
        )


if __name__ == "__main__":
    unittest.main()
