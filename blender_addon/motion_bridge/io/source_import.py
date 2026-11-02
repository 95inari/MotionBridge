"""Transactional FBX/glTF Source Take import for Blender 5.x."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Callable, Iterable
import uuid

import bpy

from ..editing.action_api import action_slot_for_id, iter_action_fcurves
from ..editing.correction_layers import action_content_checksum
from .source_take import (
    SOURCE_ACTION_SLOT_PROPERTY,
    SOURCE_ARCHIVE_ACTION_PROPERTY,
    SOURCE_ARCHIVE_KIND,
    SOURCE_BIND_POSE_PROPERTY,
    SOURCE_INSTANCE_ID_PROPERTY,
    SourceFileFormat,
    SourceTakeProvenance,
    bone_bind_pose_fingerprint,
    bone_hierarchy_fingerprint,
    sha256_file,
    validate_source_take_path,
)


class SourceTakeImportError(RuntimeError):
    """Raised when an importer or imported Source Take fails validation."""


@dataclass(frozen=True, slots=True)
class SourceTakeCandidate:
    """Serializable user-facing identity for one imported animation take."""

    action_name: str
    frame_start: float
    frame_end: float
    take_id: str = ""


class SourceTakeSelectionRequired(SourceTakeImportError):
    """Raised when a caller must explicitly choose one of several takes."""

    def __init__(self, candidates: tuple[SourceTakeCandidate, ...]) -> None:
        self.candidates = candidates
        summaries = "; ".join(
            f"{candidate.action_name!r} "
            f"[frames {candidate.frame_start:g}..{candidate.frame_end:g}]"
            for candidate in candidates
        )
        super().__init__(
            "Source Take contains multiple usable Actions and cannot be "
            f"selected safely. Candidates: {summaries}. "
            "Choose one take explicitly."
        )


ImportDispatcher = Callable[[Path, SourceFileFormat], set[str]]
BlenderOperator = Callable[..., set[str]]


@dataclass(frozen=True, slots=True)
class SourceTakeImport:
    filepath: Path
    file_format: SourceFileFormat
    armature: bpy.types.Object
    source_action: bpy.types.Action
    archive_action: bpy.types.Action
    usable_actions: tuple[bpy.types.Action, ...]
    imported_objects: tuple[bpy.types.Object, ...]
    provenance: SourceTakeProvenance


ImportCommit = Callable[[SourceTakeImport], None]


_SNAPSHOT_COLLECTIONS = (
    "objects",
    "collections",
    "actions",
    "shape_keys",
    "meshes",
    "armatures",
    "curves",
    "cameras",
    "lights",
    "lattices",
    "metaballs",
    "grease_pencils",
    "pointclouds",
    "volumes",
    "materials",
    "node_groups",
    "textures",
    "images",
    "fonts",
)

_CLEANUP_ORDER = (
    "objects",
    "collections",
    "actions",
    "shape_keys",
    "meshes",
    "armatures",
    "curves",
    "cameras",
    "lights",
    "lattices",
    "metaballs",
    "grease_pencils",
    "pointclouds",
    "volumes",
    "materials",
    "node_groups",
    "textures",
    "images",
    "fonts",
)


@dataclass(frozen=True, slots=True)
class _DataSnapshot:
    pointers: dict[str, frozenset[int]]

    @classmethod
    def capture(cls) -> _DataSnapshot:
        return cls(
            {
                name: frozenset(item.as_pointer() for item in getattr(bpy.data, name))
                for name in _SNAPSHOT_COLLECTIONS
                if hasattr(bpy.data, name)
            }
        )

    def new_ids(self, collection_name: str) -> tuple[bpy.types.ID, ...]:
        before = self.pointers.get(collection_name, frozenset())
        collection = getattr(bpy.data, collection_name)
        return tuple(item for item in collection if item.as_pointer() not in before)


@dataclass(slots=True)
class _OwnedData:
    """Exact IDs owned after an importer has returned ``FINISHED``."""

    pointers: dict[str, set[int]]

    @classmethod
    def freeze(cls, snapshot: _DataSnapshot) -> _OwnedData:
        return cls(
            {
                name: {item.as_pointer() for item in snapshot.new_ids(name)}
                for name in _SNAPSHOT_COLLECTIONS
                if hasattr(bpy.data, name)
            }
        )

    def add(self, collection_name: str, item: bpy.types.ID) -> None:
        self.pointers.setdefault(collection_name, set()).add(item.as_pointer())


@dataclass(frozen=True, slots=True)
class _EditorState:
    active: bpy.types.Object | None
    selected: tuple[bpy.types.Object, ...]
    mode: str
    frame_start: int
    frame_end: int
    use_preview_range: bool
    preview_start: int
    preview_end: int
    frame_current: int
    subframe: float

    @classmethod
    def capture(cls, context: bpy.types.Context) -> _EditorState:
        scene = context.scene
        active = context.view_layer.objects.active
        return cls(
            active=active,
            selected=tuple(context.selected_objects),
            mode="OBJECT" if active is None else active.mode,
            frame_start=scene.frame_start,
            frame_end=scene.frame_end,
            use_preview_range=scene.use_preview_range,
            preview_start=scene.frame_preview_start,
            preview_end=scene.frame_preview_end,
            frame_current=scene.frame_current,
            subframe=scene.frame_subframe,
        )

    def restore(self, context: bpy.types.Context) -> None:
        current = context.view_layer.objects.active
        if current is not None and current.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        for obj in tuple(context.selected_objects):
            obj.select_set(False)
        for obj in self.selected:
            if _object_is_in_view_layer(context, obj):
                obj.select_set(True)

        active = self.active
        if active is not None and _object_is_in_view_layer(context, active):
            context.view_layer.objects.active = active
        else:
            context.view_layer.objects.active = None

        scene = context.scene
        scene.frame_start = self.frame_start
        scene.frame_end = self.frame_end
        scene.use_preview_range = self.use_preview_range
        scene.frame_preview_start = self.preview_start
        scene.frame_preview_end = self.preview_end
        scene.frame_set(self.frame_current, subframe=self.subframe)

        if (
            active is not None
            and context.view_layer.objects.active is active
            and active.mode != self.mode
        ):
            bpy.ops.object.mode_set(mode=self.mode)


def _object_is_in_view_layer(
    context: bpy.types.Context,
    obj: bpy.types.Object,
) -> bool:
    return any(candidate is obj for candidate in context.view_layer.objects)


def _prepare_object_mode(context: bpy.types.Context) -> None:
    active = context.view_layer.objects.active
    if active is not None and active.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")


def _remove_id(collection: object, item: bpy.types.ID) -> None:
    try:
        collection.remove(item, do_unlink=True)
    except TypeError:
        collection.remove(item)


def _cleanup_data_pointers(pointers: dict[str, set[int]]) -> None:
    """Remove only live IDs whose pointers are explicitly owned."""

    for collection_name in _CLEANUP_ORDER:
        if not hasattr(bpy.data, collection_name):
            continue
        collection = getattr(bpy.data, collection_name)
        # Key datablocks have no collection.remove API.  Removing their newly
        # imported Mesh/Curve/Lattice owner below releases them automatically.
        if collection_name == "shape_keys":
            continue
        owned = pointers.get(collection_name, set())
        for item in reversed(tuple(collection)):
            if item.as_pointer() not in owned:
                continue
            try:
                _remove_id(collection, item)
            except ReferenceError:
                pass


def _cleanup_new_data(snapshot: _DataSnapshot) -> None:
    """Clean a failed/CANCELLED importer before an ownership seam exists.

    Until the importer returns ``FINISHED``, Blender exposes no reliable way to
    distinguish its partial IDs from IDs created by callbacks it invokes.  The
    whole post-snapshot delta is therefore treated as importer-owned only on
    that early failure path.
    """

    _cleanup_data_pointers(
        {
            name: {item.as_pointer() for item in snapshot.new_ids(name)}
            for name in _SNAPSHOT_COLLECTIONS
            if hasattr(bpy.data, name)
        }
    )


def _cleanup_owned_data(owned: _OwnedData) -> None:
    """Clean only IDs frozen at importer success or added explicitly later."""

    _cleanup_data_pointers(owned.pointers)


def _id_is_live(collection_name: str, item: bpy.types.ID) -> bool:
    """Return whether ``item`` still belongs to Blender's live ID collection."""

    return any(candidate is item for candidate in getattr(bpy.data, collection_name))


