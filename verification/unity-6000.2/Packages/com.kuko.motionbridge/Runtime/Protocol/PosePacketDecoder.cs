using System;
using System.Buffers.Binary;
using UnityEngine;

namespace Kuko.MotionBridge.Protocol
{
    public enum PoseDecodeError
    {
        None,
        TooShort,
        BadMagic,
        UnsupportedVersion,
        UnsupportedMessageType,
        ReservedFlags,
        InvalidLength,
        InvalidJointCount,
        DestinationTooSmall,
        InvalidPresentMask,
        NonFiniteValue,
        InvalidQuaternion,
        InvalidContactWeight,
    }

    public static class PosePacketDecoder
    {
        /// <summary>
        /// Decodes into a caller-owned unpublished/scratch frame. A failed decode may leave
        /// destination arrays partially written, so the caller must publish the frame only
        /// after this method and the session validator both succeed.
        /// </summary>
        public static bool TryDecode(
            ReadOnlySpan<byte> packet,
            PoseFrame destination,
            out PoseDecodeError error)
        {
            if (destination == null)
                throw new ArgumentNullException(nameof(destination));
            if (packet.Length < MotionBridgeProtocol.HeaderBytes)
                return Fail(PoseDecodeError.TooShort, out error);
            if (packet[0] != (byte)'M' || packet[1] != (byte)'B' ||
                packet[2] != (byte)'P' || packet[3] != (byte)'1')
                return Fail(PoseDecodeError.BadMagic, out error);
            if (packet[4] != MotionBridgeProtocol.Major ||
                packet[5] > MotionBridgeProtocol.Minor)
                return Fail(PoseDecodeError.UnsupportedVersion, out error);
            if (packet[6] != MotionBridgeProtocol.PoseMessageType)
                return Fail(PoseDecodeError.UnsupportedMessageType, out error);

            var rawFlags = packet[7];
            if ((rawFlags & 0xF0) != 0)
                return Fail(PoseDecodeError.ReservedFlags, out error);

            var payloadBytes = BinaryPrimitives.ReadUInt16LittleEndian(packet.Slice(28, 2));
            var jointCount = BinaryPrimitives.ReadUInt16LittleEndian(packet.Slice(30, 2));
            if (jointCount < 1 || jointCount > MotionBridgeProtocol.MaxPoseJointCount)
                return Fail(PoseDecodeError.InvalidJointCount, out error);
            if (destination.JointRotations.Length < jointCount)
                return Fail(PoseDecodeError.DestinationTooSmall, out error);

            var expectedPayload = MotionBridgeProtocol.FixedPayloadBytes +
                                  MotionBridgeProtocol.QuaternionBytes * jointCount;
            if (payloadBytes != expectedPayload ||
                packet.Length != MotionBridgeProtocol.HeaderBytes + expectedPayload ||
                packet.Length > MotionBridgeProtocol.MaxPacketBytes)
                return Fail(PoseDecodeError.InvalidLength, out error);

            var offset = MotionBridgeProtocol.HeaderBytes;
            if (!TryReadVector3(packet, ref offset, out var rootPosition))
                return Fail(PoseDecodeError.NonFiniteValue, out error);
            if (!TryReadQuaternion(packet, ref offset, out var rootRotation, out error))
                return false;
            if (!TryReadVector3(packet, ref offset, out var hipsLocalPositionDelta))
                return Fail(PoseDecodeError.NonFiniteValue, out error);

            var presentMask = BinaryPrimitives.ReadUInt64LittleEndian(packet.Slice(offset, 8));
            offset += 8;
            var validMask = jointCount == 64 ? ulong.MaxValue : (1ul << jointCount) - 1ul;
            if ((presentMask & ~validMask) != 0)
                return Fail(PoseDecodeError.InvalidPresentMask, out error);

            for (var i = 0; i < jointCount; i++)
            {
                if (!TryReadQuaternion(packet, ref offset, out var rotation, out error))
                    return false;
                if ((presentMask & (1ul << i)) == 0 && !IsIdentity(rotation))
                    return Fail(PoseDecodeError.InvalidPresentMask, out error);
                destination.JointRotations[i] = rotation;
            }

            for (var i = 0; i < 4; i++)
            {
                var weight = ReadSingle(packet, ref offset);
                if (!IsFinite(weight))
                    return Fail(PoseDecodeError.NonFiniteValue, out error);
                if (weight < 0f || weight > 1f)
                    return Fail(PoseDecodeError.InvalidContactWeight, out error);
                destination.ContactWeights[i] = weight;
            }
            if ((rawFlags & (byte)PosePacketFlags.Contacts) == 0 &&
                (destination.ContactWeights[0] != 0f ||
                 destination.ContactWeights[1] != 0f ||
                 destination.ContactWeights[2] != 0f ||
                 destination.ContactWeights[3] != 0f))
                return Fail(PoseDecodeError.InvalidContactWeight, out error);

            for (var i = 0; i < 4; i++)
            {
                destination.ContactEpochs[i] =
                    BinaryPrimitives.ReadUInt32LittleEndian(packet.Slice(offset, 4));
                offset += 4;
            }

            for (var i = 0; i < 4; i++)
            {
                if (!TryReadVector3(packet, ref offset, out var position))
                    return Fail(PoseDecodeError.NonFiniteValue, out error);
                destination.ContactPositions[i] = position;
            }

            for (var i = 0; i < 4; i++)
            {
                if (!TryReadQuaternion(packet, ref offset, out var rotation, out error))
                    return false;
                destination.ContactRotations[i] = rotation;
            }

            var groundPlane = new Vector4(
                ReadSingle(packet, ref offset),
                ReadSingle(packet, ref offset),
                ReadSingle(packet, ref offset),
                ReadSingle(packet, ref offset));
            if (!IsFinite(groundPlane.x) || !IsFinite(groundPlane.y) ||
                !IsFinite(groundPlane.z) || !IsFinite(groundPlane.w))
                return Fail(PoseDecodeError.NonFiniteValue, out error);
            var groundNormalNorm = NormSquared(
                groundPlane.x,
                groundPlane.y,
                groundPlane.z);
            if (groundNormalNorm < MotionBridgeProtocol.MinimumNormalizedNormSquared ||
                groundNormalNorm > MotionBridgeProtocol.MaximumNormalizedNormSquared)
                return Fail(PoseDecodeError.NonFiniteValue, out error);

            destination.SessionId = BinaryPrimitives.ReadUInt32LittleEndian(packet.Slice(8, 4));
            destination.Sequence = BinaryPrimitives.ReadUInt32LittleEndian(packet.Slice(12, 4));
            destination.TimestampUsec = BinaryPrimitives.ReadUInt64LittleEndian(packet.Slice(16, 8));
            destination.FrameIndex = BinaryPrimitives.ReadInt32LittleEndian(packet.Slice(24, 4));
            destination.Flags = (PosePacketFlags)rawFlags;
            destination.JointCount = jointCount;
            destination.PresentMask = presentMask;
            destination.RootPosition = rootPosition;
            destination.RootRotation = rootRotation;
            destination.HipsLocalPositionDelta = hipsLocalPositionDelta;
            destination.GroundPlane = groundPlane;
            error = PoseDecodeError.None;
            return true;
        }

