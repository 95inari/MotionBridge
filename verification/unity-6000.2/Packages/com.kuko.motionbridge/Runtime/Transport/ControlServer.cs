using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using Kuko.MotionBridge.Protocol;
using UnityEngine;

namespace Kuko.MotionBridge.Transport
{
    public enum ControlSessionState
    {
        Disconnected = 0,
        Listening,
        Handshaking,
        Ready,
        Streaming,
        Paused,
        Error,
    }

    public enum ControlServerEventKind
    {
        Connected = 0,
        HelloAccepted,
        Ready,
        Streaming,
        Paused,
        Ping,
        Pong,
        ClockSync,
        PoseStats,
        Disconnected,
        Error,
    }

    public readonly struct ControlServerEvent
    {
        internal ControlServerEvent(
            ControlServerEventKind kind,
            ControlSessionState state,
            uint requestId,
            string detail)
        {
            Kind = kind;
            State = state;
            RequestId = requestId;
            Detail = detail;
        }

        public ControlServerEventKind Kind { get; }
        public ControlSessionState State { get; }
        public uint RequestId { get; }
        public string Detail { get; }
    }

    public readonly struct ControlServerDiagnostics
    {
        internal ControlServerDiagnostics(
            long acceptedConnections,
            long receivedFrames,
            long sentFrames,
            long protocolErrors,
            long heartbeatTimeouts,
            long clockSyncReplies,
            long poseStatsFrames,
            string lastError)
        {
            AcceptedConnections = acceptedConnections;
            ReceivedFrames = receivedFrames;
            SentFrames = sentFrames;
            ProtocolErrors = protocolErrors;
            HeartbeatTimeouts = heartbeatTimeouts;
            ClockSyncReplies = clockSyncReplies;
            PoseStatsFrames = poseStatsFrames;
            LastError = lastError;
        }

        public long AcceptedConnections { get; }
        public long ReceivedFrames { get; }
        public long SentFrames { get; }
        public long ProtocolErrors { get; }
        public long HeartbeatTimeouts { get; }
        public long ClockSyncReplies { get; }
        public long PoseStatsFrames { get; }
        public string LastError { get; }
    }

    public readonly struct ControlServerConfig
    {
        public ControlServerConfig(
            uint sessionId,
            string skeletonId,
            string skeletonHash,
            int expectedJointCount,
            int udpPort,
            int localPort = MotionBridgeProtocol.DefaultControlPort,
            IPAddress bindAddress = null,
            int acceptedPoseHz = 60,
            string unityVersion = "unknown",
            string packageVersion = MotionBridgeProtocol.PackageVersion,
            int muscleCount = 1,
            string muscleNamesHash = null,
            string targetName = "Motion Bridge Target",
            float targetHumanScale = 1f,
            TimeSpan? pingInterval = null,
            TimeSpan? timeout = null,
            TimeSpan? poseStatsInterval = null)
        {
            if (string.IsNullOrWhiteSpace(skeletonId))
                throw new ArgumentException("Skeleton ID is required.", nameof(skeletonId));
            if (!IsSha256Hex(skeletonHash))
                throw new ArgumentException(
                    "Skeleton hash must be 64 lowercase hexadecimal characters.",
                    nameof(skeletonHash));
            if (expectedJointCount < 1 ||
                expectedJointCount > MotionBridgeProtocol.MaxDescriptorJointCount)
                throw new ArgumentOutOfRangeException(nameof(expectedJointCount));
            if (udpPort < 1 || udpPort > ushort.MaxValue)
                throw new ArgumentOutOfRangeException(nameof(udpPort));
            if (localPort < 0 || localPort > ushort.MaxValue)
                throw new ArgumentOutOfRangeException(nameof(localPort));
            var resolvedAddress = bindAddress ?? IPAddress.Loopback;
            if (!IPAddress.IsLoopback(resolvedAddress) ||
                resolvedAddress.AddressFamily != AddressFamily.InterNetwork)
            {
                throw new ArgumentException(
                    "TCP control server may only bind to IPv4 loopback.",
                    nameof(bindAddress));
            }
            if (acceptedPoseHz != 30 && acceptedPoseHz != 60)
                throw new ArgumentOutOfRangeException(nameof(acceptedPoseHz));
            if (string.IsNullOrWhiteSpace(unityVersion))
                throw new ArgumentException("Unity version is required.", nameof(unityVersion));
            if (string.IsNullOrWhiteSpace(packageVersion))
                throw new ArgumentException("Package version is required.", nameof(packageVersion));
            if (muscleCount < 1)
                throw new ArgumentOutOfRangeException(nameof(muscleCount));
            var resolvedMuscleHash = muscleNamesHash ?? new string('0', 64);
            if (!IsSha256Hex(resolvedMuscleHash))
                throw new ArgumentException(
                    "Muscle names hash must be 64 lowercase hexadecimal characters.",
                    nameof(muscleNamesHash));
            if (string.IsNullOrWhiteSpace(targetName))
                throw new ArgumentException("Target name is required.", nameof(targetName));
            if (float.IsNaN(targetHumanScale) ||
                float.IsInfinity(targetHumanScale) ||
                targetHumanScale <= 0f)
                throw new ArgumentOutOfRangeException(nameof(targetHumanScale));

            var resolvedPingInterval = pingInterval ?? TimeSpan.FromSeconds(2);
            var resolvedTimeout = timeout ?? TimeSpan.FromSeconds(5);
            var resolvedPoseStatsInterval =
                poseStatsInterval ?? TimeSpan.FromMilliseconds(500);
            if (resolvedPingInterval <= TimeSpan.Zero)
                throw new ArgumentOutOfRangeException(nameof(pingInterval));
            if (resolvedTimeout <= resolvedPingInterval)
                throw new ArgumentException(
                    "Timeout must be greater than ping interval.",
                    nameof(timeout));
            if (resolvedPoseStatsInterval <= TimeSpan.Zero)
                throw new ArgumentOutOfRangeException(nameof(poseStatsInterval));

            SessionId = sessionId;
            SkeletonId = skeletonId;
            SkeletonHash = skeletonHash;
            ExpectedJointCount = expectedJointCount;
            UdpPort = udpPort;
            LocalPort = localPort;
            BindAddress = resolvedAddress;
            AcceptedPoseHz = acceptedPoseHz;
            UnityVersion = unityVersion;
            PackageVersion = packageVersion;
            MuscleCount = muscleCount;
            MuscleNamesHash = resolvedMuscleHash;
            TargetName = targetName;
            TargetHumanScale = targetHumanScale;
            PingInterval = resolvedPingInterval;
            Timeout = resolvedTimeout;
            PoseStatsInterval = resolvedPoseStatsInterval;
        }

        public uint SessionId { get; }
        public string SkeletonId { get; }
        public string SkeletonHash { get; }
        public int ExpectedJointCount { get; }
        public int UdpPort { get; }
        public int LocalPort { get; }
        public IPAddress BindAddress { get; }
        public int AcceptedPoseHz { get; }
        public string UnityVersion { get; }
        public string PackageVersion { get; }
        public int MuscleCount { get; }
        public string MuscleNamesHash { get; }
        public string TargetName { get; }
        public float TargetHumanScale { get; }
        public TimeSpan PingInterval { get; }
        public TimeSpan Timeout { get; }
        public TimeSpan PoseStatsInterval { get; }

        private static bool IsSha256Hex(string value)
        {
            if (value == null || value.Length != 64)
                return false;
            for (var i = 0; i < value.Length; i++)
            {
                var character = value[i];
                if (!((character >= '0' && character <= '9') ||
                      (character >= 'a' && character <= 'f')))
                    return false;
            }
            return true;
        }
    }

