"""Evaluated-pose retargeting into the Motion Bridge Canonical rig."""

from importlib import import_module

from .presets import infer_humanoid_mapping, infer_humanoid_mapping_from_names

__all__ = (
    "BaseRegenerationResult",
    "PROP_RETARGET_RECIPE",
    "RETARGET_BAKED_ROLE",
    "RETARGET_RECIPE_SCHEMA_VERSION",
    "RetargetRecipe",
    "RetargetRecipeError",
    "ValidatedHumanoidMapping",
    "bake_archived_source_to_canonical",
    "bake_source_to_canonical",
    "infer_humanoid_mapping",
    "infer_humanoid_mapping_from_names",
    "regenerate_canonical_base",
    "validate_humanoid_mapping",
)


def __getattr__(name: str) -> object:
    """Keep pure name-preset helpers importable without Blender's ``bpy``."""

    if name in {"RETARGET_BAKED_ROLE", "bake_source_to_canonical"}:
        module = import_module(".bake", __name__)
    elif name in {
        "BaseRegenerationResult",
        "bake_archived_source_to_canonical",
        "regenerate_canonical_base",
    }:
        module = import_module(".regenerate", __name__)
    elif name in {
        "PROP_RETARGET_RECIPE",
        "RETARGET_RECIPE_SCHEMA_VERSION",
        "RetargetRecipe",
        "RetargetRecipeError",
    }:
        module = import_module(".recipe", __name__)
    elif name in {"ValidatedHumanoidMapping", "validate_humanoid_mapping"}:
        module = import_module(".mapping", __name__)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