        private static bool TryReadVector3(
            ReadOnlySpan<byte> packet,
            ref int offset,
            out Vector3 value)
        {
            value = new Vector3(
                ReadSingle(packet, ref offset),
                ReadSingle(packet, ref offset),
                ReadSingle(packet, ref offset));
            return IsFinite(value.x) && IsFinite(value.y) && IsFinite(value.z);
        }

        private static bool TryReadQuaternion(
            ReadOnlySpan<byte> packet,
            ref int offset,
            out Quaternion value,
            out PoseDecodeError error)
        {
            value = new Quaternion(
                ReadSingle(packet, ref offset),
                ReadSingle(packet, ref offset),
                ReadSingle(packet, ref offset),
                ReadSingle(packet, ref offset));
            if (!IsFinite(value.x) || !IsFinite(value.y) ||
                !IsFinite(value.z) || !IsFinite(value.w))
                return Fail(PoseDecodeError.NonFiniteValue, out error);

            var norm = NormSquared(value.x, value.y, value.z, value.w);
            if (norm < MotionBridgeProtocol.MinimumNormalizedNormSquared ||
                norm > MotionBridgeProtocol.MaximumNormalizedNormSquared)
                return Fail(PoseDecodeError.InvalidQuaternion, out error);

            value = Quaternion.Normalize(value);
            error = PoseDecodeError.None;
            return true;
        }

        private static float ReadSingle(ReadOnlySpan<byte> packet, ref int offset)
        {
            var bits = BinaryPrimitives.ReadInt32LittleEndian(packet.Slice(offset, 4));
            offset += 4;
            return BitConverter.Int32BitsToSingle(bits);
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

        private static bool Fail(PoseDecodeError failure, out PoseDecodeError error)
        {
            error = failure;
            return false;
        }
    }
}
