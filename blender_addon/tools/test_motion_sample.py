from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import math
from pathlib import Path
import sys
import threading
from typing import Callable

import bpy
from mathutils import Quaternion


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from motion_bridge.canonical.rig_spec import JOINTS, SKELETON_ID  # noqa: E402
from motion_bridge.editing.action_api import (  # noqa: E402
    assign_nla_strip_action,
    create_action_for_id,
    iter_action_fcurves,
)
from motion_bridge.editing.motion_sample import (  # noqa: E402
    MAX_MOTION_SAMPLES,
    MotionRangeSampler,
    MotionSamplingError,
    sample_motion_range,
)
from motion_bridge.range_spec import RangeSource, RangeSpec  # noqa: E402


EPSILON = 1.0e-6


def _expect_error(
    callback: Callable[[], object],
    error_type: type[Exception],
    label: str,
) -> Exception:
    try:
        callback()
    except error_type as error:
        return error
    raise AssertionError(f"{label} did not raise {error_type.__name__}")


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for action in tuple(bpy.data.actions):
        bpy.data.actions.remove(action)
    for armature in tuple(bpy.data.armatures):
        if armature.users == 0:
            bpy.data.armatures.remove(armature)


def _create_canonical_armature() -> bpy.types.Object:
    data = bpy.data.armatures.new("MB_Motion_Sample_Data")
    armature = bpy.data.objects.new("MB_Motion_Sample", data)
    bpy.context.scene.collection.objects.link(armature)
    armature["motion_bridge_skeleton_id"] = SKELETON_ID
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature

    bpy.ops.object.mode_set(mode="EDIT")
    bones: dict[str, bpy.types.EditBone] = {}
    for joint in JOINTS[1:]:
        assert joint.head is not None and joint.tail is not None
        bone = data.edit_bones.new(joint.name)
        bone.head = joint.head
        bone.tail = joint.tail
        if joint.parent_index > 0:
            bone.parent = bones[JOINTS[joint.parent_index].name]
        bones[joint.name] = bone
    bpy.ops.object.mode_set(mode="OBJECT")
    for pose_bone in armature.pose.bones:
        pose_bone.rotation_mode = "QUATERNION"
    return armature


def _key_motion(armature: bpy.types.Object) -> None:
    scene = bpy.context.scene
    foot = armature.pose.bones["LeftFoot"]
    for frame, x, angle in ((1, 1.0, 0.0), (3, 3.0, math.radians(90.0))):
        scene.frame_set(frame)
        armature.location = (x, 2.0, 3.0)
        armature.keyframe_insert(data_path="location", frame=frame)
        foot.rotation_quaternion = Quaternion((0.0, 0.0, 1.0), angle)
        foot.keyframe_insert(data_path="rotation_quaternion", frame=frame)
    action = armature.animation_data.action if armature.animation_data else None
    if action is None:
        raise AssertionError("Fixture did not create an Action")
    for curve in iter_action_fcurves(action):
        for point in curve.keyframe_points:
            point.interpolation = "LINEAR"


def _add_nla_state(armature: bpy.types.Object) -> tuple[object, object]:
    animation_data = armature.animation_data_create()
    access = create_action_for_id(
        "MB_Motion_Sample_State",
        armature,
        frame_start=1.0,
        frame_end=3.0,
    )
    track = animation_data.nla_tracks.new()
    track.name = "MB Motion Sample State"
    strip = track.strips.new("MB Motion Sample State", 1, access.action)
    assign_nla_strip_action(strip, access.action, access.slot)
    track.mute = True
    track.is_solo = False
    if hasattr(track, "select"):
        track.select = True
    strip.mute = False
    strip.select = True
    strip.influence = 0.35
    return track, strip


