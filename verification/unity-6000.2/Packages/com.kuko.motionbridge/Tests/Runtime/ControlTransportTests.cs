using System;
using System.Collections.Generic;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using Kuko.MotionBridge.Protocol;
using Kuko.MotionBridge.Transport;
using NUnit.Framework;
using UnityEngine;

namespace Kuko.MotionBridge.Tests
{
    public sealed class ControlTransportTests
    {
        private const uint SessionId = 0x12345678u;
        private const string SkeletonId = "mb-canonical-humanoid-v1";
        private const string SkeletonHash =
            "1111111111111111111111111111111111111111111111111111111111111111";

        [Test]
        public void DecoderHandlesFragmentedAndCoalescedFrames()
        {
            var firstJson = Message("ping", 7u);
            var secondJson = Message("pong", 7u);
            var first = ControlFrameEncoder.EncodeJson(firstJson);
            var second = ControlFrameEncoder.EncodeJson(secondJson);
            var decoder = new ControlFrameDecoder();

            decoder.Append(first, 0, 2);
            Assert.That(decoder.PendingMessageCount, Is.Zero);
            decoder.Append(first, 2, 3);
            Assert.That(decoder.PendingMessageCount, Is.Zero);
            decoder.Append(first, 5, first.Length - 5);
            Assert.That(decoder.TryDequeue(out var decodedFirst), Is.True);
            Assert.That(decodedFirst, Is.EqualTo(firstJson));

            var combined = new byte[first.Length + second.Length];
            Buffer.BlockCopy(first, 0, combined, 0, first.Length);
            Buffer.BlockCopy(second, 0, combined, first.Length, second.Length);
            decoder.Append(combined, 0, combined.Length);
            Assert.That(decoder.TryDequeue(out decodedFirst), Is.True);
            Assert.That(decoder.TryDequeue(out var decodedSecond), Is.True);
            Assert.That(decodedFirst, Is.EqualTo(firstJson));
            Assert.That(decodedSecond, Is.EqualTo(secondJson));
            Assert.That(decoder.TryDequeue(out _), Is.False);
        }

        [Test]
        public void DecoderRejectsInvalidLengthAndUtf8AndFaultsUntilReset()
        {
            var decoder = new ControlFrameDecoder();
            Assert.Throws<ControlProtocolException>(() =>
                decoder.Append(new byte[] { 0, 0, 0, 0 }, 0, 4));
            Assert.Throws<ControlProtocolException>(() =>
                decoder.Append(Array.Empty<byte>(), 0, 0));

            decoder.Reset();
            var oversized = new byte[] { 1, 0, 1, 0 };
            Assert.Throws<ControlProtocolException>(() =>
                decoder.Append(oversized, 0, oversized.Length));

            decoder.Reset();
            var invalidUtf8 = new byte[] { 1, 0, 0, 0, 0xff };
            Assert.Throws<ControlProtocolException>(() =>
                decoder.Append(invalidUtf8, 0, invalidUtf8.Length));
            Assert.Throws<ControlProtocolException>(() =>
                ControlFrameEncoder.EncodeJson(
                    "{\"value\":\"" + new string('x', 65537) + "\"}"));
            Assert.Throws<ControlProtocolException>(() =>
                ControlFrameEncoder.EncodeJson("[]"));
        }

