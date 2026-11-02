"""Deterministic, non-destructive sampling of evaluated Canonical motion.

The sampler intentionally owns no Blender data.  It evaluates the active
Scene one integer frame at a time and copies every value into immutable core
types, so callers can run motion-quality analysis after Blender state has been
restored.  :class:`MotionRangeSampler` is chunk-friendly for modal operators;
``sample_motion_range`` is its synchronous convenience wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
from typing import Callable, Final, Iterable

import bpy
from mathutils import Matrix

from ..canonical.rig_spec import JOINTS, SKELETON_ID
from ..live.pose_sampler import validate_canonical_armature
from ..motion_quality import (
    BoneSample,
    EffectorSample,
    MotionFrame,
    MotionSequence,
    validate_motion_sequence,
)
from ..range_spec import RangeSpec
from .action_api import iter_action_fcurves


MAX_MOTION_SAMPLES: Final = 20_000
FOOT_EFFECTOR_NAMES: Final = ("LeftFoot", "RightFoot")
_FRAME_EPSILON: Final = 1.0e-6

FrameCallback = Callable[[int], None]


class MotionSamplingError(RuntimeError):
    """Raised when evaluated motion cannot be copied safely."""


@dataclass(frozen=True, slots=True)
class _TrackState:
    track: object
    mute: bool
    is_solo: bool
    lock: bool
    select: bool | None


@dataclass(frozen=True, slots=True)
class _StripState:
    strip: object
    mute: bool
    select: bool
    influence: float


@dataclass(frozen=True, slots=True)
class _NlaState:
    animation_data: object
    use_nla: bool
    active_track: object | None
    tracks: tuple[_TrackState, ...]
    strips: tuple[_StripState, ...]


@dataclass(frozen=True, slots=True)
class _ContextState:
    frame: int
    subframe: float
    use_preview_range: bool
    preview_start: int
    preview_end: int
    selected_objects: tuple[object, ...]
    active_object: object | None
    active_mode: str
    nla: _NlaState | None


@dataclass(frozen=True, slots=True)
class _KeyMetrics:
    first_frame: int
    counts_by_frame: tuple[int, ...] | None
    keyed_channel_count: int | None

    def count_at(self, frame: int) -> int | None:
        if self.counts_by_frame is None:
            return None
        index = frame - self.first_frame
        return self.counts_by_frame[index] if 0 <= index < len(self.counts_by_frame) else 0


class MotionRangeSampler:
    """Incrementally sample an evaluated Canonical Armature.

    ``begin`` snapshots the current Blender state.  Each ``sample_frame`` call
    consumes exactly one Scene frame, which lets a modal operator yield between
    frames.  ``finalize`` and ``cancel`` always restore the snapshot.  Any
    exception raised while sampling cancels the session before it is re-raised.
    """

    def __init__(
        self,
        context: bpy.types.Context,
        armature: bpy.types.Object,
        *,
        frame_start: int | None = None,
        frame_end: int | None = None,
        range_spec: RangeSpec | None = None,
        max_samples: int = MAX_MOTION_SAMPLES,
        frame_callback: FrameCallback | None = None,
        include_key_density: bool = True,
    ) -> None:
        if range_spec is not None and (frame_start is not None or frame_end is not None):
            raise ValueError("range_spec cannot be combined with frame_start/frame_end")
        if range_spec is not None and not isinstance(range_spec, RangeSpec):
            raise TypeError("range_spec must be a RangeSpec")
        self._context = context
        self._armature = armature
        self._requested_frame_start = frame_start
        self._requested_frame_end = frame_end
        self._range_spec = range_spec
        self._max_samples = max_samples
        self._frame_callback = frame_callback
        self._include_key_density = include_key_density
        self._started_once = False
        self._state: _ContextState | None = None
        self._frame_start: int | None = None
        self._frame_end: int | None = None
        self._fps: float | None = None
        self._next_frame: int | None = None
        self._frames: list[MotionFrame] = []
        self._key_metrics = _KeyMetrics(0, (), 0)
        self._previous_root_rotation: tuple[float, float, float, float] | None = None
        self._previous_local_rotations: dict[
            str, tuple[float, float, float, float]
        ] = {}
        self._previous_global_rotations: dict[
            str, tuple[float, float, float, float]
        ] = {}

    @property
    def is_active(self) -> bool:
        return self._state is not None

    @property
    def is_complete(self) -> bool:
        return (
            self.is_active
            and self._next_frame is not None
            and self._frame_end is not None
            and self._next_frame > self._frame_end
        )

    @property
    def sampled_count(self) -> int:
        return len(self._frames)

    @property
    def sample_count(self) -> int:
        if self._frame_start is None or self._frame_end is None:
            return 0
        return self._frame_end - self._frame_start + 1

    @property
    def progress(self) -> float:
        count = self.sample_count
        return 0.0 if count == 0 else self.sampled_count / count

    @property
    def next_frame(self) -> int | None:
        return self._next_frame if self.is_active and not self.is_complete else None

    def begin(self) -> MotionRangeSampler:
        """Validate inputs and snapshot state without evaluating a frame."""

        _require_main_thread()
        if self._started_once:
            raise RuntimeError("Motion sampler instances can only be started once")
        if not _same_rna(self._context.scene, bpy.context.scene):
            raise ValueError("Motion sampling requires the active Blender Scene")
        if not _same_rna(self._context.view_layer, bpy.context.view_layer):
            raise ValueError("Motion sampling requires the active Blender View Layer")

        _require_available_canonical_armature(
            self._context,
            self._armature,
            "before sampling",
        )
        scene = self._context.scene
        if self._range_spec is not None:
            requested_start, requested_end = self._range_spec.expanded_integer_bounds()
            frame_start = _frame_bound(requested_start, "start")
            frame_end = _frame_bound(requested_end, "end")
        else:
            frame_start = _frame_bound(
                scene.frame_start
                if self._requested_frame_start is None
                else self._requested_frame_start,
                "start",
            )
            frame_end = _frame_bound(
                scene.frame_end
                if self._requested_frame_end is None
                else self._requested_frame_end,
                "end",
            )
        if frame_end < frame_start:
            raise ValueError("Motion sample frame end must not be before frame start")
        max_samples = _positive_integer(self._max_samples, "Maximum sample count")
        sample_count = frame_end - frame_start + 1
        if sample_count > max_samples:
            raise ValueError(
                f"Motion scan has {sample_count} frames; maximum is {max_samples}"
            )
        fps_base = _finite_scalar(scene.render.fps_base, "Scene FPS base")
        if fps_base <= 0.0:
            raise ValueError("Scene FPS and FPS base must be greater than zero")
        fps = _finite_scalar(scene.render.fps, "Scene FPS") / fps_base
        if fps <= 0.0:
            raise ValueError("Scene FPS and FPS base must be greater than zero")

        state = _capture_context_state(self._context, self._armature)
        try:
            key_metrics = (
                _collect_key_metrics(self._armature, frame_start, frame_end)
                if self._include_key_density
                else _KeyMetrics(frame_start, None, None)
            )
        except Exception:
            _restore_context_state(self._context, state)
            raise

        self._frame_start = frame_start
        self._frame_end = frame_end
        self._fps = fps
        self._next_frame = frame_start
        self._key_metrics = key_metrics
        self._state = state
        self._started_once = True
        return self

    def sample_frame(self) -> MotionFrame:
        """Evaluate and copy the next frame, cancelling atomically on failure."""

        _require_main_thread()
        if not self.is_active:
            raise RuntimeError("Motion sampler has not been started")
        if self.is_complete:
            raise RuntimeError("All requested motion frames have already been sampled")
        assert self._next_frame is not None
        frame = self._next_frame
        try:
            _require_available_canonical_armature(
                self._context,
                self._armature,
                f"before Scene frame {frame}",
            )
            scene = self._context.scene
            scene.frame_set(frame, subframe=0.0)
            self._context.view_layer.update()
            _require_available_canonical_armature(
                self._context,
                self._armature,
                f"after Scene frame callback at frame {frame}",
            )
            if self._frame_callback is not None:
                try:
                    self._frame_callback(frame)
                except Exception as error:
                    raise MotionSamplingError(
                        f"Motion frame callback failed at Scene frame {frame}: {error}"
                    ) from error
                self._context.view_layer.update()
                _require_available_canonical_armature(
                    self._context,
                    self._armature,
                    f"after motion frame callback at frame {frame}",
                )
            sample = self._sample_evaluated_frame(frame)
            self._frames.append(sample)
            self._next_frame = frame + 1
            return sample
        except Exception as error:
            try:
                self.cancel()
            except Exception as restore_error:
                raise MotionSamplingError(
                    f"Motion sampling failed at Scene frame {frame}; "
                    f"state restoration also failed: {restore_error}"
                ) from error
            raise

    def restore_frame_for_yield(self) -> None:
        """Return to the captured frame while an incremental caller yields.

        The sampled data and next-frame cursor remain active.  Modal callers
        use this between chunks so user input can never land on a temporary
        evaluation frame.
        """

        _require_main_thread()
        state = self._state
        if state is None:
            raise RuntimeError("Motion sampler has not been started")
        try:
            self._context.scene.frame_set(state.frame, subframe=state.subframe)
            self._context.view_layer.update()
            _require_available_canonical_armature(
                self._context,
                self._armature,
                "while yielding motion sampling",
            )
        except Exception as error:
            try:
                self.cancel()
            except Exception as restore_error:
                raise MotionSamplingError(
                    "Could not restore the captured frame while yielding; "
                    f"full state restoration also failed: {restore_error}"
                ) from error
            raise MotionSamplingError(
                f"Could not restore the captured frame while yielding: {error}"
            ) from error

    def discard_preserving_context(self) -> None:
        """Discard an incremental scan without overwriting later user input.

        This is intended only after :meth:`restore_frame_for_yield`.  Unlike
        :meth:`cancel`, it does not restore selection, mode, NLA state, or the
        frame again, so changes made by the user between modal chunks survive.
        """

        _require_main_thread()
        self._state = None
        self._frames.clear()
        self._next_frame = None

    def finalize(self) -> MotionSequence:
        """Return the immutable sequence and restore Blender state."""

        _require_main_thread()
        if not self.is_active:
            raise RuntimeError("Motion sampler has not been started")
        try:
            if not self.is_complete:
                raise RuntimeError(
                    f"Motion sampling is incomplete: {self.sampled_count}/{self.sample_count}"
                )
            assert self._frame_start is not None
            assert self._frame_end is not None
            assert self._fps is not None
            metadata: list[tuple[str, object]] = [
                ("coordinate_space", "BLENDER_WORLD"),
                ("quaternion_order", "WXYZ"),
                ("bone_count", len(JOINTS) - 1),
                ("effector_count", len(FOOT_EFFECTOR_NAMES)),
            ]
            if self._range_spec is not None:
                metadata.append(("range_source", self._range_spec.source.value))
            sequence = MotionSequence(
                fps=self._fps,
                start_frame=float(self._frame_start),
                end_frame=float(self._frame_end),
                frames=tuple(self._frames),
                source_id=SKELETON_ID,
                keyed_channel_count=self._key_metrics.keyed_channel_count,
                metadata=tuple(metadata),
            )
            validate_motion_sequence(sequence)
        except Exception:
            self.cancel()
            raise

        state = self._state
        self._state = None
        assert state is not None
        try:
            _restore_context_state(self._context, state)
        except Exception as error:
            self._frames.clear()
            raise MotionSamplingError(
                f"Motion was sampled but Blender state could not be restored: {error}"
            ) from error
        return sequence

    def cancel(self) -> None:
        """Discard partial samples and restore Blender state; safe to call twice."""

        _require_main_thread()
        state = self._state
        self._state = None
        self._frames.clear()
        self._next_frame = None
        if state is not None:
            _restore_context_state(self._context, state)

    def _sample_evaluated_frame(self, frame: int) -> MotionFrame:
        assert self._frame_start is not None
        assert self._fps is not None
        try:
            depsgraph = self._context.evaluated_depsgraph_get()
            evaluated = self._armature.evaluated_get(depsgraph)
            world = evaluated.matrix_world.copy()
            root_location = _finite_vector(
                world.translation,
                3,
                f"Root location at Scene frame {frame}",
            )
            root_rotation = _finite_quaternion(
                world.to_quaternion(),
                f"Root rotation at Scene frame {frame}",
                self._previous_root_rotation,
            )
            root_scale = _finite_vector(
                world.to_scale(),
                3,
                f"Root scale at Scene frame {frame}",
            )
            self._previous_root_rotation = root_rotation

            bones: list[BoneSample] = []
            bone_by_name: dict[str, BoneSample] = {}
            for joint in JOINTS[1:]:
                pose_bone = evaluated.pose.bones.get(joint.name)
                if pose_bone is None:
                    raise MotionSamplingError(
                        f"Evaluated Canonical pose is missing bone {joint.name!r} "
                        f"at Scene frame {frame}"
                    )
                parent = None if joint.parent_index == 0 else JOINTS[joint.parent_index].name
                local_matrix = _evaluated_local_matrix(pose_bone)
                global_matrix = world @ pose_bone.matrix
                local_rotation = _finite_quaternion(
                    local_matrix.to_quaternion(),
                    f"{joint.name} local rotation at Scene frame {frame}",
                    self._previous_local_rotations.get(joint.name),
                )
                global_rotation = _finite_quaternion(
                    global_matrix.to_quaternion(),
                    f"{joint.name} global rotation at Scene frame {frame}",
                    self._previous_global_rotations.get(joint.name),
                )
                self._previous_local_rotations[joint.name] = local_rotation
                self._previous_global_rotations[joint.name] = global_rotation
                sample = BoneSample(
                    name=joint.name,
                    local_rotation_wxyz=local_rotation,
                    global_rotation_wxyz=global_rotation,
                    head_world=_finite_vector(
                        world @ pose_bone.head,
                        3,
                        f"{joint.name} head at Scene frame {frame}",
                    ),
                    tail_world=_finite_vector(
                        world @ pose_bone.tail,
                        3,
                        f"{joint.name} tail at Scene frame {frame}",
                    ),
                    parent=parent,
                )
                bones.append(sample)
                bone_by_name[joint.name] = sample

            effectors = tuple(
                EffectorSample(
                    name=name,
                    position_world=bone_by_name[name].tail_world,
                    rotation_wxyz=bone_by_name[name].global_rotation_wxyz,
                )
                for name in FOOT_EFFECTOR_NAMES
            )
            return MotionFrame(
                frame=float(frame),
                time_seconds=(frame - self._frame_start) / self._fps,
                root_location=root_location,
                root_rotation_wxyz=root_rotation,
                root_scale=root_scale,
                bones=tuple(bones),
                effectors=effectors,
                key_count=self._key_metrics.count_at(frame),
            )
        except MotionSamplingError:
            raise
        except (ReferenceError, RuntimeError, TypeError, ValueError) as error:
            raise MotionSamplingError(
                f"Could not evaluate Canonical motion at Scene frame {frame}: {error}"
            ) from error


def sample_motion_range(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    *,
    frame_start: int | None = None,
    frame_end: int | None = None,
    range_spec: RangeSpec | None = None,
    max_samples: int = MAX_MOTION_SAMPLES,
    frame_callback: FrameCallback | None = None,
    include_key_density: bool = True,
) -> MotionSequence:
    """Synchronously sample a Scene range and restore state before returning."""

    sampler = MotionRangeSampler(
        context,
        armature,
        frame_start=frame_start,
        frame_end=frame_end,
        range_spec=range_spec,
        max_samples=max_samples,
        frame_callback=frame_callback,
        include_key_density=include_key_density,
    ).begin()
    while not sampler.is_complete:
        sampler.sample_frame()
    return sampler.finalize()


def _evaluated_local_matrix(pose_bone: bpy.types.PoseBone) -> Matrix:
    """Return final evaluated pose in Blender's rest-relative local space."""

    bone = pose_bone.bone
    parent = pose_bone.parent
    if parent is None:
        return bone.convert_local_to_pose(
            pose_bone.matrix,
            bone.matrix_local,
            invert=True,
        )
    return bone.convert_local_to_pose(
        pose_bone.matrix,
        bone.matrix_local,
        parent_matrix=parent.matrix,
        parent_matrix_local=parent.bone.matrix_local,
        invert=True,
    )