    /// <summary>
    /// Loopback-only TCP control listener. Socket waits, framing, and JSON parsing
    /// stay on one background worker. Main-thread callers only read atomic state or
    /// dequeue low-frequency immutable events. An optional descriptor validator is
    /// invoked on that worker and therefore must not call Unity APIs.
    /// </summary>
    public sealed class ControlServer : IDisposable
    {
        private enum HandshakePhase
        {
            AwaitHello = 0,
            AwaitDescriptor,
            Complete,
        }

        private readonly object _lifecycleSync = new object();
        private readonly ControlServerConfig _config;
        private readonly Func<string, bool> _descriptorValidator;
        private readonly Func<PoseUdpReceiverDiagnostics> _poseDiagnosticsProvider;
        private readonly Func<long, bool> _poseClockOffsetConsumer;
        private readonly Action _poseClockSynchronizationResetter;
        private readonly ConcurrentQueue<ControlServerEvent> _events =
            new ConcurrentQueue<ControlServerEvent>();
        private readonly byte[] _receiveBuffer = new byte[8192];

        private Socket _listener;
        private Socket _client;
        private Thread _worker;
        private int _boundPort;
        private int _state;
        private int _isRunning;
        private int _stopRequested;
        private int _isDisposed;
        private long _acceptedConnections;
        private long _receivedFrames;
        private long _sentFrames;
        private long _protocolErrors;
        private long _heartbeatTimeouts;
        private long _clockSyncReplies;
        private long _poseStatsFrames;
        private string _lastError;
        private uint _nextServerRequestId = 0x80000000u;
        private uint? _pendingPingRequestId;
        private uint _lastRequestId;
        private bool _clockSyncEnabled;
        private bool _poseStatsEnabled;
        private bool _poseAgeEnabled;

        public ControlServer(
            ControlServerConfig config,
            Func<string, bool> descriptorValidator = null,
            Func<PoseUdpReceiverDiagnostics> poseDiagnosticsProvider = null,
            Func<long, bool> poseClockOffsetConsumer = null,
            Action poseClockSynchronizationResetter = null)
        {
            if (config.BindAddress == null ||
                string.IsNullOrEmpty(config.SkeletonId) ||
                string.IsNullOrEmpty(config.SkeletonHash))
                throw new ArgumentException("Control server config is not initialized.", nameof(config));
            if ((poseClockOffsetConsumer == null) !=
                (poseClockSynchronizationResetter == null))
            {
                throw new ArgumentException(
                    "Pose clock offset and reset callbacks must be supplied together.");
            }
            _config = config;
            _descriptorValidator = descriptorValidator;
            _poseDiagnosticsProvider = poseDiagnosticsProvider;
            _poseClockOffsetConsumer = poseClockOffsetConsumer;
            _poseClockSynchronizationResetter = poseClockSynchronizationResetter;
            Volatile.Write(ref _state, (int)ControlSessionState.Disconnected);
        }

        public ControlServerConfig Config => _config;

        public int BoundPort => Volatile.Read(ref _boundPort);

        public bool IsRunning => Volatile.Read(ref _isRunning) != 0;

        public ControlSessionState State =>
            (ControlSessionState)Volatile.Read(ref _state);

        public ControlServerDiagnostics Diagnostics => new ControlServerDiagnostics(
            Interlocked.Read(ref _acceptedConnections),
            Interlocked.Read(ref _receivedFrames),
            Interlocked.Read(ref _sentFrames),
            Interlocked.Read(ref _protocolErrors),
            Interlocked.Read(ref _heartbeatTimeouts),
            Interlocked.Read(ref _clockSyncReplies),
            Interlocked.Read(ref _poseStatsFrames),
            Volatile.Read(ref _lastError));

        public bool TryDequeueEvent(out ControlServerEvent controlEvent)
        {
            return _events.TryDequeue(out controlEvent);
        }

