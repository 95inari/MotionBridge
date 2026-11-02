using System;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using Kuko.MotionBridge.Protocol;
using Kuko.MotionBridge.Transport;
using NUnit.Framework;
using UnityEngine;

namespace Kuko.MotionBridge.Tests
{
    public sealed class PoseUdpReceiverTests
    {
        private const uint SessionId = 0x12345678u;

        [Test]
        public void ReceivesValidPacketAndConsumesWithoutMainThreadAllocation()
        {
            using (var receiver = CreateReceiver())
            using (var sender = new UdpClient(AddressFamily.InterNetwork))
            {
                var port = receiver.Start();
                var endpoint = new IPEndPoint(IPAddress.Loopback, port);
                Send(sender, endpoint, EncodePacket(7u));
                WaitUntil(() => receiver.Diagnostics.AcceptedPackets == 1);

                var destination = new PoseFrame(1);
                Assert.That(receiver.TryConsumeLatest(destination), Is.True);
                Assert.That(destination.SessionId, Is.EqualTo(SessionId));
                Assert.That(destination.Sequence, Is.EqualTo(7u));
                Assert.That(destination.RootPosition.x, Is.EqualTo(7f));
                Assert.That(receiver.TryConsumeLatest(destination), Is.False);

                Send(sender, endpoint, EncodePacket(8u));
                WaitUntil(() => receiver.Diagnostics.AcceptedPackets == 2);
                var allocatedBefore = GC.GetAllocatedBytesForCurrentThread();
                var consumed = receiver.TryConsumeLatest(destination);
                var allocatedAfter = GC.GetAllocatedBytesForCurrentThread();

                Assert.That(consumed, Is.True);
                Assert.That(destination.Sequence, Is.EqualTo(8u));
                Assert.That(allocatedAfter, Is.EqualTo(allocatedBefore));
                Assert.That(receiver.Diagnostics.UnexpectedExceptions, Is.Zero);
            }
        }

        [Test]
        public void RejectsStaleAndOutOfOrderPacketsAndCountsSequenceGap()
        {
            using (var receiver = CreateReceiver())
            using (var sender = new UdpClient(AddressFamily.InterNetwork))
            {
                var endpoint = new IPEndPoint(IPAddress.Loopback, receiver.Start());
                Send(sender, endpoint, EncodePacket(10u));
                WaitUntil(() => receiver.Diagnostics.AcceptedPackets == 1);
                Send(sender, endpoint, EncodePacket(12u));
                WaitUntil(() => receiver.Diagnostics.AcceptedPackets == 2);
                Send(sender, endpoint, EncodePacket(11u));
                Send(sender, endpoint, EncodePacket(12u));
                WaitUntil(() => receiver.Diagnostics.StalePackets == 2);

                var diagnostics = receiver.Diagnostics;
                Assert.That(diagnostics.ReceivedDatagrams, Is.EqualTo(4));
                Assert.That(diagnostics.AcceptedPackets, Is.EqualTo(2));
                Assert.That(diagnostics.SessionRejectedPackets, Is.EqualTo(2));
                Assert.That(diagnostics.SequenceGapDrops, Is.EqualTo(1));
                Assert.That(diagnostics.HasAcceptedSequence, Is.True);
                Assert.That(diagnostics.LastAcceptedSequence, Is.EqualTo(12u));
                Assert.That(diagnostics.LastSessionError, Is.EqualTo(PoseSessionError.StaleSequence));

                var destination = new PoseFrame(1);
                Assert.That(receiver.TryConsumeLatest(destination), Is.True);
                Assert.That(destination.Sequence, Is.EqualTo(12u));
            }
        }

