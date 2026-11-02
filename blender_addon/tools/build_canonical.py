from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Sequence

import bpy
from mathutils import Matrix, Vector


ADDON_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ADDON_ROOT.parent
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from motion_bridge.canonical.descriptor import (  # noqa: E402
    canonicalize_json_bytes,
)
from motion_bridge.canonical.rig_spec import (  # noqa: E402
    JOINTS,
    SKELETON_ID,
    SKELETON_VERSION,
    validate_rig_spec,
)
from motion_bridge.canonical.runtime_descriptor import (  # noqa: E402
    descriptor_from_armature,
)


ARMATURE_OBJECT_NAME = "MB_Canonical_Humanoid_v1"
GENERATOR_VERSION = 1


def _arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Motion Bridge Canonical Humanoid assets"
    )
    parser.add_argument(
        "--source-blend",
        type=Path,
        default=REPOSITORY_ROOT / "samples/canonical/mb_canonical_humanoid_v1.blend",
    )
    parser.add_argument(
        "--descriptor",
        type=Path,
        default=REPOSITORY_ROOT / "protocol/generated/canonical_humanoid_v1.json",
    )
    parser.add_argument(
        "--fbx",
        type=Path,
        default=(
            REPOSITORY_ROOT
            / "verification/unity-6000.2/Packages/com.kuko.motionbridge"
            / "Samples~/Canonical/MB_Canonical_Humanoid_v1.fbx"
        ),
    )
    parser.add_argument(
        "--create-source",
        action="store_true",
        help="Recreate the source .blend from the versioned rig specification",
    )
    parser.add_argument("--skip-fbx", action="store_true")
    return parser.parse_args(argv)


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for armature in tuple(bpy.data.armatures):
        if armature.users == 0:
            bpy.data.armatures.remove(armature)


def _roll_reference(head: Vector, tail: Vector) -> Vector:
    direction = (tail - head).normalized()
    forward = Vector((0.0, -1.0, 0.0))
    if abs(direction.dot(forward)) > 0.98:
        return Vector((0.0, 0.0, 1.0))
    return forward


def create_canonical_source(source_blend: Path) -> bpy.types.Object:
    validate_rig_spec()
    _clear_scene()

    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    scene.render.fps = 60
    scene.render.fps_base = 1.0

    armature = bpy.data.armatures.new(f"{ARMATURE_OBJECT_NAME}_Armature")
    obj = bpy.data.objects.new(ARMATURE_OBJECT_NAME, armature)
    scene.collection.objects.link(obj)
    obj.show_in_front = True
    obj["motion_bridge_skeleton_id"] = SKELETON_ID
    obj["motion_bridge_skeleton_version"] = SKELETON_VERSION
    obj["motion_bridge_generator_version"] = GENERATOR_VERSION

    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")

    edit_bones: dict[str, bpy.types.EditBone] = {}
    for joint in JOINTS[1:]:
        assert joint.head is not None and joint.tail is not None
        bone = armature.edit_bones.new(joint.name)
        bone.head = joint.head
        bone.tail = joint.tail
        if joint.parent_index > 0:
            parent = edit_bones[JOINTS[joint.parent_index].name]
            bone.parent = parent
            bone.use_connect = (bone.head - parent.tail).length <= 1e-7
        bone.align_roll(_roll_reference(bone.head, bone.tail))
        edit_bones[joint.name] = bone

    bpy.ops.object.mode_set(mode="OBJECT")
    for joint in JOINTS[1:]:
        bone = armature.bones[joint.name]
        bone["motion_bridge_index"] = joint.index
        bone["motion_bridge_human_bone"] = joint.human_bone or ""
        bone["motion_bridge_required"] = joint.required

    source_blend.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(source_blend), check_existing=False)
    return obj


def _load_source(source_blend: Path) -> bpy.types.Object:
    if not source_blend.is_file():
        raise FileNotFoundError(
            f"Canonical source is missing: {source_blend}. Run with --create-source first."
        )
    bpy.ops.wm.open_mainfile(filepath=str(source_blend), load_ui=False)
    obj = bpy.data.objects.get(ARMATURE_OBJECT_NAME)
    if obj is None or obj.type != "ARMATURE":
        raise ValueError(f"Missing canonical armature object: {ARMATURE_OBJECT_NAME}")
    return obj


def validate_canonical_object(obj: bpy.types.Object) -> None:
    validate_rig_spec()
    if obj.type != "ARMATURE":
        raise ValueError("Canonical object must be an armature")
    if obj.get("motion_bridge_skeleton_id") != SKELETON_ID:
        raise ValueError("Canonical skeleton id mismatch")
    if int(obj.get("motion_bridge_skeleton_version", -1)) != SKELETON_VERSION:
        raise ValueError("Canonical skeleton version mismatch")
    identity = Matrix.Identity(4)
    if any(
        abs(obj.matrix_world[row][column] - identity[row][column]) > 1e-7
        for row in range(4)
        for column in range(4)
    ):
        raise ValueError("Canonical armature object transform must be identity")

    actual_names = set(obj.data.bones.keys())
    expected_names = {joint.name for joint in JOINTS[1:]}
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        raise ValueError(f"Canonical bone set mismatch; missing={missing}, extra={extra}")

    for joint in JOINTS[1:]:
        bone = obj.data.bones[joint.name]
        expected_parent = None if joint.parent_index == 0 else JOINTS[joint.parent_index].name
        actual_parent = bone.parent.name if bone.parent else None
        if actual_parent != expected_parent:
            raise ValueError(
                f"Canonical parent mismatch for {joint.name}: "
                f"expected={expected_parent}, actual={actual_parent}"
            )