        public int Start()
        {
            lock (_lifecycleSync)
            {
                ThrowIfDisposed();
                if (_worker != null)
                {
                    if (IsRunning)
                        return BoundPort;
                    if (!_worker.Join(1000))
                        throw new InvalidOperationException("Previous control worker has not exited.");
                    _worker = null;
                    _listener?.Dispose();
                    _listener = null;
                    _client?.Dispose();
                    _client = null;
                }

                var listener = new Socket(
                    AddressFamily.InterNetwork,
                    SocketType.Stream,
                    ProtocolType.Tcp);
                try
                {
                    listener.NoDelay = true;
                    listener.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
                    listener.Bind(new IPEndPoint(_config.BindAddress, _config.LocalPort));
                    listener.Listen(1);
                    var endpoint = (IPEndPoint)listener.LocalEndPoint;
                    _listener = listener;
                    Volatile.Write(ref _boundPort, endpoint.Port);
                    Volatile.Write(ref _stopRequested, 0);
                    Volatile.Write(ref _isRunning, 1);
                    SetState(ControlSessionState.Listening);
                    _worker = new Thread(WorkerLoop)
                    {
                        IsBackground = true,
                        Name = "MotionBridge TCP Control Server",
                    };
                    _worker.Start();
                    return endpoint.Port;
                }
                catch
                {
                    listener.Dispose();
                    _listener = null;
                    Volatile.Write(ref _boundPort, 0);
                    Volatile.Write(ref _isRunning, 0);
                    SetState(ControlSessionState.Disconnected);
                    throw;
                }
            }
        }

        public void Stop()
        {
            Thread worker;
            lock (_lifecycleSync)
            {
                Volatile.Write(ref _stopRequested, 1);
                worker = _worker;
                CloseSocket(ref _client);
                CloseSocket(ref _listener);
            }

            if (worker != null && worker != Thread.CurrentThread && !worker.Join(1500))
                throw new InvalidOperationException("TCP control worker did not stop in time.");

            lock (_lifecycleSync)
            {
                if (_worker == worker)
                    _worker = null;
                Volatile.Write(ref _isRunning, 0);
                Volatile.Write(ref _boundPort, 0);
                _pendingPingRequestId = null;
                SetState(ControlSessionState.Disconnected);
            }
        }

        public void Dispose()
        {
            if (Interlocked.Exchange(ref _isDisposed, 1) != 0)
                return;
            Stop();
        }

        private void WorkerLoop()
        {
            try
            {
                while (Volatile.Read(ref _stopRequested) == 0)
                {
                    Socket client;
                    try
                    {
                        client = _listener.Accept();
                    }
                    catch (ObjectDisposedException) when (Volatile.Read(ref _stopRequested) != 0)
                    {
                        return;
                    }
                    catch (SocketException) when (Volatile.Read(ref _stopRequested) != 0)
                    {
                        return;
                    }

                    _client = client;
                    Interlocked.Increment(ref _acceptedConnections);
                    SetState(ControlSessionState.Handshaking);
                    Enqueue(ControlServerEventKind.Connected, 0, null);
                    try
                    {
                        ConfigureClient(client);
                        if (ProcessClient(client))
                        {
                            Enqueue(ControlServerEventKind.Disconnected, _lastRequestId, "goodbye");
                            SetState(ControlSessionState.Listening);
                        }
                    }
                    catch (TimeoutException exception)
                    {
                        Interlocked.Increment(ref _heartbeatTimeouts);
                        TrySendError(client, "HEARTBEAT_TIMEOUT", exception.Message, false);
                        Fail(exception);
                        return;
                    }
                    catch (ControlProtocolException exception)
                    {
                        Interlocked.Increment(ref _protocolErrors);
                        TrySendError(client, exception.Code, exception.Message, false);
                        Fail(exception);
                        return;
                    }
                    catch (Exception exception) when (Volatile.Read(ref _stopRequested) == 0)
                    {
                        Fail(exception);
                        return;
                    }
                    finally
                    {
                        try
                        {
                            _poseClockSynchronizationResetter?.Invoke();
                        }
                        finally
                        {
                            CloseSocket(ref _client);
                        }
                    }
                }
            }
            catch (Exception exception) when (Volatile.Read(ref _stopRequested) == 0)
            {
                Fail(exception);
            }
            finally
            {
                CloseSocket(ref _client);
                CloseSocket(ref _listener);
                Volatile.Write(ref _isRunning, 0);
                Volatile.Write(ref _boundPort, 0);
            }
        }

        private bool ProcessClient(Socket client)
        {
            var decoder = new ControlFrameDecoder();
            var phase = HandshakePhase.AwaitHello;
            var lastReceive = Stopwatch.GetTimestamp();
            var lastPing = lastReceive;
            var lastPoseStats = lastReceive;
            _pendingPingRequestId = null;
            _clockSyncEnabled = false;
            _poseStatsEnabled = false;
            _poseAgeEnabled = false;
            _poseClockSynchronizationResetter?.Invoke();

            while (Volatile.Read(ref _stopRequested) == 0)
            {
                try
                {
                    var received = client.Receive(_receiveBuffer, 0, _receiveBuffer.Length, SocketFlags.None);
                    if (received == 0)
                        return true;
                    lastReceive = Stopwatch.GetTimestamp();
                    var receiveUsec = MonotonicUsec(lastReceive);
                    decoder.Append(_receiveBuffer, 0, received);
                    while (decoder.TryDequeue(out var json))
                    {
                        Interlocked.Increment(ref _receivedFrames);
                        if (!HandleMessage(client, json, ref phase, receiveUsec))
                            return true;
                    }
                }
                catch (SocketException exception) when (
                    exception.SocketErrorCode == SocketError.TimedOut ||
                    exception.SocketErrorCode == SocketError.WouldBlock)
                {
                }
                catch (SocketException exception) when (
                    exception.SocketErrorCode == SocketError.ConnectionReset ||
                    exception.SocketErrorCode == SocketError.ConnectionAborted ||
                    exception.SocketErrorCode == SocketError.Shutdown ||
                    exception.SocketErrorCode == SocketError.NotConnected)
                {
                    return true;
                }
                catch (ObjectDisposedException) when (Volatile.Read(ref _stopRequested) != 0)
                {
                    return false;
                }
                catch (SocketException) when (Volatile.Read(ref _stopRequested) != 0)
                {
                    return false;
                }

                var now = Stopwatch.GetTimestamp();
                if (Elapsed(lastReceive, now) > _config.Timeout)
                    throw new TimeoutException("TCP control heartbeat was silent for too long.");
                if (phase != HandshakePhase.AwaitHello &&
                    _pendingPingRequestId == null &&
                    Elapsed(lastPing, now) >= _config.PingInterval)
                {
                    var requestId = _nextServerRequestId++;
                    SendJson(client, JsonUtility.ToJson(new SimpleControlMessage(
                        "ping",
                        _config.SessionId,
                        requestId)));
                    _pendingPingRequestId = requestId;
                    lastPing = now;
                    Enqueue(ControlServerEventKind.Ping, requestId, null);
                }
                if (phase == HandshakePhase.Complete &&
                    _poseStatsEnabled &&
                    Elapsed(lastPoseStats, now) >= _config.PoseStatsInterval)
                {
                    SendPoseStats(client);
                    lastPoseStats = now;
                }
            }
            return false;
        }

