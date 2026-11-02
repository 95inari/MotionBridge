"""Deterministic visual bake from an evaluated source pose to Canonical."""

from __future__ import annotations

from array import array
from dataclasses import dataclass
import json
from typing import Callable, Mapping

import bpy
from mathutils import Matrix, Quaternion, Vector

from ..canonical.rig_spec import JOINTS, SKELETON_ID
from ..editing.action_api import (
    ActionChannelAccess,
    create_action_for_id,
    ensure_fcurve,
)
from .mapping import ValidatedHumanoidMapping, validate_humanoid_mapping


RETARGET_BAKED_ROLE = "RETARGET_BAKED"


def bake_source_to_canonical(
    context: bpy.types.Context,
    source_armature: bpy.types.Object,
    canonical_armature: bpy.types.Object,
    mapping: Mapping[str, str],
    *,
    frame_start: int,
    frame_end: int,
    name: str | None = None,
    _on_frame: Callable[[int], None] | None = None,
) -> bpy.types.Action:
    """Bake evaluated source motion into a new, unbound Canonical Action.

    Source constraints, drivers, and animation are sampled from the evaluated
    dependency graph and never edited.  The returned Action contains one key
    per integer frame, complete WXYZ quaternion channels for every Canonical
    bone, and height-scaled Hips translation channels.
    """

    plan = validate_humanoid_mapping(source_armature, canonical_armature, mapping)
    if isinstance(frame_start, bool) or not isinstance(frame_start, int):
        raise TypeError("frame_start must be an integer")
    if isinstance(frame_end, bool) or not isinstance(frame_end, int):
        raise TypeError("frame_end must be an integer")
    if frame_end < frame_start:
        raise ValueError("Retarget frame range is reversed")
    if context.view_layer.objects.get(source_armature.name) is not source_armature:
        raise ValueError("Source armature must belong to the current View Layer")
    if context.view_layer.objects.get(canonical_armature.name) is not canonical_armature:
        raise ValueError("Canonical armature must belong to the current View Layer")

    state = _EditorState.capture(context, canonical_armature)
    access: ActionChannelAccess | None = None
    result: bpy.types.Action | None = None
    try:
        state.prepare_target(context, canonical_armature, frame_start, frame_end)
        access = create_action_for_id(
            name or f"MB_RETARGET::{source_armature.name}",
            canonical_armature,
            frame_start=frame_start,
            frame_end=frame_end,
        )
        writer = _ActionWriter(access)
        context.scene.frame_set(frame_start)
        context.view_layer.update()
        source_anchor = source_armature.evaluated_get(
            context.evaluated_depsgraph_get()
        ).matrix_world.copy()
        source_anchor_inverse = source_anchor.inverted_safe()
        source_anchor_rotation = source_anchor.to_quaternion().normalized()
        previous_quaternions: dict[str, Quaternion] = {}
        for frame in range(frame_start, frame_end + 1):
            context.scene.frame_set(frame)
            context.view_layer.update()
            if _on_frame is not None:
                _on_frame(frame)
            _sample_frame(
                context,
                plan,
                writer,
                frame,
                source_anchor_inverse,
                source_anchor_rotation,
                previous_quaternions,
            )
        writer.finish()

        action = access.action
        action["motion_bridge_role"] = RETARGET_BAKED_ROLE
        action["motion_bridge_skeleton_id"] = SKELETON_ID
        action["motion_bridge_source"] = source_armature.name
        action["motion_bridge_height_scale"] = plan.height_scale
        action["motion_bridge_frame_start"] = frame_start
        action["motion_bridge_frame_end"] = frame_end
        action["motion_bridge_has_trajectory"] = True
        action["motion_bridge_human_mapping"] = json.dumps(
            dict(plan.source_bones_by_human_bone),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        action.use_fake_user = True
        result = action
    finally:
        restored = False
        try:
            state.restore(context, canonical_armature)
            restored = True
        finally:
            if (result is None or not restored) and access is not None:
                action = access.action
                action.use_fake_user = False
                if action.users == 0:
                    bpy.data.actions.remove(action)
    assert result is not None
    return result


def _sample_frame(
    context: bpy.types.Context,
    plan: ValidatedHumanoidMapping,
    writer: _ActionWriter,
    frame: int,
    source_anchor_inverse: Matrix,
    source_anchor_rotation: Quaternion,
    previous_quaternions: dict[str, Quaternion],
) -> None:
    source = plan.source_armature
    target = plan.canonical_armature
    evaluated = source.evaluated_get(context.evaluated_depsgraph_get())
    target_object_rotation = target.matrix_world.to_quaternion().normalized()
    target_object_inverse = target_object_rotation.inverted()

    relative_object = source_anchor_inverse @ evaluated.matrix_world
    root_location = relative_object.translation * plan.height_scale
    root_rotation = relative_object.to_quaternion().normalized()
    previous_root = previous_quaternions.get("__OBJECT_ROOT__")
    if previous_root is not None and previous_root.dot(root_rotation) < 0.0:
        root_rotation.negate()
    previous_quaternions["__OBJECT_ROOT__"] = root_rotation.copy()
    writer.insert_vector("location", "Root", frame, root_location)
    writer.insert_object_quaternion(frame, root_rotation)

    desired_matrices: dict[str, Matrix] = {}
    for joint in JOINTS[1:]:
        target_bone = target.data.bones[joint.name]
        target_rest = target_bone.matrix_local
        source_name = plan.source_name_for_joint(joint)
        if source_name is not None:
            source_rest_rotation = source.data.bones[
                source_name
            ].matrix_local.to_quaternion().normalized()
            source_pose_rotation = evaluated.pose.bones[
                source_name
            ].matrix.to_quaternion().normalized()
            source_delta = source_pose_rotation @ source_rest_rotation.inverted()
            world_delta = (
                source_anchor_rotation
                @ source_delta
                @ source_anchor_rotation.inverted()
            )
            target_delta = (
                target_object_inverse @ world_delta @ target_object_rotation
            ).normalized()
            desired_rotation = (
                target_delta @ target_rest.to_quaternion().normalized()
            ).normalized()
        else:
            desired_rotation = _rest_relative_child_rotation(
                target,
                target_bone,
                desired_matrices,
            )

        if joint.name == "Hips":
            assert source_name is not None
            source_pose_location = evaluated.pose.bones[source_name].matrix.translation
            source_rest_location = source.data.bones[source_name].matrix_local.translation
            source_delta_location = source_pose_location - source_rest_location
            target_delta_location = (
                target_object_inverse
                @ source_anchor_rotation
                @ source_delta_location
            ) * plan.height_scale
            desired_location = target_rest.translation + target_delta_location
        else:
            parent_name = JOINTS[joint.parent_index].name
            parent_pose = desired_matrices[parent_name]
            parent_rest = target.data.bones[parent_name].matrix_local
            rest_offset = (
                parent_rest.to_quaternion().inverted()
                @ (target_rest.translation - parent_rest.translation)
            )
            desired_location = (
                parent_pose.translation
                + parent_pose.to_quaternion().normalized() @ rest_offset
            )

        desired = Matrix.LocRotScale(
            desired_location,
            desired_rotation,
            Vector((1.0, 1.0, 1.0)),
        )
        desired_matrices[joint.name] = desired
        pose_bone = target.pose.bones[joint.name]
        pose_bone.matrix = desired

    context.view_layer.update()
    for joint in JOINTS[1:]:
        pose_bone = target.pose.bones[joint.name]
        quaternion = pose_bone.rotation_quaternion.normalized()
        previous = previous_quaternions.get(joint.name)
        if previous is not None and previous.dot(quaternion) < 0.0:
            quaternion.negate()
        previous_quaternions[joint.name] = quaternion.copy()
        writer.insert_quaternion(joint.name, frame, quaternion)
        if joint.name == "Hips":
            writer.insert_vector(
                'pose.bones["Hips"].location',
                "Hips",
                frame,
                pose_bone.location,
            )


def _rest_relative_child_rotation(
    target: bpy.types.Object,
    target_bone: bpy.types.Bone,
    desired_matrices: Mapping[str, Matrix],
) -> Quaternion:
    parent = target_bone.parent
    if parent is None:
        return target_bone.matrix_local.to_quaternion().normalized()
    parent_pose_rotation = desired_matrices[parent.name].to_quaternion().normalized()
    rest_local_rotation = (
        parent.matrix_local.to_quaternion().inverted()
        @ target_bone.matrix_local.to_quaternion()
    ).normalized()
    return (parent_pose_rotation @ rest_local_rotation).normalized()


class _ActionWriter:
    """Buffer dense samples and commit every FCurve with one bulk write.

    ``FCurveKeyframePoints.insert`` performs collection maintenance for every
    key.  That cost becomes dominant on long takes, so sampling only appends to
    compact C arrays here.  ``finish`` creates each curve once, allocates its
    complete point range, and copies coordinates/interpolation through Blender's
    ``foreach_set`` API.
    """

    def __init__(self, access: ActionChannelAccess) -> None:
        self._access = access
        self._buffers: dict[tuple[str, int], _CurveBuffer] = {}
        self._finished = False

    def insert_quaternion(
        self,
        bone_name: str,
        frame: float,
        quaternion: Quaternion,
    ) -> None:
        data_path = f'pose.bones["{bone_name}"].rotation_quaternion'
        for index in range(4):
            self._insert(
                data_path,
                index,
                bone_name,
                frame,
                float(quaternion[index]),
            )

    def insert_object_quaternion(
        self,
        frame: float,
        quaternion: Quaternion,
    ) -> None:
        for index in range(4):
            self._insert(
                "rotation_quaternion",
                index,
                "Root",
                frame,
                float(quaternion[index]),
            )

    def insert_vector(
        self,
        data_path: str,
        group_name: str,
        frame: float,
        value: Vector,
    ) -> None:
        for index in range(3):
            self._insert(data_path, index, group_name, frame, float(value[index]))

    def finish(self) -> None:
        if self._finished:
            raise RuntimeError("Action writer has already been finished")
        self._finished = True
        # Pop in insertion order so the temporary C arrays are released as
        # their Blender-owned FCurves are materialized.  Only the small tuple
        # of dictionary keys remains live during the commit.
        for key in tuple(self._buffers):
            buffer = self._buffers.pop(key)
            curve = ensure_fcurve(
                self._access,
                buffer.data_path,
                buffer.array_index,
                group_name=buffer.group_name,
            )
            points = curve.keyframe_points
            if len(points) != 0:
                raise RuntimeError("Bulk retarget writer requires empty FCurves")
            point_count = len(buffer.coordinates) // 2
            if point_count:
                points.add(point_count)
                points.foreach_set("co", buffer.coordinates)
                linear_value = (
                    points[0]
                    .bl_rna.properties["interpolation"]
                    .enum_items["LINEAR"]
                    .value
                )
                points.foreach_set(
                    "interpolation",
                    array("i", [linear_value]) * point_count,
                )
            curve.update()

    def _insert(
        self,
        data_path: str,
        index: int,
        group_name: str,
        frame: float,
        value: float,
    ) -> None:
        if self._finished:
            raise RuntimeError("Cannot append samples after Action writer finish")
        key = (data_path, index)
        buffer = self._buffers.get(key)
        if buffer is None:
            buffer = _CurveBuffer(
                data_path,
                index,
                group_name,
                array("d"),
            )
            self._buffers[key] = buffer
        buffer.coordinates.extend((frame, value))


@dataclass(slots=True)
class _CurveBuffer:
    data_path: str
    array_index: int
    group_name: str
    coordinates: array


class _EditorState:
    def __init__(self) -> None:
        self.active: bpy.types.Object | None = None
        self.selected: tuple[bpy.types.Object, ...] = ()
        self.mode: str | None = None
        self.frame_start = 0
        self.frame_end = 0
        self.frame = 0
        self.subframe = 0.0
        self.target_action: bpy.types.Action | None = None
        self.target_slot: object | None = None
        self.target_last_slot_identifier: str | None = None
        self.target_tweak_slot_handle: int | None = None
        self.target_tracks: tuple[tuple[bpy.types.NlaTrack, bool, bool], ...] = ()
        self.target_pose: tuple[tuple[bpy.types.PoseBone, Matrix, str], ...] = ()
        self.target_matrix_basis = Matrix.Identity(4)
        self.target_rotation_mode = "QUATERNION"

    @classmethod
    def capture(
        cls,
        context: bpy.types.Context,
        target: bpy.types.Object,
    ) -> _EditorState:
        state = cls()
        state.active = context.view_layer.objects.active
        state.selected = tuple(context.selected_objects)
        state.mode = state.active.mode if state.active is not None else None
        scene = context.scene
        state.frame_start = scene.frame_start
        state.frame_end = scene.frame_end
        state.frame = scene.frame_current
        state.subframe = scene.frame_subframe
        animation_data = target.animation_data
        if animation_data is not None:
            if getattr(animation_data, "use_tweak_mode", False):
                raise ValueError("Exit target NLA Tweak Mode before retarget bake")
            state.target_action = animation_data.action
            state.target_slot = getattr(animation_data, "action_slot", None)
            state.target_last_slot_identifier = getattr(
                animation_data,
                "last_slot_identifier",
                None,
            )
            state.target_tweak_slot_handle = getattr(
                animation_data,
                "action_slot_handle_tweak_storage",
                None,
            )
            state.target_tracks = tuple(
                (track, track.mute, track.is_solo)
                for track in animation_data.nla_tracks
            )
        state.target_pose = tuple(
            (pose_bone, pose_bone.matrix_basis.copy(), pose_bone.rotation_mode)
            for pose_bone in target.pose.bones
        )
        state.target_matrix_basis = target.matrix_basis.copy()
        state.target_rotation_mode = target.rotation_mode
        return state

    def prepare_target(
        self,
        context: bpy.types.Context,
        target: bpy.types.Object,
        frame_start: int,
        frame_end: int,
    ) -> None:
        if self.active is not None and self.active.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for selected in tuple(context.selected_objects):
            selected.select_set(False)
        target.select_set(True)
        context.view_layer.objects.active = target
        animation_data = target.animation_data
        if animation_data is not None:
            animation_data.action = None
            for track in animation_data.nla_tracks:
                track.is_solo = False
                track.mute = True
        target.rotation_mode = "QUATERNION"
        target.matrix_basis = Matrix.Identity(4)
        for pose_bone in target.pose.bones:
            pose_bone.rotation_mode = "QUATERNION"
            pose_bone.matrix_basis = Matrix.Identity(4)
        context.scene.frame_start = frame_start
        context.scene.frame_end = frame_end

    def restore(
        self,
        context: bpy.types.Context,
        target: bpy.types.Object,
    ) -> None:
        if context.object is not None and context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        animation_data = target.animation_data
        target.rotation_mode = self.target_rotation_mode
        target.matrix_basis = self.target_matrix_basis
        if animation_data is not None:
            animation_data.action = self.target_action
            if self.target_action is not None and self.target_slot is not None:
                if hasattr(animation_data, "action_slot"):
                    animation_data.action_slot = self.target_slot
                elif hasattr(animation_data, "action_slot_handle"):
                    animation_data.action_slot_handle = self.target_slot.handle
            if self.target_last_slot_identifier is not None:
                animation_data.last_slot_identifier = self.target_last_slot_identifier
            if self.target_tweak_slot_handle is not None:
                animation_data.action_slot_handle_tweak_storage = (
                    self.target_tweak_slot_handle
                )
            for track, mute, is_solo in self.target_tracks:
                track.mute = mute
                track.is_solo = is_solo

        for pose_bone, matrix_basis, rotation_mode in self.target_pose:
            pose_bone.rotation_mode = rotation_mode
            pose_bone.matrix_basis = matrix_basis

        for selected in tuple(context.selected_objects):
            selected.select_set(False)
        for selected in self.selected:
            if context.view_layer.objects.get(selected.name) is selected:
                selected.select_set(True)
        if self.active is not None and context.view_layer.objects.get(
            self.active.name
        ) is self.active:
            context.view_layer.objects.active = self.active
        else:
            context.view_layer.objects.active = None

        scene = context.scene
        scene.frame_start = self.frame_start
        scene.frame_end = self.frame_end
        scene.frame_set(self.frame, subframe=self.subframe)
        if (
            self.active is not None
            and context.view_layer.objects.active is self.active
            and self.mode is not None
            and self.active.mode != self.mode
        ):
            bpy.ops.object.mode_set(mode=self.mode)
