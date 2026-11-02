from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Mapping

import bpy

from ..canonical.rig_spec import JOINTS, SKELETON_ID


VISUAL_COLLECTION_PROPERTY: Final = "motion_bridge_visual_role"
_LEGACY_VISUAL_SCHEMA_PROPERTY: Final = "motion_bridge_visual_schema"


@dataclass(frozen=True, slots=True)
class BoneRoleStyle:
    role: str
    label: str
    collection_name: str
    normal: tuple[float, float, float]
    selected: tuple[float, float, float]
    active: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class RigVisualizationResult:
    colored_bones: int
    role_collections: int
    viewport_overlays: int
    source_configured: bool


@dataclass(slots=True)
class _BoneViewState:
    palette: str
    normal: tuple[float, float, float]
    selected: tuple[float, float, float]
    active: tuple[float, float, float]
    collection_names: tuple[str, ...]


@dataclass(slots=True)
class _ArmatureViewState:
    obj: bpy.types.Object
    data: bpy.types.Armature
    display_type: str
    show_names: bool
    show_bone_colors: bool
    show_in_front: bool
    hidden: bool | None
    view_layer: bpy.types.ViewLayer | None
    bone_states: dict[str, _BoneViewState] | None
    collection_names: frozenset[str]

    @classmethod
    def capture(
        cls,
        context: bpy.types.Context,
        obj: bpy.types.Object,
        *,
        include_bones: bool = False,
    ) -> _ArmatureViewState:
        data = _require_local_armature(obj, "Armature")
        view_layer = getattr(context, "view_layer", None)
        hidden = None
        if view_layer is not None and view_layer.objects.get(obj.name) is obj:
            hidden = bool(obj.hide_get(view_layer=view_layer))
        bone_states = None
        if include_bones:
            bone_states = {
                bone.name: _BoneViewState(
                    palette=str(bone.color.palette),
                    normal=tuple(float(value) for value in bone.color.custom.normal),
                    selected=tuple(float(value) for value in bone.color.custom.select),
                    active=tuple(float(value) for value in bone.color.custom.active),
                    collection_names=tuple(
                        collection.name for collection in bone.collections
                    ),
                )
                for bone in data.bones
            }
        return cls(
            obj=obj,
            data=data,
            display_type=str(data.display_type),
            show_names=bool(data.show_names),
            show_bone_colors=bool(data.show_bone_colors),
            show_in_front=bool(obj.show_in_front),
            hidden=hidden,
            view_layer=view_layer,
            bone_states=bone_states,
            collection_names=frozenset(
                collection.name for collection in data.collections_all
            ),
        )

    def restore(self) -> None:
        try:
            self.data.display_type = self.display_type
            self.data.show_names = self.show_names
            self.data.show_bone_colors = self.show_bone_colors
            self.obj.show_in_front = self.show_in_front
            if (
                self.hidden is not None
                and self.view_layer is not None
                and self.view_layer.objects.get(self.obj.name) is self.obj
            ):
                self.obj.hide_set(self.hidden, view_layer=self.view_layer)
            if self.bone_states is not None:
                for bone_name, bone_state in self.bone_states.items():
                    bone = self.data.bones.get(bone_name)
                    if bone is None:
                        continue
                    expected_collections = set(bone_state.collection_names)
                    for collection in tuple(bone.collections):
                        if collection.name not in expected_collections:
                            collection.unassign(bone)
                    for collection_name in bone_state.collection_names:
                        collection = self.data.collections_all.get(collection_name)
                        if collection is not None:
                            collection.assign(bone)
                    bone.color.custom.normal = bone_state.normal
                    bone.color.custom.select = bone_state.selected
                    bone.color.custom.active = bone_state.active
                    bone.color.palette = bone_state.palette
                for collection in tuple(self.data.collections_all):
                    if (
                        collection.get(VISUAL_COLLECTION_PROPERTY)
                        and collection.name not in self.collection_names
                    ):
                        self.data.collections.remove(collection)
        except (ReferenceError, RuntimeError):
            return


@dataclass(slots=True)
class _RigViewState:
    canonical: _ArmatureViewState
    source: _ArmatureViewState | None = None