        private bool HandleMessage(
            Socket client,
            string json,
            ref HandshakePhase phase,
            long receiveUsec)
        {
            var envelope = ParseJson<ControlEnvelope>(json);
            ValidateEnvelope(envelope);
            _lastRequestId = envelope.requestId;

            if (envelope.type == "ping")
            {
                if (phase == HandshakePhase.AwaitHello)
                    throw new ControlProtocolException("ping is not valid before hello.");
                SendJson(client, JsonUtility.ToJson(new SimpleControlMessage(
                    "pong",
                    _config.SessionId,
                    envelope.requestId)));
                Enqueue(ControlServerEventKind.Pong, envelope.requestId, null);
                return true;
            }
            if (envelope.type == "pong")
            {
                if (_pendingPingRequestId != envelope.requestId)
                    throw new ControlProtocolException(
                        "pong requestId does not match the pending ping.");
                _pendingPingRequestId = null;
                Enqueue(ControlServerEventKind.Pong, envelope.requestId, null);
                return true;
            }
            if (envelope.type == "clock_sync_ping")
            {
                if (phase == HandshakePhase.AwaitHello)
                    throw new ControlProtocolException(
                        "clock_sync_ping is not valid before hello.");
                if (!_clockSyncEnabled)
                    throw new ControlProtocolException(
                        "clock_sync_ping capability was not negotiated.");
                var clockPing = ParseJson<ClockSyncPingControlMessage>(json);
                if (clockPing.t0BlenderSendUsec <= 0)
                    throw new ControlProtocolException(
                        "clock_sync_ping t0BlenderSendUsec must be positive.");
                var response = new ClockSyncPongControlMessage
                {
                    type = "clock_sync_pong",
                    protocolMajor = MotionBridgeProtocol.Major,
                    protocolMinor = MotionBridgeProtocol.Minor,
                    sessionId = _config.SessionId,
                    requestId = envelope.requestId,
                    t0BlenderSendUsec = clockPing.t0BlenderSendUsec,
                    t1UnityReceiveUsec = receiveUsec,
                    t2UnitySendUsec = MonotonicUsec(Stopwatch.GetTimestamp()),
                };
                SendJson(client, JsonUtility.ToJson(response));
                Interlocked.Increment(ref _clockSyncReplies);
                Enqueue(ControlServerEventKind.ClockSync, envelope.requestId, null);
                return true;
            }
            if (envelope.type == "clock_sync_offset")
            {
                ValidatePoseAgeMessageState(phase, "clock_sync_offset");
                if (!TryReadStrictInt64Field(
                        json,
                        "unityMinusBlenderOffsetUsec",
                        out var offsetUsec))
                {
                    throw new ControlProtocolException(
                        "clock_sync_offset unityMinusBlenderOffsetUsec must be exactly one signed int64.");
                }

                bool accepted;
                try
                {
                    accepted = _poseClockOffsetConsumer(offsetUsec);
                }
                catch (Exception exception)
                {
                    throw new ControlProtocolException(
                        "Pose receiver rejected clock synchronization.",
                        exception);
                }
                if (!accepted)
                {
                    throw new ControlProtocolException(
                        "clock_sync_offset jumped by more than the allowed tolerance; reset is required.");
                }
                Enqueue(ControlServerEventKind.ClockSync, envelope.requestId, "offset");
                return true;
            }
            if (envelope.type == "clock_sync_reset")
            {
                ValidatePoseAgeMessageState(phase, "clock_sync_reset");
                try
                {
                    _poseClockSynchronizationResetter();
                }
                catch (Exception exception)
                {
                    throw new ControlProtocolException(
                        "Pose receiver failed to reset clock synchronization.",
                        exception);
                }
                Enqueue(ControlServerEventKind.ClockSync, envelope.requestId, "reset");
                return true;
            }
            if (envelope.type == "goodbye")
            {
                if (phase == HandshakePhase.AwaitHello)
                    throw new ControlProtocolException("goodbye is not valid before hello.");
                return false;
            }
            if (phase == HandshakePhase.AwaitHello && envelope.type == "hello")
            {
                HandleHello(client, json, envelope.requestId);
                phase = HandshakePhase.AwaitDescriptor;
                return true;
            }
            if (phase == HandshakePhase.AwaitDescriptor &&
                envelope.type == "skeleton_descriptor")
            {
                HandleDescriptor(client, json, envelope.requestId);
                phase = HandshakePhase.Complete;
                return true;
            }
            if (phase == HandshakePhase.Complete && envelope.type == "resume")
            {
                if (State != ControlSessionState.Ready && State != ControlSessionState.Paused)
                    throw new ControlProtocolException("resume requires Ready or Paused state.");
                SetState(ControlSessionState.Streaming);
                Enqueue(ControlServerEventKind.Streaming, envelope.requestId, null);
                return true;
            }
            if (phase == HandshakePhase.Complete && envelope.type == "pause")
            {
                if (State != ControlSessionState.Streaming)
                    throw new ControlProtocolException("pause requires Streaming state.");
                SetState(ControlSessionState.Paused);
                Enqueue(ControlServerEventKind.Paused, envelope.requestId, null);
                return true;
            }
            throw new ControlProtocolException(
                $"Message '{envelope.type}' is invalid in {State} state.");
        }

