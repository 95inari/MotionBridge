using System;
using System.Buffers.Binary;
using System.IO;
using Kuko.MotionBridge.Protocol;
using NUnit.Framework;

namespace Kuko.MotionBridge.Tests
{
    public sealed class PosePacketDecoderTests
    {
        [Test]
        public void DecodesIdentityPacket()
        {
            var packet = BuildIdentityPacket(2);
            var frame = new PoseFrame();

            Assert.That(PosePacketDecoder.TryDecode(packet, frame, out var error), Is.True);
            Assert.That(error, Is.EqualTo(PoseDecodeError.None));
            Assert.That(frame.SessionId, Is.EqualTo(0x12345678u));
            Assert.That(frame.Sequence, Is.EqualTo(7u));
            Assert.That(frame.FrameIndex, Is.EqualTo(42));
            Assert.That(frame.JointCount, Is.EqualTo(2));
            Assert.That(frame.RootRotation.w, Is.EqualTo(1f));
        }

        [Test]
        public void RejectsTruncatedPacket()
        {
            var packet = BuildIdentityPacket(2);
            var frame = new PoseFrame();

            Assert.That(
                PosePacketDecoder.TryDecode(packet.AsSpan(0, packet.Length - 1), frame, out var error),
                Is.False);
            Assert.That(error, Is.EqualTo(PoseDecodeError.InvalidLength));
        }

        [Test]
        public void RejectsZeroJointQuaternion()
        {
            var packet = BuildIdentityPacket(2);
            WriteFloat(packet, 92, 0f);
            var frame = new PoseFrame();

            Assert.That(PosePacketDecoder.TryDecode(packet, frame, out var error), Is.False);
            Assert.That(error, Is.EqualTo(PoseDecodeError.InvalidQuaternion));
        }

        [Test]
        public void RejectsReservedFlag()
        {
            var packet = BuildIdentityPacket(2);
            packet[7] = 0x10;
            var frame = new PoseFrame();

            Assert.That(PosePacketDecoder.TryDecode(packet, frame, out var error), Is.False);
            Assert.That(error, Is.EqualTo(PoseDecodeError.ReservedFlags));
        }

        [Test]
        public void RejectsPresentMaskOutsideJointCount()
        {
            var packet = BuildIdentityPacket(2);
            BinaryPrimitives.WriteUInt64LittleEndian(packet.AsSpan(72, 8), 0b100ul);
            var frame = new PoseFrame();

            Assert.That(PosePacketDecoder.TryDecode(packet, frame, out var error), Is.False);
            Assert.That(error, Is.EqualTo(PoseDecodeError.InvalidPresentMask));
        }

        [Test]
        public void RejectsPoseThatCannotFitSafeUdpBudget()
        {
            const ushort jointCount = 61;
            var packet = new byte[MotionBridgeProtocol.PosePacketBytes(jointCount)];
            packet[0] = (byte)'M';
            packet[1] = (byte)'B';
            packet[2] = (byte)'P';
            packet[3] = (byte)'1';
            packet[4] = MotionBridgeProtocol.Major;
            packet[5] = MotionBridgeProtocol.Minor;
            packet[6] = MotionBridgeProtocol.PoseMessageType;
            BinaryPrimitives.WriteUInt16LittleEndian(packet.AsSpan(30, 2), jointCount);

            Assert.That(PosePacketDecoder.TryDecode(packet, new PoseFrame(), out var error), Is.False);
            Assert.That(error, Is.EqualTo(PoseDecodeError.InvalidJointCount));
        }

        [Test]
        public void RejectsContactWeightWhenContactsFlagIsUnset()
        {
            var packet = BuildIdentityPacket(1);
            WriteFloat(packet, 96, 1f);

            Assert.That(PosePacketDecoder.TryDecode(packet, new PoseFrame(), out var error), Is.False);
            Assert.That(error, Is.EqualTo(PoseDecodeError.InvalidContactWeight));
        }