        [Test]
        public void LoopbackHandshakeTransitionsReadyStreamingPausedAndGoodbye()
        {
            var descriptorWasValidated = 0;
            using (var server = new ControlServer(
                CreateConfig(),
                json =>
                {
                    Interlocked.Exchange(ref descriptorWasValidated, 1);
                    return json.Contains(SkeletonHash);
                }))
            using (var client = new TcpClient(AddressFamily.InterNetwork))
            {
                var port = server.Start();
                client.Connect(IPAddress.Loopback, port);
                client.NoDelay = true;
                var stream = client.GetStream();
                stream.ReadTimeout = 2000;
                stream.WriteTimeout = 2000;

                var hello = ControlFrameEncoder.EncodeJson(Hello());
                stream.Write(hello, 0, 2);
                stream.Write(hello, 2, 3);
                stream.Write(hello, 5, hello.Length - 5);
                var accept = ParseEnvelope(ReadJson(stream));
                Assert.That(accept.type, Is.EqualTo("accept"));
                Assert.That(accept.requestId, Is.EqualTo(1u));
                Assert.That(accept.udpPort, Is.EqualTo(50321));

                WriteJson(stream, Descriptor());
                var ready = ParseEnvelope(ReadJson(stream));
                Assert.That(ready.type, Is.EqualTo("ready"));
                Assert.That(ready.requestId, Is.EqualTo(2u));
                WaitUntil(() => server.State == ControlSessionState.Ready);
                Assert.That(Volatile.Read(ref descriptorWasValidated), Is.EqualTo(1));

                WriteJson(stream, Message("resume", 3u));
                WaitUntil(() => server.State == ControlSessionState.Streaming);

                var ping = ControlFrameEncoder.EncodeJson(Message("ping", 4u));
                var pause = ControlFrameEncoder.EncodeJson(Message("pause", 5u));
                var combined = new byte[ping.Length + pause.Length];
                Buffer.BlockCopy(ping, 0, combined, 0, ping.Length);
                Buffer.BlockCopy(pause, 0, combined, ping.Length, pause.Length);
                stream.Write(combined, 0, combined.Length);
                var pong = ParseEnvelope(ReadJson(stream));
                Assert.That(pong.type, Is.EqualTo("pong"));
                Assert.That(pong.requestId, Is.EqualTo(4u));
                WaitUntil(() => server.State == ControlSessionState.Paused);

                WriteJson(stream, Message("resume", 6u));
                WaitUntil(() => server.State == ControlSessionState.Streaming);
                WriteJson(stream, Message("goodbye", 7u));
                WaitUntil(() => server.State == ControlSessionState.Listening);

                var kinds = new List<ControlServerEventKind>();
                while (server.TryDequeueEvent(out var controlEvent))
                    kinds.Add(controlEvent.Kind);
                CollectionAssert.Contains(kinds, ControlServerEventKind.HelloAccepted);
                CollectionAssert.Contains(kinds, ControlServerEventKind.Ready);
                CollectionAssert.Contains(kinds, ControlServerEventKind.Streaming);
                CollectionAssert.Contains(kinds, ControlServerEventKind.Paused);
                CollectionAssert.Contains(kinds, ControlServerEventKind.Disconnected);
                Assert.That(server.Diagnostics.ProtocolErrors, Is.Zero);
            }
        }

        [Test]
        public void IncompleteContactsCapabilityIsNotAcceptedWhenOffered()
        {
            using (var server = new ControlServer(CreateConfig()))
            using (var client = new TcpClient(AddressFamily.InterNetwork))
            {
                client.Connect(IPAddress.Loopback, server.Start());
                var stream = client.GetStream();
                stream.ReadTimeout = 2000;

                WriteJson(stream, Hello());
                var accept = ParseEnvelope(ReadJson(stream));

                CollectionAssert.DoesNotContain(accept.capabilities, "contacts-v1");
                CollectionAssert.Contains(accept.capabilities, "global-rest-delta-v1");
            }
        }

        [Test]
        public void InvalidMessageStateReturnsErrorAndStopsSession()
        {
            using (var server = new ControlServer(CreateConfig()))
            using (var client = new TcpClient(AddressFamily.InterNetwork))
            {
                client.Connect(IPAddress.Loopback, server.Start());
                var stream = client.GetStream();
                stream.ReadTimeout = 2000;
                WriteJson(stream, Message("resume", 1u));

                var error = ParseEnvelope(ReadJson(stream));
                Assert.That(error.type, Is.EqualTo("error"));
                Assert.That(error.code, Is.EqualTo("PROTOCOL_ERROR"));
                WaitUntil(() => server.State == ControlSessionState.Error);
                Assert.That(server.Diagnostics.ProtocolErrors, Is.EqualTo(1));
            }
        }