        private void HandleHello(Socket client, string json, uint requestId)
        {
            var hello = ParseJson<HelloControlMessage>(json);
            if (hello.skeletonId != _config.SkeletonId ||
                hello.skeletonHash != _config.SkeletonHash)
            {
                throw new ControlProtocolException(
                    "SKELETON_HASH_MISMATCH",
                    "hello canonical skeleton does not match; " +
                    $"expected id={IdentityPart(_config.SkeletonId)}, " +
                    $"hash={IdentityPart(_config.SkeletonHash)}, " +
                    $"received id={IdentityPart(hello.skeletonId)}, " +
                    $"hash={IdentityPart(hello.skeletonHash)}.");
            }
            if (string.IsNullOrWhiteSpace(hello.addonVersion) ||
                string.IsNullOrWhiteSpace(hello.blenderVersion))
                throw new ControlProtocolException("hello version fields are required.");
            if (hello.frameRate == null ||
                hello.frameRate.numerator < 1 ||
                hello.frameRate.denominator < 1)
                throw new ControlProtocolException("hello frameRate must be positive.");
            if (hello.rootMode != "WorldRoot" && hello.rootMode != "InPlace")
                throw new ControlProtocolException("hello rootMode is unsupported.");
            if (hello.requestedPoseHz != 30 && hello.requestedPoseHz != 60)
                throw new ControlProtocolException("hello requestedPoseHz must be 30 or 60.");
            if (hello.capabilities == null)
                throw new ControlProtocolException("hello capabilities are required.");
            var offeredCapabilities = new HashSet<string>(StringComparer.Ordinal);
            foreach (var capability in hello.capabilities)
            {
                if (string.IsNullOrWhiteSpace(capability) ||
                    !offeredCapabilities.Add(capability))
                    throw new ControlProtocolException(
                        "hello capabilities must be unique non-empty strings.");
            }
            _clockSyncEnabled = offeredCapabilities.Contains("clock-sync-v1");
            _poseStatsEnabled = offeredCapabilities.Contains("pose-stats-v1");
            _poseAgeEnabled =
                offeredCapabilities.Contains("one-way-pose-age-v1") &&
                _clockSyncEnabled &&
                _poseStatsEnabled &&
                _poseDiagnosticsProvider != null &&
                _poseClockOffsetConsumer != null &&
                _poseClockSynchronizationResetter != null;
            var acceptedCapabilities = new List<string>();
            AddCapabilityIfOffered(
                offeredCapabilities,
                acceptedCapabilities,
                "global-rest-delta-v1");
            AddCapabilityIfOffered(
                offeredCapabilities,
                acceptedCapabilities,
                "clock-sync-v1");
            AddCapabilityIfOffered(
                offeredCapabilities,
                acceptedCapabilities,
                "pose-stats-v1");
            if (_poseAgeEnabled)
                acceptedCapabilities.Add("one-way-pose-age-v1");

            // Wire type "accept" is the v1 hello acknowledgement.
            var response = new AcceptControlMessage
            {
                type = "accept",
                protocolMajor = MotionBridgeProtocol.Major,
                protocolMinor = MotionBridgeProtocol.Minor,
                sessionId = _config.SessionId,
                requestId = requestId,
                unityVersion = _config.UnityVersion,
                packageVersion = _config.PackageVersion,
                udpHost = IPAddress.Loopback.ToString(),
                udpPort = _config.UdpPort,
                acceptedPoseHz = Math.Min(hello.requestedPoseHz, _config.AcceptedPoseHz),
                humanTrait = new HumanTraitControl
                {
                    muscleCount = _config.MuscleCount,
                    muscleNamesHash = _config.MuscleNamesHash,
                },
                capabilities = acceptedCapabilities.ToArray(),
            };
            SendJson(client, JsonUtility.ToJson(response));
            Enqueue(ControlServerEventKind.HelloAccepted, requestId, null);
        }

        private void HandleDescriptor(Socket client, string json, uint requestId)
        {
            var descriptor = ParseJson<SkeletonDescriptorControlMessage>(json);
            ValidateDescriptor(descriptor);
            if (_descriptorValidator != null)
            {
                bool accepted;
                try
                {
                    accepted = _descriptorValidator(json);
                }
                catch (Exception exception)
                {
                    throw new ControlProtocolException(
                        "Skeleton descriptor validator failed.",
                        exception);
                }
                if (!accepted)
                    throw new ControlProtocolException(
                        "Skeleton descriptor validator rejected the message.");
            }

            var response = new ReadyControlMessage
            {
                type = "ready",
                protocolMajor = MotionBridgeProtocol.Major,
                protocolMinor = MotionBridgeProtocol.Minor,
                sessionId = _config.SessionId,
                requestId = requestId,
                target = new ReadyTargetControl
                {
                    name = _config.TargetName,
                    avatarValid = true,
                    avatarHuman = true,
                    humanScale = _config.TargetHumanScale,
                },
            };
            SendJson(client, JsonUtility.ToJson(response));
            SetState(ControlSessionState.Ready);
            Enqueue(ControlServerEventKind.Ready, requestId, _config.TargetName);
        }

