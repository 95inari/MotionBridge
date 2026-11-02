"""Bake the visible correction stack into a fresh Base and empty edit layer."""

from __future__ import annotations

import bpy

from .action_api import action_slot_for_id
from .bake import bake_layer_stack
from .correction_layers import (
    BASE_TRACK_NAME,
    PROP_ACTIVE_LAYER_ID,
    PROP_ARCHIVE_ACTION,
    PROP_BASE_ACTION,
    PROP_CORRECTION_ACTION,
    PROP_CORRECTIONS_BYPASSED,
    PROP_RETARGET_RECIPE,
    PROP_SCHEMA,
    PROP_STACK_ID,
    _apply_binding_state,
    _binding_state,
    _configure_strip,
    _correction_track_name,
    _restore_binding,
    _strip_for_action,
    action_is_unshared_for_armature,
    create_correction_layer,
    require_unshared_correction_actions,
    validate_layer_stack,
)


_ARMATURE_KEYS = (
    PROP_SCHEMA,
    PROP_STACK_ID,
    PROP_ARCHIVE_ACTION,
    PROP_BASE_ACTION,
    PROP_CORRECTION_ACTION,
    PROP_ACTIVE_LAYER_ID,
    PROP_CORRECTIONS_BYPASSED,
    PROP_RETARGET_RECIPE,
)


def commit_corrections_to_base(
    context: bpy.types.Context,
    armature: bpy.types.Object,
) -> bpy.types.Action:
    """Create a new checkpoint stack; retain old Actions as recoverable history."""

    validation = validate_layer_stack(armature)
    validation.raise_for_errors()
    stack = validation.stack
    assert stack is not None
    animation_data = armature.animation_data
    assert animation_data is not None
    if animation_data.use_tweak_mode or animation_data.action is not None:
        raise ValueError("修正作業を終了してからBaseへ統合してください")
    if not action_is_unshared_for_armature(armature, stack.base_action):
        raise ValueError("Base Actionが別のリグと共有されています")
    require_unshared_correction_actions(armature, stack)

    baked = bake_layer_stack(
        context, armature, name=f"MB_BASE_MERGED::{stack.base_action.name}"
    )
    baked.use_fake_user = True
    old_id = str(armature[PROP_STACK_ID])
    old_actions = (
        stack.archive_action,
        stack.base_action,
        *(layer.action for layer in stack.correction_layers),
    )
    old_fake_users = tuple(bool(action.use_fake_user) for action in old_actions)
    old_values = {key: (key in armature, armature.get(key)) for key in _ARMATURE_KEYS}
    old_bindings = tuple(_binding_state(layer) for layer in stack.correction_layers)
    old_base_select = bool(stack.base_track.select)
    old_base_strip_select = bool(stack.base_strip.select)
    old_base_slot = action_slot_for_id(stack.base_action, armature)
    new_actions: tuple[bpy.types.Action, ...] = ()

    try:
        for action in old_actions:
            action.use_fake_user = True
        for track in tuple(animation_data.nla_tracks):
            animation_data.nla_tracks.remove(track)
        for key in _ARMATURE_KEYS:
            if key in armature:
                del armature[key]
        _restore_binding(animation_data, baked, action_slot_for_id(baked, armature))
        rebuilt = create_correction_layer(
            context,
            armature,
            provenance={
                "mergedFromStack": old_id,
                "previousArchiveAction": stack.archive_action.name,
                "previousBaseAction": stack.base_action.name,
            },
        )
        new_actions = (rebuilt.archive_action, rebuilt.correction_action)
        validate_layer_stack(armature).raise_for_errors()
        return rebuilt.base_action
    except Exception:
        animation_data.action = None
        for track in tuple(animation_data.nla_tracks):
            animation_data.nla_tracks.remove(track)
        for key in _ARMATURE_KEYS:
            if key in armature:
                del armature[key]
        for key, (existed, value) in old_values.items():
            if existed:
                armature[key] = value
        base_track = animation_data.nla_tracks.new()
        base_track.name = BASE_TRACK_NAME
        base_strip = _strip_for_action(
            base_track,
            "Base",
            stack.base_action,
            old_base_slot,
            float(stack.base_action.frame_range[0]),
        )
        _configure_strip(base_strip, blend_type="REPLACE", extrapolation="HOLD")
        base_track.lock = True
        base_track.select = old_base_select
        base_strip.select = old_base_strip_select
        for index, state in enumerate(old_bindings):
            track = animation_data.nla_tracks.new()
            track.name = _correction_track_name(state.name, first=index == 0)
            strip = _strip_for_action(
                track,
                state.name,
                state.action,
                action_slot_for_id(state.action, armature),
                float(stack.base_action.frame_range[0]),
            )
            _configure_strip(strip, blend_type="COMBINE", extrapolation="NOTHING")
            _apply_binding_state(armature, track, state, first=index == 0)
        for action, fake_user in zip(old_actions, old_fake_users, strict=True):
            action.use_fake_user = fake_user
        for action in new_actions + (baked,):
            if action not in old_actions and action.users <= int(action.use_fake_user):
                action.use_fake_user = False
                bpy.data.actions.remove(action)
        validate_layer_stack(armature).raise_for_errors()
        raise
