"""Blender Extension entry point for Motion Bridge."""


def register() -> None:
    from .motion_bridge import register as register_addon

    register_addon()


def unregister() -> None:
    from .motion_bridge import unregister as unregister_addon

    unregister_addon()