        private void ValidateEnvelope(ControlEnvelope envelope)
        {
            if (envelope == null || string.IsNullOrWhiteSpace(envelope.type))
                throw new ControlProtocolException("Control message type is required.");
            if (envelope.protocolMajor != MotionBridgeProtocol.Major)
                throw new ControlProtocolException("Control protocol major mismatch.");
            if (envelope.protocolMinor > MotionBridgeProtocol.Minor)
                throw new ControlProtocolException("Control protocol minor is unsupported.");
            if (envelope.sessionId != _config.SessionId)
                throw new ControlProtocolException("Control session ID mismatch.");
        }

        private void ValidatePoseAgeMessageState(
            HandshakePhase phase,
            string messageType)
        {
            if (phase != HandshakePhase.Complete ||
                (State != ControlSessionState.Ready &&
                 State != ControlSessionState.Streaming &&
                 State != ControlSessionState.Paused))
            {
                throw new ControlProtocolException(
                    $"{messageType} is only valid after the handshake.");
            }
            if (!_poseAgeEnabled ||
                _poseClockOffsetConsumer == null ||
                _poseClockSynchronizationResetter == null)
            {
                throw new ControlProtocolException(
                    $"{messageType} capability was not negotiated.");
            }
        }

        private void ValidateDescriptor(SkeletonDescriptorControlMessage descriptor)
        {
            if (descriptor.skeletonId != _config.SkeletonId ||
                descriptor.skeletonHash != _config.SkeletonHash)
                throw new ControlProtocolException(
                    "SKELETON_HASH_MISMATCH",
                    "Skeleton descriptor identity does not match.");
            if (descriptor.coordinateSpace != "unity-left-handed-y-up-z-forward" ||
                descriptor.rootIndex != 0)
                throw new ControlProtocolException("Skeleton descriptor coordinate space is invalid.");
            if (descriptor.joints == null ||
                descriptor.joints.Length != _config.ExpectedJointCount ||
                descriptor.joints.Length > MotionBridgeProtocol.MaxDescriptorJointCount)
                throw new ControlProtocolException("Skeleton descriptor joint count does not match.");

            var names = new HashSet<string>(StringComparer.Ordinal);
            var humanBones = new HashSet<string>(StringComparer.Ordinal);
            var paths = new string[descriptor.joints.Length];
            for (var index = 0; index < descriptor.joints.Length; index++)
            {
                var joint = descriptor.joints[index];
                if (joint == null || joint.index != index || string.IsNullOrWhiteSpace(joint.name))
                    throw new ControlProtocolException("Skeleton joint index or name is invalid.");
                if (!names.Add(joint.name))
                    throw new ControlProtocolException("Skeleton joint names must be unique.");
                if (!string.IsNullOrEmpty(joint.humanBone) && !humanBones.Add(joint.humanBone))
                    throw new ControlProtocolException("Skeleton humanBone values must be unique.");

                if (index == 0)
                {
                    if (joint.parentIndex != -1 || joint.jointPath != string.Empty)
                        throw new ControlProtocolException("Skeleton root hierarchy is invalid.");
                }
                else
                {
                    if (joint.parentIndex < 0 || joint.parentIndex >= index)
                        throw new ControlProtocolException("Skeleton parent must precede its child.");
                    var parentPath = paths[joint.parentIndex];
                    var expectedPath = string.IsNullOrEmpty(parentPath)
                        ? joint.name
                        : parentPath + "/" + joint.name;
                    if (joint.jointPath != expectedPath)
                        throw new ControlProtocolException("Skeleton jointPath is invalid.");
                }
                paths[index] = joint.jointPath;

                ValidateFiniteArray(joint.restLocalPosition, 3, "restLocalPosition");
                ValidateQuaternion(joint.restLocalRotation, "restLocalRotation");
                ValidateQuaternion(joint.restGlobalRotation, "restGlobalRotation");
            }
        }

        private static string IdentityPart(string value)
        {
            if (string.IsNullOrEmpty(value))
                return "<missing>";
            return value.Length <= 40 ? value : value.Substring(0, 12) + "...";
        }

        private static void ValidateFiniteArray(float[] values, int length, string field)
        {
            if (values == null || values.Length != length)
                throw new ControlProtocolException($"Skeleton {field} has an invalid length.");
            for (var index = 0; index < values.Length; index++)
            {
                if (float.IsNaN(values[index]) || float.IsInfinity(values[index]))
                    throw new ControlProtocolException($"Skeleton {field} must be finite.");
            }
        }

        private static void ValidateQuaternion(float[] value, string field)
        {
            ValidateFiniteArray(value, 4, field);
            var normSquared =
                value[0] * value[0] +
                value[1] * value[1] +
                value[2] * value[2] +
                value[3] * value[3];
            if (normSquared < MotionBridgeProtocol.MinimumNormalizedNormSquared ||
                normSquared > MotionBridgeProtocol.MaximumNormalizedNormSquared)
                throw new ControlProtocolException($"Skeleton {field} must be normalized.");
        }

        private void ConfigureClient(Socket client)
        {
            client.NoDelay = true;
            var timeoutMilliseconds = Math.Max(
                10,
                Math.Min(100, (int)(_config.PingInterval.TotalMilliseconds / 2)));
            client.ReceiveTimeout = timeoutMilliseconds;
            client.SendTimeout = Math.Max(100, (int)_config.Timeout.TotalMilliseconds);
        }