        [Test]
        public void CanonicalMismatchReturnsSpecificErrorWithBothHashPrefixes()
        {
            const string ReceivedHash =
                "2222222222222222222222222222222222222222222222222222222222222222";
            using (var server = new ControlServer(CreateConfig()))
            using (var client = new TcpClient(AddressFamily.InterNetwork))
            {
                client.Connect(IPAddress.Loopback, server.Start());
                var stream = client.GetStream();
                stream.ReadTimeout = 2000;
                WriteJson(stream, Hello(skeletonHash: ReceivedHash));

                var error = ParseEnvelope(ReadJson(stream));

                Assert.That(error.type, Is.EqualTo("error"));
                Assert.That(error.code, Is.EqualTo("SKELETON_HASH_MISMATCH"));
                Assert.That(error.message, Does.Contain(SkeletonHash.Substring(0, 12)));
                Assert.That(error.message, Does.Contain(ReceivedHash.Substring(0, 12)));
                WaitUntil(() => server.State == ControlSessionState.Error);
            }
        }

        [Test]
        public void HandshakeHonorsLowerRequestedPoseRate()
        {
            using (var server = new ControlServer(CreateConfig()))
            using (var client = new TcpClient(AddressFamily.InterNetwork))
            {
                client.Connect(IPAddress.Loopback, server.Start());
                var stream = client.GetStream();
                stream.ReadTimeout = 2000;
                WriteJson(stream, Hello(30));

                var accept = ParseEnvelope(ReadJson(stream));
                Assert.That(accept.type, Is.EqualTo("accept"));
                Assert.That(accept.acceptedPoseHz, Is.EqualTo(30));
                WriteJson(stream, Message("goodbye", 2u));
            }
        }

        [Test]
        public void ClockSyncPongEchoesT0AndCapturesOrderedUnityTimestamps()
        {
            using (var server = new ControlServer(CreateConfig()))
            using (var client = new TcpClient(AddressFamily.InterNetwork))
            {
                client.Connect(IPAddress.Loopback, server.Start());
                var stream = client.GetStream();
                stream.ReadTimeout = 2000;
                CompleteHandshake(stream, telemetry: true);

                WriteJson(stream, ClockSyncPing(20u, 1_000_000L));
                var pong = ParseEnvelope(ReadJson(stream));
                Assert.That(pong.type, Is.EqualTo("clock_sync_pong"));
                Assert.That(pong.requestId, Is.EqualTo(20u));
                Assert.That(pong.t0BlenderSendUsec, Is.EqualTo(1_000_000L));
                Assert.That(pong.t1UnityReceiveUsec, Is.GreaterThan(0L));
                Assert.That(
                    pong.t2UnitySendUsec,
                    Is.GreaterThanOrEqualTo(pong.t1UnityReceiveUsec));
                WaitUntil(() => server.Diagnostics.ClockSyncReplies == 1);
            }
        }

        [Test]
        public void ClockSyncRejectsMissingOrNonPositiveT0()
        {
            using (var server = new ControlServer(CreateConfig()))
            using (var client = new TcpClient(AddressFamily.InterNetwork))
            {
                client.Connect(IPAddress.Loopback, server.Start());
                var stream = client.GetStream();
                stream.ReadTimeout = 2000;
                WriteJson(stream, Hello(telemetry: true));
                Assert.That(ParseEnvelope(ReadJson(stream)).type, Is.EqualTo("accept"));
                WriteJson(stream, ClockSyncPing(20u, 0L));

                var error = ParseEnvelope(ReadJson(stream));
                Assert.That(error.type, Is.EqualTo("error"));
                Assert.That(error.code, Is.EqualTo("PROTOCOL_ERROR"));
                WaitUntil(() => server.State == ControlSessionState.Error);
            }
        }