def _require_live_import_result(
    context: bpy.types.Context,
    result: SourceTakeImport,
) -> None:
    """Reject IDs removed by callbacks before the import transaction commits."""

    if not _id_is_live("objects", result.armature):
        raise SourceTakeImportError(
            "Imported Source rig was removed before the import could finish"
        )
    if not _object_is_in_view_layer(context, result.armature):
        raise SourceTakeImportError(
            "Imported Source rig left the current View Layer before import finished"
        )
    if any(not _id_is_live("objects", obj) for obj in result.imported_objects):
        raise SourceTakeImportError(
            "An imported object was removed before the import could finish"
        )
    if not _id_is_live("actions", result.source_action):
        raise SourceTakeImportError(
            "Imported Source Action was removed before the import could finish"
        )
    if not _id_is_live("actions", result.archive_action):
        raise SourceTakeImportError(
            "Imported Source archive was removed before the import could finish"
        )
    try:
        armature_data = result.armature.data
    except ReferenceError as error:
        raise SourceTakeImportError(
            "Imported Source Armature was removed before the import could finish"
        ) from error
    if not _id_is_live("armatures", armature_data):
        raise SourceTakeImportError(
            "Imported Source Armature was removed before the import could finish"
        )


def _operator_exists(operator: BlenderOperator) -> bool:
    rna_type = getattr(operator, "get_rna_type", None)
    if rna_type is None:
        return True
    try:
        rna_type()
    except RuntimeError:
        return False
    return True