        private void SendPoseStats(Socket client)
        {
            var diagnostics = _poseDiagnosticsProvider != null
                ? _poseDiagnosticsProvider()
                : default;
            PoseStatsControlMessage response;
            if (_poseAgeEnabled && diagnostics.HasOneWayAge)
            {
                response = new PoseStatsWithAgeControlMessage
                {
                    oneWayAgeUsecP50 = diagnostics.OneWayAgeUsecP50,
                    oneWayAgeUsecP95 = diagnostics.OneWayAgeUsecP95,
                };
            }
            else
            {
                response = new PoseStatsControlMessage();
            }
            response.type = "pose_stats";
            response.protocolMajor = MotionBridgeProtocol.Major;
            response.protocolMinor = MotionBridgeProtocol.Minor;
            response.sessionId = _config.SessionId;
            response.requestId = _nextServerRequestId++;
            response.lastSequence = diagnostics.HasAcceptedSequence
                ? diagnostics.LastAcceptedSequence
                : 0u;
            response.received = Nonnegative(diagnostics.AcceptedPackets);
            response.droppedBySequence = Nonnegative(diagnostics.SequenceGapDrops);
            response.rejected = SaturatingAdd(
                Nonnegative(diagnostics.DecodeRejectedPackets),
                Nonnegative(diagnostics.SessionRejectedPackets));
            response.receiverOverrun = Nonnegative(diagnostics.ReceiverOverruns);
            SendJson(client, JsonUtility.ToJson(response));
            Interlocked.Increment(ref _poseStatsFrames);
            Enqueue(ControlServerEventKind.PoseStats, response.requestId, null);
        }

        private void SendJson(Socket client, string json)
        {
            var frame = ControlFrameEncoder.EncodeJson(json);
            var offset = 0;
            while (offset < frame.Length)
            {
                var sent = client.Send(frame, offset, frame.Length - offset, SocketFlags.None);
                if (sent <= 0)
                    throw new ControlProtocolException("TCP control send returned zero bytes.");
                offset += sent;
            }
            Interlocked.Increment(ref _sentFrames);
        }

        private void TrySendError(
            Socket client,
            string code,
            string message,
            bool recoverable)
        {
            if (client == null)
                return;
            try
            {
                SendJson(client, JsonUtility.ToJson(new ErrorControlMessage
                {
                    type = "error",
                    protocolMajor = MotionBridgeProtocol.Major,
                    protocolMinor = MotionBridgeProtocol.Minor,
                    sessionId = _config.SessionId,
                    requestId = _lastRequestId,
                    code = code,
                    message = message,
                    recoverable = recoverable,
                }));
            }
            catch
            {
            }
        }

        private static T ParseJson<T>(string json) where T : class
        {
            if (string.IsNullOrWhiteSpace(json))
                throw new ControlProtocolException("Control JSON must not be empty.");
            var trimmed = json.Trim();
            if (trimmed.Length < 2 || trimmed[0] != '{' || trimmed[trimmed.Length - 1] != '}')
                throw new ControlProtocolException("Control JSON root must be an object.");
            try
            {
                var value = JsonUtility.FromJson<T>(trimmed);
                if (value == null)
                    throw new ControlProtocolException("Control JSON object could not be decoded.");
                return value;
            }
            catch (ArgumentException exception)
            {
                throw new ControlProtocolException("Control JSON is malformed.", exception);
            }
        }

        private static bool TryReadStrictInt64Field(
            string json,
            string fieldName,
            out long value)
        {
            value = 0;
            if (!TryFindUniqueRootPropertyValue(
                    json,
                    fieldName,
                    out var index))
                return false;

            var negative = index < json.Length && json[index] == '-';
            if (negative)
                index++;
            var digitStart = index;
            if (index >= json.Length || json[index] < '0' || json[index] > '9')
                return false;
            if (json[index] == '0' && index + 1 < json.Length &&
                json[index + 1] >= '0' && json[index + 1] <= '9')
                return false;

            const ulong maxPositive = 0x7ffffffffffffffful;
            const ulong maxNegativeMagnitude = 0x8000000000000000ul;
            var limit = negative ? maxNegativeMagnitude : maxPositive;
            ulong magnitude = 0;
            while (index < json.Length && json[index] >= '0' && json[index] <= '9')
            {
                var digit = (ulong)(json[index] - '0');
                if (magnitude > (limit - digit) / 10ul)
                    return false;
                magnitude = magnitude * 10ul + digit;
                index++;
            }
            if (index == digitStart)
                return false;

            SkipJsonWhitespace(json, ref index);
            if (index >= json.Length || (json[index] != ',' && json[index] != '}'))
                return false;

            if (negative)
            {
                value = magnitude == maxNegativeMagnitude
                    ? long.MinValue
                    : -(long)magnitude;
            }
            else
            {
                value = (long)magnitude;
            }
            return true;
        }

        private static bool TryFindUniqueRootPropertyValue(
            string json,
            string fieldName,
            out int valueIndex)
        {
            valueIndex = -1;
            var depth = 0;
            var matches = 0;
            for (var index = 0; index < json.Length; index++)
            {
                var character = json[index];
                if (character == '"')
                {
                    var stringStart = index + 1;
                    var stringEnd = stringStart;
                    var hasEscape = false;
                    while (stringEnd < json.Length && json[stringEnd] != '"')
                    {
                        if (json[stringEnd] == '\\')
                        {
                            hasEscape = true;
                            stringEnd += 2;
                        }
                        else
                        {
                            stringEnd++;
                        }
                    }
                    if (stringEnd >= json.Length)
                        return false;

                    if (depth == 1 && !hasEscape &&
                        stringEnd - stringStart == fieldName.Length &&
                        string.CompareOrdinal(
                            json,
                            stringStart,
                            fieldName,
                            0,
                            fieldName.Length) == 0)
                    {
                        var candidateIndex = stringEnd + 1;
                        SkipJsonWhitespace(json, ref candidateIndex);
                        if (candidateIndex < json.Length &&
                            json[candidateIndex] == ':')
                        {
                            candidateIndex++;
                            SkipJsonWhitespace(json, ref candidateIndex);
                            matches++;
                            valueIndex = candidateIndex;
                        }
                    }
                    index = stringEnd;
                    continue;
                }

                if (character == '{' || character == '[')
                {
                    depth++;
                }
                else if (character == '}' || character == ']')
                {
                    depth--;
                    if (depth < 0)
                        return false;
                }
            }
            return depth == 0 && matches == 1 && valueIndex >= 0;
        }

