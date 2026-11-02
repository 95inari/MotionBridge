from __future__ import annotations

import json
import os
from pathlib import Path
import sys
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
from motion_bridge.editing.correction_layers import (  # noqa: E402
    create_correction_layer,
)
from test_motion_sample import (  # noqa: E402
    _clear_scene,
    _create_canonical_armature,
    _key_motion,
)


class _LayoutRecorder:
    def __init__(
        self,
        operators: list[tuple[str, SimpleNamespace]] | None = None,
        labels: list[str] | None = None,
    ) -> None:
        self.operators = operators if operators is not None else []
        self.labels = labels if labels is not None else []
        self.alert = False
        self.enabled = True
        self.scale_y = 1.0

    def box(self) -> _LayoutRecorder:
        return _LayoutRecorder(self.operators, self.labels)

    def row(self, **_kwargs: object) -> _LayoutRecorder:
        return _LayoutRecorder(self.operators, self.labels)

    def label(self, **kwargs: object) -> None:
        self.labels.append(str(kwargs.get("text", "")))

    def operator(self, operator_id: str, **_kwargs: object) -> SimpleNamespace:
        properties = SimpleNamespace()
        self.operators.append((operator_id, properties))
        return properties


class _MotionQualityModalHarness:
    """Drive the operator's modal core without requiring a foreground window."""

    _abort = addon.MB_OT_ScanMotionQuality._abort
    _abort_or_defer = addon.MB_OT_ScanMotionQuality._abort_or_defer
    _cleanup = addon.MB_OT_ScanMotionQuality._cleanup
    _prepare_external_cancel = addon.MB_OT_ScanMotionQuality._prepare_external_cancel
    _restore_sampler_and_detach_analysis = (
        addon.MB_OT_ScanMotionQuality._restore_sampler_and_detach_analysis
    )
    _report_result = addon.MB_OT_ScanMotionQuality._report_result

    def __init__(
        self,
        armature: bpy.types.Object,
        range_spec: object | None = None,
    ) -> None:
        scene = bpy.context.scene
        range_spec = (
            addon._motion_quality_range_spec(bpy.context, armature)
            if range_spec is None
            else range_spec
        )
        frame_start, frame_end = range_spec.expanded_integer_bounds()
        self._timer = None
        self._sampler = addon.MotionRangeSampler(
            bpy.context,
            armature,
            range_spec=range_spec,
            include_key_density=False,
        ).begin()
        self._analysis_job = None
        self._armature = armature
        self._frame_start = frame_start
        self._frame_end = frame_end
        self._fps = int(scene.render.fps)
        self._fps_base = float(scene.render.fps_base)
        self._stack_id = str(armature.get("motion_bridge_stack_id", ""))
        self._evaluation_fingerprint = (
            addon._motion_quality_evaluation_fingerprint(armature)
        )
        self._range_spec = range_spec
        self._motion_revision = int(
            armature.get(addon.MOTION_REVISION_PROPERTY, 0)
        )
        self._validation_settings = addon._motion_quality_settings(bpy.context)
        self._aborted = False
        self.messages: list[tuple[set[str], str]] = []

    def report(self, levels: set[str], message: str) -> None:
        self.messages.append((set(levels), str(message)))


def _begin_modal_harness(
    armature: bpy.types.Object,
    range_spec: object | None = None,
) -> _MotionQualityModalHarness:
    if addon._motion_quality_scan_state is not None:
        raise AssertionError("A previous Motion Quality modal scan is still active")
    harness = _MotionQualityModalHarness(armature, range_spec)
    addon._motion_quality_scan_state = addon._MotionQualityScanDisplayState(
        armature_pointer=int(armature.as_pointer()),
        dependency_pointers=frozenset(
            addon._motion_quality_dependency_pointers(armature)
        ),
        sampled_count=0,
        sample_count=int(harness._sampler.sample_count),
    )
    addon._motion_quality_scan_operator = harness
    return harness


def _drive_modal(
    harness: _MotionQualityModalHarness,
    *,
    timeout_seconds: float = 5.0,
) -> set[str]:
    deadline = time.monotonic() + timeout_seconds
    event = SimpleNamespace(type="TIMER", timer=harness._timer)
    while time.monotonic() < deadline:
        result = addon.MB_OT_ScanMotionQuality.modal(
            harness,
            bpy.context,
            event,
        )
        if result in ({"FINISHED"}, {"CANCELLED"}):
            return result
        time.sleep(0.001)
    raise AssertionError("Motion Quality modal scan did not terminate")


