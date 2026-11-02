"""Archived Source Take bake and transactional Canonical Base regeneration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping
import uuid

import bpy

from ..editing.action_api import (
    action_slot_for_id,
    assign_nla_strip_action,
    iter_action_fcurves,
    strip_slot_matches,
)
from ..editing.correction_layers import (
    LAYER_SCHEMA_VERSION,
    PROP_ARCHIVE_ACTION,
    PROP_ARCHIVE_CHECKSUM,
    PROP_BASE_ACTION,
    PROP_BASE_CHECKSUM,
    PROP_CHECKSUM_ALGORITHM,
    PROP_PROVENANCE,
    PROP_ROLE,
    PROP_SCHEMA,
    PROP_STACK_ID,
    ROLE_ARCHIVE,
    ROLE_BASE,
    _protect_action,
    action_checksum,
    validate_layer_stack,
)
from ..io.source_take import (
    SOURCE_ACTION_SLOT_PROPERTY,
    SOURCE_ARCHIVE_CHECKSUM_ALGORITHM,
    SOURCE_ARCHIVE_CHECKSUM_ALGORITHM_PROPERTY,
    SOURCE_ARCHIVE_CHECKSUM_PROPERTY,
    SOURCE_ARCHIVE_KIND,
    SOURCE_BIND_POSE_PROPERTY,
    SOURCE_INSTANCE_ID_PROPERTY,
    SOURCE_PROVENANCE_SCHEMA_PROPERTY,
    SOURCE_PROVENANCE_SCHEMA_VERSION,
    bone_bind_pose_fingerprint,
    bone_hierarchy_fingerprint,
)
from .bake import bake_source_to_canonical
from .mapping import validate_humanoid_mapping
from .recipe import PROP_RETARGET_RECIPE, RetargetRecipe


_SOURCE_PROVENANCE_PROPERTIES = (
    SOURCE_PROVENANCE_SCHEMA_PROPERTY,
    "motion_bridge_source_format",
    "motion_bridge_source_filepath",
    "motion_bridge_source_file_sha256",
    "motion_bridge_source_armature",
    "motion_bridge_source_action",
    "motion_bridge_source_frame_start",
    "motion_bridge_source_frame_end",
    "motion_bridge_source_bone_hierarchy_sha256",
    "motion_bridge_source_archive_id",
    SOURCE_INSTANCE_ID_PROPERTY,
    SOURCE_ACTION_SLOT_PROPERTY,
    SOURCE_BIND_POSE_PROPERTY,
)


def _require_local_editable(datablock: object, label: str) -> None:
    if (
        getattr(datablock, "library", None) is not None
        or getattr(datablock, "override_library", None) is not None
        or not bool(getattr(datablock, "is_editable", True))
    ):
        raise ValueError(f"{label} must be a local editable datablock")


@dataclass(frozen=True, slots=True)
class BaseRegenerationResult:
    base_action: bpy.types.Action
    archive_action: bpy.types.Action
    source_armature: bpy.types.Object
    source_archive_action: bpy.types.Action
    checksum: str


@dataclass(frozen=True, slots=True)
class _SourceTakeAssets:
    armature: bpy.types.Object
    archive_action: bpy.types.Action
    instance_id: str
    slot_identifier: str
    bind_pose_sha256: str


def _armature_bind_pose_fingerprint(armature: bpy.types.Object) -> str:
    return bone_bind_pose_fingerprint(
        (
            bone.name,
            None if bone.parent is None else bone.parent.name,
            tuple(float(value) for value in bone.head_local),
            tuple(float(value) for value in bone.tail_local),
            tuple(
                float(bone.matrix_local[row][column])
                for row in range(4)
                for column in range(4)
            ),
        )
        for bone in armature.data.bones
    )


def _armature_hierarchy_fingerprint(armature: bpy.types.Object) -> str:
    return bone_hierarchy_fingerprint(
        (
            bone.name,
            None if bone.parent is None else bone.parent.name,
        )
        for bone in armature.data.bones
    )


def _slot_by_identifier(
    action: bpy.types.Action,
    identifier: str,
    *,
    label: str,
) -> object | None:
    slots = getattr(action, "slots", None)
    if slots is None:
        if identifier:
            raise ValueError(f"{label} uses a saved slot unsupported by this Blender build")
        return None
    if len(slots) == 0:
        if identifier:
            raise ValueError(f"{label} no longer contains its saved Action Slot")
        return None
    if not identifier:
        raise ValueError(f"{label} is missing its saved Action Slot identifier")
    matches = [slot for slot in slots if getattr(slot, "identifier", "") == identifier]
    if len(matches) != 1:
        raise ValueError(f"{label} no longer contains exactly one saved Action Slot")
    return matches[0]


def _source_archive_is_protected(action: bpy.types.Action) -> bool:
    for curve in iter_action_fcurves(action):
        if not curve.lock or (hasattr(curve, "hide") and not curve.hide):
            return False
        group = curve.group
        if group is not None and hasattr(group, "lock") and not group.lock:
            return False
    return True


def _validate_matching_provenance(
    armature: bpy.types.Object,
    archive: bpy.types.Action,
) -> None:
    for key in _SOURCE_PROVENANCE_PROPERTIES:
        if armature.get(key) != archive.get(key):
            raise ValueError(f"Source Take provenance mismatch: {key}")


def _source_assets_from_armature(
    context: bpy.types.Context,
    armature: bpy.types.Object,
) -> _SourceTakeAssets:
    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Archived Source Take must be an armature Object")
    if context.view_layer.objects.get(armature.name) is not armature:
        raise ValueError("Source Take armature must belong to the current View Layer")
    _require_local_editable(armature, "Source Take armature")
    _require_local_editable(armature.data, "Source Take armature data")
    if armature.get(SOURCE_PROVENANCE_SCHEMA_PROPERTY) != (
        SOURCE_PROVENANCE_SCHEMA_VERSION
    ):
        raise ValueError("Source Take provenance is legacy or unsupported; re-import it")
    instance_id = armature.get(SOURCE_INSTANCE_ID_PROPERTY)
    if not isinstance(instance_id, str) or not instance_id:
        raise ValueError("Source Take was imported without an instance id; re-import it")
    archive_matches = [
        action
        for action in bpy.data.actions
        if action.get(SOURCE_INSTANCE_ID_PROPERTY) == instance_id
        and action.get("motion_bridge_archive_kind") == SOURCE_ARCHIVE_KIND
    ]
    if len(archive_matches) != 1:
        raise ValueError(
            "Source Take must resolve to exactly one embedded SOURCE_ARCHIVE Action"
        )
    archive = archive_matches[0]
    _require_local_editable(archive, "Source Archive")
    _validate_matching_provenance(armature, archive)
    if archive.get("motion_bridge_archive_immutable") is not True:
        raise ValueError("Source Archive immutability marker is missing")
    if not _source_archive_is_protected(archive):
        raise ValueError("Source Archive curves are not fully protected")
    hierarchy_sha256 = _armature_hierarchy_fingerprint(armature)
    if hierarchy_sha256 != armature.get(
        "motion_bridge_source_bone_hierarchy_sha256"
    ):
        raise ValueError("Source armature hierarchy changed after import")
    bind_pose_sha256 = _armature_bind_pose_fingerprint(armature)
    if bind_pose_sha256 != armature.get(SOURCE_BIND_POSE_PROPERTY):
        raise ValueError("Source armature bind pose changed after import")
    slot_identifier = armature.get(SOURCE_ACTION_SLOT_PROPERTY)
    if not isinstance(slot_identifier, str):
        raise ValueError("Source Take Action Slot provenance is missing")
    _slot_by_identifier(archive, slot_identifier, label="Source Archive")
    return _SourceTakeAssets(
        armature,
        archive,
        instance_id,
        slot_identifier,
        bind_pose_sha256,
    )


def _source_assets_from_recipe(
    context: bpy.types.Context,
    recipe: RetargetRecipe,
) -> _SourceTakeAssets:
    objects = [
        obj
        for obj in context.view_layer.objects
        if obj.type == "ARMATURE"
        and obj.get(SOURCE_INSTANCE_ID_PROPERTY) == recipe.source_instance_id
    ]
    if len(objects) != 1:
        raise ValueError(
            "Retarget recipe must resolve to exactly one Source Take armature"
        )
    assets = _source_assets_from_armature(context, objects[0])
    archive = assets.archive_action
    expected = {
        "motion_bridge_source_format": recipe.source_file_format,
        "motion_bridge_source_filepath": recipe.source_filepath,
        "motion_bridge_source_archive_id": recipe.source_archive_id,
        "motion_bridge_source_file_sha256": recipe.source_file_sha256,
        "motion_bridge_source_armature": recipe.source_armature_name,
        "motion_bridge_source_action": recipe.source_action_name,
        "motion_bridge_source_bone_hierarchy_sha256": (
            recipe.source_hierarchy_sha256
        ),
        SOURCE_BIND_POSE_PROPERTY: recipe.source_bind_pose_sha256,
        SOURCE_ACTION_SLOT_PROPERTY: recipe.source_slot_identifier,
    }
    for key, value in expected.items():
        if assets.armature.get(key) != value or archive.get(key) != value:
            raise ValueError(f"Saved Source Take no longer matches recipe: {key}")
    stored_checksum = archive.get(SOURCE_ARCHIVE_CHECKSUM_PROPERTY)
    stored_algorithm = archive.get(SOURCE_ARCHIVE_CHECKSUM_ALGORITHM_PROPERTY)
    if stored_algorithm != SOURCE_ARCHIVE_CHECKSUM_ALGORITHM:
        raise ValueError("Source Archive checksum algorithm is missing or unsupported")
    if stored_checksum != recipe.source_archive_checksum:
        raise ValueError("Source Archive checksum metadata no longer matches recipe")
    actual_checksum = action_checksum(archive)
    if actual_checksum != recipe.source_archive_checksum:
        raise ValueError("Source Archive content no longer matches its checksum")
    return assets


class _SourceArchiveBinding:
    """Temporarily evaluate one saved Source Archive and restore exact binding."""

    def __init__(
        self,
        context: bpy.types.Context,
        assets: _SourceTakeAssets,
    ) -> None:
        self.context = context
        self.assets = assets
        self.animation_data = assets.armature.animation_data
        if self.animation_data is None:
            raise ValueError("Source Take armature has no animation data")
        if getattr(self.animation_data, "use_tweak_mode", False):
            raise ValueError("Exit source NLA Tweak Mode before archived retarget bake")
        self.action = self.animation_data.action
        self.slot = getattr(self.animation_data, "action_slot", None)
        self.last_slot_identifier = getattr(
            self.animation_data,
            "last_slot_identifier",
            None,
        )
        self.tweak_slot_handle = getattr(
            self.animation_data,
            "action_slot_handle_tweak_storage",
            None,
        )
        self.use_nla = getattr(self.animation_data, "use_nla", None)
        self.action_blend_type = getattr(
            self.animation_data,
            "action_blend_type",
            None,
        )
        self.action_extrapolation = getattr(
            self.animation_data,
            "action_extrapolation",
            None,
        )
        self.action_influence = getattr(
            self.animation_data,
            "action_influence",
            None,
        )
        self.tracks = tuple(
            (track, bool(track.mute), bool(track.is_solo))
            for track in self.animation_data.nla_tracks
        )
        self.frame = context.scene.frame_current
        self.subframe = context.scene.frame_subframe

    def __enter__(self) -> _SourceArchiveBinding:
        slot = _slot_by_identifier(
            self.assets.archive_action,
            self.assets.slot_identifier,
            label="Source Archive",
        )
        try:
            for track, _mute, _solo in self.tracks:
                track.is_solo = False
                track.mute = True
            if self.use_nla is not None:
                self.animation_data.use_nla = False
            self.animation_data.action = self.assets.archive_action
            if slot is not None:
                if hasattr(self.animation_data, "action_slot"):
                    self.animation_data.action_slot = slot
                else:
                    self.animation_data.action_slot_handle = slot.handle
            if self.action_blend_type is not None:
                self.animation_data.action_blend_type = "REPLACE"
            if self.action_extrapolation is not None:
                self.animation_data.action_extrapolation = "HOLD"
            if self.action_influence is not None:
                self.animation_data.action_influence = 1.0
            self.context.view_layer.update()
            return self
        except Exception:
            self._restore()
            raise

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self._restore()

    def _restore(self) -> None:
        self.animation_data.action = self.action
        if self.action is not None and self.slot is not None:
            if hasattr(self.animation_data, "action_slot"):
                self.animation_data.action_slot = self.slot
            else:
                self.animation_data.action_slot_handle = self.slot.handle
        if self.last_slot_identifier is not None:
            self.animation_data.last_slot_identifier = self.last_slot_identifier
        if self.tweak_slot_handle is not None:
            self.animation_data.action_slot_handle_tweak_storage = self.tweak_slot_handle
        if self.use_nla is not None:
            self.animation_data.use_nla = self.use_nla
        if self.action_blend_type is not None:
            self.animation_data.action_blend_type = self.action_blend_type
        if self.action_extrapolation is not None:
            self.animation_data.action_extrapolation = self.action_extrapolation
        if self.action_influence is not None:
            self.animation_data.action_influence = self.action_influence
        for track, mute, solo in self.tracks:
            track.mute = mute
            track.is_solo = solo
        self.context.scene.frame_set(self.frame, subframe=self.subframe)
        self.context.view_layer.update()


def _discard_action(action: bpy.types.Action | None) -> None:
    if action is None:
        return
    action.use_fake_user = False
    if action.users == 0:
        bpy.data.actions.remove(action)


def _action_slot_identity(
    action: bpy.types.Action,
    armature: bpy.types.Object,
) -> tuple[str, str]:
    slot = action_slot_for_id(action, armature)
    slots = getattr(action, "slots", None)
    if slots is not None and len(slots) and slot is None:
        raise ValueError("Canonical bake has no unambiguous armature Action Slot")
    return (
        str(getattr(slot, "identifier", "")),
        str(getattr(slot, "name_display", "")),
    )


def _restore_canonical_slot_identity(
    action: bpy.types.Action,
    armature: bpy.types.Object,
    recipe: RetargetRecipe,
) -> None:
    slot = action_slot_for_id(action, armature)
    slots = getattr(action, "slots", None)
    if slots is None:
        if recipe.canonical_slot_identifier:
            raise ValueError("Saved Canonical Action Slot is unsupported")
        return
    if len(slots) != 1 or slot is None:
        raise ValueError("Regenerated Base must contain exactly one Canonical Action Slot")
    if recipe.canonical_slot_display_name and hasattr(slot, "name_display"):
        slot.name_display = recipe.canonical_slot_display_name
    if getattr(slot, "identifier", "") != recipe.canonical_slot_identifier:
        slot.identifier = recipe.canonical_slot_identifier
    if getattr(slot, "identifier", "") != recipe.canonical_slot_identifier:
        raise ValueError(
            "Canonical Action Slot identity could not be restored"
        )


def _recipe_source_value(assets: _SourceTakeAssets, key: str) -> str:
    value = assets.armature.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Source Take provenance is missing: {key}")
    return value


def bake_archived_source_to_canonical(
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
    """Bake the immutable imported Source Archive and attach a saved recipe."""

    assets = _source_assets_from_armature(context, source_armature)
    # Validate before the potentially expensive checksum and bake.
    plan = validate_humanoid_mapping(source_armature, canonical_armature, mapping)
    source_checksum = action_checksum(assets.archive_action)
    existing_checksum = assets.archive_action.get(SOURCE_ARCHIVE_CHECKSUM_PROPERTY)
    existing_algorithm = assets.archive_action.get(
        SOURCE_ARCHIVE_CHECKSUM_ALGORITHM_PROPERTY
    )
    if existing_checksum is not None and existing_checksum != source_checksum:
        raise ValueError("Source Archive content changed after its checksum was saved")
    if existing_algorithm is not None and (
        existing_algorithm != SOURCE_ARCHIVE_CHECKSUM_ALGORITHM
    ):
        raise ValueError("Source Archive checksum algorithm is unsupported")

    result: bpy.types.Action | None = None
    checksum_existed = SOURCE_ARCHIVE_CHECKSUM_PROPERTY in assets.archive_action
    checksum_before = assets.archive_action.get(SOURCE_ARCHIVE_CHECKSUM_PROPERTY)
    algorithm_existed = (
        SOURCE_ARCHIVE_CHECKSUM_ALGORITHM_PROPERTY in assets.archive_action
    )
    algorithm_before = assets.archive_action.get(
        SOURCE_ARCHIVE_CHECKSUM_ALGORITHM_PROPERTY
    )
    try:
        with _SourceArchiveBinding(context, assets):
            result = bake_source_to_canonical(
                context,
                source_armature,
                canonical_armature,
                plan.source_bones_by_human_bone,
                frame_start=frame_start,
                frame_end=frame_end,
                name=name,
                _on_frame=_on_frame,
            )
        slot_identifier, slot_display_name = _action_slot_identity(
            result,
            canonical_armature,
        )
        recipe = RetargetRecipe.create(
            source_instance_id=assets.instance_id,
            source_object_hint=source_armature.name,
            source_archive_action_hint=assets.archive_action.name,
            source_archive_id=_recipe_source_value(
                assets,
                "motion_bridge_source_archive_id",
            ),
            source_archive_checksum=source_checksum,
            source_slot_identifier=assets.slot_identifier,
            source_file_format=_recipe_source_value(
                assets,
                "motion_bridge_source_format",
            ),
            source_filepath=_recipe_source_value(
                assets,
                "motion_bridge_source_filepath",
            ),
            source_file_sha256=_recipe_source_value(
                assets,
                "motion_bridge_source_file_sha256",
            ),
            source_armature_name=_recipe_source_value(
                assets,
                "motion_bridge_source_armature",
            ),
            source_action_name=_recipe_source_value(
                assets,
                "motion_bridge_source_action",
            ),
            source_hierarchy_sha256=_recipe_source_value(
                assets,
                "motion_bridge_source_bone_hierarchy_sha256",
            ),
            source_bind_pose_sha256=assets.bind_pose_sha256,
            canonical_bind_pose_sha256=_armature_bind_pose_fingerprint(
                canonical_armature
            ),
            canonical_slot_identifier=slot_identifier,
            canonical_slot_display_name=slot_display_name,
            frame_start=frame_start,
            frame_end=frame_end,
            mapping=plan.source_bones_by_human_bone,
        )
        result[PROP_RETARGET_RECIPE] = recipe.to_json()
        assets.archive_action[SOURCE_ARCHIVE_CHECKSUM_PROPERTY] = source_checksum
        assets.archive_action[SOURCE_ARCHIVE_CHECKSUM_ALGORITHM_PROPERTY] = (
            SOURCE_ARCHIVE_CHECKSUM_ALGORITHM
        )
        return result
    except Exception:
        if checksum_existed:
            assets.archive_action[SOURCE_ARCHIVE_CHECKSUM_PROPERTY] = checksum_before
        elif SOURCE_ARCHIVE_CHECKSUM_PROPERTY in assets.archive_action:
            del assets.archive_action[SOURCE_ARCHIVE_CHECKSUM_PROPERTY]
        if algorithm_existed:
            assets.archive_action[SOURCE_ARCHIVE_CHECKSUM_ALGORITHM_PROPERTY] = (
                algorithm_before
            )
        elif SOURCE_ARCHIVE_CHECKSUM_ALGORITHM_PROPERTY in assets.archive_action:
            del assets.archive_action[SOURCE_ARCHIVE_CHECKSUM_ALGORITHM_PROPERTY]
        _discard_action(result)
        raise


def _bake_from_recipe(
    context: bpy.types.Context,
    source_assets: _SourceTakeAssets,
    canonical_armature: bpy.types.Object,
    recipe: RetargetRecipe,
    *,
    name: str,
    _on_frame: Callable[[int], None] | None,
) -> bpy.types.Action:
    result: bpy.types.Action | None = None
    try:
        with _SourceArchiveBinding(context, source_assets):
            result = bake_source_to_canonical(
                context,
                source_assets.armature,
                canonical_armature,
                recipe.mapping,
                frame_start=recipe.frame_start,
                frame_end=recipe.frame_end,
                name=name,
                _on_frame=_on_frame,
            )
        _restore_canonical_slot_identity(result, canonical_armature, recipe)
        result[PROP_RETARGET_RECIPE] = recipe.to_json()
        return result
    except Exception:
        _discard_action(result)
        raise


@dataclass(frozen=True, slots=True)
class _PropertyState:
    owner: object
    key: str
    existed: bool
    value: object

    @classmethod
    def capture(cls, owner: object, key: str) -> _PropertyState:
        return cls(owner, key, key in owner, owner.get(key))

    def restore(self) -> None:
        if self.existed:
            self.owner[self.key] = self.value
        elif self.key in self.owner:
            del self.owner[self.key]


@dataclass(frozen=True, slots=True)
class _BaseStripState:
    action: bpy.types.Action
    slot: object | None
    frame_start: float
    frame_end: float
    action_frame_start: float
    action_frame_end: float
    scale: float
    repeat: float
    use_sync_length: bool | None

    @classmethod
    def capture(
        cls,
        strip: bpy.types.NlaStrip,
        armature: bpy.types.Object,
    ) -> _BaseStripState:
        assert strip.action is not None
        return cls(
            strip.action,
            action_slot_for_id(strip.action, armature),
            float(strip.frame_start),
            float(strip.frame_end),
            float(strip.action_frame_start),
            float(strip.action_frame_end),
            float(strip.scale),
            float(strip.repeat),
            (
                bool(strip.use_sync_length)
                if hasattr(strip, "use_sync_length")
                else None
            ),
        )

    def bind(
        self,
        strip: bpy.types.NlaStrip,
        action: bpy.types.Action,
        slot: object | None,
    ) -> None:
        if hasattr(strip, "use_sync_length"):
            strip.use_sync_length = False
        assign_nla_strip_action(strip, action, slot)
        strip.action_frame_start = self.action_frame_start
        strip.action_frame_end = self.action_frame_end
        strip.scale = self.scale
        strip.repeat = self.repeat
        strip.frame_start = self.frame_start
        strip.frame_end = self.frame_end
        if self.use_sync_length is not None:
            strip.use_sync_length = self.use_sync_length


def _non_fake_users(action: bpy.types.Action) -> int:
    return int(action.users) - int(bool(action.use_fake_user))


def _require_unshared_stack_actions(stack: object) -> None:
    if _non_fake_users(stack.base_action) != 1:
        raise ValueError("Base regeneration requires an unshared Base Action")
    if _non_fake_users(stack.archive_action) != 0:
        raise ValueError("Base regeneration requires an unshared Canonical Archive")
    for layer in stack.correction_layers:
        if _non_fake_users(layer.action) != 1:
            raise ValueError(
                f"Base regeneration requires an unshared Correction Action: {layer.name}"
            )


def _matching_range(action: bpy.types.Action, recipe: RetargetRecipe) -> bool:
    actual = tuple(float(value) for value in action.frame_range)
    expected = (float(recipe.frame_start), float(recipe.frame_end))
    return all(abs(value - saved) <= 1e-6 for value, saved in zip(actual, expected))


def _validate_recipe_stack_metadata(
    canonical_armature: bpy.types.Object,
    stack: object,
    recipe_json: str,
    expected_checksum: str,
) -> None:
    if stack.base_action.get(PROP_RETARGET_RECIPE) != recipe_json:
        raise ValueError("Base and Canonical Archive retarget recipes differ")
    armature_recipe = canonical_armature.get(PROP_RETARGET_RECIPE)
    if armature_recipe is not None and armature_recipe != recipe_json:
        raise ValueError("Armature and Canonical Archive retarget recipes differ")
    if stack.base_action.get(PROP_BASE_CHECKSUM) != expected_checksum:
        raise ValueError("Base checksum metadata no longer matches its Archive")
    for layer in stack.correction_layers:
        if layer.action.get(PROP_BASE_ACTION) != stack.base_action.name:
            raise ValueError(f"Correction Base reference is stale: {layer.name}")
        if layer.action.get(PROP_BASE_CHECKSUM) != expected_checksum:
            raise ValueError(f"Correction Base checksum is stale: {layer.name}")
    if not _matching_range(stack.base_action, RetargetRecipe.from_json(recipe_json)):
        raise ValueError("Saved retarget frame range differs from the Base Action")


def _pending_name(prefix: str, action: bpy.types.Action) -> str:
    return f"{prefix}::{uuid.uuid4()}::{action.name}"


def _set_base_references(
    canonical_armature: bpy.types.Object,
    stack: object,
    base_action: bpy.types.Action,
    archive_action: bpy.types.Action,
    checksum: str,
    recipe_json: str,
) -> None:
    base_action[PROP_ARCHIVE_ACTION] = archive_action.name
    base_action[PROP_BASE_CHECKSUM] = checksum
    canonical_armature[PROP_ARCHIVE_ACTION] = archive_action.name
    canonical_armature[PROP_BASE_ACTION] = base_action.name
    canonical_armature[PROP_RETARGET_RECIPE] = recipe_json
    for layer in stack.correction_layers:
        layer.action[PROP_BASE_ACTION] = base_action.name
        layer.action[PROP_BASE_CHECKSUM] = checksum


def _build_regeneration_candidate(
    context: bpy.types.Context,
    source_assets: _SourceTakeAssets,
    canonical_armature: bpy.types.Object,
    stack: object,
    recipe: RetargetRecipe,
    recipe_json: str,
    expected_checksum: str,
    *,
    _on_frame: Callable[[int], None] | None,
    _on_stage: Callable[[str], None] | None,
) -> tuple[bpy.types.Action, bpy.types.Action]:
    candidate: bpy.types.Action | None = None
    candidate_archive: bpy.types.Action | None = None
    try:
        candidate = _bake_from_recipe(
            context,
            source_assets,
            canonical_armature,
            recipe,
            name=_pending_name("MB_BASE_REGENERATE_PENDING", stack.base_action),
            _on_frame=_on_frame,
        )
        if _on_stage is not None:
            _on_stage("candidate_baked")
        if action_checksum(candidate) != expected_checksum:
            raise ValueError(
                "Regenerated Base differs from the Correction reference; "
                "changing mapping or retarget results requires an explicit rebase"
            )
        _protect_action(candidate)

        candidate_archive = candidate.copy()
        candidate_archive.name = _pending_name(
            "MB_ARCHIVE_REGENERATE_PENDING",
            stack.archive_action,
        )
        candidate_archive.use_fake_user = True
        _protect_action(candidate_archive)
        stack_id = str(canonical_armature[PROP_STACK_ID])
        candidate[PROP_SCHEMA] = LAYER_SCHEMA_VERSION
        candidate[PROP_STACK_ID] = stack_id
        candidate[PROP_ROLE] = ROLE_BASE
        candidate[PROP_BASE_CHECKSUM] = expected_checksum
        candidate_archive[PROP_SCHEMA] = LAYER_SCHEMA_VERSION
        candidate_archive[PROP_STACK_ID] = stack_id
        candidate_archive[PROP_ROLE] = ROLE_ARCHIVE
        candidate_archive[PROP_ARCHIVE_CHECKSUM] = expected_checksum
        candidate_archive[PROP_CHECKSUM_ALGORITHM] = "sha256"
        candidate_archive[PROP_PROVENANCE] = stack.archive_action[PROP_PROVENANCE]
        candidate_archive[PROP_RETARGET_RECIPE] = recipe_json
        if _on_stage is not None:
            _on_stage("candidate_prepared")
        return candidate, candidate_archive
    except Exception:
        _discard_action(candidate_archive)
        _discard_action(candidate)
        raise


def regenerate_canonical_base(
    context: bpy.types.Context,
    canonical_armature: bpy.types.Object,
    *,
    _on_frame: Callable[[int], None] | None = None,
    _on_stage: Callable[[str], None] | None = None,
) -> BaseRegenerationResult:
    """Rebuild Base from its saved Source Archive without rebasing Corrections.

    Only a byte-equivalent Canonical animation checksum may replace the Base.
    Mapping, source instance, bind poses, Action Slots, and frame range all come
    from the immutable recipe; current panel values are deliberately ignored.
    """

    if canonical_armature is None or canonical_armature.type != "ARMATURE":
        raise ValueError("Base regeneration requires a Canonical armature Object")
    if context.view_layer.objects.get(canonical_armature.name) is not canonical_armature:
        raise ValueError("Canonical armature must belong to the current View Layer")
    _require_local_editable(canonical_armature, "Canonical armature")
    _require_local_editable(canonical_armature.data, "Canonical armature data")
    animation_data = canonical_armature.animation_data
    if animation_data is None:
        raise ValueError("Canonical armature has no animation data")
    if getattr(animation_data, "use_tweak_mode", False):
        raise ValueError("Exit target NLA Tweak Mode before Base regeneration")
    if animation_data.action is not None:
        raise ValueError("Base regeneration requires no active target Action binding")

    validation = validate_layer_stack(canonical_armature, check_content=False)
    validation.raise_for_errors()
    assert validation.stack is not None
    stack = validation.stack
    _require_local_editable(stack.base_action, "Base Action")
    _require_local_editable(stack.archive_action, "Canonical Archive")
    for layer in stack.correction_layers:
        _require_local_editable(layer.action, f"Correction Action {layer.name}")
    _require_unshared_stack_actions(stack)
    expected_checksum = stack.archive_action.get(PROP_ARCHIVE_CHECKSUM)
    if (
        stack.archive_action.get(PROP_CHECKSUM_ALGORITHM) != "sha256"
        or not isinstance(expected_checksum, str)
        or len(expected_checksum) != 64
        or any(
            character not in "0123456789abcdef"
            for character in expected_checksum
        )
    ):
        raise ValueError("Canonical Archive checksum metadata is missing or malformed")
    recipe_json = stack.archive_action.get(PROP_RETARGET_RECIPE)
    recipe = RetargetRecipe.from_json(recipe_json)
    assert isinstance(recipe_json, str)
    _validate_recipe_stack_metadata(
        canonical_armature,
        stack,
        recipe_json,
        expected_checksum,
    )
    if canonical_armature.get("motion_bridge_skeleton_id") != recipe.canonical_skeleton_id:
        raise ValueError("Canonical skeleton no longer matches the saved recipe")
    if _armature_bind_pose_fingerprint(
        canonical_armature
    ) != recipe.canonical_bind_pose_sha256:
        raise ValueError("Canonical armature bind pose changed after the initial bake")
    source_assets = _source_assets_from_recipe(context, recipe)
    validate_humanoid_mapping(
        source_assets.armature,
        canonical_armature,
        recipe.mapping,
    )

    candidate, candidate_archive = _build_regeneration_candidate(
        context,
        source_assets,
        canonical_armature,
        stack,
        recipe,
        recipe_json,
        expected_checksum,
        _on_frame=_on_frame,
        _on_stage=_on_stage,
    )
    try:
        touched = [
            _PropertyState.capture(stack.archive_action, PROP_ROLE),
            _PropertyState.capture(canonical_armature, PROP_ARCHIVE_ACTION),
            _PropertyState.capture(canonical_armature, PROP_BASE_ACTION),
            _PropertyState.capture(canonical_armature, PROP_RETARGET_RECIPE),
            *(
                state
                for layer in stack.correction_layers
                for state in (
                    _PropertyState.capture(layer.action, PROP_BASE_ACTION),
                    _PropertyState.capture(layer.action, PROP_BASE_CHECKSUM),
                )
            ),
        ]
        strip_state = _BaseStripState.capture(stack.base_strip, canonical_armature)
    except Exception:
        _discard_action(candidate_archive)
        _discard_action(candidate)
        raise
    old_base_name = stack.base_action.name
    old_archive_name = stack.archive_action.name
    old_base_retired_name = _pending_name("MB_RETIRED_BASE", stack.base_action)
    old_archive_retired_name = _pending_name(
        "MB_RETIRED_ARCHIVE",
        stack.archive_action,
    )
    swapped = False
    renamed = False
    committed = False
    try:
        candidate_slot = action_slot_for_id(candidate, canonical_armature)
        if getattr(candidate, "slots", None) and candidate_slot is None:
            raise ValueError("Regenerated Base has no Canonical Action Slot")
        strip_state.bind(stack.base_strip, candidate, candidate_slot)
        if not strip_slot_matches(stack.base_strip, candidate_slot):
            raise RuntimeError("Regenerated Base Action Slot was not assigned explicitly")
        swapped = True
        stack.archive_action[PROP_ROLE] = "RETIRED"
        _set_base_references(
            canonical_armature,
            stack,
            candidate,
            candidate_archive,
            expected_checksum,
            recipe_json,
        )
        if _on_stage is not None:
            _on_stage("base_swapped")
        validate_layer_stack(
            canonical_armature,
            check_content=False,
        ).raise_for_errors()

        renamed = True
        stack.base_action.name = old_base_retired_name
        stack.archive_action.name = old_archive_retired_name
        candidate.name = old_base_name
        candidate_archive.name = old_archive_name
        _set_base_references(
            canonical_armature,
            stack,
            candidate,
            candidate_archive,
            expected_checksum,
            recipe_json,
        )
        validate_layer_stack(
            canonical_armature,
            check_content=False,
        ).raise_for_errors()
        if _on_stage is not None:
            _on_stage("metadata_committed")
        committed = True
    finally:
        if not committed:
            if swapped or stack.base_strip.action is not strip_state.action:
                strip_state.bind(
                    stack.base_strip,
                    strip_state.action,
                    strip_state.slot,
                )
            for state in reversed(touched):
                state.restore()
            _discard_action(candidate_archive)
            _discard_action(candidate)
            if renamed:
                stack.base_action.name = old_base_name
                stack.archive_action.name = old_archive_name

    # Both Actions were required to be private to this stack, so the swap left
    # no non-fake consumers.  Retire them only after all rollback seams passed.
    _discard_action(stack.base_action)
    _discard_action(stack.archive_action)
    candidate.use_fake_user = False
    resolved = validate_layer_stack(
        canonical_armature,
        check_content=False,
    )
    resolved.raise_for_errors()
    assert resolved.stack is not None
    return BaseRegenerationResult(
        resolved.stack.base_action,
        resolved.stack.archive_action,
        source_assets.armature,
        source_assets.archive_action,
        expected_checksum,
    )
