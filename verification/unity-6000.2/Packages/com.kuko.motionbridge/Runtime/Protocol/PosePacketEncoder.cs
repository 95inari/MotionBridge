using System;
using System.Buffers.Binary;
using UnityEngine;

namespace Kuko.MotionBridge.Protocol
{
    public enum PoseEncodeError
    {
        None,
        InvalidJointCount,
        SourceTooSmall,
        DestinationTooSmall,
        ReservedFlags,
        InvalidPresentMask,
        NonFiniteValue,
        InvalidQuaternion,
        InvalidContactWeight,
        InvalidGroundPlane,
    }

    /// <summary>
    /// Allocation-free view of one pose packet. Spans may point at reusable caller-owned
    /// buffers; JointCount selects the active prefix of JointRotations.
    /// </summary>
    public readonly ref struct PosePacketSource
    {
        public PosePacketSource(
            uint sessionId,
            uint sequence,
            ulong timestampUsec,
            int frameIndex,
            PosePacketFlags flags,
            Vector3 rootPosition,
            Quaternion rootRotation,
            Vector3 hipsLocalPositionDelta,
            int jointCount,
            ulong presentMask,
            ReadOnlySpan<Quaternion> jointRotations,
            ReadOnlySpan<float> contactWeights,
            ReadOnlySpan<uint> contactEpochs,
            ReadOnlySpan<Vector3> contactPositions,
            ReadOnlySpan<Quaternion> contactRotations,
            Vector4 groundPlane)
        {
            SessionId = sessionId;
            Sequence = sequence;
            TimestampUsec = timestampUsec;
            FrameIndex = frameIndex;
            Flags = flags;
            RootPosition = rootPosition;
            RootRotation = rootRotation;
            HipsLocalPositionDelta = hipsLocalPositionDelta;
            JointCount = jointCount;
            PresentMask = presentMask;
            JointRotations = jointRotations;
            ContactWeights = contactWeights;
            ContactEpochs = contactEpochs;
            ContactPositions = contactPositions;
            ContactRotations = contactRotations;
            GroundPlane = groundPlane;
        }

        public uint SessionId { get; }
        public uint Sequence { get; }
        public ulong TimestampUsec { get; }
        public int FrameIndex { get; }
        public PosePacketFlags Flags { get; }
        public Vector3 RootPosition { get; }
        public Quaternion RootRotation { get; }
        public Vector3 HipsLocalPositionDelta { get; }
        public int JointCount { get; }
        public ulong PresentMask { get; }
        public ReadOnlySpan<Quaternion> JointRotations { get; }
        public ReadOnlySpan<float> ContactWeights { get; }
        public ReadOnlySpan<uint> ContactEpochs { get; }
        public ReadOnlySpan<Vector3> ContactPositions { get; }
        public ReadOnlySpan<Quaternion> ContactRotations { get; }
        public Vector4 GroundPlane { get; }
    }

    public static class PosePacketEncoder
    {
        private const int ContactCount = 4;
        private const float QuaternionZeroThreshold = 1e-12f;

        /// <summary>
        /// Encodes into caller-owned storage without allocating. The destination is only
        /// written after the complete source has passed validation.
        /// </summary>
        public static bool TryEncode(
            in PosePacketSource source,
            Span<byte> destination,
            out int bytesWritten,
            out PoseEncodeError error)
        {
            bytesWritten = 0;
            if (!Validate(in source, out error))
                return false;

            var packetBytes = MotionBridgeProtocol.PosePacketBytes(source.JointCount);
            if (destination.Length < packetBytes)
                return Fail(PoseEncodeError.DestinationTooSmall, out error);

            destination[0] = (byte)'M';
            destination[1] = (byte)'B';
            destination[2] = (byte)'P';
            destination[3] = (byte)'1';
            destination[4] = MotionBridgeProtocol.Major;
            destination[5] = MotionBridgeProtocol.Minor;
            destination[6] = MotionBridgeProtocol.PoseMessageType;
            destination[7] = (byte)source.Flags;
            BinaryPrimitives.WriteUInt32LittleEndian(destination.Slice(8, 4), source.SessionId);
            BinaryPrimitives.WriteUInt32LittleEndian(destination.Slice(12, 4), source.Sequence);
            BinaryPrimitives.WriteUInt64LittleEndian(destination.Slice(16, 8), source.TimestampUsec);
            BinaryPrimitives.WriteInt32LittleEndian(destination.Slice(24, 4), source.FrameIndex);
            BinaryPrimitives.WriteUInt16LittleEndian(
                destination.Slice(28, 2),
                (ushort)(MotionBridgeProtocol.FixedPayloadBytes +
                         MotionBridgeProtocol.QuaternionBytes * source.JointCount));
            BinaryPrimitives.WriteUInt16LittleEndian(destination.Slice(30, 2), (ushort)source.JointCount);

            var offset = MotionBridgeProtocol.HeaderBytes;
            WriteVector3(destination, ref offset, source.RootPosition);
            TryNormalizeQuaternion(source.RootRotation, out var rootRotation, out _);
            WriteQuaternion(destination, ref offset, rootRotation);
            WriteVector3(destination, ref offset, source.HipsLocalPositionDelta);
            BinaryPrimitives.WriteUInt64LittleEndian(destination.Slice(offset, 8), source.PresentMask);
            offset += 8;

            for (var i = 0; i < source.JointCount; i++)
            {
                TryNormalizeQuaternion(source.JointRotations[i], out var rotation, out _);
                WriteQuaternion(destination, ref offset, rotation);
            }

            for (var i = 0; i < ContactCount; i++)
                WriteSingle(destination, ref offset, source.ContactWeights[i]);
            for (var i = 0; i < ContactCount; i++)
            {
                BinaryPrimitives.WriteUInt32LittleEndian(
                    destination.Slice(offset, 4), source.ContactEpochs[i]);
                offset += 4;
            }
            for (var i = 0; i < ContactCount; i++)
                WriteVector3(destination, ref offset, source.ContactPositions[i]);
            for (var i = 0; i < ContactCount; i++)
            {
                TryNormalizeQuaternion(source.ContactRotations[i], out var rotation, out _);
                WriteQuaternion(destination, ref offset, rotation);
            }
            WriteVector4(destination, ref offset, source.GroundPlane);

            bytesWritten = packetBytes;
            error = PoseEncodeError.None;
            return true;
        }