def _scene_state() -> tuple[object, ...]:
    scene = bpy.context.scene
    active = bpy.context.view_layer.objects.active
    return (
        int(scene.frame_current),
        float(scene.frame_subframe),
        bool(scene.use_preview_range),
        int(scene.frame_preview_start),
        int(scene.frame_preview_end),
        tuple(sorted(obj.name for obj in bpy.context.selected_objects)),
        None if active is None else active.name,
        None if active is None else str(active.mode),
    )


def _scan_and_check_result(armature: bpy.types.Object) -> dict[str, object]:
    scene = bpy.context.scene
    settings = scene.motion_bridge_session
    scene.frame_set(2, subframe=0.25)
    scene.use_preview_range = True
    scene.frame_preview_start = 1
    scene.frame_preview_end = 2
    before = _scene_state()

    if bpy.ops.motion_bridge.scan_motion_quality("EXEC_DEFAULT") != {"FINISHED"}:
        raise AssertionError("Synchronous Motion Quality operator failed")
    if _scene_state() != before:
        raise AssertionError("Motion Quality scan did not restore Blender UI state")
    if settings.motion_quality_target is not armature:
        raise AssertionError("Motion Quality result lost its target Armature")
    if settings.motion_quality_summary != "FIX_RECOMMENDED":
        raise AssertionError(
            "Synthetic root jump was not presented as Fix Recommended: "
            f"{settings.motion_quality_summary}"
        )

    payload = json.loads(settings.motion_quality_report_json)
    if payload.get("schema") != "motion-bridge.motion-quality-report/1":
        raise AssertionError("Motion Quality operator did not store canonical JSON")
    if payload.get("sampleCount") != 2:
        raise AssertionError("Motion Quality report sampled the wrong Scene range")
    if settings.motion_quality_range_source != "preview_range":
        raise AssertionError("Motion Quality result lost its Preview Range source")
    if payload.get("summary") != "fix_recommended":
        raise AssertionError("Motion Quality JSON summary disagrees with the UI")
    if int(settings.motion_quality_issue_count) != len(payload.get("issues", ())):
        raise AssertionError("Motion Quality result lost its total issue count")
    if not any(
        issue.get("code") == "root_translation_discontinuity"
        for issue in payload.get("issues", ())
    ):
        raise AssertionError("Synthetic root discontinuity was not reported")
    if any(
        issue.get("code") == "key_density_high"
        for issue in payload.get("issues", ())
    ):
        raise AssertionError("Expected dense Canonical Base was shown as a visual defect")

    return payload


def _check_modal_lifecycle(armature: bpy.types.Object) -> None:
    scene = bpy.context.scene
    settings = scene.motion_bridge_session
    addon._clear_motion_quality_result(settings)
    scene.frame_set(2, subframe=0.25)
    scene.use_preview_range = True
    scene.frame_preview_start = 1
    scene.frame_preview_end = 2
    before = _scene_state()

    harness = _begin_modal_harness(armature)
    if _drive_modal(harness) != {"FINISHED"}:
        raise AssertionError(f"Modal Motion Quality scan failed: {harness.messages!r}")
    if _scene_state() != before:
        raise AssertionError("Completed modal scan did not restore Blender UI state")
    if not settings.motion_quality_report_json:
        raise AssertionError("Completed modal scan did not store its report")
    if addon._motion_quality_scan_state is not None:
        raise AssertionError("Completed modal scan retained its display state")

    addon._clear_motion_quality_result(settings)
    scene.frame_set(3, subframe=0.4)
    scene.use_preview_range = False
    before = _scene_state()
    harness = _begin_modal_harness(armature)
    harness._sampler.sample_frame()
    harness._sampler.restore_frame_for_yield()
    state = addon._motion_quality_scan_state
    if state is None:
        raise AssertionError("Modal scan lost its cancellation state")
    state.cancel_requested = True
    result = addon.MB_OT_ScanMotionQuality.modal(
        harness,
        bpy.context,
        SimpleNamespace(type="MOUSEMOVE"),
    )
    if result != {"CANCELLED"}:
        raise AssertionError("Modal cancellation did not terminate the operator")
    if _scene_state() != before:
        raise AssertionError("Cancelled modal scan did not restore Blender UI state")
    if settings.motion_quality_report_json:
        raise AssertionError("Cancelled modal scan stored a partial report")

    scene.frame_set(3, subframe=0.4)
    harness = _begin_modal_harness(armature)
    harness._sampler.sample_frame()
    harness._sampler.restore_frame_for_yield()
    scene.frame_set(1, subframe=0.1)
    scene.use_preview_range = True
    scene.frame_preview_start = 1
    scene.frame_preview_end = 3
    user_state = _scene_state()
    addon._cancel_motion_quality_runtime()
    if _scene_state() != user_state:
        raise AssertionError("External cancellation overwrote later user input")
    state = addon._motion_quality_scan_state
    if state is None or not state.cancel_requested:
        raise AssertionError("External cancellation detached the modal too early")
    result = addon.MB_OT_ScanMotionQuality.modal(
        harness,
        bpy.context,
        SimpleNamespace(type="MOUSEMOVE"),
    )
    if result != {"CANCELLED"} or addon._motion_quality_scan_state is not None:
        raise AssertionError("Externally cancelled modal did not exit cleanly")