def _collect_key_metrics(
    armature: bpy.types.Object,
    frame_start: int,
    frame_end: int,
) -> _KeyMetrics:
    animation_data = armature.animation_data
    if animation_data is None:
        return _KeyMetrics(frame_start, None, None)

    counts = {frame: 0 for frame in range(frame_start, frame_end + 1)}
    channels: set[tuple[str, int]] = set()
    exact_counts_available = True

    action = animation_data.action
    if action is not None:
        slot = getattr(animation_data, "action_slot", None)
        for curve in iter_action_fcurves(action, slot):
            channels.add((str(curve.data_path), int(curve.array_index)))
            for point in curve.keyframe_points:
                keyed_frame = _integer_frame(float(point.co[0]))
                if keyed_frame is not None and keyed_frame in counts:
                    counts[keyed_frame] += 1

    if bool(getattr(animation_data, "use_nla", True)):
        tracks = tuple(animation_data.nla_tracks)
        soloed = any(bool(track.is_solo) for track in tracks)
        for track in tracks:
            if bool(track.mute) or (soloed and not bool(track.is_solo)):
                continue
            for strip in track.strips:
                if bool(strip.mute) or strip.action is None:
                    continue
                slot = getattr(strip, "action_slot", None)
                curves = tuple(iter_action_fcurves(strip.action, slot))
                channels.update(
                    (str(curve.data_path), int(curve.array_index)) for curve in curves
                )
                if bool(getattr(strip, "use_animated_time", False)):
                    exact_counts_available = False
                    continue
                for curve in curves:
                    for point in curve.keyframe_points:
                        for scene_frame in _strip_scene_frames(strip, float(point.co[0])):
                            keyed_frame = _integer_frame(scene_frame)
                            if keyed_frame is not None and keyed_frame in counts:
                                counts[keyed_frame] += 1

    if not channels or not exact_counts_available:
        return _KeyMetrics(frame_start, None, None)
    return _KeyMetrics(frame_start, tuple(counts.values()), len(channels))


