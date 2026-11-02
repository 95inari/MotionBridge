from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

import bpy


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from motion_bridge import addon  # noqa: E402


def _assert_close(actual: float, expected: float, label: str) -> None:
    if abs(actual - expected) > 1.0e-4:
        raise AssertionError(f"{label}: {actual!r} != {expected!r}")


def main() -> None:
    addon.register()
    registered = True
    try:
        settings = bpy.context.scene.motion_bridge_session
        user_tree = bpy.data.node_groups.new(
            addon._ENVELOPE_CURVE_TREE_NAME,
            "ShaderNodeTree",
        )
        user_tree["user_sentinel"] = "unchanged"
        user_tree[addon._ENVELOPE_CURVE_OWNED_PROPERTY] = True
        user_tree[addon._ENVELOPE_CURVE_SCHEMA_PROPERTY] = 999
        user_tree[addon._ENVELOPE_CURVE_OWNER_PROPERTY] = "user-owned"
        user_tree.use_fake_user = True
        user_node = user_tree.nodes.new("ShaderNodeFloatCurve")
        user_node.name = addon._ENVELOPE_CURVE_NODE_NAME
        user_node.label = "User Envelope Curve"
        user_node["user_sentinel"] = "unchanged"
        user_tree_pointer = user_tree.as_pointer()
        user_node_pointer = user_node.as_pointer()
        group_count = len(bpy.data.node_groups)
        if addon._find_envelope_curve_node(settings) is not None:
            raise AssertionError("Fresh UI unexpectedly found envelope curve data")
        if len(bpy.data.node_groups) != group_count:
            raise AssertionError("Read-only curve lookup created a NodeTree")
        if settings.correction_envelope_curve_tree is not None:
            raise AssertionError("Read-only curve lookup changed the Scene pointer")

        node = addon._ensure_envelope_curve_node(settings)
        if node.bl_idname != "ShaderNodeFloatCurve":
            raise AssertionError("Envelope UI did not create a Float Curve node")
        owned_tree = node.id_data
        if owned_tree.as_pointer() == user_tree_pointer:
            raise AssertionError("Envelope UI reused a same-named user NodeTree")
        owner = bpy.context.scene.get(addon._ENVELOPE_SCENE_ID_PROPERTY)
        if not isinstance(owner, str) or not owner:
            raise AssertionError("Envelope UI omitted the per-Scene owner ID")
        for data, label in ((owned_tree, "NodeTree"), (node, "node")):
            if (
                not bool(data.get(addon._ENVELOPE_CURVE_OWNED_PROPERTY, False))
                or data.get(addon._ENVELOPE_CURVE_SCHEMA_PROPERTY)
                != addon._ENVELOPE_CURVE_SCHEMA
                or data.get(addon._ENVELOPE_CURVE_OWNER_PROPERTY) != owner
            ):
                raise AssertionError(f"Envelope {label} ownership is incomplete")
        if (
            owned_tree.get(addon._ENVELOPE_CURVE_OWNER_SCENE_PROPERTY)
            != bpy.context.scene
        ):
            raise AssertionError("Envelope NodeTree omitted its owning Scene reference")
        if (
            user_tree.get("user_sentinel") != "unchanged"
            or user_node.get("user_sentinel") != "unchanged"
            or user_node.label != "User Envelope Curve"
        ):
            raise AssertionError("Envelope initialization changed same-named user data")

        settings.correction_envelope_curve_tree = None
        pointerless_group_count = len(bpy.data.node_groups)
        found = addon._find_envelope_curve_node(settings)
        if found is not None:
            raise AssertionError("Read-only lookup searched global named NodeTrees")
        if settings.correction_envelope_curve_tree is not None:
            raise AssertionError("Panel-safe lookup wrote the Scene pointer")
        if len(bpy.data.node_groups) != pointerless_group_count:
            raise AssertionError("Panel-safe lookup changed NodeTree data")
        node = addon._ensure_envelope_curve_node(settings)
        if (
            settings.correction_envelope_curve_tree is None
            or settings.correction_envelope_curve_tree.as_pointer()
            != node.id_data.as_pointer()
        ):
            raise AssertionError(
                "Explicit curve initialization did not restore the pointer"
            )
        if node.id_data.as_pointer() in {user_tree_pointer, owned_tree.as_pointer()}:
            raise AssertionError(
                "Explicit initialization reused an unowned global Tree"
            )
        mapping = node.mapping
        if not mapping.use_clip:
            raise AssertionError("Safe Envelope UI must clip the normal 0..1 range")

        settings.correction_envelope_preset = "PLATEAU"
        node = addon._ensure_envelope_curve_node(settings)
        plateau_signature = addon._envelope_curve_signature(node)
        source_tree_pointer = node.id_data.as_pointer()
        source_owner_scene = node.id_data.get(
            addon._ENVELOPE_CURVE_OWNER_SCENE_PROPERTY
        )
        second_scene = bpy.context.scene.copy()
        second_scene.name = "Motion Bridge Envelope Scene"
        second_settings = second_scene.motion_bridge_session
        if second_scene.get(addon._ENVELOPE_SCENE_ID_PROPERTY) != owner:
            raise AssertionError(
                "Scene copy did not exercise duplicated owner metadata"
            )
        second_node = addon._ensure_envelope_curve_node(second_settings)
        if second_node.id_data.as_pointer() == node.id_data.as_pointer():
            raise AssertionError("Envelope Curve NodeTree was shared between Scenes")
        second_owner = second_scene.get(addon._ENVELOPE_SCENE_ID_PROPERTY)
        if (
            not isinstance(second_owner, str)
            or not second_owner
            or second_owner == owner
        ):
            raise AssertionError("Envelope Curve owner IDs are not unique per Scene")
        if addon._envelope_curve_signature(second_node) != plateau_signature:
            raise AssertionError("Scene copy reset the PLATEAU Envelope curve")
        if addon._envelope_curve_signature(node) != plateau_signature:
            raise AssertionError("Scene copy changed the original PLATEAU curve")
        if (
            node.id_data.as_pointer() != source_tree_pointer
            or node.get(addon._ENVELOPE_CURVE_OWNER_PROPERTY) != owner
            or node.id_data.get(addon._ENVELOPE_CURVE_OWNER_PROPERTY) != owner
            or node.id_data.get(addon._ENVELOPE_CURVE_OWNER_SCENE_PROPERTY)
            != source_owner_scene
        ):
            raise AssertionError("Scene copy retagged the original Envelope Tree")
        for data, label in (
            (second_node.id_data, "duplicated PLATEAU Tree"),
            (second_node, "duplicated PLATEAU node"),
        ):
            if data.get(addon._ENVELOPE_CURVE_OWNER_PROPERTY) != second_owner:
                raise AssertionError(f"Envelope {label} retained the source owner")
        if (
            second_node.id_data.get(addon._ENVELOPE_CURVE_OWNER_SCENE_PROPERTY)
            != second_scene
        ):
            raise AssertionError("Duplicated PLATEAU Tree retained the source Scene")

        custom_points = (
            (0.0, 0.15),
            (0.17, 0.85),
            (0.61, 0.35),
            (1.0, 0.9),
        )
        addon._set_envelope_curve_points(node, custom_points)
        custom_signature = addon._envelope_curve_signature(node)
        custom_source_tree_pointer = node.id_data.as_pointer()
        custom_source_owner = node.id_data.get(
            addon._ENVELOPE_CURVE_OWNER_PROPERTY
        )
        custom_source_owner_scene = node.id_data.get(
            addon._ENVELOPE_CURVE_OWNER_SCENE_PROPERTY
        )
        custom_scene = bpy.context.scene.copy()
        custom_scene.name = "Motion Bridge Custom Envelope Scene"
        custom_settings = custom_scene.motion_bridge_session
        custom_node = addon._ensure_envelope_curve_node(custom_settings)
        custom_owner = custom_scene.get(addon._ENVELOPE_SCENE_ID_PROPERTY)
        if custom_node.id_data.as_pointer() == custom_source_tree_pointer:
            raise AssertionError("CUSTOM Envelope Tree was shared between Scenes")
        if addon._envelope_curve_signature(custom_node) != custom_signature:
            raise AssertionError("Scene copy reset the CUSTOM Envelope curve")
        if addon._envelope_curve_signature(node) != custom_signature:
            raise AssertionError("Scene copy changed the original CUSTOM curve")
        if (
            node.id_data.as_pointer() != custom_source_tree_pointer
            or node.get(addon._ENVELOPE_CURVE_OWNER_PROPERTY)
            != custom_source_owner
            or node.id_data.get(addon._ENVELOPE_CURVE_OWNER_PROPERTY)
            != custom_source_owner
            or node.id_data.get(addon._ENVELOPE_CURVE_OWNER_SCENE_PROPERTY)
            != custom_source_owner_scene
        ):
            raise AssertionError("Scene copy retagged the original CUSTOM Tree")
        if (
            not isinstance(custom_owner, str)
            or not custom_owner
            or custom_owner == owner
            or custom_node.get(addon._ENVELOPE_CURVE_OWNER_PROPERTY) != custom_owner
            or custom_node.id_data.get(addon._ENVELOPE_CURVE_OWNER_PROPERTY)
            != custom_owner
            or custom_node.id_data.get(
                addon._ENVELOPE_CURVE_OWNER_SCENE_PROPERTY
            )
            != custom_scene
        ):
            raise AssertionError("Duplicated CUSTOM curve ownership was not retagged")

        second_settings.correction_envelope_curve_tree = user_tree
        replacement = addon._ensure_envelope_curve_node(second_settings)
        if replacement.id_data.as_pointer() == user_tree_pointer:
            raise AssertionError("Envelope UI accepted a foreign owner/schema Tree")
        if user_node.as_pointer() != user_node_pointer:
            raise AssertionError("Foreign Envelope node was replaced")
        shared_scene = bpy.context.scene.copy()
        shared_scene.name = "Motion Bridge Untouched Scene Copy"
        shared_scene.use_fake_user = True
        if (
            shared_scene.motion_bridge_session.correction_envelope_curve_tree.as_pointer()
            != node.id_data.as_pointer()
        ):
            raise AssertionError(
                "Scene copy did not exercise a shared Envelope pointer"
            )

        expected_endpoints = {
            "SMOOTH_PULSE": (0.0, 0.0),
            "PLATEAU": (0.0, 0.0),
            "EASE_IN": (0.0, 1.0),
            "EASE_OUT": (0.0, 1.0),
            "EASE_IN_OUT": (0.0, 1.0),
            "S_CURVE": (0.0, 1.0),
        }
        for preset, (expected_start, expected_end) in expected_endpoints.items():
            settings.correction_envelope_preset = preset
            node = addon._ensure_envelope_curve_node(settings)
            evaluate = addon._envelope_curve_evaluator(node)
            _assert_close(evaluate(0.0), expected_start, f"{preset} start")
            _assert_close(evaluate(1.0), expected_end, f"{preset} end")
            sample_times = getattr(evaluate, "sample_times", ())
            if 0.0 not in sample_times or 1.0 not in sample_times:
                raise AssertionError(f"{preset} omitted adaptive sample boundaries")

        settings.correction_envelope_preset = "SMOOTH_PULSE"
        node = addon._ensure_envelope_curve_node(settings)
        evaluate = addon._envelope_curve_evaluator(node)
        _assert_close(evaluate(0.5), 1.0, "Smooth Pulse midpoint")
        if not (evaluate(0.25) > 0.0 and evaluate(0.75) > 0.0):
            raise AssertionError("Smooth Pulse lost its descending/ascending segments")

        metadata = json.loads(addon._envelope_curve_metadata(settings, node))
        if (
            metadata.get("schema") != 1
            or metadata.get("preset") != "SMOOTH_PULSE"
            or len(metadata.get("points", ())) != 3
        ):
            raise AssertionError(f"Envelope Curve metadata is incomplete: {metadata!r}")

        for operator_name in (
            "begin_correction_envelope",
            "apply_correction_envelope",
            "cancel_correction_envelope",
            "apply_rig_visualization",
            "restore_rig_visualization",
            "exit_saved_correction_tweak",
        ):
            if not hasattr(bpy.ops.motion_bridge, operator_name):
                raise AssertionError(
                    f"Motion Bridge operator is not registered: {operator_name}"
                )
        for property_name in (
            "rig_visual_style",
            "rig_visual_source_visible",
            "rig_visual_wire_opacity",
            "rig_visual_show_names",
        ):
            if not hasattr(settings, property_name):
                raise AssertionError(
                    f"Rig View setting is not registered: {property_name}"
                )

        localized_properties = {
            "rig_visual_style": "修正リグの表示",
            "rig_visual_source_visible": "比較用の元リグ",
            "rig_visual_wire_opacity": "ワイヤー透過度",
            "rig_visual_show_names": "ボーン名",
            "correction_include_location": "位置も修正（Hipsのみ）",
            "correction_blend_frames": "前後のなじませ",
            "correction_interpolation": "補間方法",
            "correction_envelope_preset": "カーブ形状",
        }
        for property_name, expected_label in localized_properties.items():
            actual_label = settings.bl_rna.properties[property_name].name
            if actual_label != expected_label:
                raise AssertionError(
                    f"Japanese UI label mismatch for {property_name}: "
                    f"{actual_label!r} != {expected_label!r}"
                )
        preset_items = {
            item.identifier: item
            for item in settings.bl_rna.properties[
                "correction_envelope_preset"
            ].enum_items
        }
        if preset_items["EASE_OUT"].name != "早めに強く":
            raise AssertionError("EASE_OUT label contradicts its increasing curve")
        if "修正を強め" not in preset_items["EASE_OUT"].description:
            raise AssertionError("EASE_OUT tooltip contradicts its increasing curve")

        localized_types = {
            addon.MB_PT_Session: "詳細設定",
            addon.MB_OT_ApplyRigVisualization: "リグ表示を適用",
            addon.MB_OT_InsertCorrectionPoseKeys: "現在フレームに修正を記録",
            addon.MB_OT_BeginCorrectionEnvelope: "補正カーブをプレビュー",
            addon.MB_OT_ExitSavedCorrectionTweak: "保存された修正モードを安全に終了",
            addon.MB_OT_ExportCorrectedFbx: "修正済みFBXを書き出す",
        }
        for ui_type, expected_label in localized_types.items():
            if ui_type.bl_label != expected_label:
                raise AssertionError(
                    f"Japanese UI label mismatch for {ui_type.__name__}: "
                    f"{ui_type.bl_label!r} != {expected_label!r}"
                )

        user_tree_name = user_tree.name
        with tempfile.TemporaryDirectory(prefix="motion_bridge_envelope_") as temp_dir:
            blend_path = str(Path(temp_dir) / "ownership.blend")
            if bpy.ops.wm.save_as_mainfile(filepath=blend_path) != {"FINISHED"}:
                raise AssertionError("Envelope ownership fixture could not be saved")
            if bpy.ops.wm.open_mainfile(filepath=blend_path) != {"FINISHED"}:
                raise AssertionError("Envelope ownership fixture could not be reloaded")

        user_tree = bpy.data.node_groups.get(user_tree_name)
        if user_tree is None:
            raise AssertionError("Save/load removed the same-named user NodeTree")
        user_node = user_tree.nodes.get(addon._ENVELOPE_CURVE_NODE_NAME)
        if (
            user_node is None
            or user_tree.get("user_sentinel") != "unchanged"
            or user_node.get("user_sentinel") != "unchanged"
            or user_node.label != "User Envelope Curve"
        ):
            raise AssertionError("Save/load changed same-named user data")
        user_tree_pointer = user_tree.as_pointer()
        user_node_pointer = user_node.as_pointer()
        settings = bpy.context.scene.motion_bridge_session
        if addon._find_envelope_curve_node(settings) is None:
            raise AssertionError("Save/load lost the active Scene Envelope curve")
        owner = bpy.context.scene.get(addon._ENVELOPE_SCENE_ID_PROPERTY)
        second_scene = bpy.data.scenes.get("Motion Bridge Envelope Scene")
        if second_scene is None:
            raise AssertionError("Save/load lost the secondary Scene")
        second_owner = second_scene.get(addon._ENVELOPE_SCENE_ID_PROPERTY)
        if (
            not isinstance(second_owner, str)
            or not second_owner
            or second_owner == owner
        ):
            raise AssertionError("Save/load lost per-Scene Envelope ownership")
        if addon._find_envelope_curve_node(second_scene.motion_bridge_session) is None:
            raise AssertionError("Save/load lost the secondary Scene Envelope curve")
        shared_scene = bpy.data.scenes.get("Motion Bridge Untouched Scene Copy")
        if shared_scene is None:
            raise AssertionError("Save/load lost the untouched Scene copy")
        referenced_owned_tree = settings.correction_envelope_curve_tree
        referenced_owned_tree.use_fake_user = True

        orphan_scene = bpy.data.scenes.new("Motion Bridge Deleted Envelope Owner")
        orphan_node = addon._ensure_envelope_curve_node(
            orphan_scene.motion_bridge_session
        )
        orphan_tree = orphan_node.id_data
        orphan_tree_name = orphan_tree.name
        orphan_owner = orphan_tree.get(addon._ENVELOPE_CURVE_OWNER_PROPERTY)
        if addon._valid_envelope_owner_id(orphan_owner) != orphan_owner:
            raise AssertionError("Owned Envelope Tree did not use a valid owner UUID")
        bpy.data.scenes.remove(orphan_scene)
        if (
            orphan_tree.users != 0
            or orphan_tree.get(addon._ENVELOPE_CURVE_OWNER_SCENE_PROPERTY) is not None
        ):
            raise AssertionError("Deleted Scene did not create the orphan regression state")

        fake_owned_tree = bpy.data.node_groups.new(
            addon._ENVELOPE_CURVE_TREE_NAME,
            "ShaderNodeTree",
        )
        fake_owned_tree[addon._ENVELOPE_CURVE_OWNED_PROPERTY] = True
        fake_owned_tree[addon._ENVELOPE_CURVE_SCHEMA_PROPERTY] = (
            addon._ENVELOPE_CURVE_SCHEMA
        )
        fake_owned_tree[addon._ENVELOPE_CURVE_OWNER_PROPERTY] = "user-owned"
        fake_owned_tree["user_sentinel"] = "unchanged"
        fake_owned_node = fake_owned_tree.nodes.new("ShaderNodeFloatCurve")
        fake_owned_node[addon._ENVELOPE_CURVE_OWNED_PROPERTY] = True
        fake_owned_node[addon._ENVELOPE_CURVE_SCHEMA_PROPERTY] = (
            addon._ENVELOPE_CURVE_SCHEMA
        )
        fake_owned_node[addon._ENVELOPE_CURVE_OWNER_PROPERTY] = "user-owned"
        fake_owned_node["user_sentinel"] = "unchanged"
        fake_owned_tree_pointer = fake_owned_tree.as_pointer()
        fake_owned_node_pointer = fake_owned_node.as_pointer()

        addon._cleanup_envelope_curve_data()
        if (
            settings.correction_envelope_curve_tree is not None
            or shared_scene.motion_bridge_session.correction_envelope_curve_tree
            is not None
        ):
            raise AssertionError("Envelope cleanup left a copied Scene pointer")
        if bpy.data.node_groups.get(orphan_tree_name) is not None:
            raise AssertionError("Envelope cleanup left a deleted-Scene orphan Tree")
        if (
            fake_owned_tree.as_pointer() != fake_owned_tree_pointer
            or fake_owned_node.as_pointer() != fake_owned_node_pointer
            or fake_owned_tree.get("user_sentinel") != "unchanged"
            or fake_owned_node.get("user_sentinel") != "unchanged"
        ):
            raise AssertionError("Envelope cleanup changed fake-owned user data")
        addon.unregister()
        registered = False
        if bpy.data.node_groups.get(user_tree.name) is None:
            raise AssertionError("Add-on cleanup removed a user NodeTree")
        if (
            user_tree.as_pointer() != user_tree_pointer
            or user_node.as_pointer() != user_node_pointer
            or user_tree.get("user_sentinel") != "unchanged"
            or user_node.get("user_sentinel") != "unchanged"
        ):
            raise AssertionError("Add-on cleanup changed same-named user data")
        remaining_owned_trees = [
            tree
            for tree in bpy.data.node_groups
            if bool(tree.get(addon._ENVELOPE_CURVE_OWNED_PROPERTY, False))
            and tree.get(addon._ENVELOPE_CURVE_SCHEMA_PROPERTY)
            == addon._ENVELOPE_CURVE_SCHEMA
        ]
        if {tree.as_pointer() for tree in remaining_owned_trees} != {
            referenced_owned_tree.as_pointer(),
            fake_owned_tree_pointer,
        }:
            raise AssertionError(
                "Add-on cleanup removed referenced data or left unreferenced owned data"
            )
        if (
            fake_owned_tree.as_pointer() != fake_owned_tree_pointer
            or fake_owned_node.as_pointer() != fake_owned_node_pointer
            or fake_owned_tree.get("user_sentinel") != "unchanged"
            or fake_owned_node.get("user_sentinel") != "unchanged"
        ):
            raise AssertionError("Add-on unregister changed fake-owned user data")
        referenced_owned_tree.use_fake_user = False
        bpy.data.node_groups.remove(referenced_owned_tree)
        bpy.data.node_groups.remove(fake_owned_tree)

        print(
            "MOTION_BRIDGE_CORRECTION_ENVELOPE_UI_TEST="
            + json.dumps(
                {
                    "status": "PASS",
                    "curveMapping": True,
                    "drawLookupReadOnly": True,
                    "ownership": True,
                    "perScene": True,
                    "sceneCopyCurvePreserved": True,
                    "deletedSceneOrphanCleanup": True,
                    "saveLoad": True,
                    "presets": len(expected_endpoints),
                    "metadata": True,
                    "operators": True,
                    "rigView": True,
                    "japaneseUI": True,
                },
                sort_keys=True,
            )
        )
    finally:
        if registered:
            addon.unregister()


if __name__ == "__main__":
    main()