def _scene_state(
    track: object,
    strip: object,
) -> tuple[object, ...]:
    scene = bpy.context.scene
    active = bpy.context.view_layer.objects.active
    return (
        int(scene.frame_current),
        float(scene.frame_subframe),
        bool(scene.use_preview_range),
        int(scene.frame_preview_start),
        int(scene.frame_preview_end),
        tuple(sorted(obj.name for obj in bpy.context.selected_objects)),
        None if active is None else active.name,
        None if active is None else str(active.mode),
        bool(bpy.data.objects["MB_Motion_Sample"].animation_data.use_nla),
        bool(track.mute),  # type: ignore[attr-defined]
        bool(track.is_solo),  # type: ignore[attr-defined]
        bool(track.select) if hasattr(track, "select") else None,  # type: ignore[attr-defined]
        bool(strip.mute),  # type: ignore[attr-defined]
        bool(strip.select),  # type: ignore[attr-defined]
        float(strip.influence),  # type: ignore[attr-defined]
    )


def _assert_vector(
    actual: tuple[float, ...],
    expected: tuple[float, ...],
    *,
    tolerance: float = EPSILON,
) -> None:
    if len(actual) != len(expected) or any(
        abs(left - right) > tolerance for left, right in zip(actual, expected, strict=True)
    ):
        raise AssertionError(f"Vector mismatch: actual={actual}, expected={expected}")


def _assert_quaternion(
    actual: tuple[float, float, float, float],
    expected: tuple[float, float, float, float],
    *,
    tolerance: float = EPSILON,
) -> None:
    if abs(abs(sum(a * b for a, b in zip(actual, expected, strict=True))) - 1.0) > tolerance:
        raise AssertionError(f"Quaternion mismatch: actual={actual}, expected={expected}")


def _test_evaluated_motion_and_restore(
    armature: bpy.types.Object,
    track: object,
    strip: object,
) -> dict[str, object]:
    scene = bpy.context.scene
    scene.frame_set(2, subframe=0.25)
    scene.use_preview_range = True
    scene.frame_preview_start = 1
    scene.frame_preview_end = 2
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode="POSE")
    before = _scene_state(track, strip)

    sequence = sample_motion_range(
        bpy.context,
        armature,
        frame_start=1,
        frame_end=3,
        max_samples=3,
    )
    if _scene_state(track, strip) != before:
        raise AssertionError("Successful sampling did not restore Blender state")
    if not isinstance(sequence.frames, tuple) or len(sequence.frames) != 3:
        raise AssertionError("MotionSequence does not own an immutable frame tuple")
    if tuple(frame.frame for frame in sequence.frames) != (1.0, 2.0, 3.0):
        raise AssertionError("MotionSequence frame order is not deterministic")
    _assert_vector(sequence.frames[1].root_location, (2.0, 2.0, 3.0))
    _assert_quaternion(sequence.frames[1].root_rotation_wxyz, (1.0, 0.0, 0.0, 0.0))
    _assert_vector(sequence.frames[1].root_scale, (1.0, 1.0, 1.0))
    if abs(sequence.frames[2].time_seconds - (2.0 / 30.0)) > EPSILON:
        raise AssertionError("MotionSequence time_seconds did not use Scene FPS")

    final = sequence.frames[-1]
    if tuple(bone.name for bone in final.bones) != tuple(
        sorted(joint.name for joint in JOINTS[1:])
    ):
        raise AssertionError("MotionSequence does not contain all 22 Canonical bones")
    hips = next(bone for bone in final.bones if bone.name == "Hips")
    spine = next(bone for bone in final.bones if bone.name == "Spine")
    if hips.parent is not None or spine.parent != "Hips":
        raise AssertionError("Canonical parent names were not retained")
    left_foot = next(bone for bone in final.bones if bone.name == "LeftFoot")
    expected_foot_rotation = tuple(Quaternion((0.0, 0.0, 1.0), math.radians(90.0)))
    _assert_quaternion(left_foot.local_rotation_wxyz, expected_foot_rotation)
    if tuple(effector.name for effector in final.effectors) != (
        "LeftFoot",
        "RightFoot",
    ):
        raise AssertionError("Left/Right Foot effectors were not emitted")
    left_effector = final.effectors[0]
    _assert_vector(left_effector.position_world, left_foot.tail_world)
    _assert_quaternion(left_effector.rotation_wxyz, left_foot.global_rotation_wxyz)
    if sequence.keyed_channel_count != 7:
        raise AssertionError(
            f"Expected 7 keyed channels, got {sequence.keyed_channel_count}"
        )
    if tuple(frame.key_count for frame in sequence.frames) != (7, 0, 7):
        raise AssertionError("Per-frame key density did not match the fixture Action")

    repeated = sample_motion_range(
        bpy.context,
        armature,
        frame_start=1,
        frame_end=3,
        max_samples=3,
    )
    if repeated != sequence:
        raise AssertionError("Repeated evaluated sampling was not deterministic")
    if repeated.content_hash() != sequence.content_hash():
        raise AssertionError("Repeated evaluated sampling changed the content hash")
    if _scene_state(track, strip) != before:
        raise AssertionError("Repeated sampling changed Blender state")

    try:
        sequence.frames = ()  # type: ignore[misc]
    except (AttributeError, FrozenInstanceError):
        pass
    else:
        raise AssertionError("MotionSequence is mutable")

    return {
        "frameCount": len(sequence.frames),
        "boneCount": len(final.bones),
        "effectorCount": len(final.effectors),
        "keyedChannels": sequence.keyed_channel_count,
        "deterministic": True,
    }