def _run_operator(
    operator: BlenderOperator,
    *,
    filepath: Path,
    options: dict[str, object] | None = None,
) -> set[str]:
    kwargs: dict[str, object] = {"filepath": str(filepath)}
    if options is not None:
        kwargs.update(options)
    result = operator(**kwargs)
    if "FINISHED" not in result:
        raise SourceTakeImportError(f"Import operator returned {sorted(result)}")
    return result


def _dispatch_fbx_import(
    filepath: Path,
    *,
    fast_operator: BlenderOperator | None = None,
    legacy_operator: BlenderOperator | None = None,
) -> set[str]:
    """Prefer Blender 5's native FBX importer, then use the add-on fallback."""

    fast = fast_operator
    if fast is None:
        fast = getattr(bpy.ops.wm, "fbx_import")
    legacy = legacy_operator
    if legacy is None:
        legacy = bpy.ops.import_scene.fbx

    fast_error: Exception | None = None
    if _operator_exists(fast):
        attempt_snapshot = _DataSnapshot.capture()
        try:
            return _run_operator(fast, filepath=filepath)
        except (RuntimeError, TypeError, ValueError) as error:
            fast_error = error
            _cleanup_new_data(attempt_snapshot)

    try:
        return _run_operator(
            legacy,
            filepath=filepath,
            options={"use_anim": True, "anim_offset": 0.0},
        )
    except (RuntimeError, TypeError, ValueError) as legacy_error:
        if fast_error is None:
            raise SourceTakeImportError(
                f"FBX import failed: {legacy_error}"
            ) from legacy_error
        raise SourceTakeImportError(
            f"Native FBX import failed ({fast_error}); "
            f"compatibility importer failed ({legacy_error})"
        ) from legacy_error


def _dispatch_import(filepath: Path, file_format: SourceFileFormat) -> set[str]:
    if file_format is SourceFileFormat.FBX:
        return _dispatch_fbx_import(filepath)
    return _run_operator(bpy.ops.import_scene.gltf, filepath=filepath)