        [Test]
        public void PoseStatsPublishesReceiverCountersEveryConfiguredInterval()
        {
            using (var receiver = new PoseUdpReceiver(new PoseUdpReceiverConfig(
                SessionId,
                1,
                1ul)))
            using (var sender = new UdpClient(AddressFamily.InterNetwork))
            {
                var udpPort = receiver.Start();
                using (var server = new ControlServer(
                    CreateConfig(
                        udpPort: udpPort,
                        poseStatsInterval: TimeSpan.FromMilliseconds(50)),
                    poseDiagnosticsProvider: () => receiver.Diagnostics,
                    poseClockOffsetConsumer: receiver.TrySetClockSynchronization,
                    poseClockSynchronizationResetter:
                        receiver.ClearClockSynchronization))
                using (var client = new TcpClient(AddressFamily.InterNetwork))
                {
                    client.Connect(IPAddress.Loopback, server.Start());
                    var stream = client.GetStream();
                    stream.ReadTimeout = 2000;
                    CompleteHandshake(stream, telemetry: true, poseAgeAccepted: true);
                    WriteJson(stream, ClockSyncOffset(21u, 0L));

                    var endpoint = new IPEndPoint(IPAddress.Loopback, udpPort);
                    SendPose(sender, endpoint, EncodePosePacket(10u));
                    SendPose(sender, endpoint, EncodePosePacket(12u));
                    SendPose(sender, endpoint, EncodePosePacket(11u));
                    WaitUntil(() =>
                    {
                        var current = receiver.Diagnostics;
                        return current.AcceptedPackets == 2 &&
                               current.SessionRejectedPackets == 1;
                    });

                    TestEnvelope stats = null;
                    for (var attempt = 0; attempt < 10; attempt++)
                    {
                        var candidate = ParseEnvelope(ReadJson(stream));
                        if (candidate.type == "pose_stats" && candidate.received >= 2)
                        {
                            stats = candidate;
                            break;
                        }
                    }
                    Assert.That(stats, Is.Not.Null);
                    Assert.That(stats.type, Is.EqualTo("pose_stats"));
                    Assert.That(stats.lastSequence, Is.EqualTo(12u));
                    Assert.That(stats.received, Is.EqualTo(2L));
                    Assert.That(stats.droppedBySequence, Is.EqualTo(1L));
                    Assert.That(stats.rejected, Is.EqualTo(1L));
                    Assert.That(stats.receiverOverrun, Is.Zero);
                    Assert.That(stats.oneWayAgeUsecP95, Is.GreaterThanOrEqualTo(
                        stats.oneWayAgeUsecP50));
                    Assert.That(server.Diagnostics.PoseStatsFrames, Is.GreaterThanOrEqualTo(1));
                }
            }
        }

        [Test]
        public void PoseStatsOmitsOneWayAgeFieldsWhileUnsynchronized()
        {
            using (var receiver = new PoseUdpReceiver(new PoseUdpReceiverConfig(
                SessionId,
                1,
                1ul)))
            {
                var udpPort = receiver.Start();
                using (var server = new ControlServer(
                           CreateConfig(
                               udpPort: udpPort,
                               poseStatsInterval: TimeSpan.FromMilliseconds(25)),
                           poseDiagnosticsProvider: () => receiver.Diagnostics,
                           poseClockOffsetConsumer: receiver.TrySetClockSynchronization,
                           poseClockSynchronizationResetter:
                               receiver.ClearClockSynchronization))
                using (var client = new TcpClient(AddressFamily.InterNetwork))
                {
                    client.Connect(IPAddress.Loopback, server.Start());
                    var stream = client.GetStream();
                    stream.ReadTimeout = 2000;
                    CompleteHandshake(stream, telemetry: true, poseAgeAccepted: true);

                    var json = ReadJson(stream);
                    Assert.That(ParseEnvelope(json).type, Is.EqualTo("pose_stats"));
                    Assert.That(json, Does.Not.Contain("oneWayAgeUsecP50"));
                    Assert.That(json, Does.Not.Contain("oneWayAgeUsecP95"));
                    Assert.That(receiver.Diagnostics.ClockSynchronized, Is.False);
                }
            }
        }

        [TestCase("")]
        [TestCase("\"unityMinusBlenderOffsetUsec\":1.5")]
        [TestCase("\"unityMinusBlenderOffsetUsec\":\"1\"")]
        [TestCase("\"unityMinusBlenderOffsetUsec\":9223372036854775808")]
        [TestCase("\"unityMinusBlenderOffsetUsec\":1,\"unityMinusBlenderOffsetUsec\":2")]
        [TestCase("\"payload\":{\"unityMinusBlenderOffsetUsec\":1}")]
        public void ClockSyncOffsetRejectsMissingMalformedOrDuplicateInt64(string fieldJson)
        {
            using (var receiver = CreatePoseReceiver())
            {
                receiver.Start();
                using (var server = CreatePoseAgeServer(receiver))
                using (var client = new TcpClient(AddressFamily.InterNetwork))
                {
                    client.Connect(IPAddress.Loopback, server.Start());
                    var stream = client.GetStream();
                    stream.ReadTimeout = 2000;
                    CompleteHandshake(stream, telemetry: true, poseAgeAccepted: true);
                    WriteJson(stream, ClockSyncOffsetRaw(21u, fieldJson));

                    var error = ParseEnvelope(ReadJson(stream));
                    Assert.That(error.type, Is.EqualTo("error"));
                    Assert.That(error.code, Is.EqualTo("PROTOCOL_ERROR"));
                    WaitUntil(() => server.State == ControlSessionState.Error);
                    Assert.That(receiver.Diagnostics.ClockSynchronized, Is.False);
                }
            }
        }