        [Test]
        public void DecodesHipsAndStatelessContactState()
        {
            var packet = BuildIdentityPacket(1);
            packet[7] = (byte)PosePacketFlags.Contacts;
            WriteFloat(packet, 60, 1f);
            WriteFloat(packet, 64, 2f);
            WriteFloat(packet, 68, 3f);
            WriteFloat(packet, 96, 0.75f);
            BinaryPrimitives.WriteUInt32LittleEndian(packet.AsSpan(112, 4), 17u);
            WriteFloat(packet, 128, 4f);
            WriteFloat(packet, 132, 5f);
            WriteFloat(packet, 136, 6f);

            var frame = Decode(packet);
            Assert.That(frame.HipsLocalPositionDelta.x, Is.EqualTo(1f));
            Assert.That(frame.HipsLocalPositionDelta.y, Is.EqualTo(2f));
            Assert.That(frame.HipsLocalPositionDelta.z, Is.EqualTo(3f));
            Assert.That(frame.ContactWeights[0], Is.EqualTo(0.75f));
            Assert.That(frame.ContactEpochs[0], Is.EqualTo(17u));
            Assert.That(frame.ContactPositions[0].x, Is.EqualTo(4f));
            Assert.That(frame.ContactPositions[0].y, Is.EqualTo(5f));
            Assert.That(frame.ContactPositions[0].z, Is.EqualTo(6f));
        }

        [Test]
        public void DecodesSharedPythonFullStateFixture()
        {
            var frame = Decode(ReadSharedGoldenFixture());

            Assert.That(frame.SessionId, Is.EqualTo(0x12345678u));
            Assert.That(frame.Sequence, Is.EqualTo(7u));
            Assert.That(frame.JointCount, Is.EqualTo(1));
            Assert.That(frame.RootPosition.x, Is.EqualTo(1f));
            Assert.That(frame.HipsLocalPositionDelta.z, Is.EqualTo(0.3f).Within(0.000001f));
            Assert.That(frame.ContactWeights[0], Is.EqualTo(0.75f));
            Assert.That(frame.ContactEpochs[3], Is.EqualTo(20u));
            Assert.That(frame.ContactPositions[3].x, Is.EqualTo(4f));
            Assert.That(frame.GroundPlane.w, Is.EqualTo(-0.2f).Within(0.000001f));
        }

        [Test]
        public void SessionValidatorRejectsWrongSessionAndOldSequence()
        {
            var frame = Decode(BuildIdentityPacket(2, sessionId: 0x12345678u, sequence: 7u));
            var validator = new PoseSessionValidator(0x12345678u, 2, 0b11ul);

            Assert.That(validator.TryAccept(frame, out var firstError), Is.True);
            Assert.That(firstError, Is.EqualTo(PoseSessionError.None));

            var oldFrame = Decode(BuildIdentityPacket(2, sessionId: 0x12345678u, sequence: 6u));
            Assert.That(validator.TryAccept(oldFrame, out var oldError), Is.False);
            Assert.That(oldError, Is.EqualTo(PoseSessionError.StaleSequence));

            var otherSession = Decode(BuildIdentityPacket(2, sessionId: 9u, sequence: 8u));
            Assert.That(validator.TryAccept(otherSession, out var sessionError), Is.False);
            Assert.That(sessionError, Is.EqualTo(PoseSessionError.SessionMismatch));
        }

        [Test]
        public void SessionValidatorAcceptsSequenceWrap()
        {
            var validator = new PoseSessionValidator(1u, 1, 1ul);
            var beforeWrap = Decode(BuildIdentityPacket(1, sessionId: 1u, sequence: uint.MaxValue));
            var afterWrap = Decode(BuildIdentityPacket(1, sessionId: 1u, sequence: 0u));

            Assert.That(validator.TryAccept(beforeWrap, out _), Is.True);
            Assert.That(validator.TryAccept(afterWrap, out _), Is.True);
        }

        [Test]
        public void SessionValidatorRejectsMissingRequiredJoint()
        {
            var packet = BuildIdentityPacket(2, sessionId: 1u, sequence: 1u);
            BinaryPrimitives.WriteUInt64LittleEndian(packet.AsSpan(72, 8), 0b01ul);
            var frame = Decode(packet);
            var validator = new PoseSessionValidator(1u, 2, 0b11ul);

            Assert.That(validator.TryAccept(frame, out var error), Is.False);
            Assert.That(error, Is.EqualTo(PoseSessionError.MissingRequiredJoint));
        }