def _action_has_animation(action: bpy.types.Action) -> bool:
    if getattr(action, "is_empty", False):
        return False
    try:
        start, end = (float(frame) for frame in action.frame_range)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(start) or not math.isfinite(end) or end < start:
        return False
    for curve in iter_action_fcurves(action):
        if len(curve.keyframe_points) > 0 or len(curve.sampled_points) > 0:
            return True
    return False


def _linked_actions(armature: bpy.types.Object) -> Iterable[bpy.types.Action]:
    animation_data = armature.animation_data
    if animation_data is None:
        return
    if animation_data.action is not None:
        yield animation_data.action
    for track in animation_data.nla_tracks:
        for strip in track.strips:
            if strip.action is not None:
                yield strip.action


def _usable_actions(
    armature: bpy.types.Object,
    new_actions: tuple[bpy.types.Action, ...],
) -> tuple[bpy.types.Action, ...]:
    usable = {
        action.as_pointer(): action
        for action in new_actions
        if _action_has_animation(action)
    }
    if not usable:
        return ()

    def has_armature_curve(action: bpy.types.Action) -> bool:
        return any(
            curve.data_path.startswith('pose.bones["')
            for curve in iter_action_fcurves(action)
        )

    ordered: list[bpy.types.Action] = []
    seen: set[int] = set()
    for action in _linked_actions(armature):
        pointer = action.as_pointer()
        if pointer in usable and pointer not in seen:
            ordered.append(action)
            seen.add(pointer)
    if not ordered:
        # Pose-looking curves can still belong to an unbound Action.  Returning
        # one as the primary take would leave the Source Armature evaluating its
        # rest pose in the retarget step.
        return ()
    for action in sorted(usable.values(), key=lambda candidate: candidate.name):
        pointer = action.as_pointer()
        if pointer not in seen and has_armature_curve(action):
            ordered.append(action)
            seen.add(pointer)
    return tuple(ordered)


def _action_candidate_summary(action: bpy.types.Action) -> str:
    frame_start, frame_end = (float(frame) for frame in action.frame_range)
    return f"{action.name!r} [frames {frame_start:g}..{frame_end:g}]"


def _action_candidate(
    action: bpy.types.Action,
    take_id: str,
) -> SourceTakeCandidate:
    frame_start, frame_end = (float(frame) for frame in action.frame_range)
    return SourceTakeCandidate(action.name, frame_start, frame_end, take_id)