        [Test]
        public void ClockSyncOffsetJumpRequiresResetAndInvalidatesReceiverAge()
        {
            using (var receiver = CreatePoseReceiver())
            {
                receiver.Start();
                using (var server = CreatePoseAgeServer(receiver))
                using (var client = new TcpClient(AddressFamily.InterNetwork))
                {
                    client.Connect(IPAddress.Loopback, server.Start());
                    var stream = client.GetStream();
                    stream.ReadTimeout = 2000;
                    CompleteHandshake(stream, telemetry: true, poseAgeAccepted: true);
                    WriteJson(stream, ClockSyncOffset(21u, 1_000L));
                    WaitUntil(() => receiver.Diagnostics.ClockSynchronized);
                    WriteJson(stream, ClockSyncOffset(22u, 6_001L));

                    var error = ParseEnvelope(ReadJson(stream));
                    Assert.That(error.type, Is.EqualTo("error"));
                    Assert.That(error.code, Is.EqualTo("PROTOCOL_ERROR"));
                    WaitUntil(() => server.State == ControlSessionState.Error);
                    Assert.That(receiver.Diagnostics.ClockSynchronized, Is.False);
                    Assert.That(receiver.Diagnostics.OneWayAgeSampleCount, Is.Zero);
                }
            }
        }

        [Test]
        public void ClockSyncResetStartsANewOffsetEpoch()
        {
            using (var receiver = CreatePoseReceiver())
            {
                receiver.Start();
                using (var server = CreatePoseAgeServer(receiver))
                using (var client = new TcpClient(AddressFamily.InterNetwork))
                {
                    client.Connect(IPAddress.Loopback, server.Start());
                    var stream = client.GetStream();
                    stream.ReadTimeout = 2000;
                    CompleteHandshake(stream, telemetry: true, poseAgeAccepted: true);
                    WriteJson(stream, ClockSyncOffset(21u, 1_000L));
                    WaitUntil(() => receiver.Diagnostics.ClockSynchronized);
                    WriteJson(stream, Message("clock_sync_reset", 22u));
                    WaitUntil(() => !receiver.Diagnostics.ClockSynchronized);
                    WriteJson(stream, ClockSyncOffset(23u, 100_000L));
                    WaitUntil(() => receiver.Diagnostics.ClockSynchronized);

                    Assert.That(server.State, Is.EqualTo(ControlSessionState.Ready));
                    Assert.That(server.Diagnostics.ProtocolErrors, Is.Zero);
                }
            }
        }

        [Test]
        public void ClockSynchronizationClearsWhenThePeerDisconnects()
        {
            using (var receiver = CreatePoseReceiver())
            {
                receiver.Start();
                using (var server = CreatePoseAgeServer(receiver))
                using (var client = new TcpClient(AddressFamily.InterNetwork))
                {
                    client.Connect(IPAddress.Loopback, server.Start());
                    var stream = client.GetStream();
                    stream.ReadTimeout = 2000;
                    CompleteHandshake(stream, telemetry: true, poseAgeAccepted: true);
                    WriteJson(stream, ClockSyncOffset(21u, 1_000L));
                    WaitUntil(() => receiver.Diagnostics.ClockSynchronized);

                    WriteJson(stream, Message("goodbye", 22u));
                    WaitUntil(() => server.State == ControlSessionState.Listening);
                    Assert.That(receiver.Diagnostics.ClockSynchronized, Is.False);
                    Assert.That(receiver.Diagnostics.OneWayAgeSampleCount, Is.Zero);
                }
            }
        }

