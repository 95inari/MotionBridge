from __future__ import annotations

import hashlib
import io
import pickle
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from motion_bridge.motion_quality import (
    BoneSample,
    ContactSample,
    MotionFrame,
    MotionSequence,
    MotionValidationSettings,
    analyze_motion,
)
from motion_bridge.motion_quality_process import (
    MotionQualityProcessError,
    MotionQualityProcessJob,
)
import motion_bridge.motion_quality_process as process_module
import motion_bridge.motion_quality_worker as worker_module


IDENTITY = (1.0, 0.0, 0.0, 0.0)


def _frame(index: int, bone_name: str = "Hips") -> MotionFrame:
    bone = BoneSample(
        name=bone_name,
        local_rotation_wxyz=IDENTITY,
        global_rotation_wxyz=IDENTITY,
        head_world=(0.0, 0.0, 0.0),
        tail_world=(0.0, 0.0, 1.0),
    )
    return MotionFrame(
        frame=index,
        time_seconds=index / 30.0,
        root_location=(index * 0.01, 0.0, 0.0),
        root_rotation_wxyz=IDENTITY,
        root_scale=(1.0, 1.0, 1.0),
        bones=(bone,),
    )


def _sequence(count: int = 3) -> MotionSequence:
    return MotionSequence(
        fps=30.0,
        start_frame=0,
        end_frame=count - 1,
        frames=tuple(_frame(index) for index in range(count)),
        source_id="process-test",
    )


def _frame_with_contacts(
    index: int,
    contacts: tuple[ContactSample, ...],
) -> MotionFrame:
    frame = _frame(index)
    return MotionFrame(
        frame=frame.frame,
        time_seconds=frame.time_seconds,
        root_location=frame.root_location,
        root_rotation_wxyz=frame.root_rotation_wxyz,
        root_scale=frame.root_scale,
        bones=frame.bones,
        contacts=contacts,
    )


def _hierarchical_sequence() -> MotionSequence:
    hips = _frame(0).bones[0]
    spine = BoneSample(
        name="Spine",
        local_rotation_wxyz=IDENTITY,
        global_rotation_wxyz=IDENTITY,
        head_world=(0.0, 0.0, 1.0),
        tail_world=(0.0, 0.0, 2.0),
        parent="Hips",
    )
    frame = MotionFrame(
        frame=0,
        time_seconds=0.0,
        root_location=(0.0, 0.0, 0.0),
        root_rotation_wxyz=IDENTITY,
        root_scale=(1.0, 1.0, 1.0),
        bones=(hips, spine),
    )
    return MotionSequence(
        fps=30.0,
        start_frame=0,
        end_frame=0,
        frames=(frame,),
        source_id="hierarchical-process-test",
    )


def _settings(**overrides: object) -> MotionValidationSettings:
    values: dict[str, object] = {
        "auto_detect_contacts": False,
        "limbs": (),
    }
    values.update(overrides)
    return MotionValidationSettings(**values)


def _finish(job: MotionQualityProcessJob, timeout: float = 5.0):
    while job.phase == "serializing":
        job.step_serialization(time.monotonic() + 0.008)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        report = job.poll()
        if report is not None:
            return report
        time.sleep(0.01)
    raise AssertionError("motion quality worker did not finish")


