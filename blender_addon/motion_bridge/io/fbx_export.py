from __future__ import annotations

import math
from pathlib import Path
from typing import Callable

import bpy
from bpy.props import StringProperty
from bpy.types import Operator
from bpy_extras.io_utils import ExportHelper

from ..canonical.rig_spec import SKELETON_ID
from ..editing import activate_action_for_export, bake_layer_stack


ExportOperator = Callable[..., set[str]]


def active_canonical_action(
    context: bpy.types.Context,
) -> tuple[bpy.types.Object, bpy.types.Action]:
    """Return the active Canonical armature and its baked Action."""

    obj = context.view_layer.objects.active
    if obj is None or obj.type != "ARMATURE":
        raise ValueError("The active object must be a Canonical armature")
    if obj.get("motion_bridge_skeleton_id") != SKELETON_ID:
        raise ValueError(
            "The active armature is not the Motion Bridge Canonical skeleton"
        )
    animation_data = obj.animation_data
    action = animation_data.action if animation_data is not None else None
    if action is None:
        raise ValueError("The Canonical armature must have an active baked Action")
    if getattr(action, "is_empty", False):
        raise ValueError("The active Action has no animation data")
    frame_start, frame_end = (float(frame) for frame in action.frame_range)
    if not math.isfinite(frame_start) or not math.isfinite(frame_end):
        raise ValueError("The active Action has an invalid frame range")
    return obj, action


def export_active_canonical_action(
    context: bpy.types.Context,
    filepath: str | Path,
    *,
    _exporter: ExportOperator | None = None,
) -> Path:
    """Export only the active Canonical armature and its active baked Action.

    Selection, active object, armature mode, active Action, timeline range, and
    current frame are restored in ``finally`` even when the FBX operator fails.
    ``_exporter`` exists only to exercise that failure path in Blender tests.
    """

    obj, action = active_canonical_action(context)
    view_layer = context.view_layer
    scene = context.scene
    animation_data = obj.animation_data
    assert animation_data is not None

    previous_active = view_layer.objects.active
    previous_selected = tuple(context.selected_objects)
    previous_mode = obj.mode
    previous_action = animation_data.action
    previous_frame_start = scene.frame_start
    previous_frame_end = scene.frame_end
    previous_frame = scene.frame_current
    previous_subframe = scene.frame_subframe

    output = Path(bpy.path.abspath(str(filepath))).expanduser()
    if output.suffix.lower() != ".fbx":
        output = output.with_suffix(".fbx")
    output.parent.mkdir(parents=True, exist_ok=True)
    exporter = _exporter if _exporter is not None else bpy.ops.export_scene.fbx
    action_start, action_end = (float(frame) for frame in action.frame_range)

    try:
        if obj.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for selected in tuple(context.selected_objects):
            selected.select_set(False)
        obj.select_set(True)
        view_layer.objects.active = obj

        scene.frame_start = math.floor(action_start)
        scene.frame_end = math.ceil(action_end)
        scene.frame_set(scene.frame_start)

        result = exporter(
            filepath=str(output),
            check_existing=False,
            use_selection=True,
            object_types={"ARMATURE"},
            global_scale=1.0,
            apply_unit_scale=True,
            apply_scale_options="FBX_SCALE_UNITS",
            axis_forward="-Z",
            axis_up="Y",
            use_space_transform=True,
            bake_space_transform=False,
            primary_bone_axis="Y",
            secondary_bone_axis="X",
            add_leaf_bones=False,
            armature_nodetype="NULL",
            use_armature_deform_only=False,
            bake_anim=True,
            bake_anim_use_all_bones=True,
            bake_anim_use_all_actions=False,
            bake_anim_use_nla_strips=False,
            bake_anim_force_startend_keying=True,
            bake_anim_step=1.0,
            bake_anim_simplify_factor=0.0,
            use_custom_props=True,
            use_metadata=False,
        )
        if "FINISHED" not in result:
            raise RuntimeError(f"FBX export failed: {result}")
        return output
    finally:
        current_active = view_layer.objects.active
        if current_active is not None and current_active.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        if animation_data.action is not previous_action:
            animation_data.action = previous_action

        for selected in tuple(context.selected_objects):
            selected.select_set(False)
        for selected in previous_selected:
            if view_layer.objects.get(selected.name) is selected:
                selected.select_set(True)
        if (
            previous_active is not None
            and view_layer.objects.get(previous_active.name) is previous_active
        ):
            view_layer.objects.active = previous_active

        scene.frame_start = previous_frame_start
        scene.frame_end = previous_frame_end
        scene.frame_set(previous_frame, subframe=previous_subframe)

        if (
            previous_active is not None
            and view_layer.objects.active is previous_active
            and previous_active.mode != previous_mode
        ):
            bpy.ops.object.mode_set(mode=previous_mode)


