from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
from typing import Callable, NoReturn, cast

import bpy


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from motion_bridge.editing.action_api import (  # noqa: E402
    create_action_for_id,
    insert_quaternion_key,
    iter_action_fcurves,
)
from motion_bridge import addon  # noqa: E402
from motion_bridge.canonical.rig_spec import SKELETON_ID  # noqa: E402
from motion_bridge.io import SourceFileFormat, import_source_take  # noqa: E402
from motion_bridge.io.source_import import (  # noqa: E402
    SourceTakeImport,
    SourceTakeImportError,
    SourceTakeSelectionRequired,
    _dispatch_fbx_import,
)
from motion_bridge.retargeting.profile import HUMANOID_MAPPING_FIELDS  # noqa: E402


FIRST_FRAME = 1
LAST_FRAME = 18


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for action in tuple(bpy.data.actions):
        bpy.data.actions.remove(action)
    for armature in tuple(bpy.data.armatures):
        if armature.users == 0:
            bpy.data.armatures.remove(armature)
    for mesh in tuple(bpy.data.meshes):
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def _create_animated_source() -> tuple[bpy.types.Object, bpy.types.Action]:
    _clear_scene()
    armature_data = bpy.data.armatures.new("SourceRigData")
    armature = bpy.data.objects.new("SourceRig", armature_data)
    bpy.context.scene.collection.objects.link(armature)
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode="EDIT")
    hips = armature_data.edit_bones.new("Hips")
    hips.head = (0.0, 0.0, 0.0)
    hips.tail = (0.0, 0.0, 1.0)
    spine = armature_data.edit_bones.new("Spine")
    spine.head = hips.tail
    spine.tail = (0.0, 0.0, 1.7)
    spine.parent = hips
    spine.use_connect = True
    bpy.ops.object.mode_set(mode="POSE")

    pose_bone = armature.pose.bones["Hips"]
    pose_bone.rotation_mode = "QUATERNION"
    bpy.context.scene.frame_set(FIRST_FRAME)
    pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    pose_bone.keyframe_insert(
        data_path="rotation_quaternion", frame=FIRST_FRAME, group="Hips"
    )
    bpy.context.scene.frame_set(LAST_FRAME)
    pose_bone.rotation_quaternion = (
        math.cos(math.radians(20.0)),
        0.0,
        0.0,
        math.sin(math.radians(20.0)),
    )
    pose_bone.keyframe_insert(
        data_path="rotation_quaternion", frame=LAST_FRAME, group="Hips"
    )
    action = armature.animation_data.action if armature.animation_data else None
    if action is None:
        raise AssertionError("Synthetic animation did not create an Action")
    action.name = "SourceTakeWalk"
    bpy.ops.object.mode_set(mode="OBJECT")
    return armature, action


def _export_fbx(armature: bpy.types.Object, filepath: Path) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature
    result = bpy.ops.export_scene.fbx(
        filepath=str(filepath),
        check_existing=False,
        use_selection=True,
        object_types={"ARMATURE"},
        add_leaf_bones=False,
        bake_anim=True,
        bake_anim_use_all_bones=True,
        bake_anim_use_all_actions=False,
        bake_anim_use_nla_strips=False,
        bake_anim_simplify_factor=0.0,
    )
    if "FINISHED" not in result:
        raise AssertionError(f"Synthetic FBX export failed: {result}")


def _export_glb(armature: bpy.types.Object, filepath: Path) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature
    result = bpy.ops.export_scene.gltf(
        filepath=str(filepath),
        export_format="GLB",
        use_selection=True,
        export_animations=True,
        export_frame_range=True,
        export_force_sampling=True,
    )
    if "FINISHED" not in result:
        raise AssertionError(f"Synthetic glTF export failed: {result}")


