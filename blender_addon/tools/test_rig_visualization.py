from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

import bpy
from mathutils import Vector


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from motion_bridge.canonical.rig_spec import JOINTS, SKELETON_ID  # noqa: E402
from motion_bridge.canonical.runtime_descriptor import (  # noqa: E402
    descriptor_from_armature,
)
from motion_bridge.editing.action_api import iter_action_fcurves  # noqa: E402
from motion_bridge.editing.rig_visualization import (  # noqa: E402
    BONE_ROLE_STYLES,
    CANONICAL_BONE_ROLES,
    VISUAL_COLLECTION_PROPERTY,
    apply_rig_visualization,
    restore_rig_visualization,
)


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)


def _create_canonical() -> bpy.types.Object:
    data = bpy.data.armatures.new("MB_Rig_View_Canonical_Data")
    armature = bpy.data.objects.new("MB_Rig_View_Canonical", data)
    bpy.context.scene.collection.objects.link(armature)
    armature["motion_bridge_skeleton_id"] = SKELETON_ID
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    created: dict[str, bpy.types.EditBone] = {}
    for joint in JOINTS[1:]:
        assert joint.head is not None and joint.tail is not None
        bone = data.edit_bones.new(joint.name)
        bone.head = joint.head
        bone.tail = joint.tail
        if joint.parent_index > 0:
            parent = created[JOINTS[joint.parent_index].name]
            bone.parent = parent
            bone.use_connect = (bone.head - parent.tail).length <= 1.0e-7
        created[joint.name] = bone
    bpy.ops.object.mode_set(mode="OBJECT")
    for joint in JOINTS[1:]:
        bone = data.bones[joint.name]
        bone["motion_bridge_index"] = joint.index
        bone["motion_bridge_human_bone"] = joint.human_bone or ""
        bone["motion_bridge_required"] = joint.required
    return armature


def _create_source(name: str = "MB_Rig_View_Source") -> bpy.types.Object:
    data = bpy.data.armatures.new(f"{name}_Data")
    source = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(source)
    bpy.context.view_layer.objects.active = source
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    source.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bone = data.edit_bones.new("ReferenceBone")
    bone.head = (0.0, 0.0, 0.0)
    bone.tail = (0.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode="OBJECT")
    return source


def _action_snapshot(armature: bpy.types.Object) -> tuple[object, ...]:
    animation_data = armature.animation_data
    action = animation_data.action if animation_data is not None else None
    if action is None:
        return ()
    return tuple(
        (
            curve.data_path,
            curve.array_index,
            tuple(
                (
                    float(point.co[0]),
                    float(point.co[1]),
                    str(point.interpolation),
                )
                for point in curve.keyframe_points
            ),
        )
        for curve in iter_action_fcurves(action)
    )


def _managed_collections(data: bpy.types.Armature) -> tuple[bpy.types.BoneCollection, ...]:
    return tuple(
        collection
        for collection in data.collections_all
        if collection.get(VISUAL_COLLECTION_PROPERTY)
    )


def _assert_persisted(canonical: bpy.types.Object) -> None:
    if canonical.data.display_type != "STICK" or not canonical.data.show_bone_colors:
        raise AssertionError("Correction rig display settings were not persisted")
    if len(_managed_collections(canonical.data)) != len(BONE_ROLE_STYLES):
        raise AssertionError("Role collections were not persisted")
    if any(
        canonical.data.bones[name].color.palette != "CUSTOM"
        for name in CANONICAL_BONE_ROLES
    ):
        raise AssertionError("Bone role colors were not persisted")


