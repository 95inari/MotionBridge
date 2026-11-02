"""Bone selection compatibility for Blender 4.5 and 5.x."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import bpy


def pose_bone_selected(pose_bone: bpy.types.PoseBone) -> bool:
    # Selection moved from Bone to PoseBone in Blender 5.0.
    owner = pose_bone if hasattr(pose_bone, "select") else pose_bone.bone
    return bool(owner.select)


def set_pose_bone_selected(pose_bone: bpy.types.PoseBone, selected: bool) -> None:
    owner = pose_bone if hasattr(pose_bone, "select") else pose_bone.bone
    owner.select = selected