def _test_incremental_cancel(
    armature: bpy.types.Object,
    track: object,
    strip: object,
) -> None:
    before = _scene_state(track, strip)
    sampler = MotionRangeSampler(
        bpy.context,
        armature,
        frame_start=1,
        frame_end=3,
        max_samples=3,
    ).begin()
    sample = sampler.sample_frame()
    if sample.frame != 1.0 or sampler.sampled_count != 1 or sampler.progress != 1.0 / 3.0:
        raise AssertionError("Incremental sampler progress is incorrect")
    if sampler.next_frame != 2:
        raise AssertionError("Incremental sampler did not expose its next frame")
    sampler.restore_frame_for_yield()
    if _scene_state(track, strip) != before:
        raise AssertionError("Incremental yield exposed its temporary sample frame")
    second = sampler.sample_frame()
    if second.frame != 2.0 or sampler.next_frame != 3:
        raise AssertionError("Incremental sampling did not resume after yielding")
    sampler.cancel()
    sampler.cancel()
    if _scene_state(track, strip) != before:
        raise AssertionError("Incremental cancel did not restore Blender state")
    _expect_error(sampler.begin, RuntimeError, "Sampler reuse")


def _test_range_spec_input(
    armature: bpy.types.Object,
    track: object,
    strip: object,
) -> None:
    before = _scene_state(track, strip)
    spec = RangeSpec(2.0, 3.0, RangeSource.PREVIEW_RANGE)
    sequence = sample_motion_range(
        bpy.context,
        armature,
        range_spec=spec,
        max_samples=2,
    )
    if tuple(frame.frame for frame in sequence.frames) != (2.0, 3.0):
        raise AssertionError("RangeSpec did not control sampled Scene frames")
    if dict(sequence.metadata).get("range_source") != "preview_range":
        raise AssertionError("RangeSpec source was not recorded in sample metadata")
    if _scene_state(track, strip) != before:
        raise AssertionError("RangeSpec sampling did not restore Blender state")

    _expect_error(
        lambda: MotionRangeSampler(
            bpy.context,
            armature,
            frame_start=1,
            range_spec=spec,
        ),
        ValueError,
        "RangeSpec and legacy frame arguments",
    )