def _check_cancel_cleanup_retry(armature: bpy.types.Object) -> None:
    class FlakyCancelJob:
        def __init__(self) -> None:
            self.cancel_calls = 0

        def cancel(self) -> None:
            self.cancel_calls += 1
            if self.cancel_calls == 1:
                raise addon.MotionQualityProcessError(
                    "injected worker cleanup failure",
                    stderr="/private/internal/path",
                )

    harness = _begin_modal_harness(armature)
    job = FlakyCancelJob()
    harness._analysis_job = job
    try:
        first = addon.MB_OT_ScanMotionQuality.modal(
            harness,
            bpy.context,
            SimpleNamespace(type="ESC"),
        )
        if first != {"PASS_THROUGH"}:
            raise AssertionError("A transient cleanup failure leaked from modal cancel")
        if harness._analysis_job is not job:
            raise AssertionError("Failed cleanup discarded the process handle")
        state = addon._motion_quality_scan_state
        if state is None or not state.cancel_requested:
            raise AssertionError("Failed cleanup was not scheduled for retry")

        second = addon.MB_OT_ScanMotionQuality.modal(
            harness,
            bpy.context,
            SimpleNamespace(type="MOUSEMOVE"),
        )
        if second != {"CANCELLED"} or job.cancel_calls != 2:
            raise AssertionError("Modal cancel did not retry process cleanup")
        if addon._motion_quality_scan_state is not None:
            raise AssertionError("Successful cleanup retry retained modal ownership")
    finally:
        if addon._motion_quality_scan_state is not None:
            addon._cancel_motion_quality_runtime(force_cleanup=True)


def _check_load_detach_cleanup_retry() -> None:
    class FlakyDetachedJob:
        def __init__(self) -> None:
            self.cancel_calls = 0
            self.can_finish = False

        def cancel(self) -> None:
            self.cancel_calls += 1
            if not self.can_finish:
                raise addon.MotionQualityProcessError(
                    "injected detached cleanup failure"
                )

    if addon._motion_quality_pending_cleanup_jobs:
        raise AssertionError("A previous detached cleanup job is still pending")
    job = FlakyDetachedJob()
    operator = SimpleNamespace(
        _aborted=False,
        _analysis_job=job,
        _sampler=object(),
    )
    addon._motion_quality_scan_operator = operator
    addon._motion_quality_scan_state = object()
    try:
        addon._detach_motion_quality_runtime_after_load()
        if (
            addon._motion_quality_scan_operator is not None
            or addon._motion_quality_scan_state is not None
        ):
            raise AssertionError("Load detach retained Blender UI ownership")
        if operator._analysis_job is not None or not operator._aborted:
            raise AssertionError("Load detach retained the invalidated operator state")
        if addon._motion_quality_pending_cleanup_jobs != [job]:
            raise AssertionError("Failed load cleanup lost its retryable job")

        addon._queue_motion_quality_cleanup(job)
        if addon._motion_quality_pending_cleanup_jobs != [job]:
            raise AssertionError("A detached cleanup job was queued twice")
        if bpy.app.timers.is_registered(addon._motion_quality_cleanup_timer):
            bpy.app.timers.unregister(addon._motion_quality_cleanup_timer)
        retry_delay = addon._motion_quality_cleanup_timer()
        if retry_delay != addon._MOTION_QUALITY_CLEANUP_RETRY_SECONDS:
            raise AssertionError("Failed detached cleanup was not retained for retry")

        job.can_finish = True
        if addon._motion_quality_cleanup_timer() is not None:
            raise AssertionError("Successful detached cleanup kept its retry timer")
        if addon._motion_quality_pending_cleanup_jobs or job.cancel_calls != 3:
            raise AssertionError("Detached cleanup retry did not release its job")
    finally:
        job.can_finish = True
        addon._retry_motion_quality_cleanup_jobs()
        addon._motion_quality_scan_operator = None
        addon._motion_quality_scan_state = None
        if bpy.app.timers.is_registered(addon._motion_quality_cleanup_timer):
            bpy.app.timers.unregister(addon._motion_quality_cleanup_timer)


