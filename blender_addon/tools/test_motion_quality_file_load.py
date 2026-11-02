from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

import bpy


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))
TOOLS_ROOT = Path(__file__).resolve().parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from motion_bridge import addon  # noqa: E402
from test_motion_sample import (  # noqa: E402
    _clear_scene,
    _create_canonical_armature,
    _key_motion,
)
from test_motion_quality_ui import _begin_modal_harness  # noqa: E402


def _store_fixture_report(
    scene: bpy.types.Scene,
    armature: bpy.types.Object,
) -> None:
    settings = scene.motion_bridge_session
    settings.motion_quality_summary = "FIX_RECOMMENDED"
    settings.motion_quality_report_json = json.dumps(
        {"schema": "motion-bridge.test-motion-quality-file-load/1"},
        separators=(",", ":"),
    )
    settings.motion_quality_issue_preview_json = "[]"
    settings.motion_quality_issue_count = 7
    settings.motion_quality_target = armature
    settings.motion_quality_stack_id = "fixture-stack"
    settings.motion_quality_evaluation_fingerprint = "fixture-fingerprint"
    settings.motion_quality_range_source = "whole_take"
    settings.motion_quality_frame_start = 1
    settings.motion_quality_frame_end = 300
    settings.motion_quality_sample_count = 300
    settings.motion_quality_fps = 30
    settings.motion_quality_fps_base = 1.0
    settings.motion_quality_motion_revision = 42


def _assert_report_cleared(scene: bpy.types.Scene) -> None:
    settings = scene.motion_bridge_session
    if settings.motion_quality_summary != "NOT_RUN":
        raise AssertionError(f"Loaded Scene retained summary: {scene.name}")
    if settings.motion_quality_report_json:
        raise AssertionError(f"Loaded Scene retained report JSON: {scene.name}")
    if settings.motion_quality_issue_preview_json:
        raise AssertionError(f"Loaded Scene retained issue preview: {scene.name}")
    if int(settings.motion_quality_issue_count) != 0:
        raise AssertionError(f"Loaded Scene retained issue count: {scene.name}")
    if settings.motion_quality_target is not None:
        raise AssertionError(f"Loaded Scene retained report target: {scene.name}")
    if settings.motion_quality_stack_id:
        raise AssertionError(f"Loaded Scene retained report stack ID: {scene.name}")
    if settings.motion_quality_evaluation_fingerprint:
        raise AssertionError(
            f"Loaded Scene retained evaluation fingerprint: {scene.name}"
        )
    if settings.motion_quality_range_source:
        raise AssertionError(f"Loaded Scene retained range source: {scene.name}")
    if (
        int(settings.motion_quality_frame_start) != 0
        or int(settings.motion_quality_frame_end) != -1
        or int(settings.motion_quality_sample_count) != 0
        or int(settings.motion_quality_fps) != 0
        or float(settings.motion_quality_fps_base) != 0.0
        or int(settings.motion_quality_motion_revision) != -1
    ):
        raise AssertionError(f"Loaded Scene retained report metadata: {scene.name}")


def _begin_active_sampling_scan(
    armature: bpy.types.Object,
) -> object:
    operator = _begin_modal_harness(armature)
    # Keep this file-load regression valid while the UI harness evolves: the
    # production operator owns a subprocess job rather than a Python Future.
    operator._analysis_job = None
    sampler = operator._sampler
    sampler.sample_frame()
    sampler.restore_frame_for_yield()
    if sampler.is_complete or int(sampler.sampled_count) != 1:
        raise AssertionError("Motion Quality fixture did not yield during sampling")
    state = addon._motion_quality_scan_state
    if state is None:
        raise AssertionError("Motion Quality fixture lost its display state")
    state.sampled_count = int(sampler.sampled_count)
    return operator


def _begin_active_analysis_scan(
    armature: bpy.types.Object,
) -> tuple[object, object]:
    operator = _begin_modal_harness(armature)
    timer_event = SimpleNamespace(type="TIMER", timer=operator._timer)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        result = addon.MB_OT_ScanMotionQuality.modal(
            operator,
            bpy.context,
            timer_event,
        )
        job = operator._analysis_job
        if job is not None and job.phase == "analyzing":
            if job._process is None or job._job_dir is None:
                raise AssertionError("Analysis fixture did not own process resources")
            return operator, job
        if result in ({"FINISHED"}, {"CANCELLED"}):
            raise AssertionError("Analysis fixture terminated before process startup")
        time.sleep(0.001)
    raise AssertionError("Analysis fixture did not start its isolated process")