def _create_state_anchor() -> tuple[bpy.types.Object, bpy.types.Object]:
    anchor_data = bpy.data.armatures.new("EditorStateAnchorData")
    anchor = bpy.data.objects.new("EditorStateAnchor", anchor_data)
    companion = bpy.data.objects.new("EditorStateCompanion", None)
    bpy.context.scene.collection.objects.link(anchor)
    bpy.context.scene.collection.objects.link(companion)
    anchor.select_set(True)
    companion.select_set(True)
    bpy.context.view_layer.objects.active = anchor
    bpy.ops.object.mode_set(mode="EDIT")
    bone = anchor_data.edit_bones.new("Anchor")
    bone.head = (0.0, 0.0, 0.0)
    bone.tail = (0.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode="POSE")

    scene = bpy.context.scene
    scene.frame_start = 12
    scene.frame_end = 144
    scene.use_preview_range = True
    scene.frame_preview_start = 3
    scene.frame_preview_end = 80
    scene.frame_set(37, subframe=0.25)
    return anchor, companion


def _assert_editor_state(
    anchor: bpy.types.Object,
    companion: bpy.types.Object,
) -> None:
    context = bpy.context
    if context.view_layer.objects.active is not anchor:
        raise AssertionError("Active object was not restored")
    if {obj.as_pointer() for obj in context.selected_objects} != {
        anchor.as_pointer(),
        companion.as_pointer(),
    }:
        raise AssertionError("Selection was not restored")
    if anchor.mode != "POSE":
        raise AssertionError(f"Mode was not restored: {anchor.mode}")
    scene = context.scene
    if (scene.frame_start, scene.frame_end) != (12, 144):
        raise AssertionError(
            f"Timeline range was not restored: {(scene.frame_start, scene.frame_end)}"
        )
    if not scene.use_preview_range or (
        scene.frame_preview_start,
        scene.frame_preview_end,
    ) != (3, 80):
        raise AssertionError("Preview range was not restored")
    if scene.frame_current != 37 or abs(scene.frame_subframe - 0.25) > 1e-7:
        raise AssertionError("Current frame was not restored")


def _curve_signature(action: bpy.types.Action) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        sorted(
            (
                curve.data_path,
                curve.array_index,
                len(curve.keyframe_points) + len(curve.sampled_points),
            )
            for curve in iter_action_fcurves(action)
        )
    )


def _verify_success(
    filepath: Path,
    expected_format: SourceFileFormat,
    anchor: bpy.types.Object,
    companion: bpy.types.Object,
) -> dict[str, object]:
    result = import_source_take(bpy.context, filepath)
    _assert_editor_state(anchor, companion)
    if result.file_format is not expected_format:
        raise AssertionError(f"Unexpected Source Take format: {result.file_format}")
    if result.armature.type != "ARMATURE":
        raise AssertionError("Imported Source Take did not return an armature")
    if result.source_action is result.archive_action:
        raise AssertionError("Source and archive Actions must be distinct")
    if result.source_action.get("motion_bridge_archive_kind") is not None:
        raise AssertionError("Original imported Action was mutated")
    if not result.archive_action.use_fake_user:
        raise AssertionError("SOURCE_ARCHIVE Action has no fake user")
    if result.archive_action.get("motion_bridge_archive_kind") != "SOURCE_ARCHIVE":
        raise AssertionError("Archive kind was not recorded")
    if result.archive_action.get("motion_bridge_archive_immutable") is not True:
        raise AssertionError("Archive immutability marker was not recorded")
    for curve in iter_action_fcurves(result.archive_action):
        if not curve.lock or not curve.hide:
            raise AssertionError("Archive FCurve is not locked and hidden")
        if curve.group is not None and not curve.group.lock:
            raise AssertionError("Archive FCurve group is not locked")
    if _curve_signature(result.source_action) != _curve_signature(
        result.archive_action
    ):
        raise AssertionError("Archive animation differs from the original Action")

    expected_hash = hashlib.sha256(filepath.read_bytes()).hexdigest()
    if result.provenance.file_sha256 != expected_hash:
        raise AssertionError("Source file hash mismatch")
    properties = result.provenance.custom_properties()
    for name, value in properties.items():
        if result.armature.get(name) != value:
            raise AssertionError(f"Armature provenance mismatch: {name}")
        if result.archive_action.get(name) != value:
            raise AssertionError(f"Archive provenance mismatch: {name}")
    return {
        "format": expected_format.value,
        "armature": result.armature.name,
        "action": result.source_action.name,
        "archive": result.archive_action.name,
        "usableActions": len(result.usable_actions),
        "frameRange": [
            result.provenance.frame_start,
            result.provenance.frame_end,
        ],
        "fileSha256": result.provenance.file_sha256,
        "hierarchySha256": result.provenance.bone_hierarchy_sha256,
    }