        [Test]
        public void RejectsWireSessionJointAndRequiredMaskMismatches()
        {
            using (var receiver = CreateReceiver())
            using (var sender = new UdpClient(AddressFamily.InterNetwork))
            {
                var endpoint = new IPEndPoint(IPAddress.Loopback, receiver.Start());
                var badMagic = EncodePacket(1u);
                badMagic[0] = (byte)'X';
                Send(sender, endpoint, badMagic);
                Send(sender, endpoint, EncodePacket(2u, sessionId: SessionId + 1));
                Send(sender, endpoint, EncodePacket(3u, presentMask: 0ul));
                Send(sender, endpoint, EncodePacket(4u, jointCount: 2));
                WaitUntil(() =>
                {
                    var current = receiver.Diagnostics;
                    return current.DecodeRejectedPackets == 1 &&
                           current.SessionRejectedPackets == 3;
                });

                var diagnostics = receiver.Diagnostics;
                Assert.That(diagnostics.ReceivedDatagrams, Is.EqualTo(4));
                Assert.That(diagnostics.AcceptedPackets, Is.Zero);
                Assert.That(diagnostics.DecodeRejectedPackets, Is.EqualTo(1));
                Assert.That(diagnostics.SessionRejectedPackets, Is.EqualTo(3));
                Assert.That(diagnostics.LastDecodeError, Is.EqualTo(PoseDecodeError.BadMagic));
                Assert.That(diagnostics.LastSessionError, Is.EqualTo(PoseSessionError.JointCountMismatch));
                Assert.That(receiver.TryConsumeLatest(new PoseFrame(1)), Is.False);
            }
        }

        [Test]
        public void BurstPublishesOnlyLatestFrame()
        {
            using (var receiver = CreateReceiver())
            using (var sender = new UdpClient(AddressFamily.InterNetwork))
            {
                var endpoint = new IPEndPoint(IPAddress.Loopback, receiver.Start());
                const uint packetCount = 32;
                for (uint sequence = 1; sequence <= packetCount; sequence++)
                    Send(sender, endpoint, EncodePacket(sequence));
                WaitUntil(() => receiver.Diagnostics.AcceptedPackets == packetCount);

                var destination = new PoseFrame(1);
                Assert.That(receiver.TryConsumeLatest(destination), Is.True);
                Assert.That(destination.Sequence, Is.EqualTo(packetCount));
                Assert.That(receiver.TryConsumeLatest(destination), Is.False);

                var diagnostics = receiver.Diagnostics;
                Assert.That(diagnostics.SupersededFrames, Is.EqualTo(packetCount - 1));
                Assert.That(diagnostics.ReceiverOverruns, Is.Zero);
                Assert.That(diagnostics.SequenceGapDrops, Is.Zero);
            }
        }

        [Test]
        public void OneWayAgeIsAbsentUntilClockSynchronization()
        {
            var nowUsec = 10_000L;
            using (var receiver = new PoseUdpReceiver(
                       new PoseUdpReceiverConfig(SessionId, 1, 1ul),
                       () => Volatile.Read(ref nowUsec)))
            using (var sender = new UdpClient(AddressFamily.InterNetwork))
            {
                var endpoint = new IPEndPoint(IPAddress.Loopback, receiver.Start());
                Send(sender, endpoint, EncodePacket(1u, timestampUsec: 9_000ul));
                WaitUntil(() => receiver.Diagnostics.AcceptedPackets == 1);

                var diagnostics = receiver.Diagnostics;
                Assert.That(diagnostics.ClockSynchronized, Is.False);
                Assert.That(diagnostics.HasOneWayAge, Is.False);
                Assert.That(diagnostics.OneWayAgeSampleCount, Is.Zero);
            }
        }