def _strip_scene_frames(
    strip: bpy.types.NlaStrip,
    action_frame: float,
) -> Iterable[float]:
    action_start = float(strip.action_frame_start)
    action_end = float(strip.action_frame_end)
    if not math.isfinite(action_frame) or not (
        action_start - _FRAME_EPSILON
        <= action_frame
        <= action_end + _FRAME_EPSILON
    ):
        return ()
    action_span = action_end - action_start
    scale = abs(float(strip.scale))
    if not math.isfinite(action_span) or not math.isfinite(scale):
        return ()
    if action_span <= _FRAME_EPSILON or scale <= _FRAME_EPSILON:
        return (float(strip.frame_start),)

    cycle_span = action_span * scale
    local = (action_frame - action_start) * scale
    if bool(strip.use_reverse):
        local = cycle_span - local
    strip_start = float(strip.frame_start)
    strip_end = float(strip.frame_end)
    result: list[float] = []
    cycle = 0
    while True:
        scene_frame = strip_start + cycle * cycle_span + local
        if scene_frame > strip_end + _FRAME_EPSILON:
            break
        if scene_frame >= strip_start - _FRAME_EPSILON:
            result.append(scene_frame)
        cycle += 1
    return tuple(result)


def _capture_context_state(
    context: bpy.types.Context,
    armature: bpy.types.Object,
) -> _ContextState:
    scene = context.scene
    active = context.view_layer.objects.active
    active_mode = str(active.mode) if active is not None else "OBJECT"
    return _ContextState(
        frame=int(scene.frame_current),
        subframe=float(scene.frame_subframe),
        use_preview_range=bool(scene.use_preview_range),
        preview_start=int(scene.frame_preview_start),
        preview_end=int(scene.frame_preview_end),
        selected_objects=tuple(context.selected_objects),
        active_object=active,
        active_mode=active_mode,
        nla=_capture_nla_state(armature),
    )