def export_corrected_canonical_action(
    context: bpy.types.Context,
    filepath: str | Path,
    *,
    _exporter: ExportOperator | None = None,
) -> Path:
    """Visual-bake Base+Correction, export it, then discard the temporary Action."""

    obj = context.view_layer.objects.active
    if obj is None:
        raise ValueError("The active object must be a Canonical armature")
    baked_action: bpy.types.Action | None = None
    try:
        baked_action = bake_layer_stack(context, obj)
        with activate_action_for_export(obj, baked_action):
            return export_active_canonical_action(
                context,
                filepath,
                _exporter=_exporter,
            )
    finally:
        if baked_action is not None:
            baked_action.use_fake_user = False
            if baked_action.users == 0:
                bpy.data.actions.remove(baked_action)


class MB_OT_ExportCanonicalActionFbx(Operator, ExportHelper):
    bl_idname = "motion_bridge.export_canonical_action_fbx"
    bl_label = "現在のモーションをFBXで書き出す"
    bl_description = (
        "現在の変換済みCanonicalモーションをArmatureのみのFBXで書き出します"
    )
    bl_options = {"REGISTER"}

    filename_ext = ".fbx"
    filter_glob: StringProperty(default="*.fbx", options={"HIDDEN"})

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        obj = context.view_layer.objects.active
        return bool(
            obj is not None
            and obj.type == "ARMATURE"
            and obj.get("motion_bridge_skeleton_id") == SKELETON_ID
            and obj.animation_data is not None
            and obj.animation_data.action is not None
        )

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        if not self.filepath:
            obj = context.view_layer.objects.active
            action = obj.animation_data.action if obj and obj.animation_data else None
            filename = (
                bpy.path.clean_name(action.name) if action is not None else "motion"
            )
            self.filepath = filename + self.filename_ext
        return ExportHelper.invoke(self, context, event)

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            output = export_active_canonical_action(context, self.filepath)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"FBXを書き出しました：{output.name}")
        return {"FINISHED"}


class MB_OT_ExportCorrectedFbx(Operator, ExportHelper):
    bl_idname = "motion_bridge.export_corrected_fbx"
    bl_label = "修正済みFBXを書き出す"
    bl_description = "Baseと修正レイヤーを合成し、ArmatureのみのFBXで書き出します"
    bl_options = {"REGISTER"}

    filename_ext = ".fbx"
    filter_glob: StringProperty(default="*.fbx", options={"HIDDEN"})

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        obj = context.view_layer.objects.active
        return bool(
            obj is not None
            and obj.type == "ARMATURE"
            and obj.get("motion_bridge_skeleton_id") == SKELETON_ID
            and obj.get("motion_bridge_stack_id")
        )

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        if not self.filepath:
            obj = context.view_layer.objects.active
            base_name = (
                obj.get("motion_bridge_base_action", "corrected_motion")
                if obj is not None
                else "corrected_motion"
            )
            self.filepath = bpy.path.clean_name(str(base_name)) + self.filename_ext
        return ExportHelper.invoke(self, context, event)

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            output = export_corrected_canonical_action(context, self.filepath)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"修正済みFBXを書き出しました：{output.name}")
        return {"FINISHED"}
