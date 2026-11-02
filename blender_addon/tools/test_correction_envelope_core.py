from __future__ import annotations

import bisect
import json
import math
from pathlib import Path
import sys

from mathutils import Quaternion, Vector


ADDON_ROOT = Path(__file__).resolve().parents[1]
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from motion_bridge.editing.correction_envelope import (  # noqa: E402
    adaptive_envelope_samples,
    weighted_quaternion_delta,
)


ROTATION_EPSILON_DEGREES = 0.101


def _normalized_lerp(
    left: Quaternion,
    right: Quaternion,
    factor: float,
) -> Quaternion:
    right_values = tuple(float(value) for value in right)
    if left.dot(right) < 0.0:
        right_values = tuple(-value for value in right_values)
    values = tuple(
        ((1.0 - factor) * float(start)) + (factor * end)
        for start, end in zip(left, right_values, strict=True)
    )
    norm = math.hypot(*values)
    return Quaternion(tuple(value / norm for value in values))


def _interpolated_sample_rotation(
    samples: tuple[tuple[float, float], ...],
    delta: Quaternion,
    time: float,
) -> Quaternion:
    times = tuple(sample[0] for sample in samples)
    right_index = bisect.bisect_right(times, time)
    if right_index <= 0:
        return weighted_quaternion_delta(delta, samples[0][1])
    if right_index >= len(samples):
        return weighted_quaternion_delta(delta, samples[-1][1])
    left_time, left_weight = samples[right_index - 1]
    right_time, right_weight = samples[right_index]
    factor = (time - left_time) / (right_time - left_time)
    return _normalized_lerp(
        weighted_quaternion_delta(delta, left_weight),
        weighted_quaternion_delta(delta, right_weight),
        factor,
    )


def _test_rotation_aware_adaptive_sampling() -> int:
    delta = Quaternion(
        Vector((0.25, 0.8, -0.35)).normalized(),
        math.radians(170.0),
    ).normalized()
    samples = adaptive_envelope_samples(
        lambda time: time,
        rotation_deltas=(delta,),
        tolerance=0.002,
        rotation_tolerance_degrees=0.1,
    )
    if len(samples) <= 2:
        raise AssertionError(
            "Linear weight incorrectly used one component-linear Quaternion segment"
        )
    for index in range(1001):
        time = index / 1000.0
        expected = weighted_quaternion_delta(delta, time)
        approximated = _interpolated_sample_rotation(samples, delta, time)
        error = math.degrees(expected.rotation_difference(approximated).angle)
        if error > ROTATION_EPSILON_DEGREES:
            raise AssertionError(
                f"Adaptive Quaternion error {error:g}° exceeds tolerance at {time:g}"
            )
    return len(samples)


def _test_curve_control_point_seeds() -> int:
    def narrow_spike(time: float) -> float:
        return max(0.0, 1.0 - (abs(time - 0.503) / 0.003))

    # The broad 0/.25/.5/.75/1 probes all miss this narrow peak.  CurveMapping
    # control-point x values are therefore mandatory adaptive interval seeds.
    narrow_spike.sample_times = (0.5, 0.503, 0.506)  # type: ignore[attr-defined]
    samples = adaptive_envelope_samples(narrow_spike, tolerance=0.002)
    sample_times = {time for time, _weight in samples}
    for required in narrow_spike.sample_times:  # type: ignore[attr-defined]
        if required not in sample_times:
            raise AssertionError(f"Adaptive samples omitted curve control point {required:g}")
    peak = dict(samples)[0.503]
    if abs(peak - 1.0) > 1.0e-9:
        raise AssertionError("Adaptive seed did not retain the narrow CurveMapping peak")
    return len(samples)


def _test_adaptive_limits_are_explicit() -> None:
    for arguments, label in (
        ({"max_samples": 2}, "sample limit"),
        ({"max_depth": 1}, "depth limit"),
    ):
        try:
            adaptive_envelope_samples(
                lambda time: time * time * time,
                tolerance=1.0e-8,
                **arguments,
            )
        except ValueError:
            continue
        raise AssertionError(f"Adaptive {label} silently exceeded its tolerance")


def main() -> None:
    rotation_samples = _test_rotation_aware_adaptive_sampling()
    spike_samples = _test_curve_control_point_seeds()
    _test_adaptive_limits_are_explicit()
    print(
        "MOTION_BRIDGE_CORRECTION_ENVELOPE_CORE_TEST="
        + json.dumps(
            {
                "status": "PASS",
                "rotationAwareAdaptive": True,
                "rotationSamples": rotation_samples,
                "curvePointSeeds": True,
                "adaptiveLimitsExplicit": True,
                "spikeSamples": spike_samples,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