        [Test]
        public void PeerCloseWithoutGoodbyeReturnsToListening()
        {
            using (var server = new ControlServer(CreateConfig()))
            {
                var port = server.Start();
                using (var client = new TcpClient(AddressFamily.InterNetwork))
                {
                    client.Connect(IPAddress.Loopback, port);
                    var stream = client.GetStream();
                    stream.ReadTimeout = 2000;
                    WriteJson(stream, Hello());
                    Assert.That(ParseEnvelope(ReadJson(stream)).type, Is.EqualTo("accept"));
                }

                WaitUntil(() => server.State == ControlSessionState.Listening);
                Assert.That(server.IsRunning, Is.True);
                Assert.That(server.Diagnostics.ProtocolErrors, Is.Zero);
                var disconnected = false;
                while (server.TryDequeueEvent(out var controlEvent))
                    disconnected |= controlEvent.Kind == ControlServerEventKind.Disconnected;
                Assert.That(disconnected, Is.True);
            }
        }

        [Test]
        public void LegacyCapabilitiesDoNotEnableTelemetry()
        {
            using (var server = new ControlServer(CreateConfig(
                poseStatsInterval: TimeSpan.FromMilliseconds(25))))
            using (var client = new TcpClient(AddressFamily.InterNetwork))
            {
                client.Connect(IPAddress.Loopback, server.Start());
                var stream = client.GetStream();
                stream.ReadTimeout = 2000;
                CompleteHandshake(stream);
                Thread.Sleep(100);
                Assert.That(server.Diagnostics.PoseStatsFrames, Is.Zero);

                WriteJson(stream, ClockSyncPing(20u, 1_000_000L));
                var error = ParseEnvelope(ReadJson(stream));
                Assert.That(error.type, Is.EqualTo("error"));
                Assert.That(error.code, Is.EqualTo("PROTOCOL_ERROR"));
                Assert.That(server.Diagnostics.ClockSyncReplies, Is.Zero);
            }
        }

        [Test]
        public void InvalidJsonReturnsErrorAndStopsSession()
        {
            using (var server = new ControlServer(CreateConfig()))
            using (var client = new TcpClient(AddressFamily.InterNetwork))
            {
                client.Connect(IPAddress.Loopback, server.Start());
                var stream = client.GetStream();
                stream.ReadTimeout = 2000;
                WriteJson(stream, "{not-json}");

                var error = ParseEnvelope(ReadJson(stream));
                Assert.That(error.type, Is.EqualTo("error"));
                WaitUntil(() => server.State == ControlSessionState.Error);
            }
        }

        [Test]
        public void MissingHeartbeatTimesOutAndCleanDisposeReleasesPort()
        {
            var config = CreateConfig(
                pingInterval: TimeSpan.FromMilliseconds(50),
                timeout: TimeSpan.FromMilliseconds(180));
            var server = new ControlServer(config);
            var port = 0;
            try
            {
                port = server.Start();
                using (var client = new TcpClient(AddressFamily.InterNetwork))
                {
                    client.Connect(IPAddress.Loopback, port);
                    var stream = client.GetStream();
                    stream.ReadTimeout = 2000;
                    WriteJson(stream, Hello());
                    Assert.That(ParseEnvelope(ReadJson(stream)).type, Is.EqualTo("accept"));
                    WaitUntil(() => server.State == ControlSessionState.Error);
                    Assert.That(server.Diagnostics.HeartbeatTimeouts, Is.EqualTo(1));
                }
            }
            finally
            {
                server.Dispose();
                server.Dispose();
            }

            using (var rebound = new ControlServer(CreateConfig(localPort: port)))
            {
                Assert.That(rebound.Start(), Is.EqualTo(port));
                rebound.Stop();
                rebound.Stop();
                Assert.That(rebound.State, Is.EqualTo(ControlSessionState.Disconnected));
            }
            Assert.Throws<ObjectDisposedException>(() => server.Start());
        }

        [Test]
        public void ConfigRejectsNonLoopbackBind()
        {
            Assert.Throws<ArgumentException>(() => CreateConfig(bindAddress: IPAddress.Any));
        }

        private static ControlServerConfig CreateConfig(
            int localPort = 0,
            IPAddress bindAddress = null,
            TimeSpan? pingInterval = null,
            TimeSpan? timeout = null,
            TimeSpan? poseStatsInterval = null,
            int udpPort = 50321)
        {
            return new ControlServerConfig(
                SessionId,
                SkeletonId,
                SkeletonHash,
                1,
                udpPort,
                localPort,
                bindAddress,
                60,
                "6000.2.15f1",
                "0.1.0",
                95,
                new string('2', 64),
                "Character_A",
                1.03f,
                pingInterval ?? TimeSpan.FromSeconds(1),
                timeout ?? TimeSpan.FromSeconds(3),
                poseStatsInterval);
        }