def _capture_nla_state(armature: bpy.types.Object) -> _NlaState | None:
    animation_data = armature.animation_data
    if animation_data is None:
        return None
    tracks: list[_TrackState] = []
    strips: list[_StripState] = []
    for track in animation_data.nla_tracks:
        tracks.append(
            _TrackState(
                track=track,
                mute=bool(track.mute),
                is_solo=bool(track.is_solo),
                lock=bool(track.lock),
                select=bool(track.select) if hasattr(track, "select") else None,
            )
        )
        strips.extend(
            _StripState(
                strip=strip,
                mute=bool(strip.mute),
                select=bool(strip.select),
                influence=float(strip.influence),
            )
            for strip in track.strips
        )
    return _NlaState(
        animation_data,
        bool(animation_data.use_nla),
        animation_data.nla_tracks.active,
        tuple(tracks),
        tuple(strips),
    )


def _restore_context_state(
    context: bpy.types.Context,
    state: _ContextState,
) -> None:
    errors: list[str] = []
    scene = context.scene
    try:
        scene.frame_set(state.frame, subframe=state.subframe)
        context.view_layer.update()
    except Exception as error:
        errors.append(f"frame/subframe ({error})")
    try:
        scene.use_preview_range = state.use_preview_range
        scene.frame_preview_start = state.preview_start
        scene.frame_preview_end = state.preview_end
    except Exception as error:
        errors.append(f"Preview Range ({error})")
    try:
        _restore_nla_state(state.nla)
    except Exception as error:
        errors.append(f"NLA ({error})")
    try:
        _restore_selection_and_mode(context, state)
    except Exception as error:
        errors.append(f"selection/mode ({error})")
    if errors:
        raise MotionSamplingError("Could not restore " + ", ".join(errors))


