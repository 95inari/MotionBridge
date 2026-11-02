from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time


REPOSITORY = Path(__file__).resolve().parents[2]
DEFAULT_UNITY = Path(
    "/Applications/Unity/Hub/Editor/6000.2.15f1/Unity.app/Contents/MacOS/Unity"
)
DEFAULT_BLENDER = Path("/Applications/Blender.app/Contents/MacOS/Blender")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the real Python/Blender client against the C#/Unity server"
    )
    parser.add_argument("--unity", type=Path, default=DEFAULT_UNITY)
    parser.add_argument("--blender", type=Path, default=DEFAULT_BLENDER)
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser.parse_args()


def _wait_for_file(path: Path, process: subprocess.Popen, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size:
            return
        if process.poll() is not None:
            raise RuntimeError(f"Unity exited before publishing its port (code {process.returncode})")
        time.sleep(0.05)
    raise TimeoutError("Timed out waiting for the Unity control port")


def main() -> int:
    arguments = _arguments()
    for executable in (arguments.unity, arguments.blender):
        if not executable.is_file():
            raise FileNotFoundError(executable)

    with tempfile.TemporaryDirectory(prefix="motion-bridge-e2e-") as temporary:
        root = Path(temporary)
        port_file = root / "ports.json"
        unity_report = root / "unity-report.json"
        blender_report = root / "blender-report.json"
        unity_log = root / "unity.log"
        blender_log = root / "blender.log"
        unity_command = [
            str(arguments.unity),
            "-batchmode",
            "-nographics",
            "-quit",
            "-projectPath",
            str(REPOSITORY / "verification" / "unity-6000.2"),
            "-executeMethod",
            "Kuko.MotionBridge.Verification.MotionBridgeTransportE2E.Run",
            "--motion-bridge-port-file",
            str(port_file),
            "--motion-bridge-report-file",
            str(unity_report),
            "-logFile",
            str(unity_log),
        ]
        with unity_log.open("ab") as unity_output:
            unity = subprocess.Popen(
                unity_command,
                cwd=REPOSITORY,
                stdout=unity_output,
                stderr=subprocess.STDOUT,
            )
        try:
            _wait_for_file(port_file, unity, arguments.timeout)
            blender_command = [
                str(arguments.blender),
                "--background",
                "--factory-startup",
                "--python-exit-code",
                "1",
                "--python",
                str(REPOSITORY / "blender_addon" / "tools" / "test_cross_runtime_e2e.py"),
                "--",
                "--port-file",
                str(port_file),
                "--report",
                str(blender_report),
            ]
            with blender_log.open("wb") as blender_output:
                blender = subprocess.run(
                    blender_command,
                    cwd=REPOSITORY,
                    stdout=blender_output,
                    stderr=subprocess.STDOUT,
                    timeout=arguments.timeout,
                    check=False,
                )
            if blender.returncode:
                raise RuntimeError(
                    "Blender E2E client failed:\n"
                    + blender_log.read_text(encoding="utf-8", errors="replace")[-8000:]
                )
            unity_code = unity.wait(timeout=arguments.timeout)
            if unity_code:
                raise RuntimeError(
                    "Unity E2E server failed:\n"
                    + unity_log.read_text(encoding="utf-8", errors="replace")[-8000:]
                )
            reports = {
                "blender": json.loads(blender_report.read_text(encoding="utf-8")),
                "unity": json.loads(unity_report.read_text(encoding="utf-8")),
            }
            print("MOTION_BRIDGE_CROSS_RUNTIME_E2E=" + json.dumps(reports, sort_keys=True))
            return 0
        finally:
            if unity.poll() is None:
                unity.terminate()
                try:
                    unity.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    unity.kill()


if __name__ == "__main__":
    raise SystemExit(main())