        [Test]
        public void PacketGateRequiresDecodeAndSessionValidationBeforePublish()
        {
            var gate = new PosePacketGate(1u, 1, 1ul);
            var scratch = new PoseFrame();

            Assert.That(
                gate.TryDecodeAndAccept(
                    BuildIdentityPacket(1, sessionId: 1u, sequence: 1u),
                    scratch,
                    out var decodeError,
                    out var sessionError),
                Is.True);
            Assert.That(decodeError, Is.EqualTo(PoseDecodeError.None));
            Assert.That(sessionError, Is.EqualTo(PoseSessionError.None));

            Assert.That(
                gate.TryDecodeAndAccept(
                    BuildIdentityPacket(1, sessionId: 2u, sequence: 2u),
                    scratch,
                    out decodeError,
                    out sessionError),
                Is.False);
            Assert.That(decodeError, Is.EqualTo(PoseDecodeError.None));
            Assert.That(sessionError, Is.EqualTo(PoseSessionError.SessionMismatch));
        }

        private static PoseFrame Decode(byte[] packet)
        {
            var frame = new PoseFrame();
            Assert.That(PosePacketDecoder.TryDecode(packet, frame, out var error), Is.True, error.ToString());
            return frame;
        }

        private static byte[] BuildIdentityPacket(
            ushort jointCount,
            uint sessionId = 0x12345678u,
            uint sequence = 7u)
        {
            var packet = new byte[MotionBridgeProtocol.PosePacketBytes(jointCount)];
            packet[0] = (byte)'M';
            packet[1] = (byte)'B';
            packet[2] = (byte)'P';
            packet[3] = (byte)'1';
            packet[4] = MotionBridgeProtocol.Major;
            packet[5] = MotionBridgeProtocol.Minor;
            packet[6] = MotionBridgeProtocol.PoseMessageType;
            BinaryPrimitives.WriteUInt32LittleEndian(packet.AsSpan(8, 4), sessionId);
            BinaryPrimitives.WriteUInt32LittleEndian(packet.AsSpan(12, 4), sequence);
            BinaryPrimitives.WriteUInt64LittleEndian(packet.AsSpan(16, 8), 123_000ul);
            BinaryPrimitives.WriteInt32LittleEndian(packet.AsSpan(24, 4), 42);
            BinaryPrimitives.WriteUInt16LittleEndian(
                packet.AsSpan(28, 2),
                (ushort)(MotionBridgeProtocol.FixedPayloadBytes +
                         MotionBridgeProtocol.QuaternionBytes * jointCount));
            BinaryPrimitives.WriteUInt16LittleEndian(packet.AsSpan(30, 2), jointCount);

            WriteFloat(packet, 56, 1f); // root quaternion w
            BinaryPrimitives.WriteUInt64LittleEndian(
                packet.AsSpan(72, 8),
                jointCount == 64 ? ulong.MaxValue : (1ul << jointCount) - 1ul);

            var jointOffset = 80;
            for (var i = 0; i < jointCount; i++)
                WriteFloat(packet, jointOffset + i * 16 + 12, 1f);

            var contactOffset = jointOffset + jointCount * 16;
            var contactRotationOffset = contactOffset + 16 + 16 + 48;
            for (var i = 0; i < 4; i++)
                WriteFloat(packet, contactRotationOffset + i * 16 + 12, 1f);
            var groundPlaneOffset = contactRotationOffset + 64;
            WriteFloat(packet, groundPlaneOffset + 4, 1f);
            return packet;
        }

        private static void WriteFloat(byte[] destination, int offset, float value)
        {
            BinaryPrimitives.WriteInt32LittleEndian(
                destination.AsSpan(offset, 4),
                BitConverter.SingleToInt32Bits(value));
        }

        private static byte[] ReadSharedGoldenFixture()
        {
            var path = RepositoryFixturePath.Find(
                "golden",
                "pose_v1_full_state_1_joint.hex");
            Assert.That(File.Exists(path), Is.True, path);
            var hex = string.Concat(File.ReadAllLines(path));
            Assert.That(hex.Length % 2, Is.Zero);
            var result = new byte[hex.Length / 2];
            for (var i = 0; i < result.Length; i++)
                result[i] = Convert.ToByte(hex.Substring(i * 2, 2), 16);
            return result;
        }
    }
}
