from __future__ import annotations

import json
import math
from pathlib import Path
import sys
from typing import Callable

import bpy


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from motion_bridge import addon  # noqa: E402
from motion_bridge.canonical.rig_spec import JOINTS, SKELETON_ID  # noqa: E402
import motion_bridge.editing.foot_contact as foot_contact_module  # noqa: E402
from motion_bridge.editing.correction_layers import (  # noqa: E402
    action_checksum,
    create_correction_layer,
)
from motion_bridge.editing.foot_contact import (  # noqa: E402
    FootContactInterval,
    FootContactSample,
    contact_interval_at_frame,
    detect_contact_range_at_current,
    detect_foot_contact_intervals,
    sample_canonical_foot_tail,
)


EPSILON = 1.0e-6


def _expect_error(
    callback: Callable[[], object],
    error_type: type[Exception],
    label: str,
) -> None:
    try:
        callback()
    except error_type:
        return
    raise AssertionError(f"{label} did not raise {error_type.__name__}")


def _samples(
    heights: tuple[float, ...],
    *,
    x_step: float = 0.0,
    first_frame: int = 1,
) -> tuple[FootContactSample, ...]:
    return tuple(
        FootContactSample(
            first_frame + index,
            (float(index) * x_step, 0.0, float(height)),
        )
        for index, height in enumerate(heights)
    )


def _detect(
    samples: tuple[FootContactSample, ...],
    *,
    max_height: float = 0.08,
    max_speed: float = 1_000.0,
    min_frames: int = 1,
    gap_frames: int = 0,
) -> tuple[FootContactInterval, ...]:
    return detect_foot_contact_intervals(
        samples,
        frames_per_second=30.0,
        ground_height=0.0,
        max_height=max_height,
        max_speed=max_speed,
        min_frames=min_frames,
        gap_frames=gap_frames,
    )