def _check_chunk_yield_restores_visible_frame(armature: bpy.types.Object) -> None:
    scene = bpy.context.scene
    original_end = int(scene.frame_end)
    scene.frame_end = 300
    scene.frame_set(2, subframe=0.25)
    before = _scene_state()
    harness = _begin_modal_harness(
        armature,
        addon.RangeSpec(1.0, 300.0, addon.RangeSource.PREVIEW_RANGE),
    )
    try:
        result = addon.MB_OT_ScanMotionQuality.modal(
            harness,
            bpy.context,
            SimpleNamespace(type="TIMER", timer=harness._timer),
        )
        if result != {"PASS_THROUGH"} or harness._analysis_job is not None:
            raise AssertionError("Chunk fixture did not yield during sampling")
        if _scene_state() != before:
            raise AssertionError("A modal sampling chunk exposed a temporary frame")
        bpy.context.view_layer.update()
        state = addon._motion_quality_scan_state
        if state is None or state.input_changed:
            raise AssertionError("Internal sampling was mistaken for a user edit")
    finally:
        if addon._motion_quality_scan_state is not None:
            state = addon._motion_quality_scan_state
            state.cancel_requested = True
            addon.MB_OT_ScanMotionQuality.modal(
                harness,
                bpy.context,
                SimpleNamespace(type="MOUSEMOVE"),
            )
        scene.frame_end = original_end


def _check_changed_settings_discard_running_report(
    armature: bpy.types.Object,
) -> None:
    scene = bpy.context.scene
    settings = scene.motion_bridge_session
    addon._clear_motion_quality_result(settings)
    scene.frame_set(2, subframe=0.25)
    before = _scene_state()
    original_max_height = float(settings.contact_max_height)
    harness: _MotionQualityModalHarness | None = None
    try:
        harness = _begin_modal_harness(armature)
        timer_event = SimpleNamespace(type="TIMER", timer=harness._timer)
        deadline = time.monotonic() + 5.0
        while harness._analysis_job is None and time.monotonic() < deadline:
            result = addon.MB_OT_ScanMotionQuality.modal(
                harness,
                bpy.context,
                timer_event,
            )
            if result in ({"FINISHED"}, {"CANCELLED"}):
                raise AssertionError("Modal scan terminated before analysis started")
        if harness._analysis_job is None:
            raise AssertionError("Modal scan did not prepare its isolated analysis")

        settings.contact_max_height = original_max_height + 0.01
        state = addon._motion_quality_scan_state
        if state is None or not state.input_changed:
            raise AssertionError("Changed Motion Quality settings were not noticed")
        result = _drive_modal(harness)
        if result not in ({"FINISHED"}, {"CANCELLED"}):
            raise AssertionError("Changed-input modal scan did not terminate")
        if settings.motion_quality_report_json:
            raise AssertionError("A report made with old settings was stored as current")
        if settings.motion_quality_summary != "NOT_RUN":
            raise AssertionError("A changed-input scan retained a stale summary")
        if _scene_state() != before:
            raise AssertionError("Changed-input modal scan did not restore Blender UI state")
    finally:
        if addon._motion_quality_scan_state is not None:
            addon._cancel_motion_quality_runtime()
        settings.contact_max_height = original_max_height