        /// <summary>
        /// Convenience overload for reusable PoseFrame instances.
        /// </summary>
        public static bool TryEncode(
            PoseFrame source,
            Span<byte> destination,
            out int bytesWritten,
            out PoseEncodeError error)
        {
            if (source == null)
                throw new ArgumentNullException(nameof(source));

            var view = new PosePacketSource(
                source.SessionId,
                source.Sequence,
                source.TimestampUsec,
                source.FrameIndex,
                source.Flags,
                source.RootPosition,
                source.RootRotation,
                source.HipsLocalPositionDelta,
                source.JointCount,
                source.PresentMask,
                source.JointRotations,
                source.ContactWeights,
                source.ContactEpochs,
                source.ContactPositions,
                source.ContactRotations,
                source.GroundPlane);
            return TryEncode(in view, destination, out bytesWritten, out error);
        }

        private static bool Validate(in PosePacketSource source, out PoseEncodeError error)
        {
            if (source.JointCount < 1 || source.JointCount > MotionBridgeProtocol.MaxPoseJointCount)
                return Fail(PoseEncodeError.InvalidJointCount, out error);
            if (source.JointRotations.Length < source.JointCount ||
                source.ContactWeights.Length < ContactCount ||
                source.ContactEpochs.Length < ContactCount ||
                source.ContactPositions.Length < ContactCount ||
                source.ContactRotations.Length < ContactCount)
                return Fail(PoseEncodeError.SourceTooSmall, out error);
            if ((((byte)source.Flags) & 0xF0) != 0)
                return Fail(PoseEncodeError.ReservedFlags, out error);
            if (!IsFinite(source.RootPosition) || !IsFinite(source.HipsLocalPositionDelta))
                return Fail(PoseEncodeError.NonFiniteValue, out error);
            if (!TryNormalizeQuaternion(source.RootRotation, out _, out error))
                return false;

            var validMask = (1ul << source.JointCount) - 1ul;
            if ((source.PresentMask & ~validMask) != 0)
                return Fail(PoseEncodeError.InvalidPresentMask, out error);

            for (var i = 0; i < source.JointCount; i++)
            {
                if (!TryNormalizeQuaternion(source.JointRotations[i], out var rotation, out error))
                    return false;
                if ((source.PresentMask & (1ul << i)) == 0 && !IsIdentity(rotation))
                    return Fail(PoseEncodeError.InvalidPresentMask, out error);
            }

            for (var i = 0; i < ContactCount; i++)
            {
                var weight = source.ContactWeights[i];
                if (!IsFinite(weight))
                    return Fail(PoseEncodeError.NonFiniteValue, out error);
                if (weight < 0f || weight > 1f)
                    return Fail(PoseEncodeError.InvalidContactWeight, out error);
                if ((source.Flags & PosePacketFlags.Contacts) == 0 && weight != 0f)
                    return Fail(PoseEncodeError.InvalidContactWeight, out error);
                if (!IsFinite(source.ContactPositions[i]))
                    return Fail(PoseEncodeError.NonFiniteValue, out error);
                if (!TryNormalizeQuaternion(source.ContactRotations[i], out _, out error))
                    return false;
            }

            if (!IsFinite(source.GroundPlane))
                return Fail(PoseEncodeError.NonFiniteValue, out error);
            var groundNormalNorm = NormSquared(
                source.GroundPlane.x,
                source.GroundPlane.y,
                source.GroundPlane.z);
            if (groundNormalNorm < MotionBridgeProtocol.MinimumNormalizedNormSquared ||
                groundNormalNorm > MotionBridgeProtocol.MaximumNormalizedNormSquared)
                return Fail(PoseEncodeError.InvalidGroundPlane, out error);

            error = PoseEncodeError.None;
            return true;
        }

