from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import tempfile

import bpy
from mathutils import Quaternion


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))
TOOLS_ROOT = Path(__file__).resolve().parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from motion_bridge import addon  # noqa: E402
from motion_bridge.editing import (  # noqa: E402
    add_correction_layer,
    create_correction_layer,
    get_correction_layer_stack,
    set_active_correction_layer,
    set_correction_layer_muted,
    set_correction_layer_solo,
    set_corrections_preview_enabled,
    validate_layer_stack,
)
from motion_bridge.editing.action_api import (  # noqa: E402
    action_slot_for_id,
    insert_quaternion_key,
    iter_action_fcurves,
)
from motion_bridge.editing.correction_layers import (  # noqa: E402
    PROP_ACTIVE_LAYER_ID,
    PROP_ARCHIVE_ACTION,
    PROP_ARCHIVE_CHECKSUM,
    PROP_BASE_ACTION,
    PROP_BASE_CHECKSUM,
    PROP_CORRECTIONS_BYPASSED,
    PROP_LAYER_MUTED,
    PROP_LAYER_SOLO,
    action_checksum,
)
from motion_bridge.io import import_source_take  # noqa: E402
from motion_bridge.io.source_take import (  # noqa: E402
    SOURCE_ARCHIVE_CHECKSUM_PROPERTY,
)
from motion_bridge.retargeting import (  # noqa: E402
    bake_archived_source_to_canonical,
    regenerate_canonical_base,
)
from test_source_retarget import (  # noqa: E402
    FIRST_FRAME,
    LAST_FRAME,
    SOURCE_ANCHOR_LOCATION,
    SOURCE_ANCHOR_ROTATION,
    SOURCE_SCALE,
    _assign_action,
    _clear_scene,
    _create_armature,
    _key_source_motion,
)


def _mutate_original_source(action: bpy.types.Action) -> None:
    for curve in iter_action_fcurves(action):
        if curve.data_path == "location" and curve.array_index == 0:
            curve.keyframe_points[-1].co[1] += 7.0
            curve.update()
            return
    raise AssertionError("Synthetic Source Action has no root location curve")


def _corrupt_base(action: bpy.types.Action) -> None:
    curve = next(iter(iter_action_fcurves(action)), None)
    if curve is None:
        raise AssertionError("Synthetic Base has no FCurve")
    curve.keyframe_points[-1].co[1] += 0.125
    curve.update()


def _key_layer(layer: object, axis: tuple[float, float, float], degrees: float) -> None:
    quaternion = Quaternion(axis, math.radians(degrees))
    for frame in (FIRST_FRAME, LAST_FRAME):
        insert_quaternion_key(layer.access, "LeftUpperArm", frame, quaternion)


def _layer_snapshot(armature: bpy.types.Object) -> tuple[tuple[object, ...], ...]:
    stack = get_correction_layer_stack(armature)
    if stack is None:
        raise AssertionError("Correction stack disappeared")
    return tuple(
        (
            layer.layer_id,
            layer.name,
            layer.action.as_pointer(),
            layer.track.as_pointer(),
            layer.strip.as_pointer(),
            action_checksum(layer.action),
            float(layer.strip.influence),
            bool(layer.action.get(PROP_LAYER_MUTED, False)),
            bool(layer.action.get(PROP_LAYER_SOLO, False)),
            bool(layer.track.mute),
            bool(layer.strip.mute),
        )
        for layer in stack.correction_layers
    )


def _source_snapshot(
    armature: bpy.types.Object,
    source_action: bpy.types.Action,
    archive: bpy.types.Action,
) -> tuple[object, ...]:
    animation_data = armature.animation_data
    slot = (
        action_slot_for_id(animation_data.action, armature)
        if animation_data is not None and animation_data.action is not None
        else None
    )
    return (
        action_checksum(source_action),
        action_checksum(archive),
        animation_data.action.as_pointer()
        if animation_data is not None and animation_data.action is not None
        else None,
        getattr(slot, "identifier", ""),
        tuple(
            (bool(track.mute), bool(track.is_solo))
            for track in animation_data.nla_tracks
        )
        if animation_data is not None
        else (),
        tuple(sorted((key, str(value)) for key, value in archive.items())),
        tuple(
            (
                bool(curve.lock),
                bool(curve.hide),
                bool(curve.group.lock) if curve.group is not None else None,
            )
            for curve in iter_action_fcurves(archive)
        ),
    )


