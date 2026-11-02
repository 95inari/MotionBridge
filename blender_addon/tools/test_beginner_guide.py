from __future__ import annotations

import math
from pathlib import Path
import sys
from types import SimpleNamespace

import bpy
from mathutils import Quaternion, Vector


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))
TOOLS_ROOT = Path(__file__).resolve().parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from motion_bridge import addon  # noqa: E402
from motion_bridge.canonical.rig_spec import SKELETON_ID  # noqa: E402
from motion_bridge.editing import (  # noqa: E402
    corrections_preview_enabled,
    get_correction_layer_stack,
)
from motion_bridge.editing.action_api import iter_action_fcurves  # noqa: E402
from motion_bridge.editing.pose_api import set_pose_bone_selected  # noqa: E402
from motion_bridge.workflow_guide import GuideStage, resolve_guide_stage  # noqa: E402
from test_source_retarget import (  # noqa: E402
    FIRST_FRAME,
    LAST_FRAME,
    _create_armature as _create_motion_armature,
    _key_source_motion,
)


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for action in tuple(bpy.data.actions):
        if action.users == 0:
            bpy.data.actions.remove(action)


def _armature(name: str, *, canonical: bool = False) -> bpy.types.Object:
    data = bpy.data.armatures.new(f"{name}_Data")
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    if canonical:
        obj["motion_bridge_skeleton_id"] = SKELETON_ID
    return obj