def _test_pure_detector() -> dict[str, object]:
    height_gap = _samples(
        (1.0, 0.05, 0.05, 0.05, 0.50, 0.05, 0.05, 0.05, 1.0)
    )
    split = _detect(height_gap, min_frames=3, gap_frames=0)
    if tuple((item.start, item.end) for item in split) != ((2, 4), (6, 8)):
        raise AssertionError(f"One-frame gap was joined while disabled: {split!r}")

    bridged = _detect(height_gap, min_frames=3, gap_frames=1)
    if len(bridged) != 1 or (bridged[0].start, bridged[0].end) != (2, 8):
        raise AssertionError(f"One-frame contact gap was not joined: {bridged!r}")
    interval = bridged[0]
    if abs(interval.anchor[2] - (0.8 / 7.0)) > EPSILON:
        raise AssertionError(f"Unexpected joined-run anchor: {interval.anchor!r}")
    if abs(interval.peak_ground_distance - 0.50) > EPSILON:
        raise AssertionError("Joined gap was not included in interval diagnostics")
    if contact_interval_at_frame(bridged, 5.5) is not interval:
        raise AssertionError("Current-frame lookup did not select the joined interval")

    moving = _detect(
        _samples((0.05,) * 5, x_step=0.1),
        max_speed=0.5,
        min_frames=1,
    )
    if moving:
        raise AssertionError("Low but fast-moving samples were classified as contact")

    planted_between_swings = _detect(
        (
            FootContactSample(1, (-1.0, 0.0, 1.0)),
            FootContactSample(2, (0.0, 0.0, 0.05)),
            FootContactSample(3, (0.0, 0.0, 0.05)),
            FootContactSample(4, (0.0, 0.0, 0.05)),
            FootContactSample(5, (1.0, 0.0, 1.0)),
        ),
        max_speed=0.5,
        min_frames=3,
    )
    if tuple((item.start, item.end) for item in planted_between_swings) != ((2, 4),):
        raise AssertionError(
            "Swing velocity leaked into the planted interval boundary: "
            f"{planted_between_swings!r}"
        )

    penetrated = _detect(
        _samples((-0.5, -0.5, -0.5)),
        max_height=0.08,
        min_frames=3,
    )
    if tuple((item.start, item.end) for item in penetrated) != ((1, 3),):
        raise AssertionError("A stationary foot below the floor was not detected")

    too_high = _detect(
        _samples((0.09,) * 5),
        max_height=0.08,
        min_frames=1,
    )
    if too_high:
        raise AssertionError("Stationary samples above Max Height were classified")

    too_short = _detect(
        _samples((0.05, 0.05)),
        min_frames=3,
    )
    if too_short:
        raise AssertionError("A run shorter than Min Frames was retained")

    invalid_cases: tuple[
        tuple[str, Callable[[], object], type[Exception]], ...
    ] = (
        ("empty samples", lambda: _detect(()), ValueError),
        (
            "non-consecutive frames",
            lambda: _detect(
                (
                    FootContactSample(1, (0.0, 0.0, 0.0)),
                    FootContactSample(3, (0.0, 0.0, 0.0)),
                )
            ),
            ValueError,
        ),
        (
            "boolean frame",
            lambda: _detect((FootContactSample(True, (0.0, 0.0, 0.0)),)),
            TypeError,
        ),
        (
            "non-finite position",
            lambda: _detect((FootContactSample(1, (math.nan, 0.0, 0.0)),)),
            ValueError,
        ),
        (
            "zero FPS",
            lambda: detect_foot_contact_intervals(
                _samples((0.0,)),
                frames_per_second=0.0,
                ground_height=0.0,
                max_height=0.1,
                max_speed=1.0,
                min_frames=1,
                gap_frames=0,
            ),
            ValueError,
        ),
        (
            "non-finite ground",
            lambda: detect_foot_contact_intervals(
                _samples((0.0,)),
                frames_per_second=30.0,
                ground_height=math.inf,
                max_height=0.1,
                max_speed=1.0,
                min_frames=1,
                gap_frames=0,
            ),
            ValueError,
        ),
        (
            "negative Max Height",
            lambda: _detect(_samples((0.0,)), max_height=-0.1),
            ValueError,
        ),
        (
            "negative Max Speed",
            lambda: _detect(_samples((0.0,)), max_speed=-0.1),
            ValueError,
        ),
        (
            "zero Min Frames",
            lambda: _detect(_samples((0.0,)), min_frames=0),
            ValueError,
        ),
        (
            "boolean Min Frames",
            lambda: _detect(_samples((0.0,)), min_frames=True),
            ValueError,
        ),
        (
            "negative Join Gap",
            lambda: _detect(_samples((0.0,)), gap_frames=-1),
            ValueError,
        ),
        (
            "boolean Join Gap",
            lambda: _detect(_samples((0.0,)), gap_frames=False),
            ValueError,
        ),
        (
            "non-finite current frame",
            lambda: contact_interval_at_frame(bridged, math.nan),
            ValueError,
        ),
        (
            "current frame outside contacts",
            lambda: contact_interval_at_frame(bridged, 20.0),
            ValueError,
        ),
    )
    for label, callback, error_type in invalid_cases:
        _expect_error(callback, error_type, label)

    return {
        "heightThreshold": True,
        "speedThreshold": True,
        "oneFrameGap": True,
        "minimumFrames": True,
        "penetrationDetected": True,
        "swingBoundaryIsolated": True,
        "invalidCases": len(invalid_cases),
    }


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for action in tuple(bpy.data.actions):
        bpy.data.actions.remove(action)


def _create_canonical_armature() -> bpy.types.Object:
    _clear_scene()
    data = bpy.data.armatures.new("MB_Foot_Contact_Data")
    armature = bpy.data.objects.new("MB_Foot_Contact", data)
    bpy.context.scene.collection.objects.link(armature)
    armature["motion_bridge_skeleton_id"] = SKELETON_ID
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature

    bpy.ops.object.mode_set(mode="EDIT")
    bones: dict[str, bpy.types.EditBone] = {}
    for joint in JOINTS[1:]:
        bone = data.edit_bones.new(joint.name)
        assert joint.head is not None and joint.tail is not None
        bone.head = joint.head
        bone.tail = joint.tail
        if joint.parent_index > 0:
            bone.parent = bones[JOINTS[joint.parent_index].name]
        bones[joint.name] = bone
    bpy.ops.object.mode_set(mode="OBJECT")
    for pose_bone in armature.pose.bones:
        pose_bone.rotation_mode = "QUATERNION"
    return armature


def _key_object_trajectory(armature: bpy.types.Object) -> None:
    armature.location = (0.0, 0.0, 0.0)
    armature.keyframe_insert(data_path="location", frame=1)
    armature.location = (2.0, 0.0, 0.0)
    armature.keyframe_insert(data_path="location", frame=5)
    action = armature.animation_data.action if armature.animation_data else None
    if action is None:
        raise AssertionError("Object trajectory did not create a Base Action")
    channelbags = action.layers[0].strips[0].channelbags
    if len(channelbags) != 1:
        raise AssertionError("Object trajectory did not use one Action Slot")
    for curve in channelbags[0].fcurves:
        for point in curve.keyframe_points:
            point.interpolation = "LINEAR"


