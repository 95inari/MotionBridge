#!/usr/bin/env python3
"""Run Motion Bridge checks with one reproducible, machine-readable entrypoint."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


REPOSITORY = Path(__file__).resolve().parents[1]
DEFAULT_BLENDER = Path("/Applications/Blender.app/Contents/MacOS/Blender")
SKIPPED_BLENDER_SCRIPTS = {"test_cross_runtime_e2e.py"}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Motion Bridge regression suites and print a JSON summary"
    )
    parser.add_argument(
        "--include-blender",
        action="store_true",
        help=(
            "Run every self-contained Blender test; the Undo gate briefly uses "
            "foreground Blender"
        ),
    )
    parser.add_argument(
        "--include-cross-runtime",
        action="store_true",
        help="Run the Unity/Blender transport process test",
    )
    parser.add_argument(
        "--blender-executable",
        type=Path,
        default=DEFAULT_BLENDER,
    )
    parser.add_argument("--json-output", type=Path)
    return parser.parse_args()


def _run(
    name: str,
    command: list[str],
    *,
    environment: dict[str, str],
) -> dict[str, object]:
    started = time.monotonic()
    print(f"\n[{name}] {' '.join(command)}", flush=True)
    completed = subprocess.run(
        command,
        cwd=REPOSITORY,
        env=environment,
        check=False,
    )
    return {
        "durationSeconds": round(time.monotonic() - started, 3),
        "exitCode": completed.returncode,
        "name": name,
        "status": "passed" if completed.returncode == 0 else "failed",
    }


def main() -> int:
    arguments = _arguments()
    environment = os.environ.copy()
    addon_root = str(REPOSITORY / "blender_addon")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        addon_root
        if not existing_pythonpath
        else os.pathsep.join((addon_root, existing_pythonpath))
    )

    commands: list[tuple[str, list[str]]] = [
        (
            "distribution-unit",
            [sys.executable, "-m", "unittest", "discover", "-s", "tools/tests", "-p", "test_*.py", "-v"],
        ),
        (
            "python-unit",
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "blender_addon/tests",
                "-p",
                "test_*.py",
                "-v",
            ],
        ),
        ("protocol-constants", [sys.executable, "protocol/tools/verify_constants.py"]),
        (
            "python-compile",
            [
                sys.executable,
                "-m",
                "compileall",
                "-q",
                "blender_addon/motion_bridge",
                "blender_addon/tests",
                "blender_addon/tools",
            ],
        ),
    ]
    ruff = shutil.which("ruff")
    if ruff is not None:
        commands.append(
            (
                "ruff",
                [
                    ruff,
                    "check",
                    "blender_addon/motion_bridge",
                    "blender_addon/tests",
                    "blender_addon/tools",
                    "tools",
                ],
            )
        )

    if arguments.include_blender:
        blender = arguments.blender_executable.expanduser().resolve()
        if not blender.is_file():
            raise SystemExit(f"Blender executable not found: {blender}")
        for script in sorted((REPOSITORY / "blender_addon/tools").glob("test_*.py")):
            if script.name in SKIPPED_BLENDER_SCRIPTS:
                continue
            blender_mode = [] if script.name == "test_phase0_undo.py" else ["--background"]
            commands.append(
                (
                    f"blender:{script.stem}",
                    [
                        str(blender),
                        *blender_mode,
                        "--factory-startup",
                        "--python-exit-code",
                        "1",
                        "--python",
                        str(script.relative_to(REPOSITORY)),
                    ],
                )
            )

    if arguments.include_cross_runtime:
        commands.append(
            (
                "cross-runtime",
                [
                    sys.executable,
                    "protocol/tools/run_cross_runtime_e2e.py",
                    "--timeout",
                    "90",
                ],
            )
        )

    started = time.monotonic()
    results = [
        _run(name, command, environment=environment) for name, command in commands
    ]
    failed = [result for result in results if result["status"] == "failed"]
    summary = {
        "durationSeconds": round(time.monotonic() - started, 3),
        "failed": len(failed),
        "passed": len(results) - len(failed),
        "pythonPath": addon_root,
        "results": results,
        "schemaVersion": 1,
        "status": "PASS" if not failed else "FAIL",
    }
    encoded = json.dumps(summary, ensure_ascii=False, sort_keys=True)
    print("\nMOTION_BRIDGE_TEST_SUMMARY=" + encoded)
    if arguments.json_output is not None:
        arguments.json_output.parent.mkdir(parents=True, exist_ok=True)
        arguments.json_output.write_text(encoded + "\n", encoding="utf-8")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
