from __future__ import annotations

from collections.abc import Iterable


_MIXAMO_ALIASES = {
    "Hips": "Hips",
    "Spine": "Spine",
    "Chest": "Spine1",
    "UpperChest": "Spine2",
    "Neck": "Neck",
    "Head": "Head",
    "LeftShoulder": "LeftShoulder",
    "LeftUpperArm": "LeftArm",
    "LeftLowerArm": "LeftForeArm",
    "LeftHand": "LeftHand",
    "RightShoulder": "RightShoulder",
    "RightUpperArm": "RightArm",
    "RightLowerArm": "RightForeArm",
    "RightHand": "RightHand",
    "LeftUpperLeg": "LeftUpLeg",
    "LeftLowerLeg": "LeftLeg",
    "LeftFoot": "LeftFoot",
    "LeftToes": "LeftToeBase",
    "RightUpperLeg": "RightUpLeg",
    "RightLowerLeg": "RightLeg",
    "RightFoot": "RightFoot",
    "RightToes": "RightToeBase",
}

_GENERIC_ALIASES = {
    "LeftUpperLeg": ("LeftThigh",),
    "LeftLowerLeg": ("LeftShin",),
    "LeftToes": ("LeftToe",),
    "RightUpperLeg": ("RightThigh",),
    "RightLowerLeg": ("RightShin",),
    "RightToes": ("RightToe",),
}


def infer_humanoid_mapping_from_names(
    bone_names: Iterable[str],
    preset: str = "AUTO",
) -> dict[str, str]:
    """Infer a conservative HumanBone mapping from Unity or Mixamo names.

    Namespace prefixes (``Armature|`` / ``mixamorig:``) and punctuation are
    ignored, but fuzzy substring matching is intentionally not used.  An
    ambiguous normalized name is a hard error rather than a silent limb swap.
    """

    mode = str(preset).upper()
    if mode not in {"AUTO", "EXACT", "MIXAMO"}:
        raise ValueError("preset must be AUTO, EXACT, or MIXAMO")

    names_by_key: dict[str, list[str]] = {}
    for name in bone_names:
        if not isinstance(name, str) or not name:
            raise ValueError("Source bone names must be non-empty strings")
        names_by_key.setdefault(_normalized_name(name), []).append(name)

    mapping: dict[str, str] = {}
    for human_bone, mixamo_name in _MIXAMO_ALIASES.items():
        candidates: tuple[str, ...]
        if mode == "EXACT":
            candidates = (human_bone,)
        elif mode == "MIXAMO":
            candidates = (mixamo_name,)
        else:
            candidates = tuple(dict.fromkeys((human_bone, mixamo_name)))

        matches: list[str] = []
        for candidate in candidates:
            matches.extend(names_by_key.get(_normalized_name(candidate), ()))
        matches = list(dict.fromkeys(matches))
        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous source bones for {human_bone}: {', '.join(matches)}"
            )
        if matches:
            mapping[human_bone] = matches[0]

    if mode == "AUTO":
        for human_bone, aliases in _GENERIC_ALIASES.items():
            if human_bone in mapping:
                continue
            matches = _matches_for_candidates(names_by_key, aliases)
            if len(matches) > 1:
                raise ValueError(
                    f"Ambiguous source bones for {human_bone}: {', '.join(matches)}"
                )
            if matches:
                mapping[human_bone] = matches[0]

        _apply_shifted_spine_family(names_by_key, mapping)
    return mapping


def _matches_for_candidates(
    names_by_key: dict[str, list[str]],
    candidates: Iterable[str],
) -> list[str]:
    matches: list[str] = []
    for candidate in candidates:
        matches.extend(names_by_key.get(_normalized_name(candidate), ()))
    return list(dict.fromkeys(matches))


def _apply_shifted_spine_family(
    names_by_key: dict[str, list[str]],
    mapping: dict[str, str],
) -> None:
    """Recognize rigs whose torso starts at Spine1 instead of Spine.

    This is only safe when canonical torso names are absent.  It keeps Mixamo
    rigs on Spine/Spine1/Spine2 while supporting Spine1/Spine2/Spine3 rigs.
    """

    if any(
        names_by_key.get(_normalized_name(name))
        for name in ("Spine", "Chest", "UpperChest")
    ):
        return
    if not names_by_key.get(_normalized_name("Spine1")):
        return

    shifted = (
        ("Spine", "Spine1"),
        ("Chest", "Spine2"),
        ("UpperChest", "Spine3"),
    )
    resolved: dict[str, str] = {}
    for human_bone, source_name in shifted:
        matches = _matches_for_candidates(names_by_key, (source_name,))
        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous source bones for {human_bone}: {', '.join(matches)}"
            )
        if matches:
            resolved[human_bone] = matches[0]

    mapping.pop("Chest", None)
    mapping.pop("UpperChest", None)
    mapping.update(resolved)


def infer_humanoid_mapping(source_armature: object, preset: str = "AUTO") -> dict[str, str]:
    if source_armature is None or getattr(source_armature, "type", None) != "ARMATURE":
        raise ValueError("Source must be an armature object")
    return infer_humanoid_mapping_from_names(
        (bone.name for bone in source_armature.data.bones),
        preset,
    )


def _normalized_name(name: str) -> str:
    base = name.replace("|", ":").rsplit(":", 1)[-1]
    normalized = "".join(character.lower() for character in base if character.isalnum())
    if normalized.startswith("mixamorig"):
        normalized = normalized[len("mixamorig") :]
    return normalized
