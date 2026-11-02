from __future__ import annotations

import json
from pathlib import Path
import sys

import bpy
from mathutils import Quaternion

ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_layer_merge import _create_base_action  # noqa: E402
from motion_bridge.editing.action_api import (  # noqa: E402
    insert_quaternion_key,
    iter_action_fcurves,
)
from motion_bridge.editing.correction_layers import (  # noqa: E402
    add_correction_layer,
    create_correction_layer,
    validate_layer_stack,
)
from motion_bridge.editing.flatten_stack import (  # noqa: E402
    commit_corrections_to_base,
)
import motion_bridge.editing.flatten_stack as flatten_module  # noqa: E402


def _pose(armature: bpy.types.Object, frame: int) -> tuple[float, ...]:
    bpy.context.scene.frame_set(frame)
    bpy.context.view_layer.update()
    evaluated = armature.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return tuple(evaluated.pose.bones["Hips"].matrix.to_quaternion())


def main() -> None:
    armature, _base = _create_base_action()
    initial = create_correction_layer(bpy.context, armature)
    for frame in (1, 12):
        insert_quaternion_key(
            initial.active_layer.access,
            "Hips",
            frame,
            Quaternion((0.0, 0.0, 1.0), 0.5),
        )
    before = tuple(_pose(armature, frame) for frame in (1, 6, 12))
    old_actions = (
        initial.archive_action,
        initial.base_action,
        initial.correction_action,
    )
    bpy.ops.object.mode_set(mode="OBJECT")
    result = commit_corrections_to_base(bpy.context, armature)
    validation = validate_layer_stack(armature)
    validation.raise_for_errors()
    after = tuple(_pose(armature, frame) for frame in (1, 6, 12))
    for old_frame, new_frame in zip(before, after, strict=True):
        if abs(sum(a * b for a, b in zip(old_frame, new_frame, strict=True))) < 0.99999:
            raise AssertionError("Base integration changed the evaluated pose")
    live_actions = {action.as_pointer() for action in bpy.data.actions}
    if any(action.as_pointer() not in live_actions or not action.use_fake_user for action in old_actions):
        raise AssertionError("Original motion Actions were not retained")
    if len(validation.stack.correction_layers) != 1:
        raise AssertionError("Integration did not leave one fresh Correction layer")
    if any(iter_action_fcurves(validation.stack.correction_action)):
        raise AssertionError("Fresh Correction layer is not empty")
    add_correction_layer(bpy.context, armature, name="Further correction")
    validate_layer_stack(armature).raise_for_errors()
    original_id = armature["motion_bridge_stack_id"]
    original_pose = _pose(armature, 6)
    real_create = flatten_module.create_correction_layer

    def fail_rebuild(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected rebuild failure")

    flatten_module.create_correction_layer = fail_rebuild
    try:
        try:
            commit_corrections_to_base(bpy.context, armature)
        except RuntimeError as error:
            if "injected rebuild failure" not in str(error):
                raise
        else:
            raise AssertionError("Injected rebuild failure was not raised")
    finally:
        flatten_module.create_correction_layer = real_create
    if armature["motion_bridge_stack_id"] != original_id:
        raise AssertionError("Failed integration changed the stack identity")
    validate_layer_stack(armature).raise_for_errors()
    if abs(sum(a * b for a, b in zip(original_pose, _pose(armature, 6), strict=True))) < 0.99999:
        raise AssertionError("Failed integration changed the evaluated pose")
    print("MOTION_BRIDGE_FLATTEN_STACK_TEST=" + json.dumps({"status": "PASS", "base": result.name}))


if __name__ == "__main__":
    main()
