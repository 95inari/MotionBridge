"""Run pure motion-quality analysis in an isolated child Python process.

Blender's main thread incrementally writes one sampled frame at a time.  Once
the stream is complete, the worker performs the CPU-heavy analysis without a
resident thread touching Blender's lifetime or undo/load state.

The pickle transport contains built-in values only.  This is important for
Blender Extensions, whose import namespace can differ from the stand-alone
worker's namespace.  Pickle files are confined to a private directory created
and owned by this job; they are not a public interchange format.
"""

from __future__ import annotations

from dataclasses import fields
import gc
import hashlib
import math
import os
from pathlib import Path
import pickle
import re
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, BinaryIO, Final

from .motion_quality import (
    BoneSample,
    ContactSample,
    EffectorSample,
    FrameInterval,
    IssueSeverity,
    LimbDefinition,
    MAX_MOTION_QUALITY_ISSUES,
    MetricSummary,
    MotionFrame,
    MotionIssue,
    MotionQualityReport,
    MotionSequence,
    MotionValidationSettings,
    QualitySummary,
)


_INPUT_FORMAT: Final = "motion-bridge.motion-quality-process-input/1"
_FRAME_FORMAT: Final = "motion-bridge.motion-quality-process-frame/1"
_REPORT_FORMAT: Final = "motion-bridge.motion-quality-process-report/1"
_END_FORMAT: Final = "motion-bridge.motion-quality-process-end/1"

_INPUT_NAME: Final = "input.pickle"
_INPUT_PART_NAME: Final = "input.pickle.part"
_OUTPUT_NAME: Final = "output.pickle"
_STDERR_NAME: Final = "worker.stderr"
_JOB_PREFIX: Final = "motion-bridge-quality-"
# Protocol 3 uses explicit BINPUT indexes.  Unlike protocol 4+ MEMOIZE, those
# indexes remain valid when both sides clear their memo between concatenated
# records, keeping streaming memory bounded independently of take length.
_STREAM_PICKLE_PROTOCOL: Final = 3

# These hard ceilings remain in force even if a caller constructs unusually
# permissive MotionValidationSettings.  Defaults in motion_quality.py use the
# same semantic limits.
_MAX_FRAME_COUNT: Final = 100_000
_MAX_BONE_COUNT: Final = 256
_MAX_EFFECTOR_COUNT: Final = 64
_MAX_CONTACT_COUNT: Final = _MAX_BONE_COUNT + _MAX_EFFECTOR_COUNT
_MAX_TOTAL_BONE_SAMPLES: Final = 2_000_000
_MAX_TOTAL_CONTACT_SAMPLES: Final = 2_000_000
_MAX_INPUT_BYTES: Final = 2 * 1024 * 1024 * 1024
_MAX_OUTPUT_BYTES: Final = 32 * 1024 * 1024
_MAX_STDERR_BYTES: Final = 64 * 1024
_CANCEL_WAIT_SECONDS: Final = 2.0

_PHASE_SERIALIZING: Final = "serializing"
_PHASE_ANALYZING: Final = "analyzing"
_PHASE_COMPLETE: Final = "complete"
_PHASE_FAILED: Final = "failed"
_PHASE_CANCELLED: Final = "cancelled"


class _BuiltinsOnlyUnpickler(pickle.Unpickler):
    """Reject GLOBAL/STACK_GLOBAL so transport pickle cannot import code."""

    def find_class(self, module: str, name: str) -> Any:
        raise pickle.UnpicklingError(
            f"global pickle value {module}.{name} is not permitted"
        )