        [Test]
        public void OneWayAgePublishesRollingPercentilesAndDiscardsNegativeSamples()
        {
            var nowUsec = 1_000L;
            using (var receiver = new PoseUdpReceiver(
                       new PoseUdpReceiverConfig(SessionId, 1, 1ul),
                       () => Volatile.Read(ref nowUsec)))
            using (var sender = new UdpClient(AddressFamily.InterNetwork))
            {
                var endpoint = new IPEndPoint(IPAddress.Loopback, receiver.Start());
                Assert.That(receiver.TrySetClockSynchronization(100L), Is.True);
                var ages = new long[] { 0L, 10L, 20L, 30L, 100L };
                for (var index = 0; index < ages.Length; index++)
                {
                    Volatile.Write(ref nowUsec, 1_000L + ages[index]);
                    Send(
                        sender,
                        endpoint,
                        EncodePacket((uint)(index + 1), timestampUsec: 900ul));
                    var accepted = index + 1;
                    WaitUntil(() => receiver.Diagnostics.AcceptedPackets == accepted);
                }

                var diagnostics = receiver.Diagnostics;
                Assert.That(diagnostics.ClockSynchronized, Is.True);
                Assert.That(diagnostics.OneWayAgeSampleCount, Is.EqualTo(5));
                Assert.That(diagnostics.OneWayAgeUsecP50, Is.EqualTo(20L));
                Assert.That(diagnostics.OneWayAgeUsecP95, Is.EqualTo(100L));

                Volatile.Write(ref nowUsec, 1_000L);
                Send(sender, endpoint, EncodePacket(6u, timestampUsec: 1_000ul));
                WaitUntil(() => receiver.Diagnostics.AcceptedPackets == 6);
                diagnostics = receiver.Diagnostics;
                Assert.That(diagnostics.OneWayAgeSampleCount, Is.EqualTo(5));
                Assert.That(diagnostics.OneWayAgeUsecP50, Is.EqualTo(20L));
                Assert.That(diagnostics.OneWayAgeUsecP95, Is.EqualTo(100L));

                Assert.That(receiver.TrySetClockSynchronization(130L), Is.True);
                diagnostics = receiver.Diagnostics;
                Assert.That(diagnostics.OneWayAgeSampleCount, Is.EqualTo(2));
                Assert.That(diagnostics.OneWayAgeUsecP50, Is.Zero);
                Assert.That(diagnostics.OneWayAgeUsecP95, Is.EqualTo(70L));
            }
        }

        [Test]
        public void OffsetRebasePreservesTheChronologicalRollingWindow()
        {
            var nowUsec = 2_000L;
            using (var receiver = new PoseUdpReceiver(
                       new PoseUdpReceiverConfig(SessionId, 1, 1ul),
                       () => Volatile.Read(ref nowUsec)))
            using (var sender = new UdpClient(AddressFamily.InterNetwork))
            {
                var endpoint = new IPEndPoint(IPAddress.Loopback, receiver.Start());
                Assert.That(receiver.TrySetClockSynchronization(100L), Is.True);
                for (var index = 0; index < 266; index++)
                {
                    Volatile.Write(ref nowUsec, 2_000L + index);
                    Send(
                        sender,
                        endpoint,
                        EncodePacket((uint)(index + 1), timestampUsec: 900ul));
                    var accepted = index + 1;
                    WaitUntil(() => receiver.Diagnostics.AcceptedPackets == accepted);
                }

                Assert.That(receiver.TrySetClockSynchronization(101L), Is.True);
                Volatile.Write(ref nowUsec, 3_001L);
                Send(sender, endpoint, EncodePacket(267u, timestampUsec: 900ul));
                WaitUntil(() => receiver.Diagnostics.AcceptedPackets == 267);

                var diagnostics = receiver.Diagnostics;
                Assert.That(diagnostics.OneWayAgeSampleCount, Is.EqualTo(256));
                Assert.That(diagnostics.OneWayAgeUsecP50, Is.EqualTo(1_137L));
                Assert.That(diagnostics.OneWayAgeUsecP95, Is.EqualTo(1_253L));
            }
        }

        [Test]
        public void ClockOffsetJumpInvalidatesAgeUntilExplicitReset()
        {
            using (var receiver = CreateReceiver())
            {
                Assert.That(receiver.TrySetClockSynchronization(1_000L), Is.True);
                Assert.That(receiver.TrySetClockSynchronization(6_000L), Is.True);
                Assert.That(receiver.TrySetClockSynchronization(11_001L), Is.False);
                Assert.That(receiver.Diagnostics.ClockSynchronized, Is.False);
                Assert.That(receiver.Diagnostics.OneWayAgeSampleCount, Is.Zero);
                Assert.That(receiver.TrySetClockSynchronization(0L), Is.False);

                receiver.ClearClockSynchronization();
                Assert.That(receiver.TrySetClockSynchronization(long.MinValue), Is.True);
                Assert.That(receiver.TrySetClockSynchronization(long.MaxValue), Is.False);
            }
        }