def _editor_snapshot() -> tuple[object, ...]:
    active = bpy.context.view_layer.objects.active
    return (
        active.as_pointer() if active is not None else None,
        active.mode if active is not None else None,
        tuple(sorted(obj.name for obj in bpy.context.selected_objects)),
        bpy.context.scene.frame_start,
        bpy.context.scene.frame_end,
        bpy.context.scene.frame_current,
        float(bpy.context.scene.frame_subframe),
    )


def _stack_snapshot(armature: bpy.types.Object) -> tuple[object, ...]:
    stack = get_correction_layer_stack(armature)
    if stack is None:
        raise AssertionError("Correction stack disappeared")
    return (
        stack.base_action.as_pointer(),
        stack.archive_action.as_pointer(),
        str(armature.get(PROP_BASE_ACTION)),
        str(armature.get(PROP_ARCHIVE_ACTION)),
        str(armature.get(PROP_ACTIVE_LAYER_ID)),
        bool(armature.get(PROP_CORRECTIONS_BYPASSED, False)),
        _layer_snapshot(armature),
    )


def _expect_failure(callback: object, message: str) -> None:
    try:
        callback()
    except (RuntimeError, TypeError, ValueError) as error:
        if message not in str(error):
            raise AssertionError(f"Unexpected error: {error}") from error
    else:
        raise AssertionError(f"Expected failure containing {message!r}")


