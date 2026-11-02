"""Validation and immutable lookup data for source-to-Canonical mappings."""

from __future__ import annotations

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Mapping

import bpy

from ..canonical.rig_spec import JOINTS, SKELETON_ID, JointSpec


_JOINT_BY_HUMAN_BONE = {
    joint.human_bone: joint for joint in JOINTS if joint.human_bone is not None
}


@dataclass(frozen=True, slots=True)
class ValidatedHumanoidMapping:
    """Validated source names and the linear source-to-target height scale."""

    source_armature: bpy.types.Object
    canonical_armature: bpy.types.Object
    source_bones_by_human_bone: Mapping[str, str]
    height_scale: float

    def source_name_for_joint(self, joint: JointSpec) -> str | None:
        if joint.human_bone is None:
            return None
        return self.source_bones_by_human_bone.get(joint.human_bone)


def validate_humanoid_mapping(
    source_armature: bpy.types.Object,
    canonical_armature: bpy.types.Object,
    mapping: Mapping[str, str],
) -> ValidatedHumanoidMapping:
    """Validate an explicit ``HumanBone -> source bone`` mapping.

    Helper bones may occur between mapped source bones.  The nearest mapped
    ancestor must nevertheless match the nearest mapped Canonical ancestor,
    which catches crossed limbs and disconnected mappings without requiring
    source-specific naming conventions.
    """

    if source_armature is canonical_armature:
        raise ValueError("Source and Canonical armatures must be different objects")
    if source_armature.type != "ARMATURE":
        raise ValueError("Source must be an armature object")
    _validate_canonical_structure(canonical_armature)
    if not isinstance(mapping, Mapping):
        raise TypeError("mapping must be a HumanBone-to-source-name mapping")

    normalized: dict[str, str] = {}
    for human_bone, source_name in mapping.items():
        if not isinstance(human_bone, str) or human_bone not in _JOINT_BY_HUMAN_BONE:
            raise ValueError(f"Unknown Human bone mapping key: {human_bone!r}")
        if not isinstance(source_name, str) or not source_name:
            raise ValueError(f"Source bone for {human_bone} must be a non-empty name")
        if source_name not in source_armature.data.bones:
            raise ValueError(
                f"Mapped source bone does not exist: {human_bone} -> {source_name}"
            )
        normalized[human_bone] = source_name

    missing = sorted(
        joint.human_bone
        for joint in JOINTS
        if joint.required
        and joint.human_bone is not None
        and joint.human_bone not in normalized
    )
    if missing:
        raise ValueError(f"Missing required Human bone mappings: {', '.join(missing)}")

    names = tuple(normalized.values())
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"Source bones are mapped more than once: {', '.join(duplicates)}")

    _validate_hierarchy(source_armature, normalized)
    source_height = _humanoid_height(source_armature, normalized)
    canonical_names = {
        joint.human_bone: joint.name
        for joint in JOINTS
        if joint.human_bone is not None
    }
    target_height = _humanoid_height(canonical_armature, canonical_names)
    height_scale = target_height / source_height
    if not math.isfinite(height_scale) or height_scale <= 1e-8:
        raise ValueError("Computed Human height scale is invalid")

    return ValidatedHumanoidMapping(
        source_armature=source_armature,
        canonical_armature=canonical_armature,
        source_bones_by_human_bone=MappingProxyType(dict(normalized)),
        height_scale=height_scale,
    )


def _validate_canonical_structure(armature: bpy.types.Object) -> None:
    """Validate rig identity without rejecting an evaluated root trajectory."""

    if armature.type != "ARMATURE":
        raise ValueError("Canonical target must be an armature object")
    if armature.get("motion_bridge_skeleton_id") != SKELETON_ID:
        raise ValueError("Target is not the Motion Bridge Canonical skeleton")
    if armature.parent is not None:
        raise ValueError("Canonical target Armature Object must not be parented")
    expected_names = {joint.name for joint in JOINTS[1:]}
    actual_names = set(armature.data.bones.keys())
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        raise ValueError(
            f"Canonical bone set mismatch; missing={missing}, extra={extra}"
        )
    for joint in JOINTS[1:]:
        bone = armature.data.bones[joint.name]
        expected_parent = (
            None if joint.parent_index == 0 else JOINTS[joint.parent_index].name
        )
        actual_parent = bone.parent.name if bone.parent is not None else None
        if actual_parent != expected_parent:
            raise ValueError(
                f"Canonical parent mismatch for {joint.name}: "
                f"expected={expected_parent}, actual={actual_parent}"
            )


def _validate_hierarchy(
    source_armature: bpy.types.Object,
    mapping: Mapping[str, str],
) -> None:
    source_to_human = {source_name: human for human, source_name in mapping.items()}
    mapped_humans = set(mapping)
    for human_bone, source_name in mapping.items():
        joint = _JOINT_BY_HUMAN_BONE[human_bone]
        expected_parent = _nearest_mapped_canonical_ancestor(joint, mapped_humans)

        source_parent = source_armature.data.bones[source_name].parent
        actual_parent: str | None = None
        while source_parent is not None:
            actual_parent = source_to_human.get(source_parent.name)
            if actual_parent is not None:
                break
            source_parent = source_parent.parent

        if actual_parent != expected_parent:
            raise ValueError(
                f"Hierarchy mismatch for {human_bone}: expected mapped ancestor "
                f"{expected_parent!r}, found {actual_parent!r}"
            )


def _nearest_mapped_canonical_ancestor(
    joint: JointSpec,
    mapped_humans: set[str],
) -> str | None:
    parent_index = joint.parent_index
    while parent_index > 0:
        parent = JOINTS[parent_index]
        if parent.human_bone in mapped_humans:
            return parent.human_bone
        parent_index = parent.parent_index
    return None


def _humanoid_height(
    armature: bpy.types.Object,
    mapping: Mapping[str, str],
) -> float:
    """Return a rotation-independent head-to-feet Human height estimate."""

    bones = armature.data.bones
    hips = bones[mapping["Hips"]].head_local
    head_top = bones[mapping["Head"]].tail_local
    left_foot = bones[mapping["LeftFoot"]].tail_local
    right_foot = bones[mapping["RightFoot"]].tail_local
    torso = (head_top - hips).length
    legs = 0.5 * ((left_foot - hips).length + (right_foot - hips).length)
    height = float(torso + legs)
    if not math.isfinite(height) or height <= 1e-8:
        raise ValueError("Mapped source Human height is zero or non-finite")
    return height
