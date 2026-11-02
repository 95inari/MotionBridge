using System;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using Kuko.MotionBridge.Protocol;

namespace Kuko.MotionBridge.Transport
{
    public readonly struct PoseUdpReceiverConfig
    {
        public PoseUdpReceiverConfig(
            uint sessionId,
            int jointCount,
            ulong requiredJointMask,
            int localPort = 0,
            IPAddress bindAddress = null)
        {
            if (jointCount < 1 || jointCount > MotionBridgeProtocol.MaxPoseJointCount)
                throw new ArgumentOutOfRangeException(nameof(jointCount));
            var validMask = (1ul << jointCount) - 1ul;
            if ((requiredJointMask & ~validMask) != 0)
                throw new ArgumentOutOfRangeException(nameof(requiredJointMask));
            if (localPort < 0 || localPort > ushort.MaxValue)
                throw new ArgumentOutOfRangeException(nameof(localPort));

            var resolvedAddress = bindAddress ?? IPAddress.Loopback;
            if (!IPAddress.IsLoopback(resolvedAddress))
                throw new ArgumentException("Pose UDP receiver may only bind to loopback.", nameof(bindAddress));
            if (resolvedAddress.AddressFamily != AddressFamily.InterNetwork &&
                resolvedAddress.AddressFamily != AddressFamily.InterNetworkV6)
                throw new ArgumentException("Unsupported loopback address family.", nameof(bindAddress));

            SessionId = sessionId;
            JointCount = jointCount;
            RequiredJointMask = requiredJointMask;
            LocalPort = localPort;
            BindAddress = resolvedAddress;
        }

        public uint SessionId { get; }
        public int JointCount { get; }
        public ulong RequiredJointMask { get; }
        public int LocalPort { get; }
        public IPAddress BindAddress { get; }
    }

    public readonly struct PoseUdpReceiverDiagnostics
    {
        internal PoseUdpReceiverDiagnostics(
            long receivedDatagrams,
            long acceptedPackets,
            long decodeRejectedPackets,
            long sessionRejectedPackets,
            long stalePackets,
            long sequenceGapDrops,
            long supersededFrames,
            long receiverOverruns,
            long socketExceptions,
            long unexpectedExceptions,
            PoseDecodeError lastDecodeError,
            PoseSessionError lastSessionError,
            SocketError lastSocketError,
            bool hasAcceptedSequence,
            uint lastAcceptedSequence,
            bool clockSynchronized,
            int oneWayAgeSampleCount,
            long oneWayAgeUsecP50,
            long oneWayAgeUsecP95)
        {
            ReceivedDatagrams = receivedDatagrams;
            AcceptedPackets = acceptedPackets;
            DecodeRejectedPackets = decodeRejectedPackets;
            SessionRejectedPackets = sessionRejectedPackets;
            StalePackets = stalePackets;
            SequenceGapDrops = sequenceGapDrops;
            SupersededFrames = supersededFrames;
            ReceiverOverruns = receiverOverruns;
            SocketExceptions = socketExceptions;
            UnexpectedExceptions = unexpectedExceptions;
            LastDecodeError = lastDecodeError;
            LastSessionError = lastSessionError;
            LastSocketError = lastSocketError;
            HasAcceptedSequence = hasAcceptedSequence;
            LastAcceptedSequence = lastAcceptedSequence;
            ClockSynchronized = clockSynchronized;
            OneWayAgeSampleCount = oneWayAgeSampleCount;
            OneWayAgeUsecP50 = oneWayAgeUsecP50;
            OneWayAgeUsecP95 = oneWayAgeUsecP95;
        }

        public long ReceivedDatagrams { get; }
        public long AcceptedPackets { get; }
        public long DecodeRejectedPackets { get; }
        public long SessionRejectedPackets { get; }
        public long StalePackets { get; }
        public long SequenceGapDrops { get; }
        public long SupersededFrames { get; }
        public long ReceiverOverruns { get; }
        public long SocketExceptions { get; }
        public long UnexpectedExceptions { get; }
        public PoseDecodeError LastDecodeError { get; }
        public PoseSessionError LastSessionError { get; }
        public SocketError LastSocketError { get; }
        public bool HasAcceptedSequence { get; }
        public uint LastAcceptedSequence { get; }
        public bool ClockSynchronized { get; }
        public int OneWayAgeSampleCount { get; }
        public bool HasOneWayAge => ClockSynchronized && OneWayAgeSampleCount > 0;
        public long OneWayAgeUsecP50 { get; }
        public long OneWayAgeUsecP95 { get; }
    }

