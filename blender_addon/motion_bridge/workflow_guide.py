"""Pure state resolver for the beginner-facing Motion Bridge guide.

The Blender panel is redrawn frequently, so the guide derives its current step
from existing scene state instead of storing a wizard page.  Keeping this module
free of ``bpy`` also lets the decision table run in ordinary unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class GuideStage(str, Enum):
    NO_CANONICAL = "no_canonical"
    SELECT_CANONICAL = "select_canonical"
    IMPORT_SOURCE = "import_source"
    SET_SOURCE_RANGE = "set_source_range"
    BAKE_SOURCE = "bake_source"
    CREATE_CORRECTION = "create_correction"
    UNSUPPORTED_ACTION = "unsupported_action"
    UNSUPPORTED_NLA = "unsupported_nla"
    SHARED_BASE = "shared_base"
    SHARED_CORRECTION = "shared_correction"
    BROKEN_STACK = "broken_stack"
    BEFORE_PREVIEW = "before_preview"
    READY_EDIT = "ready_edit"
    RECOVER_EDIT = "recover_edit"
    BLOCKED_TWEAK = "blocked_tweak"
    SELECT_BONE = "select_bone"
    EDIT_POSE = "edit_pose"
    ENVELOPE_PREVIEW = "envelope_preview"


@dataclass(frozen=True, slots=True)
class WorkflowFacts:
    canonical_count: int
    active_is_canonical: bool
    source_ready: bool
    source_frame_range_known: bool
    scene_matches_source_range: bool
    has_motion: bool
    stack_marker: bool
    stack_valid: bool
    initial_base_action_private: bool
    correction_actions_private: bool
    corrections_enabled: bool
    unsupported_active_action: bool
    unsupported_nla: bool
    edit_session: bool
    editing: bool
    unrelated_tweak_mode: bool
    envelope_preview: bool
    selected_pose_bones: int


def resolve_guide_stage(facts: WorkflowFacts) -> GuideStage:
    """Return the single safest next editing action for the current scene."""

    if facts.canonical_count <= 0:
        return GuideStage.NO_CANONICAL
    if not facts.active_is_canonical:
        return GuideStage.SELECT_CANONICAL
    if facts.unsupported_active_action:
        return GuideStage.UNSUPPORTED_ACTION
    if facts.unsupported_nla:
        return GuideStage.UNSUPPORTED_NLA
    if facts.stack_marker and not facts.stack_valid:
        return GuideStage.BROKEN_STACK
    if facts.stack_valid:
        if facts.edit_session and not facts.editing:
            return GuideStage.RECOVER_EDIT
        if facts.editing:
            if not facts.correction_actions_private:
                return GuideStage.SHARED_CORRECTION
            if facts.envelope_preview:
                return GuideStage.ENVELOPE_PREVIEW
            if facts.selected_pose_bones <= 0:
                return GuideStage.SELECT_BONE
            return GuideStage.EDIT_POSE
        if not facts.corrections_enabled:
            return GuideStage.BEFORE_PREVIEW
        if facts.unrelated_tweak_mode:
            return GuideStage.BLOCKED_TWEAK
        if not facts.correction_actions_private:
            return GuideStage.SHARED_CORRECTION
        return GuideStage.READY_EDIT
    if not facts.has_motion:
        if not facts.source_ready:
            return GuideStage.IMPORT_SOURCE
        if facts.source_frame_range_known and not facts.scene_matches_source_range:
            return GuideStage.SET_SOURCE_RANGE
        return GuideStage.BAKE_SOURCE
    if facts.unrelated_tweak_mode:
        return GuideStage.BLOCKED_TWEAK
    if not facts.initial_base_action_private:
        return GuideStage.SHARED_BASE
    return GuideStage.CREATE_CORRECTION


class LiveGuideState(str, Enum):
    OFFLINE = "offline"
    CONNECTING = "connecting"
    STREAMING = "streaming"
    PAUSED = "paused"


def resolve_live_guide_state(
    *,
    requested: bool,
    control_state: str | None,
    pose_session_active: bool,
) -> LiveGuideState:
    normalized = (control_state or "").strip().lower()
    if normalized == "paused":
        return LiveGuideState.PAUSED
    if pose_session_active and normalized in {"", "ready", "streaming"}:
        return LiveGuideState.STREAMING
    if requested:
        return LiveGuideState.CONNECTING
    return LiveGuideState.OFFLINE


def friendly_live_error(message: str) -> str:
    """Translate common transport failures into one actionable Japanese hint."""

    normalized = message.strip().lower()
    if not normalized:
        return "Unity側で「プレビュー待受を開始」を押してください。"
    if "connection refused" in normalized or "errno 61" in normalized:
        return "Unity側のプレビュー待受がまだ開始されていません。"
    if "peer closed" in normalized or "reconnecting" in normalized:
        return "Unityの再生切替を待っています。自動で再接続します。"
    if "session" in normalized:
        return "Unityと接続番号が一致しません。詳細設定のSession IDを確認してください。"
    if "descriptor" in normalized or "skeleton" in normalized:
        return (
            "Canonicalリグが一致しません。同じ配布セットのリグを開き直し、"
            "Unityの待受→Blenderの接続の順でやり直してください。"
        )
    if "timed out" in normalized or "timeout" in normalized:
        return (
            "Unityから応答がありません。プレビュー待受が動いているか確認してください。"
        )
    return "Unityへ接続できません。詳細設定に技術情報があります。"