class _DigestingWriter:
    """Forward pickle bytes while calculating their exact transport digest."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._hasher = hashlib.sha256()

    def write(self, data: bytes) -> int:
        written = self._stream.write(data)
        if type(written) is not int or written < 0 or written > len(data):
            raise OSError("motion quality stream returned an invalid write count")
        self._hasher.update(data[:written])
        if written != len(data):
            raise OSError("motion quality stream produced a short write")
        return written

    def tell(self) -> int:
        return self._stream.tell()

    def flush(self) -> None:
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()

    def hexdigest(self) -> str:
        return self._hasher.hexdigest()


class MotionQualityProcessError(RuntimeError):
    """Explicit job failure, including bounded worker stderr when available."""

    def __init__(
        self,
        message: str,
        *,
        stderr: str = "",
        returncode: int | None = None,
    ) -> None:
        self.stderr = stderr
        self.returncode = returncode
        detail = ""
        if stderr:
            lines = [line.strip() for line in stderr.splitlines() if line.strip()]
            if lines:
                detail = f": {lines[-1]}"
        super().__init__(f"{message}{detail}")


class MotionQualityProcessJob:
    """Incrementally serialize and analyze one immutable motion sequence.

    ``step_serialization`` accepts an absolute ``time.monotonic()`` deadline.
    It always writes at least one whole frame while work remains, then checks
    the deadline between frames.  ``progress`` is serialization progress; it
    stays at 1.0 while the analysis phase is indeterminate.
    """

    def __init__(
        self,
        sequence: MotionSequence,
        settings: MotionValidationSettings,
    ) -> None:
        if not isinstance(sequence, MotionSequence):
            raise TypeError("sequence must be a MotionSequence")
        if not isinstance(settings, MotionValidationSettings):
            raise TypeError("settings must be MotionValidationSettings")

        frame_count = len(sequence.frames)
        maximum_frames = min(settings.max_frame_count, _MAX_FRAME_COUNT)
        if frame_count == 0:
            raise ValueError("motion sequence must contain at least one frame")
        if frame_count > maximum_frames:
            raise ValueError("motion sequence exceeds the process frame budget")

        self._phase = _PHASE_SERIALIZING
        self._sequence: MotionSequence | None = sequence
        self._settings = settings
        self._expected_frame_count = frame_count
        self._expected_settings_hash = settings.content_hash()
        self._expected_input_hash: str | None = None
        self._frame_index = 0
        self._total_bone_samples = 0
        self._total_contact_samples = 0
        self._process: subprocess.Popen[bytes] | None = None
        self._result: MotionQualityReport | None = None
        self._failure: MotionQualityProcessError | None = None
        self._writer: _DigestingWriter | None = None
        self._pickler: pickle.Pickler | None = None
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._job_dir: Path | None = None

        temporary: tempfile.TemporaryDirectory[str] | None = None
        raw_writer: BinaryIO | None = None
        try:
            temporary = tempfile.TemporaryDirectory(
                prefix=_JOB_PREFIX,
                ignore_cleanup_errors=False,
            )
            job_dir = Path(temporary.name).resolve(strict=True)
            if os.name != "nt":
                job_dir.chmod(0o700)
            input_part = job_dir / _INPUT_PART_NAME
            raw_writer = input_part.open("xb", buffering=64 * 1024)
            if os.name != "nt":
                input_part.chmod(0o600)
            writer = _DigestingWriter(raw_writer)
            pickler = pickle.Pickler(writer, protocol=_STREAM_PICKLE_PROTOCOL)
            pickler.dump(
                _encode_input_header(sequence, settings),
            )
            # Every transport record owns its memo.  The worker clears the
            # corresponding Unpickler memo after each load; this prevents a
            # BINGET in a later independently encoded frame from resolving to
            # an object stored by the header or a previous frame.
            pickler.clear_memo()
        except Exception:
            if raw_writer is not None:
                try:
                    raw_writer.close()
                except Exception:
                    pass
            if temporary is not None:
                try:
                    temporary.cleanup()
                except Exception:
                    # Preserve the construction error. TemporaryDirectory's
                    # finalizer retains a second best-effort cleanup path.
                    pass
            raise

        self._temporary = temporary
        self._job_dir = job_dir
        self._writer = writer
        self._pickler = pickler

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def progress(self) -> float:
        if self._phase == _PHASE_COMPLETE:
            return 1.0
        if self._expected_frame_count <= 0:
            return 0.0
        return min(1.0, self._frame_index / self._expected_frame_count)

    def step_serialization(self, deadline: float) -> bool:
        """Write complete frames until ``deadline``; return true when submitted."""

        if type(deadline) not in (int, float) or not math.isfinite(float(deadline)):
            raise ValueError("deadline must be a finite monotonic timestamp")
        if self._phase == _PHASE_FAILED:
            assert self._failure is not None
            raise self._failure
        if self._phase != _PHASE_SERIALIZING:
            return self._phase in (_PHASE_ANALYZING, _PHASE_COMPLETE)

        sequence = self._sequence
        writer = self._writer
        pickler = self._pickler
        if sequence is None or writer is None or pickler is None:
            error = self._set_failure("motion quality serialization state is unavailable")
            raise error

        try:
            while self._frame_index < self._expected_frame_count:
                frame = sequence.frames[self._frame_index]
                self._validate_frame_budget(frame)
                pickler.dump(_encode_frame(frame))
                pickler.clear_memo()
                self._frame_index += 1
                if writer.tell() > _MAX_INPUT_BYTES:
                    raise ValueError("motion sequence exceeds the serialized input budget")
                if (
                    self._frame_index < self._expected_frame_count
                    and time.monotonic() >= float(deadline)
                ):
                    break

            if self._frame_index == self._expected_frame_count:
                self._finish_serialization()
        except MotionQualityProcessError:
            raise
        except Exception as cause:
            error = self._set_failure(f"motion quality serialization failed: {cause}")
            raise error from cause
        return self._phase == _PHASE_ANALYZING

    def poll(self) -> MotionQualityReport | None:
        """Return the completed report, ``None`` while busy, or raise on failure."""

        if self._phase == _PHASE_COMPLETE:
            return self._result
        if self._phase == _PHASE_FAILED:
            assert self._failure is not None
            raise self._failure
        if self._phase in (_PHASE_SERIALIZING, _PHASE_CANCELLED):
            return None

        process = self._process
        if process is None:
            error = self._set_failure("motion quality worker process is unavailable")
            raise error
        returncode = process.poll()
        if returncode is None:
            return None

        process.wait()
        self._process = None
        stderr = self._read_worker_stderr()
        if returncode != 0:
            error = self._set_failure(
                f"motion quality worker exited with status {returncode}",
                stderr=stderr,
                returncode=returncode,
            )
            raise error

        try:
            payload = self._read_output_payload()
            if (
                type(payload) is not dict
                or self._expected_input_hash is None
                or payload.get("input_hash") != self._expected_input_hash
            ):
                raise ValueError(
                    "worker output input hash does not match the submitted stream"
                )
            report = _decode_report(payload)
            if not isinstance(report, MotionQualityReport):
                raise TypeError("worker output is not a MotionQualityReport")
            if report.sample_count != self._expected_frame_count:
                raise ValueError("worker output sample count does not match the input")
            if report.settings_hash != self._expected_settings_hash:
                raise ValueError("worker output settings hash does not match the input")
        except Exception as cause:
            error = self._set_failure(
                f"motion quality worker produced invalid output: {cause}",
                stderr=stderr,
                returncode=returncode,
            )
            raise error from cause

        self._result = report
        self._phase = _PHASE_COMPLETE
        self._cleanup_temporary()
        return report

    def cancel(self) -> None:
        """Terminate and reap the worker, close streams, and remove owned files."""

        process = self._process
        if self._phase in (_PHASE_COMPLETE, _PHASE_FAILED, _PHASE_CANCELLED):
            if process is None:
                self._cleanup_temporary()
                return

        self._close_writer()
        process = self._process
        if process is not None:
            try:
                returncode = process.poll()
                if returncode is None:
                    process.terminate()
                    try:
                        process.wait(timeout=_CANCEL_WAIT_SECONDS)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=_CANCEL_WAIT_SECONDS)
                else:
                    process.wait()
            except Exception as cause:
                # Keep the handle and private directory so another lifecycle
                # callback can retry termination/reaping instead of orphaning
                # a worker whose state is uncertain.
                error = MotionQualityProcessError(
                    f"could not stop motion quality worker: {cause}"
                )
                self._failure = error
                self._phase = _PHASE_FAILED
                raise error from cause
            else:
                self._process = None

        self._sequence = None
        self._phase = _PHASE_CANCELLED
        self._cleanup_temporary()

    def _validate_frame_budget(self, frame: MotionFrame) -> None:
        bone_count = len(frame.bones)
        effector_count = len(frame.effectors)
        contact_count = len(frame.contacts)
        if bone_count > min(self._settings.max_bone_count, _MAX_BONE_COUNT):
            raise ValueError(
                f"frame {frame.frame:g} exceeds the process bone-count budget"
            )
        if effector_count > min(self._settings.max_effector_count, _MAX_EFFECTOR_COUNT):
            raise ValueError(
                f"frame {frame.frame:g} exceeds the process effector-count budget"
            )
        maximum_contacts = min(
            self._settings.max_bone_count + self._settings.max_effector_count,
            _MAX_CONTACT_COUNT,
        )
        if contact_count > maximum_contacts:
            raise ValueError(
                f"frame {frame.frame:g} exceeds the process contact-count budget"
            )
        self._total_bone_samples += bone_count
        self._total_contact_samples += contact_count
        maximum_samples = min(
            self._settings.max_total_bone_samples,
            _MAX_TOTAL_BONE_SAMPLES,
        )
        if self._total_bone_samples > maximum_samples:
            raise ValueError("motion sequence exceeds the process bone-sample budget")
        if self._total_contact_samples > _MAX_TOTAL_CONTACT_SAMPLES:
            raise ValueError("motion sequence exceeds the process contact-sample budget")

    def _finish_serialization(self) -> None:
        writer = self._writer
        pickler = self._pickler
        job_dir = self._job_dir
        if writer is None or pickler is None or job_dir is None:
            raise RuntimeError("motion quality serialization state is unavailable")
        pickler.dump((_END_FORMAT, self._expected_frame_count))
        pickler.clear_memo()
        writer.flush()
        self._expected_input_hash = writer.hexdigest()
        self._close_writer()

        input_part = job_dir / _INPUT_PART_NAME
        input_path = job_dir / _INPUT_NAME
        if input_part.stat().st_size > _MAX_INPUT_BYTES:
            raise ValueError("motion sequence exceeds the serialized input budget")
        os.replace(input_part, input_path)

        python_executable = _resolve_python_executable()
        worker_path = Path(__file__).with_name("motion_quality_worker.py").resolve(strict=True)
        stderr_path = job_dir / _STDERR_NAME
        stderr_writer = stderr_path.open("xb")
        if os.name != "nt":
            input_path.chmod(0o600)
            stderr_path.chmod(0o600)

        command = [
            str(python_executable),
            "-I",
            "-X",
            "utf8",
            str(worker_path),
            str(job_dir),
        ]
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("PYTHON")
        }
        popen_kwargs: dict[str, Any] = {
            "cwd": str(job_dir),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": stderr_writer,
            "shell": False,
            "close_fds": True,
            "env": environment,
        }
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self._process = subprocess.Popen(command, **popen_kwargs)
        finally:
            # Popen duplicates/inherits the handle.  Closing the parent copy is
            # required before Windows can remove the directory after exit.
            stderr_writer.close()
        self._sequence = None
        self._phase = _PHASE_ANALYZING

    def _read_output_payload(self) -> Any:
        job_dir = self._job_dir
        if job_dir is None:
            raise RuntimeError("motion quality temporary directory is unavailable")
        output_path = job_dir / _OUTPUT_NAME
        if output_path.is_symlink():
            raise ValueError("worker output must not be a symbolic link")
        output_stat = output_path.stat()
        if not stat.S_ISREG(output_stat.st_mode):
            raise ValueError("worker output is not a regular file")
        if output_stat.st_size <= 0 or output_stat.st_size > _MAX_OUTPUT_BYTES:
            raise ValueError("worker output exceeds the output budget")
        if hasattr(os, "getuid") and output_stat.st_uid != os.getuid():
            raise PermissionError("worker output is not owned by the current user")
        with output_path.open("rb") as stream:
            payload = _BuiltinsOnlyUnpickler(stream).load()
            if stream.read(1):
                raise ValueError("worker output contains trailing data")
        return payload

    def _read_worker_stderr(self) -> str:
        job_dir = self._job_dir
        if job_dir is None:
            return ""
        path = job_dir / _STDERR_NAME
        try:
            with path.open("rb") as stream:
                data = stream.read(_MAX_STDERR_BYTES + 1)
        except OSError:
            return ""
        truncated = len(data) > _MAX_STDERR_BYTES
        decoded = data[:_MAX_STDERR_BYTES].decode("utf-8", errors="replace").strip()
        if truncated:
            decoded = f"{decoded}\n[stderr truncated]"
        return decoded

    def _set_failure(
        self,
        message: str,
        *,
        stderr: str = "",
        returncode: int | None = None,
    ) -> MotionQualityProcessError:
        self._close_writer()
        self._sequence = None
        self._phase = _PHASE_FAILED
        self._failure = MotionQualityProcessError(
            message,
            stderr=stderr,
            returncode=returncode,
        )
        # A spawn-boundary failure can leave a successfully created child even
        # when post-Popen stream cleanup raises. Keep the process handle and its
        # private directory intact so cancel() can terminate/reap and retry.
        if self._process is None:
            self._cleanup_temporary()
        return self._failure

    def _close_writer(self) -> None:
        self._pickler = None
        writer = self._writer
        self._writer = None
        if writer is not None:
            writer.close()

    def _cleanup_temporary(self) -> None:
        self._close_writer()
        temporary = self._temporary
        if temporary is None:
            self._job_dir = None
            return
        try:
            temporary.cleanup()
        except OSError:
            # Closed Python file objects can be finalized one GC cycle late on
            # Windows.  Retry once; do not redirect cleanup to a broader path.
            gc.collect()
            temporary.cleanup()
        self._temporary = None
        self._job_dir = None

    def __del__(self) -> None:
        try:
            self.cancel()
        except Exception:
            # Destructors cannot report reliably.  Explicit cancel()/poll()
            # retain the actionable failure path.
            pass


def _encode_input_header(
    sequence: MotionSequence,
    settings: MotionValidationSettings,
) -> dict[str, Any]:
    return {
        "format": _INPUT_FORMAT,
        "sequence": (
            sequence.fps,
            sequence.start_frame,
            sequence.end_frame,
            sequence.source_id,
            sequence.keyed_channel_count,
            sequence.metadata,
            len(sequence.frames),
        ),
        "settings": _encode_settings(settings),
    }


def _encode_settings(settings: MotionValidationSettings) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for descriptor in fields(MotionValidationSettings):
        value = getattr(settings, descriptor.name)
        if descriptor.name == "limbs":
            value = tuple(
                (limb.name, limb.bones, limb.effector, limb.rest_length)
                for limb in value
            )
        result[descriptor.name] = value
    return result


def _decode_settings(payload: Any) -> MotionValidationSettings:
    if type(payload) is not dict:
        raise TypeError("settings payload must be a dictionary")
    expected = {descriptor.name for descriptor in fields(MotionValidationSettings)}
    if set(payload) != expected:
        raise ValueError("settings payload fields do not match the current schema")
    limbs_payload = payload["limbs"]
    if type(limbs_payload) is not tuple:
        raise TypeError("settings limbs payload must be a tuple")
    limbs: list[LimbDefinition] = []
    for item in limbs_payload:
        if type(item) is not tuple or len(item) != 4:
            raise TypeError("settings limb payload is invalid")
        limbs.append(
            LimbDefinition(
                name=item[0],
                bones=item[1],
                effector=item[2],
                rest_length=item[3],
            )
        )
    kwargs = dict(payload)
    kwargs["limbs"] = tuple(limbs)
    return MotionValidationSettings(**kwargs)


def _encode_frame(frame: MotionFrame) -> tuple[Any, ...]:
    return (
        _FRAME_FORMAT,
        frame.frame,
        frame.time_seconds,
        frame.root_location,
        frame.root_rotation_wxyz,
        frame.root_scale,
        tuple(
            (
                bone.name,
                bone.local_rotation_wxyz,
                bone.global_rotation_wxyz,
                bone.head_world,
                bone.tail_world,
                bone.parent,
            )
            for bone in frame.bones
        ),
        tuple(
            (effector.name, effector.position_world, effector.rotation_wxyz)
            for effector in frame.effectors
        ),
        tuple(
            (contact.effector, contact.active, contact.ground_height)
            for contact in frame.contacts
        ),
        frame.key_count,
    )


def _decode_frame(
    payload: Any,
    *,
    max_bone_count: int = _MAX_BONE_COUNT,
    max_effector_count: int = _MAX_EFFECTOR_COUNT,
    max_contact_count: int = _MAX_CONTACT_COUNT,
) -> MotionFrame:
    if type(payload) is not tuple or len(payload) != 10 or payload[0] != _FRAME_FORMAT:
        raise ValueError("frame payload is invalid")
    bones_payload = payload[6]
    effectors_payload = payload[7]
    contacts_payload = payload[8]
    if any(type(value) is not tuple for value in (bones_payload, effectors_payload, contacts_payload)):
        raise TypeError("frame collection payloads must be tuples")
    if len(bones_payload) > max_bone_count:
        raise ValueError("frame payload exceeds the process bone-count budget")
    if len(effectors_payload) > max_effector_count:
        raise ValueError("frame payload exceeds the process effector-count budget")
    if len(contacts_payload) > max_contact_count:
        raise ValueError("frame payload exceeds the process contact-count budget")

    bones: list[BoneSample] = []
    for item in bones_payload:
        if type(item) is not tuple or len(item) != 6:
            raise TypeError("bone payload is invalid")
        bones.append(
            BoneSample(
                name=item[0],
                local_rotation_wxyz=item[1],
                global_rotation_wxyz=item[2],
                head_world=item[3],
                tail_world=item[4],
                parent=item[5],
            )
        )
    effectors: list[EffectorSample] = []
    for item in effectors_payload:
        if type(item) is not tuple or len(item) != 3:
            raise TypeError("effector payload is invalid")
        effectors.append(EffectorSample(item[0], item[1], item[2]))
    contacts: list[ContactSample] = []
    for item in contacts_payload:
        if type(item) is not tuple or len(item) != 3:
            raise TypeError("contact payload is invalid")
        contacts.append(ContactSample(item[0], item[1], item[2]))

    return MotionFrame(
        frame=payload[1],
        time_seconds=payload[2],
        root_location=payload[3],
        root_rotation_wxyz=payload[4],
        root_scale=payload[5],
        bones=tuple(bones),
        effectors=tuple(effectors),
        contacts=tuple(contacts),
        key_count=payload[9],
    )


def _is_sha256_text(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _encode_report(
    report: MotionQualityReport,
    *,
    input_hash: str,
) -> dict[str, Any]:
    if not isinstance(report, MotionQualityReport):
        raise TypeError("analysis output must be a MotionQualityReport")
    if not _is_sha256_text(input_hash):
        raise ValueError("input_hash must be a lowercase SHA-256 digest")
    if len(report.issues) > MAX_MOTION_QUALITY_ISSUES:
        raise ValueError("analysis output exceeds the issue interval limit")
    return {
        "format": _REPORT_FORMAT,
        "input_hash": input_hash,
        "source_hash": report.source_hash,
        "settings_hash": report.settings_hash,
        "summary": report.summary.value,
        "sample_count": report.sample_count,
        "input_finite": report.input_finite,
        "metrics": tuple(
            (
                metric.name,
                metric.unit,
                metric.maximum,
                metric.p95,
                metric.count,
                metric.exceedance_count,
                None
                if metric.frame_range is None
                else (
                    metric.frame_range.start_frame,
                    metric.frame_range.end_frame,
                    metric.frame_range.sample_count,
                ),
            )
            for metric in report.metrics
        ),
        "issues": tuple(
            (
                issue.code,
                issue.metric,
                issue.severity.value,
                issue.subject,
                (
                    issue.interval.start_frame,
                    issue.interval.end_frame,
                    issue.interval.sample_count,
                ),
                issue.peak,
                issue.threshold,
            )
            for issue in report.issues
        ),
        "schema_version": report.schema_version,
    }


def _decode_report(payload: Any) -> MotionQualityReport:
    if type(payload) is not dict:
        raise TypeError("report payload must be a dictionary")
    expected = {
        "format",
        "input_hash",
        "source_hash",
        "settings_hash",
        "summary",
        "sample_count",
        "input_finite",
        "metrics",
        "issues",
        "schema_version",
    }
    if set(payload) != expected or payload.get("format") != _REPORT_FORMAT:
        raise ValueError("report payload fields do not match the current schema")
    if not _is_sha256_text(payload["input_hash"]):
        raise ValueError("report input hash is invalid")
    metrics_payload = payload["metrics"]
    issues_payload = payload["issues"]
    if type(metrics_payload) is not tuple or type(issues_payload) is not tuple:
        raise TypeError("report collection payloads must be tuples")
    if len(metrics_payload) > 64:
        raise ValueError("report contains too many metrics")
    if len(issues_payload) > MAX_MOTION_QUALITY_ISSUES:
        raise ValueError("report exceeds the issue interval limit")

    metrics: list[MetricSummary] = []
    for item in metrics_payload:
        if type(item) is not tuple or len(item) != 7:
            raise TypeError("metric payload is invalid")
        interval_payload = item[6]
        interval = None
        if interval_payload is not None:
            if type(interval_payload) is not tuple or len(interval_payload) != 3:
                raise TypeError("metric interval payload is invalid")
            interval = FrameInterval(*interval_payload)
        metrics.append(
            MetricSummary(
                name=item[0],
                unit=item[1],
                maximum=item[2],
                p95=item[3],
                count=item[4],
                exceedance_count=item[5],
                frame_range=interval,
            )
        )

    issues: list[MotionIssue] = []
    for item in issues_payload:
        if type(item) is not tuple or len(item) != 7:
            raise TypeError("issue payload is invalid")
        interval_payload = item[4]
        if type(interval_payload) is not tuple or len(interval_payload) != 3:
            raise TypeError("issue interval payload is invalid")
        issues.append(
            MotionIssue(
                code=item[0],
                metric=item[1],
                severity=IssueSeverity(item[2]),
                subject=item[3],
                interval=FrameInterval(*interval_payload),
                peak=item[5],
                threshold=item[6],
            )
        )

    return MotionQualityReport(
        source_hash=payload["source_hash"],
        settings_hash=payload["settings_hash"],
        summary=QualitySummary(payload["summary"]),
        sample_count=payload["sample_count"],
        input_finite=payload["input_finite"],
        metrics=tuple(metrics),
        issues=tuple(issues),
        schema_version=payload["schema_version"],
    )


def _resolve_python_executable() -> Path:
    """Resolve Python without PATH lookup, using only sys executable/prefix."""

    executable = Path(sys.executable).expanduser()
    direct = _usable_python_executable(executable)
    if direct is not None:
        return direct

    prefix = Path(sys.prefix).expanduser().resolve(strict=True)
    major = sys.version_info.major
    minor = sys.version_info.minor
    if os.name == "nt":
        relative_candidates = (
            "python.exe",
            "bin/python.exe",
            f"bin/python{major}{minor}.exe",
            f"bin/python{major}.exe",
        )
    else:
        relative_candidates = (
            f"bin/python{major}.{minor}",
            f"bin/python{major}",
            "bin/python3",
            "bin/python",
        )
    for relative in relative_candidates:
        resolved = _usable_python_executable(prefix / relative)
        if resolved is None or not _is_relative_to(resolved, prefix):
            continue
        return resolved
    raise RuntimeError(
        "could not locate Blender's bundled Python from sys.executable/sys.prefix"
    )


def _usable_python_executable(path: Path) -> Path | None:
    name = path.name.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if re.fullmatch(r"pythonw?(?:\d+(?:\.\d+)*)?", name) is None:
        return None
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_file():
        return None
    if os.name != "nt" and not os.access(resolved, os.X_OK):
        return None
    return resolved


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


__all__ = (
    "MotionQualityProcessError",
    "MotionQualityProcessJob",
)