def _check_pre_handlers_restore_and_cancel(armature: bpy.types.Object) -> None:
    settings = bpy.context.scene.motion_bridge_session
    for name, handler in (
        ("save_pre", addon._handle_envelope_save_pre),
        ("undo_pre", addon._handle_envelope_history_pre),
        ("load_pre", addon._handle_motion_quality_load_pre),
    ):
        addon._clear_motion_quality_result(settings)
        bpy.context.scene.frame_set(3, subframe=0.4)
        before = _scene_state()
        harness = _begin_modal_harness(armature)
        harness._sampler.sample_frame()
        harness._sampler.restore_frame_for_yield()
        handler()
        if _scene_state() != before:
            raise AssertionError(f"{name} did not restore the sampler state")
        state = addon._motion_quality_scan_state
        if state is None or not state.cancel_requested:
            raise AssertionError(f"{name} detached its modal handler too early")
        result = addon.MB_OT_ScanMotionQuality.modal(
            harness,
            bpy.context,
            SimpleNamespace(type="MOUSEMOVE"),
        )
        if result != {"CANCELLED"} or addon._motion_quality_scan_state is not None:
            raise AssertionError(f"{name} did not finish through the modal callback")


def _check_isolated_analysis_cancel_and_rescan(armature: bpy.types.Object) -> None:
    settings = bpy.context.scene.motion_bridge_session
    addon._clear_motion_quality_result(settings)
    harness: _MotionQualityModalHarness | None = None
    try:
        harness = _begin_modal_harness(armature)
        timer_event = SimpleNamespace(type="TIMER", timer=harness._timer)
        deadline = time.monotonic() + 5.0
        while harness._analysis_job is None and time.monotonic() < deadline:
            addon.MB_OT_ScanMotionQuality.modal(harness, bpy.context, timer_event)
        job = harness._analysis_job
        if job is None:
            raise AssertionError("Isolated analysis job was not created")
        while job.phase == "serializing" and time.monotonic() < deadline:
            addon.MB_OT_ScanMotionQuality.modal(harness, bpy.context, timer_event)
        process = job._process
        if job.phase != "analyzing" or process is None:
            raise AssertionError("Isolated analysis process was not started")
        if process.pid == os.getpid():
            raise AssertionError("Motion Quality analysis ran in Blender's process")
        job_dir = job._job_dir
        addon._cancel_motion_quality_runtime()
        if job.phase != "cancelled":
            raise AssertionError("Cancelled analysis process was not reaped")
        if job_dir is not None and job_dir.exists():
            raise AssertionError("Cancelled analysis retained its temporary directory")
        result = addon.MB_OT_ScanMotionQuality.modal(
            harness,
            bpy.context,
            SimpleNamespace(type="MOUSEMOVE"),
        )
        if result != {"CANCELLED"}:
            raise AssertionError("Cancelled analysis did not exit its modal callback")
        if not addon.MB_OT_ScanMotionQuality.poll(bpy.context):
            raise AssertionError("A new scan was blocked after process cleanup")
        if settings.motion_quality_report_json:
            raise AssertionError("A cancelled analysis stored a report")
    finally:
        if addon._motion_quality_scan_state is not None:
            addon._cancel_motion_quality_runtime()