def _take_id(
    action: bpy.types.Action,
    *,
    file_sha256: str,
    hierarchy_sha256: str,
    ordinal: int,
) -> str:
    """Return a stable ID unaffected by scene-local datablock renaming."""

    payload = json.dumps(
        {
            "actionContentSha256": action_content_checksum(action),
            "fileSha256": file_sha256,
            "hierarchySha256": hierarchy_sha256,
            "ordinal": ordinal,
            "schemaVersion": 1,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _archive_name(provenance: SourceTakeProvenance) -> str:
    safe_action_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", provenance.action_name)
    safe_action_name = safe_action_name.strip("_") or "Take"
    return f"SOURCE_ARCHIVE::{provenance.file_sha256[:12]}::{safe_action_name}"


def _apply_custom_properties(
    target: bpy.types.ID,
    properties: dict[str, str | int | float | bool],
) -> None:
    for name, value in properties.items():
        target[name] = value


def _lock_archive_action(action: bpy.types.Action) -> None:
    """Make the archive visibly non-editable in Graph Editor surfaces."""

    locked_groups: set[int] = set()
    for curve in iter_action_fcurves(action):
        curve.lock = True
        curve.hide = True
        group = curve.group
        if group is None or group.as_pointer() in locked_groups:
            continue
        group.lock = True
        locked_groups.add(group.as_pointer())


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


def _bind_selected_source_action(
    armature: bpy.types.Object,
    action: bpy.types.Action,
    slot: object | None,
) -> None:
    """Make the displayed Source rig evaluate the same take as its Archive."""

    animation_data = armature.animation_data_create()
    if animation_data.use_tweak_mode:
        raise SourceTakeImportError(
            "Imported Source Take unexpectedly retained NLA Tweak Mode"
        )
    for track in animation_data.nla_tracks:
        track.mute = True
    animation_data.action = action
    if slot is not None:
        if hasattr(animation_data, "action_slot"):
            animation_data.action_slot = slot
        elif hasattr(animation_data, "action_slot_handle"):
            animation_data.action_slot_handle = slot.handle
        else:
            raise SourceTakeImportError(
                "Imported Source Take cannot bind the selected Action Slot"
            )
    if animation_data.action is not action:
        raise SourceTakeImportError(
            "Selected Source Take Action could not be activated"
        )


def import_source_take(
    context: bpy.types.Context,
    filepath: str | Path,
    *,
    selected_take_id: str | None = None,
    selected_action_name: str | None = None,
    _importer: ImportDispatcher | None = None,
    _commit: ImportCommit | None = None,
) -> SourceTakeImport:
    """Import and archive one FBX/glTF Source Take as an atomic transaction.

    Existing datablocks are never considered import results.  Any importer or
    validation failure removes all datablocks created by the transaction and
    restores the user's selection, active mode, and timeline.  The original
    imported Action is left untouched; provenance is attached only to its
    fake-user archive copy and to the imported armature object.
    """

    if selected_take_id is not None and selected_action_name is not None:
        raise SourceTakeImportError(
            "Choose Source Take by id or legacy Action name, not both"
        )
    blender_path = Path(bpy.path.abspath(str(filepath)))
    validated = validate_source_take_path(blender_path)
    file_sha256 = sha256_file(validated.path)
    snapshot = _DataSnapshot.capture()
    editor_state = _EditorState.capture(context)
    restore_attempted = False
    owned_data: _OwnedData | None = None

    try:
        _prepare_object_mode(context)
        importer = _dispatch_import if _importer is None else _importer
        result = importer(validated.path, validated.file_format)
        if "FINISHED" not in result:
            raise SourceTakeImportError(
                f"Source Take importer returned {sorted(result)}"
            )
        # Freeze importer ownership before validation, frame restoration, and
        # the caller's commit callback. IDs created by those later callbacks
        # are external side effects and must never be swept up by rollback.
        owned_data = _OwnedData.freeze(snapshot)

        imported_objects = tuple(
            item
            for item in snapshot.new_ids("objects")
            if isinstance(item, bpy.types.Object)
        )
        armatures = tuple(obj for obj in imported_objects if obj.type == "ARMATURE")
        if len(armatures) != 1:
            raise SourceTakeImportError(
                f"Source Take must contain exactly one armature; got {len(armatures)}"
            )
        armature = armatures[0]
        new_actions = tuple(
            item
            for item in snapshot.new_ids("actions")
            if isinstance(item, bpy.types.Action)
        )
        usable_actions = _usable_actions(armature, new_actions)
        if not usable_actions:
            raise SourceTakeImportError(
                "Source Take must contain at least one Action with animation keys"
            )
        hierarchy_sha256 = bone_hierarchy_fingerprint(
            (
                bone.name,
                None if bone.parent is None else bone.parent.name,
            )
            for bone in armature.data.bones
        )
        candidates = tuple(
            _action_candidate(
                action,
                _take_id(
                    action,
                    file_sha256=file_sha256,
                    hierarchy_sha256=hierarchy_sha256,
                    ordinal=index,
                ),
            )
            for index, action in enumerate(usable_actions)
        )
        if selected_take_id is None and selected_action_name is None:
            if len(usable_actions) > 1:
                raise SourceTakeSelectionRequired(candidates)
            source_action = usable_actions[0]
            selected_candidate = candidates[0]
        elif selected_take_id is not None:
            matching = tuple(
                (action, candidate)
                for action, candidate in zip(usable_actions, candidates, strict=True)
                if candidate.take_id == selected_take_id
            )
            if len(matching) != 1:
                raise SourceTakeImportError(
                    f"Selected Source Take id {selected_take_id!r} was not found uniquely"
                )
            source_action, selected_candidate = matching[0]
        else:
            selected_actions = tuple(
                (action, candidate)
                for action, candidate in zip(usable_actions, candidates, strict=True)
                if action.name == selected_action_name
            )
            if len(selected_actions) != 1:
                candidates = "; ".join(
                    _action_candidate_summary(action) for action in usable_actions
                )
                raise SourceTakeImportError(
                    f"Selected Source Take {selected_action_name!r} was not found "
                    f"uniquely. Candidates: {candidates}"
                )
            source_action, selected_candidate = selected_actions[0]
        source_slot = action_slot_for_id(source_action, armature)
        source_slots = getattr(source_action, "slots", None)
        if source_slots is not None and len(source_slots) and source_slot is None:
            raise SourceTakeImportError(
                "Source Action has no unambiguous armature Action Slot"
            )
        _bind_selected_source_action(armature, source_action, source_slot)
        source_slot_identifier = str(getattr(source_slot, "identifier", ""))
        frame_start, frame_end = (float(frame) for frame in source_action.frame_range)
        provenance = SourceTakeProvenance(
            file_format=validated.file_format,
            source_filepath=str(validated.path),
            file_sha256=file_sha256,
            source_armature=armature.name,
            action_name=source_action.name,
            frame_start=frame_start,
            frame_end=frame_end,
            bone_hierarchy_sha256=hierarchy_sha256,
            take_id=selected_candidate.take_id,
        )
        source_instance_id = str(uuid.uuid4())
        bind_pose_sha256 = _armature_bind_pose_fingerprint(armature)

        archive = source_action.copy()
        owned_data.add("actions", archive)
        archive.name = _archive_name(provenance)
        archive.use_fake_user = True
        archive_properties = provenance.custom_properties()
        archive_properties.update(
            {
                "motion_bridge_archive_kind": SOURCE_ARCHIVE_KIND,
                "motion_bridge_archive_immutable": True,
                SOURCE_INSTANCE_ID_PROPERTY: source_instance_id,
                SOURCE_ACTION_SLOT_PROPERTY: source_slot_identifier,
                SOURCE_BIND_POSE_PROPERTY: bind_pose_sha256,
            }
        )
        _apply_custom_properties(archive, archive_properties)
        _lock_archive_action(archive)
        _apply_custom_properties(armature, provenance.custom_properties())
        armature[SOURCE_INSTANCE_ID_PROPERTY] = source_instance_id
        armature[SOURCE_ACTION_SLOT_PROPERTY] = source_slot_identifier
        armature[SOURCE_BIND_POSE_PROPERTY] = bind_pose_sha256
        armature[SOURCE_ARCHIVE_ACTION_PROPERTY] = archive.name

        pending = SourceTakeImport(
            filepath=validated.path,
            file_format=validated.file_format,
            armature=armature,
            source_action=source_action,
            archive_action=archive,
            usable_actions=usable_actions,
            imported_objects=imported_objects,
            provenance=provenance,
        )
        # Restoring the frame can run arbitrary frame_change handlers. Keep all
        # new IDs inside the transaction until those callbacks complete and the
        # caller has committed its external pointer (the Session Source).
        restore_attempted = True
        editor_state.restore(context)
        _require_live_import_result(context, pending)
        if _commit is not None:
            _commit(pending)
        _require_live_import_result(context, pending)
        return pending
    except Exception:
        if owned_data is None:
            _cleanup_new_data(snapshot)
        else:
            _cleanup_owned_data(owned_data)
        raise
    finally:
        if not restore_attempted:
            try:
                editor_state.restore(context)
            except Exception:
                if owned_data is None:
                    _cleanup_new_data(snapshot)
                else:
                    _cleanup_owned_data(owned_data)
                raise