        private static bool TryNormalizeQuaternion(
            Quaternion value,
            out Quaternion normalized,
            out PoseEncodeError error)
        {
            normalized = default;
            if (!IsFinite(value.x) || !IsFinite(value.y) ||
                !IsFinite(value.z) || !IsFinite(value.w))
                return Fail(PoseEncodeError.NonFiniteValue, out error);

            var scale = Mathf.Max(
                Mathf.Max(Mathf.Abs(value.x), Mathf.Abs(value.y)),
                Mathf.Max(Mathf.Abs(value.z), Mathf.Abs(value.w)));
            if (scale <= QuaternionZeroThreshold)
                return Fail(PoseEncodeError.InvalidQuaternion, out error);

            var x = value.x / scale;
            var y = value.y / scale;
            var z = value.z / scale;
            var w = value.w / scale;
            var inverseNorm = 1f / Mathf.Sqrt(x * x + y * y + z * z + w * w);
            normalized = new Quaternion(
                x * inverseNorm,
                y * inverseNorm,
                z * inverseNorm,
                w * inverseNorm);
            if (!IsFinite(normalized.x) || !IsFinite(normalized.y) ||
                !IsFinite(normalized.z) || !IsFinite(normalized.w))
                return Fail(PoseEncodeError.NonFiniteValue, out error);
            var wireNorm = NormSquared(
                normalized.x,
                normalized.y,
                normalized.z,
                normalized.w);
            if (wireNorm < MotionBridgeProtocol.MinimumNormalizedNormSquared ||
                wireNorm > MotionBridgeProtocol.MaximumNormalizedNormSquared)
                return Fail(PoseEncodeError.InvalidQuaternion, out error);

            error = PoseEncodeError.None;
            return true;
        }

        private static void WriteVector3(Span<byte> destination, ref int offset, Vector3 value)
        {
            WriteSingle(destination, ref offset, value.x);
            WriteSingle(destination, ref offset, value.y);
            WriteSingle(destination, ref offset, value.z);
        }

        private static void WriteVector4(Span<byte> destination, ref int offset, Vector4 value)
        {
            WriteSingle(destination, ref offset, value.x);
            WriteSingle(destination, ref offset, value.y);
            WriteSingle(destination, ref offset, value.z);
            WriteSingle(destination, ref offset, value.w);
        }

        private static void WriteQuaternion(
            Span<byte> destination,
            ref int offset,
            Quaternion value)
        {
            WriteSingle(destination, ref offset, value.x);
            WriteSingle(destination, ref offset, value.y);
            WriteSingle(destination, ref offset, value.z);
            WriteSingle(destination, ref offset, value.w);
        }

        private static void WriteSingle(Span<byte> destination, ref int offset, float value)
        {
            BinaryPrimitives.WriteInt32LittleEndian(
                destination.Slice(offset, 4), BitConverter.SingleToInt32Bits(value));
            offset += 4;
        }

        private static bool IsFinite(Vector3 value)
        {
            return IsFinite(value.x) && IsFinite(value.y) && IsFinite(value.z);
        }

        private static bool IsFinite(Vector4 value)
        {
            return IsFinite(value.x) && IsFinite(value.y) &&
                   IsFinite(value.z) && IsFinite(value.w);
        }

        private static bool IsFinite(float value)
        {
            return !float.IsNaN(value) && !float.IsInfinity(value);
        }

        private static double NormSquared(float x, float y, float z)
        {
            return (double)x * x + (double)y * y + (double)z * z;
        }

        private static double NormSquared(float x, float y, float z, float w)
        {
            return (double)x * x + (double)y * y + (double)z * z + (double)w * w;
        }

        private static bool IsIdentity(Quaternion value)
        {
            const float tolerance = 0.00001f;
            return Mathf.Abs(value.x) <= tolerance &&
                   Mathf.Abs(value.y) <= tolerance &&
                   Mathf.Abs(value.z) <= tolerance &&
                   Mathf.Abs(Mathf.Abs(value.w) - 1f) <= tolerance;
        }

        private static bool Fail(PoseEncodeError failure, out PoseEncodeError error)
        {
            error = failure;
            return false;
        }
    }
}
