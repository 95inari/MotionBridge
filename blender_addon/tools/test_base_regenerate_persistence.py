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

from motion_bridge.editing import (  # noqa: E402
    add_correction_layer,
    create_correction_layer,
    duplicate_correction_layer,
    enter_correction_edit_mode,
    get_correction_layer_stack,
    merge_correction_layer_down,
    move_correction_layer,
    remove_correction_layer,
    rename_correction_layer,
    set_active_correction_layer,
    set_correction_layer_muted,
    set_correction_layer_solo,
    set_corrections_preview_enabled,
    validate_layer_stack,
)
from motion_bridge.editing.action_api import (  # noqa: E402
    insert_quaternion_key,
    iter_action_fcurves,
)
from motion_bridge.editing.correction_layers import (  # noqa: E402
    PROP_ACTIVE_LAYER_ID,
    PROP_ARCHIVE_CHECKSUM,
    PROP_CORRECTIONS_BYPASSED,
    PROP_LAYER_MUTED,
    PROP_LAYER_SOLO,
    PROP_STACK_ID,
    action_checksum,
)
from motion_bridge.io import import_source_take  # noqa: E402
from motion_bridge.io.source_take import (  # noqa: E402
    SOURCE_ACTION_SLOT_PROPERTY,
    SOURCE_ARCHIVE_ACTION_PROPERTY,
    SOURCE_ARCHIVE_CHECKSUM_ALGORITHM,
    SOURCE_ARCHIVE_CHECKSUM_ALGORITHM_PROPERTY,
    SOURCE_ARCHIVE_CHECKSUM_PROPERTY,
    SOURCE_ARCHIVE_KIND,
    SOURCE_INSTANCE_ID_PROPERTY,
    SOURCE_TAKE_ID_PROPERTY,
)
from motion_bridge.retargeting import (  # noqa: E402
    PROP_RETARGET_RECIPE,
    RetargetRecipe,
    bake_archived_source_to_canonical,
    regenerate_canonical_base,
)
from motion_bridge.retargeting.regenerate import (  # noqa: E402
    _source_assets_from_recipe,
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


def _key_layer(layer: object, axis: tuple[float, float, float], degrees: float) -> None:
    quaternion = Quaternion(axis, math.radians(degrees))
    for frame in (FIRST_FRAME, LAST_FRAME):
        insert_quaternion_key(layer.access, "LeftUpperArm", frame, quaternion)


def _corrupt_base(action: bpy.types.Action) -> None:
    curve = next(iter(iter_action_fcurves(action)), None)
    if curve is None:
        raise AssertionError("Synthetic Base has no FCurve")
    curve.keyframe_points[-1].co[1] += 0.125
    curve.update()


def _persistent_layer_snapshot(
    armature: bpy.types.Object,
) -> tuple[object, ...]:
    stack = get_correction_layer_stack(armature)
    if stack is None:
        raise AssertionError("Correction stack disappeared")
    return (
        str(armature.get(PROP_STACK_ID, "")),
        str(armature.get(PROP_ACTIVE_LAYER_ID, "")),
        bool(armature.get(PROP_CORRECTIONS_BYPASSED, False)),
        tuple(
            (
                layer.layer_id,
                layer.name,
                layer.action.name,
                action_checksum(layer.action),
                layer.track.name,
                layer.strip.name,
                float(layer.strip.influence),
                bool(layer.action.get(PROP_LAYER_MUTED, False)),
                bool(layer.action.get(PROP_LAYER_SOLO, False)),
                bool(layer.track.mute),
                bool(layer.strip.mute),
            )
            for layer in stack.correction_layers
        ),
    )


def _runtime_layer_snapshot(
    armature: bpy.types.Object,
) -> tuple[tuple[object, ...], ...]:
    stack = get_correction_layer_stack(armature)
    if stack is None:
        raise AssertionError("Correction stack disappeared")
    return tuple(
        (
            layer.layer_id,
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


def _find_unique(items: list[object], label: str) -> object:
    if len(items) != 1:
        raise AssertionError(f"Expected exactly one {label}, found {len(items)}")
    return items[0]


def _assert_recipe_copies(
    target: bpy.types.Object,
    expected_json: str,
) -> RetargetRecipe:
    stack = get_correction_layer_stack(target)
    if stack is None:
        raise AssertionError("Correction stack disappeared")
    copies = (
        target.get(PROP_RETARGET_RECIPE),
        stack.base_action.get(PROP_RETARGET_RECIPE),
        stack.archive_action.get(PROP_RETARGET_RECIPE),
    )
    if copies != (expected_json, expected_json, expected_json):
        raise AssertionError("Persisted retarget recipe copies diverged")
    return RetargetRecipe.from_json(expected_json)


def main() -> None:
    _clear_scene()
    target, _unused = _create_armature(
        "MB_Persisted_Regenerate_Target",
        scale=1.0,
        source=False,
    )
    holder: dict[str, object] = {}

    with tempfile.TemporaryDirectory(
        prefix="motion_bridge_regenerate_persistence_"
    ) as directory:
        temporary_root = Path(directory)
        source_path = temporary_root / "archived_take.fbx"
        blend_path = temporary_root / "regenerate_roundtrip.blend"
        source_path.write_bytes(b"synthetic persisted Motion Bridge Source Take")

        def fake_importer(_path: Path, _format: object) -> set[str]:
            source, mapping = _create_armature(
                "MB_Persisted_Source",
                scale=SOURCE_SCALE,
                source=True,
            )
            source.rotation_mode = "QUATERNION"
            source.rotation_quaternion = SOURCE_ANCHOR_ROTATION
            source.location = SOURCE_ANCHOR_LOCATION
            holder["mapping"] = mapping
            holder["source_action"] = _key_source_motion(source, mapping)
            return {"FINISHED"}

        imported = import_source_take(
            bpy.context,
            source_path,
            _importer=fake_importer,
        )
        source = imported.armature
        source_archive = imported.archive_action
        mapping = holder["mapping"]

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
        stack = create_correction_layer(bpy.context, target)

        first = stack.active_layer
        _key_layer(first, (1.0, 0.0, 0.0), 7.0)
        second = add_correction_layer(bpy.context, target, name="Hands")
        _key_layer(second, (0.0, 1.0, 0.0), 13.0)
        third = add_correction_layer(bpy.context, target, name="Polish")
        _key_layer(third, (0.0, 0.0, 1.0), 19.0)
        refreshed = get_correction_layer_stack(target)
        assert refreshed is not None
        for layer, influence in zip(
            refreshed.correction_layers,
            (0.2, 0.55, 0.8),
        ):
            layer.strip.influence = influence
        set_correction_layer_muted(target, second.layer_id, True)
        set_active_correction_layer(target, third.layer_id)
        set_correction_layer_solo(target, third.layer_id)
        set_corrections_preview_enabled(target, False)

        stack = get_correction_layer_stack(target)
        assert stack is not None
        expected_checksum = str(stack.archive_action[PROP_ARCHIVE_CHECKSUM])
        expected_recipe_json = str(stack.archive_action[PROP_RETARGET_RECIPE])
        expected_recipe = _assert_recipe_copies(target, expected_recipe_json)
        expected_instance_id = str(source[SOURCE_INSTANCE_ID_PROPERTY])
        expected_source_archive_id = str(
            source_archive["motion_bridge_source_archive_id"]
        )
        expected_take_id = str(source_archive[SOURCE_TAKE_ID_PROPERTY])
        expected_source_archive_checksum = str(
            source_archive[SOURCE_ARCHIVE_CHECKSUM_PROPERTY]
        )
        expected_source_slot = str(source[SOURCE_ACTION_SLOT_PROPERTY])
        expected_archive_hint = str(source[SOURCE_ARCHIVE_ACTION_PROPERTY])
        expected_stack_id = str(target[PROP_STACK_ID])

        if expected_recipe.source_instance_id != expected_instance_id:
            raise AssertionError("Recipe did not save the Source instance UUID")
        if expected_recipe.source_archive_id != expected_source_archive_id:
            raise AssertionError("Recipe did not save the Source Archive id")
        if expected_recipe.source_archive_id != expected_take_id:
            raise AssertionError("Selected take ID changed before the recipe")
        if expected_recipe.source_slot_identifier != expected_source_slot:
            raise AssertionError("Recipe did not save the Source Action Slot")
        if expected_recipe.source_archive_checksum != expected_source_archive_checksum:
            raise AssertionError("Recipe did not save the Source Archive checksum")
        if expected_recipe.source_filepath != str(source_path.resolve()):
            raise AssertionError("Recipe did not save the original Source filepath")

        _corrupt_base(stack.base_action)
        source_path.unlink()
        if source_path.exists():
            raise AssertionError("Synthetic original Source file was not deleted")
        persistent_layers = _persistent_layer_snapshot(target)

        save_result = bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
        if "FINISHED" not in save_result or not blend_path.is_file():
            raise AssertionError(f"Failed to save persistence fixture: {save_result}")
        open_result = bpy.ops.wm.open_mainfile(filepath=str(blend_path))
        if "FINISHED" not in open_result:
            raise AssertionError(f"Failed to reopen persistence fixture: {open_result}")
        if Path(bpy.data.filepath).resolve() != blend_path.resolve():
            raise AssertionError("Blender did not reopen the saved persistence fixture")
        if source_path.exists():
            raise AssertionError("Reopening the .blend unexpectedly restored Source bytes")

        source = _find_unique(
            [
                obj
                for obj in bpy.data.objects
                if obj.type == "ARMATURE"
                and obj.get(SOURCE_INSTANCE_ID_PROPERTY) == expected_instance_id
            ],
            "persisted Source Take",
        )
        target = _find_unique(
            [
                obj
                for obj in bpy.data.objects
                if obj.type == "ARMATURE"
                and obj.get(PROP_STACK_ID) == expected_stack_id
            ],
            "persisted Canonical target",
        )
        source_archive = _find_unique(
            [
                action
                for action in bpy.data.actions
                if action.get(SOURCE_INSTANCE_ID_PROPERTY) == expected_instance_id
                and action.get("motion_bridge_archive_kind") == SOURCE_ARCHIVE_KIND
            ],
            "persisted Source Archive",
        )

        if _persistent_layer_snapshot(target) != persistent_layers:
            raise AssertionError("Correction identity/state changed across .blend reload")
        persisted_recipe = _assert_recipe_copies(target, expected_recipe_json)
        if persisted_recipe != expected_recipe:
            raise AssertionError("Retarget recipe changed across .blend reload")
        if source.get(SOURCE_ARCHIVE_ACTION_PROPERTY) != expected_archive_hint:
            raise AssertionError("Source Archive name hint changed across .blend reload")
        if source.get(SOURCE_ACTION_SLOT_PROPERTY) != expected_source_slot:
            raise AssertionError("Source Action Slot changed across .blend reload")
        if source_archive.get("motion_bridge_source_archive_id") != expected_source_archive_id:
            raise AssertionError("Source Archive id changed across .blend reload")
        if (
            source_archive.get(SOURCE_ARCHIVE_CHECKSUM_PROPERTY)
            != expected_source_archive_checksum
            or source_archive.get(SOURCE_ARCHIVE_CHECKSUM_ALGORITHM_PROPERTY)
            != SOURCE_ARCHIVE_CHECKSUM_ALGORITHM
            or action_checksum(source_archive) != expected_source_archive_checksum
        ):
            raise AssertionError("Source Archive checksum changed across .blend reload")
        slot_identifiers = tuple(
            getattr(slot, "identifier", "")
            for slot in getattr(source_archive, "slots", ())
        )
        if slot_identifiers.count(expected_source_slot) != 1:
            raise AssertionError("Saved Source Action Slot did not survive .blend reload")

        damaged = validate_layer_stack(target)
        if damaged.is_valid or not any(
            "Base Action content" in error for error in damaged.errors
        ):
            raise AssertionError("Persisted damaged Base was not detected after reload")

        source.name = "MB_Persisted_Source_Renamed_After_Reload"
        source_archive.name = "SOURCE_ARCHIVE_Renamed_After_Reload"
        target.name = "MB_Persisted_Target_Renamed_After_Reload"
        correction_before_regenerate = _runtime_layer_snapshot(target)
        old_stack = get_correction_layer_stack(target)
        assert old_stack is not None
        old_base_pointer = old_stack.base_action.as_pointer()
        old_archive_pointer = old_stack.archive_action.as_pointer()

        result = regenerate_canonical_base(bpy.context, target)
        if result.checksum != expected_checksum:
            raise AssertionError("Persisted regeneration returned the wrong checksum")
        if (
            result.base_action.as_pointer() == old_base_pointer
            or result.archive_action.as_pointer() == old_archive_pointer
        ):
            raise AssertionError("Persisted Base/Archive were not replaced")
        if (
            action_checksum(result.base_action) != expected_checksum
            or action_checksum(result.archive_action) != expected_checksum
        ):
            raise AssertionError("Persisted regeneration changed Canonical content")
        if _runtime_layer_snapshot(target) != correction_before_regenerate:
            raise AssertionError("Regeneration changed persisted Correction identity/state")
        if _persistent_layer_snapshot(target) != persistent_layers:
            raise AssertionError("Regeneration changed persisted Correction metadata/state")
        if (
            source.get(SOURCE_INSTANCE_ID_PROPERTY) != expected_instance_id
            or source.get(SOURCE_ACTION_SLOT_PROPERTY) != expected_source_slot
            or source_archive.get(SOURCE_INSTANCE_ID_PROPERTY) != expected_instance_id
            or action_checksum(source_archive) != expected_source_archive_checksum
        ):
            raise AssertionError("Regeneration changed persisted Source identity/content")
        _assert_recipe_copies(target, expected_recipe_json)
        validate_layer_stack(target).raise_for_errors()

        if target.get(PROP_CORRECTIONS_BYPASSED) is not True:
            raise AssertionError("Before preview state did not survive reload/regeneration")
        regenerated_stack = get_correction_layer_stack(target)
        assert regenerated_stack is not None
        if any(not layer.track.mute for layer in regenerated_stack.correction_layers):
            raise AssertionError("Before preview no longer mutes every Correction")
        set_corrections_preview_enabled(target, True)
        regenerated_stack = get_correction_layer_stack(target)
        assert regenerated_stack is not None
        if tuple(layer.track.mute for layer in regenerated_stack.correction_layers) != (
            True,
            True,
            False,
        ):
            raise AssertionError("Mute/solo state was not restored after persisted Before")

        original_scene = bpy.context.scene
        source_archive_pointer = source_archive.as_pointer()
        source_archive_content = action_checksum(source_archive)
        if bpy.ops.scene.new(type="FULL_COPY") != {"FINISHED"}:
            raise AssertionError("Could not create a Full Copy Scene")

        copied_source = _find_unique(
            [
                obj
                for obj in bpy.context.view_layer.objects
                if obj.type == "ARMATURE"
                and obj.get(SOURCE_INSTANCE_ID_PROPERTY) == expected_instance_id
            ],
            "Full Copy Source Take in the current View Layer",
        )
        copied_target = _find_unique(
            [
                obj
                for obj in bpy.context.view_layer.objects
                if obj.type == "ARMATURE"
                and obj.get(PROP_STACK_ID) == expected_stack_id
            ],
            "Full Copy Canonical target in the current View Layer",
        )
        if copied_source is source or copied_target is target:
            raise AssertionError("Full Copy Scene reused an original armature Object")
        original_stack_state = _persistent_layer_snapshot(target)
        copied_stack_state = _persistent_layer_snapshot(copied_target)
        copied_recipe = _assert_recipe_copies(copied_target, expected_recipe_json)
        copied_assets = _source_assets_from_recipe(bpy.context, copied_recipe)
        if copied_assets.armature is not copied_source:
            raise AssertionError("Full Copy recipe resolved the Source from another Scene")
        if copied_assets.archive_action.as_pointer() != source_archive_pointer:
            raise AssertionError("Full Copy recipe did not reuse the immutable Source Archive")

        copied_stack = get_correction_layer_stack(copied_target)
        if copied_stack is None or len(copied_stack.correction_layers) < 2:
            raise AssertionError("Full Copy lost the persisted Correction layers")
        copied_active_id = copied_stack.active_layer_id
        copied_first_id = copied_stack.correction_layers[0].layer_id

        def expect_shared_correction_block(
            label: str,
            mutation: object,
        ) -> None:
            before_original = _persistent_layer_snapshot(target)
            before_copy = _persistent_layer_snapshot(copied_target)
            try:
                mutation()
            except ValueError as error:
                if "unshared Correction Actions" not in str(error):
                    raise AssertionError(
                        f"{label} returned the wrong shared-Action error: {error}"
                    ) from error
            else:
                raise AssertionError(f"{label} changed a shared Correction stack")
            if (
                _persistent_layer_snapshot(target) != before_original
                or _persistent_layer_snapshot(copied_target) != before_copy
            ):
                raise AssertionError(
                    f"{label} mutated an original or Full Copy Correction stack"
                )
            validate_layer_stack(target).raise_for_errors()
            validate_layer_stack(copied_target).raise_for_errors()

        shared_mutations = (
            (
                "idempotent create",
                lambda: create_correction_layer(bpy.context, copied_target),
            ),
            (
                "add layer",
                lambda: add_correction_layer(
                    bpy.context,
                    copied_target,
                    name="Must Not Add",
                ),
            ),
            (
                "duplicate layer",
                lambda: duplicate_correction_layer(
                    bpy.context,
                    copied_target,
                    copied_active_id,
                ),
            ),
            (
                "select layer",
                lambda: set_active_correction_layer(
                    copied_target,
                    copied_first_id,
                ),
            ),
            (
                "rename layer",
                lambda: rename_correction_layer(
                    copied_target,
                    copied_active_id,
                    "Must Not Rename",
                ),
            ),
            (
                "reorder layer",
                lambda: move_correction_layer(
                    copied_target,
                    copied_active_id,
                    -1,
                ),
            ),
            (
                "mute layer",
                lambda: set_correction_layer_muted(
                    copied_target,
                    copied_active_id,
                    True,
                ),
            ),
            (
                "solo layer",
                lambda: set_correction_layer_solo(
                    copied_target,
                    copied_active_id,
                ),
            ),
            (
                "remove layer",
                lambda: remove_correction_layer(
                    copied_target,
                    copied_active_id,
                ),
            ),
            (
                "Before preview",
                lambda: set_corrections_preview_enabled(copied_target, False),
            ),
            (
                "merge layers",
                lambda: merge_correction_layer_down(
                    bpy.context,
                    copied_target,
                    copied_active_id,
                ),
            ),
            (
                "Correction Edit",
                lambda: enter_correction_edit_mode(bpy.context, copied_target),
            ),
        )
        for label, mutation in shared_mutations:
            expect_shared_correction_block(label, mutation)

        try:
            regenerate_canonical_base(bpy.context, copied_target)
        except ValueError as error:
            if "unshared" not in str(error):
                raise
        else:
            raise AssertionError("Full Copy regenerated a shared Correction stack")
        if (
            _persistent_layer_snapshot(target) != original_stack_state
            or _persistent_layer_snapshot(copied_target) != copied_stack_state
        ):
            raise AssertionError("Blocked Full Copy regeneration changed a stack")
        validate_layer_stack(target).raise_for_errors()
        validate_layer_stack(copied_target).raise_for_errors()

        source_archives = [
            action
            for action in bpy.data.actions
            if action.get(SOURCE_INSTANCE_ID_PROPERTY) == expected_instance_id
            and action.get("motion_bridge_archive_kind") == SOURCE_ARCHIVE_KIND
        ]
        if len(source_archives) != 1:
            raise AssertionError("Full Copy duplicated the immutable Source Archive")
        if (
            action_checksum(source_archives[0]) != source_archive_content
            or source_archives[0].get(SOURCE_ARCHIVE_CHECKSUM_PROPERTY)
            != expected_source_archive_checksum
        ):
            raise AssertionError("Full Copy changed the immutable Source Archive")

        bpy.context.window.scene = original_scene
        original_assets = _source_assets_from_recipe(bpy.context, expected_recipe)
        if original_assets.armature is not source:
            raise AssertionError("Original recipe resolved the Full Copy Source")
        if original_assets.archive_action.as_pointer() != source_archive_pointer:
            raise AssertionError("Original recipe lost the shared Source Archive")
        try:
            regenerate_canonical_base(bpy.context, target)
        except ValueError as error:
            if "unshared" not in str(error):
                raise
        else:
            raise AssertionError("Original Scene regenerated a shared Correction stack")
        if (
            _persistent_layer_snapshot(target) != original_stack_state
            or _persistent_layer_snapshot(copied_target) != copied_stack_state
        ):
            raise AssertionError("Blocked original regeneration changed a stack")
        validate_layer_stack(target).raise_for_errors()
        validate_layer_stack(copied_target).raise_for_errors()

    print(
        json.dumps(
            {
                "correctionIdentityStatePersisted": True,
                "deletedSourceFileSupportedAfterReload": True,
                "recipePersisted": True,
                "regenerateChecksumMatched": True,
                "renameAfterReloadSupported": True,
                "sceneFullCopyRegenerateSafeBlock": True,
                "sceneFullCopySharedCorrectionSafeBlock": True,
                "sceneFullCopyScopedSource": True,
                "sceneFullCopySharedArchiveSafe": True,
                "sourceIdentityArchiveSlotPersisted": True,
                "status": "PASS",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