    /// <summary>
    /// Loopback-only UDP receiver with a background decode thread and a latest-only,
    /// allocation-free main-thread snapshot boundary.
    /// </summary>
    public sealed class PoseUdpReceiver : IDisposable
    {
        public const long MaximumClockOffsetJumpUsec = 5_000L;

        private const int SlotCount = 3;
        private const int OneWayAgeWindowCapacity = 256;
        private const int SlotFree = 0;
        private const int SlotWriting = 1;
        private const int SlotPublished = 2;
        private const int SlotReading = 3;

        private sealed class FrameSlot
        {
            public FrameSlot()
            {
                Frame = new PoseFrame(MotionBridgeProtocol.MaxPoseJointCount);
            }

            public readonly PoseFrame Frame;
            public int State;
        }

        private readonly object _lifecycleSync = new object();
        private readonly object _oneWayAgeSync = new object();
        private readonly PoseUdpReceiverConfig _config;
        private readonly PosePacketGate _gate;
        private readonly FrameSlot[] _slots;
        private readonly byte[] _receiveBuffer;
        private readonly long[] _oneWayAgeSamples = new long[OneWayAgeWindowCapacity];
        private readonly long[] _oneWayAgeSortBuffer = new long[OneWayAgeWindowCapacity];
        private readonly Func<long> _monotonicUsec;

        private Socket _socket;
        private Thread _receiveThread;
        private int _publishedIndex = -1;
        private int _nextWriteIndex;
        private int _boundPort;
        private int _isRunning;
        private int _stopRequested;
        private int _isDisposed;

        private int _hasAcceptedSequence;
        private int _lastAcceptedSequenceBits;

        private long _receivedDatagrams;
        private long _acceptedPackets;
        private long _decodeRejectedPackets;
        private long _sessionRejectedPackets;
        private long _stalePackets;
        private long _sequenceGapDrops;
        private long _supersededFrames;
        private long _receiverOverruns;
        private long _socketExceptions;
        private long _unexpectedExceptions;
        private int _lastDecodeError;
        private int _lastSessionError;
        private int _lastSocketError;
        private bool _clockSynchronized;
        private bool _clockResetRequired;
        private long _unityMinusBlenderOffsetUsec;
        private int _oneWayAgeWriteIndex;
        private int _oneWayAgeSampleCount;

        public PoseUdpReceiver(
            PoseUdpReceiverConfig config,
            Func<long> monotonicUsec = null)
        {
            if (config.JointCount < 1 ||
                config.JointCount > MotionBridgeProtocol.MaxPoseJointCount ||
                config.BindAddress == null)
                throw new ArgumentException("Pose UDP receiver config is not initialized.", nameof(config));

            _config = config;
            _gate = new PosePacketGate(
                config.SessionId,
                config.JointCount,
                config.RequiredJointMask);
            _receiveBuffer = new byte[MotionBridgeProtocol.MaxPacketBytes + 1];
            _monotonicUsec = monotonicUsec ?? CurrentMonotonicUsec;
            _slots = new FrameSlot[SlotCount];
            for (var i = 0; i < _slots.Length; i++)
                _slots[i] = new FrameSlot();
        }

        public PoseUdpReceiverConfig Config => _config;

        public int BoundPort => Volatile.Read(ref _boundPort);

        public bool IsRunning => Volatile.Read(ref _isRunning) != 0;

