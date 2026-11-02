from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
import time
import uuid

import bpy
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras.io_utils import ImportHelper

from .canonical.rig_spec import SKELETON_HASH, SKELETON_ID
from .canonical.runtime_descriptor import descriptor_from_armature
from .editing.action_api import action_slot_for_id
from .editing.auto_correction import (
    AUTO_CORRECTION_MAX_FRAMES,
    FootLockSide,
    apply_foot_lock_correction,
    apply_joint_limit_correction,
    apply_smoothing_correction,
)
from .editing.comparison import (
    corrections_preview_enabled,
    set_corrections_preview_enabled,
)
from .editing.correction_edit import (
    active_correction_edit_layer,
    apply_correction_pose_range,
    cancel_all_correction_edit_sessions,
    cancel_correction_edit_mode,
    clear_correction_edit_sessions,
    enter_correction_edit_mode,
    exit_correction_edit_mode,
    exit_stale_correction_tweak_mode,
    has_correction_edit_session,
    insert_correction_pose_keys,
    is_correction_editing,
    remove_correction_pose_keys,
    reset_correction_pose_keys,
    scene_to_correction_frame,
    stale_correction_tweak_layer,
)
from .editing.correction_envelope import (
    active_correction_envelope_preview,
    begin_committed_pose_envelope_preview,
    begin_pose_envelope_preview,
    begin_reweight_envelope_preview,
    cancel_all_correction_envelope_previews,
    clear_correction_envelope_previews,
    committed_pose_envelope_available,
    has_correction_envelope_preview,
    resolve_envelope_range,
)
from .editing.correction_layers import (
    action_is_unshared_for_armature,
    action_checksum,
    add_correction_layer,
    correction_actions_are_unshared,
    create_correction_layer,
    duplicate_correction_layer,
    get_correction_layer_stack,
    has_unsupported_nla_coexistence,
    move_correction_layer,
    remove_correction_layer,
    rename_correction_layer,
    set_active_correction_layer,
    set_correction_layer_muted,
    set_correction_layer_solo,
    validate_layer_stack,
)
from .editing.correction_writer import (
    MOTION_REVISION_PROPERTY,
    CorrectionBoneTarget,
    CorrectionFrameTarget,
    write_correction_target,
)
from .editing.motion_sample import _evaluated_local_matrix
from .editing.pose_api import pose_bone_selected
from .editing.foot_contact import detect_and_commit_contact_preview_range
from .editing.motion_sample import (
    MAX_MOTION_SAMPLES,
    MotionRangeSampler,
    sample_motion_range,
)
from .editing.layer_merge import merge_correction_layer_down
from .editing.flatten_stack import commit_corrections_to_base
from .editing.rig_visualization import (
    RigVisualizationResult,
    apply_rig_visualization,
    clear_rig_visualization_snapshots,
    has_rig_visualization_snapshot,
    restore_all_rig_visualizations,
    restore_rig_visualization,
)
from .io.fbx_export import (
    MB_OT_ExportCanonicalActionFbx,
    MB_OT_ExportCorrectedFbx,
)
from .io.source_import import (
    SourceTakeImport,
    SourceTakeImportError,
    SourceTakeSelectionRequired,
    import_source_take,
)
from .io.source_take import (
    SOURCE_ACTION_SLOT_PROPERTY,
    SOURCE_INSTANCE_ID_PROPERTY,
    SOURCE_PROVENANCE_SCHEMA_PROPERTY,
    SOURCE_PROVENANCE_SCHEMA_VERSION,
    bone_bind_pose_fingerprint,
    bone_hierarchy_fingerprint,
)
from .live.control_client import (
    BlenderControlClient,
    BlenderControlClientConfig,
    ControlSessionState,
    ReconnectBackoff,
    is_retryable_control_error,
)
from .live.pose_session import LivePoseSession
from .live.scheduling import PoseSendScheduler
from .performance.budget import TakeBudgetTier
from .performance.take_analysis import analyze_action, format_point_count
from .motion_quality import MotionValidationSettings, analyze_motion
from .motion_quality_process import MotionQualityProcessError, MotionQualityProcessJob
from .foot_lock import FootLockSettings
from .motion_filter import SmoothingStrength
from .range_spec import (
    RangeSource,
    RangeSpec,
    centered_preview_range,
    resolve_range_spec,
)
from .retargeting import (
    PROP_RETARGET_RECIPE,
    bake_archived_source_to_canonical,
    bake_source_to_canonical,
    infer_humanoid_mapping,
    regenerate_canonical_base,
    validate_humanoid_mapping,
)
from .retargeting.profile import (
    HUMANOID_MAPPING_FIELDS,
    SourceMappingProfile,
    SourceMappingProfileError,
    StaleSourceMappingProfileError,
    mapping_from_profile_values,
    profile_values_from_mapping,
)
from .workflow_guide import (
    GuideStage,
    LiveGuideState,
    WorkflowFacts,
    friendly_live_error,
    resolve_guide_stage,
    resolve_live_guide_state,
)


_live_session: LivePoseSession | None = None
_live_scheduler: PoseSendScheduler | None = None
_control_client: BlenderControlClient | None = None
_pending_live_armature: bpy.types.Object | None = None
_pending_control_config: BlenderControlClientConfig | None = None
_pending_root_mode = "WORLD_ROOT"
_live_reconnect_backoff = ReconnectBackoff()
_last_live_error = ""
_LIVE_TIMER_POLL_SECONDS = 1.0 / 60.0

_ENVELOPE_CURVE_TREE_NAME = ".Motion Bridge Bone Correction Envelope"
_ENVELOPE_CURVE_NODE_NAME = "Bone Correction Envelope"
_ENVELOPE_CURVE_SCHEMA = 1
_ENVELOPE_CURVE_OWNED_PROPERTY = "motion_bridge_owned"
_ENVELOPE_CURVE_SCHEMA_PROPERTY = "motion_bridge_envelope_schema"
_ENVELOPE_CURVE_OWNER_PROPERTY = "motion_bridge_envelope_owner_scene_id"
_ENVELOPE_CURVE_OWNER_SCENE_PROPERTY = "motion_bridge_envelope_owner_scene"
_ENVELOPE_SCENE_ID_PROPERTY = "motion_bridge_envelope_scene_id"
_SOURCE_MAPPING_INSTANCE_ID_PROPERTY = "motion_bridge_mapping_source_instance_id"
_SOURCE_MAPPING_ACTION_CHECKSUM_PROPERTY = (
    "motion_bridge_mapping_source_action_checksum"
)
_SOURCE_TAKE_ENUM_CACHE: dict[str, tuple[tuple[str, str, str, int], ...]] = {}
_MANUAL_MAPPING_UPDATE_GUARD: set[int] = set()
_ENVELOPE_PRESET_POINTS: dict[str, tuple[tuple[float, float], ...]] = {
    "SMOOTH_PULSE": ((0.0, 0.0), (0.5, 1.0), (1.0, 0.0)),
    "PLATEAU": ((0.0, 0.0), (0.2, 1.0), (0.8, 1.0), (1.0, 0.0)),
    "EASE_IN": ((0.0, 0.0), (0.5, 0.25), (1.0, 1.0)),
    "EASE_OUT": ((0.0, 0.0), (0.5, 0.75), (1.0, 1.0)),
    "EASE_IN_OUT": ((0.0, 0.0), (0.25, 0.1), (0.75, 0.9), (1.0, 1.0)),
    "S_CURVE": ((0.0, 0.0), (0.2, 0.05), (0.5, 0.5), (0.8, 0.95), (1.0, 1.0)),
}
_envelope_last_error = ""
_source_pose_clipboard: tuple[
    int,
    str,
    float,
    tuple[CorrectionBoneTarget, ...],
    tuple[float, ...] | None,
    tuple[float, ...] | None,
] | None = None
_rig_visual_last_error = ""


@dataclass(slots=True)
class _MotionQualityScanDisplayState:
    armature_pointer: int
    dependency_pointers: frozenset[int]
    sampled_count: int
    sample_count: int
    phase: str = "SAMPLING"
    analysis_progress: float = 0.0
    cancel_requested: bool = False
    input_changed: bool = False
    internal_update: bool = False


_motion_quality_scan_state: _MotionQualityScanDisplayState | None = None
_motion_quality_scan_operator: object | None = None
_motion_quality_pending_cleanup_jobs: list[object] = []
_MOTION_QUALITY_CLEANUP_RETRY_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class _SourceImportSessionState:
    source_armature: bpy.types.Object | None
    manual_mapping_bound_source: bpy.types.Object | None
    manual_mapping_profile_json: str
    manual_mapping_profiles_json: str
    manual_mapping: dict[str, str]
    retarget_preset: str
    beginner_retarget_error: str
    source_properties: tuple[
        tuple[bpy.types.Object, tuple[tuple[str, bool, object], ...]], ...
    ]
    rig_visual_canonical: bpy.types.Object | None
    rig_visual_was_applied: bool


def _source_take_enum_items(
    operator: object,
    _context: object,
) -> tuple[tuple[str, str, str, int], ...]:
    raw_candidates = str(getattr(operator, "take_candidates_json", ""))
    cached = _SOURCE_TAKE_ENUM_CACHE.get(raw_candidates)
    if cached is not None:
        return cached
    try:
        candidates = json.loads(raw_candidates)
    except json.JSONDecodeError:
        candidates = []
    items = tuple(
        (
            str(candidate["takeId"]),
            str(candidate["actionName"]),
            (
                f"{float(candidate['frameStart']):g}〜"
                f"{float(candidate['frameEnd']):g}フレーム"
            ),
            index,
        )
        for index, candidate in enumerate(candidates)
        if isinstance(candidate, dict)
        and isinstance(candidate.get("takeId"), str)
        and candidate["takeId"]
        and isinstance(candidate.get("actionName"), str)
        and candidate["actionName"]
    )
    if len(_SOURCE_TAKE_ENUM_CACHE) >= 32:
        _SOURCE_TAKE_ENUM_CACHE.pop(next(iter(_SOURCE_TAKE_ENUM_CACHE)))
    _SOURCE_TAKE_ENUM_CACHE[raw_candidates] = items
    return items


def _envelope_scene(settings: object) -> bpy.types.Scene | None:
    scene = getattr(settings, "id_data", None)
    return scene if isinstance(scene, bpy.types.Scene) else None


def _envelope_scene_owner_id(
    settings: object,
    *,
    create: bool,
) -> str | None:
    """Return this Scene's unique persistent Envelope owner identifier."""

    scene = _envelope_scene(settings)
    if scene is None:
        return None
    raw_owner = scene.get(_ENVELOPE_SCENE_ID_PROPERTY)
    owner = raw_owner if isinstance(raw_owner, str) and raw_owner else None
    if owner is not None:
        matching_scenes = [
            candidate
            for candidate in bpy.data.scenes
            if candidate.get(_ENVELOPE_SCENE_ID_PROPERTY) == owner
        ]
        if len(matching_scenes) == 1:
            return owner
        # Duplicating a Scene also copies custom properties and its PointerProperty.
        # The NodeTree's direct Scene reference identifies the original owner;
        # an explicit edit assigns every copy a fresh UUID and Tree.
        owns_tree = False
        for tree in bpy.data.node_groups:
            try:
                owns_tree = (
                    bool(tree.get(_ENVELOPE_CURVE_OWNED_PROPERTY, False))
                    and int(tree.get(_ENVELOPE_CURVE_SCHEMA_PROPERTY, 0))
                    == _ENVELOPE_CURVE_SCHEMA
                    and tree.get(_ENVELOPE_CURVE_OWNER_PROPERTY) == owner
                    and tree.get(_ENVELOPE_CURVE_OWNER_SCENE_PROPERTY) == scene
                )
            except (ReferenceError, TypeError, ValueError):
                owns_tree = False
            if owns_tree:
                break
        if owns_tree:
            return owner
    if not create:
        return None

    existing: set[str] = set()
    for candidate in bpy.data.scenes:
        candidate_owner = candidate.get(_ENVELOPE_SCENE_ID_PROPERTY)
        if isinstance(candidate_owner, str) and candidate_owner:
            existing.add(candidate_owner)
    owner = uuid.uuid4().hex
    while owner in existing:
        owner = uuid.uuid4().hex
    scene[_ENVELOPE_SCENE_ID_PROPERTY] = owner
    return owner


def _owned_envelope_curve_tree(
    tree: object,
    scene: bpy.types.Scene,
    owner: str,
) -> bpy.types.NodeTree | None:
    try:
        valid = (
            tree is not None
            and tree.bl_idname == "ShaderNodeTree"
            and bool(tree.get(_ENVELOPE_CURVE_OWNED_PROPERTY, False))
            and int(tree.get(_ENVELOPE_CURVE_SCHEMA_PROPERTY, 0))
            == _ENVELOPE_CURVE_SCHEMA
            and tree.get(_ENVELOPE_CURVE_OWNER_PROPERTY) == owner
            and tree.get(_ENVELOPE_CURVE_OWNER_SCENE_PROPERTY) == scene
        )
    except (ReferenceError, TypeError, ValueError):
        valid = False
    return tree if valid else None


def _owned_envelope_curve_node(
    tree: bpy.types.NodeTree,
    owner: str,
) -> bpy.types.ShaderNodeFloatCurve | None:
    for node in tree.nodes:
        try:
            valid = (
                node.bl_idname == "ShaderNodeFloatCurve"
                and bool(node.get(_ENVELOPE_CURVE_OWNED_PROPERTY, False))
                and int(node.get(_ENVELOPE_CURVE_SCHEMA_PROPERTY, 0))
                == _ENVELOPE_CURVE_SCHEMA
                and node.get(_ENVELOPE_CURVE_OWNER_PROPERTY) == owner
            )
        except (ReferenceError, TypeError, ValueError):
            valid = False
        if valid:
            return node
    return None


def _inherited_envelope_curve_tree(
    settings: object,
    scene: bpy.types.Scene,
) -> tuple[bpy.types.NodeTree, str] | None:
    """Return the owned Tree inherited by a freshly duplicated Scene.

    ``Scene.copy()`` duplicates the owner UUID and the PointerProperty while the
    Tree's direct owner reference still points at the source Scene.  Requiring
    that complete relationship prevents a foreign Tree with copied-looking
    custom properties from being adopted.
    """

    tree = getattr(settings, "correction_envelope_curve_tree", None)
    try:
        owner = tree.get(_ENVELOPE_CURVE_OWNER_PROPERTY) if tree is not None else None
        source_scene = (
            tree.get(_ENVELOPE_CURVE_OWNER_SCENE_PROPERTY)
            if tree is not None
            else None
        )
        if (
            not isinstance(owner, str)
            or not owner
            or scene.get(_ENVELOPE_SCENE_ID_PROPERTY) != owner
            or not isinstance(source_scene, bpy.types.Scene)
            or source_scene == scene
            or _owned_envelope_curve_tree(tree, source_scene, owner) is None
            or _owned_envelope_curve_node(tree, owner) is None
        ):
            return None
        source_settings = getattr(source_scene, "motion_bridge_session", None)
        source_tree = getattr(
            source_settings,
            "correction_envelope_curve_tree",
            None,
        )
        if source_tree is None or source_tree.as_pointer() != tree.as_pointer():
            return None
    except (AttributeError, ReferenceError, TypeError, ValueError):
        return None
    return tree, owner


def _find_envelope_curve_node(
    settings: object,
) -> bpy.types.ShaderNodeFloatCurve | None:
    """Find the envelope curve without mutating Blender data.

    Panel ``draw()`` callbacks run in a read-only ID context in Blender 5.
    Keep this lookup side-effect free so merely displaying the sidebar can
    never create a NodeTree, node, or Scene property assignment.
    """

    owner = _envelope_scene_owner_id(settings, create=False)
    scene = _envelope_scene(settings)
    if owner is None or scene is None:
        return None
    tree = _owned_envelope_curve_tree(
        getattr(settings, "correction_envelope_curve_tree", None),
        scene,
        owner,
    )
    if tree is None:
        return None
    return _owned_envelope_curve_node(tree, owner)


def _ensure_envelope_curve_node(
    settings: object,
) -> bpy.types.ShaderNodeFloatCurve:
    """Create or repair the curve from an operator/update callback only."""

    scene = _envelope_scene(settings)
    if scene is None:
        raise RuntimeError("Envelope curve settings must belong to a Scene")
    inherited = _inherited_envelope_curve_tree(settings, scene)
    owner = _envelope_scene_owner_id(settings, create=True)
    if owner is None:
        raise RuntimeError("Envelope curve settings must belong to a Scene")
    tree = _owned_envelope_curve_tree(
        getattr(settings, "correction_envelope_curve_tree", None),
        scene,
        owner,
    )
    if tree is None:
        if inherited is None:
            tree = bpy.data.node_groups.new(
                _ENVELOPE_CURVE_TREE_NAME,
                "ShaderNodeTree",
            )
        else:
            inherited_tree, inherited_owner = inherited
            tree = inherited_tree.copy()
            tree.use_fake_user = False
            inherited_node = _owned_envelope_curve_node(tree, inherited_owner)
            if inherited_node is None:
                bpy.data.node_groups.remove(tree)
                raise RuntimeError("Duplicated Envelope Tree lost its owned curve node")
            inherited_node[_ENVELOPE_CURVE_OWNER_PROPERTY] = owner
        tree[_ENVELOPE_CURVE_OWNED_PROPERTY] = True
        tree[_ENVELOPE_CURVE_SCHEMA_PROPERTY] = _ENVELOPE_CURVE_SCHEMA
        tree[_ENVELOPE_CURVE_OWNER_PROPERTY] = owner
        tree[_ENVELOPE_CURVE_OWNER_SCENE_PROPERTY] = scene
        settings.correction_envelope_curve_tree = tree

    node = _owned_envelope_curve_node(tree, owner)
    if node is None:
        node = tree.nodes.new("ShaderNodeFloatCurve")
        node.name = _ENVELOPE_CURVE_NODE_NAME
        node.label = _ENVELOPE_CURVE_NODE_NAME
        node[_ENVELOPE_CURVE_OWNED_PROPERTY] = True
        node[_ENVELOPE_CURVE_SCHEMA_PROPERTY] = _ENVELOPE_CURVE_SCHEMA
        node[_ENVELOPE_CURVE_OWNER_PROPERTY] = owner
        mapping = node.mapping
        mapping.use_clip = True
        mapping.clip_min_x = 0.0
        mapping.clip_max_x = 1.0
        mapping.clip_min_y = 0.0
        mapping.clip_max_y = 1.0
        preset = str(
            getattr(settings, "correction_envelope_preset", "SMOOTH_PULSE")
        )
        _set_envelope_curve_points(
            node,
            _ENVELOPE_PRESET_POINTS.get(
                preset,
                _ENVELOPE_PRESET_POINTS["SMOOTH_PULSE"],
            ),
        )
    return node


