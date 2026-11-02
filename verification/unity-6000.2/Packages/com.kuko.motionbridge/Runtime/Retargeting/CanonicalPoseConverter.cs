using System;
using Unity.Collections;
using UnityEngine;
using Kuko.MotionBridge.Protocol;

namespace Kuko.MotionBridge.Retargeting
{
    public enum CanonicalPoseConversionError
    {
        None,
        JointCountMismatch,
        SourceCapacityTooSmall,
        InvalidPresentMask,
        MissingRequiredJoint,
        NonFiniteValue,
        InvalidQuaternion,
        InvalidSyntheticRoot,
    }

    /// <summary>
    /// Converts Canonical Humanoid v1 global rest-relative joint deltas into an unbound
    /// Source Avatar HumanPose. This class owns and reuses all per-pose buffers.
    /// Call it from the Unity main thread because HumanPoseHandler is a Unity API.
    /// </summary>
    public sealed class CanonicalPoseConverter : IDisposable
    {
        private readonly Avatar _sourceAvatar;
        private readonly HumanPoseHandler _handler;
        private readonly Quaternion[] _poseGlobalRotations;
        private NativeArray<float> _internalAvatarPose;
        private HumanPose _humanPose;
        private bool _hasPose;
        private bool _disposed;

        public CanonicalPoseConverter(Avatar sourceAvatar)
        {
            if (sourceAvatar == null)
                throw new ArgumentNullException(nameof(sourceAvatar));
            if (!sourceAvatar.isValid || !sourceAvatar.isHuman)
                throw new ArgumentException(
                    "Source Avatar must be a valid Humanoid Avatar.",
                    nameof(sourceAvatar));

            _sourceAvatar = sourceAvatar;
            _poseGlobalRotations = new Quaternion[CanonicalHumanoidV1.JointCount];
            _internalAvatarPose = new NativeArray<float>(
                CanonicalHumanoidV1.JointCount * 7,
                Allocator.Persistent,
                NativeArrayOptions.UninitializedMemory);
            _humanPose = new HumanPose
            {
                muscles = new float[HumanTrait.MuscleCount],
            };

            try
            {
                _handler = CanonicalHumanoidV1.CreateUnboundHandler(sourceAvatar);
            }
            catch
            {
                _internalAvatarPose.Dispose();
                throw;
            }
        }

        public Avatar SourceAvatar => _sourceAvatar;

        /// <summary>
        /// The muscle array is reused and remains owned by this converter. Consume it before
        /// the next TryConvert call rather than retaining or mutating it.
        /// </summary>
        public HumanPose HumanPose
        {
            get
            {
                ThrowIfDisposed();
                return _humanPose;
            }
        }

        public Vector3 TrajectoryRootPosition { get; private set; }

        public Quaternion TrajectoryRootRotation { get; private set; } = Quaternion.identity;

        public PosePacketFlags Flags { get; private set; }

        public bool HasPose => _hasPose && !_disposed;