def _assert_motion_quality_handlers_registered() -> None:
    expected = (
        (bpy.app.handlers.load_pre, addon._handle_motion_quality_load_pre),
        (bpy.app.handlers.load_post, addon._handle_live_file_load),
        (
            bpy.app.handlers.frame_change_pre,
            addon._handle_motion_quality_frame_change_pre,
        ),
        (
            bpy.app.handlers.frame_change_post,
            addon._handle_motion_quality_frame_change_post,
        ),
        (
            bpy.app.handlers.depsgraph_update_post,
            addon._handle_motion_quality_depsgraph_update,
        ),
    )
    for handlers, callback in expected:
        if callback not in handlers:
            raise AssertionError(
                f"File load removed registered handler: {callback.__name__}"
            )


def main() -> None:
    _clear_scene()
    addon.register()
    try:
        armature = _create_canonical_armature()
        _key_motion(armature)
        scene = bpy.context.scene
        scene.frame_start = 1
        scene.frame_end = 300
        scene.render.fps = 30
        scene.render.fps_base = 1.0

        bpy.data.scenes.new("Motion Quality Load Secondary")
        for fixture_scene in bpy.data.scenes:
            _store_fixture_report(fixture_scene, armature)

        sampling_operator = None
        analysis_operator = None
        analysis_job = None
        analysis_job_dir = None
        analysis_process = None
        with tempfile.TemporaryDirectory(
            prefix="motion_bridge_motion_quality_load_"
        ) as directory:
            blend_path = str(Path(directory) / "motion_quality_load.blend")
            if (
                bpy.ops.wm.save_as_mainfile(
                    filepath=blend_path,
                    check_existing=False,
                )
                != {"FINISHED"}
            ):
                raise AssertionError("Motion Quality load fixture could not be saved")
            if not all(
                fixture_scene.motion_bridge_session.motion_quality_report_json
                for fixture_scene in bpy.data.scenes
            ):
                raise AssertionError("Fixture report was cleared before it was saved")

            sampling_operator = _begin_active_sampling_scan(armature)
            if (
                addon._motion_quality_scan_state is None
                or addon._motion_quality_scan_operator is not sampling_operator
            ):
                raise AssertionError("Motion Quality sampling scan did not become active")

            if (
                bpy.ops.wm.open_mainfile(filepath=blend_path, load_ui=False)
                != {"FINISHED"}
            ):
                raise AssertionError("Motion Quality load fixture could not be reopened")

            if addon._motion_quality_scan_state is not None:
                raise AssertionError("Sampling load retained Motion Quality display state")
            loaded_armature = bpy.context.active_object
            if loaded_armature is None or loaded_armature.type != "ARMATURE":
                raise AssertionError("Reloaded fixture lost its active Armature")
            analysis_operator, analysis_job = _begin_active_analysis_scan(
                loaded_armature
            )
            analysis_job_dir = analysis_job._job_dir
            analysis_process = analysis_job._process
            if (
                bpy.ops.wm.open_mainfile(filepath=blend_path, load_ui=False)
                != {"FINISHED"}
            ):
                raise AssertionError(
                    "Motion Quality analysis-phase fixture could not be reopened"
                )

        if addon._motion_quality_scan_state is not None:
            raise AssertionError("load_post retained Motion Quality display state")
        if addon._motion_quality_scan_operator is not None:
            raise AssertionError("load_post retained Motion Quality operator ownership")
        for operator in (sampling_operator, analysis_operator):
            if operator is None or not bool(getattr(operator, "_aborted", False)):
                raise AssertionError(
                    "load_post did not mark the detached operator as aborted"
                )
        if analysis_job is None or analysis_job.phase != "cancelled":
            raise AssertionError("Analysis process was not cancelled during file load")
        if analysis_process is None or analysis_process.poll() is None:
            raise AssertionError("Analysis process was not reaped during file load")
        if analysis_job_dir is not None and analysis_job_dir.exists():
            raise AssertionError("Analysis temporary directory survived file load")
        if addon._motion_quality_pending_cleanup_jobs:
            raise AssertionError("File load retained a completed cleanup job")
        for loaded_scene in bpy.data.scenes:
            _assert_report_cleared(loaded_scene)
        _assert_motion_quality_handlers_registered()

        print(
            "MOTION_BRIDGE_MOTION_QUALITY_FILE_LOAD="
            + json.dumps(
                {
                    "activeSamplingCancelled": True,
                    "activeAnalysisCancelled": True,
                    "analysisProcessReaped": True,
                    "analysisTemporaryFilesRemoved": True,
                    "handlersPersistent": True,
                    "loadedSceneCount": len(bpy.data.scenes),
                    "reportsCleared": True,
                },
                sort_keys=True,
            )
        )
    finally:
        addon.unregister()


if __name__ == "__main__":
    main()
