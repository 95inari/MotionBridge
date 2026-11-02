"""Private stand-alone worker for :mod:`motion_quality_process`.

This module intentionally has no Blender dependency.  It accepts exactly one
private job-directory path and derives all filenames within that directory.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import pickle
import stat
import sys
import traceback
from typing import BinaryIO


if __package__:
    from .motion_quality import MotionFrame, MotionSequence, analyze_motion
    from .motion_quality_process import (
        _END_FORMAT,
        _INPUT_FORMAT,
        _INPUT_NAME,
        _MAX_BONE_COUNT,
        _MAX_CONTACT_COUNT,
        _MAX_EFFECTOR_COUNT,
        _MAX_FRAME_COUNT,
        _MAX_INPUT_BYTES,
        _MAX_OUTPUT_BYTES,
        _MAX_TOTAL_BONE_SAMPLES,
        _MAX_TOTAL_CONTACT_SAMPLES,
        _OUTPUT_NAME,
        _BuiltinsOnlyUnpickler,
        _decode_frame,
        _decode_settings,
        _encode_report,
    )
else:
    # ``-I`` deliberately removes the script directory from sys.path.  Add the
    # package's on-disk parent, never cwd or an environment-provided path.
    package_parent = Path(__file__).resolve(strict=True).parent.parent
    sys.path.insert(0, str(package_parent))
    from motion_bridge.motion_quality import MotionFrame, MotionSequence, analyze_motion
    from motion_bridge.motion_quality_process import (
        _END_FORMAT,
        _INPUT_FORMAT,
        _INPUT_NAME,
        _MAX_BONE_COUNT,
        _MAX_CONTACT_COUNT,
        _MAX_EFFECTOR_COUNT,
        _MAX_FRAME_COUNT,
        _MAX_INPUT_BYTES,
        _MAX_OUTPUT_BYTES,
        _MAX_TOTAL_BONE_SAMPLES,
        _MAX_TOTAL_CONTACT_SAMPLES,
        _OUTPUT_NAME,
        _BuiltinsOnlyUnpickler,
        _decode_frame,
        _decode_settings,
        _encode_report,
    )


def _private_job_directory(value: str) -> Path:
    raw = Path(value)
    if not raw.is_absolute() or raw.is_symlink():
        raise ValueError("job directory must be an absolute non-symlink path")
    path = raw.resolve(strict=True)
    metadata = path.stat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("job directory is not a directory")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise PermissionError("job directory is not owned by the current user")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PermissionError("job directory must not be accessible by group or other")
    return path


def _open_private_input(path: Path) -> BinaryIO:
    if path.is_symlink():
        raise ValueError("job input must not be a symbolic link")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("job input is not a regular file")
    if metadata.st_size <= 0 or metadata.st_size > _MAX_INPUT_BYTES:
        raise ValueError("job input exceeds the serialized input budget")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise PermissionError("job input is not owned by the current user")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PermissionError("job input must not be accessible by group or other")
    return path.open("rb")


def _input_sha256(stream: BinaryIO) -> str:
    hasher = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        hasher.update(chunk)
    stream.seek(0)
    return hasher.hexdigest()


def _load_job(path: Path) -> tuple[MotionSequence, object, str]:
    input_path = path / _INPUT_NAME
    with _open_private_input(input_path) as stream:
        # Digest and decode through the same open file description, so a path
        # replacement cannot make the report identify different input bytes.
        input_hash = _input_sha256(stream)
        unpickler = _BuiltinsOnlyUnpickler(stream)
        header = unpickler.load()
        unpickler.memo.clear()
        if type(header) is not dict or set(header) != {"format", "sequence", "settings"}:
            raise ValueError("job input header is invalid")
        if header["format"] != _INPUT_FORMAT:
            raise ValueError("job input format is unsupported")
        sequence_payload = header["sequence"]
        if type(sequence_payload) is not tuple or len(sequence_payload) != 7:
            raise ValueError("job sequence header is invalid")
        settings = _decode_settings(header["settings"])

        frame_count = sequence_payload[6]
        if type(frame_count) is not int or frame_count <= 0:
            raise ValueError("job frame count must be a positive integer")
        if frame_count > min(settings.max_frame_count, _MAX_FRAME_COUNT):
            raise ValueError("job input exceeds the process frame budget")

        frames: list[MotionFrame] = []
        total_bone_samples = 0
        total_contact_samples = 0
        maximum_bones = min(settings.max_bone_count, _MAX_BONE_COUNT)
        maximum_effectors = min(
            settings.max_effector_count,
            _MAX_EFFECTOR_COUNT,
        )
        maximum_contacts = min(
            settings.max_bone_count + settings.max_effector_count,
            _MAX_CONTACT_COUNT,
        )
        for _index in range(frame_count):
            frame_payload = unpickler.load()
            unpickler.memo.clear()
            frame = _decode_frame(
                frame_payload,
                max_bone_count=maximum_bones,
                max_effector_count=maximum_effectors,
                max_contact_count=maximum_contacts,
            )
            total_bone_samples += len(frame.bones)
            if total_bone_samples > min(
                settings.max_total_bone_samples,
                _MAX_TOTAL_BONE_SAMPLES,
            ):
                raise ValueError("job input exceeds the process bone-sample budget")
            total_contact_samples += len(frame.contacts)
            if total_contact_samples > _MAX_TOTAL_CONTACT_SAMPLES:
                raise ValueError("job input exceeds the process contact-sample budget")
            frames.append(frame)

        trailer = unpickler.load()
        unpickler.memo.clear()
        if trailer != (_END_FORMAT, frame_count):
            raise ValueError("job input trailer is invalid")
        if stream.read(1):
            raise ValueError("job input contains trailing data")

    sequence = MotionSequence(
        fps=sequence_payload[0],
        start_frame=sequence_payload[1],
        end_frame=sequence_payload[2],
        source_id=sequence_payload[3],
        keyed_channel_count=sequence_payload[4],
        metadata=sequence_payload[5],
        frames=tuple(frames),
    )
    return sequence, settings, input_hash


def _write_report(path: Path, payload: object) -> None:
    output = path / _OUTPUT_NAME
    partial = path / f"{_OUTPUT_NAME}.part"
    with partial.open("xb") as stream:
        if os.name != "nt":
            partial.chmod(0o600)
        pickle.dump(payload, stream, protocol=pickle.HIGHEST_PROTOCOL)
        stream.flush()
    if partial.stat().st_size > _MAX_OUTPUT_BYTES:
        partial.unlink(missing_ok=True)
        raise ValueError("analysis output exceeds the output budget")
    os.replace(partial, output)


def main(arguments: list[str] | None = None) -> int:
    values = sys.argv[1:] if arguments is None else arguments
    if len(values) != 1:
        print("worker requires exactly one private job directory", file=sys.stderr)
        return 2
    try:
        job_dir = _private_job_directory(values[0])
        sequence, settings, input_hash = _load_job(job_dir)
        report = analyze_motion(sequence, settings)
        _write_report(job_dir, _encode_report(report, input_hash=input_hash))
    except Exception:
        traceback.print_exc(limit=8, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
