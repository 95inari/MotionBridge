"""Exercise the shared FCurve/group API on Blender 4.5 and 5.x."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import bpy


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from motion_bridge.editing.action_api import (  # noqa: E402
    ActionChannelAccess,
    create_action_for_id,
    ensure_fcurve,
    iter_action_fcurves,
    remove_fcurve,
)


def main() -> None:
    obj = bpy.data.objects.new("MB_Action_API_Test", None)
    access = create_action_for_id(
        "MB_Action_API_Test", obj, frame_start=1, frame_end=10
    )
    try:
        channelbag = access.channelbag
        assert channelbag is not None
        x_curve = ensure_fcurve(access, "location", 0, group_name="Root")
        y_curve = ensure_fcurve(access, "location", 1, group_name="Root")
        assert x_curve.group == y_curve.group == channelbag.groups["Root"]
        assert len(channelbag.groups) == 1
        x_curve.keyframe_points.insert(1, 0.25)

        # Reusing a channel must preserve its keys/group, without duplicates.
        reused = ensure_fcurve(access, "location", 0, group_name="Other")
        assert reused == x_curve
        assert len(reused.keyframe_points) == 1
        assert abs(reused.evaluate(1) - 0.25) < 1.0e-6
        assert reused.group.name == "Root"
        assert len(channelbag.groups) == 1
        assert len(channelbag.fcurves) == 2

        ungrouped = ensure_fcurve(access, "scale", 0, group_name="")
        assert ungrouped.group is None
        assert len(channelbag.groups) == 1

        # A rebuilt access handle must resolve the same channelbag and group.
        rebuilt = ActionChannelAccess(access.action, access.slot, None)
        z_curve = ensure_fcurve(rebuilt, "location", 2, group_name="Root")
        assert z_curve.group == x_curve.group
        assert len(channelbag.groups) == 1

        # Matching paths/group names in a different slot are independent.
        second_slot = access.action.slots.new("OBJECT", "MB_Second_Object")
        strip = access.action.layers[0].strips[0]
        second_bag = strip.channelbags.new(second_slot)
        second_access = ActionChannelAccess(access.action, second_slot, second_bag)
        second_curve = ensure_fcurve(
            second_access, "location", 0, group_name="Root"
        )
        assert second_curve != x_curve
        assert second_curve.group == second_bag.groups["Root"]
        assert second_curve.group != x_curve.group
        assert len(second_bag.fcurves) == len(second_bag.groups) == 1
        assert len(tuple(iter_action_fcurves(access.action, access.slot))) == 4

        remove_fcurve(second_access, second_curve)
        assert len(second_bag.fcurves) == 0
        assert len(channelbag.fcurves) == 4
        print(json.dumps({"status": "passed", "blender": bpy.app.version_string}))
    finally:
        bpy.data.actions.remove(access.action)
        bpy.data.objects.remove(obj)


if __name__ == "__main__":
    main()