class MotionQualityProcessTests(unittest.TestCase):
    def test_constructor_closes_input_after_header_failure(self) -> None:
        opened_writers = []
        job_directories: list[Path] = []
        actual_open = Path.open

        def recording_open(path: Path, *args, **kwargs):
            stream = actual_open(path, *args, **kwargs)
            if path.name == process_module._INPUT_PART_NAME:
                opened_writers.append(stream)
                job_directories.append(path.parent)
            return stream

        with (
            patch.object(Path, "open", new=recording_open),
            patch.object(
                process_module,
                "_encode_input_header",
                side_effect=RuntimeError("injected header failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "header failure"),
        ):
            MotionQualityProcessJob(_sequence(1), _settings())

        self.assertEqual(1, len(opened_writers))
        self.assertTrue(opened_writers[0].closed)
        self.assertEqual(1, len(job_directories))
        self.assertFalse(job_directories[0].exists())

    def test_transport_hash_tracks_written_bytes_and_rejects_short_write(self) -> None:
        class ShortWriter:
            def __init__(self) -> None:
                self.data = bytearray()

            def write(self, data: bytes) -> int:
                written = min(3, len(data))
                self.data.extend(data[:written])
                return written

        target = ShortWriter()
        writer = process_module._DigestingWriter(target)

        with self.assertRaisesRegex(OSError, "short write"):
            writer.write(b"abcdef")

        self.assertEqual(
            hashlib.sha256(bytes(target.data)).hexdigest(),
            writer.hexdigest(),
        )

    def test_worker_hashes_and_decodes_from_the_same_rewound_stream(self) -> None:
        payload = b"motion-quality-input"
        stream = io.BytesIO(payload)

        digest = worker_module._input_sha256(stream)

        self.assertEqual(hashlib.sha256(payload).hexdigest(), digest)
        self.assertEqual(0, stream.tell())

    def test_separate_pickle_records_preserve_parent_names(self) -> None:
        sequence = _hierarchical_sequence()
        settings = _settings()
        job = MotionQualityProcessJob(sequence, settings)
        try:
            report = _finish(job)
            self.assertEqual(
                analyze_motion(sequence, settings).canonical_json(),
                report.canonical_json(),
            )
        finally:
            job.cancel()

    def test_deadline_stream_and_worker_match_synchronous_report(self) -> None:
        sequence = _sequence(4)
        settings = _settings()
        job = MotionQualityProcessJob(sequence, settings)
        try:
            # Even an expired deadline makes one frame of bounded progress.
            self.assertFalse(job.step_serialization(time.monotonic() - 1.0))
            self.assertEqual("serializing", job.phase)
            self.assertEqual(0.25, job.progress)

            report = _finish(job)

            self.assertEqual("complete", job.phase)
            self.assertEqual(1.0, job.progress)
            self.assertRegex(job._expected_input_hash or "", r"^[0-9a-f]{64}$")
            self.assertEqual(
                analyze_motion(sequence, settings).canonical_json(),
                report.canonical_json(),
            )
            self.assertIsNone(job._job_dir)
        finally:
            job.cancel()

    def test_worker_validation_failure_includes_stderr_and_cleans_temp(self) -> None:
        # MotionSequence construction permits this cross-frame mismatch; the
        # worker's full validator must reject it before analysis output exists.
        sequence = MotionSequence(
            fps=30.0,
            start_frame=0,
            end_frame=1,
            frames=(_frame(0, "Hips"), _frame(1, "Spine")),
        )
        job = MotionQualityProcessJob(sequence, _settings())
        job_dir = job._job_dir
        self.assertIsNotNone(job_dir)
        while job.phase == "serializing":
            job.step_serialization(time.monotonic() + 100.0)

        deadline = time.monotonic() + 5.0
        with self.assertRaises(MotionQualityProcessError) as raised:
            while time.monotonic() < deadline:
                job.poll()
                time.sleep(0.01)
            self.fail("worker failure was not reported")

        self.assertIn("inconsistent bone set", raised.exception.stderr)
        self.assertEqual(1, raised.exception.returncode)
        self.assertEqual("failed", job.phase)
        assert job_dir is not None
        self.assertFalse(job_dir.exists())

    def test_serialization_enforces_total_bone_sample_budget(self) -> None:
        job = MotionQualityProcessJob(
            _sequence(2),
            _settings(max_total_bone_samples=1),
        )
        job_dir = job._job_dir
        with self.assertRaisesRegex(
            MotionQualityProcessError,
            "bone-sample budget",
        ):
            job.step_serialization(time.monotonic() + 100.0)
        self.assertEqual("failed", job.phase)
        assert job_dir is not None
        self.assertFalse(job_dir.exists())

    def test_serialization_rejects_oversized_contact_frame_before_pickling(self) -> None:
        contacts = tuple(
            ContactSample(f"Effector{index}", False, 0.0)
            for index in range(process_module._MAX_CONTACT_COUNT + 1)
        )
        sequence = MotionSequence(
            fps=30.0,
            start_frame=0,
            end_frame=0,
            frames=(_frame_with_contacts(0, contacts),),
        )
        job = MotionQualityProcessJob(sequence, _settings())
        job_dir = job._job_dir

        with self.assertRaisesRegex(
            MotionQualityProcessError,
            "contact-count budget",
        ):
            job.step_serialization(time.monotonic() + 100.0)

        assert job_dir is not None
        self.assertFalse(job_dir.exists())

    def test_serialization_enforces_total_contact_sample_budget(self) -> None:
        contact = ContactSample("Hips", False, 0.0)
        sequence = MotionSequence(
            fps=30.0,
            start_frame=0,
            end_frame=1,
            frames=(
                _frame_with_contacts(0, (contact,)),
                _frame_with_contacts(1, (contact,)),
            ),
        )
        with patch.object(process_module, "_MAX_TOTAL_CONTACT_SAMPLES", 1):
            job = MotionQualityProcessJob(sequence, _settings())
            with self.assertRaisesRegex(
                MotionQualityProcessError,
                "contact-sample budget",
            ):
                job.step_serialization(time.monotonic() + 100.0)

    def test_frame_decoder_rejects_contact_count_before_construction(self) -> None:
        payload = list(process_module._encode_frame(_frame(0)))
        payload[8] = tuple(
            (f"Effector{index}", False, 0.0)
            for index in range(process_module._MAX_CONTACT_COUNT + 1)
        )

        with (
            patch.object(
                process_module,
                "ContactSample",
                side_effect=AssertionError("contact construction must not run"),
            ) as constructor,
            self.assertRaisesRegex(ValueError, "contact-count budget"),
        ):
            process_module._decode_frame(tuple(payload))
        constructor.assert_not_called()

    def test_cancel_terminates_reaps_and_removes_owned_directory(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.returncode = None
                self.terminated = False
                self.waited = False

            def poll(self):
                return self.returncode

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout=None):
                self.waited = True
                self.returncode = -15
                return self.returncode

            def kill(self) -> None:
                self.returncode = -9

        fake = FakeProcess()
        sequence = _sequence(1)
        with patch.object(process_module.subprocess, "Popen", return_value=fake) as popen:
            job = MotionQualityProcessJob(sequence, _settings())
            job.step_serialization(time.monotonic() + 100.0)
        job_dir = job._job_dir
        self.assertEqual("analyzing", job.phase)
        self.assertIsNotNone(job_dir)

        command = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        self.assertEqual("-I", command[1])
        self.assertEqual(Path(sys.executable).resolve(), Path(command[0]).resolve())
        self.assertIs(False, kwargs["shell"])

        job.cancel()

        self.assertTrue(fake.terminated)
        self.assertTrue(fake.waited)
        self.assertEqual("cancelled", job.phase)
        assert job_dir is not None
        self.assertFalse(job_dir.exists())

    def test_spawn_stream_close_failure_retains_live_process_for_cancel(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.returncode = None
                self.terminated = False
                self.waited = False

            def poll(self):
                return self.returncode

            def terminate(self) -> None:
                self.terminated = True
                self.returncode = -15

            def wait(self, timeout=None):
                self.waited = True
                return self.returncode

            def kill(self) -> None:
                self.returncode = -9

        class CloseErrorStream:
            def __init__(self, stream) -> None:
                self._stream = stream

            def close(self) -> None:
                self._stream.close()
                raise OSError("injected stderr close failure")

        fake = FakeProcess()
        actual_open = Path.open

        def open_with_close_error(path: Path, *args, **kwargs):
            stream = actual_open(path, *args, **kwargs)
            if path.name == process_module._STDERR_NAME:
                return CloseErrorStream(stream)
            return stream

        with (
            patch.object(Path, "open", new=open_with_close_error),
            patch.object(process_module.subprocess, "Popen", return_value=fake),
        ):
            job = MotionQualityProcessJob(_sequence(1), _settings())
            job_dir = job._job_dir
            with self.assertRaisesRegex(
                MotionQualityProcessError,
                "stderr close failure",
            ):
                job.step_serialization(time.monotonic() + 100.0)

        self.assertEqual("failed", job.phase)
        self.assertIs(job._process, fake)
        assert job_dir is not None
        self.assertTrue(job_dir.exists())

        job.cancel()

        self.assertTrue(fake.terminated)
        self.assertTrue(fake.waited)
        self.assertIsNone(job._process)
        self.assertFalse(job_dir.exists())

    def test_failed_cancel_can_retry_a_live_process(self) -> None:
        class FlakyProcess:
            def __init__(self) -> None:
                self.returncode = None
                self.allow_terminate = False
                self.terminate_calls = 0

            def poll(self):
                return self.returncode

            def terminate(self) -> None:
                self.terminate_calls += 1
                if not self.allow_terminate:
                    raise OSError("injected terminate failure")
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self) -> None:
                self.returncode = -9

        fake = FlakyProcess()
        with patch.object(process_module.subprocess, "Popen", return_value=fake):
            job = MotionQualityProcessJob(_sequence(1), _settings())
            job.step_serialization(time.monotonic() + 100.0)
        job_dir = job._job_dir

        with self.assertRaisesRegex(MotionQualityProcessError, "could not stop"):
            job.cancel()
        self.assertEqual("failed", job.phase)
        self.assertIs(job._process, fake)
        assert job_dir is not None
        self.assertTrue(job_dir.exists())

        fake.allow_terminate = True
        job.cancel()

        self.assertEqual(2, fake.terminate_calls)
        self.assertEqual("cancelled", job.phase)
        self.assertIsNone(job._process)
        self.assertFalse(job_dir.exists())

    def test_failed_wait_can_retry_reaping_an_exited_process(self) -> None:
        class FlakyWaitProcess:
            returncode = 0

            def __init__(self) -> None:
                self.wait_calls = 0

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise OSError("injected wait failure")
                return self.returncode

            def terminate(self) -> None:
                raise AssertionError("exited process must not be terminated")

            def kill(self) -> None:
                raise AssertionError("exited process must not be killed")

        fake = FlakyWaitProcess()
        with patch.object(process_module.subprocess, "Popen", return_value=fake):
            job = MotionQualityProcessJob(_sequence(1), _settings())
            job.step_serialization(time.monotonic() + 100.0)
        job_dir = job._job_dir

        with self.assertRaisesRegex(MotionQualityProcessError, "could not stop"):
            job.cancel()
        self.assertEqual("failed", job.phase)
        self.assertIs(job._process, fake)
        assert job_dir is not None
        self.assertTrue(job_dir.exists())

        job.cancel()

        self.assertEqual(2, fake.wait_calls)
        self.assertEqual("cancelled", job.phase)
        self.assertIsNone(job._process)
        self.assertFalse(job_dir.exists())

    def test_cancel_kills_and_reaps_after_terminate_timeout(self) -> None:
        class TimeoutProcess:
            def __init__(self) -> None:
                self.returncode = None
                self.terminated = False
                self.killed = False
                self.wait_calls = 0

            def poll(self):
                return self.returncode

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout=None):
                self.wait_calls += 1
                if not self.killed:
                    raise process_module.subprocess.TimeoutExpired("worker", timeout)
                self.returncode = -9
                return self.returncode

            def kill(self) -> None:
                self.killed = True

        fake = TimeoutProcess()
        with patch.object(process_module.subprocess, "Popen", return_value=fake):
            job = MotionQualityProcessJob(_sequence(1), _settings())
            job.step_serialization(time.monotonic() + 100.0)

        job.cancel()

        self.assertTrue(fake.terminated)
        self.assertTrue(fake.killed)
        self.assertEqual(2, fake.wait_calls)
        self.assertEqual("cancelled", job.phase)

    def test_temporary_cleanup_can_retry_after_both_initial_attempts_fail(self) -> None:
        job = MotionQualityProcessJob(_sequence(1), _settings())
        temporary = job._temporary
        job_dir = job._job_dir
        assert temporary is not None
        assert job_dir is not None
        actual_cleanup = temporary.cleanup
        cleanup_calls = 0

        def flaky_cleanup() -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1
            if cleanup_calls <= 2:
                raise OSError("injected cleanup failure")
            actual_cleanup()

        with patch.object(temporary, "cleanup", side_effect=flaky_cleanup):
            with self.assertRaisesRegex(OSError, "cleanup failure"):
                job.cancel()
            self.assertIs(job._temporary, temporary)
            self.assertEqual(job_dir, job._job_dir)
            self.assertTrue(job_dir.exists())

            job.cancel()

        self.assertEqual(3, cleanup_calls)
        self.assertIsNone(job._temporary)
        self.assertIsNone(job._job_dir)
        self.assertFalse(job_dir.exists())

    def test_invalid_worker_output_is_rejected_before_return(self) -> None:
        class CompletedProcess:
            returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        def spawn_with_invalid_output(command, **_kwargs):
            job_dir = Path(command[-1])
            with (job_dir / process_module._OUTPUT_NAME).open("wb") as stream:
                pickle.dump({"format": "not-a-report"}, stream)
            return CompletedProcess()

        with patch.object(
            process_module.subprocess,
            "Popen",
            side_effect=spawn_with_invalid_output,
        ):
            job = MotionQualityProcessJob(_sequence(1), _settings())
            job_dir = job._job_dir
            job.step_serialization(time.monotonic() + 100.0)
            with self.assertRaisesRegex(
                MotionQualityProcessError,
                "invalid output",
            ):
                job.poll()

        self.assertEqual("failed", job.phase)
        assert job_dir is not None
        self.assertFalse(job_dir.exists())

    def test_output_file_size_is_rejected_before_unpickling(self) -> None:
        job = MotionQualityProcessJob(_sequence(1), _settings())
        job_dir = job._job_dir
        assert job_dir is not None
        output = job_dir / process_module._OUTPUT_NAME
        try:
            with output.open("xb") as stream:
                stream.truncate(process_module._MAX_OUTPUT_BYTES + 1)
            with self.assertRaisesRegex(ValueError, "output budget"):
                job._read_output_payload()
        finally:
            job.cancel()

    def test_report_decoder_rejects_oversized_collections_before_building(self) -> None:
        report = analyze_motion(_sequence(1), _settings())
        payload = process_module._encode_report(report, input_hash="a" * 64)

        too_many_metrics = dict(payload)
        too_many_metrics["metrics"] = ((),) * 65
        with self.assertRaisesRegex(ValueError, "too many metrics"):
            process_module._decode_report(too_many_metrics)

        too_many_issues = dict(payload)
        too_many_issues["issues"] = ((),) * (
            process_module.MAX_MOTION_QUALITY_ISSUES + 1
        )
        with self.assertRaisesRegex(ValueError, "issue interval limit"):
            process_module._decode_report(too_many_issues)

    def test_report_decoder_rejects_invalid_input_digest(self) -> None:
        report = analyze_motion(_sequence(1), _settings())
        payload = process_module._encode_report(report, input_hash="a" * 64)
        payload["input_hash"] = "not-a-sha256"

        with self.assertRaisesRegex(ValueError, "input hash is invalid"):
            process_module._decode_report(payload)

    def test_worker_input_hash_must_match_streamed_input(self) -> None:
        sequence = _sequence(1)
        settings = _settings()

        class CompletedProcess:
            returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        def spawn_with_wrong_input_hash(command, **_kwargs):
            job_dir = Path(command[-1])
            report = analyze_motion(sequence, settings)
            payload = process_module._encode_report(
                report,
                input_hash="0" * 64,
            )
            with (job_dir / process_module._OUTPUT_NAME).open("wb") as stream:
                pickle.dump(payload, stream)
            return CompletedProcess()

        with patch.object(
            process_module.subprocess,
            "Popen",
            side_effect=spawn_with_wrong_input_hash,
        ):
            job = MotionQualityProcessJob(sequence, settings)
            job.step_serialization(time.monotonic() + 100.0)
            with self.assertRaisesRegex(
                MotionQualityProcessError,
                "input hash does not match",
            ):
                job.poll()

    def test_module_has_no_blender_dependency(self) -> None:
        self.assertNotIn("bpy", sys.modules)


if __name__ == "__main__":
    unittest.main()
