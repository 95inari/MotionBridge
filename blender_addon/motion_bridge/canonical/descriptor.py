from __future__ import annotations

import hashlib
import math
import re
from typing import Any


__all__ = (
    "canonicalize_json_bytes",
    "skeleton_hash_hex",
    "validate_descriptor",
)

MAX_SAFE_INTEGER = (1 << 53) - 1
MAX_DESCRIPTOR_JOINTS = 64
COORDINATE_SPACE = "unity-left-handed-y-up-z-forward"

_DESCRIPTOR_FIELDS = frozenset(
    {
        "type",
        "protocolMajor",
        "protocolMinor",
        "sessionId",
        "requestId",
        "skeletonId",
        "skeletonHash",
        "coordinateSpace",
        "rootIndex",
        "joints",
    }
)
_JOINT_FIELDS = frozenset(
    {
        "index",
        "name",
        "humanBone",
        "parentIndex",
        "jointPath",
        "required",
        "restLocalPosition",
        "restLocalRotation",
        "restGlobalRotation",
    }
)
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")

# Session/request identity and the derived hash are deliberately excluded.
# The resulting value is stable enough to store on the generated Source Avatar.
SKELETON_HASH_FIELDS = (
    "skeletonId",
    "coordinateSpace",
    "rootIndex",
    "joints",
)


def canonicalize_json_bytes(value: Any) -> bytes:
    """Return RFC 8785/JCS-compatible UTF-8 bytes for supported JSON values.

    Supported values are ``None``, booleans, safe integers, finite binary64
    floats, strings, lists, and dictionaries with string keys.  Container
    cycles, lone Unicode surrogates, non-finite floats, and other Python types
    are rejected rather than being silently coerced.
    """

    parts: list[str] = []
    _serialize(value, parts, set())
    return "".join(parts).encode("utf-8")


def canonical_sha256_hex(value: Any) -> str:
    """Hash the JCS-compatible representation of *value* with SHA-256."""

    return hashlib.sha256(canonicalize_json_bytes(value)).hexdigest()


def skeleton_hash_projection(descriptor: dict[str, Any]) -> dict[str, Any]:
    """Return the stable skeleton data covered by ``skeletonHash``.

    The wire envelope contains per-connection ``sessionId`` and ``requestId``
    values, while ``skeletonHash`` is derived data.  None of those fields may
    affect the semantic skeleton identity shared with the Unity Source Avatar.
    """

    if not isinstance(descriptor, dict):
        raise TypeError("descriptor must be a dictionary")
    missing = [field for field in SKELETON_HASH_FIELDS if field not in descriptor]
    if missing:
        raise ValueError(f"descriptor is missing hash fields: {', '.join(missing)}")
    return {field: descriptor[field] for field in SKELETON_HASH_FIELDS}


def skeleton_hash_hex(descriptor: dict[str, Any]) -> str:
    """Return the lowercase SHA-256 hex digest for a skeleton descriptor."""

    return canonical_sha256_hex(skeleton_hash_projection(descriptor))