        public PoseUdpReceiverDiagnostics Diagnostics
        {
            get
            {
                bool clockSynchronized;
                int ageSampleCount;
                long ageP50;
                long ageP95;
                lock (_oneWayAgeSync)
                {
                    clockSynchronized = _clockSynchronized;
                    ageSampleCount = _oneWayAgeSampleCount;
                    if (clockSynchronized && ageSampleCount > 0)
                    {
                        Array.Copy(
                            _oneWayAgeSamples,
                            _oneWayAgeSortBuffer,
                            ageSampleCount);
                        Array.Sort(_oneWayAgeSortBuffer, 0, ageSampleCount);
                        ageP50 = PercentileNearestRank(
                            _oneWayAgeSortBuffer,
                            ageSampleCount,
                            50);
                        ageP95 = PercentileNearestRank(
                            _oneWayAgeSortBuffer,
                            ageSampleCount,
                            95);
                    }
                    else
                    {
                        ageP50 = 0;
                        ageP95 = 0;
                    }
                }

                return new PoseUdpReceiverDiagnostics(
                    Interlocked.Read(ref _receivedDatagrams),
                    Interlocked.Read(ref _acceptedPackets),
                    Interlocked.Read(ref _decodeRejectedPackets),
                    Interlocked.Read(ref _sessionRejectedPackets),
                    Interlocked.Read(ref _stalePackets),
                    Interlocked.Read(ref _sequenceGapDrops),
                    Interlocked.Read(ref _supersededFrames),
                    Interlocked.Read(ref _receiverOverruns),
                    Interlocked.Read(ref _socketExceptions),
                    Interlocked.Read(ref _unexpectedExceptions),
                    (PoseDecodeError)Volatile.Read(ref _lastDecodeError),
                    (PoseSessionError)Volatile.Read(ref _lastSessionError),
                    (SocketError)Volatile.Read(ref _lastSocketError),
                    Volatile.Read(ref _hasAcceptedSequence) != 0,
                    unchecked((uint)Volatile.Read(ref _lastAcceptedSequenceBits)),
                    clockSynchronized,
                    ageSampleCount,
                    ageP50,
                    ageP95);
            }
        }

        /// <summary>
        /// Installs the negotiated Unity-minus-Blender monotonic-clock offset. A jump
        /// larger than five milliseconds invalidates synchronization and must be
        /// preceded by ClearClockSynchronization before a new clock epoch is accepted.
        /// </summary>
        public bool TrySetClockSynchronization(long unityMinusBlenderOffsetUsec)
        {
            lock (_oneWayAgeSync)
            {
                if (_clockResetRequired)
                    return false;
                if (_clockSynchronized && OffsetJumpExceedsLimit(
                        _unityMinusBlenderOffsetUsec,
                        unityMinusBlenderOffsetUsec))
                {
                    ClearClockSynchronizationLocked();
                    _clockResetRequired = true;
                    return false;
                }

                if (_clockSynchronized)
                {
                    RebaseOneWayAgeSamplesLocked(
                        unityMinusBlenderOffsetUsec -
                        _unityMinusBlenderOffsetUsec);
                }
                _unityMinusBlenderOffsetUsec = unityMinusBlenderOffsetUsec;
                _clockSynchronized = true;
                return true;
            }
        }

        public void ClearClockSynchronization()
        {
            lock (_oneWayAgeSync)
                ClearClockSynchronizationLocked();
        }