@dataclass(slots=True)
class _OverlayViewState:
    space: bpy.types.SpaceView3D
    show_bones: bool
    show_xray_bone: bool
    bone_wire_alpha: float
    xray_alpha_bone: float

    def restore(self) -> None:
        try:
            overlay = self.space.overlay
            overlay.show_bones = self.show_bones
            overlay.show_xray_bone = self.show_xray_bone
            overlay.bone_wire_alpha = self.bone_wire_alpha
            overlay.xray_alpha_bone = self.xray_alpha_bone
        except (ReferenceError, RuntimeError):
            return


_RIG_VIEW_STATES: dict[int, _RigViewState] = {}
_OVERLAY_VIEW_STATES: dict[int, _OverlayViewState] = {}


# The palette follows the common animation-rig convention: center controls are
# warm, the character's left is blue, and the character's right is red.
BONE_ROLE_STYLES: Final[tuple[BoneRoleStyle, ...]] = (
    BoneRoleStyle(
        "ROOT_MOTION",
        "Hips / Root Motion",
        "MB | Hips - Root Motion",
        (0.95, 0.52, 0.06),
        (1.00, 0.78, 0.22),
        (1.00, 0.95, 0.50),
    ),
    BoneRoleStyle(
        "TORSO",
        "Torso",
        "MB | Torso",
        (0.18, 0.62, 0.24),
        (0.38, 0.88, 0.42),
        (0.72, 1.00, 0.68),
    ),
    BoneRoleStyle(
        "HEAD",
        "Head / Look",
        "MB | Head - Look",
        (0.52, 0.25, 0.82),
        (0.75, 0.48, 1.00),
        (0.92, 0.76, 1.00),
    ),
    BoneRoleStyle(
        "LEFT_ARM",
        "Left Arm",
        "MB | Left Arm",
        (0.08, 0.42, 0.92),
        (0.20, 0.70, 1.00),
        (0.62, 0.90, 1.00),
    ),
    BoneRoleStyle(
        "RIGHT_ARM",
        "Right Arm",
        "MB | Right Arm",
        (0.88, 0.10, 0.16),
        (1.00, 0.32, 0.38),
        (1.00, 0.70, 0.72),
    ),
    BoneRoleStyle(
        "LEFT_LEG",
        "Left Leg",
        "MB | Left Leg",
        (0.04, 0.68, 0.72),
        (0.18, 0.92, 0.94),
        (0.62, 1.00, 1.00),
    ),
    BoneRoleStyle(
        "RIGHT_LEG",
        "Right Leg",
        "MB | Right Leg",
        (0.92, 0.34, 0.05),
        (1.00, 0.58, 0.16),
        (1.00, 0.84, 0.48),
    ),
)

_STYLE_BY_ROLE: Final = {style.role: style for style in BONE_ROLE_STYLES}

_ROLE_BY_HUMAN_BONE: Final[dict[str, str]] = {
    "Hips": "ROOT_MOTION",
    "Spine": "TORSO",
    "Chest": "TORSO",
    "UpperChest": "TORSO",
    "Neck": "HEAD",
    "Head": "HEAD",
    "LeftShoulder": "LEFT_ARM",
    "LeftUpperArm": "LEFT_ARM",
    "LeftLowerArm": "LEFT_ARM",
    "LeftHand": "LEFT_ARM",
    "RightShoulder": "RIGHT_ARM",
    "RightUpperArm": "RIGHT_ARM",
    "RightLowerArm": "RIGHT_ARM",
    "RightHand": "RIGHT_ARM",
    "LeftUpperLeg": "LEFT_LEG",
    "LeftLowerLeg": "LEFT_LEG",
    "LeftFoot": "LEFT_LEG",
    "LeftToes": "LEFT_LEG",
    "RightUpperLeg": "RIGHT_LEG",
    "RightLowerLeg": "RIGHT_LEG",
    "RightFoot": "RIGHT_LEG",
    "RightToes": "RIGHT_LEG",
}

CANONICAL_BONE_ROLES: Final[dict[str, str]] = {
    joint.name: _ROLE_BY_HUMAN_BONE[joint.human_bone]
    for joint in JOINTS[1:]
    if joint.human_bone is not None
}


def bone_role_for_human_bone(human_bone: str) -> str | None:
    return _ROLE_BY_HUMAN_BONE.get(human_bone)