def _check_beginner_card_and_actions(armature: bpy.types.Object) -> None:
    scene = bpy.context.scene
    settings = scene.motion_bridge_session
    original_preview_range = (
        bool(scene.use_preview_range),
        int(scene.frame_preview_start),
        int(scene.frame_preview_end),
    )
    original_playback_range = (int(scene.frame_start), int(scene.frame_end))
    layout = _LayoutRecorder()
    facts = addon._workflow_facts(bpy.context)
    addon._draw_beginner_motion_quality(bpy.context, layout, settings, facts)
    operator_ids = [operator_id for operator_id, _properties in layout.operators]
    if addon.MB_OT_FocusMotionQualityIssue.bl_idname not in operator_ids:
        raise AssertionError("Beginner result did not offer an issue-range focus action")
    if addon.MB_OT_ScanMotionQuality.bl_idname not in operator_ids:
        raise AssertionError("Beginner result did not offer a rescan action")
    beginner_text = "\n".join(layout.labels)
    if "修正をおすすめする区間" not in beginner_text:
        raise AssertionError("Beginner card omitted its three-level result")
    if any(
        implementation_term in beginner_text
        for implementation_term in (
            "Quaternion",
            "NLA",
            "solver",
            "Profile",
            "time-map",
            "checksum",
            "LeftUpperArm",
            "RightUpperArm",
            "LeftFoot",
            "RightFoot",
        )
    ):
        raise AssertionError(
            f"Beginner Motion Quality card exposed implementation terms: {beginner_text!r}"
        )
    limit_message = addon._motion_quality_error_message(
        ValueError("motion quality issue interval limit exceeded")
    )
    if "確認したい区間が多すぎます" not in limit_message:
        raise AssertionError("Issue limit did not use a beginner-facing error")
    process_message = addon._motion_quality_error_message(
        addon.MotionQualityProcessError(
            "motion quality worker produced invalid output",
            stderr="Traceback: /private/internal/path",
        )
    )
    if "確認範囲を短くして" not in process_message:
        raise AssertionError("Process failure omitted a beginner recovery action")
    if "worker" in process_message or "/private/" in process_message:
        raise AssertionError("Process internals leaked into the beginner error")
    setup_message = addon._motion_quality_error_message(
        OSError("permission denied: /private/internal/input.pickle")
    )
    if "確認範囲を短くして" not in setup_message or "/private/" in setup_message:
        raise AssertionError("Setup failure leaked an internal path")

    original_preview = settings.motion_quality_issue_preview_json
    original_issue_count = int(settings.motion_quality_issue_count)
    settings.motion_quality_issue_preview_json = json.dumps(
        [
            {
                "frameEnd": index + 1,
                "frameStart": index + 1,
                "severity": "REVIEW",
                "subject": "モーション",
                "title": "動きを目で確認",
            }
            for index in range(12)
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    settings.motion_quality_issue_count = 20
    many_layout = _LayoutRecorder()
    addon._draw_beginner_motion_quality(
        bpy.context,
        many_layout,
        settings,
        addon._workflow_facts(bpy.context),
    )
    if "ほか 17 区間" not in many_layout.labels:
        raise AssertionError("Truncated preview did not show the true remaining count")
    settings.motion_quality_issue_preview_json = original_preview
    settings.motion_quality_issue_count = original_issue_count

    advanced = _LayoutRecorder()
    addon._draw_motion_quality_metrics(
        advanced,
        settings.motion_quality_report_json,
    )
    if not any("体全体の位置飛び" in label for label in advanced.labels):
        raise AssertionError("Advanced Motion Quality data omitted numeric metrics")

    preview = json.loads(settings.motion_quality_issue_preview_json)
    first = preview[0]
    if (
        bpy.ops.motion_bridge.focus_motion_quality_issue(
            frame_start=int(first["frameStart"]),
            frame_end=int(first["frameEnd"]),
        )
        != {"FINISHED"}
    ):
        raise AssertionError("Issue-range focus action failed")
    if not scene.use_preview_range:
        raise AssertionError("Issue-range focus did not enable Preview Range")
    whole_start, whole_end = addon.resolve_range_spec(
        whole_take_range=addon._motion_quality_whole_take_range(armature),
    ).expanded_integer_bounds()
    expected_first = max(
        whole_start,
        min(int(first["frameStart"]), whole_end),
    )
    if scene.frame_current != expected_first:
        raise AssertionError("Issue-range focus did not move to the first frame")

    # Focusing an issue follows the take rather than an unrelated narrow Scene
    # playback range, and changing only the viewing range keeps the useful
    # previous issue list visible until that subset is rescanned.
    scene.frame_start = whole_start
    scene.frame_end = whole_start
    focus_start = whole_end
    focus_end = whole_end
    if (
        bpy.ops.motion_bridge.focus_motion_quality_issue(
            frame_start=focus_start,
            frame_end=focus_end,
        )
        != {"FINISHED"}
    ):
        raise AssertionError("Take-relative issue focus failed")
    if (
        scene.frame_preview_start != focus_start
        or scene.frame_preview_end != focus_end
        or scene.frame_current != focus_start
        or scene.frame_start != whole_start
        or scene.frame_end != whole_end
    ):
        raise AssertionError("Issue focus clipped the interval to Scene playback")
    if not addon._motion_quality_result_is_motion_current(bpy.context, armature):
        raise AssertionError("A view-only focus invalidated the sampled motion")
    if addon._motion_quality_result_is_current(bpy.context, armature):
        raise AssertionError("A changed focus range was treated as already scanned")
    focused_layout = _LayoutRecorder()
    addon._draw_beginner_motion_quality(
        bpy.context,
        focused_layout,
        settings,
        addon._workflow_facts(bpy.context),
    )
    focused_operator_ids = [operator_id for operator_id, _ in focused_layout.operators]
    if addon.MB_OT_FocusMotionQualityIssue.bl_idname not in focused_operator_ids:
        raise AssertionError("View-only focus hid the previous issue list")
    if "表示中の範囲はまだ確認していません" not in focused_layout.labels:
        raise AssertionError("Changed focus range was not explained to beginners")

    scene.frame_start = original_playback_range[0]
    scene.frame_end = original_playback_range[1]
    scene.use_preview_range = original_preview_range[0]
    scene.frame_preview_start = original_preview_range[1]
    scene.frame_preview_end = original_preview_range[2]

    # Blender's system clipboard is intentionally unavailable in background
    # mode, so exercise the operator against the same small context contract.
    clipboard = SimpleNamespace(clipboard="")
    copy_context = SimpleNamespace(scene=scene, window_manager=clipboard)
    copy_operator = SimpleNamespace(report=lambda _levels, _message: None)
    if (
        addon.MB_OT_CopyMotionQualityReport.execute(copy_operator, copy_context)
        != {"FINISHED"}
    ):
        raise AssertionError("Motion Quality JSON copy action failed")
    if clipboard.clipboard != settings.motion_quality_report_json:
        raise AssertionError("Motion Quality JSON copy used the wrong report")


def _check_long_take_one_click_range(armature: bpy.types.Object) -> None:
    scene = bpy.context.scene
    original_limit = addon.MAX_MOTION_SAMPLES
    original_preview = (
        bool(scene.use_preview_range),
        int(scene.frame_preview_start),
        int(scene.frame_preview_end),
    )
    original_whole_take_range = addon._motion_quality_whole_take_range
    try:
        scene.use_preview_range = False
        addon.MAX_MOTION_SAMPLES = 2
        layout = _LayoutRecorder()
        addon._draw_beginner_motion_quality(
            bpy.context,
            layout,
            scene.motion_bridge_session,
            addon._workflow_facts(bpy.context),
        )
        bounded_buttons = [
            properties
            for operator_id, properties in layout.operators
            if operator_id == addon.MB_OT_ScanMotionQuality.bl_idname
            and bool(getattr(properties, "limit_to_current_window", False))
        ]
        if len(bounded_buttons) != 1:
            raise AssertionError("Long take did not offer one-click bounded checking")

        spec = addon._motion_quality_range_spec(
            bpy.context,
            armature,
            limit_to_current_window=True,
            commit_preview=True,
        )
        if spec.integer_sample_count != 2 or not scene.use_preview_range:
            raise AssertionError("One-click check did not create a bounded Preview Range")
        if (
            int(scene.frame_preview_start),
            int(scene.frame_preview_end),
        ) != spec.expanded_integer_bounds():
            raise AssertionError("Committed Preview Range disagrees with RangeSpec")

        addon._motion_quality_whole_take_range = lambda _armature: (1.5, 100.5)
        scene.frame_set(1)
        fractional = addon._motion_quality_range_spec(
            bpy.context,
            armature,
            limit_to_current_window=True,
            commit_preview=True,
        )
        reloaded = addon._motion_quality_range_spec(bpy.context, armature)
        if (
            fractional.expanded_integer_bounds() != (1, 2)
            or reloaded.expanded_integer_bounds() != (1, 2)
        ):
            raise AssertionError("Fractional take produced an invalid committed Preview Range")
    finally:
        addon._motion_quality_whole_take_range = original_whole_take_range
        addon.MAX_MOTION_SAMPLES = original_limit
        scene.use_preview_range = original_preview[0]
        scene.frame_preview_start = original_preview[1]
        scene.frame_preview_end = original_preview[2]


def _check_stale_and_history_invalidation() -> None:
    scene = bpy.context.scene
    settings = scene.motion_bridge_session
    scene.frame_set(scene.frame_start)
    if not addon._motion_quality_result_is_current(
        bpy.context,
        bpy.context.active_object,
    ):
        raise AssertionError("A normal frame change invalidated the previous report")

    armature = bpy.context.active_object
    original_revision = int(
        armature.get(addon.MOTION_REVISION_PROPERTY, 0)
    )
    armature[addon.MOTION_REVISION_PROPERTY] = original_revision + 1
    if addon._motion_quality_result_is_current(bpy.context, armature):
        raise AssertionError("A Correction Writer revision left the old report current")
    armature[addon.MOTION_REVISION_PROPERTY] = original_revision
    if not addon._motion_quality_result_is_current(bpy.context, armature):
        raise AssertionError("Restoring the input revision did not restore report currency")

    stack = addon.get_correction_layer_stack(armature)
    if stack is None:
        raise AssertionError("Correction layer stack disappeared during stale-result test")
    original_influence = float(stack.active_layer.strip.influence)
    stack.active_layer.strip.influence = max(0.0, original_influence - 0.25)
    if addon._motion_quality_result_is_current(bpy.context, armature):
        raise AssertionError("A changed layer strength left the old report current")
    stack.active_layer.strip.influence = original_influence
    if not addon._motion_quality_result_is_current(bpy.context, armature):
        raise AssertionError("Restoring layer strength did not restore report currency")

    original_fps = int(scene.render.fps)
    scene.render.fps = original_fps + 1
    if addon._motion_quality_result_is_current(bpy.context, bpy.context.active_object):
        raise AssertionError("A changed Scene FPS left the old report current")
    scene.render.fps = original_fps

    original_scene_end = scene.frame_end
    scene.frame_end = original_scene_end + 1
    if not addon._motion_quality_result_is_current(
        bpy.context,
        bpy.context.active_object,
    ):
        raise AssertionError("Scene playback range invalidated an active Preview result")
    scene.frame_end = original_scene_end

    original_preview_end = scene.frame_preview_end
    scene.frame_preview_end = min(3, original_preview_end + 1)
    if addon._motion_quality_result_is_current(bpy.context, bpy.context.active_object):
        raise AssertionError("A changed Preview Range left the old report current")
    layout = _LayoutRecorder()
    addon._draw_beginner_motion_quality(
        bpy.context,
        layout,
        settings,
        addon._workflow_facts(bpy.context),
    )
    if "表示中の範囲はまだ確認していません" not in layout.labels:
        raise AssertionError("An unscanned viewing range did not request a rescan")
    if not any(
        operator_id == addon.MB_OT_FocusMotionQualityIssue.bl_idname
        for operator_id, _properties in layout.operators
    ):
        raise AssertionError("A view-only range change hid the previous issue list")
    scene.frame_preview_end = original_preview_end

    addon._handle_motion_quality_history_post()
    if settings.motion_quality_summary != "NOT_RUN":
        raise AssertionError("Undo/Redo history did not invalidate the quality result")
    if settings.motion_quality_report_json or settings.motion_quality_target is not None:
        raise AssertionError("History invalidation retained stale quality data")


def main() -> None:
    _clear_scene()
    addon.register()
    try:
        armature = _create_canonical_armature()
        _key_motion(armature)
        scene = bpy.context.scene
        scene.frame_start = 1
        scene.frame_end = 3
        scene.render.fps = 30
        scene.render.fps_base = 1.0
        create_correction_layer(bpy.context, armature)

        payload = _scan_and_check_result(armature)
        _check_beginner_card_and_actions(armature)
        _check_long_take_one_click_range(armature)
        _check_stale_and_history_invalidation()
        _check_modal_lifecycle(armature)
        _check_cancel_cleanup_retry(armature)
        _check_load_detach_cleanup_retry()
        _check_chunk_yield_restores_visible_frame(armature)
        _check_changed_settings_discard_running_report(armature)
        _check_pre_handlers_restore_and_cancel(armature)
        _check_isolated_analysis_cancel_and_rescan(armature)
        print(
            "MOTION_BRIDGE_MOTION_QUALITY_UI="
            + json.dumps(
                {
                    "issueCount": len(payload["issues"]),
                    "jsonStored": True,
                    "stateRestored": True,
                    "beginnerTerms": True,
                    "rangeFocus": True,
                    "rangeFocusPreservesReport": True,
                    "rangeFocusUsesTake": True,
                    "historyInvalidation": True,
                    "modalLifecycle": True,
                    "samplingYieldRestored": True,
                    "preHandlerCancellation": True,
                    "isolatedAnalysisProcess": True,
                    "staleAnalysisDiscarded": True,
                    "writerRevisionInvalidation": True,
                    "layerStrengthInvalidation": True,
                    "issueLimitFriendly": True,
                    "processFailureFriendly": True,
                    "previewTotalCount": True,
                    "previewRangeSampling": True,
                    "longTakeOneClickRange": True,
                    "fractionalTakeRange": True,
                    "cancelCleanupRetry": True,
                    "loadDetachCleanupRetry": True,
                },
                sort_keys=True,
            )
        )
    finally:
        addon.unregister()


if __name__ == "__main__":
    main()