        public bool TryConvert(PoseFrame source, out CanonicalPoseConversionError error)
        {
            ThrowIfDisposed();
            if (source == null)
                throw new ArgumentNullException(nameof(source));
            if (source.JointCount != CanonicalHumanoidV1.JointCount)
                return Fail(CanonicalPoseConversionError.JointCountMismatch, out error);
            if (source.JointRotations.Length < CanonicalHumanoidV1.JointCount)
                return Fail(CanonicalPoseConversionError.SourceCapacityTooSmall, out error);

            var validMask = (1ul << CanonicalHumanoidV1.JointCount) - 1ul;
            if ((source.PresentMask & ~validMask) != 0)
                return Fail(CanonicalPoseConversionError.InvalidPresentMask, out error);
            if ((source.PresentMask & CanonicalHumanoidV1.RequiredJointMask) !=
                CanonicalHumanoidV1.RequiredJointMask)
                return Fail(CanonicalPoseConversionError.MissingRequiredJoint, out error);
            if (!IsFinite(source.RootPosition) || !IsFinite(source.HipsLocalPositionDelta))
                return Fail(CanonicalPoseConversionError.NonFiniteValue, out error);
            if (!TryNormalize(source.RootRotation, out var trajectoryRotation, out error))
                return false;
            if (!TryNormalize(
                    source.JointRotations[(int)CanonicalHumanoidJoint.Root],
                    out var syntheticRootDelta,
                    out error))
                return false;
            if (!IsIdentity(syntheticRootDelta))
                return Fail(CanonicalPoseConversionError.InvalidSyntheticRoot, out error);

            var restGlobalRotations = CanonicalHumanoidV1.RestGlobalRotations;
            _poseGlobalRotations[(int)CanonicalHumanoidJoint.Root] =
                restGlobalRotations[(int)CanonicalHumanoidJoint.Root];
            for (var index = 1; index < CanonicalHumanoidV1.JointCount; index++)
            {
                var delta = Quaternion.identity;
                if ((source.PresentMask & (1ul << index)) != 0 &&
                    !TryNormalize(source.JointRotations[index], out delta, out error))
                    return false;
                _poseGlobalRotations[index] = Quaternion.Normalize(
                    delta * restGlobalRotations[index]);
            }

            var parentIndices = CanonicalHumanoidV1.ParentIndices;
            var restLocalPositions = CanonicalHumanoidV1.RestLocalPositions;
            var restLocalRotations = CanonicalHumanoidV1.RestLocalRotations;
            for (var index = 0; index < CanonicalHumanoidV1.JointCount; index++)
            {
                var position = restLocalPositions[index];
                if (index == (int)CanonicalHumanoidJoint.Hips)
                    position += source.HipsLocalPositionDelta;

                Quaternion rotation;
                if (index == (int)CanonicalHumanoidJoint.Root)
                {
                    rotation = restLocalRotations[index];
                }
                else
                {
                    rotation = Quaternion.Normalize(
                        Quaternion.Inverse(_poseGlobalRotations[parentIndices[index]]) *
                        _poseGlobalRotations[index]);
                }
                WriteInternalPose(index, position, rotation);
            }

            _handler.SetInternalAvatarPose(_internalAvatarPose);
            _handler.GetInternalHumanPose(ref _humanPose);
            var hasWorldRoot = (source.Flags & PosePacketFlags.WorldRoot) != 0;
            TrajectoryRootPosition = hasWorldRoot ? source.RootPosition : Vector3.zero;
            TrajectoryRootRotation = hasWorldRoot ? trajectoryRotation : Quaternion.identity;
            Flags = source.Flags;
            _hasPose = true;
            error = CanonicalPoseConversionError.None;
            return true;
        }

        public void Dispose()
        {
            if (_disposed)
                return;
            _disposed = true;
            try
            {
                _handler.Dispose();
            }
            finally
            {
                if (_internalAvatarPose.IsCreated)
                    _internalAvatarPose.Dispose();
                _hasPose = false;
            }
        }

        private void WriteInternalPose(int index, Vector3 position, Quaternion rotation)
        {
            var offset = index * 7;
            _internalAvatarPose[offset] = position.x;
            _internalAvatarPose[offset + 1] = position.y;
            _internalAvatarPose[offset + 2] = position.z;
            _internalAvatarPose[offset + 3] = rotation.x;
            _internalAvatarPose[offset + 4] = rotation.y;
            _internalAvatarPose[offset + 5] = rotation.z;
            _internalAvatarPose[offset + 6] = rotation.w;
        }

        private static bool TryNormalize(
            Quaternion value,
            out Quaternion normalized,
            out CanonicalPoseConversionError error)
        {
            normalized = default;
            if (!IsFinite(value.x) || !IsFinite(value.y) ||
                !IsFinite(value.z) || !IsFinite(value.w))
                return Fail(CanonicalPoseConversionError.NonFiniteValue, out error);
            var normSquared = (double)value.x * value.x + (double)value.y * value.y +
                              (double)value.z * value.z + (double)value.w * value.w;
            if (normSquared < MotionBridgeProtocol.MinimumNormalizedNormSquared ||
                normSquared > MotionBridgeProtocol.MaximumNormalizedNormSquared)
                return Fail(CanonicalPoseConversionError.InvalidQuaternion, out error);
            normalized = Quaternion.Normalize(value);
            error = CanonicalPoseConversionError.None;
            return true;
        }

        private void ThrowIfDisposed()
        {
            if (_disposed)
                throw new ObjectDisposedException(nameof(CanonicalPoseConverter));
        }

        private static bool IsFinite(Vector3 value)
        {
            return IsFinite(value.x) && IsFinite(value.y) && IsFinite(value.z);
        }

        private static bool IsFinite(float value)
        {
            return !float.IsNaN(value) && !float.IsInfinity(value);
        }

        private static bool IsIdentity(Quaternion value)
        {
            const float tolerance = 0.00001f;
            return Mathf.Abs(value.x) <= tolerance &&
                   Mathf.Abs(value.y) <= tolerance &&
                   Mathf.Abs(value.z) <= tolerance &&
                   Mathf.Abs(Mathf.Abs(value.w) - 1f) <= tolerance;
        }

        private static bool Fail(
            CanonicalPoseConversionError failure,
            out CanonicalPoseConversionError error)
        {
            error = failure;
            return false;
        }
    }
}