def _require_local_armature(obj: bpy.types.Object, label: str) -> bpy.types.Armature:
    if obj is None or obj.type != "ARMATURE":
        raise TypeError(f"{label} must be an Armature Object")
    armature = obj.data
    if armature.library is not None:
        raise ValueError(f"{label} Armature data must be local and editable")
    return armature


def _rna_pointer(value: object | None) -> int:
    if value is None:
        return 0
    try:
        return int(value.as_pointer())  # type: ignore[attr-defined]
    except ReferenceError:
        return 0


def _role_collection(
    armature: bpy.types.Armature,
    style: BoneRoleStyle,
) -> bpy.types.BoneCollection:
    for collection in armature.collections_all:
        if collection.get(VISUAL_COLLECTION_PROPERTY) == style.role:
            return collection
    collection = armature.collections.get(style.collection_name)
    if (
        collection is not None
        and collection.get(VISUAL_COLLECTION_PROPERTY) != style.role
    ):
        collection = None
    if collection is None:
        collection = armature.collections.new(style.collection_name)
    collection[VISUAL_COLLECTION_PROPERTY] = style.role
    collection.is_visible = True
    return collection


def apply_bone_role_visuals(
    armature_object: bpy.types.Object,
    bone_roles: Mapping[str, str],
) -> tuple[int, int]:
    """Color editable bones and place them in namespaced role collections.

    Existing non-Motion-Bridge collections are preserved. Reapplying is
    idempotent and also removes stale assignments from managed collections.
    """

    armature = _require_local_armature(armature_object, "Correction rig")
    unknown_roles = sorted(set(bone_roles.values()) - set(_STYLE_BY_ROLE))
    if unknown_roles:
        raise ValueError(f"Unknown Motion Bridge bone roles: {unknown_roles}")

    collections = {
        role: _role_collection(armature, style)
        for role, style in _STYLE_BY_ROLE.items()
        if role in bone_roles.values()
    }
    for role, collection in collections.items():
        for bone in tuple(collection.bones):
            if bone_roles.get(bone.name) != role:
                collection.unassign(bone)

    colored = 0
    for bone_name, role in bone_roles.items():
        bone = armature.bones.get(bone_name)
        if bone is None:
            continue
        style = _STYLE_BY_ROLE[role]
        collections[role].assign(bone)
        bone.color.palette = "CUSTOM"
        bone.color.custom.normal = style.normal
        bone.color.custom.select = style.selected
        bone.color.custom.active = style.active
        colored += 1

    armature.show_bone_colors = True
    # v0.1.7 pre-release briefly stored the display schema on Armature data.
    # FBX exports custom Armature properties, so remove that visual-only marker.
    if _LEGACY_VISUAL_SCHEMA_PROPERTY in armature:
        del armature[_LEGACY_VISUAL_SCHEMA_PROPERTY]
    return colored, len(collections)


def _configure_viewport_overlays(
    context: bpy.types.Context,
    wire_opacity: float,
) -> int:
    opacity = max(0.05, min(1.0, float(wire_opacity)))
    configured = 0
    seen: set[int] = set()
    current_area = getattr(context, "area", None)
    screen = getattr(context, "screen", None)
    if current_area is not None and current_area.type == "VIEW_3D":
        areas = (current_area,)
    elif screen is not None:
        areas = tuple(area for area in screen.areas if area.type == "VIEW_3D")
    else:
        areas = ()
    for area in areas:
        space = area.spaces.active
        pointer = space.as_pointer()
        if pointer in seen:
            continue
        seen.add(pointer)
        overlay = space.overlay
        if pointer not in _OVERLAY_VIEW_STATES:
            _OVERLAY_VIEW_STATES[pointer] = _OverlayViewState(
                space=space,
                show_bones=bool(overlay.show_bones),
                show_xray_bone=bool(overlay.show_xray_bone),
                bone_wire_alpha=float(overlay.bone_wire_alpha),
                xray_alpha_bone=float(overlay.xray_alpha_bone),
            )
        overlay.show_bones = True
        overlay.show_xray_bone = True
        overlay.bone_wire_alpha = opacity
        overlay.xray_alpha_bone = opacity
        area.tag_redraw()
        configured += 1
    return configured


