from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "protocol/schema/protocol_v1.json"
PYTHON_PATH = ROOT / "blender_addon/motion_bridge/live/protocol_v1.py"
CSHARP_PATH = (
    ROOT
    / "verification/unity-6000.2/Packages/com.kuko.motionbridge"
    / "Runtime/Protocol/MotionBridgeProtocol.cs"
)


def load_python_module():
    spec = importlib.util.spec_from_file_location("motion_bridge_protocol_v1", PYTHON_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {PYTHON_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def csharp_int(source: str, name: str) -> int:
    match = re.search(rf"public const (?:byte|int) {re.escape(name)} = (\d+);", source)
    if match is None:
        raise ValueError(f"Missing C# constant: {name}")
    return int(match.group(1))


def main() -> int:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    protocol = schema["protocol"]
    pose = schema["posePacket"]
    python = load_python_module()
    csharp = CSHARP_PATH.read_text(encoding="utf-8")

    comparisons = {
        "major": (protocol["major"], python.PROTOCOL_MAJOR, csharp_int(csharp, "Major")),
        "minor": (protocol["minor"], python.PROTOCOL_MINOR, csharp_int(csharp, "Minor")),
        "maxPacketBytes": (
            protocol["maxPacketBytes"],
            python.MAX_PACKET_BYTES,
            csharp_int(csharp, "MaxPacketBytes"),
        ),
        "controlLengthPrefixBytes": (
            protocol["controlLengthPrefixBytes"],
            python.CONTROL_LENGTH_PREFIX_BYTES,
            csharp_int(csharp, "ControlLengthPrefixBytes"),
        ),
        "maxControlPayloadBytes": (
            protocol["maxControlPayloadBytes"],
            python.MAX_CONTROL_PAYLOAD_BYTES,
            csharp_int(csharp, "MaxControlPayloadBytes"),
        ),
        "maxDescriptorJointCount": (
            protocol["maxDescriptorJointCount"],
            python.MAX_DESCRIPTOR_JOINTS,
            csharp_int(csharp, "MaxDescriptorJointCount"),
        ),
        "maxPoseJointCount": (
            protocol["maxPoseJointCount"],
            python.MAX_JOINTS,
            csharp_int(csharp, "MaxPoseJointCount"),
        ),
        "defaultControlPort": (
            protocol["defaultControlPort"],
            python.DEFAULT_CONTROL_PORT,
            csharp_int(csharp, "DefaultControlPort"),
        ),
        "headerBytes": (pose["headerBytes"], python.HEADER_BYTES, csharp_int(csharp, "HeaderBytes")),
        "fixedPayloadBytes": (
            pose["fixedPayloadBytes"],
            python.FIXED_PAYLOAD_BYTES,
            csharp_int(csharp, "FixedPayloadBytes"),
        ),
        "contactBlockBytes": (
            pose["contactBlockBytes"],
            python.CONTACT_BLOCK_BYTES,
            csharp_int(csharp, "ContactBlockBytes"),
        ),
        "quaternionBytes": (
            pose["quaternionBytes"],
            python.QUATERNION.size,
            csharp_int(csharp, "QuaternionBytes"),
        ),
    }

    mismatches = [f"{name}: schema/python/C#={values}" for name, values in comparisons.items() if len(set(values)) != 1]
    if mismatches:
        print("\n".join(mismatches), file=sys.stderr)
        return 1

    print(f"Protocol constants match ({len(comparisons)} checks).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
