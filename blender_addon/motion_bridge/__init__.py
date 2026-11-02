"""Motion Bridge Blender package.

Imports are intentionally lazy so protocol unit tests can run with the system
Python without importing Blender's ``bpy`` module.
"""


def register() -> None:
    from .addon import register as register_addon

    register_addon()


def unregister() -> None:
    from .addon import unregister as unregister_addon

    unregister_addon()