def has_rig_visualization_snapshot(
    canonical_armature: bpy.types.Object | None,
) -> bool:
    if canonical_armature is None:
        return False
    try:
        return canonical_armature.as_pointer() in _RIG_VIEW_STATES
    except ReferenceError:
        return False


def _restore_overlay_states() -> None:
    for state in tuple(_OVERLAY_VIEW_STATES.values()):
        state.restore()
    _OVERLAY_VIEW_STATES.clear()


def restore_rig_visualization(
    canonical_armature: bpy.types.Object,
) -> bool:
    try:
        key = canonical_armature.as_pointer()
    except ReferenceError:
        return False
    state = _RIG_VIEW_STATES.pop(key, None)
    if state is None:
        return False
    if state.source is not None:
        state.source.restore()
    state.canonical.restore()
    if not _RIG_VIEW_STATES:
        _restore_overlay_states()
    return True


def restore_all_rig_visualizations() -> None:
    for state in tuple(_RIG_VIEW_STATES.values()):
        if state.source is not None:
            state.source.restore()
        state.canonical.restore()
    _RIG_VIEW_STATES.clear()
    _restore_overlay_states()


def clear_rig_visualization_snapshots() -> None:
    """Forget pre-load RNA references after Blender replaces all datablocks."""

    _RIG_VIEW_STATES.clear()
    _OVERLAY_VIEW_STATES.clear()


def apply_rig_visualization(
    context: bpy.types.Context,
    canonical_armature: bpy.types.Object,
    *,
    source_armature: bpy.types.Object | None = None,
    canonical_style: str = "STICK",
    source_visible: bool = True,
    wire_opacity: float = 0.25,
    show_names: bool = False,
) -> RigVisualizationResult:
    """Apply an editing-oriented view without touching transforms or animation."""

    canonical_data = _require_local_armature(canonical_armature, "Correction rig")
    if canonical_data.users != 1:
        raise ValueError(
            "Correction rig Armature data must be single-user before applying Rig View"
        )
    if canonical_armature.get("motion_bridge_skeleton_id") != SKELETON_ID:
        raise ValueError("Correction rig is not the Motion Bridge Canonical skeleton")
    if canonical_style not in {"STICK", "WIRE", "OCTAHEDRAL"}:
        raise ValueError(f"Unsupported Correction rig display style: {canonical_style}")
    source_data = None
    if source_armature is not None and source_armature is not canonical_armature:
        source_data = _require_local_armature(source_armature, "Source reference")
        if source_data.users != 1:
            raise ValueError(
                "Source reference Armature data must be single-user before applying Rig View"
            )

    canonical_key = _rna_pointer(canonical_armature)
    state = _RIG_VIEW_STATES.get(canonical_key)
    if state is None:
        state = _RigViewState(
            canonical=_ArmatureViewState.capture(
                context,
                canonical_armature,
                include_bones=True,
            )
        )
        _RIG_VIEW_STATES[canonical_key] = state
    previous_source = state.source
    source_changed = (
        previous_source is not None
        and (
            source_armature is None
            or _rna_pointer(previous_source.obj) != _rna_pointer(source_armature)
        )
    )
    if source_changed:
        previous_source.restore()
        state.source = None
    if (
        source_data is not None
        and source_armature is not None
        and state.source is None
    ):
        state.source = _ArmatureViewState.capture(context, source_armature)

    colored, collection_count = apply_bone_role_visuals(
        canonical_armature,
        CANONICAL_BONE_ROLES,
    )
    canonical_data.display_type = canonical_style
    canonical_data.show_names = bool(show_names)
    canonical_armature.show_in_front = True

    source_configured = False
    if source_data is not None and source_armature is not None:
        source_data.display_type = "WIRE"
        source_data.show_names = False
        source_armature.show_in_front = False
        view_layer = getattr(context, "view_layer", None)
        if (
            view_layer is not None
            and view_layer.objects.get(source_armature.name) is source_armature
        ):
            source_armature.hide_set(not bool(source_visible))
        source_configured = True

    overlays = _configure_viewport_overlays(context, wire_opacity)
    return RigVisualizationResult(
        colored_bones=colored,
        role_collections=collection_count,
        viewport_overlays=overlays,
        source_configured=source_configured,
    )