def _preview_state(scene: bpy.types.Scene) -> tuple[bool, int, int]:
    return (
        bool(scene.use_preview_range),
        int(scene.frame_preview_start),
        int(scene.frame_preview_end),
    )


def _scene_time(scene: bpy.types.Scene) -> tuple[int, float]:
    return int(scene.frame_current), float(scene.frame_subframe)


def _remove_pose_bone(armature: bpy.types.Object, name: str) -> None:
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bone = armature.data.edit_bones.get(name)
    if bone is None:
        raise AssertionError(f"Test fixture has no bone {name!r}")
    armature.data.edit_bones.remove(bone)
    bpy.ops.object.mode_set(mode="OBJECT")


def _test_adaptive_scan(armature: bpy.types.Object) -> None:
    scene = bpy.context.scene
    original_scope = (
        int(scene.frame_start),
        int(scene.frame_end),
        int(scene.frame_current),
        float(scene.frame_subframe),
    )
    original_sampler = foot_contact_module.sample_canonical_foot_tail
    sampled_frames: set[int] = set()

    def fake_sampler(
        _context: bpy.types.Context,
        _armature: bpy.types.Object,
        _foot_bone_name: str,
        scene_frame_start: int,
        scene_frame_end: int,
        *,
        max_samples: int,
    ) -> tuple[FootContactSample, ...]:
        del max_samples
        result: list[FootContactSample] = []
        for frame in range(scene_frame_start, scene_frame_end + 1):
            if frame in sampled_frames:
                raise AssertionError(f"Adaptive scan sampled frame {frame} twice")
            sampled_frames.add(frame)
            height = 0.05 if 2360 <= frame <= 2640 else 1.0
            result.append(FootContactSample(frame, (0.0, 0.0, height)))
        return tuple(result)

    try:
        scene.frame_start = 1
        scene.frame_end = 5000
        scene.frame_set(2500)
        foot_contact_module.sample_canonical_foot_tail = fake_sampler
        interval = detect_contact_range_at_current(
            bpy.context,
            armature,
            foot_bone_name="LeftFoot",
            scene_frame_start=1,
            scene_frame_end=5000,
            ground_height=0.0,
            max_height=0.08,
            max_speed=0.3,
            min_frames=3,
            gap_frames=0,
            max_samples=1000,
        )
    finally:
        foot_contact_module.sample_canonical_foot_tail = original_sampler
        scene.frame_start = original_scope[0]
        scene.frame_end = original_scope[1]
        scene.frame_set(original_scope[2], subframe=original_scope[3])
    if (interval.start, interval.end) != (2360, 2640):
        raise AssertionError(f"Adaptive scan returned the wrong interval: {interval!r}")
    if len(sampled_frames) != 481:
        raise AssertionError(
            f"Adaptive scan sampled {len(sampled_frames)} frames instead of 481"
        )


