using System;
using System.Buffers.Binary;
using System.IO;
using Kuko.MotionBridge.Protocol;
using NUnit.Framework;
using UnityEngine;

namespace Kuko.MotionBridge.Tests
{
    public sealed class PosePacketEncoderTests
    {
        [Test]
        public void EncodesSharedPythonFixtureByteForByte()
        {
            var source = new PosePacketSource(
                0x12345678u,
                7u,
                123_000ul,
                42,
                PosePacketFlags.WorldRoot | PosePacketFlags.Contacts,
                new Vector3(1f, 2f, 3f),
                Quaternion.identity,
                new Vector3(0.1f, 0.2f, 0.3f),
                1,
                1ul,
                new[] { Quaternion.identity },
                new[] { 0.75f, 0.5f, 0.25f, 0f },
                new uint[] { 17u, 18u, 19u, 20u },
                new[]
                {
                    new Vector3(1f, 0f, 0f),
                    new Vector3(2f, 0f, 0f),
                    new Vector3(3f, 0f, 0f),
                    new Vector3(4f, 0f, 0f),
                },
                new[]
                {
                    Quaternion.identity,
                    Quaternion.identity,
                    Quaternion.identity,
                    Quaternion.identity,
                },
                new Vector4(0f, 1f, 0f, -0.2f));
            var destination = new byte[MotionBridgeProtocol.PosePacketBytes(1)];

            Assert.That(
                PosePacketEncoder.TryEncode(
                    in source, destination, out var bytesWritten, out var error),
                Is.True);
            Assert.That(error, Is.EqualTo(PoseEncodeError.None));
            Assert.That(bytesWritten, Is.EqualTo(256));
            Assert.That(destination, Is.EqualTo(ReadSharedGoldenFixture()));
        }

        [Test]
        public void EncodesMaximumSixtyJointPacketAndDecoderAcceptsIt()
        {
            var joints = new Quaternion[MotionBridgeProtocol.MaxPoseJointCount];
            for (var i = 0; i < joints.Length; i++)
                joints[i] = Quaternion.identity;
            var source = CreateSource(
                joints,
                jointCount: MotionBridgeProtocol.MaxPoseJointCount,
                presentMask: (1ul << MotionBridgeProtocol.MaxPoseJointCount) - 1ul);
            var destination = new byte[MotionBridgeProtocol.MaxPacketBytes];

            Assert.That(
                PosePacketEncoder.TryEncode(
                    in source, destination, out var bytesWritten, out var encodeError),
                Is.True);
            Assert.That(encodeError, Is.EqualTo(PoseEncodeError.None));
            Assert.That(bytesWritten, Is.EqualTo(MotionBridgeProtocol.MaxPacketBytes));

            var decoded = new PoseFrame(MotionBridgeProtocol.MaxPoseJointCount);
            Assert.That(
                PosePacketDecoder.TryDecode(destination, decoded, out var decodeError),
                Is.True);
            Assert.That(decodeError, Is.EqualTo(PoseDecodeError.None));
            Assert.That(decoded.JointCount, Is.EqualTo(MotionBridgeProtocol.MaxPoseJointCount));
        }

        [Test]
        public void RejectsDestinationThatCannotHoldPacketWithoutWriting()
        {
            var source = CreateSource(new[] { Quaternion.identity });
            var destination = new byte[MotionBridgeProtocol.PosePacketBytes(1) - 1];
            Array.Fill(destination, (byte)0xA5);

            Assert.That(
                PosePacketEncoder.TryEncode(
                    in source, destination, out var bytesWritten, out var error),
                Is.False);
            Assert.That(error, Is.EqualTo(PoseEncodeError.DestinationTooSmall));
            Assert.That(bytesWritten, Is.Zero);
            Assert.That(destination, Is.All.EqualTo(0xA5));
        }

        [Test]
        public void RejectsReservedFlags()
        {
            var source = CreateSource(
                new[] { Quaternion.identity }, flags: (PosePacketFlags)0x10);

            AssertEncodeFails(in source, PoseEncodeError.ReservedFlags);
        }