        private static void SkipJsonWhitespace(string json, ref int index)
        {
            while (index < json.Length)
            {
                var character = json[index];
                if (character != ' ' && character != '\t' &&
                    character != '\r' && character != '\n')
                    return;
                index++;
            }
        }

        private static TimeSpan Elapsed(long start, long end)
        {
            return TimeSpan.FromSeconds((end - start) / (double)Stopwatch.Frequency);
        }

        private static long MonotonicUsec(long stopwatchTimestamp)
        {
            var wholeSeconds = stopwatchTimestamp / Stopwatch.Frequency;
            var remainder = stopwatchTimestamp % Stopwatch.Frequency;
            return checked(
                wholeSeconds * 1_000_000L +
                remainder * 1_000_000L / Stopwatch.Frequency);
        }

        private static long Nonnegative(long value)
        {
            return value < 0 ? 0 : value;
        }

        private static long SaturatingAdd(long left, long right)
        {
            return left > long.MaxValue - right ? long.MaxValue : left + right;
        }

        private static void AddCapabilityIfOffered(
            HashSet<string> offered,
            List<string> accepted,
            string capability)
        {
            if (offered.Contains(capability))
                accepted.Add(capability);
        }

        private void SetState(ControlSessionState state)
        {
            Volatile.Write(ref _state, (int)state);
        }

        private void Enqueue(
            ControlServerEventKind kind,
            uint requestId,
            string detail)
        {
            _events.Enqueue(new ControlServerEvent(kind, State, requestId, detail));
        }

        private void Fail(Exception exception)
        {
            Volatile.Write(ref _lastError, $"{exception.GetType().Name}: {exception.Message}");
            SetState(ControlSessionState.Error);
            Enqueue(ControlServerEventKind.Error, _lastRequestId, Volatile.Read(ref _lastError));
        }

        private void ThrowIfDisposed()
        {
            if (Volatile.Read(ref _isDisposed) != 0)
                throw new ObjectDisposedException(nameof(ControlServer));
        }

        private static void CloseSocket(ref Socket socket)
        {
            var current = Interlocked.Exchange(ref socket, null);
            if (current == null)
                return;
            try
            {
                current.Shutdown(SocketShutdown.Both);
            }
            catch
            {
            }
            current.Dispose();
        }

        [Serializable]
        private class ControlEnvelope
        {
            public string type;
            public byte protocolMajor;
            public byte protocolMinor;
            public uint sessionId;
            public uint requestId;
        }

        [Serializable]
        private sealed class SimpleControlMessage : ControlEnvelope
        {
            public SimpleControlMessage(string messageType, uint session, uint request)
            {
                type = messageType;
                protocolMajor = MotionBridgeProtocol.Major;
                protocolMinor = MotionBridgeProtocol.Minor;
                sessionId = session;
                requestId = request;
            }
        }

        [Serializable]
        private sealed class ClockSyncPingControlMessage : ControlEnvelope
        {
            public long t0BlenderSendUsec;
        }

        [Serializable]
        private sealed class ClockSyncPongControlMessage : ControlEnvelope
        {
            public long t0BlenderSendUsec;
            public long t1UnityReceiveUsec;
            public long t2UnitySendUsec;
        }

        [Serializable]
        private class PoseStatsControlMessage : ControlEnvelope
        {
            public uint lastSequence;
            public long received;
            public long droppedBySequence;
            public long rejected;
            public long receiverOverrun;
        }

        [Serializable]
        private sealed class PoseStatsWithAgeControlMessage : PoseStatsControlMessage
        {
            public long oneWayAgeUsecP50;
            public long oneWayAgeUsecP95;
        }

        [Serializable]
        private sealed class FrameRateControl
        {
            public int numerator;
            public int denominator;
        }

        [Serializable]
        private sealed class HelloControlMessage : ControlEnvelope
        {
            public string addonVersion;
            public string blenderVersion;
            public string skeletonId;
            public string skeletonHash;
            public FrameRateControl frameRate;
            public string rootMode;
            public int requestedPoseHz;
            public string[] capabilities;
        }

        [Serializable]
        private sealed class HumanTraitControl
        {
            public int muscleCount;
            public string muscleNamesHash;
        }

        [Serializable]
        private sealed class AcceptControlMessage : ControlEnvelope
        {
            public string unityVersion;
            public string packageVersion;
            public string udpHost;
            public int udpPort;
            public int acceptedPoseHz;
            public HumanTraitControl humanTrait;
            public string[] capabilities;
        }

        [Serializable]
        private sealed class SkeletonJointControl
        {
            public int index;
            public string name;
            public string humanBone;
            public int parentIndex;
            public string jointPath;
            public bool required;
            public float[] restLocalPosition;
            public float[] restLocalRotation;
            public float[] restGlobalRotation;
        }

        [Serializable]
        private sealed class SkeletonDescriptorControlMessage : ControlEnvelope
        {
            public string skeletonId;
            public string skeletonHash;
            public string coordinateSpace;
            public int rootIndex;
            public SkeletonJointControl[] joints;
        }

        [Serializable]
        private sealed class ReadyTargetControl
        {
            public string name;
            public bool avatarValid;
            public bool avatarHuman;
            public float humanScale;
        }

        [Serializable]
        private sealed class ReadyControlMessage : ControlEnvelope
        {
            public ReadyTargetControl target;
        }

        [Serializable]
        private sealed class ErrorControlMessage : ControlEnvelope
        {
            public string code;
            public string message;
            public bool recoverable;
        }
    }
}