def descriptor_from_object(obj: bpy.types.Object) -> dict[str, Any]:
    validate_canonical_object(obj)
    return descriptor_from_armature(obj)


def write_descriptor(obj: bpy.types.Object, output: Path) -> str:
    descriptor = descriptor_from_object(obj)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonicalize_json_bytes(descriptor) + b"\n")
    return str(descriptor["skeletonHash"])


def _normalize_fbx_source_metadata(output: Path) -> None:
    """Keep the source filename without exposing the build machine's path.

    Blender's FBX exporter writes ApplicationNativeFile even when
    use_metadata=False. Re-encode the parsed elements so changing the string's
    length also updates the binary offsets; leave rig data untouched.
    """

    from io_scene_fbx import encode_bin, parse_fbx

    root, version = parse_fbx.parse(str(output))
    writers = {
        ord(code): method
        for code, method in (
            ("B", "add_bool"),
            ("C", "add_char"),
            ("Z", "add_int8"),
            ("Y", "add_int16"),
            ("I", "add_int32"),
            ("L", "add_int64"),
            ("F", "add_float32"),
            ("D", "add_float64"),
            ("R", "add_bytes"),
            ("S", "add_string"),
            ("b", "add_bool_array"),
            ("c", "add_byte_array"),
            ("i", "add_int32_array"),
            ("l", "add_int64_array"),
            ("f", "add_float32_array"),
            ("d", "add_float64_array"),
        )
    }
    metadata_path = (b"FBXHeaderExtension", b"SceneInfo", b"Properties70", b"P")

    def convert(element: Any, ancestry: tuple[bytes, ...] = ()) -> Any:
        path = (*ancestry, element.id) if element.id else ancestry
        values = list(element.props)
        if (
            path == metadata_path
            and values
            and values[0] == b"Original|ApplicationNativeFile"
        ):
            # Handle both separator styles even when building on another OS.
            values[-1] = values[-1].replace(b"\\", b"/").rsplit(b"/", 1)[-1]
        encoded = encode_bin.FBXElem(element.id)
        for prop_type, value in zip(element.props_type, values, strict=True):
            getattr(encoded, writers[prop_type])(value)
        encoded.elems.extend(convert(child, path) for child in element.elems)
        return encoded

    encoded_root = convert(root)
    with tempfile.TemporaryDirectory(prefix=".motion-bridge-fbx-", dir=output.parent) as temporary:
        sanitized = Path(temporary) / output.name
        encode_bin.write(str(sanitized), encoded_root, version)
        sanitized.replace(output)


def export_fbx(obj: bpy.types.Object, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    result = bpy.ops.export_scene.fbx(
        filepath=str(output),
        check_existing=False,
        use_selection=True,
        object_types={"ARMATURE"},
        global_scale=1.0,
        apply_unit_scale=True,
        apply_scale_options="FBX_SCALE_UNITS",
        axis_forward="-Z",
        axis_up="Y",
        use_space_transform=True,
        bake_space_transform=False,
        primary_bone_axis="Y",
        secondary_bone_axis="X",
        add_leaf_bones=False,
        armature_nodetype="NULL",
        use_armature_deform_only=False,
        bake_anim=False,
        use_custom_props=True,
        use_metadata=False,
    )
    if "FINISHED" not in result:
        raise RuntimeError(f"FBX export failed: {result}")
    _normalize_fbx_source_metadata(output)


def main(argv: Sequence[str] | None = None) -> int:
    if argv is not None:
        arguments = list(argv)
    elif "--" in sys.argv:
        arguments = sys.argv[sys.argv.index("--") + 1 :]
    else:
        arguments = []
    args = _arguments(arguments)
    source_blend = args.source_blend.resolve()
    obj = (
        create_canonical_source(source_blend)
        if args.create_source
        else _load_source(source_blend)
    )
    validate_canonical_object(obj)
    skeleton_hash = write_descriptor(obj, args.descriptor.resolve())
    if not args.skip_fbx:
        export_fbx(obj, args.fbx.resolve())

    print(
        json.dumps(
            {
                "sourceBlend": str(source_blend),
                "descriptor": str(args.descriptor.resolve()),
                "fbx": None if args.skip_fbx else str(args.fbx.resolve()),
                "jointCount": len(JOINTS),
                "skeletonHash": skeleton_hash,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