        [Test]
        public void RejectsContactWeightWhenContactsFlagIsUnset()
        {
            var source = CreateSource(
                new[] { Quaternion.identity }, contactWeights: new[] { 1f, 0f, 0f, 0f });

            AssertEncodeFails(in source, PoseEncodeError.InvalidContactWeight);
        }

        [Test]
        public void RejectsNonIdentityRotationForAbsentJoint()
        {
            var source = CreateSource(
                new[] { new Quaternion(1f, 0f, 0f, 0f) }, presentMask: 0ul);

            AssertEncodeFails(in source, PoseEncodeError.InvalidPresentMask);
        }

        [Test]
        public void RejectsZeroQuaternion()
        {
            var source = CreateSource(new[] { new Quaternion(0f, 0f, 0f, 0f) });

            AssertEncodeFails(in source, PoseEncodeError.InvalidQuaternion);
        }

        [Test]
        public void EncoderAndDecoderRejectCrossLanguageGroundNormBoundaryValues()
        {
            var groundPlanes = new[]
            {
                new Vector4(
                    0.9444475769996643f,
                    0.016245830804109573f,
                    -0.296403169631958f,
                    0f),
                new Vector4(
                    -0.44875013786458123f,
                    0.6513351440677846f,
                    -0.5953871814419072f,
                    0f),
            };

            foreach (var groundPlane in groundPlanes)
            {
                var source = CreateSource(
                    new[] { Quaternion.identity }, groundPlane: groundPlane);
                AssertEncodeFails(in source, PoseEncodeError.InvalidGroundPlane);

                var packet = ReadSharedGoldenFixture();
                var groundPlaneOffset = packet.Length - 16;
                WriteSingle(packet, groundPlaneOffset, groundPlane.x);
                WriteSingle(packet, groundPlaneOffset + 4, groundPlane.y);
                WriteSingle(packet, groundPlaneOffset + 8, groundPlane.z);
                WriteSingle(packet, groundPlaneOffset + 12, groundPlane.w);
                var decoded = new PoseFrame(1);
                Assert.That(
                    PosePacketDecoder.TryDecode(packet, decoded, out var error),
                    Is.False);
                Assert.That(error, Is.EqualTo(PoseDecodeError.NonFiniteValue));
            }
        }

        private static PosePacketSource CreateSource(
            Quaternion[] joints,
            int jointCount = 1,
            ulong presentMask = 1ul,
            PosePacketFlags flags = PosePacketFlags.None,
            float[] contactWeights = null,
            Vector4? groundPlane = null)
        {
            return new PosePacketSource(
                1u,
                1u,
                1ul,
                1,
                flags,
                Vector3.zero,
                Quaternion.identity,
                Vector3.zero,
                jointCount,
                presentMask,
                joints,
                contactWeights ?? new[] { 0f, 0f, 0f, 0f },
                new uint[4],
                new Vector3[4],
                new[]
                {
                    Quaternion.identity,
                    Quaternion.identity,
                    Quaternion.identity,
                    Quaternion.identity,
                },
                groundPlane ?? new Vector4(0f, 1f, 0f, 0f));
        }

        private static void AssertEncodeFails(
            in PosePacketSource source,
            PoseEncodeError expectedError)
        {
            var destination = new byte[MotionBridgeProtocol.MaxPacketBytes];
            Assert.That(
                PosePacketEncoder.TryEncode(
                    in source, destination, out var bytesWritten, out var error),
                Is.False);
            Assert.That(error, Is.EqualTo(expectedError));
            Assert.That(bytesWritten, Is.Zero);
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

        private static void WriteSingle(byte[] destination, int offset, float value)
        {
            BinaryPrimitives.WriteInt32LittleEndian(
                destination.AsSpan(offset, 4), BitConverter.SingleToInt32Bits(value));
        }
    }
}