def _snapshot_counts() -> dict[str, int]:
    return {
        name: len(getattr(bpy.data, name))
        for name in (
            "objects",
            "collections",
            "actions",
            "shape_keys",
            "meshes",
            "armatures",
            "materials",
            "images",
        )
    }


def _transaction_pointer_snapshot() -> dict[str, frozenset[int]]:
    return {
        name: frozenset(item.as_pointer() for item in getattr(bpy.data, name))
        for name in ("objects", "collections", "actions", "armatures")
    }


_CallbackOwnedData = tuple[
    bpy.types.Collection,
    bpy.types.Object,
    bpy.types.Armature,
    bpy.types.Action,
]


def _create_callback_owned_data(label: str) -> _CallbackOwnedData:
    collection = bpy.data.collections.new(f"{label} Collection")
    bpy.context.scene.collection.children.link(collection)
    armature = bpy.data.armatures.new(f"{label} Armature")
    obj = bpy.data.objects.new(f"{label} Object", armature)
    collection.objects.link(obj)
    action = bpy.data.actions.new(f"{label} Action")
    return collection, obj, armature, action


def _callback_pointer_additions(
    data: _CallbackOwnedData,
) -> dict[str, frozenset[int]]:
    collection, obj, armature, action = data
    return {
        "collections": frozenset((collection.as_pointer(),)),
        "objects": frozenset((obj.as_pointer(),)),
        "armatures": frozenset((armature.as_pointer(),)),
        "actions": frozenset((action.as_pointer(),)),
    }