        [Test]
        public void StartStopDisposeAreIdempotentAndReleasePortForRebind()
        {
            Assert.Throws<ArgumentException>(() => new PoseUdpReceiverConfig(
                SessionId,
                1,
                1ul,
                bindAddress: IPAddress.Any));

            var receiver = CreateReceiver();
            var rebound = default(PoseUdpReceiver);
            try
            {
                var firstPort = receiver.Start();
                Assert.That(receiver.Start(), Is.EqualTo(firstPort));
                Assert.That(receiver.IsRunning, Is.True);
                Assert.That(receiver.TrySetClockSynchronization(123L), Is.True);
                using (var sender = new UdpClient(AddressFamily.InterNetwork))
                {
                    Send(
                        sender,
                        new IPEndPoint(IPAddress.Loopback, firstPort),
                        EncodePacket(100u));
                    WaitUntil(() => receiver.Diagnostics.AcceptedPackets == 1);
                }
                Assert.That(receiver.Diagnostics.LastAcceptedSequence, Is.EqualTo(100u));

                var stopwatch = Stopwatch.StartNew();
                receiver.Stop();
                stopwatch.Stop();
                Assert.That(stopwatch.Elapsed, Is.LessThan(TimeSpan.FromSeconds(2)));
                receiver.Stop();
                Assert.That(receiver.IsRunning, Is.False);
                Assert.That(receiver.BoundPort, Is.Zero);
                Assert.That(receiver.Diagnostics.ClockSynchronized, Is.False);

                var restartedPort = receiver.Start();
                using (var sender = new UdpClient(AddressFamily.InterNetwork))
                {
                    Send(
                        sender,
                        new IPEndPoint(IPAddress.Loopback, restartedPort),
                        EncodePacket(0u));
                    WaitUntil(() => receiver.Diagnostics.AcceptedPackets == 1);
                }
                Assert.That(receiver.Diagnostics.LastAcceptedSequence, Is.Zero);
                Assert.That(receiver.Diagnostics.StalePackets, Is.Zero);
                Assert.That(receiver.Diagnostics.SessionRejectedPackets, Is.Zero);
                receiver.Stop();

                rebound = new PoseUdpReceiver(new PoseUdpReceiverConfig(
                    SessionId,
                    1,
                    1ul,
                    restartedPort));
                Assert.That(rebound.Start(), Is.EqualTo(restartedPort));
                rebound.Stop();
                rebound.Dispose();
                rebound.Dispose();
                Assert.Throws<ObjectDisposedException>(() => rebound.Start());
            }
            finally
            {
                rebound?.Dispose();
                receiver.Dispose();
                receiver.Dispose();
            }

            Assert.Throws<ObjectDisposedException>(() => receiver.Start());
        }

        private static PoseUdpReceiver CreateReceiver()
        {
            return new PoseUdpReceiver(new PoseUdpReceiverConfig(
                SessionId,
                1,
                1ul));
        }

        private static byte[] EncodePacket(
            uint sequence,
            uint sessionId = SessionId,
            int jointCount = 1,
            ulong? presentMask = null,
            ulong? timestampUsec = null)
        {
            var joints = new Quaternion[jointCount];
            for (var i = 0; i < joints.Length; i++)
                joints[i] = Quaternion.identity;
            var source = new PosePacketSource(
                sessionId,
                sequence,
                timestampUsec ?? sequence * 1000ul,
                (int)sequence,
                PosePacketFlags.None,
                new Vector3(sequence, 2f, 3f),
                Quaternion.identity,
                new Vector3(0.1f, 0.2f, 0.3f),
                jointCount,
                presentMask ?? ((1ul << jointCount) - 1ul),
                joints,
                new float[4],
                new uint[4],
                new Vector3[4],
                new[]
                {
                    Quaternion.identity,
                    Quaternion.identity,
                    Quaternion.identity,
                    Quaternion.identity,
                },
                new Vector4(0f, 1f, 0f, 0f));
            var packet = new byte[MotionBridgeProtocol.PosePacketBytes(jointCount)];
            Assert.That(
                PosePacketEncoder.TryEncode(
                    in source,
                    packet,
                    out var bytesWritten,
                    out var error),
                Is.True,
                error.ToString());
            Assert.That(bytesWritten, Is.EqualTo(packet.Length));
            return packet;
        }

        private static void Send(UdpClient sender, IPEndPoint endpoint, byte[] packet)
        {
            Assert.That(sender.Send(packet, packet.Length, endpoint), Is.EqualTo(packet.Length));
        }

        private static void WaitUntil(Func<bool> predicate)
        {
            Assert.That(
                SpinWait.SpinUntil(predicate, TimeSpan.FromSeconds(3)),
                Is.True,
                "Timed out waiting for the background UDP receiver.");
        }
    }
}