        private static PoseUdpReceiver CreatePoseReceiver()
        {
            return new PoseUdpReceiver(new PoseUdpReceiverConfig(SessionId, 1, 1ul));
        }

        private static ControlServer CreatePoseAgeServer(PoseUdpReceiver receiver)
        {
            return new ControlServer(
                CreateConfig(udpPort: receiver.BoundPort),
                poseDiagnosticsProvider: () => receiver.Diagnostics,
                poseClockOffsetConsumer: receiver.TrySetClockSynchronization,
                poseClockSynchronizationResetter: receiver.ClearClockSynchronization);
        }

        private static void CompleteHandshake(
            NetworkStream stream,
            bool telemetry = false,
            bool poseAgeAccepted = false)
        {
            WriteJson(stream, Hello(telemetry: telemetry));
            var accept = ParseEnvelope(ReadJson(stream));
            Assert.That(accept.type, Is.EqualTo("accept"));
            CollectionAssert.DoesNotContain(accept.capabilities, "contacts-v1");
            if (telemetry)
            {
                CollectionAssert.Contains(accept.capabilities, "clock-sync-v1");
                CollectionAssert.Contains(accept.capabilities, "pose-stats-v1");
                if (poseAgeAccepted)
                    CollectionAssert.Contains(accept.capabilities, "one-way-pose-age-v1");
                else
                    CollectionAssert.DoesNotContain(accept.capabilities, "one-way-pose-age-v1");
            }
            else
            {
                CollectionAssert.DoesNotContain(accept.capabilities, "clock-sync-v1");
                CollectionAssert.DoesNotContain(accept.capabilities, "pose-stats-v1");
            }
            WriteJson(stream, Descriptor());
            Assert.That(ParseEnvelope(ReadJson(stream)).type, Is.EqualTo("ready"));
        }

        private static string ClockSyncPing(uint requestId, long t0Usec)
        {
            return "{" +
                "\"type\":\"clock_sync_ping\"," +
                "\"protocolMajor\":1," +
                "\"protocolMinor\":0," +
                $"\"sessionId\":{SessionId}," +
                $"\"requestId\":{requestId}," +
                $"\"t0BlenderSendUsec\":{t0Usec}" +
                "}";
        }

        private static string ClockSyncOffset(uint requestId, long offsetUsec)
        {
            return ClockSyncOffsetRaw(
                requestId,
                $"\"unityMinusBlenderOffsetUsec\":{offsetUsec}");
        }

        private static string ClockSyncOffsetRaw(uint requestId, string fieldJson)
        {
            var optionalField = string.IsNullOrEmpty(fieldJson)
                ? string.Empty
                : "," + fieldJson;
            return "{" +
                "\"type\":\"clock_sync_offset\"," +
                "\"protocolMajor\":1," +
                "\"protocolMinor\":0," +
                $"\"sessionId\":{SessionId}," +
                $"\"requestId\":{requestId}" +
                optionalField +
                "}";
        }