        /// <summary>
        /// Starts the background receiver and returns the actual bound port. Repeated calls
        /// while running are idempotent. Port zero requests an ephemeral loopback port.
        /// </summary>
        public int Start()
        {
            lock (_lifecycleSync)
            {
                ThrowIfDisposed();
                if (_receiveThread != null)
                {
                    if (Volatile.Read(ref _stopRequested) != 0)
                        throw new InvalidOperationException("Pose UDP receiver is still stopping.");
                    if (IsRunning)
                        return BoundPort;
                    if (!_receiveThread.Join(1000))
                        throw new InvalidOperationException("Previous receive thread has not exited.");
                    _socket?.Dispose();
                    _socket = null;
                    _receiveThread = null;
                    Volatile.Write(ref _boundPort, 0);
                    ClearPublication();
                }

                ResetForStart();
                var socket = new Socket(
                    _config.BindAddress.AddressFamily,
                    SocketType.Dgram,
                    ProtocolType.Udp);
                try
                {
                    socket.ReceiveBufferSize = 64 * 1024;
                    socket.Bind(new IPEndPoint(_config.BindAddress, _config.LocalPort));
                    var endpoint = (IPEndPoint)socket.LocalEndPoint;

                    _socket = socket;
                    Volatile.Write(ref _boundPort, endpoint.Port);
                    Volatile.Write(ref _stopRequested, 0);
                    Volatile.Write(ref _isRunning, 1);
                    _receiveThread = new Thread(ReceiveLoop)
                    {
                        IsBackground = true,
                        Name = "MotionBridge Pose UDP Receiver",
                    };
                    _receiveThread.Start();
                    return endpoint.Port;
                }
                catch
                {
                    Volatile.Write(ref _isRunning, 0);
                    Volatile.Write(ref _boundPort, 0);
                    _receiveThread = null;
                    _socket = null;
                    socket.Dispose();
                    throw;
                }
            }
        }

        /// <summary>
        /// Copies the newest not-yet-consumed frame into caller-owned storage. The method
        /// performs no managed allocation and never exposes a slot still owned by the receiver.
        /// </summary>
        public bool TryConsumeLatest(PoseFrame destination)
        {
            if (destination == null)
                throw new ArgumentNullException(nameof(destination));
            if (destination.JointRotations.Length < _config.JointCount)
                throw new ArgumentException("Destination joint capacity is too small.", nameof(destination));

            for (var attempt = 0; attempt < SlotCount; attempt++)
            {
                var index = Volatile.Read(ref _publishedIndex);
                if (index < 0)
                    return false;

                var slot = _slots[index];
                if (Interlocked.CompareExchange(
                        ref slot.State,
                        SlotReading,
                        SlotPublished) != SlotPublished)
                {
                    if (Volatile.Read(ref _publishedIndex) != index)
                        continue;
                    return false;
                }

                if (Volatile.Read(ref _publishedIndex) != index)
                {
                    Volatile.Write(ref slot.State, SlotFree);
                    continue;
                }

                try
                {
                    CopyFrame(slot.Frame, destination);
                    return true;
                }
                finally
                {
                    Volatile.Write(ref slot.State, SlotFree);
                }
            }

            return false;
        }

        public void Stop()
        {
            StopCore(false);
        }

        public void Dispose()
        {
            StopCore(true);
        }

        private void ReceiveLoop()
        {
            var socket = _socket;
            try
            {
                while (Volatile.Read(ref _stopRequested) == 0)
                {
                    int packetBytes;
                    try
                    {
                        packetBytes = socket.Receive(
                            _receiveBuffer,
                            0,
                            _receiveBuffer.Length,
                            SocketFlags.None);
                    }
                    catch (ObjectDisposedException)
                    {
                        if (Volatile.Read(ref _stopRequested) == 0)
                            Interlocked.Increment(ref _unexpectedExceptions);
                        break;
                    }
                    catch (SocketException exception)
                    {
                        if (Volatile.Read(ref _stopRequested) != 0)
                            break;
                        Interlocked.Increment(ref _socketExceptions);
                        Volatile.Write(ref _lastSocketError, (int)exception.SocketErrorCode);
                        if (exception.SocketErrorCode == SocketError.Interrupted ||
                            exception.SocketErrorCode == SocketError.OperationAborted ||
                            exception.SocketErrorCode == SocketError.NotSocket)
                            break;
                        if (exception.SocketErrorCode == SocketError.ConnectionReset ||
                            exception.SocketErrorCode == SocketError.TimedOut ||
                            exception.SocketErrorCode == SocketError.WouldBlock)
                            continue;
                        break;
                    }

                    var arrivalUnityUsec = _monotonicUsec();
                    ProcessDatagram(packetBytes, arrivalUnityUsec);
                }
            }
            catch
            {
                Interlocked.Increment(ref _unexpectedExceptions);
            }
            finally
            {
                Volatile.Write(ref _isRunning, 0);
            }
        }

