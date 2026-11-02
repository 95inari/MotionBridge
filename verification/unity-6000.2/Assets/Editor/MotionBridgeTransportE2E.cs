using System;
using System.IO;
using System.Threading;
using Kuko.MotionBridge.Protocol;
using Kuko.MotionBridge.Retargeting;
using Kuko.MotionBridge.Transport;
using UnityEngine;

namespace Kuko.MotionBridge.Verification
{
    public static class MotionBridgeTransportE2E
    {
        private const uint SessionId = 0x12345678u;

        public static void Run()
        {
            var portFile = RequiredArgument("--motion-bridge-port-file");
            var reportFile = RequiredArgument("--motion-bridge-report-file");
            var frame = new PoseFrame(CanonicalHumanoidV1.JointCount);
            var report = new TransportReport
            {
                unityVersion = Application.unityVersion,
                sessionId = SessionId,
            };

            using (var receiver = new PoseUdpReceiver(new PoseUdpReceiverConfig(
                       SessionId,
                       CanonicalHumanoidV1.JointCount,
                       CanonicalHumanoidV1.RequiredJointMask)))
            {
                var udpPort = receiver.Start();
                using (var server = new ControlServer(
                           new ControlServerConfig(
                               SessionId,
                               CanonicalHumanoidV1.SkeletonId,
                               CanonicalHumanoidV1.SkeletonHash,
                               CanonicalHumanoidV1.JointCount,
                               udpPort,
                               localPort: 0,
                               acceptedPoseHz: 60,
                               unityVersion: Application.unityVersion,
                               packageVersion: "0.1.0",
                               muscleCount: HumanTrait.MuscleCount,
                               muscleNamesHash: new string('a', 64),
                               targetName: "TransportE2E",
                               targetHumanScale: 1f),
                           poseDiagnosticsProvider: () => receiver.Diagnostics,
                           poseClockOffsetConsumer: receiver.TrySetClockSynchronization,
                           poseClockSynchronizationResetter:
                               receiver.ClearClockSynchronization))
                {
                    var controlPort = server.Start();
                    report.controlPort = controlPort;
                    report.udpPort = udpPort;
                    WriteJson(portFile, new PortInfo
                    {
                        sessionId = SessionId,
                        controlPort = controlPort,
                        udpPort = udpPort,
                    });

                    var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(20);
                    while (DateTime.UtcNow < deadline && !report.disconnected)
                    {
                        while (server.TryDequeueEvent(out var controlEvent))
                        {
                            if (controlEvent.Kind == ControlServerEventKind.Error)
                                throw new InvalidOperationException(controlEvent.Detail);
                            if (controlEvent.Kind == ControlServerEventKind.Ready)
                                report.ready = true;
                            if (controlEvent.Kind == ControlServerEventKind.Streaming)
                            {
                                report.streaming = true;
                                report.streamingEvents++;
                            }
                            if (controlEvent.Kind == ControlServerEventKind.Paused)
                                report.paused = true;
                            if (controlEvent.Kind == ControlServerEventKind.Disconnected)
                                report.disconnected = true;
                        }
                        if (receiver.TryConsumeLatest(frame))
                        {
                            report.receivedPose = true;
                            report.lastSequence = frame.Sequence;
                            report.lastFrameIndex = frame.FrameIndex;
                            report.lastRootPosition = frame.RootPosition;
                        }
                        var liveDiagnostics = receiver.Diagnostics;
                        if (liveDiagnostics.HasOneWayAge)
                        {
                            report.clockSynchronized = true;
                            report.oneWayAgeSamples = liveDiagnostics.OneWayAgeSampleCount;
                            report.oneWayAgeUsecP50 = liveDiagnostics.OneWayAgeUsecP50;
                            report.oneWayAgeUsecP95 = liveDiagnostics.OneWayAgeUsecP95;
                        }
                        Thread.Sleep(2);
                    }

                    var diagnostics = receiver.Diagnostics;
                    var controlDiagnostics = server.Diagnostics;
                    report.acceptedPackets = diagnostics.AcceptedPackets;
                    report.rejectedPackets =
                        diagnostics.DecodeRejectedPackets + diagnostics.SessionRejectedPackets;
                    report.clockSyncReplies = controlDiagnostics.ClockSyncReplies;
                    report.poseStatsFrames = controlDiagnostics.PoseStatsFrames;
                    if (!report.ready || !report.streaming || !report.receivedPose ||
                        !report.paused || report.streamingEvents < 2 ||
                        !report.disconnected || report.acceptedPackets != 3 ||
                        report.rejectedPackets != 0 || report.lastSequence != 2u ||
                        report.clockSyncReplies < 1 || report.poseStatsFrames < 1 ||
                        !report.clockSynchronized || report.oneWayAgeSamples < 1 ||
                        report.oneWayAgeUsecP95 < report.oneWayAgeUsecP50 ||
                        report.lastFrameIndex != 3 ||
                        Vector3.Distance(report.lastRootPosition, new Vector3(-0.2f, 0f, 0f)) >
                        0.0001f)
                    {
                        throw new InvalidOperationException(
                            "Cross-runtime transport did not complete: " +
                            JsonUtility.ToJson(report));
                    }
                }
            }

            WriteJson(reportFile, report);
            Debug.Log("MOTION_BRIDGE_CROSS_RUNTIME_E2E=" + JsonUtility.ToJson(report));
        }

        private static string RequiredArgument(string name)
        {
            var arguments = Environment.GetCommandLineArgs();
            for (var index = 0; index + 1 < arguments.Length; index++)
            {
                if (arguments[index] == name && !string.IsNullOrWhiteSpace(arguments[index + 1]))
                    return arguments[index + 1];
            }
            throw new ArgumentException("Missing required command-line argument: " + name);
        }

        private static void WriteJson(string path, object value)
        {
            var directory = Path.GetDirectoryName(Path.GetFullPath(path));
            if (!string.IsNullOrEmpty(directory))
                Directory.CreateDirectory(directory);
            File.WriteAllText(path, JsonUtility.ToJson(value));
        }

        [Serializable]
        private sealed class PortInfo
        {
            public uint sessionId;
            public int controlPort;
            public int udpPort;
        }

        [Serializable]
        private sealed class TransportReport
        {
            public string unityVersion;
            public uint sessionId;
            public int controlPort;
            public int udpPort;
            public bool ready;
            public bool streaming;
            public int streamingEvents;
            public bool paused;
            public bool receivedPose;
            public bool disconnected;
            public long acceptedPackets;
            public long rejectedPackets;
            public long clockSyncReplies;
            public long poseStatsFrames;
            public bool clockSynchronized;
            public int oneWayAgeSamples;
            public long oneWayAgeUsecP50;
            public long oneWayAgeUsecP95;
            public uint lastSequence;
            public int lastFrameIndex;
            public Vector3 lastRootPosition;
        }
    }
}