def _valid_envelope_owner_id(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = uuid.UUID(hex=value)
    except (AttributeError, ValueError):
        return None
    return value if parsed.hex == value and parsed.version == 4 else None


def _tombstoned_envelope_curve_tree(
    tree: object,
    live_owner_ids: set[str],
) -> bpy.types.NodeTree | None:
    """Recognize an owned Tree whose direct owner Scene was deleted."""

    try:
        owner = _valid_envelope_owner_id(tree.get(_ENVELOPE_CURVE_OWNER_PROPERTY))
        valid = (
            tree is not None
            and tree.bl_idname == "ShaderNodeTree"
            and bool(tree.get(_ENVELOPE_CURVE_OWNED_PROPERTY, False))
            and int(tree.get(_ENVELOPE_CURVE_SCHEMA_PROPERTY, 0))
            == _ENVELOPE_CURVE_SCHEMA
            and owner is not None
            and owner not in live_owner_ids
            and tree.get(_ENVELOPE_CURVE_OWNER_SCENE_PROPERTY) is None
            and _owned_envelope_curve_node(tree, owner) is not None
        )
    except (AttributeError, ReferenceError, TypeError, ValueError):
        valid = False
    return tree if valid else None


def _cleanup_envelope_curve_data() -> None:
    """Remove unreferenced Envelope trees owned by this add-on and Scene."""

    ownerships: list[tuple[bpy.types.Scene, str]] = []
    for scene in bpy.data.scenes:
        owner = scene.get(_ENVELOPE_SCENE_ID_PROPERTY)
        if isinstance(owner, str) and owner:
            ownerships.append((scene, owner))

    live_owned_trees = [
        tree
        for tree in bpy.data.node_groups
        if any(
            _owned_envelope_curve_tree(tree, scene, owner) is not None
            for scene, owner in ownerships
        )
    ]
    live_owner_ids = {owner for _scene, owner in ownerships}
    tombstoned_trees = [
        tree
        for tree in bpy.data.node_groups
        if _tombstoned_envelope_curve_tree(tree, live_owner_ids) is not None
    ]
    owned_trees = list(live_owned_trees)
    owned_tree_pointers = {tree.as_pointer() for tree in owned_trees}
    for tree in tombstoned_trees:
        if tree.as_pointer() not in owned_tree_pointers:
            owned_trees.append(tree)
            owned_tree_pointers.add(tree.as_pointer())
    for scene in bpy.data.scenes:
        settings = getattr(scene, "motion_bridge_session", None)
        tree = getattr(settings, "correction_envelope_curve_tree", None)
        try:
            references_owned_tree = (
                tree is not None and tree.as_pointer() in owned_tree_pointers
            )
        except ReferenceError:
            references_owned_tree = False
        if references_owned_tree:
            settings.correction_envelope_curve_tree = None

    for tree in owned_trees:
        try:
            removable = tree.users == 0 and not tree.use_fake_user
        except ReferenceError:
            removable = False
        if removable:
            bpy.data.node_groups.remove(tree)


def _set_envelope_curve_points(
    node: bpy.types.ShaderNodeFloatCurve,
    points: tuple[tuple[float, float], ...],
) -> None:
    mapping = node.mapping
    curve = mapping.curves[0]
    while len(curve.points) > 2:
        curve.points.remove(curve.points[1])
    curve.points[0].location = points[0]
    curve.points[-1].location = points[-1]
    for x, y in points[1:-1]:
        curve.points.new(float(x), float(y))
    for point in curve.points:
        point.handle_type = "AUTO_CLAMPED"
    mapping.update()


def _update_envelope_curve_preset(settings: object, _context: object) -> None:
    preset = str(getattr(settings, "correction_envelope_preset", "SMOOTH_PULSE"))
    points = _ENVELOPE_PRESET_POINTS.get(preset)
    if points is None:
        return
    try:
        _set_envelope_curve_points(_ensure_envelope_curve_node(settings), points)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        # Registration and file-load callbacks can briefly run without a valid
        # NodeTree owner.  The next explicit edit operation repairs it.
        return


def _envelope_curve_signature(
    node: bpy.types.ShaderNodeFloatCurve,
) -> tuple[tuple[float, float, str], ...]:
    return tuple(
        (
            round(float(point.location[0]), 9),
            round(float(point.location[1]), 9),
            str(point.handle_type),
        )
        for point in node.mapping.curves[0].points
    )


def _envelope_curve_evaluator(
    node: bpy.types.ShaderNodeFloatCurve,
):
    mapping = node.mapping
    mapping.update()
    curve = mapping.curves[0]

    def evaluate(value: float) -> float:
        return float(mapping.evaluate(curve, min(1.0, max(0.0, float(value)))))

    evaluate.sample_times = tuple(  # type: ignore[attr-defined]
        sorted({min(1.0, max(0.0, float(point.location[0]))) for point in curve.points})
    )
    return evaluate


def _envelope_curve_metadata(
    settings: object,
    node: bpy.types.ShaderNodeFloatCurve,
) -> str:
    payload = {
        "schema": _ENVELOPE_CURVE_SCHEMA,
        "preset": str(settings.correction_envelope_preset),
        "points": [
            {
                "x": float(point.location[0]),
                "y": float(point.location[1]),
                "handle": str(point.handle_type),
            }
            for point in node.mapping.curves[0].points
        ],
        "clip": [
            float(node.mapping.clip_min_x),
            float(node.mapping.clip_max_x),
            float(node.mapping.clip_min_y),
            float(node.mapping.clip_max_y),
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _uses_archived_source_bake(source: bpy.types.Object) -> bool:
    return (
        bool(source.get(SOURCE_INSTANCE_ID_PROPERTY))
        or source.get(SOURCE_PROVENANCE_SCHEMA_PROPERTY)
        == SOURCE_PROVENANCE_SCHEMA_VERSION
    )


def _source_take_frame_range(
    source: bpy.types.Object | None,
) -> tuple[int, int] | None:
    if source is None or source.type != "ARMATURE":
        return None
    raw_start = source.get("motion_bridge_source_frame_start")
    raw_end = source.get("motion_bridge_source_frame_end")
    if raw_start is None or raw_end is None:
        animation_data = source.animation_data
        action = animation_data.action if animation_data is not None else None
        if action is None:
            return None
        raw_start, raw_end = action.frame_range
    try:
        start = float(raw_start)
        end = float(raw_end)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(start) or not math.isfinite(end) or end < start:
        return None
    return math.floor(start), math.ceil(end)


def _mark_live_pose_dirty(*_args: object) -> None:
    if _live_scheduler is not None:
        _live_scheduler.mark_dirty()


def _queue_motion_quality_cleanup(job: object) -> None:
    """Retain a failed process cleanup without retaining Blender IDs."""

    if all(
        candidate is not job for candidate in _motion_quality_pending_cleanup_jobs
    ):
        _motion_quality_pending_cleanup_jobs.append(job)


def _retry_motion_quality_cleanup_jobs() -> bool:
    """Retry retained process cleanup and remove only confirmed successes."""

    for job in tuple(_motion_quality_pending_cleanup_jobs):
        try:
            job.cancel()
        except Exception:
            continue
        _motion_quality_pending_cleanup_jobs[:] = [
            candidate
            for candidate in _motion_quality_pending_cleanup_jobs
            if candidate is not job
        ]
    return not _motion_quality_pending_cleanup_jobs


def _motion_quality_cleanup_timer() -> float | None:
    if _retry_motion_quality_cleanup_jobs():
        return None
    return _MOTION_QUALITY_CLEANUP_RETRY_SECONDS


def _ensure_motion_quality_cleanup_timer() -> None:
    if (
        _motion_quality_pending_cleanup_jobs
        and not bpy.app.timers.is_registered(_motion_quality_cleanup_timer)
    ):
        bpy.app.timers.register(
            _motion_quality_cleanup_timer,
            first_interval=_MOTION_QUALITY_CLEANUP_RETRY_SECONDS,
            persistent=True,
        )


def _cancel_motion_quality_runtime(
    *,
    force_cleanup: bool = False,
) -> None:
    global _motion_quality_scan_operator, _motion_quality_scan_state
    _retry_motion_quality_cleanup_jobs()
    operator = _motion_quality_scan_operator
    if operator is not None:
        try:
            if force_cleanup:
                operator._abort(bpy.context)
            else:
                # save/undo/load_pre run outside the modal callback.  Restore
                # Blender state now, but leave the timer alive so the callback
                # can return CANCELLED to WindowManager on its next event.
                operator._prepare_external_cancel(bpy.context)
        except Exception:
            # File load/unregister can invalidate the operator's Blender IDs.
            try:
                operator._abort(bpy.context)
            except Exception:
                if force_cleanup:
                    job = getattr(operator, "_analysis_job", None)
                    if job is not None:
                        _queue_motion_quality_cleanup(job)
                        operator._analysis_job = None
                    operator._sampler = None
                    operator._aborted = True
    elif _motion_quality_scan_state is not None:
        _motion_quality_scan_state = None
    if force_cleanup:
        _motion_quality_scan_operator = None
        _motion_quality_scan_state = None
        _ensure_motion_quality_cleanup_timer()


@persistent
def _handle_motion_quality_load_pre(*_args: object) -> None:
    # Restore the sampler's temporary frame before Blender invalidates its IDs.
    _cancel_motion_quality_runtime()


def _detach_motion_quality_runtime_after_load() -> None:
    """Release global ownership after Blender replaces all Scene IDs.

    The modal timer is intentionally left for its callback to return CANCELLED;
    clearing the globals here prevents a dropped load-time modal from leaving
    the newly loaded file permanently stuck in the scanning state.
    """

    global _motion_quality_scan_operator, _motion_quality_scan_state
    operator = _motion_quality_scan_operator
    if operator is not None:
        job = getattr(operator, "_analysis_job", None)
        if job is not None:
            try:
                job.cancel()
            except Exception:
                _queue_motion_quality_cleanup(job)
        operator._aborted = True
        operator._sampler = None
        operator._analysis_job = None
    _motion_quality_scan_operator = None
    _motion_quality_scan_state = None
    _ensure_motion_quality_cleanup_timer()


@persistent
def _handle_live_file_load(*_args: object) -> None:
    global _source_pose_clipboard
    _source_pose_clipboard = None
    _detach_motion_quality_runtime_after_load()
    _stop_live_session()
    clear_correction_envelope_previews()
    clear_correction_edit_sessions()
    clear_rig_visualization_snapshots()
    _initialize_manual_mapping_source_bindings()
    for scene in bpy.data.scenes:
        settings = getattr(scene, "motion_bridge_session", None)
        if settings is not None:
            _clear_motion_quality_result(settings)


@persistent
def _handle_envelope_save_pre(*_args: object) -> None:
    global _envelope_last_error
    _cancel_motion_quality_runtime()
    had_preview = any(
        obj.type == "ARMATURE" and has_correction_envelope_preview(obj)
        for obj in bpy.data.objects
    )
    if not had_preview:
        return
    errors = cancel_all_correction_envelope_previews()
    _envelope_last_error = (
        "Envelope preview was cancelled before saving"
        if not errors
        else "Envelope save rollback failed: " + "; ".join(errors)
    )
    _mark_live_pose_dirty()


@persistent
def _handle_envelope_history_pre(*_args: object) -> None:
    global _envelope_last_error
    _cancel_motion_quality_runtime()
    had_preview = any(
        obj.type == "ARMATURE" and has_correction_envelope_preview(obj)
        for obj in bpy.data.objects
    )
    if not had_preview:
        return
    errors = cancel_all_correction_envelope_previews()
    _envelope_last_error = (
        "Envelope preview was cancelled before Undo/Redo"
        if not errors
        else "Envelope history rollback failed: " + "; ".join(errors)
    )


@persistent
def _handle_motion_quality_history_post(*_args: object) -> None:
    for scene in bpy.data.scenes:
        settings = getattr(scene, "motion_bridge_session", None)
        if settings is not None:
            _clear_motion_quality_result(settings)


_LIVE_DIRTY_HANDLERS = (
    (bpy.app.handlers.frame_change_post, _mark_live_pose_dirty),
    (bpy.app.handlers.depsgraph_update_post, _mark_live_pose_dirty),
    (bpy.app.handlers.undo_post, _mark_live_pose_dirty),
    (bpy.app.handlers.redo_post, _mark_live_pose_dirty),
)


def _install_live_handlers() -> None:
    for handlers, callback in _LIVE_DIRTY_HANDLERS:
        if callback not in handlers:
            handlers.append(callback)


def _remove_live_handlers() -> None:
    for handlers, callback in _LIVE_DIRTY_HANDLERS:
        while callback in handlers:
            handlers.remove(callback)


def _armature_object_poll(
    _settings: bpy.types.PropertyGroup,
    obj: bpy.types.Object,
) -> bool:
    return obj.type == "ARMATURE"


def _resolve_canonical_armature(
    context: bpy.types.Context,
) -> bpy.types.Object | None:
    active = context.active_object
    if (
        active is not None
        and active.type == "ARMATURE"
        and active.get("motion_bridge_skeleton_id") == SKELETON_ID
    ):
        return active
    canonical_armatures = [
        obj
        for obj in context.scene.objects
        if obj.type == "ARMATURE"
        and obj.get("motion_bridge_skeleton_id") == SKELETON_ID
    ]
    return canonical_armatures[0] if len(canonical_armatures) == 1 else None


def _canonical_armatures(context: bpy.types.Context) -> tuple[bpy.types.Object, ...]:
    return tuple(
        obj
        for obj in context.scene.objects
        if obj.type == "ARMATURE"
        and obj.get("motion_bridge_skeleton_id") == SKELETON_ID
    )


_MOTION_QUALITY_ISSUE_LABELS = {
    "NON_FINITE": "計算できない値",
    "QUATERNION_NORM": "回転データの乱れ",
    "QUATERNION_UNIT": "回転データの乱れ",
    "QUATERNION_NOT_UNIT": "回転データの乱れ",
    "QUATERNION_FLIP": "回転のつながり",
    "QUATERNION_SIGN_FLIP": "回転のつながり",
    "ANGULAR_SPEED": "急すぎる回転",
    "BONE_ANGULAR_SPEED_SPIKE": "急すぎる回転",
    "ANGULAR_ACCELERATION": "回転の急変",
    "BONE_ANGULAR_ACCELERATION_SPIKE": "回転の急変",
    "ROOT_JUMP": "体全体の位置飛び",
    "ROOT_TRANSLATION_JUMP": "体全体の位置飛び",
    "ROOT_TRANSLATION_DISCONTINUITY": "体全体の位置飛び",
    "ROOT_ROTATION_DISCONTINUITY": "体全体の回転飛び",
    "CONTACT_SLIDE": "足滑り",
    "FOOT_SLIDE": "足滑り",
    "FOOT_SLIDE_DURING_CONTACT": "足滑り",
    "GROUND_PENETRATION": "足が地面に入り込む",
    "GROUND_PENETRATION_DURING_CONTACT": "足が地面に入り込む",
    "PENETRATION": "足が地面に入り込む",
    "REACH": "手足の伸びすぎ",
    "LIMB_REACH_EXCEEDED": "手足の伸びすぎ",
    "KEY_DENSITY": "キーが多い",
    "KEY_DENSITY_HIGH": "キーが多い",
}
_MOTION_QUALITY_SUBJECT_LABELS = {
    "Spine": "胴体",
    "Chest": "胴体",
    "UpperChest": "胴体",
    "Neck": "首",
    "Head": "頭",
    "LeftShoulder": "左肩",
    "LeftUpperArm": "左腕",
    "LeftLowerArm": "左腕",
    "RightShoulder": "右肩",
    "RightUpperArm": "右腕",
    "RightLowerArm": "右腕",
    "LeftFoot": "左足",
    "RightFoot": "右足",
    "LeftHand": "左手",
    "RightHand": "右手",
    "LeftUpperLeg": "左脚",
    "LeftLowerLeg": "左脚",
    "LeftToes": "左足",
    "RightUpperLeg": "右脚",
    "RightLowerLeg": "右脚",
    "RightToes": "右足",
    "Hips": "腰",
    "Root": "体全体",
    "root": "体全体",
    "Action": "モーション",
}
_MOTION_QUALITY_METRIC_LABELS = {
    "bone_angular_acceleration": "回転の急変",
    "bone_angular_speed": "回転速度",
    "foot_slide": "接地中の足滑り",
    "ground_penetration": "地面への入り込み",
    "key_density": "キー密度",
    "limb_reach": "手足の到達率",
    "quaternion_sign_flip": "回転符号の反転",
    "quaternion_unit_error": "回転の正規化誤差",
    "root_rotation_discontinuity": "体全体の回転飛び",
    "root_translation_discontinuity": "体全体の位置飛び",
}


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _motion_quality_issue_title(code: object) -> str:
    normalized = _enum_value(code).strip().upper().replace("-", "_")
    direct = _MOTION_QUALITY_ISSUE_LABELS.get(normalized)
    if direct is not None:
        return direct
    for marker, label in _MOTION_QUALITY_ISSUE_LABELS.items():
        if marker in normalized:
            return label
    return "動きを目で確認"


def _motion_quality_issue_subject(subject: object) -> str:
    value = _enum_value(subject).strip()
    if value.startswith("Bone:"):
        value = value.split(":", 2)[1]
    elif value.startswith("Effector:"):
        value = value.split(":", 1)[1]
    return _MOTION_QUALITY_SUBJECT_LABELS.get(value, "モーション")


def _motion_quality_issue_preview(report: object) -> str:
    rows: list[dict[str, object]] = []
    for issue in tuple(getattr(report, "issues", ()))[:12]:
        interval = getattr(issue, "interval", None)
        start = getattr(
            interval,
            "start_frame",
            getattr(
                interval,
                "frame_start",
                getattr(interval, "start", getattr(issue, "frame", 0)),
            ),
        )
        end = getattr(
            interval,
            "end_frame",
            getattr(interval, "frame_end", getattr(interval, "end", start)),
        )
        try:
            frame_start = math.floor(float(start))
            frame_end = math.ceil(float(end))
        except (TypeError, ValueError, OverflowError):
            continue
        if frame_end < frame_start:
            frame_start, frame_end = frame_end, frame_start
        rows.append(
            {
                "code": _enum_value(getattr(issue, "code", "")),
                "frameEnd": frame_end,
                "frameStart": frame_start,
                "severity": _enum_value(getattr(issue, "severity", "REVIEW")),
                "subject": _motion_quality_issue_subject(
                    getattr(issue, "subject", "")
                ),
                "title": _motion_quality_issue_title(getattr(issue, "code", "")),
            }
        )
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def _motion_quality_pointer(value: object | None) -> int:
    if value is None:
        return 0
    return int(value.as_pointer())


def _motion_quality_float(value: object) -> str:
    return float(value).hex()


def _motion_quality_strip_fingerprint(strip: bpy.types.NlaStrip) -> tuple[object, ...]:
    """Return the cheap evaluation state of one NLA strip.

    Action curve contents are covered by Motion Bridge's revision/history
    invalidation.  This fingerprint handles Blender RNA controls that users can
    edit directly in the simple layer card, especially strip influence.
    """

    action_slot = getattr(strip, "action_slot", None)
    action_slot_handle = getattr(
        action_slot,
        "handle",
        getattr(strip, "action_slot_handle", None),
    )
    return (
        _motion_quality_pointer(strip),
        _motion_quality_pointer(getattr(strip, "action", None)),
        action_slot_handle,
        bool(strip.mute),
        str(strip.blend_type),
        str(strip.extrapolation),
        _motion_quality_float(strip.influence),
        _motion_quality_float(strip.frame_start),
        _motion_quality_float(strip.frame_end),
        _motion_quality_float(strip.action_frame_start),
        _motion_quality_float(strip.action_frame_end),
        _motion_quality_float(strip.scale),
        _motion_quality_float(strip.repeat),
        bool(strip.use_animated_influence),
        bool(strip.use_animated_time),
        bool(getattr(strip, "use_auto_blend", False)),
        bool(getattr(strip, "use_reverse", False)),
        bool(getattr(strip, "use_sync_length", False)),
        _motion_quality_float(getattr(strip, "blend_in", 0.0)),
        _motion_quality_float(getattr(strip, "blend_out", 0.0)),
        _motion_quality_float(getattr(strip, "strip_time", 0.0)),
    )


def _motion_quality_track_fingerprint(
    track: bpy.types.NlaTrack,
    strip: bpy.types.NlaStrip,
) -> tuple[object, ...]:
    return (
        _motion_quality_pointer(track),
        bool(track.mute),
        bool(getattr(track, "is_solo", False)),
        _motion_quality_strip_fingerprint(strip),
    )


def _motion_quality_evaluation_fingerprint(
    armature: bpy.types.Object | None,
) -> str:
    """Hash the lightweight Blender state that selects/evaluates motion."""

    if armature is None or armature.type != "ARMATURE":
        return ""
    try:
        animation_data = armature.animation_data
        if animation_data is None:
            return ""
        active_slot = getattr(animation_data, "action_slot", None)
        active_slot_handle = getattr(
            active_slot,
            "handle",
            getattr(animation_data, "action_slot_handle", None),
        )
        payload: tuple[object, ...] = (
            str(armature.get("motion_bridge_stack_id", "")),
            _motion_quality_pointer(animation_data.action),
            active_slot_handle,
            bool(animation_data.use_nla),
            bool(animation_data.use_tweak_mode),
            str(animation_data.action_blend_type),
            str(animation_data.action_extrapolation),
            _motion_quality_float(animation_data.action_influence),
        )
        stack = get_correction_layer_stack(armature)
        if stack is not None:
            payload += (
                _motion_quality_track_fingerprint(
                    stack.base_track,
                    stack.base_strip,
                ),
                tuple(
                    (
                        layer.layer_id,
                        _motion_quality_track_fingerprint(layer.track, layer.strip),
                    )
                    for layer in stack.correction_layers
                ),
            )
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return ""
    return hashlib.sha256(encoded).hexdigest()


def _clear_motion_quality_result(settings: bpy.types.PropertyGroup) -> None:
    settings.motion_quality_summary = "NOT_RUN"
    settings.motion_quality_report_json = ""
    settings.motion_quality_issue_preview_json = ""
    settings.motion_quality_issue_count = 0
    settings.motion_quality_target = None
    settings.motion_quality_stack_id = ""
    settings.motion_quality_evaluation_fingerprint = ""
    settings.motion_quality_range_source = ""
    settings.motion_quality_frame_start = 0
    settings.motion_quality_frame_end = -1
    settings.motion_quality_sample_count = 0
    settings.motion_quality_fps = 0
    settings.motion_quality_fps_base = 0.0
    settings.motion_quality_motion_revision = -1


def _invalidate_motion_quality_result(context: bpy.types.Context) -> None:
    settings = getattr(context.scene, "motion_bridge_session", None)
    if settings is not None:
        _clear_motion_quality_result(settings)


def _update_motion_quality_setting(
    settings: bpy.types.PropertyGroup,
    _context: object,
) -> None:
    if _motion_quality_scan_state is not None:
        _motion_quality_scan_state.input_changed = True
    _clear_motion_quality_result(settings)


def _store_motion_quality_result(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    report: object,
    *,
    range_spec: RangeSpec,
) -> None:
    settings = context.scene.motion_bridge_session
    summary = _enum_value(getattr(report, "summary", "NOT_RUN")).upper()
    if summary not in {"NO_ISSUES", "REVIEW_RECOMMENDED", "FIX_RECOMMENDED"}:
        raise ValueError(f"Unknown motion quality summary: {summary}")
    canonical_json = getattr(report, "canonical_json", None)
    if not callable(canonical_json):
        raise TypeError("Motion quality report does not provide canonical JSON")
    settings.motion_quality_summary = summary
    settings.motion_quality_report_json = canonical_json()
    settings.motion_quality_issue_preview_json = _motion_quality_issue_preview(report)
    settings.motion_quality_issue_count = len(tuple(getattr(report, "issues", ())))
    settings.motion_quality_target = armature
    settings.motion_quality_stack_id = str(
        armature.get("motion_bridge_stack_id", "")
    )
    evaluation_fingerprint = _motion_quality_evaluation_fingerprint(armature)
    if not evaluation_fingerprint:
        raise RuntimeError("モーションの評価状態を確認できませんでした")
    settings.motion_quality_evaluation_fingerprint = evaluation_fingerprint
    frame_start, frame_end = range_spec.expanded_integer_bounds()
    settings.motion_quality_frame_start = frame_start
    settings.motion_quality_frame_end = frame_end
    settings.motion_quality_range_source = range_spec.source.value
    settings.motion_quality_sample_count = int(getattr(report, "sample_count", 0))
    settings.motion_quality_fps = int(context.scene.render.fps)
    settings.motion_quality_fps_base = float(context.scene.render.fps_base)
    settings.motion_quality_motion_revision = int(
        armature.get(MOTION_REVISION_PROPERTY, 0)
    )


def _motion_quality_result_is_motion_current(
    context: bpy.types.Context,
    armature: bpy.types.Object | None,
) -> bool:
    if armature is None:
        return False
    settings = context.scene.motion_bridge_session
    return bool(
        settings.motion_quality_summary != "NOT_RUN"
        and settings.motion_quality_report_json
        and settings.motion_quality_target is armature
        and settings.motion_quality_stack_id
        == str(armature.get("motion_bridge_stack_id", ""))
        and bool(settings.motion_quality_evaluation_fingerprint)
        and settings.motion_quality_evaluation_fingerprint
        == _motion_quality_evaluation_fingerprint(armature)
        and int(settings.motion_quality_fps) == int(context.scene.render.fps)
        and float(settings.motion_quality_fps_base)
        == float(context.scene.render.fps_base)
        and int(settings.motion_quality_motion_revision)
        == int(armature.get(MOTION_REVISION_PROPERTY, 0))
    )


def _motion_quality_result_is_current(
    context: bpy.types.Context,
    armature: bpy.types.Object | None,
) -> bool:
    """Return whether both the motion inputs and requested scan range match."""

    if not _motion_quality_result_is_motion_current(context, armature):
        return False
    assert armature is not None
    settings = context.scene.motion_bridge_session
    try:
        current_range = _motion_quality_range_spec(context, armature)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return False
    frame_start, frame_end = current_range.expanded_integer_bounds()
    return bool(
        int(settings.motion_quality_frame_start) == frame_start
        and int(settings.motion_quality_frame_end) == frame_end
        and settings.motion_quality_range_source == current_range.source.value
    )


def _motion_quality_dependency_pointers(
    armature: bpy.types.Object,
) -> set[int]:
    """Collect Blender IDs whose edits can change the evaluated motion."""

    pointers = {int(armature.as_pointer())}
    armature_data = getattr(armature, "data", None)
    if armature_data is not None:
        pointers.add(int(armature_data.as_pointer()))

    animation_data = armature.animation_data
    if animation_data is not None:
        action = animation_data.action
        if action is not None:
            pointers.add(int(action.as_pointer()))

        def add_strip_actions(strips: object) -> None:
            for strip in strips:
                strip_action = getattr(strip, "action", None)
                if strip_action is not None:
                    pointers.add(int(strip_action.as_pointer()))
                nested = getattr(strip, "strips", None)
                if nested is not None:
                    add_strip_actions(nested)

        for track in animation_data.nla_tracks:
            add_strip_actions(track.strips)

    constraints = list(getattr(armature, "constraints", ()))
    for pose_bone in getattr(getattr(armature, "pose", None), "bones", ()):
        constraints.extend(pose_bone.constraints)
    for constraint in constraints:
        for attribute in ("target", "pole_target", "space_object"):
            dependency = getattr(constraint, attribute, None)
            if dependency is not None and hasattr(dependency, "as_pointer"):
                pointers.add(int(dependency.as_pointer()))
    return pointers


@persistent
def _handle_motion_quality_frame_change_pre(*_args: object) -> None:
    state = _motion_quality_scan_state
    if state is not None and not state.internal_update:
        state.input_changed = True


@persistent
def _handle_motion_quality_frame_change_post(*_args: object) -> None:
    state = _motion_quality_scan_state
    if state is not None and not state.internal_update:
        state.input_changed = True


@persistent
def _handle_motion_quality_depsgraph_update(
    _scene: bpy.types.Scene,
    depsgraph: bpy.types.Depsgraph,
) -> None:
    state = _motion_quality_scan_state
    if state is None or state.internal_update:
        return
    changed_pointers: set[int] = set()
    for update in depsgraph.updates:
        changed_id = getattr(update.id, "original", update.id)
        try:
            changed_pointers.add(int(changed_id.as_pointer()))
        except (AttributeError, ReferenceError, RuntimeError):
            continue
    if not changed_pointers:
        return
    if changed_pointers.intersection(state.dependency_pointers):
        state.input_changed = True


def _scene_manual_mapping_changes_locked(scene: bpy.types.Scene) -> bool:
    """Keep source/mapping edits out of an already-corrected workfile."""

    return any(
        obj.type == "ARMATURE"
        and obj.get("motion_bridge_skeleton_id") == SKELETON_ID
        and bool(obj.get("motion_bridge_stack_id"))
        for obj in scene.objects
    )


def _manual_mapping_changes_locked(context: bpy.types.Context) -> bool:
    return _scene_manual_mapping_changes_locked(context.scene)


def _apply_rig_visual_settings(
    context: bpy.types.Context,
) -> RigVisualizationResult | None:
    settings = getattr(context.scene, "motion_bridge_session", None)
    if settings is None:
        return None
    canonical = _resolve_canonical_armature(context)
    if canonical is None:
        return None
    return apply_rig_visualization(
        context,
        canonical,
        source_armature=settings.source_armature,
        canonical_style=settings.rig_visual_style,
        source_visible=settings.rig_visual_source_visible,
        wire_opacity=settings.rig_visual_wire_opacity,
        show_names=settings.rig_visual_show_names,
    )


def _update_rig_visualization(_settings: object, context: object) -> None:
    global _rig_visual_last_error
    if context is None or getattr(context, "scene", None) is None:
        return
    try:
        _apply_rig_visual_settings(context)
    except (RuntimeError, TypeError, ValueError) as exception:
        _rig_visual_last_error = str(exception)
    else:
        _rig_visual_last_error = ""


def _manual_profile_values(profile: bpy.types.PropertyGroup) -> dict[str, str]:
    return {
        field.property_name: getattr(profile, field.property_name)
        for field in HUMANOID_MAPPING_FIELDS
    }


def _manual_humanoid_mapping(
    settings: bpy.types.PropertyGroup,
    *,
    bind_current: bool = False,
    verify_content: bool = True,
) -> dict[str, str]:
    mapping = mapping_from_profile_values(
        _manual_profile_values(settings.manual_mapping)
    )
    source = getattr(settings, "source_armature", None)
    if source is None:
        raise ValueError("手動ボーン対応を使う元モーションリグがありません")
    encoded_profile = str(settings.manual_mapping_profile_json)
    try:
        profile = SourceMappingProfile.from_json(encoded_profile)
    except SourceMappingProfileError as error:
        if bind_current and not encoded_profile and mapping:
            # Legacy RNA fields are accepted for this explicit Bake attempt,
            # but are persisted only after the complete bake succeeds.
            return mapping
        raise ValueError(
            "手動ボーン対応が現在の元モーションと一致しません。"
            "「自動認識結果を反映」を押し直してください"
        ) from error
    try:
        profile.require_source(
            **_source_mapping_identity(
                source,
                create=False,
                verify_content=verify_content,
            )
        )
    except (SourceMappingProfileError, StaleSourceMappingProfileError) as error:
        raise ValueError(
            "手動ボーン対応が現在の元モーションと一致しません。"
            "「自動認識結果を反映」を押し直してください"
        ) from error
    if profile.mapping != mapping:
        if not bind_current:
            raise ValueError("手動ボーン対応の保存情報を更新してください")
    return mapping


def _set_manual_humanoid_mapping(
    settings: bpy.types.PropertyGroup,
    mapping: dict[str, str],
) -> None:
    key = _manual_mapping_settings_key(settings)
    _MANUAL_MAPPING_UPDATE_GUARD.add(key)
    try:
        profile = settings.manual_mapping
        for property_name, source_name in profile_values_from_mapping(mapping).items():
            setattr(profile, property_name, source_name)
    finally:
        _MANUAL_MAPPING_UPDATE_GUARD.discard(key)


def _capture_source_import_session(
    context: bpy.types.Context,
    settings: bpy.types.PropertyGroup,
) -> _SourceImportSessionState:
    source_armature = settings.source_armature
    bound_source = settings.manual_mapping_bound_source
    source_properties: list[
        tuple[bpy.types.Object, tuple[tuple[str, bool, object], ...]]
    ] = []
    seen: set[int] = set()
    for source in (source_armature, bound_source):
        if source is None:
            continue
        pointer = source.as_pointer()
        if pointer in seen:
            continue
        seen.add(pointer)
        properties = tuple(
            (name, name in source, source.get(name))
            for name in (
                SOURCE_INSTANCE_ID_PROPERTY,
                _SOURCE_MAPPING_INSTANCE_ID_PROPERTY,
                _SOURCE_MAPPING_ACTION_CHECKSUM_PROPERTY,
            )
        )
        source_properties.append((source, properties))
    canonical = _resolve_canonical_armature(context)
    return _SourceImportSessionState(
        source_armature=source_armature,
        manual_mapping_bound_source=bound_source,
        manual_mapping_profile_json=str(settings.manual_mapping_profile_json),
        manual_mapping_profiles_json=str(settings.manual_mapping_profiles_json),
        manual_mapping=_manual_profile_values(settings.manual_mapping),
        retarget_preset=str(settings.retarget_preset),
        beginner_retarget_error=str(settings.beginner_retarget_error),
        source_properties=tuple(source_properties),
        rig_visual_canonical=canonical,
        rig_visual_was_applied=has_rig_visualization_snapshot(canonical),
    )


def _restore_source_import_session(
    context: bpy.types.Context,
    settings: bpy.types.PropertyGroup,
    state: _SourceImportSessionState,
) -> None:
    """Undo Session/update-callback changes made by a failed Source import."""

    for source, properties in state.source_properties:
        if not any(candidate is source for candidate in bpy.data.objects):
            continue
        for name, existed, value in properties:
            if existed:
                source[name] = value
            elif name in source:
                del source[name]

    key = _manual_mapping_settings_key(settings)
    _MANUAL_MAPPING_UPDATE_GUARD.add(key)
    try:
        settings.source_armature = state.source_armature
        settings.manual_mapping_bound_source = state.manual_mapping_bound_source
        settings.manual_mapping_profile_json = state.manual_mapping_profile_json
        settings.manual_mapping_profiles_json = state.manual_mapping_profiles_json
        for property_name, value in state.manual_mapping.items():
            setattr(settings.manual_mapping, property_name, value)
        settings.retarget_preset = state.retarget_preset
        settings.beginner_retarget_error = state.beginner_retarget_error
    finally:
        _MANUAL_MAPPING_UPDATE_GUARD.discard(key)

    canonical = state.rig_visual_canonical
    if canonical is None or not any(
        candidate is canonical for candidate in bpy.data.objects
    ):
        return
    if state.rig_visual_was_applied:
        apply_rig_visualization(
            context,
            canonical,
            source_armature=state.source_armature,
            canonical_style=settings.rig_visual_style,
            source_visible=settings.rig_visual_source_visible,
            wire_opacity=settings.rig_visual_wire_opacity,
            show_names=settings.rig_visual_show_names,
        )
    elif has_rig_visualization_snapshot(canonical):
        restore_rig_visualization(canonical)


def _armature_source_fingerprints(
    source: bpy.types.Object,
) -> tuple[str, str]:
    hierarchy = bone_hierarchy_fingerprint(
        (
            bone.name,
            None if bone.parent is None else bone.parent.name,
        )
        for bone in source.data.bones
    )
    bind_pose = bone_bind_pose_fingerprint(
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
        for bone in source.data.bones
    )
    return hierarchy, bind_pose


def _source_mapping_identity(
    source: bpy.types.Object,
    *,
    create: bool,
    verify_content: bool = True,
) -> dict[str, str]:
    if source.type != "ARMATURE":
        raise ValueError("元モーションリグはArmatureである必要があります")
    hierarchy, bind_pose = _armature_source_fingerprints(source)

    imported_source = (
        source.get(SOURCE_PROVENANCE_SCHEMA_PROPERTY)
        == SOURCE_PROVENANCE_SCHEMA_VERSION
    )
    instance_property = (
        SOURCE_INSTANCE_ID_PROPERTY
        if imported_source
        else _SOURCE_MAPPING_INSTANCE_ID_PROPERTY
    )
    instance_id = source.get(instance_property)
    pending_properties: dict[str, str] = {}
    if not instance_id:
        if not create:
            raise ValueError("元モーションに手動ボーン対応用の識別情報がありません")
        instance_id = str(uuid.uuid4())
        pending_properties[instance_property] = instance_id

    archive_id = source.get("motion_bridge_source_archive_id")
    action_name = source.get("motion_bridge_source_action")
    slot_identifier = source.get(SOURCE_ACTION_SLOT_PROPERTY)
    if not archive_id or not action_name or not isinstance(slot_identifier, str):
        animation_data = source.animation_data
        action = animation_data.action if animation_data is not None else None
        if action is None:
            raise ValueError(
                "元モーションのActionを確認できないため手動ボーン対応を保存できません"
            )
        stored_checksum = source.get(_SOURCE_MAPPING_ACTION_CHECKSUM_PROPERTY)
        if not isinstance(stored_checksum, str) or len(stored_checksum) != 64:
            if not create:
                raise ValueError(
                    "元モーションに手動ボーン対応用のAction識別情報がありません"
                )
            stored_checksum = action_checksum(action)
            pending_properties[_SOURCE_MAPPING_ACTION_CHECKSUM_PROPERTY] = (
                stored_checksum
            )
        if verify_content:
            archive_id = action_checksum(action)
            if create:
                pending_properties[_SOURCE_MAPPING_ACTION_CHECKSUM_PROPERTY] = archive_id
        else:
            archive_id = stored_checksum
        action_name = action.name
        slot = action_slot_for_id(action, source)
        slots = getattr(action, "slots", None)
        if slots is not None and len(slots) and slot is None:
            raise ValueError("元モーションのAction Slotを一意に特定できません")
        slot_identifier = str(getattr(slot, "identifier", ""))

    identity = {
        "source_instance_id": instance_id,
        "source_archive_id": archive_id,
        "source_hierarchy_sha256": hierarchy,
        "source_bind_pose_sha256": bind_pose,
        "source_action_name": action_name,
        "source_action_slot_identifier": slot_identifier,
    }
    # Validate the complete identity before committing either hidden property.
    # Populate must not leave a half-bound Source when an Action/Slot is absent
    # or existing provenance is malformed.
    SourceMappingProfile.create(mapping={}, **identity)
    property_state = tuple(
        (key, key in source, source.get(key)) for key in pending_properties
    )
    try:
        for key, value in pending_properties.items():
            source[key] = value
    except Exception:
        for key, existed, value in reversed(property_state):
            if existed:
                source[key] = value
            elif key in source:
                del source[key]
        raise
    return identity


def _manual_mapping_profile_json(
    settings: bpy.types.PropertyGroup,
    mapping: dict[str, str],
    *,
    source: bpy.types.Object | None = None,
    verify_content: bool = True,
) -> str:
    if source is None:
        source = settings.source_armature
    if source is None:
        raise ValueError("手動ボーン対応を保存する元モーションリグがありません")
    return SourceMappingProfile.create(
        mapping=mapping,
        **_source_mapping_identity(
            source,
            create=True,
            verify_content=verify_content,
        ),
    ).to_json()


def _manual_mapping_settings_key(settings: object) -> int:
    owner = getattr(settings, "id_data", None)
    try:
        return int(owner.as_pointer()) if owner is not None else id(settings)
    except (AttributeError, ReferenceError, TypeError):
        return id(settings)


def _manual_mapping_registry(settings: object) -> dict[str, str]:
    raw_registry = str(getattr(settings, "manual_mapping_profiles_json", ""))
    if not raw_registry:
        return {}
    try:
        payload = json.loads(raw_registry)
    except json.JSONDecodeError as error:
        raise SourceMappingProfileError(
            "Manual mapping profile registry is not valid JSON"
        ) from error
    if (
        not isinstance(payload, dict)
        or type(payload.get("schemaVersion")) is not int
        or payload.get("schemaVersion") != 1
    ):
        raise SourceMappingProfileError(
            "Manual mapping profile registry schema is unsupported"
        )
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict):
        raise SourceMappingProfileError(
            "Manual mapping profile registry entries are malformed"
        )
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in profiles.items()
    ):
        raise SourceMappingProfileError(
            "Manual mapping profile registry entries must be strings"
        )
    return dict(profiles)


def _write_manual_mapping_registry(
    settings: object,
    profiles: dict[str, str],
) -> None:
    settings.manual_mapping_profiles_json = json.dumps(
        {"profiles": profiles, "schemaVersion": 1},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _profile_with_mapping(
    profile: SourceMappingProfile,
    mapping: dict[str, str],
) -> SourceMappingProfile:
    return SourceMappingProfile.create(
        mapping=mapping,
        source_instance_id=profile.source_instance_id,
        source_archive_id=profile.source_archive_id,
        source_hierarchy_sha256=profile.source_hierarchy_sha256,
        source_bind_pose_sha256=profile.source_bind_pose_sha256,
        source_action_name=profile.source_action_name,
        source_action_slot_identifier=profile.source_action_slot_identifier,
    )


def _save_selected_manual_mapping_profile(
    settings: object,
    *,
    legacy_source: bpy.types.Object | None = None,
) -> None:
    raw_profile = str(getattr(settings, "manual_mapping_profile_json", ""))
    mapping = mapping_from_profile_values(
        _manual_profile_values(settings.manual_mapping)
    )
    try:
        profiles = _manual_mapping_registry(settings)
        if raw_profile:
            profile = SourceMappingProfile.from_json(raw_profile)
            profile = _profile_with_mapping(profile, mapping)
            encoded = profile.to_json()
        elif legacy_source is not None and mapping:
            encoded = _manual_mapping_profile_json(
                settings,
                mapping,
                source=legacy_source,
                verify_content=False,
            )
            profile = SourceMappingProfile.from_json(encoded)
        else:
            return
        profiles[profile.source_instance_id] = encoded
        settings.manual_mapping_profile_json = encoded
        _write_manual_mapping_registry(settings, profiles)
    except (AttributeError, SourceMappingProfileError, TypeError, ValueError):
        return


def _store_manual_mapping_profile(
    settings: object,
    mapping: dict[str, str],
    *,
    verify_content: bool = True,
) -> SourceMappingProfile:
    encoded = _manual_mapping_profile_json(
        settings,
        mapping,
        verify_content=verify_content,
    )
    profile = SourceMappingProfile.from_json(encoded)
    profiles = _manual_mapping_registry(settings)
    profiles[profile.source_instance_id] = encoded
    settings.manual_mapping_profile_json = encoded
    _write_manual_mapping_registry(settings, profiles)
    return profile


def _update_manual_mapping_value(_profile: object, _context: object) -> None:
    owner_scene = getattr(_profile, "id_data", None)
    settings = getattr(owner_scene, "motion_bridge_session", None)
    if settings is None:
        return
    if _manual_mapping_settings_key(settings) in _MANUAL_MAPPING_UPDATE_GUARD:
        return
    if isinstance(owner_scene, bpy.types.Scene) and _scene_manual_mapping_changes_locked(
        owner_scene
    ):
        try:
            saved = SourceMappingProfile.from_json(
                settings.manual_mapping_profile_json
            ).mapping
        except (AttributeError, SourceMappingProfileError, TypeError, ValueError):
            saved = {}
        _set_manual_humanoid_mapping(settings, saved)
        return
    if getattr(settings, "source_armature", None) is None:
        return
    mapping = mapping_from_profile_values(
        _manual_profile_values(settings.manual_mapping)
    )
    try:
        encoded_profile = str(settings.manual_mapping_profile_json)
        if encoded_profile:
            saved_profile = SourceMappingProfile.from_json(encoded_profile)
            saved_profile.require_source(
                **_source_mapping_identity(
                    settings.source_armature,
                    create=False,
                    verify_content=True,
                )
            )
        _store_manual_mapping_profile(
            settings,
            mapping,
            verify_content=True,
        )
    except (
        AttributeError,
        RuntimeError,
        SourceMappingProfileError,
        TypeError,
        ValueError,
    ):
        return


def _update_source_armature(settings: object, context: object) -> None:
    """Switch to this Source's own mapping without reusing another Source."""

    owner_scene = getattr(settings, "id_data", None)
    settings_key = _manual_mapping_settings_key(settings)
    if settings_key in _MANUAL_MAPPING_UPDATE_GUARD:
        return
    if isinstance(owner_scene, bpy.types.Scene) and _scene_manual_mapping_changes_locked(
        owner_scene
    ):
        _MANUAL_MAPPING_UPDATE_GUARD.add(settings_key)
        try:
            settings.source_armature = settings.manual_mapping_bound_source
        finally:
            _MANUAL_MAPPING_UPDATE_GUARD.discard(settings_key)
        return

    source = getattr(settings, "source_armature", None)
    previous_source = getattr(settings, "manual_mapping_bound_source", None)
    current_profile = str(getattr(settings, "manual_mapping_profile_json", ""))
    settings.beginner_retarget_error = ""
    try:
        _manual_mapping_registry(settings)
        if current_profile:
            SourceMappingProfile.from_json(current_profile)
    except SourceMappingProfileError:
        # A future add-on version may own this data. Keep its bytes intact and
        # expose no mapping until that version can interpret it safely.
        _set_manual_humanoid_mapping(settings, {})
        settings.manual_mapping_bound_source = source
        _update_rig_visualization(settings, context)
        return

    _save_selected_manual_mapping_profile(
        settings,
        legacy_source=previous_source,
    )
    mapping: dict[str, str] = {}
    encoded = ""
    if source is not None:
        try:
            identity = _source_mapping_identity(
                source,
                create=True,
                verify_content=False,
            )
            candidate = _manual_mapping_registry(settings).get(
                identity["source_instance_id"]
            )
            if candidate:
                profile = SourceMappingProfile.from_json(candidate)
                profile.require_source(**identity)
                mapping = profile.mapping
                encoded = profile.to_json()
            else:
                encoded = _manual_mapping_profile_json(
                    settings,
                    {},
                    verify_content=False,
                )
        except (RuntimeError, TypeError, ValueError):
            # Bake/Populate reports a user-facing recovery if the Source has no
            # stable Action identity. Merely choosing it must stay reversible.
            encoded = ""
    _set_manual_humanoid_mapping(settings, mapping)
    settings.manual_mapping_profile_json = encoded
    if encoded:
        profile = SourceMappingProfile.from_json(encoded)
        profiles = _manual_mapping_registry(settings)
        profiles[profile.source_instance_id] = encoded
        _write_manual_mapping_registry(settings, profiles)
    settings.manual_mapping_bound_source = source
    _update_rig_visualization(settings, context)


def _initialize_manual_mapping_source_bindings() -> None:
    """Remember the Source owning legacy RNA fields without rewriting them."""

    for scene in bpy.data.scenes:
        settings = getattr(scene, "motion_bridge_session", None)
        if settings is None:
            continue
        if getattr(settings, "manual_mapping_bound_source", None) is None:
            settings.manual_mapping_bound_source = settings.source_armature


def _initialize_manual_mapping_source_bindings_timer() -> float | None:
    """Defer scene access while Blender is registering an extension at startup."""

    try:
        _initialize_manual_mapping_source_bindings()
    except AttributeError:
        # bpy.data is _RestrictData while Blender is still loading preferences.
        return 0.1
    return None


def _close_live_transport(*, send_goodbye: bool) -> None:
    global _live_session, _live_scheduler, _control_client
    if _live_session is not None:
        _live_session.close()
        _live_session = None
    _live_scheduler = None
    if _control_client is not None:
        _control_client.close(send_goodbye=send_goodbye)
        _control_client = None


def _schedule_live_reconnect(reason: object, now: float) -> float:
    global _last_live_error
    _close_live_transport(send_goodbye=False)
    if _pending_control_config is None or _pending_live_armature is None:
        return 0.0
    delay = _live_reconnect_backoff.schedule(now)
    _last_live_error = f"Reconnecting in {delay:.2f}s — {reason}"
    return delay


def _live_descriptor(armature: bpy.types.Object) -> dict[str, object]:
    descriptor = descriptor_from_armature(armature)
    actual_hash = str(descriptor["skeletonHash"])
    if actual_hash != SKELETON_HASH:
        raise ValueError(
            "Canonicalリグの構造が配布版と一致しません。"
            "同じ配布セットのCanonicalリグを開き直してください "
            f"(expected={SKELETON_HASH[:8]}, actual={actual_hash[:8]})"
        )
    return descriptor


def _start_live_control_connection(now: float) -> float:
    global _control_client, _pending_control_config
    config = _pending_control_config
    if config is None:
        return 0.0
    armature = _pending_live_armature
    if armature is None:
        return 0.0
    descriptor = _live_descriptor(armature)
    if descriptor["skeletonHash"] != config.descriptor["skeletonHash"]:
        config = replace(config, descriptor=descriptor)
        _pending_control_config = config
    client = BlenderControlClient(config)
    _control_client = client
    try:
        client.connect()
    except Exception as exception:
        if is_retryable_control_error(exception):
            return _schedule_live_reconnect(exception, now)
        _control_client = None
        client.close(send_goodbye=False)
        raise
    return 0.0


def _stop_live_with_error(error: object) -> None:
    global _last_live_error
    message = str(error)
    _stop_live_session()
    _last_live_error = message


def _live_timer() -> float | None:
    global _live_session, _live_scheduler, _last_live_error
    if _pending_control_config is None or _pending_live_armature is None:
        return None
    if _motion_quality_scan_state is not None:
        # Range sampling temporarily moves the Scene frame.  Keep Unity on its
        # last user-visible pose, then send the restored pose when scanning ends.
        return _LIVE_TIMER_POLL_SECONDS
    now = time.monotonic()
    client = _control_client
    if client is None:
        wait_seconds = _live_reconnect_backoff.seconds_until_retry(now)
        if wait_seconds > 0.0:
            return max(0.01, wait_seconds)
        try:
            scheduled_delay = _start_live_control_connection(now)
        except Exception as exception:  # Blender timer boundary must not leak.
            _stop_live_with_error(exception)
            return None
        if scheduled_delay > 0.0:
            return scheduled_delay
        client = _control_client
        if client is None:
            return _LIVE_TIMER_POLL_SECONDS
    try:
        state = client.poll()
        if state is ControlSessionState.ERROR:
            message = client.last_error or "TCP control session failed"
            if client.failure_is_retryable:
                return _schedule_live_reconnect(message, now)
            raise RuntimeError(message)
        if state is ControlSessionState.READY and _live_session is None:
            endpoint = client.udp_endpoint
            armature = _pending_live_armature
            send_hz = client.accepted_pose_hz
            if endpoint is None or armature is None or send_hz is None:
                raise RuntimeError(
                    "TCP control session became Ready without pose settings"
                )
            _live_session = LivePoseSession(
                armature,
                endpoint[0],
                endpoint[1],
                client.session_id,
                _pending_root_mode,
                send_hz=send_hz,
                initial_sequence=0,
            )
            _live_scheduler = PoseSendScheduler(send_hz)
            client.start_streaming()
            state = client.poll()
            if state is ControlSessionState.ERROR:
                message = client.last_error or "TCP control session failed"
                if client.failure_is_retryable:
                    return _schedule_live_reconnect(message, now)
                raise RuntimeError(message)
            _live_reconnect_backoff.reset()
            _last_live_error = ""

        session = _live_session
        scheduler = _live_scheduler
        if session is None or scheduler is None:
            return _LIVE_TIMER_POLL_SECONDS
        if not scheduler.should_send(now):
            return scheduler.send_interval
        session.tick(bpy.context.scene)
        if session.last_send_succeeded:
            scheduler.did_send(now)
        else:
            scheduler.mark_dirty()
    except Exception as exception:  # Blender timer boundary must not leak exceptions.
        if is_retryable_control_error(exception):
            return _schedule_live_reconnect(exception, now)
        _stop_live_with_error(exception)
        return None
    return session.interval_seconds


def _stop_live_session(*, unregister_timer: bool = True) -> None:
    global _pending_live_armature, _pending_control_config
    _close_live_transport(send_goodbye=True)
    _pending_live_armature = None
    _pending_control_config = None
    _live_reconnect_backoff.reset()
    _remove_live_handlers()
    if unregister_timer and bpy.app.timers.is_registered(_live_timer):
        bpy.app.timers.unregister(_live_timer)


class MB_PG_ManualHumanoidMapping(PropertyGroup):
    """Persistent 22-bone HumanBone-to-source-bone mapping."""


MB_PG_ManualHumanoidMapping.__annotations__ = {}
for _mapping_field in HUMANOID_MAPPING_FIELDS:
    MB_PG_ManualHumanoidMapping.__annotations__[_mapping_field.property_name] = (
        StringProperty(
            name=_mapping_field.human_bone,
            description=(f"Source armature bone mapped to {_mapping_field.human_bone}"),
            default="",
            update=_update_manual_mapping_value,
        )
    )


class MB_PG_SessionSettings(PropertyGroup):
    beginner_live_details: BoolProperty(
        name="接続の詳細を表示",
        description="TCP、Session ID、送信頻度、ルート移動の設定を表示します",
        default=False,
    )
    beginner_edit_details: BoolProperty(
        name="修正の詳細を表示",
        description="なじませ、補間、リセット、既存キー調整を表示します",
        default=False,
    )
    beginner_auto_details: BoolProperty(
        name="ワンクリック補正の設定を表示",
        description="揺れ除去の強さとFoot Lockする足を表示します",
        default=False,
    )
    auto_smoothing_strength: EnumProperty(
        name="揺れ除去の強さ",
        description="ワンクリック揺れ除去で細かな回転ノイズをならす強さです",
        items=(
            ("LOW", "弱め", "元の動きを多めに残して細かな揺れだけを減らします"),
            ("STANDARD", "標準", "通常のモーションに適した強さです"),
            ("STRONG", "強め", "大きめの揺れもならします"),
        ),
        default="STANDARD",
    )
    auto_foot_lock_side: EnumProperty(
        name="Foot Lockする足",
        description="接地中の滑りを固定する足を選びます",
        items=(
            ("BOTH", "両足", "左右の接地区間を自動で固定します"),
            ("LEFT", "左足", "左足の接地区間だけを固定します"),
            ("RIGHT", "右足", "右足の接地区間だけを固定します"),
        ),
        default="BOTH",
    )
    motion_quality_summary: EnumProperty(
        name="モーション確認結果",
        description="最後に確認したモーションの要約です",
        items=(
            ("NOT_RUN", "未確認", "モーションはまだ確認されていません"),
            ("NO_ISSUES", "問題なし", "問題は見つかりませんでした"),
            (
                "REVIEW_RECOMMENDED",
                "確認推奨",
                "目で確認したい区間が見つかりました",
            ),
            (
                "FIX_RECOMMENDED",
                "修正推奨",
                "修正をおすすめする区間が見つかりました",
            ),
        ),
        default="NOT_RUN",
        options={"HIDDEN"},
    )
    motion_quality_report_json: StringProperty(
        name="モーション確認JSON",
        description="最後のモーション確認結果を保持する機械可読JSONです",
        default="",
        options={"HIDDEN"},
    )
    motion_quality_issue_preview_json: StringProperty(
        name="モーション確認の表示用区間",
        description="初心者画面に表示する問題区間の内部データです",
        default="",
        options={"HIDDEN"},
    )
    motion_quality_issue_count: IntProperty(
        name="モーション確認の問題区間数",
        description="最後の確認で見つかった問題区間の総数です",
        default=0,
        min=0,
        options={"HIDDEN"},
    )
    motion_quality_target: PointerProperty(
        name="モーション確認対象",
        description="最後にモーションを確認した編集用リグです",
        type=bpy.types.Object,
        poll=_armature_object_poll,
        options={"HIDDEN"},
    )
    motion_quality_stack_id: StringProperty(
        name="モーション確認対象の内部識別子",
        default="",
        options={"HIDDEN"},
    )
    motion_quality_evaluation_fingerprint: StringProperty(
        name="モーション確認時の評価状態",
        description="最後に確認したActionと修正レイヤー設定の内部識別情報です",
        default="",
        options={"HIDDEN"},
    )
    motion_quality_range_source: StringProperty(
        name="モーション確認範囲の種類",
        description="最後に確認した範囲が全体かプレビュー範囲かを保持します",
        default="",
        options={"HIDDEN"},
    )
    motion_quality_frame_start: IntProperty(
        name="モーション確認の開始フレーム",
        default=0,
        options={"HIDDEN"},
    )
    motion_quality_frame_end: IntProperty(
        name="モーション確認の終了フレーム",
        default=-1,
        options={"HIDDEN"},
    )
    motion_quality_sample_count: IntProperty(
        name="モーション確認のサンプル数",
        default=0,
        min=0,
        options={"HIDDEN"},
    )
    motion_quality_fps: IntProperty(
        name="モーション確認時のFPS",
        default=0,
        options={"HIDDEN"},
    )
    motion_quality_fps_base: FloatProperty(
        name="モーション確認時のFPS基準値",
        default=0.0,
        options={"HIDDEN"},
    )
    motion_quality_motion_revision: IntProperty(
        name="モーション確認時のモーション更新番号",
        default=-1,
        min=-1,
        options={"HIDDEN"},
    )
    source_armature: PointerProperty(
        name="元モーションリグ",
        description="Canonicalリグへ変換する、読み込み済みHumanoidモーションです",
        type=bpy.types.Object,
        poll=_armature_object_poll,
        update=_update_source_armature,
    )
    retarget_preset: EnumProperty(
        name="ボーン名の認識",
        items=(
            ("AUTO", "自動", "Unity HumanBone名とMixamo名を自動認識します"),
            ("EXACT", "Unity / Canonical", "Unity HumanBone名と完全一致させます"),
            ("MIXAMO", "Mixamo", "Mixamoのボーン名と名前空間を認識します"),
            ("MANUAL", "手動", "下の22ボーン対応表を手動で設定します"),
        ),
        default="AUTO",
    )
    manual_mapping: PointerProperty(type=MB_PG_ManualHumanoidMapping)
    manual_mapping_profile_json: StringProperty(
        name="手動ボーン対応の元モーション情報",
        description="手動ボーン対応を作成した元モーションの内部識別情報です",
        default="",
        options={"HIDDEN"},
    )
    manual_mapping_profiles_json: StringProperty(
        name="元モーション別の手動ボーン対応",
        description="元モーションごとに保存した手動ボーン対応の内部一覧です",
        default="",
        options={"HIDDEN"},
    )
    manual_mapping_bound_source: PointerProperty(
        name="手動ボーン対応を保持する元モーション",
        description="旧形式の手動ボーン対応を安全に移行する内部参照です",
        type=bpy.types.Object,
        poll=_armature_object_poll,
        options={"HIDDEN"},
    )
    beginner_retarget_error: StringProperty(
        name="元モーション変換の案内",
        default="",
        options={"HIDDEN"},
    )
    host: StringProperty(
        name="Unityの場所",
        description="Unityプレビューの接続先です。同じPC上のUnityに接続します",
        default="127.0.0.1",
    )
    control_port: IntProperty(
        name="TCPポート",
        description="Unityプレビューとの接続・状態確認に使用するTCPポートです",
        default=39871,
        min=1024,
        max=65535,
    )
    session_id: IntProperty(
        name="Session ID（接続番号）",
        default=0x12345678,
        min=1,
        max=0x7FFFFFFF,
    )
    send_hz: EnumProperty(
        name="送信頻度",
        items=(("30", "30 Hz", ""), ("60", "60 Hz", "")),
        default="60",
    )
    root_mode: EnumProperty(
        name="ルート移動",
        items=(
            ("IN_PLACE", "その場", "Armatureオブジェクトのルート移動を送りません"),
            ("WORLD_ROOT", "ワールド移動", "ワールド空間のルート移動を送ります"),
        ),
        default="WORLD_ROOT",
    )
    rig_visual_style: EnumProperty(
        name="修正リグの表示",
        description="編集できるCanonicalボーンの見た目を選びます",
        items=(
            ("STICK", "色付きスティック", "役割ごとの色が付いた細いボーンです"),
            ("WIRE", "半透明ワイヤー", "透過度を使ったワイヤー表示です"),
            ("OCTAHEDRAL", "通常ボーン", "Blender標準の八面体ボーンです"),
        ),
        default="STICK",
        update=_update_rig_visualization,
    )
    rig_visual_source_visible: BoolProperty(
        name="比較用の元リグ",
        description=(
            "読み込んだ元スケルトンを薄いワイヤーで表示します。"
            "このリグは修正ツールでは編集しません"
        ),
        default=True,
        update=_update_rig_visualization,
    )
    rig_visual_wire_opacity: FloatProperty(
        name="ワイヤー透過度",
        description="比較用の元リグとワイヤーボーンの濃さです",
        default=0.25,
        min=0.05,
        max=1.0,
        subtype="FACTOR",
        update=_update_rig_visualization,
    )
    rig_visual_show_names: BoolProperty(
        name="ボーン名",
        description="編集できるCanonicalリグにボーン名を表示します",
        default=False,
        update=_update_rig_visualization,
    )
    correction_include_location: BoolProperty(
        name="位置も修正（Hipsのみ）",
        description=("XYZ位置キーも修正します。通常はHipsを選んだときだけ有効にします"),
        default=False,
    )
    correction_blend_frames: IntProperty(
        name="前後のなじませ",
        description="修正なしの状態から自然につなぐ前後のフレーム数です",
        default=2,
        min=0,
        max=120,
    )
    correction_interpolation: EnumProperty(
        name="補間方法",
        items=(
            ("BEZIER", "なめらか", "オーバーシュートを抑えたBezier補間です"),
            ("LINEAR", "直線", "一定速度でつなぎます"),
            ("CONSTANT", "段階", "次のキーまで値を変えません"),
        ),
        default="BEZIER",
    )
    correction_envelope_preset: EnumProperty(
        name="カーブ形状",
        description="複数フレームへ修正をなじませるカーブの初期形状です",
        items=(
            (
                "SMOOTH_PULSE",
                "中央だけなめらかに",
                "中央へ向けて強め、両端で元に戻します",
            ),
            ("PLATEAU", "中央を一定に保つ", "中央の区間で修正を100%保ちます"),
            ("EASE_IN", "徐々に強く", "終了フレームへ向けて修正を強めます"),
            (
                "EASE_OUT",
                "早めに強く",
                "開始直後に修正を強め、終了フレームへなめらかに近づけます",
            ),
            ("EASE_IN_OUT", "なめらかに切替", "両端をなめらかにした一方向の変化です"),
            ("S_CURVE", "S字カーブ", "柔らかいS字で一方向に変化します"),
        ),
        default="SMOOTH_PULSE",
        update=_update_envelope_curve_preset,
    )
    correction_envelope_curve_tree: PointerProperty(
        name="補正カーブ内部データ",
        description="補正カーブエディターが内部で使用するデータです",
        type=bpy.types.NodeTree,
        options={"HIDDEN"},
    )
    contact_foot: EnumProperty(
        name="足",
        items=(
            ("LEFT", "左足", "LeftFootの接地範囲を検出します"),
            ("RIGHT", "右足", "RightFootの接地範囲を検出します"),
        ),
        default="LEFT",
    )
    contact_ground_height: FloatProperty(
        name="地面の高さ",
        description="足の接地判定に使うシーン空間の地面の高さです",
        default=0.0,
        subtype="DISTANCE",
        unit="LENGTH",
        update=_update_motion_quality_setting,
    )
    contact_max_height: FloatProperty(
        name="接地の最大高さ",
        description="接地中とみなす足先の地面からの最大高さです",
        default=0.08,
        min=0.0,
        soft_max=0.5,
        subtype="DISTANCE",
        unit="LENGTH",
        update=_update_motion_quality_setting,
    )
    contact_max_speed: FloatProperty(
        name="接地の最大速度",
        description="接地中とみなす足のワールド空間での最大速度です",
        default=0.30,
        min=0.0,
        soft_max=3.0,
        unit="VELOCITY",
        update=_update_motion_quality_setting,
    )
    contact_min_frames: IntProperty(
        name="最短フレーム",
        description="接地範囲として扱う最小の連続フレーム数です",
        default=3,
        min=1,
        max=240,
        update=_update_motion_quality_setting,
    )
    contact_gap_frames: IntProperty(
        name="隙間を結合",
        description="このフレーム数以下の接地の隙間を一つにつなぎます",
        default=1,
        min=0,
        max=30,
    )


class MB_OT_ApplyRigVisualization(Operator):
    bl_idname = "motion_bridge.apply_rig_visualization"
    bl_label = "リグ表示を適用"
    bl_description = (
        "編集できるCanonicalボーンを役割ごとに色分けし、元リグを薄い"
        "比較用ワイヤーとして表示します"
    )
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _resolve_canonical_armature(context) is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        global _rig_visual_last_error
        try:
            result = _apply_rig_visual_settings(context)
            if result is None:
                raise RuntimeError("Canonicalリグを選択してください")
        except (RuntimeError, TypeError, ValueError) as exception:
            _rig_visual_last_error = str(exception)
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        _rig_visual_last_error = ""
        self.report(
            {"INFO"},
            (
                f"{result.colored_bones}本のボーンを"
                f"{result.role_collections}グループに色分けしました"
            ),
        )
        return {"FINISHED"}


class MB_OT_RestoreRigVisualization(Operator):
    bl_idname = "motion_bridge.restore_rig_visualization"
    bl_label = "リグ表示を元に戻す"
    bl_description = (
        "リグ表示を適用する前に保存したArmatureと3Dビューの表示設定へ戻します"
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return has_rig_visualization_snapshot(_resolve_canonical_armature(context))

    def execute(self, context: bpy.types.Context) -> set[str]:
        canonical = _resolve_canonical_armature(context)
        if canonical is None or not restore_rig_visualization(canonical):
            self.report({"INFO"}, "元に戻せるリグ表示設定がありません")
            return {"CANCELLED"}
        self.report({"INFO"}, "以前のリグ表示設定へ戻しました")
        return {"FINISHED"}


class MB_OT_StartLivePose(Operator):
    bl_idname = "motion_bridge.start_live_pose"
    bl_label = "Unityへ接続"
    bl_description = (
        "TCPでUnityプレビューへ接続し、Canonicalリグのポーズをリアルタイム送信します"
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return bool(
            _pending_control_config is None
            and _resolve_canonical_armature(context) is not None
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        global _pending_control_config, _pending_live_armature, _pending_root_mode
        global _last_live_error
        settings = context.scene.motion_bridge_session
        try:
            armature = _resolve_canonical_armature(context)
            if armature is None:
                raise RuntimeError("Canonicalリグを1体選択してください")
            descriptor = _live_descriptor(armature)
            scene = context.scene
            frame_rate_denominator = max(1, round(float(scene.render.fps_base) * 1000))
            frame_rate_numerator = int(scene.render.fps) * 1000
            config = BlenderControlClientConfig(
                session_id=settings.session_id,
                descriptor=descriptor,
                control_port=settings.control_port,
                control_host=settings.host,
                blender_version=bpy.app.version_string,
                requested_pose_hz=int(settings.send_hz),
                root_mode=(
                    "WorldRoot" if settings.root_mode == "WORLD_ROOT" else "InPlace"
                ),
                frame_rate_numerator=frame_rate_numerator,
                frame_rate_denominator=frame_rate_denominator,
            )
            _pending_control_config = config
            _pending_live_armature = armature
            _pending_root_mode = settings.root_mode
            _live_reconnect_backoff.reset()
            _install_live_handlers()
            _last_live_error = ""
            _start_live_control_connection(time.monotonic())
            if not bpy.app.timers.is_registered(_live_timer):
                bpy.app.timers.register(_live_timer, first_interval=0.0)
        except (OSError, RuntimeError, TypeError, ValueError) as exception:
            _last_live_error = str(exception)
            _stop_live_session()
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        self.report({"INFO"}, "Unityへの接続を開始しました")
        return {"FINISHED"}


class MB_OT_StopLivePose(Operator):
    bl_idname = "motion_bridge.stop_live_pose"
    bl_label = "Unityとの接続を終了"

    @classmethod
    def poll(cls, _context: bpy.types.Context) -> bool:
        return _pending_control_config is not None

    def execute(self, _context: bpy.types.Context) -> set[str]:
        global _last_live_error
        _stop_live_session()
        _last_live_error = ""
        self.report({"INFO"}, "Unityへのリアルタイム送信を終了しました")
        return {"FINISHED"}


class MB_OT_ToggleLivePause(Operator):
    bl_idname = "motion_bridge.toggle_live_pause"
    bl_label = "一時停止／再開"
    bl_description = "接続を切らずにUnityプレビューを一時停止または再開します"

    @classmethod
    def poll(cls, _context: bpy.types.Context) -> bool:
        return bool(
            _control_client is not None
            and _control_client.state
            in {ControlSessionState.STREAMING, ControlSessionState.PAUSED}
        )

    def execute(self, _context: bpy.types.Context) -> set[str]:
        global _last_live_error
        client = _control_client
        if client is None:
            return {"CANCELLED"}
        try:
            if client.state is ControlSessionState.STREAMING:
                client.pause_streaming()
                message = "Unityプレビューを一時停止しました"
            else:
                client.start_streaming()
                message = "Unityプレビューを再開しました"
            if client.poll() is ControlSessionState.ERROR:
                failure = client.last_error or "TCP control session failed"
                if client.failure_is_retryable:
                    _schedule_live_reconnect(failure, time.monotonic())
                    self.report(
                        {"WARNING"}, "Unityの再起動を検出しました。再接続します"
                    )
                    return {"FINISHED"}
                raise RuntimeError(failure)
        except (OSError, RuntimeError, ValueError) as exception:
            _last_live_error = str(exception)
            _stop_live_session()
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        self.report({"INFO"}, message)
        return {"FINISHED"}


class MB_OT_CreateCorrectionLayer(Operator):
    bl_idname = "motion_bridge.create_correction_layer"
    bl_label = "修正レイヤーを作成"
    bl_description = "現在のモーションをBaseとして保護し、修正用レイヤーを作成します"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        armature = context.active_object
        animation_data = armature.animation_data if armature is not None else None
        base_action = animation_data.action if animation_data is not None else None
        has_stack = bool(
            armature is not None and armature.get("motion_bridge_stack_id")
        )
        return bool(
            armature is not None
            and armature.type == "ARMATURE"
            and armature.get("motion_bridge_skeleton_id") == SKELETON_ID
            and animation_data is not None
            and (
                has_stack
                or (
                    base_action is not None
                    and action_is_unshared_for_armature(armature, base_action)
                )
            )
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        armature = context.active_object
        animation_data = armature.animation_data if armature is not None else None
        base_action = animation_data.action if animation_data is not None else None
        if (
            armature is not None
            and not armature.get("motion_bridge_stack_id")
            and base_action is not None
            and not action_is_unshared_for_armature(armature, base_action)
        ):
            self.report(
                {"ERROR"},
                "元のシーンと複製側を同時には修正できません。"
                "不要な複製シーンを削除してから続けてください",
            )
            return {"CANCELLED"}
        try:
            stack = create_correction_layer(context, armature)
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        _invalidate_motion_quality_result(context)
        self.report(
            {"INFO"}, f"修正レイヤーを作成しました：{stack.correction_action.name}"
        )
        return {"FINISHED"}


def _active_correction_stack(context: bpy.types.Context):
    return get_correction_layer_stack(context.active_object)


def _idle_correction_stack(context: bpy.types.Context):
    stack = _active_correction_stack(context)
    armature = context.active_object
    animation_data = armature.animation_data if armature is not None else None
    if (
        stack is None
        or animation_data is None
        or animation_data.use_tweak_mode
        or bool(getattr(context.scene, "is_nla_tweakmode", False))
        or animation_data.action is not None
        or has_correction_edit_session(armature)
    ):
        return None
    return stack


def _unexpected_active_action(context: bpy.types.Context) -> bpy.types.Action | None:
    armature = context.active_object
    stack = get_correction_layer_stack(armature)
    animation_data = armature.animation_data if armature is not None else None
    action = animation_data.action if animation_data is not None else None
    if stack is None or action is None:
        return None
    correction_actions = {layer.action for layer in stack.correction_layers}
    return None if action in correction_actions else action


class MB_OT_DetachUnexpectedActiveAction(Operator):
    bl_idname = "motion_bridge.detach_unexpected_active_action"
    bl_label = "別のアニメーションを外す"
    bl_description = "修正レイヤーの上にあるActionを削除せず、一時的な割り当てだけ外します"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _unexpected_active_action(context) is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        action = _unexpected_active_action(context)
        animation_data = context.active_object.animation_data
        if action is None or animation_data is None:
            return {"CANCELLED"}
        animation_data.action = None
        _invalidate_motion_quality_result(context)
        self.report({"INFO"}, f"{action.name}の割り当てを外しました（データは保持）")
        return {"FINISHED"}


def _motion_quality_target(context: bpy.types.Context) -> bpy.types.Object:
    facts = _workflow_facts(context)
    armature = context.active_object
    if armature is None or not facts.active_is_canonical or not facts.has_motion:
        raise ValueError("モーションが入った編集用リグを選択してください")
    if (
        facts.unsupported_active_action
        or facts.unsupported_nla
        or facts.unrelated_tweak_mode
        or facts.edit_session
        or facts.editing
        or facts.envelope_preview
    ):
        raise ValueError("開いている修正作業を終了してからモーションを確認してください")
    if facts.stack_marker and not facts.stack_valid:
        raise ValueError("修正レイヤーの状態を直してからモーションを確認してください")
    if not facts.corrections_enabled:
        raise ValueError("「修正後」へ戻してからモーションを確認してください")
    return armature


def _motion_quality_whole_take_range(
    armature: bpy.types.Object,
) -> tuple[float, float] | None:
    stack = get_correction_layer_stack(armature)
    if stack is not None:
        return float(stack.base_strip.frame_start), float(stack.base_strip.frame_end)
    animation_data = armature.animation_data
    action = animation_data.action if animation_data is not None else None
    if action is None:
        return None
    return tuple(float(value) for value in action.frame_range)


def _motion_quality_range_spec(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    *,
    limit_to_current_window: bool = False,
    commit_preview: bool = False,
) -> RangeSpec:
    scene = context.scene
    whole = resolve_range_spec(
        whole_take_range=_motion_quality_whole_take_range(armature),
    )
    if limit_to_current_window:
        whole_start, whole_end = whole.expanded_integer_bounds()
        current = min(
            float(whole_end),
            max(float(whole_start), float(scene.frame_current)),
        )
        result = centered_preview_range(
            whole,
            current,
            maximum_samples=MAX_MOTION_SAMPLES,
        )
        if commit_preview:
            start, end = result.expanded_integer_bounds()
            scene.use_preview_range = True
            scene.frame_preview_start = start
            scene.frame_preview_end = end
        return result
    preview = (
        (float(scene.frame_preview_start), float(scene.frame_preview_end))
        if scene.use_preview_range
        else None
    )
    return resolve_range_spec(
        whole_take_range=(whole.scene_start, whole.scene_end),
        preview_range=preview,
    )


def _motion_quality_settings(context: bpy.types.Context) -> MotionValidationSettings:
    settings = context.scene.motion_bridge_session
    return MotionValidationSettings(
        contact_ground_height=float(settings.contact_ground_height),
        contact_max_height=float(settings.contact_max_height),
        contact_max_speed_m_s=float(settings.contact_max_speed),
        contact_min_samples=int(settings.contact_min_frames),
        # Canonical retargeting deliberately creates a dense Base.  Keep key
        # density when supplied by an offline/synchronous sample without
        # presenting expected bake density to a beginner as a visual defect.
        key_density_warning_keys_channel_s=60.0,
        key_density_error_keys_channel_s=120.0,
        # A straight but unstretched limb naturally evaluates near 1.0.  Allow
        # small matrix noise before calling it an over-reach in the beginner UI.
        limb_reach_warning_ratio=1.005,
        limb_reach_error_ratio=1.02,
    )


def _motion_quality_error_message(exception: Exception) -> str:
    message = str(exception)
    if "issue interval limit" in message:
        return (
            "確認したい区間が多すぎます。シーンの開始・終了を短くして、"
            "もう一度確認してください"
        )
    has_japanese_text = any(
        "\u3040" <= character <= "\u30ff"
        or "\u3400" <= character <= "\u9fff"
        for character in message
    )
    if isinstance(exception, MotionQualityProcessError) or not has_japanese_text:
        return (
            "モーションを確認できませんでした。確認範囲を短くして、"
            "もう一度確認してください"
        )
    return message


class MB_OT_ScanMotionQuality(Operator):
    bl_idname = "motion_bridge.scan_motion_quality"
    bl_label = "モーションを確認"
    bl_description = "足滑り、地面への入り込み、急な位置・回転の変化を自動で探します"

    limit_to_current_window: BoolProperty(
        default=False,
        options={"HIDDEN"},
    )

    _timer = None
    _sampler = None
    _analysis_job: MotionQualityProcessJob | None = None
    _armature = None
    _frame_start = 0
    _frame_end = -1
    _fps = 0
    _fps_base = 1.0
    _stack_id = ""
    _evaluation_fingerprint = ""
    _range_spec: RangeSpec | None = None
    _motion_revision = 0
    _validation_settings: MotionValidationSettings | None = None
    _aborted = False

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if (
            _motion_quality_scan_state is not None
            or _motion_quality_pending_cleanup_jobs
        ):
            return False
        try:
            _motion_quality_target(context)
        except (ReferenceError, RuntimeError, TypeError, ValueError):
            return False
        screen = context.screen
        return not bool(screen is not None and screen.is_animation_playing)

    def execute(self, context: bpy.types.Context) -> set[str]:
        """Synchronous entry point for background automation and tests."""

        if _motion_quality_scan_state is not None:
            self.report({"ERROR"}, "モーションの確認はすでに実行中です")
            return {"CANCELLED"}
        try:
            armature = _motion_quality_target(context)
            range_spec = _motion_quality_range_spec(
                context,
                armature,
                limit_to_current_window=bool(self.limit_to_current_window),
                commit_preview=bool(self.limit_to_current_window),
            )
            frame_start, frame_end = range_spec.expanded_integer_bounds()
            if range_spec.integer_sample_count > MAX_MOTION_SAMPLES:
                raise ValueError(
                    f"確認範囲が長すぎます（最大{MAX_MOTION_SAMPLES:,}フレーム）。"
                    "現在位置付近だけ確認してください"
                )
            validation_settings = _motion_quality_settings(context)
            sequence = sample_motion_range(
                context,
                armature,
                range_spec=range_spec,
            )
            report = analyze_motion(sequence, validation_settings)
            _store_motion_quality_result(
                context,
                armature,
                report,
                range_spec=range_spec,
            )
        except Exception as exception:  # Blender operator boundary.
            self.report({"ERROR"}, _motion_quality_error_message(exception))
            return {"CANCELLED"}
        _tag_motion_bridge_redraw(context)
        self._report_result(report)
        return {"FINISHED"}

    def invoke(
        self,
        context: bpy.types.Context,
        _event: bpy.types.Event,
    ) -> set[str]:
        global _motion_quality_scan_operator, _motion_quality_scan_state
        if context.window is None:
            return self.execute(context)
        if _motion_quality_scan_state is not None:
            self.report({"ERROR"}, "モーションの確認はすでに実行中です")
            return {"CANCELLED"}
        try:
            armature = _motion_quality_target(context)
            range_spec = _motion_quality_range_spec(
                context,
                armature,
                limit_to_current_window=bool(self.limit_to_current_window),
                commit_preview=bool(self.limit_to_current_window),
            )
            frame_start, frame_end = range_spec.expanded_integer_bounds()
            if range_spec.integer_sample_count > MAX_MOTION_SAMPLES:
                raise ValueError(
                    f"確認範囲が長すぎます（最大{MAX_MOTION_SAMPLES:,}フレーム）。"
                    "現在位置付近だけ確認してください"
                )
            validation_settings = _motion_quality_settings(context)
            evaluation_fingerprint = _motion_quality_evaluation_fingerprint(armature)
            if not evaluation_fingerprint:
                raise ValueError("モーションの評価状態を確認できませんでした")
            dependency_pointers = frozenset(
                _motion_quality_dependency_pointers(armature)
            )
            sampler = MotionRangeSampler(
                context,
                armature,
                range_spec=range_spec,
                # Exact per-frame key density walks every FCurve key before
                # the modal timer can start.  The interactive beginner scan
                # prioritizes immediate progress; offline callers retain it.
                include_key_density=False,
            ).begin()
        except (ReferenceError, RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}

        self._armature = armature
        self._frame_start = frame_start
        self._frame_end = frame_end
        self._fps = int(context.scene.render.fps)
        self._fps_base = float(context.scene.render.fps_base)
        self._stack_id = str(armature.get("motion_bridge_stack_id", ""))
        self._evaluation_fingerprint = evaluation_fingerprint
        self._range_spec = range_spec
        self._motion_revision = int(armature.get(MOTION_REVISION_PROPERTY, 0))
        self._validation_settings = validation_settings
        self._aborted = False
        self._sampler = sampler
        self._analysis_job = None
        _motion_quality_scan_state = _MotionQualityScanDisplayState(
            armature_pointer=int(armature.as_pointer()),
            dependency_pointers=dependency_pointers,
            sampled_count=0,
            sample_count=int(sampler.sample_count),
        )
        _motion_quality_scan_operator = self
        try:
            self._timer = context.window_manager.event_timer_add(
                0.01,
                window=context.window,
            )
            context.window_manager.modal_handler_add(self)
        except Exception as exception:  # Window lifecycle boundary.
            self._abort(context)
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        _tag_motion_bridge_redraw(context)
        return {"RUNNING_MODAL"}

    def modal(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        if self._aborted:
            self._cleanup(context)
            return {"CANCELLED"}
        state = _motion_quality_scan_state
        if event.type == "ESC" or (state is not None and state.cancel_requested):
            if state is not None:
                state.cancel_requested = True
            if not self._abort_or_defer(context):
                self.report({"WARNING"}, "確認処理を終了しています。少し待ってください")
                return {"PASS_THROUGH"}
            self.report({"INFO"}, "モーションの確認を中止しました")
            return {"CANCELLED"}
        if state is not None and state.input_changed:
            if not self._abort_or_defer(context):
                self.report({"WARNING"}, "確認処理を終了しています。少し待ってください")
                return {"PASS_THROUGH"}
            self.report(
                {"WARNING"},
                "確認中にモーションまたは設定が変わったため中止しました。"
                "もう一度確認してください",
            )
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if (
            self._timer is not None
            and getattr(event, "timer", None) is not self._timer
        ):
            return {"PASS_THROUGH"}

        try:
            if self._analysis_job is None:
                deadline = time.perf_counter() + 0.008
                sampled_any = False
                if state is not None:
                    state.internal_update = True
                try:
                    while not self._sampler.is_complete and (
                        not sampled_any or time.perf_counter() < deadline
                    ):
                        self._sampler.sample_frame()
                        sampled_any = True
                finally:
                    if state is not None:
                        state.internal_update = False
                if state is not None:
                    state.sampled_count = int(self._sampler.sampled_count)
                if not self._sampler.is_complete:
                    if state is not None:
                        state.internal_update = True
                    try:
                        self._sampler.restore_frame_for_yield()
                    finally:
                        if state is not None:
                            state.internal_update = False
                    _tag_motion_bridge_redraw(context)
                    return {"PASS_THROUGH"}
                if state is not None:
                    state.internal_update = True
                try:
                    sequence = self._sampler.finalize()
                finally:
                    if state is not None:
                        state.internal_update = False
                self._sampler = None
                if state is not None:
                    state.phase = "SERIALIZING"
                    state.analysis_progress = 0.0
                self._analysis_job = MotionQualityProcessJob(
                    sequence,
                    self._validation_settings,
                )
                _tag_motion_bridge_redraw(context)
                return {"PASS_THROUGH"}

            job = self._analysis_job
            if job.phase == "serializing":
                job.step_serialization(time.monotonic() + 0.008)
                if state is not None:
                    state.analysis_progress = float(job.progress)
                    state.phase = (
                        "ANALYZING" if job.phase == "analyzing" else "SERIALIZING"
                    )
                _tag_motion_bridge_redraw(context)
                return {"PASS_THROUGH"}

            report = job.poll()
            if report is None:
                if state is not None:
                    state.phase = "ANALYZING"
                    state.analysis_progress = 1.0
                return {"PASS_THROUGH"}
            if (
                state is None
                or state.input_changed
                or self._armature is None
                or int(self._armature.as_pointer())
                != int(getattr(state, "armature_pointer", 0))
                or self._armature.name not in context.scene.objects
                or str(self._armature.get("motion_bridge_stack_id", ""))
                != self._stack_id
                or _motion_quality_evaluation_fingerprint(self._armature)
                != self._evaluation_fingerprint
                or int(self._armature.get(MOTION_REVISION_PROPERTY, 0))
                != self._motion_revision
                or _motion_quality_range_spec(context, self._armature)
                != self._range_spec
                or int(context.scene.render.fps) != self._fps
                or float(context.scene.render.fps_base) != self._fps_base
                or _motion_quality_settings(context) != self._validation_settings
            ):
                raise RuntimeError(
                    "確認中にモーションまたは設定が変わったため、"
                    "結果を保存しませんでした。もう一度確認してください"
                )
            _store_motion_quality_result(
                context,
                self._armature,
                report,
                range_spec=self._range_spec,
            )
        except Exception as exception:  # Modal callbacks must not leak into Blender.
            self.report({"ERROR"}, _motion_quality_error_message(exception))
            state = _motion_quality_scan_state
            if state is not None:
                state.cancel_requested = True
            if not self._abort_or_defer(context):
                return {"PASS_THROUGH"}
            return {"CANCELLED"}

        self._cleanup(context)
        _mark_live_pose_dirty()
        _tag_motion_bridge_redraw(context)
        self._report_result(report)
        return {"FINISHED"}

    def cancel(self, context: bpy.types.Context) -> None:
        self._abort_or_defer(context)

    def _abort_or_defer(self, context: bpy.types.Context) -> bool:
        """Keep modal ownership alive when process cleanup needs another try."""

        try:
            self._abort(context)
        except Exception:
            state = _motion_quality_scan_state
            if state is not None:
                state.cancel_requested = True
            _mark_live_pose_dirty()
            _tag_motion_bridge_redraw(context)
            return False
        return True

    def _restore_sampler_and_detach_analysis(self) -> None:
        sampler = self._sampler
        if sampler is not None:
            try:
                sampler.discard_preserving_context()
            except (ReferenceError, RuntimeError, TypeError, ValueError):
                pass
        self._sampler = None
        job = self._analysis_job
        if job is not None:
            job.cancel()
        self._analysis_job = None

    def _prepare_external_cancel(self, context: bpy.types.Context) -> None:
        state = _motion_quality_scan_state
        if state is not None:
            state.cancel_requested = True
        self._restore_sampler_and_detach_analysis()
        _mark_live_pose_dirty()
        _tag_motion_bridge_redraw(context)

    def _abort(self, context: bpy.types.Context) -> None:
        self._restore_sampler_and_detach_analysis()
        self._aborted = True
        self._cleanup(context)
        _mark_live_pose_dirty()
        _tag_motion_bridge_redraw(context)

    def _cleanup(self, context: bpy.types.Context) -> None:
        global _motion_quality_scan_operator, _motion_quality_scan_state
        if self._timer is not None:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except (ReferenceError, RuntimeError):
                pass
            self._timer = None
        self._sampler = None
        self._analysis_job = None
        self._validation_settings = None
        self._armature = None
        self._stack_id = ""
        self._evaluation_fingerprint = ""
        self._range_spec = None
        self._motion_revision = 0
        if _motion_quality_scan_operator is self:
            _motion_quality_scan_state = None
            _motion_quality_scan_operator = None

    def _report_result(self, report: object) -> None:
        summary = _enum_value(getattr(report, "summary", "")).upper()
        issue_count = len(tuple(getattr(report, "issues", ())))
        if summary == "NO_ISSUES":
            message = "モーションを確認しました：問題は見つかりませんでした"
        else:
            message = f"モーションを確認しました：確認区間 {issue_count}件"
        self.report({"INFO"}, message)


class MB_OT_CancelMotionQualityScan(Operator):
    bl_idname = "motion_bridge.cancel_motion_quality_scan"
    bl_label = "確認を中止"
    bl_description = "実行中のモーション確認を中止し、開始前のフレームへ戻します"

    @classmethod
    def poll(cls, _context: bpy.types.Context) -> bool:
        return _motion_quality_scan_state is not None

    def execute(self, _context: bpy.types.Context) -> set[str]:
        state = _motion_quality_scan_state
        if state is None:
            return {"CANCELLED"}
        state.cancel_requested = True
        return {"FINISHED"}


class MB_OT_FocusMotionQualityIssue(Operator):
    bl_idname = "motion_bridge.focus_motion_quality_issue"
    bl_label = "この区間を表示"
    bl_description = "見つかった区間をタイムラインのプレビュー範囲にします"
    bl_options = {"REGISTER", "UNDO"}

    frame_start: IntProperty(options={"HIDDEN"})
    frame_end: IntProperty(options={"HIDDEN"})

    def execute(self, context: bpy.types.Context) -> set[str]:
        scene = context.scene
        settings = getattr(scene, "motion_bridge_session", None)
        armature = (
            getattr(settings, "motion_quality_target", None)
            if settings is not None
            else None
        )
        if armature is None or armature.type != "ARMATURE":
            armature = context.active_object
        try:
            whole = resolve_range_spec(
                whole_take_range=_motion_quality_whole_take_range(armature),
            )
        except (AttributeError, ReferenceError, TypeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        whole_start, whole_end = whole.expanded_integer_bounds()
        requested_start = min(int(self.frame_start), int(self.frame_end))
        requested_end = max(int(self.frame_start), int(self.frame_end))
        start = max(whole_start, min(requested_start, whole_end))
        end = max(start, min(requested_end, whole_end))
        # Preview playback only works predictably inside the Scene playback
        # range, so reveal the detected take interval instead of clipping it.
        scene.frame_start = min(int(scene.frame_start), start)
        scene.frame_end = max(int(scene.frame_end), end)
        scene.use_preview_range = True
        scene.frame_preview_start = start
        scene.frame_preview_end = end
        scene.frame_set(start)
        _mark_live_pose_dirty()
        self.report({"INFO"}, f"{start}〜{end}フレームを表示します")
        return {"FINISHED"}


class MB_OT_CopyMotionQualityReport(Operator):
    bl_idname = "motion_bridge.copy_motion_quality_report"
    bl_label = "確認結果JSONをコピー"
    bl_description = "最後のモーション確認結果をクリップボードへコピーします"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        settings = getattr(context.scene, "motion_bridge_session", None)
        return bool(settings is not None and settings.motion_quality_report_json)

    def execute(self, context: bpy.types.Context) -> set[str]:
        report_json = context.scene.motion_bridge_session.motion_quality_report_json
        if not report_json:
            return {"CANCELLED"}
        context.window_manager.clipboard = report_json
        self.report({"INFO"}, "モーション確認結果のJSONをコピーしました")
        return {"FINISHED"}


class MB_OT_DetectFootContactRange(Operator):
    bl_idname = "motion_bridge.detect_foot_contact_range"
    bl_label = "現在位置の接地範囲を検出"
    bl_description = (
        "現在フレームを含む選択した足の接地区間を検出し、"
        "タイムラインのプレビュー範囲に設定します"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _idle_correction_stack(context) is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        scene = context.scene
        settings = scene.motion_bridge_session
        foot_bone_name = "LeftFoot" if settings.contact_foot == "LEFT" else "RightFoot"
        try:
            interval = detect_and_commit_contact_preview_range(
                context,
                context.active_object,
                foot_bone_name=foot_bone_name,
                scene_frame_start=int(scene.frame_start),
                scene_frame_end=int(scene.frame_end),
                ground_height=float(settings.contact_ground_height),
                max_height=float(settings.contact_max_height),
                max_speed=float(settings.contact_max_speed),
                min_frames=int(settings.contact_min_frames),
                gap_frames=int(settings.contact_gap_frames),
            )
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}

        self.report(
            {"INFO"},
            (
                f"{foot_bone_name}の接地範囲："
                f"{interval.frame_start}-{interval.frame_end}"
            ),
        )
        return {"FINISHED"}


def _auto_correction_poll(context: bpy.types.Context) -> bool:
    stack = _idle_correction_stack(context)
    return bool(
        _motion_quality_scan_state is None
        and stack is not None
        and correction_actions_are_unshared(context.active_object)
        and corrections_preview_enabled(context.active_object)
        and not any(layer.solo for layer in stack.correction_layers)
    )


def _finish_auto_correction(context: bpy.types.Context) -> None:
    _invalidate_motion_quality_result(context)
    _mark_live_pose_dirty()


def _auto_correction_scan_is_idle(operator: Operator) -> bool:
    if _motion_quality_scan_state is None:
        return True
    operator.report({"ERROR"}, "モーション確認が終わってから自動補正してください")
    return False


class MB_OT_AutoSmoothMotion(Operator):
    bl_idname = "motion_bridge.auto_smooth_motion"
    bl_label = "揺れをなめらかに"
    bl_description = (
        "細かな回転の揺れを検出してならし、新しい修正レイヤーへ保存します"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _auto_correction_poll(context)

    def execute(self, context: bpy.types.Context) -> set[str]:
        if not _auto_correction_scan_is_idle(self):
            return {"CANCELLED"}
        settings = context.scene.motion_bridge_session
        try:
            result = apply_smoothing_correction(
                context,
                context.active_object,
                _motion_quality_range_spec(context, context.active_object),
                strength=SmoothingStrength[str(settings.auto_smoothing_strength)],
            )
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        if not result.applied:
            self.report({"INFO"}, "除去できる細かな揺れは見つかりませんでした")
            return {"CANCELLED"}
        _finish_auto_correction(context)
        self.report(
            {"INFO"},
            f"揺れをなめらかにしました：{result.written_frame_count}フレーム",
        )
        return {"FINISHED"}


class MB_OT_AutoFootLock(Operator):
    bl_idname = "motion_bridge.auto_foot_lock"
    bl_label = "足滑りを固定"
    bl_description = "接地区間を自動検出して足を固定し、新しい修正レイヤーへ保存します"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _auto_correction_poll(context)

    def execute(self, context: bpy.types.Context) -> set[str]:
        if not _auto_correction_scan_is_idle(self):
            return {"CANCELLED"}
        settings = context.scene.motion_bridge_session
        try:
            result = apply_foot_lock_correction(
                context,
                context.active_object,
                _motion_quality_range_spec(context, context.active_object),
                side=FootLockSide[str(settings.auto_foot_lock_side)],
                settings=FootLockSettings(
                    ground_height=float(settings.contact_ground_height),
                    max_contact_height=float(settings.contact_max_height),
                    max_contact_speed=float(settings.contact_max_speed),
                    min_contact_frames=int(settings.contact_min_frames),
                    join_gap_frames=int(settings.contact_gap_frames),
                ),
            )
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        if not result.applied:
            self.report({"INFO"}, "指定範囲に固定できる接地区間は見つかりませんでした")
            return {"CANCELLED"}
        _finish_auto_correction(context)
        level = {"WARNING"} if result.clamped_sample_count else {"INFO"}
        detail = (
            f"（届かない姿勢 {result.clamped_sample_count}サンプルは安全範囲まで）"
            if result.clamped_sample_count
            else ""
        )
        self.report(
            level,
            f"足滑りを固定しました：{result.interval_count}区間{detail}",
        )
        return {"FINISHED"}


class MB_OT_AutoJointLimits(Operator):
    bl_idname = "motion_bridge.auto_joint_limits"
    bl_label = "関節の曲がりすぎを直す"
    bl_description = (
        "標準的な関節可動域を超えた回転を柔らかく戻し、"
        "新しい修正レイヤーへ保存します"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _auto_correction_poll(context)

    def execute(self, context: bpy.types.Context) -> set[str]:
        if not _auto_correction_scan_is_idle(self):
            return {"CANCELLED"}
        try:
            result = apply_joint_limit_correction(
                context,
                context.active_object,
                _motion_quality_range_spec(context, context.active_object),
            )
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        if not result.applied:
            self.report({"INFO"}, "標準範囲を超えた関節は見つかりませんでした")
            return {"CANCELLED"}
        _finish_auto_correction(context)
        self.report(
            {"INFO"},
            f"関節を補正しました：{result.affected_sample_count}サンプル",
        )
        return {"FINISHED"}


class MB_OT_EnterCorrectionEdit(Operator):
    bl_idname = "motion_bridge.enter_correction_edit"
    bl_label = "選択中の修正レイヤーを編集"
    bl_description = "ポーズモードへ入り、選択中の修正レイヤーだけを編集します"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return bool(
            _idle_correction_stack(context) is not None
            and correction_actions_are_unshared(context.active_object)
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        if not correction_actions_are_unshared(context.active_object):
            self.report(
                {"ERROR"},
                "元のシーンと複製側を同時には編集できません。"
                "不要な複製シーンを削除してから、編集したいシーンで続けてください",
            )
            return {"CANCELLED"}
        try:
            _ensure_envelope_curve_node(context.scene.motion_bridge_session)
            session = enter_correction_edit_mode(context, context.active_object)
        except (AttributeError, RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        _invalidate_motion_quality_result(context)
        _update_rig_visualization(context.scene.motion_bridge_session, context)
        layer = active_correction_edit_layer(context.active_object)
        name = layer.name if layer is not None else session.action.name
        self.report({"INFO"}, f"修正レイヤーを編集中：{name}")
        return {"FINISHED"}


class MB_OT_ExitCorrectionEdit(Operator):
    bl_idname = "motion_bridge.exit_correction_edit"
    bl_label = "修正作業を終了"
    bl_description = "修正キーを確認し、編集前の選択・モード・タイムラインへ戻します"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return is_correction_editing(context.active_object)

    def execute(self, context: bpy.types.Context) -> set[str]:
        if has_correction_envelope_preview(context.active_object):
            self.report({"ERROR"}, "先に補正カーブを確定または取消してください")
            return {"CANCELLED"}
        layer = active_correction_edit_layer(context.active_object)
        try:
            exit_correction_edit_mode(context, context.active_object)
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            (
                f"修正作業を終了しました：{layer.name}"
                if layer is not None
                else "修正作業を終了しました"
            ),
        )
        return {"FINISHED"}


class MB_OT_RecoverCorrectionEdit(Operator):
    bl_idname = "motion_bridge.recover_correction_edit"
    bl_label = "修正キーを残して通常状態へ戻す"
    bl_description = (
        "UndoやBlender標準操作でずれた修正状態を、"
        "記録済みのキーを残して安全に終了します"
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        armature = context.active_object
        return bool(
            has_correction_edit_session(armature)
            and not is_correction_editing(armature)
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        preview = active_correction_envelope_preview(context.active_object)
        if preview is not None:
            try:
                preview.cancel()
            except (RuntimeError, TypeError, ValueError) as exception:
                self.report({"ERROR"}, str(exception))
                return {"CANCELLED"}
        try:
            session = cancel_correction_edit_mode(context, context.active_object)
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        if session is None:
            self.report({"INFO"}, "復元が必要な修正作業はありません")
        else:
            self.report({"INFO"}, "修正キーを残して通常状態へ戻しました")
        return {"FINISHED"}


class MB_OT_ExitSavedCorrectionTweak(Operator):
    bl_idname = "motion_bridge.exit_saved_correction_tweak"
    bl_label = "保存された修正モードを安全に終了"
    bl_description = (
        "再起動前に保存された修正キーを残したまま、古いNLA Tweak Modeだけを終了します"
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return stale_correction_tweak_layer(context.active_object) is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            layer = exit_stale_correction_tweak_mode(
                context,
                context.active_object,
            )
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        _update_rig_visualization(context.scene.motion_bridge_session, context)
        self.report(
            {"INFO"},
            f"修正キーを残して通常状態へ戻しました：{layer.name}",
        )
        return {"FINISHED"}


def _can_edit_correction_pose(context: bpy.types.Context) -> bool:
    armature = context.active_object
    return bool(
        is_correction_editing(armature)
        and armature is not None
        and armature.mode == "POSE"
        and any(pose_bone_selected(bone) for bone in armature.pose.bones)
    )


class MB_OT_InsertCorrectionPoseKeys(Operator):
    bl_idname = "motion_bridge.insert_correction_pose_keys"
    bl_label = "現在フレームに修正を記録"
    bl_description = "選択したボーンの回転と、前後のなじませを一式で記録します"
    bl_options = {"REGISTER", "UNDO"}

    include_location: BoolProperty(
        name="位置も記録",
        description="Hipsの位置も一式で記録します",
        default=False,
    )
    neutral_padding: IntProperty(
        name="前後のなじませ",
        description=("修正なしの状態へ戻すために前後へ置くフレーム数です。0で無効です"),
        default=2,
        min=0,
        max=120,
    )
    interpolation: EnumProperty(
        name="補間方法",
        items=(
            ("BEZIER", "なめらか", "オーバーシュートを抑えたBezier補間です"),
            ("LINEAR", "直線", "一定速度でつなぎます"),
            ("CONSTANT", "段階", "次のキーまで値を変えません"),
        ),
        default="BEZIER",
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _can_edit_correction_pose(context)

    def execute(self, context: bpy.types.Context) -> set[str]:
        armature = context.active_object
        layer = active_correction_edit_layer(armature)
        if layer is None:
            return {"CANCELLED"}
        before: float | None = None
        after: float | None = None
        if self.neutral_padding:
            try:
                scene_frame = float(context.scene.frame_current) + float(
                    context.scene.frame_subframe
                )
                frame = scene_to_correction_frame(
                    armature,
                    scene_frame,
                )
                candidate_before = scene_to_correction_frame(
                    armature,
                    scene_frame - self.neutral_padding,
                )
                candidate_after = scene_to_correction_frame(
                    armature,
                    scene_frame + self.neutral_padding,
                )
            except (RuntimeError, TypeError, ValueError) as exception:
                self.report({"ERROR"}, str(exception))
                return {"CANCELLED"}
            frame_start, frame_end = (
                float(value) for value in layer.action.frame_range
            )
            if frame_start <= candidate_before < frame:
                before = candidate_before
            if frame < candidate_after <= frame_end:
                after = candidate_after
        try:
            names = insert_correction_pose_keys(
                context,
                armature,
                include_location=self.include_location,
                neutral_frame_before=before,
                neutral_frame_after=after,
                interpolation=self.interpolation,
            )
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            f"{layer.name}へ{len(names)}本のボーンの修正を記録しました",
        )
        return {"FINISHED"}


class MB_OT_ApplyCorrectionPoseRange(Operator):
    bl_idname = "motion_bridge.apply_correction_pose_range"
    bl_label = "プレビュー範囲へ修正を反映"
    bl_description = "選択ボーンの現在の修正をタイムラインのプレビュー範囲へ反映します"
    bl_options = {"REGISTER", "UNDO"}

    include_location: BoolProperty(default=False, options={"HIDDEN"})
    ease_frames: IntProperty(default=2, min=0, max=120, options={"HIDDEN"})
    interpolation: EnumProperty(
        items=(
            ("BEZIER", "なめらか", "オーバーシュートを抑えたBezier補間です"),
            ("LINEAR", "直線", "一定速度でつなぎます"),
            ("CONSTANT", "段階", "次のキーまで値を変えません"),
        ),
        default="BEZIER",
        options={"HIDDEN"},
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        scene = context.scene
        current = float(scene.frame_current) + float(scene.frame_subframe)
        return bool(
            _can_edit_correction_pose(context)
            and scene.use_preview_range
            and scene.frame_preview_start <= current <= scene.frame_preview_end
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            names = apply_correction_pose_range(
                context,
                context.active_object,
                scene_frame_start=float(context.scene.frame_preview_start),
                scene_frame_end=float(context.scene.frame_preview_end),
                ease_in_frames=float(self.ease_frames),
                ease_out_frames=float(self.ease_frames),
                include_location=self.include_location,
                interpolation=self.interpolation,
                replace_inner_keys=True,
            )
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            f"{len(names)}本のボーンをプレビュー範囲へ反映しました",
        )
        return {"FINISHED"}


def _tag_motion_bridge_redraw(context: bpy.types.Context) -> None:
    screen = context.screen
    if screen is None:
        return
    for area in screen.areas:
        area.tag_redraw()


def _envelope_range_source_label(source: object) -> str:
    value = str(getattr(source, "value", source))
    return {
        "SELECTED_KEYS": "選択キー",
        "PREVIEW_RANGE": "プレビュー範囲",
    }.get(value, value)


def _control_session_state_label(state: ControlSessionState) -> str:
    return {
        ControlSessionState.DISCONNECTED: "未接続",
        ControlSessionState.CONNECTING: "接続中",
        ControlSessionState.HANDSHAKING: "確認中",
        ControlSessionState.READY: "準備完了",
        ControlSessionState.STREAMING: "送信中",
        ControlSessionState.PAUSED: "一時停止中",
        ControlSessionState.ERROR: "エラー",
    }.get(state, state.value)


def _take_budget_tier_label(tier: TakeBudgetTier) -> str:
    return {
        TakeBudgetTier.NATIVE: "軽量",
        TakeBudgetTier.HEAVY: "大容量",
        TakeBudgetTier.SPLIT_RECOMMENDED: "分割を推奨",
    }.get(tier, tier.value)


class MB_OT_BeginCorrectionEnvelope(Operator):
    bl_idname = "motion_bridge.begin_correction_envelope"
    bl_label = "補正カーブをプレビュー"
    bl_description = (
        "現在のポーズ補正を時間・強さカーブでプレビューします。選択した修正キー、"
        "またはタイムラインのプレビュー範囲を使用します"
    )

    mode: EnumProperty(
        items=(
            (
                "POSE_OFFSET",
                "今のポーズを範囲に反映",
                "現在の修正ポーズを範囲へ反映します",
            ),
            (
                "REWEIGHT",
                "既存キーの強さを調整",
                "選択した既存の修正キーの強さを変更します",
            ),
            (
                "REEDIT",
                "確定したカーブを再編集",
                "保存された元ポーズから最後の補正カーブを作り直します",
            ),
        ),
        default="POSE_OFFSET",
        options={"HIDDEN"},
    )

    _timer = None
    _armature: bpy.types.Object | None = None
    _curve_signature: tuple[tuple[float, float, str], ...] = ()

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        armature = context.active_object
        return bool(
            _can_edit_correction_pose(context)
            and not has_correction_envelope_preview(armature)
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        global _envelope_last_error
        armature = context.active_object
        settings = context.scene.motion_bridge_session
        scene = context.scene
        preview_start = (
            float(scene.frame_preview_start)
            if scene.use_preview_range
            else float("nan")
        )
        preview_end = (
            float(scene.frame_preview_end) if scene.use_preview_range else float("nan")
        )
        try:
            node = _ensure_envelope_curve_node(settings)
            evaluator = _envelope_curve_evaluator(node)
            if self.mode == "REEDIT":
                preview = begin_committed_pose_envelope_preview(
                    context,
                    armature,
                    evaluator,
                )
                envelope_range = preview.envelope_range
            else:
                envelope_range = resolve_envelope_range(
                    context,
                    armature,
                    preview_scene_start=preview_start,
                    preview_scene_end=preview_end,
                    prefer_preview_range=bool(scene.use_preview_range),
                    include_location=settings.correction_include_location,
                )
                span = float(envelope_range.scene_end) - float(envelope_range.scene_start)
                if span <= 0.0:
                    raise ValueError(
                        "2フレーム以上のキーを選択するか、空でないプレビュー範囲を設定してください"
                    )
                if self.mode == "REWEIGHT":
                    preview = begin_reweight_envelope_preview(
                        context,
                        armature,
                        envelope_range,
                        evaluator,
                        include_location=settings.correction_include_location,
                    )
                else:
                    # Adaptive LINEAR samples keep long flat ranges sparse while
                    # avoiding independent quaternion-component Bezier overshoot.
                    preview = begin_pose_envelope_preview(
                        context,
                        armature,
                        envelope_range,
                        evaluator,
                        include_location=settings.correction_include_location,
                        interpolation="LINEAR",
                        replace_inner_keys=True,
                    )
        except (
            AttributeError,
            ReferenceError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exception:
            message = str(exception)
            if not scene.use_preview_range and "Preview range" in message:
                message = (
                    "2フレーム以上の修正キーを選択するか、"
                    "タイムラインのプレビュー範囲を有効にしてください"
                )
            _envelope_last_error = message
            self.report({"ERROR"}, message)
            return {"CANCELLED"}

        if context.window is None:
            preview.cancel()
            _envelope_last_error = (
                "補正カーブのプレビューにはBlenderウィンドウが必要です"
            )
            self.report({"ERROR"}, _envelope_last_error)
            return {"CANCELLED"}
        self._armature = armature
        self._curve_signature = _envelope_curve_signature(node)
        self._timer = context.window_manager.event_timer_add(
            0.08,
            window=context.window,
        )
        context.window_manager.modal_handler_add(self)
        _envelope_last_error = ""
        _mark_live_pose_dirty()
        _tag_motion_bridge_redraw(context)
        source = _envelope_range_source_label(envelope_range.source)
        self.report(
            {"INFO"},
            (
                f"補正カーブをプレビュー中：{source} "
                f"{envelope_range.scene_start:g}-{envelope_range.scene_end:g}"
            ),
        )
        return {"RUNNING_MODAL"}

    def modal(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        global _envelope_last_error
        preview = active_correction_envelope_preview(self._armature)
        if preview is None:
            self._remove_timer(context)
            return {"FINISHED"}
        if not is_correction_editing(self._armature):
            try:
                preview.cancel()
                _envelope_last_error = (
                    "修正編集が終了したため補正カーブを取り消しました"
                )
            except Exception as rollback_error:
                _envelope_last_error = "補正カーブを元に戻せませんでした：" + str(
                    rollback_error
                )
            self._remove_timer(context)
            _mark_live_pose_dirty()
            _tag_motion_bridge_redraw(context)
            return {"CANCELLED"}
        if event.type == "ESC":
            try:
                preview.cancel()
            except (RuntimeError, TypeError, ValueError) as exception:
                _envelope_last_error = str(exception)
                self.report({"ERROR"}, _envelope_last_error)
            self._remove_timer(context)
            _mark_live_pose_dirty()
            _tag_motion_bridge_redraw(context)
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        try:
            settings = context.scene.motion_bridge_session
            node = _ensure_envelope_curve_node(settings)
            signature = _envelope_curve_signature(node)
            if signature != self._curve_signature:
                preview.update(_envelope_curve_evaluator(node))
                self._curve_signature = signature
                _mark_live_pose_dirty()
                _tag_motion_bridge_redraw(context)
        except (
            AttributeError,
            ReferenceError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exception:
            _envelope_last_error = str(exception)
            try:
                preview.cancel()
            except Exception as rollback_error:
                _envelope_last_error += "；元に戻せませんでした：" + str(rollback_error)
            self.report({"ERROR"}, _envelope_last_error)
            self._remove_timer(context)
            _mark_live_pose_dirty()
            _tag_motion_bridge_redraw(context)
            return {"CANCELLED"}
        return {"PASS_THROUGH"}

    def cancel(self, context: bpy.types.Context) -> None:
        preview = active_correction_envelope_preview(self._armature)
        if preview is not None:
            try:
                preview.cancel()
            except Exception:
                pass
        self._remove_timer(context)
        _mark_live_pose_dirty()

    def _remove_timer(self, context: bpy.types.Context) -> None:
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None


class MB_OT_ApplyCorrectionEnvelope(Operator):
    bl_idname = "motion_bridge.apply_correction_envelope"
    bl_label = "補正カーブを確定"
    bl_description = "現在プレビューしている補正カーブを確定します"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return has_correction_envelope_preview(context.active_object)

    def execute(self, context: bpy.types.Context) -> set[str]:
        global _envelope_last_error
        preview = active_correction_envelope_preview(context.active_object)
        if preview is None:
            return {"CANCELLED"}
        settings = context.scene.motion_bridge_session
        try:
            node = _ensure_envelope_curve_node(settings)
            payload = json.loads(_envelope_curve_metadata(settings, node))
            names = preview.apply(metadata=payload)
        except (
            AttributeError,
            ReferenceError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exception:
            _envelope_last_error = str(exception)
            self.report({"ERROR"}, _envelope_last_error)
            return {"CANCELLED"}
        _envelope_last_error = ""
        _mark_live_pose_dirty()
        _tag_motion_bridge_redraw(context)
        self.report({"INFO"}, f"補正カーブを{len(names)}本のボーンへ確定しました")
        return {"FINISHED"}


class MB_OT_CancelCorrectionEnvelope(Operator):
    bl_idname = "motion_bridge.cancel_correction_envelope"
    bl_label = "補正カーブを取り消す"
    bl_description = "補正カーブのプレビュー開始前と完全に同じキーへ戻します"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return has_correction_envelope_preview(context.active_object)

    def execute(self, context: bpy.types.Context) -> set[str]:
        global _envelope_last_error
        preview = active_correction_envelope_preview(context.active_object)
        if preview is None:
            return {"CANCELLED"}
        try:
            names = preview.cancel()
        except (RuntimeError, TypeError, ValueError) as exception:
            _envelope_last_error = str(exception)
            self.report({"ERROR"}, _envelope_last_error)
            return {"CANCELLED"}
        _envelope_last_error = ""
        _mark_live_pose_dirty()
        _tag_motion_bridge_redraw(context)
        self.report({"INFO"}, f"{len(names)}本のボーンを開始前へ戻しました")
        return {"FINISHED"}


class MB_OT_ResetCorrectionPoseKeys(Operator):
    bl_idname = "motion_bridge.reset_correction_pose_keys"
    bl_label = "現在フレームの修正を0へ戻す"
    bl_description = "現在フレームで選択ボーンの修正量を0として記録します"
    bl_options = {"REGISTER", "UNDO"}

    include_location: BoolProperty(default=False, options={"HIDDEN"})
    interpolation: EnumProperty(
        items=(
            ("BEZIER", "なめらか", "オーバーシュートを抑えたBezier補間です"),
            ("LINEAR", "直線", "一定速度でつなぎます"),
            ("CONSTANT", "段階", "次のキーまで値を変えません"),
        ),
        default="BEZIER",
        options={"HIDDEN"},
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _can_edit_correction_pose(context)

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            names = reset_correction_pose_keys(
                context,
                context.active_object,
                include_location=self.include_location,
                interpolation=self.interpolation,
            )
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        self.report({"INFO"}, f"選択した{len(names)}本のボーンの修正を0へ戻しました")
        return {"FINISHED"}


class MB_OT_RemoveCorrectionPoseKeys(Operator):
    bl_idname = "motion_bridge.remove_correction_pose_keys"
    bl_label = "現在フレームの修正キーを削除"
    bl_description = "現在フレームにある選択ボーンの修正キーを削除します"
    bl_options = {"REGISTER", "UNDO"}

    include_location: BoolProperty(default=False, options={"HIDDEN"})

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _can_edit_correction_pose(context)

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            names = remove_correction_pose_keys(
                context,
                context.active_object,
                include_location=self.include_location,
            )
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        if names:
            self.report({"INFO"}, f"選択した{len(names)}本のボーンのキーを削除しました")
        else:
            self.report({"INFO"}, "現在フレームに選択ボーンの修正キーはありません")
        return {"FINISHED"}


class MB_OT_CopySourcePose(Operator):
    bl_idname = "motion_bridge.copy_source_pose"
    bl_label = "元モーションのポーズをコピー"
    bl_description = "現在フレームのBaseモーション全身ポーズをコピーします"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _idle_correction_stack(context) is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        global _source_pose_clipboard
        armature = context.active_object
        scene = context.scene
        frame = float(scene.frame_current) + float(scene.frame_subframe)
        was_after = corrections_preview_enabled(armature)
        try:
            try:
                set_corrections_preview_enabled(armature, False)
                scene.frame_set(scene.frame_current, subframe=scene.frame_subframe)
                context.view_layer.update()
                evaluated = armature.evaluated_get(context.evaluated_depsgraph_get())
                bones = tuple(
                    CorrectionBoneTarget(
                        bone_name=bone.name,
                        local_rotation_wxyz=tuple(
                            _evaluated_local_matrix(bone).to_quaternion()
                        ),
                        location=(
                            tuple(_evaluated_local_matrix(bone).to_translation())
                            if bone.name == "Hips" else None
                        ),
                    )
                    for bone in evaluated.pose.bones
                )
                root_location = (
                    tuple(evaluated.matrix_world.translation)
                    if armature.rotation_mode == "QUATERNION" else None
                )
                root_rotation = (
                    tuple(evaluated.matrix_world.to_quaternion())
                    if armature.rotation_mode == "QUATERNION" else None
                )
            finally:
                set_corrections_preview_enabled(armature, was_after)
                scene.frame_set(scene.frame_current, subframe=scene.frame_subframe)
        except (ReferenceError, RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        _source_pose_clipboard = (
            int(armature.as_pointer()),
            str(armature["motion_bridge_stack_id"]),
            frame,
            bones,
            root_location,
            root_rotation,
        )
        self.report({"INFO"}, f"元モーションの{frame:g}フレームをコピーしました")
        return {"FINISHED"}


class MB_OT_PasteSourcePose(Operator):
    bl_idname = "motion_bridge.paste_source_pose"
    bl_label = "現在フレームへポーズを貼り付け"
    bl_description = "コピーした全身ポーズを選択中の修正レイヤーへ1フレームだけ記録します"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        clipboard = _source_pose_clipboard
        armature = context.active_object
        return bool(
            clipboard is not None
            and _idle_correction_stack(context) is not None
            and int(armature.as_pointer()) == clipboard[0]
            and armature.get("motion_bridge_stack_id") == clipboard[1]
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        clipboard = _source_pose_clipboard
        if clipboard is None or not self.poll(context):
            return {"CANCELLED"}
        scene = context.scene
        target = CorrectionFrameTarget(
            scene_frame=float(scene.frame_current) + float(scene.frame_subframe),
            bones=clipboard[3],
            root_location=clipboard[4],
            root_rotation_wxyz=clipboard[5],
        )
        try:
            write_correction_target(context, context.active_object, target)
        except (ReferenceError, RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        _invalidate_motion_quality_result(context)
        _mark_live_pose_dirty()
        self.report({"INFO"}, f"現在の{target.scene_frame:g}フレームへ貼り付けました")
        return {"FINISHED"}


class MB_OT_AddCorrectionLayer(Operator):
    bl_idname = "motion_bridge.add_correction_layer"
    bl_label = "修正レイヤーを追加"
    bl_description = "現在のレイヤー構成へ空の修正レイヤーを追加します"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _idle_correction_stack(context) is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            layer = add_correction_layer(context, context.active_object)
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        _invalidate_motion_quality_result(context)
        self.report({"INFO"}, f"追加しました：{layer.name}")
        return {"FINISHED"}


class MB_OT_SetCorrectionPreview(Operator):
    bl_idname = "motion_bridge.set_correction_preview"
    bl_label = "修正前後を比較"
    bl_description = "元モーションだけの修正前と、修正レイヤー適用後を切り替えます"
    bl_options = {"REGISTER", "UNDO"}

    enabled: BoolProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _idle_correction_stack(context) is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            set_corrections_preview_enabled(
                context.active_object,
                self.enabled,
            )
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        _invalidate_motion_quality_result(context)
        self.report(
            {"INFO"},
            "修正後を表示しています" if self.enabled else "修正前を表示しています",
        )
        return {"FINISHED"}


class MB_OT_SelectCorrectionLayer(Operator):
    bl_idname = "motion_bridge.select_correction_layer"
    bl_label = "修正レイヤーを選択"
    bl_options = {"REGISTER", "UNDO"}

    layer_id: StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _idle_correction_stack(context) is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            layer = set_active_correction_layer(
                context.active_object,
                self.layer_id,
            )
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        self.report({"INFO"}, f"選択しました：{layer.name}")
        return {"FINISHED"}


class MB_OT_RenameCorrectionLayer(Operator):
    bl_idname = "motion_bridge.rename_correction_layer"
    bl_label = "修正レイヤー名を変更"
    bl_options = {"REGISTER", "UNDO"}

    layer_id: StringProperty(options={"HIDDEN"})
    new_name: StringProperty(name="レイヤー名")

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _idle_correction_stack(context) is not None

    def invoke(
        self,
        context: bpy.types.Context,
        _event: bpy.types.Event,
    ) -> set[str]:
        stack = _idle_correction_stack(context)
        if stack is None:
            return {"CANCELLED"}
        if not self.layer_id:
            self.layer_id = stack.active_layer_id
        layer = next(
            (
                candidate
                for candidate in stack.correction_layers
                if candidate.layer_id == self.layer_id
            ),
            None,
        )
        if layer is None:
            return {"CANCELLED"}
        self.new_name = layer.name
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            layer = rename_correction_layer(
                context.active_object,
                self.layer_id,
                self.new_name,
            )
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        self.report({"INFO"}, f"レイヤー名を変更しました：{layer.name}")
        return {"FINISHED"}


class MB_OT_DuplicateCorrectionLayer(Operator):
    bl_idname = "motion_bridge.duplicate_correction_layer"
    bl_label = "修正レイヤーを複製"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _idle_correction_stack(context) is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        stack = _idle_correction_stack(context)
        if stack is None:
            return {"CANCELLED"}
        try:
            layer = duplicate_correction_layer(
                context,
                context.active_object,
                stack.active_layer_id,
            )
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        _invalidate_motion_quality_result(context)
        self.report({"INFO"}, f"複製しました：{layer.name}")
        return {"FINISHED"}


class MB_OT_MoveCorrectionLayer(Operator):
    bl_idname = "motion_bridge.move_correction_layer"
    bl_label = "修正レイヤーを移動"
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(
        items=(
            ("UP", "上へ", "NLAの評価順を後へ移動します"),
            ("DOWN", "下へ", "NLAの評価順を前へ移動します"),
        )
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _idle_correction_stack(context) is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        stack = _idle_correction_stack(context)
        if stack is None:
            return {"CANCELLED"}
        try:
            move_correction_layer(
                context.active_object,
                stack.active_layer_id,
                1 if self.direction == "UP" else -1,
            )
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        _invalidate_motion_quality_result(context)
        return {"FINISHED"}


class MB_OT_MergeCorrectionLayerDown(Operator):
    bl_idname = "motion_bridge.merge_correction_layer_down"
    bl_label = "下の修正レイヤーへ統合"
    bl_description = (
        "選択中のレイヤーを下のレイヤーへ劣化なしで統合します。"
        "同じ項目のキーが重なるなど危険な場合は実行しません"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        stack = _idle_correction_stack(context)
        if stack is None:
            return False
        return next(
            (
                index > 0
                for index, layer in enumerate(stack.correction_layers)
                if layer.layer_id == stack.active_layer_id
            ),
            False,
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        stack = _idle_correction_stack(context)
        if stack is None:
            return {"CANCELLED"}
        try:
            layer = merge_correction_layer_down(
                context,
                context.active_object,
                stack.active_layer_id,
            )
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        _invalidate_motion_quality_result(context)
        self.report({"INFO"}, f"統合しました：{layer.name}")
        return {"FINISHED"}


class MB_OT_CommitCorrectionsToBase(Operator):
    bl_idname = "motion_bridge.commit_corrections_to_base"
    bl_label = "修正をBaseへベイクして統合"
    bl_description = "表示中の修正を新しいBaseにベイクし、空の修正レイヤーを作ります。元のActionは履歴として保持します"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _idle_correction_stack(context) is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            base = commit_corrections_to_base(context, context.active_object)
        except (ReferenceError, RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        _invalidate_motion_quality_result(context)
        _mark_live_pose_dirty()
        self.report({"INFO"}, f"Baseへ統合しました：{base.name}")
        return {"FINISHED"}


class MB_OT_ToggleCorrectionLayerMute(Operator):
    bl_idname = "motion_bridge.toggle_correction_layer_mute"
    bl_label = "修正レイヤーを一時無効化"
    bl_description = "この修正レイヤーの効果だけを一時的に無効にします"
    bl_options = {"REGISTER", "UNDO"}

    layer_id: StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _idle_correction_stack(context) is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        stack = _idle_correction_stack(context)
        if stack is None:
            return {"CANCELLED"}
        layer = next(
            (
                candidate
                for candidate in stack.correction_layers
                if candidate.layer_id == self.layer_id
            ),
            None,
        )
        if layer is None:
            return {"CANCELLED"}
        muted = bool(layer.action.get("motion_bridge_layer_muted", layer.muted))
        try:
            set_correction_layer_muted(
                context.active_object,
                layer.layer_id,
                not muted,
            )
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        _invalidate_motion_quality_result(context)
        return {"FINISHED"}


class MB_OT_ToggleCorrectionLayerSolo(Operator):
    bl_idname = "motion_bridge.toggle_correction_layer_solo"
    bl_label = "修正レイヤーを単独表示"
    bl_description = "Baseとこの修正レイヤーだけを表示します"
    bl_options = {"REGISTER", "UNDO"}

    layer_id: StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _idle_correction_stack(context) is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        stack = _idle_correction_stack(context)
        if stack is None:
            return {"CANCELLED"}
        layer = next(
            (
                candidate
                for candidate in stack.correction_layers
                if candidate.layer_id == self.layer_id
            ),
            None,
        )
        if layer is None:
            return {"CANCELLED"}
        try:
            set_correction_layer_solo(
                context.active_object,
                None if layer.solo else layer.layer_id,
            )
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        _invalidate_motion_quality_result(context)
        return {"FINISHED"}


class MB_OT_RemoveCorrectionLayer(Operator):
    bl_idname = "motion_bridge.remove_correction_layer"
    bl_label = "修正レイヤーを削除"
    bl_description = "最低1枚を残して、選択中の修正レイヤーを削除します"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        stack = _idle_correction_stack(context)
        return bool(stack is not None and len(stack.correction_layers) > 1)

    def execute(self, context: bpy.types.Context) -> set[str]:
        stack = _idle_correction_stack(context)
        if stack is None:
            return {"CANCELLED"}
        try:
            layer = remove_correction_layer(
                context.active_object,
                stack.active_layer_id,
            )
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        _invalidate_motion_quality_result(context)
        self.report({"INFO"}, f"現在のレイヤー：{layer.name}")
        return {"FINISHED"}


class MB_OT_ImportSourceTake(Operator, ImportHelper):
    bl_idname = "motion_bridge.import_source_take"
    bl_label = "元モーションを読み込む"
    bl_description = "FBXまたはglTFのモーションを安全に読み込み、原本を保存します"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".fbx"
    filter_glob: StringProperty(
        default="*.fbx;*.gltf;*.glb",
        options={"HIDDEN"},
    )
    take_candidates_json: StringProperty(default="", options={"HIDDEN"})
    selected_take_id: EnumProperty(
        name="使用するモーション",
        description="このファイルから読み込むモーションを1つ選びます",
        items=_source_take_enum_items,
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return not _manual_mapping_changes_locked(context)

    def draw(self, _context: bpy.types.Context) -> None:
        if not self.take_candidates_json:
            return
        layout = self.layout
        layout.label(text="複数のモーションが見つかりました", icon="ACTION")
        layout.label(text="使用するものを1つ選んでください")
        layout.prop(self, "selected_take_id", text="モーション")

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.motion_bridge_session
        session_state = _capture_source_import_session(context, settings)

        def commit_source(result: SourceTakeImport) -> None:
            settings.source_armature = result.armature

        try:
            selected_take = (
                self.selected_take_id if self.take_candidates_json else None
            )
            result = import_source_take(
                context,
                self.filepath,
                selected_take_id=selected_take,
                _commit=commit_source,
            )
        except SourceTakeSelectionRequired as exception:
            if bpy.app.background:
                self.report({"ERROR"}, str(exception))
                return {"CANCELLED"}
            self.take_candidates_json = json.dumps(
                [
                    {
                        "actionName": candidate.action_name,
                        "frameEnd": candidate.frame_end,
                        "frameStart": candidate.frame_start,
                        "takeId": candidate.take_id,
                    }
                    for candidate in exception.candidates
                ],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            self.selected_take_id = exception.candidates[0].take_id
            return context.window_manager.invoke_props_dialog(self, width=440)
        except SourceTakeImportError as exception:
            _restore_source_import_session(context, settings, session_state)
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        except (OSError, ReferenceError, RuntimeError, TypeError, ValueError) as exception:
            _restore_source_import_session(context, settings, session_state)
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            f"{result.provenance.action_name}を読み込み、原本を保存しました / "
            f"ID {result.provenance.take_id[:12]}",
        )
        return {"FINISHED"}


class MB_OT_PopulateManualMapping(Operator):
    bl_idname = "motion_bridge.populate_manual_mapping"
    bl_label = "自動認識結果を反映"
    bl_description = "自動認識した結果を22個の手動ボーン欄へ反映します"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        settings = getattr(context.scene, "motion_bridge_session", None)
        return bool(
            settings is not None
            and settings.source_armature is not None
            and not _manual_mapping_changes_locked(context)
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.motion_bridge_session
        source = settings.source_armature
        source_property_snapshot = {
            key: (key in source, source.get(key))
            for key in (
                SOURCE_INSTANCE_ID_PROPERTY,
                _SOURCE_MAPPING_INSTANCE_ID_PROPERTY,
                _SOURCE_MAPPING_ACTION_CHECKSUM_PROPERTY,
            )
        }
        profile_snapshot = str(settings.manual_mapping_profile_json)
        registry_snapshot = str(settings.manual_mapping_profiles_json)
        mapping_snapshot = mapping_from_profile_values(
            _manual_profile_values(settings.manual_mapping)
        )
        try:
            mapping = infer_humanoid_mapping(source, "AUTO")
            _store_manual_mapping_profile(settings, mapping)
            _set_manual_humanoid_mapping(settings, mapping)
            settings.retarget_preset = "MANUAL"
        except (RuntimeError, TypeError, ValueError):
            for key, (existed, value) in source_property_snapshot.items():
                if existed:
                    source[key] = value
                elif key in source:
                    del source[key]
            settings.manual_mapping_profile_json = profile_snapshot
            settings.manual_mapping_profiles_json = registry_snapshot
            _set_manual_humanoid_mapping(settings, mapping_snapshot)
            settings.retarget_preset = "MANUAL"
            settings.beginner_retarget_error = (
                "ボーン名を自動認識できません。"
                "不足しているボーンを選んでください"
            )
            self.report({"ERROR"}, settings.beginner_retarget_error)
            return {"CANCELLED"}
        target = context.active_object
        if (
            target is not None
            and target.type == "ARMATURE"
            and target.get("motion_bridge_skeleton_id") == SKELETON_ID
        ):
            try:
                validate_humanoid_mapping(settings.source_armature, target, mapping)
            except (RuntimeError, TypeError, ValueError):
                settings.beginner_retarget_error = (
                    "認識できないボーンがあります。"
                    "不足しているボーンを下で選んでください"
                )
            else:
                settings.beginner_retarget_error = ""
        self.report({"INFO"}, f"{len(mapping)}個のボーン対応を手動欄へ反映しました")
        return {"FINISHED"}


class MB_OT_ClearManualMapping(Operator):
    bl_idname = "motion_bridge.clear_manual_mapping"
    bl_label = "すべてクリア"
    bl_description = "22個の手動ボーン欄をすべて空にします"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return not _manual_mapping_changes_locked(context)

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.motion_bridge_session
        if settings.source_armature is None:
            settings.manual_mapping_profile_json = ""
        else:
            try:
                _store_manual_mapping_profile(settings, {})
            except (RuntimeError, TypeError, ValueError) as exception:
                self.report({"ERROR"}, str(exception))
                return {"CANCELLED"}
        _set_manual_humanoid_mapping(settings, {})
        return {"FINISHED"}


class MB_OT_UseSourceFrameRange(Operator):
    bl_idname = "motion_bridge.use_source_frame_range"
    bl_label = "モーション全体を使用"
    bl_description = "シーンの開始・終了を読み込んだモーション全体へ合わせます"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        settings = getattr(context.scene, "motion_bridge_session", None)
        source = settings.source_armature if settings is not None else None
        return _source_take_frame_range(source) is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.motion_bridge_session
        source_range = _source_take_frame_range(settings.source_armature)
        if source_range is None:
            self.report({"ERROR"}, "読み込んだモーションのフレーム範囲を取得できません")
            return {"CANCELLED"}
        start, end = source_range
        scene = context.scene
        scene.frame_start = start
        scene.frame_end = end
        if scene.frame_current < start or scene.frame_current > end:
            scene.frame_set(start)
        self.report(
            {"INFO"}, f"シーン範囲をモーション全体へ合わせました：{start}-{end}"
        )
        return {"FINISHED"}


class MB_OT_BakeSourceToCanonical(Operator):
    bl_idname = "motion_bridge.bake_source_to_canonical"
    bl_label = "Canonicalへ変換"
    bl_description = (
        "Humanoidボーンを対応付け、シーン範囲の動きを編集用リグへ変換します"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        target = context.active_object
        settings = getattr(context.scene, "motion_bridge_session", None)
        source = settings.source_armature if settings is not None else None
        animation_data = target.animation_data if target is not None else None
        return bool(
            target is not None
            and target.type == "ARMATURE"
            and target.get("motion_bridge_skeleton_id") == SKELETON_ID
            and source is not None
            and source is not target
            and not target.get("motion_bridge_stack_id")
            and (
                animation_data is None
                or (animation_data.action is None and not animation_data.nla_tracks)
            )
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        target = context.active_object
        settings = context.scene.motion_bridge_session
        source = settings.source_armature
        if source is None:
            return {"CANCELLED"}
        try:
            if settings.retarget_preset == "MANUAL":
                mapping = _manual_humanoid_mapping(settings, bind_current=True)
            else:
                mapping = infer_humanoid_mapping(source, settings.retarget_preset)
            validate_humanoid_mapping(source, target, mapping)
        except (RuntimeError, TypeError, ValueError):
            settings.beginner_retarget_error = (
                "ボーンを自動認識できません。"
                "不足しているボーンを下で選んでください"
            )
            self.report({"ERROR"}, settings.beginner_retarget_error)
            return {"CANCELLED"}

        baked_action: bpy.types.Action | None = None
        target_had_animation_data = target.animation_data is not None
        target_rotation_mode = target.rotation_mode
        target_animation_data = target.animation_data
        binding_snapshot: dict[str, object] = {}
        if target_animation_data is not None:
            for attribute in (
                "action_slot",
                "action_slot_handle",
                "last_slot_identifier",
                "action_slot_handle_tweak_storage",
            ):
                if hasattr(target_animation_data, attribute):
                    binding_snapshot[attribute] = getattr(
                        target_animation_data,
                        attribute,
                    )
        profile_snapshot = str(settings.manual_mapping_profile_json)
        registry_snapshot = str(settings.manual_mapping_profiles_json)
        source_property_snapshot = {
            key: (key in source, source.get(key))
            for key in (
                _SOURCE_MAPPING_INSTANCE_ID_PROPERTY,
                _SOURCE_MAPPING_ACTION_CHECKSUM_PROPERTY,
            )
        }
        try:
            bake = (
                bake_archived_source_to_canonical
                if _uses_archived_source_bake(source)
                else bake_source_to_canonical
            )
            baked_action = bake(
                context,
                source,
                target,
                mapping,
                frame_start=context.scene.frame_start,
                frame_end=context.scene.frame_end,
            )
            animation_data = target.animation_data_create()
            animation_data.action = baked_action
            slot = action_slot_for_id(baked_action, target)
            if slot is not None:
                if hasattr(animation_data, "action_slot"):
                    animation_data.action_slot = slot
                else:
                    animation_data.action_slot_handle = slot.handle
            target.rotation_mode = "QUATERNION"
            baked_action.use_fake_user = False
            if settings.retarget_preset == "MANUAL":
                _store_manual_mapping_profile(settings, mapping)
        except (ReferenceError, RuntimeError, TypeError, ValueError) as exception:
            target_is_live = any(candidate is target for candidate in bpy.data.objects)
            action_is_live = baked_action is not None and any(
                candidate is baked_action for candidate in bpy.data.actions
            )
            if action_is_live:
                if target_is_live:
                    animation_data = target.animation_data
                    if (
                        animation_data is not None
                        and animation_data.action is baked_action
                    ):
                        animation_data.action = None
                baked_action.use_fake_user = False
                if baked_action.users == 0:
                    bpy.data.actions.remove(baked_action)
            if target_is_live:
                target.rotation_mode = target_rotation_mode
            settings.manual_mapping_profile_json = profile_snapshot
            settings.manual_mapping_profiles_json = registry_snapshot
            if any(candidate is source for candidate in bpy.data.objects):
                for key, (existed, value) in source_property_snapshot.items():
                    if existed:
                        source[key] = value
                    elif key in source:
                        del source[key]
            if target_is_live:
                if (
                    not target_had_animation_data
                    and target.animation_data is not None
                    and target.animation_data.action is None
                    and not target.animation_data.nla_tracks
                ):
                    target.animation_data_clear()
                elif target_animation_data is not None:
                    for attribute, value in binding_snapshot.items():
                        try:
                            setattr(target_animation_data, attribute, value)
                        except (AttributeError, ReferenceError, RuntimeError, TypeError):
                            pass
            settings.beginner_retarget_error = (
                "モーションを変換できませんでした。詳細設定で元データを確認してください"
            )
            self.report(
                {"ERROR"},
                f"{settings.beginner_retarget_error}: {exception}",
            )
            return {"CANCELLED"}
        settings.beginner_retarget_error = ""
        _invalidate_motion_quality_result(context)
        self.report(
            {"INFO"},
            f"{len(mapping)}本のボーンをCanonicalへ変換しました：{baked_action.name}",
        )
        return {"FINISHED"}


class MB_OT_RegenerateCanonicalBase(Operator):
    bl_idname = "motion_bridge.regenerate_canonical_base"
    bl_label = "保存した原本からBaseを作り直す"
    bl_description = "修正レイヤーを変えず、保存した元モーションとボーン対応からCanonicalのBaseを作り直します"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        armature = context.active_object
        stack = get_correction_layer_stack(armature)
        animation_data = armature.animation_data if armature is not None else None
        return bool(
            stack is not None
            and stack.archive_action.get(PROP_RETARGET_RECIPE)
            and animation_data is not None
            and animation_data.action is None
            and not getattr(animation_data, "use_tweak_mode", False)
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            result = regenerate_canonical_base(context, context.active_object)
        except (RuntimeError, TypeError, ValueError) as exception:
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        _invalidate_motion_quality_result(context)
        self.report({"INFO"}, f"Baseを作り直しました：{result.base_action.name}")
        return {"FINISHED"}


class MB_OT_ActivateCanonicalArmature(Operator):
    bl_idname = "motion_bridge.activate_canonical_armature"
    bl_label = "編集用リグを選択"
    bl_description = "Motion Bridgeで修正するCanonicalリグを選択します"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return bool(
            context.mode == "OBJECT" and len(_canonical_armatures(context)) == 1
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        candidates = _canonical_armatures(context)
        if len(candidates) != 1:
            self.report({"ERROR"}, "シーン内のCanonicalリグを1体選択してください")
            return {"CANCELLED"}
        canonical = candidates[0]
        for obj in tuple(context.selected_objects):
            obj.select_set(False)
        canonical.hide_set(False)
        canonical.hide_viewport = False
        canonical.select_set(True)
        context.view_layer.objects.active = canonical
        self.report({"INFO"}, f"編集用リグを選択しました：{canonical.name}")
        return {"FINISHED"}


def _workflow_facts(context: bpy.types.Context) -> WorkflowFacts:
    candidates = _canonical_armatures(context)
    active = context.active_object
    active_is_canonical = bool(
        active is not None
        and active.type == "ARMATURE"
        and active.get("motion_bridge_skeleton_id") == SKELETON_ID
    )
    settings = context.scene.motion_bridge_session
    source = settings.source_armature
    source_ready = bool(
        source is not None
        and source.type == "ARMATURE"
        and source is not active
        and source.name in context.scene.objects
    )
    source_frame_range = _source_take_frame_range(source) if source_ready else None
    scene_frame_range = (context.scene.frame_start, context.scene.frame_end)
    animation_data = active.animation_data if active_is_canonical else None
    stack = None
    stack_marker = bool(active_is_canonical and active.get("motion_bridge_stack_id"))
    unsupported_nla = bool(
        active_is_canonical and has_unsupported_nla_coexistence(active)
    )
    if active_is_canonical and stack_marker:
        try:
            validation = validate_layer_stack(active, check_content=False)
            stack = validation.stack if validation.is_valid else None
        except (ReferenceError, RuntimeError, TypeError, ValueError):
            stack = None
    unexpected_active_action = bool(
        stack is not None and _unexpected_active_action(context) is not None
    )
    editing = bool(active_is_canonical and is_correction_editing(active))
    edit_session = bool(active_is_canonical and has_correction_edit_session(active))
    envelope_preview = bool(
        active_is_canonical and active_correction_envelope_preview(active) is not None
    )
    selected_pose_bones = tuple(getattr(context, "selected_pose_bones", ()) or ())
    unrelated_tweak = bool(
        getattr(context.scene, "is_nla_tweakmode", False)
        or (animation_data is not None and animation_data.use_tweak_mode)
    )
    return WorkflowFacts(
        canonical_count=len(candidates),
        active_is_canonical=active_is_canonical,
        source_ready=source_ready,
        source_frame_range_known=source_frame_range is not None,
        scene_matches_source_range=(
            source_frame_range is None or scene_frame_range == source_frame_range
        ),
        has_motion=bool(
            stack is not None
            or stack_marker
            or (animation_data is not None and animation_data.action is not None)
        ),
        stack_marker=stack_marker,
        stack_valid=stack is not None,
        initial_base_action_private=(
            stack_marker
            or animation_data is None
            or animation_data.action is None
            or action_is_unshared_for_armature(active, animation_data.action)
        ),
        correction_actions_private=(
            stack is None or correction_actions_are_unshared(active)
        ),
        corrections_enabled=(
            stack is None or corrections_preview_enabled(active)
        ),
        unsupported_active_action=unexpected_active_action,
        unsupported_nla=unsupported_nla,
        edit_session=edit_session,
        editing=editing,
        unrelated_tweak_mode=unrelated_tweak,
        envelope_preview=envelope_preview,
        selected_pose_bones=len(selected_pose_bones),
    )


def _draw_beginner_live_card(
    layout: bpy.types.UILayout,
    settings: bpy.types.PropertyGroup,
) -> None:
    client = _control_client
    control_state = client.state.value if client is not None else None
    live_state = resolve_live_guide_state(
        requested=_pending_control_config is not None,
        control_state=control_state,
        pose_session_active=_live_session is not None,
    )
    box = layout.box()
    box.label(text="Unityで確認（いつでも使えます）", icon="SCENE_DATA")
    if live_state is LiveGuideState.STREAMING:
        box.label(text="接続済み：修正がUnityへ反映されています", icon="CHECKMARK")
        controls = box.row(align=True)
        controls.operator(
            MB_OT_ToggleLivePause.bl_idname,
            text="一時停止",
            icon="PAUSE",
        )
        controls.operator(MB_OT_StopLivePose.bl_idname, text="接続を終了", icon="X")
    elif live_state is LiveGuideState.PAUSED:
        box.label(text="Unityへの反映を一時停止しています", icon="PAUSE")
        controls = box.row(align=True)
        controls.operator(
            MB_OT_ToggleLivePause.bl_idname,
            text="再開",
            icon="PLAY",
        )
        controls.operator(MB_OT_StopLivePose.bl_idname, text="接続を終了", icon="X")
    elif live_state is LiveGuideState.CONNECTING:
        box.label(text="Unityのプレビュー待受を探しています", icon="TIME")
        box.label(text=friendly_live_error(_last_live_error), icon="INFO")
        box.operator(MB_OT_StopLivePose.bl_idname, text="接続待ちをやめる", icon="X")
    else:
        box.label(text="先にUnityで「プレビュー待受を開始」します", icon="INFO")
        box.operator(
            MB_OT_StartLivePose.bl_idname,
            text="Unityへ接続",
            icon="PLAY",
        )
        if _last_live_error:
            box.label(text=friendly_live_error(_last_live_error), icon="ERROR")

    details = box.row()
    details.prop(
        settings,
        "beginner_live_details",
        text="接続の詳細",
        icon="TRIA_DOWN" if settings.beginner_live_details else "TRIA_RIGHT",
        emboss=False,
    )
    if settings.beginner_live_details:
        detail_box = box.box()
        detail_box.label(text="通常は初期値のままで使えます", icon="INFO")
        detail_box.prop(settings, "host", text="Unityの場所")
        detail_box.prop(settings, "control_port", text="TCPポート")
        detail_box.prop(settings, "session_id", text="Session ID")
        rate = detail_box.row(align=True)
        rate.prop(settings, "send_hz", text="送信頻度")
        rate.prop(settings, "root_mode", text="ルート移動")
        if _last_live_error:
            detail_box.label(text=_last_live_error, icon="CONSOLE")


def _draw_beginner_edit_tools(
    context: bpy.types.Context,
    box: bpy.types.UILayout,
    settings: bpy.types.PropertyGroup,
) -> None:
    armature = context.active_object
    layer = active_correction_edit_layer(armature)
    preview = active_correction_envelope_preview(armature)
    box.label(
        text=f"修正中：{layer.name if layer is not None else 'Correction'}",
        icon="POSE_HLT",
    )
    box.label(
        text="下の欄は選択ボーンの修正キーだけを表示します",
        icon="ACTION",
    )
    curve_node = _find_envelope_curve_node(settings)
    if preview is not None:
        box.label(text="カーブを動かすとBlenderとUnityへ即時反映されます", icon="PLAY")
        box.prop(settings, "correction_envelope_preset", text="カーブ形状")
        if curve_node is not None:
            box.template_curve_mapping(
                curve_node,
                "mapping",
                type="NONE",
                levels=False,
                brush=False,
                use_negative_slope=True,
                show_tone=False,
                show_presets=False,
            )
        else:
            box.label(text="補正カーブは操作開始時に準備されます", icon="INFO")
        box.label(
            text=(
                f"対象フレーム：{preview.envelope_range.scene_start:g} - "
                f"{preview.envelope_range.scene_end:g}"
            ),
            icon="PREVIEW_RANGE",
        )
        commit = box.row(align=True)
        commit.scale_y = 1.4
        commit.operator(
            MB_OT_ApplyCorrectionEnvelope.bl_idname,
            text="この結果で確定",
            icon="CHECKMARK",
        )
        commit.operator(
            MB_OT_CancelCorrectionEnvelope.bl_idname,
            text="開始前へ戻す",
            icon="X",
        )
        return

    selected = tuple(getattr(context, "selected_pose_bones", ()) or ())
    selected_names = tuple(bone.name for bone in selected)
    box.label(text=f"選択中：{', '.join(selected_names) or 'なし'}", icon="BONE_DATA")
    box.label(
        text="最初は空で正常です。記録すると黄色の◆が表示されます",
        icon="INFO",
    )
    box.label(text="1. フレームを選ぶ  2. ボーンを選ぶ  3. Rで回す")

    hips_only = bool(selected_names) and set(selected_names) == {"Hips"}
    location = box.row()
    location.enabled = hips_only
    location.prop(
        settings,
        "correction_include_location",
        text="Hipsの位置移動も記録",
    )
    if not hips_only:
        box.label(text="位置移動はHipsを1本だけ選んだときに使えます", icon="INFO")
    include_location = bool(settings.correction_include_location and hips_only)

    key = box.row()
    key.scale_y = 1.4
    insert = key.operator(
        MB_OT_InsertCorrectionPoseKeys.bl_idname,
        text="4. このフレームに修正を記録",
        icon="KEY_HLT",
    )
    insert.include_location = include_location
    insert.neutral_padding = settings.correction_blend_frames
    insert.interpolation = settings.correction_interpolation

    envelope = box.box()
    envelope.label(text="複数フレームへ自然になじませる", icon="FCURVE")
    envelope.label(text="0＝元の動き / 1＝今の修正を完全適用")
    range_toggle = envelope.row()
    range_toggle.prop(
        context.scene,
        "use_preview_range",
        text="フレーム範囲を数値で指定",
    )
    if context.scene.use_preview_range:
        frame_range = envelope.row(align=True)
        frame_range.prop(context.scene, "frame_preview_start", text="開始")
        frame_range.prop(context.scene, "frame_preview_end", text="終了")
        envelope.label(text="現在フレームをこの範囲内へ移動してください", icon="INFO")
    else:
        envelope.label(text="選択キーがなければ上の範囲指定をONにします", icon="INFO")
    envelope.prop(settings, "correction_envelope_preset", text="カーブ形状")
    if curve_node is not None:
        envelope.template_curve_mapping(
            curve_node,
            "mapping",
            type="NONE",
            levels=False,
            brush=False,
            use_negative_slope=True,
            show_tone=False,
            show_presets=False,
        )
    else:
        envelope.label(text="補正カーブは操作開始時に準備されます", icon="INFO")
    start = envelope.row()
    start.scale_y = 1.3
    add_pose = start.operator(
        MB_OT_BeginCorrectionEnvelope.bl_idname,
        text="範囲へ自然になじませる",
        icon="PREVIEW_RANGE",
    )
    add_pose.mode = "POSE_OFFSET"
    reedit_row = envelope.row()
    reedit_row.enabled = bool(
        layer is not None and committed_pose_envelope_available(layer.action)
    )
    reedit = reedit_row.operator(
        MB_OT_BeginCorrectionEnvelope.bl_idname,
        text="確定したカーブを再編集",
        icon="GRAPH",
    )
    reedit.mode = "REEDIT"
    envelope.label(text="確定時と同じボーンを選択して使います", icon="INFO")

    details = box.row()
    details.prop(
        settings,
        "beginner_edit_details",
        text="修正の詳細",
        icon="TRIA_DOWN" if settings.beginner_edit_details else "TRIA_RIGHT",
        emboss=False,
    )
    if settings.beginner_edit_details:
        detail_box = box.box()
        blend = detail_box.row(align=True)
        blend.prop(settings, "correction_blend_frames", text="前後のなじませ")
        blend.prop(settings, "correction_interpolation", text="補間")
        cleanup = detail_box.row(align=True)
        reset = cleanup.operator(
            MB_OT_ResetCorrectionPoseKeys.bl_idname,
            text="現在の修正を0へ戻す",
            icon="LOOP_BACK",
        )
        reset.include_location = include_location
        reset.interpolation = settings.correction_interpolation
        remove = cleanup.operator(
            MB_OT_RemoveCorrectionPoseKeys.bl_idname,
            text="現在の修正キーを削除",
            icon="KEY_DEHLT",
        )
        remove.include_location = include_location
        reweight = detail_box.operator(
            MB_OT_BeginCorrectionEnvelope.bl_idname,
            text="既存キーの強さをカーブで変更",
            icon="GRAPH",
        )
        reweight.mode = "REWEIGHT"
        view = detail_box.row(align=True)
        view.prop(settings, "rig_visual_source_visible", text="元リグも表示")
        view.prop(settings, "rig_visual_show_names", text="ボーン名")

    finish = box.row()
    finish.operator(
        MB_OT_ExitCorrectionEdit.bl_idname,
        text="修正作業を終了",
        icon="CHECKMARK",
    )


def _draw_beginner_workflow(
    context: bpy.types.Context,
    layout: bpy.types.UILayout,
    settings: bpy.types.PropertyGroup,
) -> WorkflowFacts:
    facts = _workflow_facts(context)
    stage = resolve_guide_stage(facts)
    box = layout.box()
    box.label(text="モーション修正", icon="ARMATURE_DATA")
    if stage is GuideStage.NO_CANONICAL:
        box.alert = True
        box.label(text="編集用リグが見つかりません", icon="ERROR")
        box.label(text="配布の mb_canonical_humanoid_v1.blend を開きます")
        box.label(text="原本ではなく、最初に「名前を付けて保存」してください")
    elif stage is GuideStage.SELECT_CANONICAL:
        box.label(text="1. 色付きの編集用リグを選択します", icon="INFO")
        if facts.canonical_count == 1 and context.mode == "OBJECT":
            select = box.row()
            select.scale_y = 1.4
            select.operator(
                MB_OT_ActivateCanonicalArmature.bl_idname,
                text="編集用リグを選択",
                icon="RESTRICT_SELECT_OFF",
            )
        elif facts.canonical_count > 1:
            box.label(text="複数あります。編集用リグを1体選んでください")
        else:
            box.label(text="TabでObject Modeへ戻してから選択してください")
    elif stage is GuideStage.IMPORT_SOURCE:
        box.label(text="1. 修正したいモーションを読み込みます", icon="INFO")
        action = box.row()
        action.scale_y = 1.5
        action.operator(
            MB_OT_ImportSourceTake.bl_idname,
            text="モーションFBXを読み込む",
            icon="IMPORT",
        )
        box.label(text="FBX / glTF / GLBに対応しています")
    elif stage is GuideStage.SET_SOURCE_RANGE:
        source = settings.source_armature
        source_range = _source_take_frame_range(source)
        box.label(text="2. モーション全体を使う範囲へ合わせます", icon="INFO")
        box.label(
            text=f"読込済み：{source.name if source is not None else '-'}",
            icon="FILE_TICK",
        )
        if source_range is not None:
            start, end = source_range
            box.label(text=f"モーション全体：{start} - {end} フレーム")
            box.label(
                text=(
                    f"現在の範囲：{context.scene.frame_start} - "
                    f"{context.scene.frame_end}（このままでは途中で切れます）"
                ),
                icon="ERROR",
            )
            action = box.row()
            action.scale_y = 1.5
            action.operator(
                MB_OT_UseSourceFrameRange.bl_idname,
                text=f"モーション全体（{start} - {end}）を使用",
                icon="PREVIEW_RANGE",
            )
    elif stage is GuideStage.BAKE_SOURCE:
        source = settings.source_armature
        box.label(text="2. 読み込んだ動きを編集用リグへ変換します", icon="INFO")
        box.label(
            text=f"読込済み：{source.name if source is not None else '-'}",
            icon="FILE_TICK",
        )
        box.prop(settings, "retarget_preset", text="ボーン名の認識")
        box.label(text="通常は「自動」のままで進めます")
        retarget_error = str(settings.beginner_retarget_error)
        if retarget_error:
            warning = box.box()
            warning.alert = True
            warning.label(text=retarget_error, icon="ERROR")
        if retarget_error and settings.retarget_preset != "MANUAL":
            repair = box.row()
            repair.scale_y = 1.5
            repair.operator(
                MB_OT_PopulateManualMapping.bl_idname,
                text="認識結果を確認して不足分を選ぶ",
                icon="FILE_REFRESH",
            )
        else:
            if settings.retarget_preset == "MANUAL":
                mapping_profile = settings.manual_mapping
                missing_fields = tuple(
                    field
                    for field in HUMANOID_MAPPING_FIELDS
                    if field.required
                    and not getattr(mapping_profile, field.property_name)
                )
                if retarget_error:
                    # A duplicate or hierarchy error can be caused by an
                    # optional shoulder/toe/upper-chest mapping. When no
                    # required bone is missing, expose every mapping so the
                    # Beginner recovery path never becomes a retry loop.
                    fields_to_show = missing_fields or HUMANOID_MAPPING_FIELDS
                    box.label(
                        text=(
                            "認識できなかったボーンだけ選んでください"
                            if missing_fields
                            else "ボーンの対応を確認して直してください"
                        )
                    )
                    for field in fields_to_show:
                        box.prop_search(
                            mapping_profile,
                            field.property_name,
                            source.data,
                            "bones",
                            text=field.human_bone,
                        )
                elif not retarget_error:
                    repair = box.row()
                    repair.operator(
                        MB_OT_PopulateManualMapping.bl_idname,
                        text="このリグのボーンを自動認識",
                        icon="FILE_REFRESH",
                    )
            action = box.row()
            action.scale_y = 1.5
            action.operator(
                MB_OT_BakeSourceToCanonical.bl_idname,
                text="編集用リグへ変換",
                icon="ACTION",
            )
        box.label(
            text=f"対象：シーンの {context.scene.frame_start} - {context.scene.frame_end} フレーム"
        )
    elif stage is GuideStage.CREATE_CORRECTION:
        box.label(text="3. 元モーションを残したまま修正層を作ります", icon="INFO")
        action = box.row()
        action.scale_y = 1.5
        action.operator(
            MB_OT_CreateCorrectionLayer.bl_idname,
            text="修正レイヤーを作成",
            icon="ADD",
        )
    elif stage is GuideStage.UNSUPPORTED_NLA:
        box.alert = True
        box.label(text="別のアニメーションレイヤーが見つかりました", icon="ERROR")
        box.label(text="安全のため停止しました。既存の内容は変更していません")
        box.label(text="Motion Bridge専用の作業ファイルで続けてください")
    elif stage is GuideStage.UNSUPPORTED_ACTION:
        box.alert = True
        box.label(text="別のアニメーションが修正の上に重なっています", icon="ERROR")
        box.label(text="データを残したまま割り当てだけ外すと続けられます")
        action = box.row()
        action.scale_y = 1.5
        action.operator(
            MB_OT_DetachUnexpectedActiveAction.bl_idname,
            text="別のアニメーションを外して続ける",
            icon="UNLINKED",
        )
    elif stage is GuideStage.BROKEN_STACK:
        box.alert = True
        box.label(text="修正レイヤーを正しく読み込めません", icon="ERROR")
        box.label(text="自動で作り直さず、下の「詳細設定」で状態を確認します")
    elif stage is GuideStage.SHARED_BASE:
        box.alert = True
        box.label(text="元のシーンと複製側が同じモーションを使っています", icon="LOCKED")
        box.label(text="安全のため、修正の準備は行っていません")
        box.label(text="不要な複製シーンを削除してから")
        box.label(text="編集したいシーンで続けてください")
    elif stage is GuideStage.SHARED_CORRECTION:
        box.alert = True
        box.label(text="元のシーンと複製側を同時には編集できません", icon="LOCKED")
        box.label(text="安全のため、修正データは変更していません")
        if facts.editing:
            box.operator(
                MB_OT_ExitCorrectionEdit.bl_idname,
                text="修正キーを残して通常状態へ戻す",
                icon="CHECKMARK",
            )
        box.label(text="不要な複製シーンを削除してから")
        box.label(text="編集したいシーンで続けてください")
    elif stage is GuideStage.BEFORE_PREVIEW:
        box.label(text="元の動きを表示しています", icon="HIDE_OFF")
        box.label(text="修正を続けるには修正後の表示へ戻します")
        action = box.row()
        action.scale_y = 1.5
        restore = action.operator(
            MB_OT_SetCorrectionPreview.bl_idname,
            text="修正後へ戻して続ける",
            icon="HIDE_OFF",
        )
        restore.enabled = True
    elif stage is GuideStage.RECOVER_EDIT:
        box.alert = True
        box.label(text="Undoなどで修正モードの内部状態が変わりました", icon="INFO")
        box.operator(
            MB_OT_RecoverCorrectionEdit.bl_idname,
            text="キーを残して通常状態へ戻す",
            icon="FILE_REFRESH",
        )
        box.label(text="戻したあと「モーション修正を開始」で続けられます")
    elif stage is GuideStage.BLOCKED_TWEAK:
        box.alert = True
        stale_layer = stale_correction_tweak_layer(context.active_object)
        if stale_layer is not None:
            box.label(text="再起動前の修正モードが残っています", icon="INFO")
            box.label(text=f"修正キーは保存済みです：{stale_layer.name}")
            action = box.row()
            action.scale_y = 1.5
            action.operator(
                MB_OT_ExitSavedCorrectionTweak.bl_idname,
                text="キーを残して通常状態へ戻す",
                icon="FILE_REFRESH",
            )
            box.label(text="戻したあと「モーション修正を開始」で続けられます")
        else:
            box.label(text="別のアニメーション編集モードが開いています", icon="ERROR")
            box.label(text="先にその編集を終了してください")
    elif stage is GuideStage.READY_EDIT:
        stack = get_correction_layer_stack(context.active_object)
        box.label(text="4. 色付きボーンを使って動きを修正します", icon="INFO")
        if stack is not None:
            box.label(
                text=f"修正レイヤー：{stack.correction_action.name}", icon="ACTION"
            )
        box.label(text="大きい元リグは比較用、色付きリグが修正対象です")
        box.label(
            text="下の欄が空なのは正常です（元モーションは保護中）",
            icon="INFO",
        )
        box.label(text="開始後に修正を記録すると、修正キーだけ表示されます")
        action = box.row()
        action.scale_y = 1.5
        action.operator(
            MB_OT_EnterCorrectionEdit.bl_idname,
            text="モーション修正を開始（キー編集を表示）",
            icon="POSE_HLT",
        )
        pose_transfer = box.row(align=True)
        pose_transfer.operator(
            MB_OT_CopySourcePose.bl_idname,
            text="元モーションをコピー",
            icon="DUPLICATE",
        )
        pose_transfer.operator(
            MB_OT_PasteSourcePose.bl_idname,
            text="現在フレームへ貼り付け",
            icon="IMPORT",
        )
        box.operator(
            MB_OT_CommitCorrectionsToBase.bl_idname,
            text="修正をBaseへベイクして統合",
            icon="ACTION",
        )
    elif stage is GuideStage.SELECT_BONE:
        box.label(text="色付きボーンをクリックして選択します", icon="BONE_DATA")
        box.label(text="R＝回転 / G＝移動（Hipsのみ）")
        box.prop(settings, "rig_visual_source_visible", text="比較用の元リグを表示")
        box.operator(
            MB_OT_ExitCorrectionEdit.bl_idname,
            text="修正作業を終了",
            icon="CHECKMARK",
        )
    else:
        _draw_beginner_edit_tools(context, box, settings)
    return facts


def _draw_beginner_comparison(
    context: bpy.types.Context,
    layout: bpy.types.UILayout,
    facts: WorkflowFacts,
) -> None:
    if not facts.stack_valid:
        return
    comparison = layout.box()
    comparison.label(text="元の動きと比べる", icon="HIDE_OFF")
    preview_enabled = corrections_preview_enabled(context.active_object)
    controls = comparison.row(align=True)
    controls.enabled = not (
        facts.editing
        or facts.edit_session
        or facts.envelope_preview
        or facts.unsupported_active_action
    )
    before = controls.operator(
        MB_OT_SetCorrectionPreview.bl_idname,
        text="元の動き",
        depress=not preview_enabled,
    )
    before.enabled = False
    after = controls.operator(
        MB_OT_SetCorrectionPreview.bl_idname,
        text="修正後",
        depress=preview_enabled,
    )
    after.enabled = True
    if not controls.enabled:
        comparison.label(text="修正作業を終了すると比較できます")


def _motion_quality_preview_rows(
    settings: bpy.types.PropertyGroup,
) -> tuple[dict[str, object], ...]:
    try:
        decoded = json.loads(str(settings.motion_quality_issue_preview_json))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(row for row in decoded if isinstance(row, dict))


def _draw_motion_quality_metrics(
    layout: bpy.types.UILayout,
    report_json: str,
) -> None:
    try:
        report = json.loads(report_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        layout.label(text="確認結果JSONを読み込めません", icon="ERROR")
        return
    metrics = report.get("metrics", ()) if isinstance(report, dict) else ()
    if not isinstance(metrics, list):
        return
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        try:
            count = int(metric.get("count", 0))
        except (TypeError, ValueError, OverflowError):
            continue
        if count <= 0:
            continue
        name = str(metric.get("name", ""))
        label = _MOTION_QUALITY_METRIC_LABELS.get(name, name)
        maximum = metric.get("max")
        p95 = metric.get("p95")
        unit = str(metric.get("unit", ""))
        if not isinstance(maximum, (int, float)) or not isinstance(p95, (int, float)):
            continue
        layout.label(
            text=f"{label}：最大 {maximum:.4g} / 95% {p95:.4g} {unit}"
        )


def _draw_beginner_motion_quality(
    context: bpy.types.Context,
    layout: bpy.types.UILayout,
    settings: bpy.types.PropertyGroup,
    facts: WorkflowFacts,
) -> None:
    if not facts.stack_valid:
        return
    box = layout.box()
    box.label(text="モーションを確認", icon="VIEWZOOM")
    state = _motion_quality_scan_state
    armature = context.active_object
    if state is not None:
        if state.phase == "ANALYZING":
            box.label(text="見つかった動きを整理しています…", icon="TIME")
        elif state.phase == "SERIALIZING":
            percent = min(100, round(float(state.analysis_progress) * 100.0))
            box.label(text=f"確認結果を準備中… {percent}%", icon="TIME")
        else:
            total = max(1, int(state.sample_count))
            percent = min(100, round((int(state.sampled_count) / total) * 100.0))
            box.label(text=f"動きを確認中… {percent}%", icon="TIME")
        box.operator(
            MB_OT_CancelMotionQualityScan.bl_idname,
            text="確認を中止",
            icon="X",
        )
        return

    if facts.editing or facts.edit_session or facts.envelope_preview:
        box.label(text="修正作業を終了すると確認できます", icon="INFO")
        return

    try:
        range_spec = _motion_quality_range_spec(context, armature)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as error:
        warning = box.row()
        warning.alert = True
        warning.label(text=str(error), icon="ERROR")
        bounded = box.operator(
            MB_OT_ScanMotionQuality.bl_idname,
            text="現在位置付近を確認",
            icon="VIEWZOOM",
        )
        bounded.limit_to_current_window = True
        return
    frame_start, frame_end = range_spec.expanded_integer_bounds()
    sample_count = range_spec.integer_sample_count
    if sample_count > MAX_MOTION_SAMPLES:
        box.label(text="モーション全体が長いため、確認範囲を絞ります", icon="INFO")
        bounded = box.operator(
            MB_OT_ScanMotionQuality.bl_idname,
            text="現在位置付近を確認",
            icon="VIEWZOOM",
        )
        bounded.limit_to_current_window = True
        box.label(
            text=f"現在位置を中心に最大{MAX_MOTION_SAMPLES:,}フレームを確認します"
        )
        return
    if range_spec.source is RangeSource.PREVIEW_RANGE:
        box.label(
            text=f"対象：プレビュー範囲 {frame_start}〜{frame_end}",
            icon="PREVIEW_RANGE",
        )

    motion_current = _motion_quality_result_is_motion_current(context, armature)
    range_current = _motion_quality_result_is_current(context, armature)
    if motion_current:
        summary = str(settings.motion_quality_summary)
        if summary == "NO_ISSUES":
            box.label(text="前回の確認：問題は見つかりませんでした", icon="CHECKMARK")
        elif summary == "FIX_RECOMMENDED":
            warning = box.row()
            warning.alert = True
            warning.label(
                text="前回の確認：修正をおすすめする区間があります",
                icon="ERROR",
            )
        else:
            box.label(text="前回の確認：目で確認したい区間があります", icon="INFO")

        rows = _motion_quality_preview_rows(settings)
        displayed_count = 0
        for issue in rows[:3]:
            try:
                frame_start = int(issue["frameStart"])
                frame_end = int(issue["frameEnd"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            subject = str(issue.get("subject", "モーション"))
            title = str(issue.get("title", "動きを目で確認"))
            issue_row = box.row(align=True)
            issue_row.alert = str(issue.get("severity", "")).upper() == "FIX"
            issue_row.label(
                text=f"{subject}：{frame_start}〜{frame_end}（{title}）"
            )
            focus = issue_row.operator(
                MB_OT_FocusMotionQualityIssue.bl_idname,
                text="表示",
                icon="RESTRICT_VIEW_OFF",
            )
            focus.frame_start = frame_start
            focus.frame_end = frame_end
            displayed_count += 1
        total_issue_count = max(
            len(rows),
            int(getattr(settings, "motion_quality_issue_count", len(rows))),
        )
        remaining_count = max(0, total_issue_count - displayed_count)
        if remaining_count:
            box.label(text=f"ほか {remaining_count} 区間", icon="INFO")
        if not range_current:
            box.label(text="表示中の範囲はまだ確認していません", icon="INFO")
        box.operator(
            MB_OT_ScanMotionQuality.bl_idname,
            text=(
                (
                    "この範囲をもう一度確認"
                    if range_current
                    else "この範囲を確認"
                )
                if range_spec.source is RangeSource.PREVIEW_RANGE
                else "もう一度確認"
            ),
            icon="FILE_REFRESH",
        )
        return

    if settings.motion_quality_report_json:
        box.label(text="モーションまたは確認範囲が変わりました", icon="INFO")
    else:
        box.label(text="足滑りや急な動きを自動で探します", icon="INFO")
    box.operator(
        MB_OT_ScanMotionQuality.bl_idname,
        text=(
            "この範囲を確認"
            if range_spec.source is RangeSource.PREVIEW_RANGE
            else "モーションを確認"
        ),
        icon="VIEWZOOM",
    )


def _draw_beginner_auto_correction(
    context: bpy.types.Context,
    layout: bpy.types.UILayout,
    settings: bpy.types.PropertyGroup,
    facts: WorkflowFacts,
) -> None:
    if not facts.stack_valid:
        return
    box = layout.box()
    box.label(text="ワンクリック補正", icon="MODIFIER")
    box.label(text="必要なものだけ使い、Foot Lockは最後に実行します")

    controls = box.column(align=True)
    controls.enabled = _auto_correction_poll(context)
    range_spec: RangeSpec | None = None
    try:
        range_spec = _motion_quality_range_spec(context, context.active_object)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as error:
        warning = box.row()
        warning.alert = True
        warning.label(text=str(error), icon="ERROR")
        controls.enabled = False
    if range_spec is not None:
        start, end = range_spec.expanded_integer_bounds()
        if range_spec.source is RangeSource.PREVIEW_RANGE:
            box.label(text=f"対象：プレビュー範囲 {start}〜{end}", icon="PREVIEW_RANGE")
        if range_spec.integer_sample_count > AUTO_CORRECTION_MAX_FRAMES:
            controls.enabled = False
            warning = box.row()
            warning.alert = True
            warning.label(
                text=f"Pキーで{AUTO_CORRECTION_MAX_FRAMES}フレーム以内に範囲を絞ってください",
                icon="ERROR",
            )

    smooth = controls.row()
    smooth.scale_y = 1.35
    smooth.operator(
        MB_OT_AutoSmoothMotion.bl_idname,
        text="1. 揺れをなめらかに",
        icon="FCURVE",
    )
    controls.label(text="全身の細かな回転ノイズを自然にならします")
    joints = controls.row()
    joints.scale_y = 1.35
    joints.operator(
        MB_OT_AutoJointLimits.bl_idname,
        text="2. 関節の曲がりすぎを直す",
        icon="CONSTRAINT_BONE",
    )
    controls.label(text="標準範囲を超えた回転だけを柔らかく戻します")
    foot = controls.row()
    foot.scale_y = 1.35
    foot.operator(
        MB_OT_AutoFootLock.bl_idname,
        text="3. 足滑りを固定",
        icon="CON_FLOOR",
    )
    controls.label(text="接地中の左右の足を検出して最後に固定します")

    details_toggle = box.row()
    details_toggle.prop(
        settings,
        "beginner_auto_details",
        text="設定",
        icon="TRIA_DOWN" if settings.beginner_auto_details else "TRIA_RIGHT",
        emboss=False,
    )
    if settings.beginner_auto_details:
        details = box.column(align=True)
        details.prop(settings, "auto_smoothing_strength", expand=True)
        details.prop(settings, "auto_foot_lock_side", expand=True)
        details.label(text="各操作は元モーションを残して新しい修正レイヤーへ保存します")

    if not facts.correction_actions_private:
        box.label(text="別シーンと共有中のため自動補正できません", icon="LOCKED")
    elif not facts.corrections_enabled:
        box.label(text="「修正後」へ戻すと自動補正できます", icon="INFO")
    elif facts.editing or facts.edit_session or facts.envelope_preview:
        box.label(text="開いている修正作業を終了すると実行できます", icon="INFO")


class MB_PT_GettingStarted(Panel):
    bl_idname = "MB_PT_getting_started"
    bl_label = "はじめてガイド"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Motion Bridge"
    bl_order = 0

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        settings = context.scene.motion_bridge_session
        layout.label(text="大きなボタンを上から順に進めます", icon="INFO")
        facts = _draw_beginner_workflow(context, layout, settings)
        _draw_beginner_comparison(context, layout, facts)
        _draw_beginner_motion_quality(context, layout, settings, facts)
        _draw_beginner_auto_correction(context, layout, settings, facts)
        _draw_beginner_live_card(layout, settings)
        if facts.has_motion and facts.active_is_canonical:
            export = layout.box()
            export.label(text="FBX書き出し", icon="EXPORT")
            if facts.envelope_preview:
                export.label(text="先にカーブ結果を「確定」か「開始前へ戻す」にします")
            elif facts.editing or facts.edit_session:
                export.label(text="先に「修正作業を終了」します")
            elif facts.stack_valid:
                action = export.row()
                action.scale_y = 1.4
                action.operator(
                    MB_OT_ExportCorrectedFbx.bl_idname,
                    text="修正済みFBXを書き出す",
                    icon="EXPORT",
                )
            else:
                export.operator(
                    MB_OT_ExportCanonicalActionFbx.bl_idname,
                    text="現在のモーションをFBXで書き出す",
                    icon="EXPORT",
                )


class MB_PT_Session(Panel):
    bl_idname = "MB_PT_session"
    bl_label = "詳細設定"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Motion Bridge"
    bl_order = 100
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        settings = context.scene.motion_bridge_session
        workflow_facts = _workflow_facts(context)
        workflow_stage = resolve_guide_stage(workflow_facts)
        if workflow_stage in {
            GuideStage.UNSUPPORTED_ACTION,
            GuideStage.UNSUPPORTED_NLA,
            GuideStage.BROKEN_STACK,
            GuideStage.BLOCKED_TWEAK,
        }:
            status = layout.box()
            status.alert = True
            if workflow_stage is GuideStage.UNSUPPORTED_ACTION:
                status.label(
                    text="別のアニメーションが修正の上に重なっています",
                    icon="ERROR",
                )
                status.operator(
                    MB_OT_DetachUnexpectedActiveAction.bl_idname,
                    text="データを残して割り当てだけ外す",
                    icon="UNLINKED",
                )
            elif workflow_stage is GuideStage.UNSUPPORTED_NLA:
                status.label(
                    text="所有外のアニメーションレイヤーがあるため停止中です",
                    icon="ERROR",
                )
                status.label(text="Motion Bridge専用の作業ファイルで続けてください")
            elif workflow_stage is GuideStage.BROKEN_STACK:
                status.label(
                    text="修正レイヤーの構成を正しく読み込めません",
                    icon="ERROR",
                )
            else:
                status.label(
                    text="別のアニメーション編集モードを先に終了してください",
                    icon="ERROR",
                )
        if workflow_facts.stack_valid:
            quality = layout.box()
            quality.label(text="モーション確認データ", icon="VIEWZOOM")
            if settings.motion_quality_report_json:
                quality_armature = (
                    context.active_object
                    if workflow_facts.active_is_canonical
                    else None
                )
                if _motion_quality_result_is_current(context, quality_armature):
                    quality.label(text="現在の範囲の確認結果", icon="CHECKMARK")
                elif _motion_quality_result_is_motion_current(
                    context,
                    quality_armature,
                ):
                    quality.label(
                        text="以前の確認範囲の結果（表示範囲は未確認）",
                        icon="INFO",
                    )
                else:
                    quality.label(
                        text="以前の結果（モーション変更後は再確認が必要）",
                        icon="ERROR",
                    )
                quality.label(
                    text=(
                        f"{settings.motion_quality_frame_start} - "
                        f"{settings.motion_quality_frame_end} / "
                        f"{settings.motion_quality_sample_count}サンプル"
                    )
                )
                _draw_motion_quality_metrics(
                    quality,
                    settings.motion_quality_report_json,
                )
                quality.operator(
                    MB_OT_CopyMotionQualityReport.bl_idname,
                    icon="COPYDOWN",
                )
            else:
                quality.label(text="まだ確認結果はありません", icon="INFO")
        client = _control_client
        layout.prop(settings, "host")
        layout.prop(settings, "control_port")
        layout.prop(settings, "session_id")
        layout.prop(settings, "send_hz")
        layout.prop(settings, "root_mode")
        box = layout.box()
        if _live_session is not None:
            state = (
                client.state if client is not None else ControlSessionState.STREAMING
            )
            box.label(
                text=(
                    f"{_control_session_state_label(state)} UDP — 次の番号 "
                    f"{_live_session.sequence}"
                ),
                icon="PAUSE" if state is ControlSessionState.PAUSED else "PLAY",
            )
            box.label(
                text=(
                    f"送信 {_live_session.sent_packets} / "
                    f"破棄 {_live_session.dropped_packets}"
                )
            )
        elif client is not None:
            box.label(
                text=f"TCP — {_control_session_state_label(client.state)}",
                icon="LINKED",
            )
        elif _pending_control_config is not None:
            box.label(
                text=_last_live_error or "TCP — 再接続中",
                icon="TIME",
            )
        elif _last_live_error:
            box.label(text=_last_live_error, icon="ERROR")
        else:
            box.label(text="編集用リグをUnityへ送信できます", icon="INFO")
        if client is not None:
            clock_sync = client.clock_sync_estimate
            if clock_sync is not None:
                box.label(
                    text=(
                        f"TCP同期 往復 {clock_sync.round_trip_time_usec / 1000.0:.2f} ms "
                        f"（{clock_sync.sample_count}/8 回計測）"
                    )
                )
            stats = client.pose_stats
            if stats is not None:
                warning = bool(
                    stats.dropped_by_sequence
                    or stats.rejected
                    or stats.receiver_overrun
                )
                box.label(
                    text=(
                        f"Unity受信 {stats.received} / 欠落 "
                        f"{stats.dropped_by_sequence} / 拒否 {stats.rejected} / "
                        f"処理超過 {stats.receiver_overrun}"
                    ),
                    icon="ERROR" if warning else "CHECKMARK",
                )
                if stats.one_way_age_usec_p95 is not None:
                    box.label(
                        text=(
                            f"ポーズ遅延 中央値 "
                            f"{stats.one_way_age_usec_p50 / 1000.0:.2f} ms / "
                            f"p95 {stats.one_way_age_usec_p95 / 1000.0:.2f} ms"
                        )
                    )
        row = layout.row(align=True)
        row.operator(MB_OT_StartLivePose.bl_idname, icon="PLAY")
        pause_text = (
            "再開"
            if client is not None and client.state is ControlSessionState.PAUSED
            else "一時停止"
        )
        row.operator(
            MB_OT_ToggleLivePause.bl_idname,
            text=pause_text,
            icon=(
                "PLAY"
                if client is not None and client.state is ControlSessionState.PAUSED
                else "PAUSE"
            ),
        )
        row.operator(MB_OT_StopLivePose.bl_idname, text="終了", icon="X")
        layout.separator()
        retarget = layout.box()
        retarget.label(text="元モーションの変換", icon="ARMATURE_DATA")
        mapping_locked = _manual_mapping_changes_locked(context)
        if mapping_locked:
            retarget.label(
                text="修正レイヤー作成後は元モーション設定を変更できません",
                icon="LOCKED",
            )
        source_controls = retarget.column()
        source_controls.enabled = not mapping_locked
        source_controls.operator(MB_OT_ImportSourceTake.bl_idname, icon="IMPORT")
        source_controls.prop(settings, "source_armature")
        source = settings.source_armature
        if source is not None and source.get("motion_bridge_source_archive_action"):
            source_format = str(source.get("motion_bridge_source_format", "")).upper()
            source_hash = str(source.get("motion_bridge_source_file_sha256", ""))
            source_action = str(source.get("motion_bridge_source_action", ""))
            frame_start = source.get("motion_bridge_source_frame_start")
            frame_end = source.get("motion_bridge_source_frame_end")
            retarget.label(
                text=f"{source_format} / {source_action} / SHA {source_hash[:12]}",
                icon="FILE_TICK",
            )
            if frame_start is not None and frame_end is not None:
                retarget.label(text=f"元モーション範囲 {frame_start:g}–{frame_end:g}")
        preset_control = retarget.row()
        preset_control.enabled = not mapping_locked
        preset_control.prop(settings, "retarget_preset")
        manual_buttons = retarget.row(align=True)
        manual_buttons.enabled = not mapping_locked
        manual_buttons.operator(
            MB_OT_PopulateManualMapping.bl_idname,
            icon="FILE_REFRESH",
        )
        manual_buttons.operator(MB_OT_ClearManualMapping.bl_idname, icon="X")
        if settings.retarget_preset == "MANUAL":
            if source is None:
                retarget.label(
                    text="元モーションリグを選ぶとボーン対応を編集できます",
                    icon="INFO",
                )
            else:
                profile = settings.manual_mapping
                mapping_fields = retarget.column()
                mapping_fields.enabled = not mapping_locked
                for field in HUMANOID_MAPPING_FIELDS:
                    label = field.human_bone + (" *" if field.required else "")
                    mapping_fields.prop_search(
                        profile,
                        field.property_name,
                        source.data,
                        "bones",
                        text=label,
                    )
                try:
                    mapping = _manual_humanoid_mapping(
                        settings,
                        verify_content=False,
                    )
                except ValueError:
                    mapping = mapping_from_profile_values(
                        _manual_profile_values(profile)
                    )
                    retarget.label(
                        text="元モーションが変わりました。ボーン対応を反映し直してください",
                        icon="ERROR",
                    )
                stale_names = sorted(
                    source_name
                    for source_name in mapping.values()
                    if source_name not in source.data.bones
                )
                if stale_names:
                    retarget.label(
                        text=f"割り当てたボーンが{len(stale_names)}本見つかりません",
                        icon="ERROR",
                    )
            retarget.label(text="* は必須のHumanoidボーンです", icon="INFO")
        retarget.operator(MB_OT_BakeSourceToCanonical.bl_idname, icon="ACTION")
        retarget.label(text="現在のシーン範囲を変換します", icon="INFO")
        rig_view = layout.box()
        rig_view.label(text="リグ表示", icon="BONE_DATA")
        rig_view.label(text="色付きリグ：モーション修正に使います", icon="POSE_HLT")
        rig_view.label(
            text="薄い元リグ：比較専用（編集しません）", icon="ARMATURE_DATA"
        )
        rig_view.prop(settings, "rig_visual_style", text="修正リグ")
        visibility = rig_view.row(align=True)
        visibility.prop(settings, "rig_visual_source_visible")
        visibility.prop(settings, "rig_visual_show_names")
        opacity = rig_view.row()
        opacity.enabled = settings.rig_visual_source_visible or (
            settings.rig_visual_style == "WIRE"
        )
        opacity.prop(settings, "rig_visual_wire_opacity", slider=True)
        rig_view_actions = rig_view.row(align=True)
        rig_view_actions.operator(
            MB_OT_ApplyRigVisualization.bl_idname,
            text="色を適用／更新",
            icon="COLOR",
        )
        rig_view_actions.operator(
            MB_OT_RestoreRigVisualization.bl_idname,
            text="表示を元に戻す",
            icon="LOOP_BACK",
        )
        rig_view.label(text="腰＝金／胴＝緑／頭＝紫")
        rig_view.label(text="左側＝青・水色／右側＝赤・オレンジ")
        if _rig_visual_last_error:
            rig_view.label(text=_rig_visual_last_error, icon="ERROR")
        layout.separator()
        armature = context.active_object
        stack = get_correction_layer_stack(armature)
        if stack is None:
            layout.operator(MB_OT_CreateCorrectionLayer.bl_idname, icon="ACTION")
        else:
            correction = layout.box()
            correction.label(text="修正レイヤー", icon="ACTION")
            correction_actions_private = correction_actions_are_unshared(armature)
            if not correction_actions_private:
                correction.label(
                    text="別のシーンと共有中のため修正操作を停止しています",
                    icon="LOCKED",
                )
            editing_layer = active_correction_edit_layer(armature)
            editing = editing_layer is not None
            stale_edit = has_correction_edit_session(armature) and not editing
            correction_busy = (
                editing
                or stale_edit
                or bool(getattr(context.scene, "is_nla_tweakmode", False))
            )
            if editing:
                edit_box = correction.box()
                edit_box.label(
                    text=f"編集中：{editing_layer.name}",
                    icon="POSE_HLT",
                )
                envelope_preview = active_correction_envelope_preview(armature)
                edit_settings = edit_box.column()
                edit_settings.enabled = envelope_preview is None
                selected_bones = tuple(
                    getattr(context, "selected_pose_bones", ()) or ()
                )
                hips_only = tuple(bone.name for bone in selected_bones) == ("Hips",)
                location_setting = edit_settings.row()
                location_setting.enabled = envelope_preview is None and hips_only
                location_setting.prop(settings, "correction_include_location")
                include_location = bool(
                    settings.correction_include_location and hips_only
                )
                if settings.correction_include_location and not hips_only:
                    edit_settings.label(
                        text="位置補正を使う場合はHipsだけを選択してください",
                        icon="INFO",
                    )
                blend = edit_settings.row(align=True)
                blend.prop(settings, "correction_blend_frames")
                blend.prop(settings, "correction_interpolation", text="補間")
                keys = edit_box.row(align=True)
                keys.enabled = envelope_preview is None
                current_key = keys.operator(
                    MB_OT_InsertCorrectionPoseKeys.bl_idname,
                    text="現在フレームを記録",
                    icon="KEY_HLT",
                )
                current_key.include_location = include_location
                current_key.neutral_padding = settings.correction_blend_frames
                current_key.interpolation = settings.correction_interpolation
                cleanup = edit_box.row(align=True)
                cleanup.enabled = envelope_preview is None
                reset = cleanup.operator(
                    MB_OT_ResetCorrectionPoseKeys.bl_idname,
                    text="修正を0へ戻す",
                    icon="LOOP_BACK",
                )
                reset.include_location = include_location
                reset.interpolation = settings.correction_interpolation
                remove = cleanup.operator(
                    MB_OT_RemoveCorrectionPoseKeys.bl_idname,
                    text="現在キーを削除",
                    icon="KEY_DEHLT",
                )
                remove.include_location = include_location

                envelope = edit_box.box()
                envelope.label(text="複数フレームへ自然になじませる", icon="FCURVE")
                envelope.prop(settings, "correction_envelope_preset", text="カーブ形状")
                range_row = envelope.row(align=True)
                range_row.enabled = envelope_preview is None
                range_row.prop(context.scene, "use_preview_range", text="数値で範囲指定")
                if context.scene.use_preview_range:
                    numeric_range = envelope.row(align=True)
                    numeric_range.enabled = envelope_preview is None
                    numeric_range.prop(context.scene, "frame_preview_start", text="開始")
                    numeric_range.prop(context.scene, "frame_preview_end", text="終了")
                curve_node = _find_envelope_curve_node(settings)
                if curve_node is not None:
                    envelope.template_curve_mapping(
                        curve_node,
                        "mapping",
                        type="NONE",
                        levels=False,
                        brush=False,
                        use_negative_slope=True,
                        show_tone=False,
                        show_presets=False,
                    )
                else:
                    envelope.label(
                        text="補正カーブは操作開始時に準備されます",
                        icon="INFO",
                    )
                if envelope_preview is not None:
                    source = _envelope_range_source_label(
                        envelope_preview.envelope_range.source
                    )
                    envelope.label(
                        text=(
                            f"プレビュー中／{source}："
                            f"{envelope_preview.envelope_range.scene_start:g} - "
                            f"{envelope_preview.envelope_range.scene_end:g}"
                        ),
                        icon="PLAY",
                    )
                    commit = envelope.row(align=True)
                    commit.operator(
                        MB_OT_ApplyCorrectionEnvelope.bl_idname,
                        text="この結果で確定",
                        icon="CHECKMARK",
                    )
                    commit.operator(
                        MB_OT_CancelCorrectionEnvelope.bl_idname,
                        text="開始前へ戻す",
                        icon="X",
                    )
                else:
                    begin = envelope.row(align=True)
                    add_pose = begin.operator(
                        MB_OT_BeginCorrectionEnvelope.bl_idname,
                        text="今のポーズを範囲に反映",
                        icon="PREVIEW_RANGE",
                    )
                    add_pose.mode = "POSE_OFFSET"
                    reweight = begin.operator(
                        MB_OT_BeginCorrectionEnvelope.bl_idname,
                        text="既存キーの強さを調整",
                        icon="GRAPH",
                    )
                    reweight.mode = "REWEIGHT"
                    reedit_row = envelope.row()
                    reedit_row.enabled = committed_pose_envelope_available(
                        editing_layer.action
                    )
                    reedit = reedit_row.operator(
                        MB_OT_BeginCorrectionEnvelope.bl_idname,
                        text="確定したカーブを再編集",
                        icon="GRAPH",
                    )
                    reedit.mode = "REEDIT"
                    if context.scene.use_preview_range:
                        envelope.label(
                            text=(
                                "数値指定の範囲："
                                f"{context.scene.frame_preview_start} - "
                                f"{context.scene.frame_preview_end}"
                            ),
                            icon="PREVIEW_RANGE",
                        )
                    else:
                        envelope.label(
                            text="2フレーム以上のキーを選ぶか、Pで範囲を設定します",
                            icon="INFO",
                        )
                if settings.correction_include_location:
                    envelope.label(text="位置補正はHipsだけに適用されます", icon="INFO")
                if _envelope_last_error:
                    envelope.label(text=_envelope_last_error, icon="ERROR")
                finish = edit_box.row()
                finish.enabled = envelope_preview is None
                finish.operator(
                    MB_OT_ExitCorrectionEdit.bl_idname,
                    icon="CHECKMARK",
                )
            elif stale_edit:
                recovery = correction.box()
                recovery.label(
                    text="Motion Bridge以外で修正モードが終了しました",
                    icon="ERROR",
                )
                recovery.operator(
                    MB_OT_RecoverCorrectionEdit.bl_idname,
                    icon="FILE_REFRESH",
                )
            else:
                contact = correction.box()
                contact.enabled = _idle_correction_stack(context) is not None
                contact.label(text="足の接地範囲", icon="PREVIEW_RANGE")
                detect = contact.row(align=True)
                detect.prop(settings, "contact_foot", expand=True)
                detect.operator(MB_OT_DetectFootContactRange.bl_idname)
                height = contact.row(align=True)
                height.prop(settings, "contact_ground_height")
                height.prop(settings, "contact_max_height")
                motion = contact.row(align=True)
                motion.prop(settings, "contact_max_speed")
                motion.prop(settings, "contact_min_frames")
                motion.prop(settings, "contact_gap_frames")
                contact.label(
                    text="現在位置の接地区間をプレビュー範囲に設定します",
                    icon="INFO",
                )
                edit_start = correction.row()
                edit_start.enabled = correction_actions_private
                edit_start.operator(
                    MB_OT_EnterCorrectionEdit.bl_idname,
                    icon="POSE_HLT",
                )
                animation_data = armature.animation_data
                if animation_data is not None and animation_data.use_tweak_mode:
                    saved_tweak_layer = stale_correction_tweak_layer(armature)
                    if saved_tweak_layer is not None:
                        recovery = correction.box()
                        recovery.label(
                            text=f"保存済みの修正：{saved_tweak_layer.name}",
                            icon="INFO",
                        )
                        recovery.operator(
                            MB_OT_ExitSavedCorrectionTweak.bl_idname,
                            text="キーを残して通常状態へ戻す",
                            icon="FILE_REFRESH",
                        )
                    else:
                        correction.label(
                            text="先に別のNLA Tweak Modeを終了してください",
                            icon="ERROR",
                        )
            preview_enabled = corrections_preview_enabled(armature)
            comparison = correction.row(align=True)
            comparison.enabled = not correction_busy and correction_actions_private
            before = comparison.operator(
                MB_OT_SetCorrectionPreview.bl_idname,
                text="修正前",
                depress=not preview_enabled,
            )
            before.enabled = False
            after = comparison.operator(
                MB_OT_SetCorrectionPreview.bl_idname,
                text="修正後",
                depress=preview_enabled,
            )
            after.enabled = True
            for layer in reversed(stack.correction_layers):
                row = correction.row(align=True)
                row.enabled = not correction_busy and correction_actions_private
                select = row.operator(
                    MB_OT_SelectCorrectionLayer.bl_idname,
                    text=layer.name,
                    depress=layer.layer_id == stack.active_layer_id,
                )
                select.layer_id = layer.layer_id
                muted = bool(layer.action.get("motion_bridge_layer_muted", layer.muted))
                mute = row.operator(
                    MB_OT_ToggleCorrectionLayerMute.bl_idname,
                    text="M",
                    depress=muted,
                )
                mute.layer_id = layer.layer_id
                solo = row.operator(
                    MB_OT_ToggleCorrectionLayerSolo.bl_idname,
                    text="S",
                    depress=layer.solo,
                )
                solo.layer_id = layer.layer_id
                row.prop(layer.strip, "influence", text="", slider=True)

            controls = correction.row(align=True)
            controls.enabled = not correction_busy and correction_actions_private
            controls.operator(MB_OT_AddCorrectionLayer.bl_idname, text="", icon="ADD")
            controls.operator(
                MB_OT_DuplicateCorrectionLayer.bl_idname,
                text="",
                icon="DUPLICATE",
            )
            controls.operator(
                MB_OT_RenameCorrectionLayer.bl_idname,
                text="",
                icon="GREASEPENCIL",
            )
            move_down = controls.operator(
                MB_OT_MoveCorrectionLayer.bl_idname,
                text="",
                icon="TRIA_DOWN",
            )
            move_down.direction = "DOWN"
            move_up = controls.operator(
                MB_OT_MoveCorrectionLayer.bl_idname,
                text="",
                icon="TRIA_UP",
            )
            move_up.direction = "UP"
            controls.operator(
                MB_OT_RemoveCorrectionLayer.bl_idname,
                text="",
                icon="REMOVE",
            )
            merge = correction.row()
            merge.enabled = not correction_busy and correction_actions_private
            merge.operator(
                MB_OT_MergeCorrectionLayerDown.bl_idname,
                text="下のレイヤーへ統合",
                icon="AUTOMERGE_ON",
            )
            flatten = correction.row()
            flatten.enabled = not correction_busy and correction_actions_private
            flatten.operator(
                MB_OT_CommitCorrectionsToBase.bl_idname,
                text="修正をBaseへベイクして統合",
                icon="ACTION",
            )
            pose_transfer = correction.row(align=True)
            pose_transfer.enabled = not correction_busy and correction_actions_private
            pose_transfer.operator(
                MB_OT_CopySourcePose.bl_idname,
                text="元モーションをコピー",
                icon="DUPLICATE",
            )
            pose_transfer.operator(
                MB_OT_PasteSourcePose.bl_idname,
                text="現在フレームへ貼り付け",
                icon="IMPORT",
            )
            take = analyze_action(stack.base_action)
            tier_icon = (
                "ERROR" if take.tier is TakeBudgetTier.SPLIT_RECOMMENDED else "INFO"
            )
            correction.label(
                text=(
                    f"Base {format_point_count(take.total_points)}ポイント／"
                    f"{_take_budget_tier_label(take.tier)}"
                ),
                icon=tier_icon,
            )
            if stack.archive_action.get(PROP_RETARGET_RECIPE):
                correction.operator(
                    MB_OT_RegenerateCanonicalBase.bl_idname,
                    icon="FILE_REFRESH",
                )
        layout.separator()
        if stack is not None:
            layout.operator(MB_OT_ExportCorrectedFbx.bl_idname, icon="EXPORT")
            layout.label(
                text="Baseと修正レイヤーを合成してFBXへ書き出します", icon="INFO"
            )
        else:
            layout.operator(MB_OT_ExportCanonicalActionFbx.bl_idname, icon="EXPORT")
            layout.label(
                text="現在の変換済みモーションだけをFBXへ書き出します", icon="INFO"
            )


_CLASSES = (
    MB_PG_ManualHumanoidMapping,
    MB_PG_SessionSettings,
    MB_OT_ApplyRigVisualization,
    MB_OT_RestoreRigVisualization,
    MB_OT_ActivateCanonicalArmature,
    MB_OT_StartLivePose,
    MB_OT_StopLivePose,
    MB_OT_ToggleLivePause,
    MB_OT_ImportSourceTake,
    MB_OT_PopulateManualMapping,
    MB_OT_ClearManualMapping,
    MB_OT_UseSourceFrameRange,
    MB_OT_BakeSourceToCanonical,
    MB_OT_RegenerateCanonicalBase,
    MB_OT_CreateCorrectionLayer,
    MB_OT_DetachUnexpectedActiveAction,
    MB_OT_ScanMotionQuality,
    MB_OT_CancelMotionQualityScan,
    MB_OT_FocusMotionQualityIssue,
    MB_OT_CopyMotionQualityReport,
    MB_OT_DetectFootContactRange,
    MB_OT_AutoSmoothMotion,
    MB_OT_AutoFootLock,
    MB_OT_AutoJointLimits,
    MB_OT_EnterCorrectionEdit,
    MB_OT_ExitCorrectionEdit,
    MB_OT_RecoverCorrectionEdit,
    MB_OT_ExitSavedCorrectionTweak,
    MB_OT_InsertCorrectionPoseKeys,
    MB_OT_CopySourcePose,
    MB_OT_PasteSourcePose,
    MB_OT_ApplyCorrectionPoseRange,
    MB_OT_BeginCorrectionEnvelope,
    MB_OT_ApplyCorrectionEnvelope,
    MB_OT_CancelCorrectionEnvelope,
    MB_OT_ResetCorrectionPoseKeys,
    MB_OT_RemoveCorrectionPoseKeys,
    MB_OT_AddCorrectionLayer,
    MB_OT_SetCorrectionPreview,
    MB_OT_SelectCorrectionLayer,
    MB_OT_RenameCorrectionLayer,
    MB_OT_DuplicateCorrectionLayer,
    MB_OT_MoveCorrectionLayer,
    MB_OT_MergeCorrectionLayerDown,
    MB_OT_CommitCorrectionsToBase,
    MB_OT_ToggleCorrectionLayerMute,
    MB_OT_ToggleCorrectionLayerSolo,
    MB_OT_RemoveCorrectionLayer,
    MB_OT_ExportCanonicalActionFbx,
    MB_OT_ExportCorrectedFbx,
    MB_PT_GettingStarted,
    MB_PT_Session,
)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.motion_bridge_session = PointerProperty(type=MB_PG_SessionSettings)
    try:
        _initialize_manual_mapping_source_bindings()
    except AttributeError:
        if not bpy.app.timers.is_registered(
            _initialize_manual_mapping_source_bindings_timer
        ):
            bpy.app.timers.register(
                _initialize_manual_mapping_source_bindings_timer,
                first_interval=0.0,
            )
    if _handle_motion_quality_load_pre not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_handle_motion_quality_load_pre)
    if _handle_live_file_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_handle_live_file_load)
    if _handle_envelope_save_pre not in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.append(_handle_envelope_save_pre)
    for handler_list in (bpy.app.handlers.undo_pre, bpy.app.handlers.redo_pre):
        if _handle_envelope_history_pre not in handler_list:
            handler_list.append(_handle_envelope_history_pre)
    for handler_list in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _handle_motion_quality_history_post not in handler_list:
            handler_list.append(_handle_motion_quality_history_post)
    if _handle_motion_quality_frame_change_pre not in bpy.app.handlers.frame_change_pre:
        bpy.app.handlers.frame_change_pre.append(_handle_motion_quality_frame_change_pre)
    if _handle_motion_quality_frame_change_post not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(_handle_motion_quality_frame_change_post)
    if _handle_motion_quality_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(
            _handle_motion_quality_depsgraph_update
        )
    _ensure_motion_quality_cleanup_timer()


def unregister() -> None:
    if bpy.app.timers.is_registered(_initialize_manual_mapping_source_bindings_timer):
        bpy.app.timers.unregister(_initialize_manual_mapping_source_bindings_timer)
    _cancel_motion_quality_runtime(force_cleanup=True)
    if _retry_motion_quality_cleanup_jobs():
        if bpy.app.timers.is_registered(_motion_quality_cleanup_timer):
            bpy.app.timers.unregister(_motion_quality_cleanup_timer)
    else:
        # The callback and job contain no Blender IDs, so it is safer to let
        # cleanup finish after UI classes are removed than to orphan a child.
        _ensure_motion_quality_cleanup_timer()
    _stop_live_session()
    restore_all_rig_visualizations()
    cancel_all_correction_envelope_previews()
    cancel_all_correction_edit_sessions(bpy.context)
    while _handle_motion_quality_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_handle_motion_quality_load_pre)
    while _handle_live_file_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_handle_live_file_load)
    while _handle_envelope_save_pre in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.remove(_handle_envelope_save_pre)
    for handler_list in (bpy.app.handlers.undo_pre, bpy.app.handlers.redo_pre):
        while _handle_envelope_history_pre in handler_list:
            handler_list.remove(_handle_envelope_history_pre)
    for handler_list in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        while _handle_motion_quality_history_post in handler_list:
            handler_list.remove(_handle_motion_quality_history_post)
    while _handle_motion_quality_frame_change_pre in bpy.app.handlers.frame_change_pre:
        bpy.app.handlers.frame_change_pre.remove(
            _handle_motion_quality_frame_change_pre
        )
    while _handle_motion_quality_frame_change_post in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(
            _handle_motion_quality_frame_change_post
        )
    while _handle_motion_quality_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(
            _handle_motion_quality_depsgraph_update
        )
    _cleanup_envelope_curve_data()
    if hasattr(bpy.types.Scene, "motion_bridge_session"):
        del bpy.types.Scene.motion_bridge_session
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