def with_skeleton_hash(descriptor: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow descriptor copy with its semantic hash populated."""

    result = dict(descriptor)
    result["skeletonHash"] = skeleton_hash_hex(result)
    return result


def validate_descriptor(value: Any) -> None:
    """Validate a wire Skeleton Descriptor v1 or raise ``ValueError``.

    In addition to the JSON Schema shape, this verifies ordered hierarchy,
    joint paths, finite transforms, and the semantic skeleton hash.
    """

    descriptor = _require_object(value, "descriptor")
    _require_exact_fields(descriptor, _DESCRIPTOR_FIELDS, "descriptor")
    if descriptor["type"] != "skeleton_descriptor":
        raise ValueError("type must be 'skeleton_descriptor'")
    _require_integer(descriptor["protocolMajor"], "protocolMajor", minimum=1, maximum=1)
    _require_integer(descriptor["protocolMinor"], "protocolMinor", minimum=0, maximum=255)
    _require_integer(descriptor["sessionId"], "sessionId", minimum=0, maximum=0xFFFFFFFF)
    _require_integer(descriptor["requestId"], "requestId", minimum=0, maximum=0xFFFFFFFF)
    _require_nonempty_string(descriptor["skeletonId"], "skeletonId")
    if descriptor["coordinateSpace"] != COORDINATE_SPACE:
        raise ValueError(f"coordinateSpace must be {COORDINATE_SPACE!r}")
    _require_integer(descriptor["rootIndex"], "rootIndex", minimum=0, maximum=0)

    joints = descriptor["joints"]
    if not isinstance(joints, list):
        raise ValueError("joints must be an array")
    if not 1 <= len(joints) <= MAX_DESCRIPTOR_JOINTS:
        raise ValueError("joints must contain between 1 and 64 entries")

    names: set[str] = set()
    human_bones: set[str] = set()
    paths: list[str] = []
    for expected_index, candidate in enumerate(joints):
        location = f"joints[{expected_index}]"
        joint = _require_object(candidate, location)
        _require_exact_fields(joint, _JOINT_FIELDS, location)
        _require_integer(
            joint["index"], f"{location}.index", minimum=0, maximum=MAX_DESCRIPTOR_JOINTS - 1
        )
        if joint["index"] != expected_index:
            raise ValueError("joint indices must be contiguous and array-ordered")

        name = _require_nonempty_string(joint["name"], f"{location}.name")
        if name in names:
            raise ValueError(f"duplicate joint name: {name}")
        names.add(name)

        human_bone = joint["humanBone"]
        if human_bone is not None:
            human_bone = _require_nonempty_string(human_bone, f"{location}.humanBone")
            if human_bone in human_bones:
                raise ValueError(f"duplicate humanBone: {human_bone}")
            human_bones.add(human_bone)

        if type(joint["required"]) is not bool:
            raise ValueError(f"{location}.required must be a boolean")
        parent_index = _require_integer(
            joint["parentIndex"],
            f"{location}.parentIndex",
            minimum=-1,
            maximum=MAX_DESCRIPTOR_JOINTS - 1,
        )
        joint_path = _require_string(joint["jointPath"], f"{location}.jointPath")
        if expected_index == 0:
            if parent_index != -1 or joint_path != "":
                raise ValueError("joint 0 must have parentIndex -1 and an empty jointPath")
        else:
            if parent_index < 0 or parent_index >= expected_index:
                raise ValueError(f"{location}.parentIndex must refer to an earlier joint")
            parent_path = paths[parent_index]
            expected_path = f"{parent_path}/{name}" if parent_path else name
            if joint_path != expected_path:
                raise ValueError(f"{location}.jointPath must be {expected_path!r}")
        paths.append(joint_path)

        _require_number_array(joint["restLocalPosition"], f"{location}.restLocalPosition", 3)
        _require_quaternion(joint["restLocalRotation"], f"{location}.restLocalRotation")
        _require_quaternion(joint["restGlobalRotation"], f"{location}.restGlobalRotation")

    skeleton_hash = _require_string(descriptor["skeletonHash"], "skeletonHash")
    if _SHA256_HEX.fullmatch(skeleton_hash) is None:
        raise ValueError("skeletonHash must be 64 lowercase hexadecimal characters")
    if skeleton_hash != skeleton_hash_hex(descriptor):
        raise ValueError("skeletonHash does not match the canonical skeleton projection")


# Compatibility aliases for callers built before the public API names settled.
canonicalize_json = canonicalize_json_bytes
compute_skeleton_hash = skeleton_hash_hex


def _serialize(value: Any, parts: list[str], active_containers: set[int]) -> None:
    if value is None:
        parts.append("null")
        return
    if value is True:
        parts.append("true")
        return
    if value is False:
        parts.append("false")
        return
    if isinstance(value, int):
        if value < -MAX_SAFE_INTEGER or value > MAX_SAFE_INTEGER:
            raise ValueError("integer is outside the interoperable IEEE-754 range")
        parts.append(str(value))
        return
    if isinstance(value, float):
        parts.append(_format_float(value))
        return
    if isinstance(value, str):
        parts.append(_quote_string(value))
        return
    if isinstance(value, list):
        _serialize_list(value, parts, active_containers)
        return
    if isinstance(value, dict):
        _serialize_dict(value, parts, active_containers)
        return
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def _serialize_list(
    value: list[Any], parts: list[str], active_containers: set[int]
) -> None:
    identity = id(value)
    if identity in active_containers:
        raise ValueError("cyclic JSON containers are not supported")
    active_containers.add(identity)
    try:
        parts.append("[")
        for index, item in enumerate(value):
            if index:
                parts.append(",")
            _serialize(item, parts, active_containers)
        parts.append("]")
    finally:
        active_containers.remove(identity)


def _serialize_dict(
    value: dict[Any, Any], parts: list[str], active_containers: set[int]
) -> None:
    identity = id(value)
    if identity in active_containers:
        raise ValueError("cyclic JSON containers are not supported")
    active_containers.add(identity)
    try:
        for key in value:
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            _validate_unicode(key)

        parts.append("{")
        for index, key in enumerate(sorted(value, key=_utf16_sort_key)):
            if index:
                parts.append(",")
            parts.append(_quote_string(key))
            parts.append(":")
            _serialize(value[key], parts, active_containers)
        parts.append("}")
    finally:
        active_containers.remove(identity)


def _utf16_sort_key(value: str) -> bytes:
    # Big-endian bytes compare exactly like unsigned UTF-16 code units.
    return value.encode("utf-16-be")


def _validate_unicode(value: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError("lone Unicode surrogates are not valid JCS strings")


def _quote_string(value: str) -> str:
    _validate_unicode(value)
    result = ['"']
    short_escapes = {
        "\b": "\\b",
        "\t": "\\t",
        "\n": "\\n",
        "\f": "\\f",
        "\r": "\\r",
        '"': '\\"',
        "\\": "\\\\",
    }
    for character in value:
        escaped = short_escapes.get(character)
        if escaped is not None:
            result.append(escaped)
        elif ord(character) <= 0x1F:
            result.append(f"\\u{ord(character):04x}")
        else:
            result.append(character)
    result.append('"')
    return "".join(result)


def _format_float(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("NaN and Infinity are not valid JCS numbers")
    if value == 0.0:
        return "0"

    text = repr(value)
    sign = ""
    if text.startswith("-"):
        sign = "-"
        text = text[1:]

    exponent_text = ""
    exponent_value = 0
    if "e" in text:
        text, raw_exponent = text.split("e", 1)
        exponent_value = int(raw_exponent)
        exponent_text = f"e{'+' if exponent_value >= 0 else '-'}{abs(exponent_value)}"

    if "." in text:
        first, last = text.split(".", 1)
        dot = "."
    else:
        first, last, dot = text, "", ""

    if last == "0":
        last = ""
        dot = ""

    if 0 < exponent_value < 21:
        first += last
        last = ""
        dot = ""
        exponent_text = ""
        zeros = exponent_value - len(first)
        while zeros >= 0:
            first += "0"
            zeros -= 1
    elif -7 < exponent_value < 0:
        last = first + last
        first = "0"
        dot = "."
        exponent_text = ""
        zeros = exponent_value
        while zeros < -1:
            last = "0" + last
            zeros += 1

    return sign + first + dot + last + exponent_text


def _require_object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be an object")
    return value


def _require_exact_fields(
    value: dict[str, Any], expected: frozenset[str], location: str
) -> None:
    keys = set(value)
    if any(not isinstance(key, str) for key in keys):
        raise ValueError(f"{location} keys must be strings")
    missing = sorted(expected - keys)
    extra = sorted(keys - expected)
    if missing:
        raise ValueError(f"{location} is missing fields: {', '.join(missing)}")
    if extra:
        raise ValueError(f"{location} has unknown fields: {', '.join(extra)}")


def _require_integer(
    value: Any,
    location: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if type(value) is not int:
        raise ValueError(f"{location} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{location} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{location} must be at most {maximum}")
    return value


def _require_string(value: Any, location: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{location} must be a string")
    _validate_unicode(value)
    return value


def _require_nonempty_string(value: Any, location: str) -> str:
    result = _require_string(value, location)
    if not result:
        raise ValueError(f"{location} must not be empty")
    return result


def _require_number_array(value: Any, location: str, length: int) -> None:
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"{location} must be an array of {length} numbers")
    for index, number in enumerate(value):
        if type(number) not in (int, float):
            raise ValueError(f"{location}[{index}] must be a number")
        if isinstance(number, float) and not math.isfinite(number):
            raise ValueError(f"{location}[{index}] must be finite")
        if isinstance(number, int) and not -MAX_SAFE_INTEGER <= number <= MAX_SAFE_INTEGER:
            raise ValueError(f"{location}[{index}] is outside the interoperable range")


def _require_quaternion(value: Any, location: str) -> None:
    _require_number_array(value, location, 4)
    norm_squared = sum(float(component) * float(component) for component in value)
    if not 0.9998 <= norm_squared <= 1.0002:
        raise ValueError(f"{location} must be normalized")