class _LayoutRecorder:
    def __init__(
        self,
        operators: list[str] | None = None,
        searched_properties: list[str] | None = None,
        labels: list[str] | None = None,
    ) -> None:
        self.operators = operators if operators is not None else []
        self.searched_properties = (
            searched_properties if searched_properties is not None else []
        )
        self.labels = labels if labels is not None else []
        self.alert = False
        self.enabled = True
        self.scale_y = 1.0

    def box(self) -> "_LayoutRecorder":
        return _LayoutRecorder(self.operators, self.searched_properties, self.labels)

    def row(self, **_kwargs: object) -> "_LayoutRecorder":
        return _LayoutRecorder(self.operators, self.searched_properties, self.labels)

    def label(self, **kwargs: object) -> None:
        self.labels.append(str(kwargs.get("text", "")))

    def prop(self, *_args: object, **_kwargs: object) -> None:
        return None

    def prop_search(
        self,
        _data: object,
        property_name: str,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        self.searched_properties.append(property_name)

    def operator(self, operator_id: str, **_kwargs: object) -> SimpleNamespace:
        self.operators.append(operator_id)
        return SimpleNamespace()


def _select_only(obj: bpy.types.Object) -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for selected in tuple(bpy.context.selected_objects):
        selected.select_set(False)
    obj.hide_set(False)
    obj.hide_viewport = False
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def _expect_stage(expected: GuideStage, message: str) -> None:
    actual = resolve_guide_stage(addon._workflow_facts(bpy.context))
    if actual is not expected:
        raise AssertionError(f"{message}: expected={expected.value}, actual={actual.value}")


def _assert_manual_mapping_recovery_is_drawn(settings: object) -> None:
    settings.retarget_preset = "AUTO"
    automatic = _LayoutRecorder()
    addon._draw_beginner_workflow(bpy.context, automatic, settings)
    if addon.MB_OT_BakeSourceToCanonical.bl_idname not in automatic.operators:
        raise AssertionError("AUTO guide did not draw the Canonical bake action")
    if addon.MB_OT_PopulateManualMapping.bl_idname in automatic.operators:
        raise AssertionError("AUTO guide unexpectedly drew the manual recovery action")

    settings.retarget_preset = "MANUAL"
    manual = _LayoutRecorder()
    addon._draw_beginner_workflow(bpy.context, manual, settings)
    required = {
        addon.MB_OT_PopulateManualMapping.bl_idname,
        addon.MB_OT_BakeSourceToCanonical.bl_idname,
    }
    if not required.issubset(manual.operators):
        raise AssertionError(
            "MANUAL guide did not draw mapping recovery and bake actions: "
            f"{manual.operators!r}"
        )

    for field in addon.HUMANOID_MAPPING_FIELDS:
        if field.required:
            setattr(settings.manual_mapping, field.property_name, field.human_bone)
    settings.beginner_retarget_error = "ボーン対応が重複しています"
    invalid_optional = _LayoutRecorder()
    addon._draw_beginner_workflow(bpy.context, invalid_optional, settings)
    expected_fields = {
        field.property_name for field in addon.HUMANOID_MAPPING_FIELDS
    }
    if set(invalid_optional.searched_properties) != expected_fields:
        raise AssertionError(
            "MANUAL recovery hid optional fields after a non-missing validation "
            f"error: {invalid_optional.searched_properties!r}"
        )
    settings.beginner_retarget_error = ""
    settings.retarget_preset = "AUTO"


def _pre_stack_state(
    scene: bpy.types.Scene,
    armature: bpy.types.Object,
) -> tuple[object, ...]:
    animation_data = armature.animation_data
    action = animation_data.action if animation_data is not None else None
    settings = scene.motion_bridge_session
    envelope_tree = settings.correction_envelope_curve_tree
    return (
        tuple(obj.as_pointer() for obj in scene.objects),
        tuple(sorted((str(key), repr(value)) for key, value in armature.items())),
        armature.data.as_pointer(),
        action.as_pointer() if action is not None else None,
        int(action.users) if action is not None else None,
        bool(action.use_fake_user) if action is not None else None,
        tuple(
            (
                curve.data_path,
                curve.array_index,
                tuple(
                    (
                        float(point.co[0]),
                        float(point.co[1]),
                        point.interpolation,
                    )
                    for point in curve.keyframe_points
                ),
            )
            for curve in (() if action is None else iter_action_fcurves(action))
        ),
        tuple(
            (
                track.name,
                tuple(
                    (
                        strip.name,
                        strip.action.as_pointer() if strip.action is not None else None,
                        float(strip.frame_start),
                        float(strip.frame_end),
                    )
                    for strip in track.strips
                ),
            )
            for track in (() if animation_data is None else animation_data.nla_tracks)
        ),
        (
            addon._ENVELOPE_SCENE_ID_PROPERTY in scene,
            scene.get(addon._ENVELOPE_SCENE_ID_PROPERTY),
            envelope_tree.as_pointer() if envelope_tree is not None else None,
        ),
    )


def _verify_shared_pre_stack_stop(canonical: bpy.types.Object) -> None:
    original_scene = bpy.context.scene
    animation_data = canonical.animation_data
    base_action = animation_data.action if animation_data is not None else None
    if base_action is None:
        raise AssertionError("Could not prepare the shared Base fixture")
    if bpy.ops.scene.new(type="FULL_COPY") != {"FINISHED"}:
        raise AssertionError("Could not create the pre-stack Full Copy fixture")
    copied_scene = bpy.context.scene
    copied_objects = tuple(copied_scene.objects)
    copied_candidates = [
        obj
        for obj in copied_scene.objects
        if obj is not canonical
        and obj.type == "ARMATURE"
        and obj.get("motion_bridge_skeleton_id") == SKELETON_ID
        and obj.animation_data is not None
    ]
    if len(copied_candidates) != 1:
        raise AssertionError("Full Copy did not contain exactly one Canonical Base")
    copied_canonical = copied_candidates[0]
    copied_animation_data = copied_canonical.animation_data
    if copied_animation_data is None:
        raise AssertionError("Full Copy lost the Canonical animation binding")
    copied_action = copied_animation_data.action
    base_slot = getattr(animation_data, "action_slot", None)
    copied_animation_data.action = base_action
    if base_slot is not None and hasattr(copied_animation_data, "action_slot"):
        copied_animation_data.action_slot = base_slot
    if copied_action is not None and copied_action.users == 0:
        bpy.data.actions.remove(copied_action)
    _select_only(copied_canonical)

    def global_state() -> tuple[tuple[int, ...], ...]:
        return (
            tuple(action.as_pointer() for action in bpy.data.actions),
            tuple(obj.as_pointer() for obj in bpy.data.objects),
            tuple(data.as_pointer() for data in bpy.data.armatures),
            tuple(collection.as_pointer() for collection in bpy.data.collections),
            tuple(tree.as_pointer() for tree in bpy.data.node_groups),
        )
    before = (
        _pre_stack_state(original_scene, canonical),
        _pre_stack_state(copied_scene, copied_canonical),
        global_state(),
    )

    _expect_stage(
        GuideStage.SHARED_BASE,
        "Shared pre-stack Base did not show the beginner stop",
    )
    shared_layout = _LayoutRecorder()
    addon._draw_beginner_workflow(
        bpy.context,
        shared_layout,
        copied_scene.motion_bridge_session,
    )
    if addon.MB_OT_CreateCorrectionLayer.bl_idname in shared_layout.operators:
        raise AssertionError("Shared Base guide still offered Correction creation")
    shared_text = "\n".join(shared_layout.labels)
    if any(
        implementation_term in shared_text
        for implementation_term in ("NLA", "Correction", "Action", "単一ユーザー")
    ):
        raise AssertionError(
            "Shared Base recovery exposed an implementation term: "
            f"{shared_text!r}"
        )
    if "不要な複製シーンを削除" not in shared_text:
        raise AssertionError("Shared Base guide omitted its recovery action")
    if addon.MB_OT_CreateCorrectionLayer.poll(bpy.context):
        raise AssertionError("Shared Base create operator poll remained enabled")

    reports: list[tuple[set[str], str]] = []
    result = addon.MB_OT_CreateCorrectionLayer.execute(
        SimpleNamespace(
            report=lambda levels, message: reports.append((set(levels), message))
        ),
        bpy.context,
    )
    if result != {"CANCELLED"} or not reports:
        raise AssertionError("Shared Base execute preflight did not cancel")
    report_text = "\n".join(message for _levels, message in reports)
    if any(
        implementation_term in report_text
        for implementation_term in ("NLA", "Correction", "Action", "単一ユーザー")
    ):
        raise AssertionError(
            "Shared Base operator error exposed an implementation term: "
            f"{report_text!r}"
        )

    after = (
        _pre_stack_state(original_scene, canonical),
        _pre_stack_state(copied_scene, copied_canonical),
        global_state(),
    )
    if after != before:
        raise AssertionError("Cancelled shared Base creation changed either Scene")

    bpy.context.window.scene = original_scene
    bpy.data.scenes.remove(copied_scene)
    for obj in copied_objects:
        if obj.users == 0 and any(candidate is obj for candidate in bpy.data.objects):
            bpy.data.objects.remove(obj)
    _select_only(canonical)
    if not addon.MB_OT_CreateCorrectionLayer.poll(bpy.context):
        raise AssertionError("Deleting the duplicate Scene did not unblock creation")


def _run_beginner_operator_workflow(settings: object) -> None:
    _clear_scene()
    canonical, _target_mapping = _create_motion_armature(
        "MB_Canonical_Beginner_E2E",
        scale=1.0,
        source=False,
    )
    source, source_mapping = _create_motion_armature(
        "MB_Source_Beginner_E2E",
        scale=1.0,
        source=True,
    )
    source.rotation_mode = "QUATERNION"
    _key_source_motion(source, source_mapping)
    source["motion_bridge_source_frame_start"] = float(FIRST_FRAME)
    source["motion_bridge_source_frame_end"] = float(LAST_FRAME)

    _select_only(canonical)
    settings.source_armature = source
    bpy.context.scene.frame_start = FIRST_FRAME
    bpy.context.scene.frame_end = LAST_FRAME
    bpy.context.scene.frame_set((FIRST_FRAME + LAST_FRAME) // 2)
    _expect_stage(GuideStage.BAKE_SOURCE, "Synthetic Source was not bake-ready")
    _assert_manual_mapping_recovery_is_drawn(settings)

    if bpy.ops.motion_bridge.bake_source_to_canonical() != {"FINISHED"}:
        raise AssertionError("Beginner Canonical bake operator failed")
    _expect_stage(GuideStage.CREATE_CORRECTION, "Bake did not advance the guide")
    _verify_shared_pre_stack_stop(canonical)

    if bpy.ops.motion_bridge.create_correction_layer() != {"FINISHED"}:
        raise AssertionError("Beginner Correction creation operator failed")
    _expect_stage(GuideStage.READY_EDIT, "Correction creation did not advance guide")

    if bpy.ops.motion_bridge.enter_correction_edit() != {"FINISHED"}:
        raise AssertionError("Beginner Correction edit operator failed")
    _expect_stage(GuideStage.SELECT_BONE, "Edit start did not request a bone")

    bpy.ops.pose.select_all(action="DESELECT")
    pose_bone = canonical.pose.bones["LeftUpperArm"]
    set_pose_bone_selected(pose_bone, True)
    canonical.data.bones.active = pose_bone.bone
    pose_bone.rotation_mode = "QUATERNION"
    pose_bone.rotation_quaternion = Quaternion(
        Vector((0.0, 0.0, 1.0)),
        math.radians(18.0),
    )
    bpy.context.view_layer.update()
    _expect_stage(GuideStage.EDIT_POSE, "Bone selection did not show edit tools")
    if (
        bpy.ops.motion_bridge.insert_correction_pose_keys(
            include_location=False,
            neutral_padding=0,
            interpolation="BEZIER",
        )
        != {"FINISHED"}
    ):
        raise AssertionError("Beginner correction key operator failed")

    stack = get_correction_layer_stack(canonical)
    if stack is None:
        raise AssertionError("Correction stack disappeared during beginner edit")
    key_count = sum(
        len(curve.keyframe_points)
        for curve in iter_action_fcurves(stack.correction_action)
    )
    if key_count <= 0:
        raise AssertionError("Beginner correction edit did not create any keys")

    if bpy.ops.motion_bridge.exit_correction_edit() != {"FINISHED"}:
        raise AssertionError("Beginner correction exit operator failed")
    _expect_stage(GuideStage.READY_EDIT, "Edit exit did not return to ready state")
    comparison = _LayoutRecorder()
    addon._draw_beginner_comparison(
        bpy.context,
        comparison,
        addon._workflow_facts(bpy.context),
    )
    if comparison.operators.count(
        addon.MB_OT_SetCorrectionPreview.bl_idname
    ) != 2:
        raise AssertionError("Beginner panel did not draw Before/After controls")

    if bpy.ops.motion_bridge.set_correction_preview(enabled=False) != {"FINISHED"}:
        raise AssertionError("Beginner Before switch failed")
    if corrections_preview_enabled(canonical):
        raise AssertionError("Before switch left corrections enabled")
    _expect_stage(
        GuideStage.BEFORE_PREVIEW,
        "Before preview still offered Correction editing",
    )
    if bpy.ops.motion_bridge.set_correction_preview(enabled=True) != {"FINISHED"}:
        raise AssertionError("Beginner After switch failed")
    if not corrections_preview_enabled(canonical):
        raise AssertionError("After switch left corrections disabled")

    animation_data = canonical.animation_data
    if animation_data is None:
        raise AssertionError("Beginner stack lost animation data")
    unrelated_action = stack.base_action
    animation_data.action = unrelated_action
    base_slot = getattr(stack.base_strip, "action_slot", None)
    if base_slot is not None and hasattr(animation_data, "action_slot"):
        animation_data.action_slot = base_slot
    _expect_stage(
        GuideStage.UNSUPPORTED_ACTION,
        "Unrelated active Action did not show a recovery step",
    )
    if bpy.ops.motion_bridge.detach_unexpected_active_action() != {"FINISHED"}:
        raise AssertionError("Beginner active Action recovery failed")
    if bpy.data.actions.get(unrelated_action.name) is None:
        raise AssertionError("Active Action recovery deleted the Action data")
    _expect_stage(
        GuideStage.READY_EDIT,
        "Active Action recovery did not return to the editing guide",
    )

    original_scene = bpy.context.scene
    original_stack_id = canonical.get("motion_bridge_stack_id")
    if bpy.ops.scene.new(type="FULL_COPY") != {"FINISHED"}:
        raise AssertionError("Could not create the beginner Full Copy fixture")
    copied_candidates = [
        obj
        for obj in bpy.context.view_layer.objects
        if obj.type == "ARMATURE"
        and obj.get("motion_bridge_stack_id") == original_stack_id
    ]
    if len(copied_candidates) != 1:
        raise AssertionError("Full Copy did not contain exactly one Canonical stack")
    copied_canonical = copied_candidates[0]
    if copied_canonical is canonical:
        raise AssertionError("Full Copy reused the original Canonical Object")
    _select_only(copied_canonical)
    _expect_stage(
        GuideStage.SHARED_CORRECTION,
        "Full Copy did not show the shared-Correction beginner stop",
    )
    shared_layout = _LayoutRecorder()
    addon._draw_beginner_workflow(
        bpy.context,
        shared_layout,
        bpy.context.scene.motion_bridge_session,
    )
    if addon.MB_OT_EnterCorrectionEdit.bl_idname in shared_layout.operators:
        raise AssertionError("Shared-Correction guide still offered edit start")
    shared_text = "\n".join(shared_layout.labels)
    if any(
        implementation_term in shared_text
        for implementation_term in ("NLA", "Correction", "Action", "単一ユーザー")
    ):
        raise AssertionError(
            "Shared-Correction beginner recovery exposed an implementation term: "
            f"{shared_text!r}"
        )
    if "不要な複製シーンを削除" not in shared_text:
        raise AssertionError("Shared-Correction guide omitted its recovery action")
    if addon.MB_OT_EnterCorrectionEdit.poll(bpy.context):
        raise AssertionError("Shared-Correction edit operator poll remained enabled")

    copied_scene = bpy.context.scene
    scene_id_existed = addon._ENVELOPE_SCENE_ID_PROPERTY in copied_scene
    scene_id_before = copied_scene.get(addon._ENVELOPE_SCENE_ID_PROPERTY)
    copied_settings = copied_scene.motion_bridge_session
    tree_before = copied_settings.correction_envelope_curve_tree
    tree_pointer_before = tree_before.as_pointer() if tree_before is not None else None
    node_groups_before = tuple(tree.as_pointer() for tree in bpy.data.node_groups)
    reports: list[tuple[set[str], str]] = []
    result = addon.MB_OT_EnterCorrectionEdit.execute(
        SimpleNamespace(
            report=lambda levels, message: reports.append((set(levels), message))
        ),
        bpy.context,
    )
    if result != {"CANCELLED"} or not reports:
        raise AssertionError("Shared-Correction execute preflight did not cancel")
    report_text = "\n".join(message for _levels, message in reports)
    if any(
        implementation_term in report_text
        for implementation_term in ("NLA", "Correction", "Action", "単一ユーザー")
    ):
        raise AssertionError(
            "Shared-Correction operator error exposed an implementation term: "
            f"{report_text!r}"
        )
    if (addon._ENVELOPE_SCENE_ID_PROPERTY in copied_scene) != scene_id_existed:
        raise AssertionError("Cancelled shared edit changed Envelope Scene ID presence")
    if copied_scene.get(addon._ENVELOPE_SCENE_ID_PROPERTY) != scene_id_before:
        raise AssertionError("Cancelled shared edit changed the Envelope Scene ID")
    tree_after = copied_settings.correction_envelope_curve_tree
    tree_pointer_after = tree_after.as_pointer() if tree_after is not None else None
    if tree_pointer_after != tree_pointer_before:
        raise AssertionError("Cancelled shared edit rebound the Envelope NodeTree")
    if tuple(tree.as_pointer() for tree in bpy.data.node_groups) != node_groups_before:
        raise AssertionError("Cancelled shared edit created an Envelope NodeTree")

    bpy.context.window.scene = original_scene


def main() -> None:
    _clear_scene()
    addon.register()
    try:
        settings = bpy.context.scene.motion_bridge_session
        if settings.beginner_live_details or settings.beginner_edit_details:
            raise AssertionError("Beginner-only detail sections must default closed")
        if "DEFAULT_CLOSED" not in addon.MB_PT_Session.bl_options:
            raise AssertionError("Advanced panel must default closed")
        if addon.MB_PT_GettingStarted.bl_order >= addon.MB_PT_Session.bl_order:
            raise AssertionError("Getting Started panel must be ordered first")
        beginner_tooltip = addon.MB_OT_InsertCorrectionPoseKeys.bl_description
        if any(
            implementation_term in beginner_tooltip
            for implementation_term in (
                "Quaternion",
                "NLA",
                "solver",
                "Profile",
                "time-map",
            )
        ):
            raise AssertionError(
                "Beginner correction tooltip exposed an implementation term: "
                f"{beginner_tooltip!r}"
            )

        no_canonical = addon._workflow_facts(bpy.context)
        if resolve_guide_stage(no_canonical) is not GuideStage.NO_CANONICAL:
            raise AssertionError("Empty scene did not request a Canonical rig")

        canonical = _armature("MB_Canonical_Guide", canonical=True)
        source = _armature("Guide_Source")
        bpy.context.view_layer.objects.active = source
        source.select_set(True)
        select_stage = addon._workflow_facts(bpy.context)
        if resolve_guide_stage(select_stage) is not GuideStage.SELECT_CANONICAL:
            raise AssertionError("Source selection did not request the correction rig")
        if bpy.ops.motion_bridge.activate_canonical_armature() != {"FINISHED"}:
            raise AssertionError("Beginner guide could not select the Canonical rig")
        if bpy.context.active_object is not canonical:
            raise AssertionError("Canonical activation selected the wrong object")

        import_stage = addon._workflow_facts(bpy.context)
        if resolve_guide_stage(import_stage) is not GuideStage.IMPORT_SOURCE:
            raise AssertionError("Empty Canonical did not request a Source Take")
        settings.source_armature = source
        source["motion_bridge_source_frame_start"] = 1.0
        source["motion_bridge_source_frame_end"] = 3692.0
        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = 250
        range_stage = addon._workflow_facts(bpy.context)
        if resolve_guide_stage(range_stage) is not GuideStage.SET_SOURCE_RANGE:
            raise AssertionError("Long Source did not request its complete frame range")
        if bpy.ops.motion_bridge.use_source_frame_range() != {"FINISHED"}:
            raise AssertionError("Beginner guide could not set the Source frame range")
        if (bpy.context.scene.frame_start, bpy.context.scene.frame_end) != (1, 3692):
            raise AssertionError("Beginner guide truncated the imported motion range")
        bake_stage = addon._workflow_facts(bpy.context)
        if resolve_guide_stage(bake_stage) is not GuideStage.BAKE_SOURCE:
            raise AssertionError("Imported Source did not request Canonical bake")

        before = (
            bpy.context.active_object,
            tuple(bpy.context.selected_objects),
            tuple(bpy.data.actions),
            tuple(tuple(row) for row in canonical.matrix_world),
        )
        for _ in range(20):
            addon._workflow_facts(bpy.context)
        after = (
            bpy.context.active_object,
            tuple(bpy.context.selected_objects),
            tuple(bpy.data.actions),
            tuple(tuple(row) for row in canonical.matrix_world),
        )
        if before != after:
            raise AssertionError("Drawing facts mutated Blender scene state")

        canonical.location = (0.0, 0.0, 0.0)
        canonical.keyframe_insert(data_path="location", frame=1)
        canonical.location = (1.0, 0.0, 0.0)
        canonical.keyframe_insert(data_path="location", frame=2)
        animation_data = canonical.animation_data
        foreign_action = animation_data.action if animation_data is not None else None
        if animation_data is None or foreign_action is None:
            raise AssertionError("Could not create foreign NLA guide fixture")
        animation_data.action = None
        foreign_track = animation_data.nla_tracks.new()
        foreign_track.name = "Third Party Animation"
        foreign_track.strips.new("Third Party Animation", 1, foreign_action)
        unsupported_stage = addon._workflow_facts(bpy.context)
        if resolve_guide_stage(unsupported_stage) is not GuideStage.UNSUPPORTED_NLA:
            raise AssertionError(
                "Pre-stack foreign NLA did not produce the safe guide state"
            )
        canonical["motion_bridge_stack_id"] = "synthetic-broken-stack"
        unsupported_stack_stage = addon._workflow_facts(bpy.context)
        if (
            resolve_guide_stage(unsupported_stack_stage)
            is not GuideStage.UNSUPPORTED_NLA
        ):
            raise AssertionError(
                "Stack foreign NLA did not produce the safe guide state"
            )

        _run_beginner_operator_workflow(settings)
        print("MOTION_BRIDGE_BEGINNER_GUIDE_TEST=PASS")
    finally:
        addon.unregister()


if __name__ == "__main__":
    main()