def _remove_callback_owned_data(data: _CallbackOwnedData) -> None:
    collection, obj, armature, action = data
    if any(candidate is obj for candidate in bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    if any(candidate is collection for candidate in bpy.data.collections):
        bpy.data.collections.remove(collection, do_unlink=True)
    if any(candidate is action for candidate in bpy.data.actions):
        bpy.data.actions.remove(action)
    if (
        any(candidate is armature for candidate in bpy.data.armatures)
        and armature.users == 0
    ):
        bpy.data.armatures.remove(armature)


def _rna_pointer(item: bpy.types.ID | None) -> int:
    return 0 if item is None else item.as_pointer()


def _source_session_signature(settings: bpy.types.PropertyGroup) -> tuple[object, ...]:
    return (
        _rna_pointer(settings.source_armature),
        _rna_pointer(settings.manual_mapping_bound_source),
        str(settings.manual_mapping_profile_json),
        str(settings.manual_mapping_profiles_json),
        str(settings.retarget_preset),
        str(settings.beginner_retarget_error),
        tuple(
            (field.property_name, getattr(settings.manual_mapping, field.property_name))
            for field in HUMANOID_MAPPING_FIELDS
        ),
    )


def _source_identity_signature(source: bpy.types.Object) -> tuple[object, ...]:
    return tuple(
        (name, name in source, source.get(name))
        for name in (
            "motion_bridge_source_instance_id",
            "motion_bridge_mapping_source_instance_id",
            "motion_bridge_mapping_source_action_checksum",
        )
    )


def _armature_visual_signature(armature: bpy.types.Object) -> tuple[object, ...]:
    data = armature.data
    view_layer = bpy.context.view_layer
    hidden = (
        bool(armature.hide_get(view_layer=view_layer))
        if view_layer.objects.get(armature.name) is armature
        else None
    )
    collections = tuple(
        sorted(
            (
                collection.name,
                collection.get("motion_bridge_visual_role"),
                bool(collection.is_visible),
                tuple(sorted(bone.name for bone in collection.bones)),
            )
            for collection in data.collections_all
        )
    )
    bones = tuple(
        sorted(
            (
                bone.name,
                str(bone.color.palette),
                tuple(float(value) for value in bone.color.custom.normal),
                tuple(float(value) for value in bone.color.custom.select),
                tuple(float(value) for value in bone.color.custom.active),
                tuple(sorted(collection.name for collection in bone.collections)),
            )
            for bone in data.bones
        )
    )
    return (
        str(data.display_type),
        bool(data.show_names),
        bool(data.show_bone_colors),
        bool(armature.show_in_front),
        hidden,
        collections,
        bones,
    )


def _rig_visual_signature(
    canonical: bpy.types.Object,
    sources: tuple[bpy.types.Object, ...],
) -> tuple[object, ...]:
    return (
        addon.has_rig_visualization_snapshot(canonical),
        _armature_visual_signature(canonical),
        tuple(_armature_visual_signature(source) for source in sources),
    )


def _synthetic_failure(
    _filepath: Path,
    _file_format: SourceFileFormat,
) -> NoReturn:
    collection = bpy.data.collections.new("LeakedImportCollection")
    bpy.context.scene.collection.children.link(collection)
    mesh = bpy.data.meshes.new("LeakedImportMesh")
    obj = bpy.data.objects.new("LeakedImportObject", mesh)
    collection.objects.link(obj)
    obj.shape_key_add(name="Basis")
    armature_data = bpy.data.armatures.new("LeakedImportArmatureData")
    armature = bpy.data.objects.new("LeakedImportArmature", armature_data)
    collection.objects.link(armature)
    bpy.data.actions.new("LeakedImportAction")
    raise RuntimeError("synthetic importer failure")


def _two_armature_import(
    _filepath: Path,
    _file_format: SourceFileFormat,
) -> set[str]:
    for index in range(2):
        data = bpy.data.armatures.new(f"InvalidArmatureData{index}")
        obj = bpy.data.objects.new(f"InvalidArmature{index}", data)
        bpy.context.scene.collection.objects.link(obj)
    return {"FINISHED"}


def _no_action_import(
    _filepath: Path,
    _file_format: SourceFileFormat,
) -> set[str]:
    data = bpy.data.armatures.new("NoActionArmatureData")
    obj = bpy.data.objects.new("NoActionArmature", data)
    bpy.context.scene.collection.objects.link(obj)
    return {"FINISHED"}


def _unbound_action_import(
    _filepath: Path,
    _file_format: SourceFileFormat,
) -> set[str]:
    data = bpy.data.armatures.new("UnboundActionArmatureData")
    obj = bpy.data.objects.new("UnboundActionArmature", data)
    bpy.context.scene.collection.objects.link(obj)
    access = create_action_for_id(
        "UnboundPoseLookingAction",
        obj,
        frame_start=1.0,
        frame_end=2.0,
    )
    insert_quaternion_key(access, "Hips", 1.0, (1.0, 0.0, 0.0, 0.0))
    return {"FINISHED"}


def _multiple_action_import(
    _filepath: Path,
    _file_format: SourceFileFormat,
) -> set[str]:
    data = bpy.data.armatures.new("MultipleActionArmatureData")
    obj = bpy.data.objects.new("MultipleActionArmature", data)
    bpy.context.scene.collection.objects.link(obj)
    for selected in tuple(bpy.context.selected_objects):
        selected.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    hips = data.edit_bones.new("Hips")
    hips.head = (0.0, 0.0, 0.0)
    hips.tail = (0.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode="OBJECT")

    walk = create_action_for_id(
        "Walk Take",
        obj,
        frame_start=1.0,
        frame_end=12.0,
    )
    insert_quaternion_key(walk, "Hips", 1.0, (1.0, 0.0, 0.0, 0.0))
    insert_quaternion_key(walk, "Hips", 12.0, (1.0, 0.0, 0.0, 0.0))
    animation_data = obj.animation_data_create()
    animation_data.action = walk.action

    run = create_action_for_id(
        "Run Take",
        obj,
        frame_start=1.0,
        frame_end=12.0,
    )
    insert_quaternion_key(run, "Hips", 1.0, (1.0, 0.0, 0.0, 0.0))
    insert_quaternion_key(run, "Hips", 12.0, (0.0, 0.0, 0.0, 1.0))
    return {"FINISHED"}


def _verify_failure_rollback(
    filepath: Path,
    anchor: bpy.types.Object,
    companion: bpy.types.Object,
) -> None:
    before = _snapshot_counts()
    try:
        import_source_take(bpy.context, filepath, _importer=_synthetic_failure)
    except RuntimeError as error:
        if str(error) != "synthetic importer failure":
            raise
    else:
        raise AssertionError("Synthetic importer failure was not propagated")
    if _snapshot_counts() != before:
        raise AssertionError(
            f"Failed import leaked datablocks: {before} -> {_snapshot_counts()}"
        )
    _assert_editor_state(anchor, companion)


def _verify_validation_rollbacks(
    filepath: Path,
    anchor: bpy.types.Object,
    companion: bpy.types.Object,
) -> None:
    before = _snapshot_counts()
    for importer, expected_messages in (
        (_two_armature_import, ("exactly one armature",)),
        (_no_action_import, ("at least one Action",)),
        (_unbound_action_import, ("at least one Action",)),
        (
            _multiple_action_import,
            (
                "multiple usable Actions",
                "'Walk Take' [frames 1..12]",
                "'Run Take' [frames 1..12]",
            ),
        ),
    ):
        try:
            import_source_take(bpy.context, filepath, _importer=importer)
        except SourceTakeImportError as error:
            if any(message not in str(error) for message in expected_messages):
                raise AssertionError(
                    f"Unexpected validation failure: {error}"
                ) from error
            if importer is _multiple_action_import:
                if not isinstance(error, SourceTakeSelectionRequired):
                    raise AssertionError("Multiple Actions did not request selection")
                actual_candidates = tuple(
                    (
                        candidate.action_name,
                        candidate.frame_start,
                        candidate.frame_end,
                    )
                    for candidate in error.candidates
                )
                if actual_candidates != (
                    ("Walk Take", 1.0, 12.0),
                    ("Run Take", 1.0, 12.0),
                ):
                    raise AssertionError(
                        f"Unexpected take candidates: {actual_candidates!r}"
                    )
        else:
            raise AssertionError("Invalid Source Take passed validation")
        if _snapshot_counts() != before:
            raise AssertionError("Validation failure leaked imported datablocks")
        _assert_editor_state(anchor, companion)


def _verify_explicit_take_selection(
    filepath: Path,
    anchor: bpy.types.Object,
    companion: bpy.types.Object,
) -> None:
    try:
        import_source_take(
            bpy.context,
            filepath,
            _importer=_multiple_action_import,
        )
    except SourceTakeSelectionRequired as selection:
        take_ids = {candidate.action_name: candidate.take_id for candidate in selection.candidates}
    else:
        raise AssertionError("Multiple same-range takes were selected implicitly")
    if len(set(take_ids.values())) != 2:
        raise AssertionError("Same-range Source Takes received the same take ID")
    expected_ids = tuple(take_ids.values())

    # Blender suffixes imported datablock and Action Slot names when a workfile
    # already contains the same labels. Those scene-local names must not alter
    # the identity of takes from identical source bytes.
    collision_data = bpy.data.armatures.new("MultipleActionArmatureData")
    collision_object = bpy.data.objects.new("MultipleActionArmature", collision_data)
    collision_actions = (
        bpy.data.actions.new("Walk Take"),
        bpy.data.actions.new("Run Take"),
    )
    try:
        try:
            import_source_take(
                bpy.context,
                filepath,
                _importer=_multiple_action_import,
            )
        except SourceTakeSelectionRequired as selection:
            collision_ids = tuple(
                candidate.take_id for candidate in selection.candidates
            )
        else:
            raise AssertionError("Name-collision import selected a take implicitly")
    finally:
        for action in collision_actions:
            bpy.data.actions.remove(action)
        bpy.data.objects.remove(collision_object)
        if collision_data.users == 0:
            bpy.data.armatures.remove(collision_data)
    if collision_ids != expected_ids:
        raise AssertionError(
            "Datablock name collisions changed stable take IDs: "
            f"{expected_ids!r} -> {collision_ids!r}"
        )
    _assert_editor_state(anchor, companion)

    run_take_id = take_ids["Run Take"]
    result = import_source_take(
        bpy.context,
        filepath,
        selected_take_id=run_take_id,
        _importer=_multiple_action_import,
    )
    _assert_editor_state(anchor, companion)
    if result.source_action.name != "Run Take":
        raise AssertionError("Explicit take selection chose the wrong Action")
    animation_data = result.armature.animation_data
    if animation_data is None or animation_data.action is not result.source_action:
        raise AssertionError("Displayed Source rig did not bind the selected take")
    if any(not track.mute for track in animation_data.nla_tracks):
        raise AssertionError(
            "Source NLA still layers over the explicitly selected take"
        )
    if result.provenance.action_name != "Run Take":
        raise AssertionError("Selected take was not recorded in provenance")
    if tuple(result.source_action.frame_range) != (1.0, 12.0):
        raise AssertionError("Selected take frame range was not retained")
    if result.archive_action.get("motion_bridge_source_action") != "Run Take":
        raise AssertionError("Selected take was not recorded on the Archive")
    if result.armature.get("motion_bridge_source_action") != "Run Take":
        raise AssertionError("Selected take was not recorded on the Source rig")
    if result.provenance.take_id != run_take_id:
        raise AssertionError("Selected take ID changed before provenance")
    if result.provenance.archive_id != run_take_id:
        raise AssertionError("Selected take ID changed before Archive identity")
    if result.archive_action.get("motion_bridge_source_take_id") != run_take_id:
        raise AssertionError("Selected take ID was not recorded on the Archive")
    if result.armature.get("motion_bridge_source_take_id") != run_take_id:
        raise AssertionError("Selected take ID was not recorded on the Source rig")

    before = _snapshot_counts()
    try:
        import_source_take(
            bpy.context,
            filepath,
            selected_take_id="f" * 64,
            _importer=_multiple_action_import,
        )
    except SourceTakeImportError as error:
        if "was not found uniquely" not in str(error):
            raise
    else:
        raise AssertionError("Missing explicit take selection was accepted")
    if _snapshot_counts() != before:
        raise AssertionError("Invalid explicit take selection leaked datablocks")
    _assert_editor_state(anchor, companion)


def _verify_operator_transaction_rollback(
    filepath: Path,
    anchor: bpy.types.Object,
    companion: bpy.types.Object,
) -> None:
    bpy.ops.object.mode_set(mode="EDIT")
    hips = anchor.data.edit_bones.new("Hips")
    hips.head = (0.0, 0.0, 1.0)
    hips.tail = (0.0, 0.0, 1.2)
    bpy.ops.object.mode_set(mode="POSE")
    anchor["motion_bridge_skeleton_id"] = SKELETON_ID

    addon.register()
    try:
        settings = bpy.context.scene.motion_bridge_session
        settings.manual_mapping_profiles_json = (
            '{"profiles":{"keep":"future-profile"},"schemaVersion":1}'
        )
        settings.retarget_preset = "EXACT"
        settings.beginner_retarget_error = "existing message"
        first_mapping_property = HUMANOID_MAPPING_FIELDS[0].property_name
        setattr(settings.manual_mapping, first_mapping_property, "Anchor")

        def capture_rollback_state(
            *sources: bpy.types.Object,
        ) -> tuple[object, ...]:
            return (
                _transaction_pointer_snapshot(),
                _source_session_signature(settings),
                tuple(
                    (source, _source_identity_signature(source))
                    for source in sources
                ),
                _rig_visual_signature(anchor, sources),
            )

        def assert_rollback(
            label: str,
            before: tuple[object, ...],
            *,
            retained: dict[str, frozenset[int]] | None = None,
        ) -> None:
            ids_before, session_before, source_properties, rig_visual_before = before
            additions = retained or {}
            expected_ids = {
                name: pointers | additions.get(name, frozenset())
                for name, pointers in ids_before.items()
            }
            if _transaction_pointer_snapshot() != expected_ids:
                raise AssertionError(f"{label} leaked or removed datablocks")
            if _source_session_signature(settings) != session_before:
                raise AssertionError(f"{label} changed the existing Session")
            for source, properties_before in source_properties:
                if _source_identity_signature(source) != properties_before:
                    raise AssertionError(
                        f"{label} changed the existing Source identity"
                    )
            if _rig_visual_signature(
                anchor,
                tuple(source for source, _properties in source_properties),
            ) != rig_visual_before:
                raise AssertionError(f"{label} changed the Rig View state")
            _assert_editor_state(anchor, companion)

        if addon.has_rig_visualization_snapshot(anchor):
            raise AssertionError("Rig View was unexpectedly applied before import")
        before = capture_rollback_state()
        ids_before = before[0]
        known_objects = ids_before["objects"]
        handler_called = False
        handler_owned: _CallbackOwnedData | None = None

        def remove_imported_source(
            _scene: bpy.types.Scene,
            _depsgraph: bpy.types.Depsgraph,
        ) -> None:
            nonlocal handler_called, handler_owned
            for obj in tuple(bpy.data.objects):
                if (
                    obj.as_pointer() not in known_objects
                    and obj.type == "ARMATURE"
                    and obj.get("motion_bridge_source_schema_version") == 2
                ):
                    handler_called = True
                    handler_owned = _create_callback_owned_data("Frame Callback")
                    bpy.data.objects.remove(obj, do_unlink=True)
                    return

        bpy.app.handlers.frame_change_post.append(remove_imported_source)
        try:
            try:
                operator_result = bpy.ops.motion_bridge.import_source_take(
                    filepath=str(filepath)
                )
            except RuntimeError as error:
                if "removed before the import could finish" not in str(error):
                    raise
                operator_result = {"CANCELLED"}
        finally:
            if remove_imported_source in bpy.app.handlers.frame_change_post:
                bpy.app.handlers.frame_change_post.remove(remove_imported_source)
        if "CANCELLED" not in operator_result:
            raise AssertionError(
                "Source import succeeded after a frame handler removed its target"
            )
        if not handler_called:
            raise AssertionError("Source import rollback handler was not exercised")
        if handler_owned is None:
            raise AssertionError("Frame callback did not create its independent IDs")
        try:
            assert_rollback(
                "Frame-handler Source removal",
                before,
                retained=_callback_pointer_additions(handler_owned),
            )
        finally:
            _remove_callback_owned_data(handler_owned)

        original_import = addon.import_source_take

        def invoke_post_commit_failure(
            after_commit: Callable[[], None] | None = None,
        ) -> None:
            def fail_after_pointer_commit(
                context: bpy.types.Context,
                import_filepath: str | Path,
                **kwargs: object,
            ) -> SourceTakeImport:
                operator_commit = cast(
                    Callable[[SourceTakeImport], None],
                    kwargs.pop("_commit"),
                )

                def commit_then_fail(result: SourceTakeImport) -> None:
                    operator_commit(result)
                    if after_commit is not None:
                        after_commit()
                    raise RuntimeError(
                        "synthetic failure after Session pointer commit"
                    )

                return original_import(
                    context,
                    import_filepath,
                    _commit=commit_then_fail,
                    **kwargs,
                )

            addon.import_source_take = fail_after_pointer_commit
            try:
                try:
                    operator_result = bpy.ops.motion_bridge.import_source_take(
                        filepath=str(filepath)
                    )
                except RuntimeError as error:
                    if "synthetic failure after Session pointer commit" not in str(
                        error
                    ):
                        raise
                    operator_result = {"CANCELLED"}
            finally:
                addon.import_source_take = original_import
            if "CANCELLED" not in operator_result:
                raise AssertionError(
                    "Source import ignored a post-pointer commit failure"
                )

        before = capture_rollback_state()
        commit_owned: _CallbackOwnedData | None = None

        def create_commit_owned_data() -> None:
            nonlocal commit_owned
            commit_owned = _create_callback_owned_data("Commit Callback")

        invoke_post_commit_failure(create_commit_owned_data)
        if commit_owned is None:
            raise AssertionError("Commit callback did not create its independent IDs")
        try:
            assert_rollback(
                "Unapplied Rig View pointer commit failure",
                before,
                retained=_callback_pointer_additions(commit_owned),
            )
        finally:
            _remove_callback_owned_data(commit_owned)

        old_source_data = bpy.data.armatures.new("ExistingSourceData")
        old_source = bpy.data.objects.new("ExistingSource", old_source_data)
        bpy.context.scene.collection.objects.link(old_source)
        old_source_data.display_type = "BBONE"
        old_source_data.show_names = True
        old_source.show_in_front = True
        canonical_unapplied = _armature_visual_signature(anchor)
        source_unapplied = _armature_visual_signature(old_source)
        settings.source_armature = old_source
        if not addon.has_rig_visualization_snapshot(anchor):
            raise AssertionError("Rig View was not applied to the existing Source")

        before = capture_rollback_state(old_source)
        invoke_post_commit_failure()
        assert_rollback("Applied Rig View pointer commit failure", before)
        if not addon.restore_rig_visualization(anchor):
            raise AssertionError("Restored Rig View lost its runtime snapshot")
        if addon.has_rig_visualization_snapshot(anchor):
            raise AssertionError("Rig View runtime snapshot was not released")
        if _armature_visual_signature(anchor) != canonical_unapplied:
            raise AssertionError("Canonical Rig View snapshot changed during rollback")
        if _armature_visual_signature(old_source) != source_unapplied:
            raise AssertionError("Existing Source snapshot changed during rollback")
    finally:
        addon.unregister()


def _verify_fbx_compatibility_fallback(filepath: Path) -> None:
    leaked_name = "FastImporterPartialObject"

    def cancelled_fast(**_kwargs: object) -> set[str]:
        obj = bpy.data.objects.new(leaked_name, None)
        bpy.context.scene.collection.objects.link(obj)
        return {"CANCELLED"}

    def successful_legacy(**kwargs: object) -> set[str]:
        if kwargs.get("use_anim") is not True or kwargs.get("anim_offset") != 0.0:
            raise AssertionError("Legacy importer did not receive animation options")
        return {"FINISHED"}

    result = _dispatch_fbx_import(
        filepath,
        fast_operator=cancelled_fast,
        legacy_operator=successful_legacy,
    )
    if "FINISHED" not in result:
        raise AssertionError(f"FBX fallback failed: {result}")
    if bpy.data.objects.get(leaked_name) is not None:
        raise AssertionError("Native importer partial data leaked into fallback")


def main() -> None:
    with tempfile.TemporaryDirectory(
        prefix="motion_bridge_source_import_"
    ) as directory:
        root = Path(directory)
        armature, _action = _create_animated_source()
        fbx_path = root / "source_take.fbx"
        glb_path = root / "source_take.glb"
        _export_fbx(armature, fbx_path)
        _export_glb(armature, glb_path)

        _clear_scene()
        anchor, companion = _create_state_anchor()
        report = {
            "fbx": _verify_success(
                fbx_path,
                SourceFileFormat.FBX,
                anchor,
                companion,
            ),
            "glb": _verify_success(
                glb_path,
                SourceFileFormat.GLB,
                anchor,
                companion,
            ),
        }
        _verify_failure_rollback(fbx_path, anchor, companion)
        _verify_validation_rollbacks(fbx_path, anchor, companion)
        _verify_explicit_take_selection(fbx_path, anchor, companion)
        _verify_operator_transaction_rollback(fbx_path, anchor, companion)
        _verify_fbx_compatibility_fallback(fbx_path)
        _assert_editor_state(anchor, companion)
        report["failureRollback"] = True
        report["nativeFallback"] = True
        report["operatorTransactionRollback"] = True
        report["callbackOwnershipRollback"] = True
        report["takePicker"] = True
        print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, RuntimeError, SourceTakeImportError):
        import traceback

        traceback.print_exc()
        raise SystemExit(1) from None
