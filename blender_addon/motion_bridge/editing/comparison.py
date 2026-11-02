from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import bpy

from .correction_layers import (
    PROP_CORRECTIONS_BYPASSED,
    PROP_LAYER_MUTED,
    PROP_LAYER_SOLO,
    CorrectionLayer,
    CorrectionLayerStack,
    get_correction_layer_stack,
    list_correction_layers,
)


@dataclass(frozen=True, slots=True)
class _LayerMuteState:
    layer: CorrectionLayer
    track_mute: bool
    strip_mute: bool


@dataclass(frozen=True, slots=True)
class _PropertyState:
    existed: bool
    value: object | None


def _require_stack(armature: bpy.types.Object) -> CorrectionLayerStack:
    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Correction preview requires an armature Object")
    stack = get_correction_layer_stack(armature)
    if stack is None or not stack.correction_layers:
        raise ValueError("Armature has no resolvable Motion Bridge Correction stack")
    # A saved single-layer v1 stack may still keep its mute choice only on the
    # NLA binding.  Run the O(layer), curve-free metadata migration before a
    # Before toggle can overwrite that effective state.
    list_correction_layers(armature)
    stack = get_correction_layer_stack(armature)
    if stack is None or not stack.correction_layers:
        raise ValueError("Armature has no resolvable Motion Bridge Correction stack")
    return stack


def _require_unbound_animation_state(armature: bpy.types.Object) -> None:
    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Correction preview requires an armature Object")
    animation_data = armature.animation_data
    if animation_data is None:
        return
    if getattr(animation_data, "use_tweak_mode", False):
        raise ValueError("Exit NLA Tweak Mode before switching Correction preview")
    if animation_data.action is not None:
        raise ValueError("Correction preview requires no active Action binding")


def _snapshot_mutes(stack: CorrectionLayerStack) -> tuple[_LayerMuteState, ...]:
    return tuple(
        _LayerMuteState(
            layer,
            bool(layer.track.mute),
            bool(layer.strip.mute),
        )
        for layer in stack.correction_layers
    )


def _snapshot_property(armature: bpy.types.Object) -> _PropertyState:
    return _PropertyState(
        PROP_CORRECTIONS_BYPASSED in armature,
        armature.get(PROP_CORRECTIONS_BYPASSED),
    )


def _restore_property(
    armature: bpy.types.Object,
    state: _PropertyState,
) -> None:
    if state.existed:
        armature[PROP_CORRECTIONS_BYPASSED] = state.value
    elif PROP_CORRECTIONS_BYPASSED in armature:
        del armature[PROP_CORRECTIONS_BYPASSED]


def _restore_mutes(states: tuple[_LayerMuteState, ...]) -> None:
    first_error: Exception | None = None
    for state in states:
        try:
            state.layer.track.mute = state.track_mute
            state.layer.strip.mute = state.strip_mute
        except Exception as error:  # pragma: no cover - invalidated Blender RNA
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise RuntimeError("Correction preview mute state could not be restored") from first_error


def _configured_after_mutes(
    stack: CorrectionLayerStack,
) -> tuple[tuple[CorrectionLayer, bool], ...]:
    solo_layers = tuple(
        layer
        for layer in stack.correction_layers
        if bool(layer.action.get(PROP_LAYER_SOLO, False))
    )
    if len(solo_layers) > 1:
        raise ValueError("At most one Correction layer may be solo")
    solo_id = solo_layers[0].layer_id if solo_layers else None
    return tuple(
        (
            layer,
            layer.layer_id != solo_id
            if solo_id is not None
            else bool(layer.action.get(PROP_LAYER_MUTED, False)),
        )
        for layer in stack.correction_layers
    )


def _assign_layer_mute(layer: CorrectionLayer, muted: bool) -> None:
    """Assignment seam kept small so transactional behavior is testable."""

    layer.track.mute = bool(muted)
    layer.strip.mute = False


def _apply_mutes(
    stack: CorrectionLayerStack,
    *,
    enabled: bool,
) -> tuple[_LayerMuteState, ...]:
    desired = (
        _configured_after_mutes(stack)
        if enabled
        else tuple((layer, True) for layer in stack.correction_layers)
    )
    previous = _snapshot_mutes(stack)
    try:
        for layer, muted in desired:
            _assign_layer_mute(layer, muted)
    except Exception:
        _restore_mutes(previous)
        raise
    return previous


def corrections_preview_enabled(armature: bpy.types.Object) -> bool:
    """Return ``True`` for After and ``False`` for the Base-only Before view.

    The property is intentionally absent on legacy stacks; absence means After.
    This query performs no Action validation or curve access.
    """

    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Correction preview requires an armature Object")
    return not bool(armature.get(PROP_CORRECTIONS_BYPASSED, False))


def set_corrections_preview_enabled(
    armature: bpy.types.Object,
    enabled: bool,
) -> bool:
    """Switch between configured After and Base-only Before transactionally.

    Per-layer mute and solo properties are configuration and are never changed.
    Reapplying the current value also synchronizes layers added while in Before.
    """

    _require_unbound_animation_state(armature)
    stack = _require_stack(armature)
    enabled = bool(enabled)
    property_state = _snapshot_property(armature)
    previous_mutes = _snapshot_mutes(stack)
    try:
        _apply_mutes(stack, enabled=enabled)
        armature[PROP_CORRECTIONS_BYPASSED] = not enabled
    except Exception:
        _restore_mutes(previous_mutes)
        _restore_property(armature, property_state)
        raise
    return enabled


def apply_corrections_preview_state(armature: bpy.types.Object) -> bool:
    """Reapply the persisted preview mode after layer CRUD operations."""

    enabled = corrections_preview_enabled(armature)
    return set_corrections_preview_enabled(armature, enabled)


@contextmanager
def configured_after_corrections(
    armature: bpy.types.Object,
) -> Iterator[CorrectionLayerStack]:
    """Temporarily evaluate configured After mutes without changing preview mode.

    Bake/export can use this while the viewport remains persisted as Before.  Both
    the bypass property and exact incoming Correction mute state are restored on
    normal exit and exceptions.
    """

    _require_unbound_animation_state(armature)
    stack = _require_stack(armature)
    property_state = _snapshot_property(armature)
    previous_mutes = _snapshot_mutes(stack)
    try:
        _apply_mutes(stack, enabled=True)
        yield stack
    finally:
        try:
            _restore_mutes(previous_mutes)
        finally:
            _restore_property(armature, property_state)


__all__ = (
    "PROP_CORRECTIONS_BYPASSED",
    "apply_corrections_preview_state",
    "configured_after_corrections",
    "corrections_preview_enabled",
    "set_corrections_preview_enabled",
)