def main() -> None:
    _clear_scene()
    target, _unused = _create_armature(
        "MB_Canonical_Regenerate_Target",
        scale=1.0,
        source=False,
    )
    holder: dict[str, object] = {}

    with tempfile.TemporaryDirectory(prefix="motion_bridge_regenerate_") as directory:
        source_path = Path(directory) / "archived_take.fbx"
        source_path.write_bytes(b"synthetic Motion Bridge Source Take")

        def fake_importer(_path: Path, _format: object) -> set[str]:
            source, mapping = _create_armature(
                "MB_Archived_Source",
                scale=SOURCE_SCALE,
                source=True,
            )
            source.rotation_mode = "QUATERNION"
            source.rotation_quaternion = SOURCE_ANCHOR_ROTATION
            source.location = SOURCE_ANCHOR_LOCATION
            source_action = _key_source_motion(source, mapping)
            holder.update(
                source=source,
                mapping=mapping,
                source_action=source_action,
            )
            return {"FINISHED"}

        imported = import_source_take(
            bpy.context,
            source_path,
            _importer=fake_importer,
        )
        source = imported.armature
        mapping = holder["mapping"]
        source_action = imported.source_action
        source_archive = imported.archive_action

        # The editable imported Action is intentionally changed before the
        # first bake.  The high-level bake must still evaluate SOURCE_ARCHIVE.
        _mutate_original_source(source_action)
        source_mutated_checksum = action_checksum(source_action)
        archived_source_checksum = action_checksum(source_archive)
        if source_mutated_checksum == archived_source_checksum:
            raise AssertionError("Source mutation did not differ from its Archive")

        baked = bake_archived_source_to_canonical(
            bpy.context,
            source,
            target,
            mapping,
            frame_start=FIRST_FRAME,
            frame_end=LAST_FRAME,
        )
        _assign_action(target, baked)
        baked.use_fake_user = False

        # A late stack-creation failure must restore the retarget-baked role,
        # recipe, Action binding, and every temporary datablock exactly.
        create_properties = tuple(sorted((key, str(value)) for key, value in baked.items()))
        create_actions = {action.as_pointer() for action in bpy.data.actions}

        def fail_create(stage: str) -> None:
            if stage == "metadata_written":
                raise RuntimeError("synthetic Correction create failure")

        _expect_failure(
            lambda: create_correction_layer(
                bpy.context,
                target,
                _on_stage=fail_create,
            ),
            "synthetic Correction create failure",
        )
        if (
            target.animation_data.action is not baked
            or target.animation_data.nla_tracks
            or tuple(sorted((key, str(value)) for key, value in baked.items()))
            != create_properties
            or {action.as_pointer() for action in bpy.data.actions} != create_actions
        ):
            raise AssertionError("Correction create failure did not restore Base recipe")

        stack = create_correction_layer(bpy.context, target)
        expected_checksum = str(stack.archive_action[PROP_ARCHIVE_CHECKSUM])
        if action_checksum(stack.base_action) != expected_checksum:
            raise AssertionError("Initial archived bake differs from Canonical Archive")

        first = stack.active_layer
        _key_layer(first, (1.0, 0.0, 0.0), 8.0)
        second = add_correction_layer(bpy.context, target, name="Hands")
        _key_layer(second, (0.0, 1.0, 0.0), 11.0)
        third = add_correction_layer(bpy.context, target, name="Polish")
        _key_layer(third, (0.0, 0.0, 1.0), 14.0)
        refreshed = get_correction_layer_stack(target)
        assert refreshed is not None
        for layer, influence in zip(
            refreshed.correction_layers,
            (0.25, 0.5, 0.75),
        ):
            layer.strip.influence = influence
        set_correction_layer_muted(target, second.layer_id, True)
        set_active_correction_layer(target, third.layer_id)
        set_correction_layer_solo(target, third.layer_id)
        set_corrections_preview_enabled(target, False)

        _corrupt_base(get_correction_layer_stack(target).base_action)
        damaged = validate_layer_stack(target)
        if damaged.is_valid or not any(
            "Base Action content" in error for error in damaged.errors
        ):
            raise AssertionError("Damaged Base was not detected before regeneration")

        source_path.unlink()
        correction_before = _layer_snapshot(target)
        source_before = _source_snapshot(source, source_action, source_archive)
        editor_before = _editor_snapshot()
        old_stack = get_correction_layer_stack(target)
        assert old_stack is not None
        old_base_pointer = old_stack.base_action.as_pointer()
        old_archive_pointer = old_stack.archive_action.as_pointer()

        result = regenerate_canonical_base(bpy.context, target)
        if result.checksum != expected_checksum:
            raise AssertionError("Regeneration returned the wrong Canonical checksum")
        if (
            result.base_action.as_pointer() == old_base_pointer
            or result.archive_action.as_pointer() == old_archive_pointer
        ):
            raise AssertionError("Base/Canonical Archive were not transactionally replaced")
        if action_checksum(result.base_action) != expected_checksum:
            raise AssertionError("Regenerated Base content is not the archived reference")
        if action_checksum(result.archive_action) != expected_checksum:
            raise AssertionError("Regenerated Canonical Archive content differs")
        validate_layer_stack(target).raise_for_errors()
        if _layer_snapshot(target) != correction_before:
            raise AssertionError("Regeneration changed Correction layer identity or state")
        if _source_snapshot(source, source_action, source_archive) != source_before:
            raise AssertionError("Regeneration changed Source Take assets or binding")
        if _editor_snapshot() != editor_before:
            raise AssertionError("Regeneration changed editor state")
        if target.get(PROP_CORRECTIONS_BYPASSED) is not True:
            raise AssertionError("Before preview state was not preserved")
        resolved = get_correction_layer_stack(target)
        assert resolved is not None
        if any(not layer.track.mute for layer in resolved.correction_layers):
            raise AssertionError("Before preview no longer mutes every Correction")
        for layer in resolved.correction_layers:
            if (
                layer.action.get(PROP_BASE_ACTION) != result.base_action.name
                or layer.action.get(PROP_BASE_CHECKSUM) != expected_checksum
            ):
                raise AssertionError("Correction Base references were not updated")

        set_corrections_preview_enabled(target, True)
        resolved = get_correction_layer_stack(target)
        assert resolved is not None
        if tuple(layer.track.mute for layer in resolved.correction_layers) != (
            True,
            True,
            False,
        ):
            raise AssertionError("Configured mute/solo state was not restored in After")
        set_corrections_preview_enabled(target, False)

        # Scene-local datablock names are hints only. Persistent instance UUIDs
        # and saved Action Slot identity must survive ordinary renames.
        source.name = "MB_Archived_Source_Renamed"
        source_archive.name = "SOURCE_ARCHIVE_Renamed"
        target.name = "MB_Canonical_Regenerate_Target_Renamed"
        renamed_result = regenerate_canonical_base(bpy.context, target)
        if renamed_result.checksum != expected_checksum:
            raise AssertionError("Rename-safe regeneration changed the Base checksum")
        validate_layer_stack(target).raise_for_errors()

        rollback_stack = _stack_snapshot(target)
        rollback_source = _source_snapshot(source, source_action, source_archive)
        rollback_editor = _editor_snapshot()
        rollback_actions = {action.as_pointer() for action in bpy.data.actions}

        def fail_frame(frame: int) -> None:
            if frame == 3:
                raise RuntimeError("synthetic regenerate sample failure")

        _expect_failure(
            lambda: regenerate_canonical_base(
                bpy.context,
                target,
                _on_frame=fail_frame,
            ),
            "synthetic regenerate sample failure",
        )
        if (
            _stack_snapshot(target) != rollback_stack
            or _source_snapshot(source, source_action, source_archive) != rollback_source
            or _editor_snapshot() != rollback_editor
            or {action.as_pointer() for action in bpy.data.actions} != rollback_actions
        ):
            raise AssertionError("Sample failure did not roll back exactly")

        for failure_stage in ("candidate_baked", "candidate_prepared"):
            def fail_precommit(stage: str, expected: str = failure_stage) -> None:
                if stage == expected:
                    raise RuntimeError(f"synthetic regenerate {expected} failure")

            _expect_failure(
                lambda: regenerate_canonical_base(
                    bpy.context,
                    target,
                    _on_stage=fail_precommit,
                ),
                f"synthetic regenerate {failure_stage} failure",
            )
            if (
                _stack_snapshot(target) != rollback_stack
                or _source_snapshot(source, source_action, source_archive)
                != rollback_source
                or _editor_snapshot() != rollback_editor
                or {action.as_pointer() for action in bpy.data.actions}
                != rollback_actions
            ):
                raise AssertionError(
                    f"{failure_stage} failure leaked a pending Action"
                )

        def fail_commit(stage: str) -> None:
            if stage == "metadata_committed":
                raise RuntimeError("synthetic regenerate commit failure")

        _expect_failure(
            lambda: regenerate_canonical_base(
                bpy.context,
                target,
                _on_stage=fail_commit,
            ),
            "synthetic regenerate commit failure",
        )
        if (
            _stack_snapshot(target) != rollback_stack
            or _source_snapshot(source, source_action, source_archive) != rollback_source
            or _editor_snapshot() != rollback_editor
            or {action.as_pointer() for action in bpy.data.actions} != rollback_actions
        ):
            raise AssertionError("Commit failure did not roll back exactly")
        validate_layer_stack(target).raise_for_errors()

        saved_source_checksum = source_archive[SOURCE_ARCHIVE_CHECKSUM_PROPERTY]
        source_archive[SOURCE_ARCHIVE_CHECKSUM_PROPERTY] = "0" * 64
        actions_before_preflight = {
            action.as_pointer() for action in bpy.data.actions
        }
        _expect_failure(
            lambda: regenerate_canonical_base(bpy.context, target),
            "checksum metadata",
        )
        source_archive[SOURCE_ARCHIVE_CHECKSUM_PROPERTY] = saved_source_checksum
        if {action.as_pointer() for action in bpy.data.actions} != actions_before_preflight:
            raise AssertionError("Failed preflight created an Action")

        for selected in tuple(bpy.context.selected_objects):
            selected.select_set(False)
        target.select_set(True)
        bpy.context.view_layer.objects.active = target
        addon.register()
        try:
            operator_result = bpy.ops.motion_bridge.regenerate_canonical_base()
            if "FINISHED" not in operator_result:
                raise AssertionError(f"Regenerate operator failed: {operator_result}")
        finally:
            addon.unregister()
        validate_layer_stack(target).raise_for_errors()

    print(
        json.dumps(
            {
                "baseAndArchiveRegenerated": True,
                "commitFailureRollback": True,
                "correctionIdentityPreserved": True,
                "correctionCreateRollback": True,
                "deletedSourceFileSupported": True,
                "operatorWorkflow": True,
                "precommitFailureCleanup": True,
                "preflightRejectedTamper": True,
                "renameResilient": True,
                "sampleFailureRollback": True,
                "sourceArchiveAuthority": True,
                "status": "PASS",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
