from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

import bpy


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))
TOOLS_ROOT = Path(__file__).resolve().parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from motion_bridge import addon  # noqa: E402
from motion_bridge.retargeting import RETARGET_BAKED_ROLE  # noqa: E402
from motion_bridge.retargeting.profile import (  # noqa: E402
    HUMANOID_MAPPING_FIELDS,
    SourceMappingProfile,
)
from test_source_retarget import (  # noqa: E402
    FIRST_FRAME,
    LAST_FRAME,
    _clear_scene,
    _create_armature,
    _key_source_motion,
)


def _profile_values(settings: bpy.types.PropertyGroup) -> dict[str, str]:
    profile = settings.manual_mapping
    return {
        field.human_bone: getattr(profile, field.property_name)
        for field in HUMANOID_MAPPING_FIELDS
    }


def _activate_only(armature: bpy.types.Object) -> None:
    for selected in tuple(bpy.context.selected_objects):
        selected.select_set(False)
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature


def main() -> None:
    _clear_scene()
    source, expected_mapping = _create_armature(
        "MB_Manual_Source",
        scale=1.4,
        source=True,
    )
    _key_source_motion(source, expected_mapping)
    target, _ = _create_armature(
        "MB_Manual_Target",
        scale=1.0,
        source=False,
    )
    _activate_only(target)
    bpy.context.scene.frame_start = FIRST_FRAME
    bpy.context.scene.frame_end = LAST_FRAME

    addon.register()
    try:
        settings = bpy.context.scene.motion_bridge_session
        settings.source_armature = source
        result = bpy.ops.motion_bridge.populate_manual_mapping()
        if "FINISHED" not in result or settings.retarget_preset != "MANUAL":
            raise AssertionError(f"Auto Populate did not enter Manual mode: {result}")
        if _profile_values(settings) != expected_mapping:
            raise AssertionError("Auto Populate did not fill the 22-bone profile")
        source_profile = SourceMappingProfile.from_json(
            settings.manual_mapping_profile_json
        )
        if source_profile.mapping != expected_mapping:
            raise AssertionError("Manual mapping was not saved in its Source profile")
        if len(settings.manual_mapping.bl_rna.properties) < 22:
            raise AssertionError("Manual profile RNA properties were not registered")

        result = bpy.ops.motion_bridge.clear_manual_mapping()
        if "FINISHED" not in result or any(_profile_values(settings).values()):
            raise AssertionError("Clear did not blank all manual mapping fields")
        cleared_profile = SourceMappingProfile.from_json(
            settings.manual_mapping_profile_json
        )
        if cleared_profile.mapping:
            raise AssertionError("Clear retained values in the Source profile")
        if "FINISHED" not in bpy.ops.motion_bridge.populate_manual_mapping():
            raise AssertionError("Manual profile could not be repopulated")

        left_hand_property = next(
            field.property_name
            for field in HUMANOID_MAPPING_FIELDS
            if field.human_bone == "LeftHand"
        )
        setattr(settings.manual_mapping, left_hand_property, "")
        edited_profile = SourceMappingProfile.from_json(
            settings.manual_mapping_profile_json
        )
        if "LeftHand" in edited_profile.mapping:
            raise AssertionError("Manual field edit did not update the Source profile")
        setattr(
            settings.manual_mapping, left_hand_property, expected_mapping["LeftHand"]
        )
        restored_profile = SourceMappingProfile.from_json(
            settings.manual_mapping_profile_json
        )
        if restored_profile.mapping != expected_mapping:
            raise AssertionError("Restored manual field did not update the profile")

        primary_json = settings.manual_mapping_profile_json
        primary_values = _profile_values(settings)
        second_scene = bpy.data.scenes.new("MB_Manual_Second_Scene")
        second_settings = second_scene.motion_bridge_session
        second_settings.source_armature = source
        setattr(second_settings.manual_mapping, left_hand_property, "")
        if settings.manual_mapping_profile_json != primary_json:
            raise AssertionError("Editing another Scene changed the active profile")
        if _profile_values(settings) != primary_values:
            raise AssertionError("Editing another Scene changed active mapping fields")
        bpy.data.scenes.remove(second_scene)

        legacy_source, legacy_expected_mapping = _create_armature(
            "MB_Legacy_Source",
            scale=0.9,
            source=True,
        )
        _key_source_motion(legacy_source, legacy_expected_mapping)
        legacy_scene = bpy.data.scenes.new("MB_Manual_Legacy_Scene")
        try:
            legacy_settings = legacy_scene.motion_bridge_session
            legacy_settings.source_armature = source
            addon._set_manual_humanoid_mapping(
                legacy_settings,
                expected_mapping,
            )
            legacy_settings.manual_mapping_profile_json = ""
            legacy_settings.manual_mapping_profiles_json = ""

            legacy_settings.source_armature = legacy_source
            source_identity = addon._source_mapping_identity(
                source,
                create=False,
                verify_content=False,
            )
            migrated_registry = addon._manual_mapping_registry(legacy_settings)
            migrated_profile_json = migrated_registry.get(
                source_identity["source_instance_id"]
            )
            if not migrated_profile_json:
                raise AssertionError(
                    "Source switch discarded a legacy mapping instead of registering it"
                )
            migrated_profile = SourceMappingProfile.from_json(
                migrated_profile_json
            )
            if migrated_profile.mapping != expected_mapping:
                raise AssertionError(
                    "Source switch changed the legacy mapping stored for its old Source"
                )

            legacy_settings.source_armature = source
            if _profile_values(legacy_settings) != expected_mapping:
                raise AssertionError(
                    "Returning to the old Source did not restore its migrated legacy mapping"
                )
            restored_legacy_profile = SourceMappingProfile.from_json(
                legacy_settings.manual_mapping_profile_json
            )
            if restored_legacy_profile.mapping != expected_mapping:
                raise AssertionError(
                    "Returning to the old Source restored the wrong legacy profile"
                )
        finally:
            bpy.data.scenes.remove(legacy_scene)

        registry_before = settings.manual_mapping_profiles_json
        profile_before = settings.manual_mapping_profile_json
        settings.manual_mapping_profiles_json = json.dumps(
            {"profiles": {}, "schemaVersion": 99},
            sort_keys=True,
        )
        unknown_registry = settings.manual_mapping_profiles_json
        setattr(settings.manual_mapping, left_hand_property, "")
        if settings.manual_mapping_profiles_json != unknown_registry:
            raise AssertionError("Unknown profile registry schema was overwritten")
        settings.manual_mapping_profiles_json = registry_before
        settings.manual_mapping_profile_json = profile_before
        setattr(
            settings.manual_mapping, left_hand_property, expected_mapping["LeftHand"]
        )

        settings.manual_mapping_profile_json = ""
        migrated_mapping = addon._manual_humanoid_mapping(
            settings,
            bind_current=True,
        )
        if migrated_mapping != expected_mapping:
            raise AssertionError("Legacy manual mapping migration changed values")
        if settings.manual_mapping_profile_json:
            raise AssertionError("Legacy mapping was persisted before Bake succeeded")
        settings.manual_mapping_profile_json = profile_before

        mapped_hand_name = expected_mapping["LeftHand"]
        stable_profile_json = settings.manual_mapping_profile_json
        stable_registry_json = settings.manual_mapping_profiles_json
        head_property = next(
            field.property_name
            for field in HUMANOID_MAPPING_FIELDS
            if field.human_bone == "Head"
        )
        source.data.bones[mapped_hand_name].name = mapped_hand_name + "_Changed"
        try:
            addon._manual_humanoid_mapping(settings)
        except ValueError as error:
            if "自動認識結果を反映" not in str(error):
                raise
        else:
            raise AssertionError("In-place Source mutation did not stale the profile")
        try:
            setattr(settings.manual_mapping, head_property, "")
            if settings.manual_mapping_profile_json != stable_profile_json:
                raise AssertionError(
                    "Manual RNA edit silently rebound a stale Source profile"
                )
            if settings.manual_mapping_profiles_json != stable_registry_json:
                raise AssertionError(
                    "Manual RNA edit overwrote the registry for a stale Source profile"
                )
        finally:
            source.data.bones[mapped_hand_name + "_Changed"].name = mapped_hand_name
            addon._set_manual_humanoid_mapping(settings, expected_mapping)
            settings.manual_mapping_profile_json = stable_profile_json
            settings.manual_mapping_profiles_json = stable_registry_json

        _activate_only(target)
        result = bpy.ops.motion_bridge.bake_source_to_canonical()
        action = target.animation_data.action if target.animation_data else None
        if (
            "FINISHED" not in result
            or action is None
            or action.get("motion_bridge_role") != RETARGET_BAKED_ROLE
        ):
            raise AssertionError(f"Manual mapping Bake failed: {result}")

        stale_source, stale_mapping = _create_armature(
            "MB_Changed_Source",
            scale=1.0,
            source=True,
        )
        settings.source_armature = stale_source
        source_identity_keys = (
            addon._SOURCE_MAPPING_INSTANCE_ID_PROPERTY,
            addon._SOURCE_MAPPING_ACTION_CHECKSUM_PROPERTY,
        )
        if any(key in stale_source for key in source_identity_keys):
            raise AssertionError(
                "Selecting an Actionless Source leaked manual mapping identity"
            )
        actionless_state = (
            settings.manual_mapping_profile_json,
            settings.manual_mapping_profiles_json,
            _profile_values(settings),
        )
        try:
            result = bpy.ops.motion_bridge.populate_manual_mapping()
        except RuntimeError as error:
            if "ボーン名を自動認識できません" not in str(error):
                raise
        else:
            if "CANCELLED" not in result:
                raise AssertionError("Actionless Source Populate was not cancelled")
        if any(key in stale_source for key in source_identity_keys):
            raise AssertionError(
                "Cancelled Actionless Source Populate leaked mapping identity"
            )
        if (
            settings.manual_mapping_profile_json,
            settings.manual_mapping_profiles_json,
            _profile_values(settings),
        ) != actionless_state:
            raise AssertionError(
                "Cancelled Actionless Source Populate changed mapping persistence"
            )

        stale_action = _key_source_motion(stale_source, stale_mapping)
        for key in source_identity_keys:
            stale_source.pop(key, None)
        settings.manual_mapping_profile_json = ""
        settings.manual_mapping_profiles_json = json.dumps(
            {"profiles": {}, "schemaVersion": 99},
            sort_keys=True,
        )
        invalid_registry_state = (
            settings.manual_mapping_profile_json,
            settings.manual_mapping_profiles_json,
            _profile_values(settings),
        )
        try:
            result = bpy.ops.motion_bridge.populate_manual_mapping()
        except RuntimeError as error:
            if "ボーン名を自動認識できません" not in str(error):
                raise
        else:
            if "CANCELLED" not in result:
                raise AssertionError("Invalid-registry Populate was not cancelled")
        if any(key in stale_source for key in source_identity_keys):
            raise AssertionError(
                "Cancelled invalid-registry Populate leaked mapping identity"
            )
        if (
            settings.manual_mapping_profile_json,
            settings.manual_mapping_profiles_json,
            _profile_values(settings),
        ) != invalid_registry_state:
            raise AssertionError(
                "Cancelled invalid-registry Populate changed mapping persistence"
            )
        settings.manual_mapping_profile_json = actionless_state[0]
        settings.manual_mapping_profiles_json = actionless_state[1]
        stale_source.animation_data.action = None
        stale_action.use_fake_user = False
        if stale_action.users == 0:
            bpy.data.actions.remove(stale_action)
        for bone in stale_source.data.bones:
            bone.name = bone.name.replace("SRC::", "CHANGED::")
        stale_target, _ = _create_armature(
            "MB_Stale_Target",
            scale=1.0,
            source=False,
        )
        _activate_only(stale_target)
        settings.source_armature = stale_source
        if any(_profile_values(settings).values()):
            raise AssertionError("Changing Source retained stale manual bone names")
        if settings.manual_mapping_profile_json:
            raise AssertionError("Source without an Action received an unsafe profile")
        actions_before = {item.as_pointer() for item in bpy.data.actions}
        try:
            result = bpy.ops.motion_bridge.bake_source_to_canonical()
        except RuntimeError as error:
            if "ボーンを自動認識できません" not in str(error):
                raise
        else:
            if "CANCELLED" not in result:
                raise AssertionError("Stale manual Source profile was not a hard error")
        if stale_target.animation_data and stale_target.animation_data.action:
            raise AssertionError("Failed stale mapping Bake activated an Action")
        if {item.as_pointer() for item in bpy.data.actions} != actions_before:
            raise AssertionError("Failed stale mapping Bake leaked an Action")

        settings.source_armature = source
        if _profile_values(settings) != expected_mapping:
            raise AssertionError("Returning to a Source did not restore its mapping")
        if not settings.manual_mapping_profile_json:
            raise AssertionError("Returning to a Source lost its profile binding")
        persisted_values = _profile_values(settings)
        with tempfile.TemporaryDirectory(prefix="motion_bridge_manual_") as directory:
            blend_path = str(Path(directory) / "manual_mapping.blend")
            bpy.ops.wm.save_as_mainfile(filepath=blend_path, check_existing=False)
            bpy.ops.wm.open_mainfile(filepath=blend_path, load_ui=False)
            reloaded_settings = bpy.context.scene.motion_bridge_session
            if _profile_values(reloaded_settings) != persisted_values:
                raise AssertionError("Manual mapping did not survive .blend reload")
            if not reloaded_settings.manual_mapping_profile_json:
                raise AssertionError(
                    "Source profile binding did not survive .blend reload"
                )
            reloaded_target = bpy.data.objects.get("MB_Manual_Target")
            if reloaded_target is None:
                raise AssertionError("Reloaded manual target was not found")
            _activate_only(reloaded_target)
            if "FINISHED" not in bpy.ops.motion_bridge.create_correction_layer():
                raise AssertionError(
                    "Could not create Correction stack for mapping lock"
                )
            if not addon._manual_mapping_changes_locked(bpy.context):
                raise AssertionError("Correction stack did not lock mapping changes")
            locked_source = reloaded_settings.source_armature
            locked_profile = reloaded_settings.manual_mapping_profile_json
            locked_values = _profile_values(reloaded_settings)
            replacement = bpy.data.objects.get("MB_Changed_Source")
            reloaded_settings.source_armature = replacement
            if reloaded_settings.source_armature is not locked_source:
                raise AssertionError("Direct Source RNA change bypassed mapping lock")
            setattr(reloaded_settings.manual_mapping, left_hand_property, "")
            if _profile_values(reloaded_settings) != locked_values:
                raise AssertionError("Direct mapping RNA change bypassed stack lock")
            if reloaded_settings.manual_mapping_profile_json != locked_profile:
                raise AssertionError("Stack lock rewrote the saved mapping profile")
            for operator, label in (
                (bpy.ops.motion_bridge.import_source_take, "Source import"),
                (bpy.ops.motion_bridge.populate_manual_mapping, "Populate"),
                (bpy.ops.motion_bridge.clear_manual_mapping, "Clear"),
            ):
                if operator.poll():
                    raise AssertionError(f"{label} remained enabled after Correction")

            original_scene = bpy.context.scene
            locked_registry = reloaded_settings.manual_mapping_profiles_json
            if bpy.ops.scene.new(type="FULL_COPY") != {"FINISHED"}:
                raise AssertionError("Could not create a Full Copy Scene")
            copied_settings = bpy.context.scene.motion_bridge_session
            copied_source = copied_settings.source_armature
            if copied_source is None or copied_source is locked_source:
                raise AssertionError("Full Copy did not remap the Source armature")
            if (
                copied_settings.manual_mapping_profile_json != locked_profile
                or copied_settings.manual_mapping_profiles_json != locked_registry
                or _profile_values(copied_settings) != locked_values
            ):
                raise AssertionError("Full Copy changed the Source mapping profile")
            copied_profile = SourceMappingProfile.from_json(
                copied_settings.manual_mapping_profile_json
            )
            copied_profile.require_source(
                **addon._source_mapping_identity(
                    copied_source,
                    create=False,
                    verify_content=True,
                )
            )

            bpy.context.window.scene = original_scene
            original_profile = SourceMappingProfile.from_json(
                reloaded_settings.manual_mapping_profile_json
            )
            original_profile.require_source(
                **addon._source_mapping_identity(
                    locked_source,
                    create=False,
                    verify_content=True,
                )
            )
    finally:
        addon.unregister()

    print(
        json.dumps(
            {
                "status": "PASS",
                "profileFields": len(HUMANOID_MAPPING_FIELDS),
                "autoPopulate": True,
                "clear": True,
                "manualBake": True,
                "scenePersistence": True,
                "sourceBoundProfile": True,
                "sourceMutationHardError": True,
                "sourceReplacementClearsMapping": True,
                "sourceProfileRestored": True,
                "actionlessPopulateTransactional": True,
                "invalidRegistryPopulateTransactional": True,
                "manualEditsPersisted": True,
                "sceneProfilesIsolated": True,
                "sceneFullCopyProfileConsistent": True,
                "unknownProfileSchemaPreserved": True,
                "legacyMappingMigrated": True,
                "legacyMappingMigratedOnSourceSwitch": True,
                "staleManualEditDoesNotRebind": True,
                "directRnaChangesBlockedAfterCorrection": True,
                "mappingLockedAfterCorrection": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