        private void ProcessDatagram(int packetBytes, long arrivalUnityUsec)
        {
            Interlocked.Increment(ref _receivedDatagrams);
            var slotIndex = AcquireWriteSlot();
            if (slotIndex < 0)
            {
                Interlocked.Increment(ref _receiverOverruns);
                return;
            }

            var slot = _slots[slotIndex];
            if (!_gate.TryDecodeAndAccept(
                    _receiveBuffer.AsSpan(0, packetBytes),
                    slot.Frame,
                    out var decodeError,
                    out var sessionError))
            {
                if (decodeError != PoseDecodeError.None)
                {
                    Interlocked.Increment(ref _decodeRejectedPackets);
                    Volatile.Write(ref _lastDecodeError, (int)decodeError);
                }
                else
                {
                    Interlocked.Increment(ref _sessionRejectedPackets);
                    Volatile.Write(ref _lastSessionError, (int)sessionError);
                    if (sessionError == PoseSessionError.StaleSequence)
                        Interlocked.Increment(ref _stalePackets);
                }

                Volatile.Write(ref slot.State, SlotFree);
                return;
            }

            RecordAcceptedSequence(slot.Frame.Sequence);
            RecordOneWayAge(slot.Frame.TimestampUsec, arrivalUnityUsec);
            Volatile.Write(ref slot.State, SlotPublished);
            var previousIndex = Interlocked.Exchange(ref _publishedIndex, slotIndex);
            if (previousIndex >= 0 && previousIndex != slotIndex &&
                Interlocked.CompareExchange(
                    ref _slots[previousIndex].State,
                    SlotFree,
                    SlotPublished) == SlotPublished)
                Interlocked.Increment(ref _supersededFrames);
            Interlocked.Increment(ref _acceptedPackets);
        }

        private int AcquireWriteSlot()
        {
            var currentPublished = Volatile.Read(ref _publishedIndex);
            for (var offset = 0; offset < SlotCount; offset++)
            {
                var index = (_nextWriteIndex + offset) % SlotCount;
                if (index == currentPublished)
                    continue;
                if (Interlocked.CompareExchange(
                        ref _slots[index].State,
                        SlotWriting,
                        SlotFree) != SlotFree)
                    continue;

                _nextWriteIndex = (index + 1) % SlotCount;
                return index;
            }

            return -1;
        }

        private void RecordAcceptedSequence(uint sequence)
        {
            if (Volatile.Read(ref _hasAcceptedSequence) != 0)
            {
                var lastSequence = unchecked(
                    (uint)Volatile.Read(ref _lastAcceptedSequenceBits));
                var delta = unchecked(sequence - lastSequence);
                if (delta > 1 && delta < 0x80000000u)
                    Interlocked.Add(ref _sequenceGapDrops, delta - 1u);
            }

            Volatile.Write(ref _lastAcceptedSequenceBits, unchecked((int)sequence));
            Volatile.Write(ref _hasAcceptedSequence, 1);
        }

        private void RecordOneWayAge(ulong blenderSendUsec, long arrivalUnityUsec)
        {
            if (blenderSendUsec > long.MaxValue)
                return;

            lock (_oneWayAgeSync)
            {
                if (!_clockSynchronized)
                    return;

                var unitySendUsec = SaturatingAdd(
                    (long)blenderSendUsec,
                    _unityMinusBlenderOffsetUsec);
                if (arrivalUnityUsec < unitySendUsec)
                    return;
                var ageUsec = SaturatingDifference(arrivalUnityUsec, unitySendUsec);
                _oneWayAgeSamples[_oneWayAgeWriteIndex] = ageUsec;
                _oneWayAgeWriteIndex =
                    (_oneWayAgeWriteIndex + 1) % OneWayAgeWindowCapacity;
                if (_oneWayAgeSampleCount < OneWayAgeWindowCapacity)
                    _oneWayAgeSampleCount++;
            }
        }

