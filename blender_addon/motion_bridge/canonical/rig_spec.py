from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Final


SKELETON_ID: Final = "mb-canonical-humanoid-v1"
SKELETON_VERSION: Final = 1
SKELETON_HASH: Final = (
    "2d09ba30b066ac812b0b04c1eaa02e094faf5e3de7b559f81b4f701510f20930"
)
MAX_POSE_JOINTS: Final = 60

Vec3 = tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class JointSpec:
    index: int
    name: str
    human_bone: str | None
    parent_index: int
    head: Vec3 | None
    tail: Vec3 | None
    required: bool

    @property
    def is_synthetic_root(self) -> bool:
        return self.index == 0


JOINTS: Final[tuple[JointSpec, ...]] = (
    JointSpec(0, "Root", None, -1, None, None, True),
    JointSpec(1, "Hips", "Hips", 0, (-0.0, 0.0, 1.00), (0.0, 0.0, 1.12), True),
    JointSpec(2, "Spine", "Spine", 1, (0.0, 0.0, 1.12), (0.0, 0.0, 1.34), True),
    JointSpec(3, "Chest", "Chest", 2, (0.0, 0.0, 1.34), (0.0, 0.0, 1.50), True),
    JointSpec(4, "UpperChest", "UpperChest", 3, (0.0, 0.0, 1.50), (0.0, 0.0, 1.62), False),
    JointSpec(5, "Neck", "Neck", 4, (0.0, 0.0, 1.62), (0.0, 0.0, 1.76), True),
    JointSpec(6, "Head", "Head", 5, (0.0, 0.0, 1.76), (0.0, 0.0, 1.94), True),
    JointSpec(7, "LeftShoulder", "LeftShoulder", 4, (0.13, 0.0, 1.56), (0.24, 0.0, 1.56), False),
    JointSpec(8, "LeftUpperArm", "LeftUpperArm", 7, (0.24, 0.0, 1.56), (0.55, 0.0, 1.56), True),
    JointSpec(9, "LeftLowerArm", "LeftLowerArm", 8, (0.55, 0.0, 1.56), (0.82, 0.0, 1.56), True),
    JointSpec(10, "LeftHand", "LeftHand", 9, (0.82, 0.0, 1.56), (0.94, 0.0, 1.56), True),
    JointSpec(11, "RightShoulder", "RightShoulder", 4, (-0.13, 0.0, 1.56), (-0.24, 0.0, 1.56), False),
    JointSpec(12, "RightUpperArm", "RightUpperArm", 11, (-0.24, 0.0, 1.56), (-0.55, 0.0, 1.56), True),
    JointSpec(13, "RightLowerArm", "RightLowerArm", 12, (-0.55, 0.0, 1.56), (-0.82, 0.0, 1.56), True),
    JointSpec(14, "RightHand", "RightHand", 13, (-0.82, 0.0, 1.56), (-0.94, 0.0, 1.56), True),
    JointSpec(15, "LeftUpperLeg", "LeftUpperLeg", 1, (0.095, 0.0, 1.00), (0.095, 0.0, 0.57), True),
    JointSpec(16, "LeftLowerLeg", "LeftLowerLeg", 15, (0.095, 0.0, 0.57), (0.095, -0.02, 0.12), True),
    JointSpec(17, "LeftFoot", "LeftFoot", 16, (0.095, -0.02, 0.12), (0.095, -0.18, 0.06), True),
    JointSpec(18, "LeftToes", "LeftToes", 17, (0.095, -0.18, 0.06), (0.095, -0.30, 0.06), False),
    JointSpec(19, "RightUpperLeg", "RightUpperLeg", 1, (-0.095, 0.0, 1.00), (-0.095, 0.0, 0.57), True),
    JointSpec(20, "RightLowerLeg", "RightLowerLeg", 19, (-0.095, 0.0, 0.57), (-0.095, -0.02, 0.12), True),
    JointSpec(21, "RightFoot", "RightFoot", 20, (-0.095, -0.02, 0.12), (-0.095, -0.18, 0.06), True),
    JointSpec(22, "RightToes", "RightToes", 21, (-0.095, -0.18, 0.06), (-0.095, -0.30, 0.06), False),
)


def joint_path(index: int) -> str:
    joint = JOINTS[index]
    if joint.is_synthetic_root:
        return ""
    parent_path = joint_path(joint.parent_index)
    return f"{parent_path}/{joint.name}" if parent_path else joint.name


def validate_rig_spec(joints: tuple[JointSpec, ...] = JOINTS) -> None:
    if not joints or len(joints) > MAX_POSE_JOINTS:
        raise ValueError("Canonical rig must contain between 1 and 60 pose joints")

    names: set[str] = set()
    human_bones: set[str] = set()
    for expected_index, joint in enumerate(joints):
        if joint.index != expected_index:
            raise ValueError("Joint indices must be contiguous and ordered")
        if joint.name in names:
            raise ValueError(f"Duplicate joint name: {joint.name}")
        names.add(joint.name)

        if joint.index == 0:
            if joint.parent_index != -1 or joint.head is not None or joint.tail is not None:
                raise ValueError("Joint 0 must be the synthetic root")
            continue

        if joint.parent_index < 0 or joint.parent_index >= joint.index:
            raise ValueError(f"Parent must precede child: {joint.name}")
        if joint.head is None or joint.tail is None:
            raise ValueError(f"Physical joint requires head and tail: {joint.name}")
        if not all(math.isfinite(value) for value in (*joint.head, *joint.tail)):
            raise ValueError(f"Joint coordinates must be finite: {joint.name}")
        length_squared = sum((tail - head) ** 2 for head, tail in zip(joint.head, joint.tail))
        if length_squared <= 1e-8:
            raise ValueError(f"Bone length is too small: {joint.name}")
        if joint.required and joint.human_bone is None:
            raise ValueError(f"Required joint needs a Human bone mapping: {joint.name}")
        if joint.human_bone is not None:
            if joint.human_bone in human_bones:
                raise ValueError(f"Duplicate Human bone mapping: {joint.human_bone}")
            human_bones.add(joint.human_bone)


validate_rig_spec()
