import unittest

from motion_bridge.workflow_guide import (
    GuideStage,
    LiveGuideState,
    WorkflowFacts,
    friendly_live_error,
    resolve_guide_stage,
    resolve_live_guide_state,
)


def _facts(**changes: object) -> WorkflowFacts:
    values = {
        "canonical_count": 1,
        "active_is_canonical": True,
        "source_ready": False,
        "source_frame_range_known": False,
        "scene_matches_source_range": True,
        "has_motion": False,
        "stack_marker": False,
        "stack_valid": False,
        "initial_base_action_private": True,
        "correction_actions_private": True,
        "corrections_enabled": True,
        "unsupported_active_action": False,
        "unsupported_nla": False,
        "edit_session": False,
        "editing": False,
        "unrelated_tweak_mode": False,
        "envelope_preview": False,
        "selected_pose_bones": 0,
    }
    values.update(changes)
    return WorkflowFacts(**values)


class WorkflowGuideTests(unittest.TestCase):
    def test_motion_setup_sequence(self) -> None:
        self.assertEqual(
            GuideStage.NO_CANONICAL,
            resolve_guide_stage(_facts(canonical_count=0, active_is_canonical=False)),
        )
        self.assertEqual(
            GuideStage.SELECT_CANONICAL,
            resolve_guide_stage(_facts(active_is_canonical=False)),
        )
        self.assertEqual(GuideStage.IMPORT_SOURCE, resolve_guide_stage(_facts()))
        self.assertEqual(
            GuideStage.SET_SOURCE_RANGE,
            resolve_guide_stage(
                _facts(
                    source_ready=True,
                    source_frame_range_known=True,
                    scene_matches_source_range=False,
                )
            ),
        )
        self.assertEqual(
            GuideStage.BAKE_SOURCE,
            resolve_guide_stage(_facts(source_ready=True)),
        )
        self.assertEqual(
            GuideStage.CREATE_CORRECTION,
            resolve_guide_stage(_facts(has_motion=True)),
        )
        self.assertEqual(
            GuideStage.SHARED_BASE,
            resolve_guide_stage(
                _facts(has_motion=True, initial_base_action_private=False)
            ),
        )

    def test_existing_stack_does_not_require_source(self) -> None:
        self.assertEqual(
            GuideStage.READY_EDIT,
            resolve_guide_stage(
                _facts(has_motion=True, stack_marker=True, stack_valid=True)
            ),
        )
        self.assertEqual(
            GuideStage.BEFORE_PREVIEW,
            resolve_guide_stage(
                _facts(
                    has_motion=True,
                    stack_marker=True,
                    stack_valid=True,
                    corrections_enabled=False,
                )
            ),
        )
        self.assertEqual(
            GuideStage.SHARED_CORRECTION,
            resolve_guide_stage(
                _facts(
                    has_motion=True,
                    stack_marker=True,
                    stack_valid=True,
                    correction_actions_private=False,
                )
            ),
        )

    def test_editing_sequence_and_priority(self) -> None:
        base = {
            "has_motion": True,
            "stack_marker": True,
            "stack_valid": True,
            "edit_session": True,
        }
        self.assertEqual(
            GuideStage.RECOVER_EDIT,
            resolve_guide_stage(_facts(**base)),
        )
        self.assertEqual(
            GuideStage.SELECT_BONE,
            resolve_guide_stage(_facts(**base, editing=True)),
        )
        self.assertEqual(
            GuideStage.EDIT_POSE,
            resolve_guide_stage(_facts(**base, editing=True, selected_pose_bones=2)),
        )
        self.assertEqual(
            GuideStage.ENVELOPE_PREVIEW,
            resolve_guide_stage(
                _facts(
                    **base,
                    editing=True,
                    selected_pose_bones=2,
                    envelope_preview=True,
                )
            ),
        )
        self.assertEqual(
            GuideStage.SHARED_CORRECTION,
            resolve_guide_stage(
                _facts(
                    **base,
                    editing=True,
                    selected_pose_bones=2,
                    correction_actions_private=False,
                )
            ),
        )

    def test_broken_stack_and_unrelated_tweak_are_not_recreated(self) -> None:
        self.assertEqual(
            GuideStage.UNSUPPORTED_ACTION,
            resolve_guide_stage(
                _facts(
                    has_motion=True,
                    stack_marker=True,
                    stack_valid=True,
                    unsupported_active_action=True,
                )
            ),
        )
        self.assertEqual(
            GuideStage.UNSUPPORTED_NLA,
            resolve_guide_stage(_facts(has_motion=True, unsupported_nla=True)),
        )
        self.assertEqual(
            GuideStage.UNSUPPORTED_NLA,
            resolve_guide_stage(
                _facts(
                    has_motion=True,
                    stack_marker=True,
                    stack_valid=False,
                    unsupported_nla=True,
                )
            ),
        )
        self.assertEqual(
            GuideStage.BROKEN_STACK,
            resolve_guide_stage(
                _facts(has_motion=True, stack_marker=True, stack_valid=False)
            ),
        )
        self.assertEqual(
            GuideStage.BLOCKED_TWEAK,
            resolve_guide_stage(_facts(has_motion=True, unrelated_tweak_mode=True)),
        )

    def test_live_state_is_independent(self) -> None:
        self.assertEqual(
            LiveGuideState.OFFLINE,
            resolve_live_guide_state(
                requested=False,
                control_state=None,
                pose_session_active=False,
            ),
        )
        self.assertEqual(
            LiveGuideState.CONNECTING,
            resolve_live_guide_state(
                requested=True,
                control_state="Connecting",
                pose_session_active=False,
            ),
        )
        self.assertEqual(
            LiveGuideState.STREAMING,
            resolve_live_guide_state(
                requested=True,
                control_state="Streaming",
                pose_session_active=True,
            ),
        )
        self.assertEqual(
            LiveGuideState.PAUSED,
            resolve_live_guide_state(
                requested=True,
                control_state="Paused",
                pose_session_active=True,
            ),
        )

    def test_common_errors_are_actionable(self) -> None:
        self.assertIn("まだ開始", friendly_live_error("Connection refused"))
        self.assertIn("自動で再接続", friendly_live_error("peer closed; reconnecting"))
        self.assertIn("Session ID", friendly_live_error("session mismatch"))
        skeleton = friendly_live_error("hello canonical skeleton does not match")
        self.assertIn("同じ配布セット", skeleton)
        self.assertIn("Unityの待受→Blenderの接続", skeleton)


if __name__ == "__main__":
    unittest.main()