def _restore_nla_state(state: _NlaState | None) -> None:
    if state is None:
        return
    try:
        state.animation_data.use_nla = state.use_nla  # type: ignore[attr-defined]
    except ReferenceError:
        pass
    for item in state.tracks:
        try:
            item.track.mute = item.mute
            item.track.is_solo = item.is_solo
            item.track.lock = item.lock
            if item.select is not None and hasattr(item.track, "select"):
                item.track.select = item.select
        except ReferenceError:
            continue
    if state.active_track is not None:
        try:
            state.animation_data.nla_tracks.active = state.active_track  # type: ignore[attr-defined]
        except ReferenceError:
            pass
    for item in state.strips:
        try:
            item.strip.mute = item.mute
            item.strip.select = item.select
            item.strip.influence = item.influence
        except ReferenceError:
            continue


def _restore_selection_and_mode(
    context: bpy.types.Context,
    state: _ContextState,
) -> None:
    view_layer = context.view_layer
    current_active = view_layer.objects.active
    if current_active is not None and str(current_active.mode) != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    for obj in tuple(view_layer.objects):
        obj.select_set(False, view_layer=view_layer)
    selected_pointers = {
        _safe_pointer(obj) for obj in state.selected_objects if _safe_pointer(obj) is not None
    }
    for obj in tuple(view_layer.objects):
        if _safe_pointer(obj) in selected_pointers:
            obj.select_set(True, view_layer=view_layer)

    active = state.active_object
    if active is not None and _object_in_view_layer(view_layer, active):
        view_layer.objects.active = active
        if state.active_mode != "OBJECT":
            bpy.ops.object.mode_set(mode=state.active_mode)
    else:
        view_layer.objects.active = None