def _test_blender_adapter_and_operator() -> dict[str, object]:
    armature = _create_canonical_armature()
    _key_object_trajectory(armature)
    stack = create_correction_layer(bpy.context, armature)
    action_checksums = {
        action.name: action_checksum(action)
        for action in (
            stack.archive_action,
            stack.base_action,
            stack.correction_action,
        )
    }
    _test_adaptive_scan(armature)

    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = 5
    scene.render.fps = 30
    scene.render.fps_base = 1.0
    scene.frame_set(3, subframe=0.25)
    original_time = _scene_time(scene)

    samples = sample_canonical_foot_tail(
        bpy.context,
        armature,
        "LeftFoot",
        1,
        5,
        max_samples=5,
    )
    if tuple(sample.frame for sample in samples) != (1, 2, 3, 4, 5):
        raise AssertionError(f"Foot-tail sampler returned wrong frames: {samples!r}")
    if abs(samples[0].position[0] - 0.095) > EPSILON or abs(
        samples[-1].position[0] - 2.095
    ) > EPSILON:
        raise AssertionError(
            f"Sampler did not use evaluated world-space foot tails: {samples!r}"
        )
    if _scene_time(scene) != original_time:
        raise AssertionError("Foot-tail sampler did not restore frame/subframe")

    interval = detect_contact_range_at_current(
        bpy.context,
        armature,
        foot_bone_name="LeftFoot",
        scene_frame_start=1,
        scene_frame_end=5,
        ground_height=0.06,
        max_height=0.001,
        max_speed=100.0,
        min_frames=5,
        gap_frames=0,
        max_samples=5,
    )
    if (interval.start, interval.end) != (1, 5):
        raise AssertionError(f"Unexpected evaluated contact: {interval!r}")
    # LeftFoot tail X is 0.095. The evaluated Base trajectory contributes
    # 0, 0.5, 1, 1.5, 2 at the sampled integer frames.
    if abs(interval.anchor[0] - 1.095) > EPSILON:
        raise AssertionError(
            f"Detector did not sample evaluated world-space foot tails: {interval!r}"
        )
    if interval.peak_speed < 14.9:
        raise AssertionError("Evaluated object trajectory did not affect foot speed")
    if _scene_time(scene) != original_time:
        raise AssertionError("Contact adapter did not restore frame/subframe")

    _expect_error(
        lambda: detect_contact_range_at_current(
            bpy.context,
            armature,
            foot_bone_name="LeftFoot",
            scene_frame_start=1,
            scene_frame_end=5,
            ground_height=0.06,
            max_height=0.001,
            max_speed=100.0,
            min_frames=1,
            gap_frames=0,
            max_samples=4,
        ),
        ValueError,
        "Contact sample budget",
    )
    if _scene_time(scene) != original_time:
        raise AssertionError("Sample-budget rejection changed Scene time")

    settings = scene.motion_bridge_session
    settings.contact_foot = "LEFT"
    settings.contact_ground_height = 0.06
    settings.contact_max_height = 0.001
    settings.contact_max_speed = 100.0
    settings.contact_min_frames = 5
    settings.contact_gap_frames = 0
    scene.use_preview_range = False
    scene.frame_preview_start = 2
    scene.frame_preview_end = 4
    result = bpy.ops.motion_bridge.detect_foot_contact_range()
    if "FINISHED" not in result:
        raise AssertionError(f"Foot contact operator did not finish: {result}")
    if _preview_state(scene) != (True, 1, 5):
        raise AssertionError("Operator did not commit the detected Preview Range")
    if _scene_time(scene) != original_time:
        raise AssertionError("Successful operator changed current Scene time")

    failure_preview = (True, 2, 4)
    scene.use_preview_range = failure_preview[0]
    scene.frame_preview_start = failure_preview[1]
    scene.frame_preview_end = failure_preview[2]
    settings.contact_max_height = 0.0
    settings.contact_ground_height = -1.0
    _expect_error(
        lambda: bpy.ops.motion_bridge.detect_foot_contact_range(),
        RuntimeError,
        "No-contact operator",
    )
    if _preview_state(scene) != failure_preview:
        raise AssertionError("Failed detection partially changed Preview Range")
    if _scene_time(scene) != original_time:
        raise AssertionError("Failed operator changed current Scene time")

    _remove_pose_bone(armature, "RightFoot")
    settings.contact_foot = "RIGHT"
    settings.contact_ground_height = 0.06
    settings.contact_max_height = 0.001
    before_missing = _preview_state(scene)
    _expect_error(
        lambda: bpy.ops.motion_bridge.detect_foot_contact_range(),
        RuntimeError,
        "Missing-foot operator",
    )
    if _preview_state(scene) != before_missing:
        raise AssertionError("Missing-foot failure changed Preview Range")
    if _scene_time(scene) != original_time:
        raise AssertionError("Missing-foot sampling did not restore frame/subframe")
    for action in (
        stack.archive_action,
        stack.base_action,
        stack.correction_action,
    ):
        if action_checksum(action) != action_checksums[action.name]:
            raise AssertionError(f"Contact detection changed Action {action.name!r}")

    return {
        "detectedRange": [interval.start, interval.end],
        "evaluatedTail": True,
        "currentInterval": True,
        "timeRestored": True,
        "operatorRegistered": True,
        "failureTransactional": True,
        "missingBone": True,
        "actionsImmutable": True,
        "adaptiveScan": True,
    }


def main() -> None:
    pure = _test_pure_detector()
    addon.register()
    try:
        blender = _test_blender_adapter_and_operator()
    finally:
        addon.unregister()
    print(
        "MOTION_BRIDGE_FOOT_CONTACT_RANGE_TEST="
        + json.dumps(
            {"status": "PASS", **pure, **blender},
            separators=(",", ":"),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