        private void StopCore(bool dispose)
        {
            lock (_lifecycleSync)
            {
                if (Volatile.Read(ref _isDisposed) != 0)
                    return;
                if (dispose)
                    Volatile.Write(ref _isDisposed, 1);

                var thread = _receiveThread;
                var socket = _socket;
                if (thread == null)
                {
                    Volatile.Write(ref _isRunning, 0);
                    Volatile.Write(ref _boundPort, 0);
                    ClearClockSynchronization();
                    ClearPublication();
                    return;
                }

                Volatile.Write(ref _stopRequested, 1);
                try
                {
                    socket?.Close();
                }
                catch (ObjectDisposedException)
                {
                }
                catch (SocketException exception)
                {
                    Interlocked.Increment(ref _socketExceptions);
                    Volatile.Write(ref _lastSocketError, (int)exception.SocketErrorCode);
                }

                var joined = ReferenceEquals(Thread.CurrentThread, thread) || thread.Join(5000);
                if (!joined)
                {
                    Interlocked.Increment(ref _unexpectedExceptions);
                    Volatile.Write(ref _boundPort, 0);
                    return;
                }

                socket?.Dispose();
                _socket = null;
                _receiveThread = null;
                Volatile.Write(ref _isRunning, 0);
                Volatile.Write(ref _boundPort, 0);
                ClearClockSynchronization();
                ClearPublication();
            }
        }

        private void ResetForStart()
        {
            _gate.ResetSequence();
            Volatile.Write(ref _hasAcceptedSequence, 0);
            Volatile.Write(ref _lastAcceptedSequenceBits, 0);
            ClearClockSynchronization();
            ClearPublication();
            ResetDiagnostics();
        }

        private void ClearPublication()
        {
            Interlocked.Exchange(ref _publishedIndex, -1);
            _nextWriteIndex = 0;
            for (var i = 0; i < _slots.Length; i++)
            {
                Interlocked.CompareExchange(ref _slots[i].State, SlotFree, SlotPublished);
                Interlocked.CompareExchange(ref _slots[i].State, SlotFree, SlotWriting);
            }
        }

        private void ResetDiagnostics()
        {
            Interlocked.Exchange(ref _receivedDatagrams, 0);
            Interlocked.Exchange(ref _acceptedPackets, 0);
            Interlocked.Exchange(ref _decodeRejectedPackets, 0);
            Interlocked.Exchange(ref _sessionRejectedPackets, 0);
            Interlocked.Exchange(ref _stalePackets, 0);
            Interlocked.Exchange(ref _sequenceGapDrops, 0);
            Interlocked.Exchange(ref _supersededFrames, 0);
            Interlocked.Exchange(ref _receiverOverruns, 0);
            Interlocked.Exchange(ref _socketExceptions, 0);
            Interlocked.Exchange(ref _unexpectedExceptions, 0);
            Volatile.Write(ref _lastDecodeError, (int)PoseDecodeError.None);
            Volatile.Write(ref _lastSessionError, (int)PoseSessionError.None);
            Volatile.Write(ref _lastSocketError, (int)SocketError.Success);
        }

        private void ThrowIfDisposed()
        {
            if (Volatile.Read(ref _isDisposed) != 0)
                throw new ObjectDisposedException(nameof(PoseUdpReceiver));
        }

        private void ClearClockSynchronizationLocked()
        {
            _clockSynchronized = false;
            _clockResetRequired = false;
            _unityMinusBlenderOffsetUsec = 0;
            _oneWayAgeWriteIndex = 0;
            _oneWayAgeSampleCount = 0;
        }