def _require_available_canonical_armature(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    phase: str,
) -> None:
    try:
        if not _object_in_view_layer(context.view_layer, armature):
            raise ValueError("object is no longer in the active View Layer")
        validate_canonical_armature(armature)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as error:
        raise MotionSamplingError(
            f"Canonical Armature became unavailable or invalid {phase}: {error}"
        ) from error


def _object_in_view_layer(
    view_layer: bpy.types.ViewLayer,
    obj: bpy.types.Object,
) -> bool:
    pointer = _safe_pointer(obj)
    if pointer is None:
        return False
    return any(_safe_pointer(candidate) == pointer for candidate in view_layer.objects)


def _safe_pointer(value: object) -> int | None:
    try:
        pointer = int(value.as_pointer())  # type: ignore[attr-defined]
    except (AttributeError, ReferenceError, TypeError):
        return None
    return pointer or None


def _same_rna(left: object, right: object) -> bool:
    left_pointer = _safe_pointer(left)
    return left_pointer is not None and left_pointer == _safe_pointer(right)


def _frame_bound(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"Motion sample frame {label} must be an integer")
    return value


def _positive_integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _finite_scalar(value: object, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{label} must be finite") from error
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return 0.0 if result == 0.0 else result


def _finite_vector(value: object, size: int, label: str) -> tuple[float, ...]:
    try:
        components = tuple(_finite_scalar(component, label) for component in value)  # type: ignore[union-attr]
    except TypeError as error:
        raise MotionSamplingError(f"{label} must contain {size} finite values") from error
    if len(components) != size:
        raise MotionSamplingError(f"{label} must contain {size} finite values")
    return components


def _finite_quaternion(
    value: object,
    label: str,
    previous: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float]:
    components = _finite_vector(value, 4, label)
    norm_squared = sum(component * component for component in components)
    if norm_squared <= 1.0e-16:
        raise MotionSamplingError(f"{label} must not be a zero quaternion")
    inverse_norm = 1.0 / math.sqrt(norm_squared)
    result = tuple(component * inverse_norm for component in components)
    if previous is not None:
        if sum(left * right for left, right in zip(result, previous, strict=True)) < 0.0:
            result = tuple(-component for component in result)
    else:
        for component in result:
            if abs(component) <= 1.0e-15:
                continue
            if component < 0.0:
                result = tuple(-item for item in result)
            break
    return tuple(0.0 if component == 0.0 else component for component in result)  # type: ignore[return-value]


def _integer_frame(value: float) -> int | None:
    if not math.isfinite(value):
        return None
    rounded = round(value)
    return int(rounded) if abs(value - rounded) <= _FRAME_EPSILON else None


def _require_main_thread() -> None:
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Motion sampling must run on Blender's main thread")


__all__ = (
    "FOOT_EFFECTOR_NAMES",
    "MAX_MOTION_SAMPLES",
    "FrameCallback",
    "MotionRangeSampler",
    "MotionSamplingError",
    "sample_motion_range",
)