        private static byte[] EncodePosePacket(uint sequence)
        {
            var rotations = new[] { Quaternion.identity };
            var source = new PosePacketSource(
                SessionId,
                sequence,
                sequence * 1000ul,
                (int)sequence,
                PosePacketFlags.None,
                Vector3.zero,
                Quaternion.identity,
                Vector3.zero,
                1,
                1ul,
                rotations,
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
            var packet = new byte[MotionBridgeProtocol.PosePacketBytes(1)];
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

        private static void SendPose(
            UdpClient sender,
            IPEndPoint endpoint,
            byte[] packet)
        {
            Assert.That(
                sender.Send(packet, packet.Length, endpoint),
                Is.EqualTo(packet.Length));
        }

        private static string Hello(
            int requestedPoseHz = 60,
            bool telemetry = false,
            string skeletonHash = SkeletonHash)
        {
            var capabilities = telemetry
                ? "[\"contacts-v1\",\"global-rest-delta-v1\",\"clock-sync-v1\",\"pose-stats-v1\",\"one-way-pose-age-v1\"]"
                : "[\"contacts-v1\",\"global-rest-delta-v1\"]";
            return "{" +
                "\"type\":\"hello\"," +
                "\"protocolMajor\":1," +
                "\"protocolMinor\":0," +
                $"\"sessionId\":{SessionId}," +
                "\"requestId\":1," +
                "\"addonVersion\":\"0.1.0\"," +
                "\"blenderVersion\":\"5.0.1\"," +
                $"\"skeletonId\":\"{SkeletonId}\"," +
                $"\"skeletonHash\":\"{skeletonHash}\"," +
                "\"frameRate\":{\"numerator\":60,\"denominator\":1}," +
                "\"rootMode\":\"WorldRoot\"," +
                $"\"requestedPoseHz\":{requestedPoseHz}," +
                $"\"capabilities\":{capabilities}" +
                "}";
        }

        private static string Descriptor()
        {
            return "{" +
                "\"type\":\"skeleton_descriptor\"," +
                "\"protocolMajor\":1," +
                "\"protocolMinor\":0," +
                $"\"sessionId\":{SessionId}," +
                "\"requestId\":2," +
                $"\"skeletonId\":\"{SkeletonId}\"," +
                $"\"skeletonHash\":\"{SkeletonHash}\"," +
                "\"coordinateSpace\":\"unity-left-handed-y-up-z-forward\"," +
                "\"rootIndex\":0," +
                "\"joints\":[{" +
                    "\"index\":0," +
                    "\"name\":\"Root\"," +
                    "\"humanBone\":null," +
                    "\"parentIndex\":-1," +
                    "\"jointPath\":\"\"," +
                    "\"required\":true," +
                    "\"restLocalPosition\":[0,0,0]," +
                    "\"restLocalRotation\":[0,0,0,1]," +
                    "\"restGlobalRotation\":[0,0,0,1]" +
                "}]}";
        }

        private static string Message(string type, uint requestId)
        {
            return "{" +
                $"\"type\":\"{type}\"," +
                "\"protocolMajor\":1," +
                "\"protocolMinor\":0," +
                $"\"sessionId\":{SessionId}," +
                $"\"requestId\":{requestId}" +
                "}";
        }

        private static void WriteJson(NetworkStream stream, string json)
        {
            var frame = ControlFrameEncoder.EncodeJson(json);
            stream.Write(frame, 0, frame.Length);
        }

        private static string ReadJson(NetworkStream stream)
        {
            var header = ReadExact(stream, MotionBridgeProtocol.ControlLengthPrefixBytes);
            var payloadBytes =
                header[0] |
                (header[1] << 8) |
                (header[2] << 16) |
                (header[3] << 24);
            Assert.That(payloadBytes, Is.InRange(1, MotionBridgeProtocol.MaxControlPayloadBytes));
            return Encoding.UTF8.GetString(ReadExact(stream, payloadBytes));
        }

        private static byte[] ReadExact(NetworkStream stream, int count)
        {
            var result = new byte[count];
            var offset = 0;
            while (offset < count)
            {
                var read = stream.Read(result, offset, count - offset);
                if (read == 0)
                    throw new InvalidOperationException("TCP peer closed during test read.");
                offset += read;
            }
            return result;
        }

        private static TestEnvelope ParseEnvelope(string json)
        {
            return JsonUtility.FromJson<TestEnvelope>(json);
        }

        private static void WaitUntil(Func<bool> predicate)
        {
            Assert.That(
                SpinWait.SpinUntil(predicate, TimeSpan.FromSeconds(3)),
                Is.True,
                "Timed out waiting for TCP control state.");
        }

        [Serializable]
        private sealed class TestEnvelope
        {
            public string type;
            public uint requestId;
            public int udpPort;
            public int acceptedPoseHz;
            public string code;
            public string message;
            public long t0BlenderSendUsec;
            public long t1UnityReceiveUsec;
            public long t2UnitySendUsec;
            public uint lastSequence;
            public long received;
            public long droppedBySequence;
            public long rejected;
            public long receiverOverrun;
            public long oneWayAgeUsecP50;
            public long oneWayAgeUsecP95;
            public string[] capabilities;
        }
    }
}