        private void RebaseOneWayAgeSamplesLocked(long offsetDeltaUsec)
        {
            if (offsetDeltaUsec == 0 || _oneWayAgeSampleCount == 0)
                return;

            var inputCount = _oneWayAgeSampleCount;
            var oldestIndex = inputCount == OneWayAgeWindowCapacity
                ? _oneWayAgeWriteIndex
                : 0;
            var outputCount = 0;
            for (var logicalIndex = 0; logicalIndex < inputCount; logicalIndex++)
            {
                var sourceIndex = (oldestIndex + logicalIndex) % OneWayAgeWindowCapacity;
                var ageUsec = _oneWayAgeSamples[sourceIndex];
                if (offsetDeltaUsec > 0)
                {
                    if (ageUsec < offsetDeltaUsec)
                        continue;
                    ageUsec -= offsetDeltaUsec;
                }
                else
                {
                    var increaseUsec = -offsetDeltaUsec;
                    ageUsec = ageUsec > long.MaxValue - increaseUsec
                        ? long.MaxValue
                        : ageUsec + increaseUsec;
                }
                _oneWayAgeSortBuffer[outputCount++] = ageUsec;
            }
            Array.Copy(_oneWayAgeSortBuffer, _oneWayAgeSamples, outputCount);
            _oneWayAgeSampleCount = outputCount;
            _oneWayAgeWriteIndex = outputCount % OneWayAgeWindowCapacity;
        }

        private static bool OffsetJumpExceedsLimit(long current, long candidate)
        {
            const ulong signedOrderBias = 0x8000000000000000ul;
            var orderedCurrent = unchecked((ulong)current) ^ signedOrderBias;
            var orderedCandidate = unchecked((ulong)candidate) ^ signedOrderBias;
            var difference = orderedCurrent >= orderedCandidate
                ? orderedCurrent - orderedCandidate
                : orderedCandidate - orderedCurrent;
            return difference > (ulong)MaximumClockOffsetJumpUsec;
        }

        private static long SaturatingAdd(long left, long right)
        {
            if (right > 0 && left > long.MaxValue - right)
                return long.MaxValue;
            if (right < 0 && left < long.MinValue - right)
                return long.MinValue;
            return left + right;
        }

        private static long SaturatingDifference(long later, long earlier)
        {
            const ulong signedOrderBias = 0x8000000000000000ul;
            var orderedLater = unchecked((ulong)later) ^ signedOrderBias;
            var orderedEarlier = unchecked((ulong)earlier) ^ signedOrderBias;
            var difference = orderedLater - orderedEarlier;
            return difference > long.MaxValue ? long.MaxValue : (long)difference;
        }

        private static long PercentileNearestRank(
            long[] sortedValues,
            int count,
            int percentile)
        {
            var index = (count * percentile + 99) / 100 - 1;
            return sortedValues[Math.Max(0, Math.Min(count - 1, index))];
        }

        private static long CurrentMonotonicUsec()
        {
            var timestamp = Stopwatch.GetTimestamp();
            var wholeSeconds = timestamp / Stopwatch.Frequency;
            var remainder = timestamp % Stopwatch.Frequency;
            return checked(
                wholeSeconds * 1_000_000L +
                remainder * 1_000_000L / Stopwatch.Frequency);
        }

        private static void CopyFrame(PoseFrame source, PoseFrame destination)
        {
            destination.SessionId = source.SessionId;
            destination.Sequence = source.Sequence;
            destination.TimestampUsec = source.TimestampUsec;
            destination.FrameIndex = source.FrameIndex;
            destination.Flags = source.Flags;
            destination.JointCount = source.JointCount;
            destination.PresentMask = source.PresentMask;
            destination.RootPosition = source.RootPosition;
            destination.RootRotation = source.RootRotation;
            destination.HipsLocalPositionDelta = source.HipsLocalPositionDelta;
            destination.GroundPlane = source.GroundPlane;

            for (var i = 0; i < source.JointCount; i++)
                destination.JointRotations[i] = source.JointRotations[i];
            for (var i = 0; i < 4; i++)
            {
                destination.ContactWeights[i] = source.ContactWeights[i];
                destination.ContactEpochs[i] = source.ContactEpochs[i];
                destination.ContactPositions[i] = source.ContactPositions[i];
                destination.ContactRotations[i] = source.ContactRotations[i];
            }
        }
    }
}