def _test_callback_invalidation(
    armature: bpy.types.Object,
    track: object,
    strip: object,
) -> str:
    scene = bpy.context.scene
    before = _scene_state(track, strip)

    def invalidate(frame: int) -> None:
        if frame != 2:
            return
        if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        scene.use_preview_range = False
        scene.frame_preview_start = 30
        scene.frame_preview_end = 40
        armature.animation_data.use_nla = False
        track.mute = False  # type: ignore[attr-defined]
        track.is_solo = True  # type: ignore[attr-defined]
        strip.mute = True  # type: ignore[attr-defined]
        strip.select = False  # type: ignore[attr-defined]
        strip.influence = 0.9  # type: ignore[attr-defined]
        armature["motion_bridge_skeleton_id"] = "invalidated-by-test"

    error = _expect_error(
        lambda: sample_motion_range(
            bpy.context,
            armature,
            frame_start=1,
            frame_end=3,
            max_samples=3,
            frame_callback=invalidate,
        ),
        MotionSamplingError,
        "Callback armature invalidation",
    )
    message = str(error)
    if "callback" not in message or "invalid" not in message:
        raise AssertionError(f"Callback invalidation error was not explicit: {message}")
    if _scene_state(track, strip) != before:
        raise AssertionError("Callback failure did not restore Blender state")
    armature["motion_bridge_skeleton_id"] = SKELETON_ID
    return message


def _test_limits_and_main_thread(
    armature: bpy.types.Object,
    track: object,
    strip: object,
) -> None:
    before = _scene_state(track, strip)
    if MAX_MOTION_SAMPLES != 20_000:
        raise AssertionError("Unexpected production motion sample limit")
    _expect_error(
        lambda: sample_motion_range(
            bpy.context,
            armature,
            frame_start=1,
            frame_end=3,
            max_samples=2,
        ),
        ValueError,
        "Maximum sample count",
    )
    _expect_error(
        lambda: sample_motion_range(
            bpy.context,
            armature,
            frame_start=3,
            frame_end=1,
        ),
        ValueError,
        "Reversed frame range",
    )
    if _scene_state(track, strip) != before:
        raise AssertionError("Preflight rejection changed Blender state")

    errors: list[Exception] = []

    def worker() -> None:
        try:
            MotionRangeSampler(
                bpy.context,
                armature,
                frame_start=1,
                frame_end=1,
            ).begin()
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    if len(errors) != 1 or not isinstance(errors[0], RuntimeError):
        raise AssertionError(f"Background sampling was not rejected: {errors!r}")
    if "main thread" not in str(errors[0]):
        raise AssertionError(f"Main-thread error was not explicit: {errors[0]}")


def _test_nonfinite_evaluation(
    armature: bpy.types.Object,
    track: object,
    strip: object,
) -> None:
    before = _scene_state(track, strip)

    def make_nonfinite(frame: int) -> None:
        if frame == 2:
            armature.location.x = math.nan

    error = _expect_error(
        lambda: sample_motion_range(
            bpy.context,
            armature,
            frame_start=1,
            frame_end=3,
            max_samples=3,
            frame_callback=make_nonfinite,
        ),
        MotionSamplingError,
        "Non-finite evaluated root",
    )
    if "finite" not in str(error):
        raise AssertionError(f"Non-finite evaluation error was not explicit: {error}")
    if _scene_state(track, strip) != before:
        raise AssertionError("Non-finite evaluation failure did not restore Blender state")


def main() -> None:
    _clear_scene()
    armature = _create_canonical_armature()
    _key_motion(armature)
    track, strip = _add_nla_state(armature)
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = 3
    scene.render.fps = 30
    scene.render.fps_base = 1.0

    evaluated = _test_evaluated_motion_and_restore(armature, track, strip)
    _test_incremental_cancel(armature, track, strip)
    _test_range_spec_input(armature, track, strip)
    callback_error = _test_callback_invalidation(armature, track, strip)
    _test_nonfinite_evaluation(armature, track, strip)
    _test_limits_and_main_thread(armature, track, strip)

    print(
        "MOTION_BRIDGE_MOTION_SAMPLE="
        + json.dumps(
            {
                **evaluated,
                "stateRestored": True,
                "callbackFailure": callback_error,
                "sampleLimit": True,
                "mainThreadOnly": True,
                "nonFiniteRejected": True,
                "rangeSpec": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