def main() -> None:
    _clear_scene()
    canonical = _create_canonical()
    source = _create_source()
    replacement_source = _create_source("MB_Rig_View_Source_Replacement")
    artist_collection = canonical.data.collections.new("Artist Collection")
    artist_collection.assign(canonical.data.bones["Hips"])
    colliding_collection = canonical.data.collections.new("MB | Torso")
    colliding_collection.assign(canonical.data.bones["Head"])
    canonical.data["motion_bridge_visual_schema"] = 1

    source.data.display_type = "ENVELOPE"
    source.data.show_names = True
    source.show_in_front = True
    source.hide_set(False)
    replacement_source.data.display_type = "BBONE"
    replacement_source.data.show_names = True
    replacement_source.show_in_front = True
    replacement_source.hide_set(False)

    bpy.context.view_layer.objects.active = canonical
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    canonical.select_set(True)
    hips = canonical.pose.bones["Hips"]
    hips.rotation_mode = "QUATERNION"
    hips.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    hips.keyframe_insert(data_path="rotation_quaternion", frame=1.0, group="Hips")
    hips.rotation_quaternion = (0.98, 0.0, 0.0, 0.198997)
    hips.keyframe_insert(data_path="rotation_quaternion", frame=2.0, group="Hips")

    descriptor_before = descriptor_from_armature(canonical)
    action_before = _action_snapshot(canonical)
    matrices_before = {
        bone.name: tuple(value for row in bone.matrix_local for value in row)
        for bone in canonical.data.bones
    }
    canonical_display_before = (
        canonical.data.display_type,
        canonical.data.show_names,
        canonical.data.show_bone_colors,
        canonical.show_in_front,
        canonical.data.bones["Hips"].color.palette,
    )

    result = apply_rig_visualization(
        bpy.context,
        canonical,
        source_armature=source,
        canonical_style="STICK",
        source_visible=True,
        wire_opacity=0.27,
        show_names=True,
    )
    if result.colored_bones != len(JOINTS) - 1:
        raise AssertionError(f"Unexpected colored bone count: {result.colored_bones}")
    if result.role_collections != len(BONE_ROLE_STYLES):
        raise AssertionError("Not every body role received a collection")
    if canonical.data.display_type != "STICK" or not canonical.show_in_front:
        raise AssertionError("Correction rig was not changed to a thin editable view")
    if not canonical.data.show_names or not canonical.data.show_bone_colors:
        raise AssertionError("Correction rig label/color settings were not applied")
    if source.data.display_type != "WIRE" or source.show_in_front:
        raise AssertionError("Source was not configured as a background wire reference")
    if source.hide_get():
        raise AssertionError("Visible source reference was unexpectedly hidden")
    if artist_collection.name not in canonical.data.bones["Hips"].collections:
        raise AssertionError("Existing artist Bone Collection was modified")
    if colliding_collection.get(VISUAL_COLLECTION_PROPERTY) is not None:
        raise AssertionError("User-owned collection name collision was commandeered")
    if "motion_bridge_visual_schema" in canonical.data:
        raise AssertionError("Visual-only schema would leak into FBX custom properties")

    managed = _managed_collections(canonical.data)
    if len(managed) != len(BONE_ROLE_STYLES):
        raise AssertionError("Managed role collection count is incorrect")
    for bone_name, expected_role in CANONICAL_BONE_ROLES.items():
        bone = canonical.data.bones[bone_name]
        assigned_roles = {
            collection.get(VISUAL_COLLECTION_PROPERTY)
            for collection in bone.collections
            if collection.get(VISUAL_COLLECTION_PROPERTY)
        }
        if assigned_roles != {expected_role}:
            raise AssertionError(
                f"{bone_name} role assignment mismatch: {assigned_roles!r}"
            )
        if bone.color.palette != "CUSTOM":
            raise AssertionError(f"{bone_name} did not receive a custom role color")

    left = Vector(canonical.data.bones["LeftUpperArm"].color.custom.normal)
    right = Vector(canonical.data.bones["RightUpperArm"].color.custom.normal)
    if (left - right).length < 0.25:
        raise AssertionError("Left and right controls are not visually distinct")

    second = apply_rig_visualization(
        bpy.context,
        canonical,
        source_armature=source,
        canonical_style="STICK",
        source_visible=False,
        wire_opacity=0.27,
        show_names=True,
    )
    if len(_managed_collections(canonical.data)) != len(BONE_ROLE_STYLES):
        raise AssertionError("Repeated application duplicated role collections")
    if not source.hide_get() or not second.source_configured:
        raise AssertionError("Source Reference visibility toggle did not apply")

    apply_rig_visualization(
        bpy.context,
        canonical,
        source_armature=replacement_source,
        canonical_style="STICK",
        source_visible=True,
        wire_opacity=0.27,
        show_names=True,
    )
    if (
        source.data.display_type != "ENVELOPE"
        or not source.data.show_names
        or not source.show_in_front
        or source.hide_get()
    ):
        raise AssertionError("Replaced Source reference did not restore its prior view")
    if (
        replacement_source.data.display_type != "WIRE"
        or replacement_source.show_in_front
    ):
        raise AssertionError("Replacement Source was not configured as a reference")

    if descriptor_from_armature(canonical) != descriptor_before:
        raise AssertionError("Rig visualization changed the skeleton descriptor")
    matrices_after = {
        bone.name: tuple(value for row in bone.matrix_local for value in row)
        for bone in canonical.data.bones
    }
    if matrices_after != matrices_before:
        raise AssertionError("Rig visualization changed Canonical bind transforms")
    if _action_snapshot(canonical) != action_before:
        raise AssertionError("Rig visualization changed Action/FCurve content")

    if not restore_rig_visualization(canonical):
        raise AssertionError("Rig View snapshot was not restorable")
    canonical_display_after_restore = (
        canonical.data.display_type,
        canonical.data.show_names,
        canonical.data.show_bone_colors,
        canonical.show_in_front,
        canonical.data.bones["Hips"].color.palette,
    )
    if canonical_display_after_restore != canonical_display_before:
        raise AssertionError("Correction rig display/color state was not restored")
    if _managed_collections(canonical.data):
        raise AssertionError("Created role collections were not removed on restore")
    if (
        artist_collection.name not in canonical.data.bones["Hips"].collections
        or colliding_collection.name not in canonical.data.bones["Head"].collections
    ):
        raise AssertionError("Restore changed existing Bone Collections")
    if (
        replacement_source.data.display_type != "BBONE"
        or not replacement_source.data.show_names
        or not replacement_source.show_in_front
        or replacement_source.hide_get()
    ):
        raise AssertionError("Restore did not recover replacement Source display")

    shared_source = bpy.data.objects.new(
        "MB_Rig_View_Shared_Source",
        replacement_source.data,
    )
    bpy.context.scene.collection.objects.link(shared_source)
    try:
        try:
            apply_rig_visualization(
                bpy.context,
                canonical,
                source_armature=replacement_source,
            )
        except ValueError as exception:
            if "single-user" not in str(exception):
                raise
        else:
            raise AssertionError("Shared Source Armature data was not rejected")
    finally:
        bpy.data.objects.remove(shared_source, do_unlink=True)

    result = apply_rig_visualization(
        bpy.context,
        canonical,
        source_armature=source,
        canonical_style="STICK",
        source_visible=True,
        wire_opacity=0.27,
        show_names=True,
    )

    with tempfile.TemporaryDirectory(prefix="motion_bridge_rig_view_") as temp_dir:
        blend_path = Path(temp_dir) / "rig_view.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(blend_path), check_existing=False)
        bpy.ops.wm.open_mainfile(filepath=str(blend_path), load_ui=False)
        persisted = bpy.data.objects.get("MB_Rig_View_Canonical")
        if persisted is None:
            raise AssertionError("Saved Correction rig is missing")
        _assert_persisted(persisted)

    print(
        "MOTION_BRIDGE_RIG_VISUALIZATION_TEST="
        + json.dumps(
            {
                "status": "PASS",
                "coloredBones": result.colored_bones,
                "roleCollections": result.role_collections,
                "sourceWire": True,
                "sourceVisibility": True,
                "sourceSwapRestore": True,
                "idempotent": True,
                "explicitRestore": True,
                "descriptorUnchanged": True,
                "actionUnchanged": True,
                "fbxMetadataClean": True,
                "sharedDataRejected": True,
                "persistence": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
