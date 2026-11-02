using System;
using System.Collections.Generic;
using System.Security.Cryptography;
using System.Text;
using Kuko.MotionBridge.Protocol;
using Kuko.MotionBridge.Retargeting;
using Kuko.MotionBridge.Transport;
using UnityEngine;

namespace Kuko.MotionBridge.Live
{
    /// <summary>
    /// Owns the Play Mode transport, Canonical-to-HumanPose conversion, and the
    /// exclusive Humanoid writer for one scene Animator.
    /// </summary>
    [AddComponentMenu("Motion Bridge/Runtime Preview")]
    [DisallowMultipleComponent]
    public sealed class MotionBridgeRuntimePreview : MonoBehaviour
    {
        private const int DefaultControlPort = MotionBridgeProtocol.DefaultControlPort;
        private const int DefaultPosePort = 39872;
        private const uint DefaultSessionId = 0x12345678u;
        private const double PoseTimeoutSeconds = 2d;
        private const double TransportRetrySeconds = 0.5d;

        private static readonly Dictionary<int, MotionBridgeRuntimePreview> s_owners =
            new Dictionary<int, MotionBridgeRuntimePreview>();
        private static string s_muscleNamesHash;

        [SerializeField] private Animator _target;
        [SerializeField] private Avatar _sourceAvatar;
        [SerializeField, Range(0, ushort.MaxValue)]
        private int _controlPort = DefaultControlPort;
        [SerializeField, Range(0, ushort.MaxValue)]
        private int _posePort = DefaultPosePort;
        [SerializeField] private long _sessionId = DefaultSessionId;
        [SerializeField] private bool _autoStart = true;

        private PoseUdpReceiver _receiver;
        private ControlServer _controlServer;
        private CanonicalPoseConverter _converter;
        private HumanoidPosePlayable _writer;
        private PoseFrame _latestFrame;
        private Animator _activeTarget;
        private TransformSnapshot[] _transformSnapshots;
        private AnimatorSnapshot _animatorSnapshot;
        private bool _hasAnimatorSnapshot;
        private bool _previewStarted;
        private bool _hasPendingEvaluation;
        private bool _startInvoked;
        private int _ownedAnimatorId;
        private double _lastPoseTime = -1d;
        private double _streamStartedAt = -1d;
        private double _nextTransportRetryAt;
        private string _lastError;

        public Animator Target => _target;

        public Avatar SourceAvatar => _sourceAvatar;

        public bool AutoStart => _autoStart;

        public bool IsRunning =>
            _previewStarted && _writer != null && _writer.IsValid;

        public string LastError => _lastError;

        public ControlSessionState ControlState =>
            _controlServer?.State ?? ControlSessionState.Disconnected;

        public PoseUdpReceiverDiagnostics ReceiverDiagnostics =>
            _receiver?.Diagnostics ?? default;

        public int BoundControlPort => _controlServer?.BoundPort ?? 0;

        public int BoundPosePort => _receiver?.BoundPort ?? 0;

        public uint SessionId => unchecked((uint)_sessionId);

        /// <summary>
        /// Configures the runtime receiver. A running preview is safely rebuilt with
        /// the new target and endpoint settings.
        /// </summary>
        public void Configure(
            Animator target,
            Avatar sourceAvatar,
            int controlPort,
            int posePort,
            uint sessionId,
            bool autoStart = true)
        {
            if (target == null)
                throw new ArgumentNullException(nameof(target));
            if (sourceAvatar == null)
                throw new ArgumentNullException(nameof(sourceAvatar));
            ValidatePort(controlPort, nameof(controlPort));
            ValidatePort(posePort, nameof(posePort));

            var restart = _previewStarted;
            if (restart)
                StopPreview();

            _target = target;
            _sourceAvatar = sourceAvatar;
            _controlPort = controlPort;
            _posePort = posePort;
            _sessionId = sessionId;
            _autoStart = autoStart;

            if (Application.isPlaying && isActiveAndEnabled &&
                (restart || (_startInvoked && _autoStart)))
            {
                EnsureStarted();
            }
        }

        /// <summary>
        /// Starts the preview when needed and records any validation failure instead
        /// of leaking it through a MonoBehaviour lifecycle callback.
        /// </summary>
        public bool EnsureStarted()
        {
            if (IsRunning)
                return true;

            try
            {
                StartPreview();
                return IsRunning;
            }
            catch (Exception exception)
            {
                _lastError = FormatError(exception);
                Debug.LogError($"Motion Bridge runtime preview could not start: {_lastError}", this);
                return false;
            }
        }

        /// <summary>
        /// Takes exclusive ownership of the configured Animator and starts a runtime
        /// pose writer. A transient bind failure leaves the writer alive and is retried.
        /// </summary>
        public void StartPreview()
        {
            if (IsRunning)
                return;
            if (!Application.isPlaying)
                throw new InvalidOperationException(
                    "Motion Bridge runtime preview is available only in Play Mode.");

            if (_previewStarted)
                StopPreview();

            ValidateConfiguration();
            _lastError = null;
            _hasPendingEvaluation = false;
            _latestFrame = new PoseFrame(CanonicalHumanoidV1.JointCount);

            try
            {
                _activeTarget = _target;
                AcquireAnimatorOwnership(_activeTarget);
                _transformSnapshots = CaptureTransforms(_activeTarget.transform);
                _animatorSnapshot = AnimatorSnapshot.Capture(_activeTarget);
                _hasAnimatorSnapshot = true;
                ApplyExclusiveAnimatorOverride(_activeTarget);

                _converter = new CanonicalPoseConverter(_sourceAvatar);
                _writer = new HumanoidPosePlayable(
                    _activeTarget,
                    "Motion Bridge Runtime Humanoid Pose");
                _previewStarted = true;
                _nextTransportRetryAt = RealtimeNow();
                TryBindTransport();
            }
            catch
            {
                StopPreview();
                throw;
            }
        }

        /// <summary>
        /// Stops all workers and restores the Animator plus every captured child TRS.
        /// Repeated calls are safe.
        /// </summary>
        public void StopPreview()
        {
            _previewStarted = false;
            _hasPendingEvaluation = false;
            _lastPoseTime = -1d;
            _streamStartedAt = -1d;
            _nextTransportRetryAt = 0d;

            DisposeTransport();

            var writer = _writer;
            var converter = _converter;
            _writer = null;
            _converter = null;
            DisposeSafely(writer);
            DisposeSafely(converter);

            if (_hasAnimatorSnapshot)
            {
                try
                {
                    _animatorSnapshot.Restore(_activeTarget);
                }
                catch (Exception exception)
                {
                    Debug.LogException(exception);
                }
                RestoreTransforms(_transformSnapshots);
            }

            _hasAnimatorSnapshot = false;
            _transformSnapshots = null;
            _latestFrame = null;
            _activeTarget = null;
            ReleaseAnimatorOwnership();
        }

        private void Start()
        {
            _startInvoked = true;
            if (_autoStart)
                EnsureStarted();
        }

        private void OnEnable()
        {
            if (Application.isPlaying && _startInvoked && _autoStart)
                EnsureStarted();
        }

        private void OnDisable()
        {
            StopPreview();
        }

        private void OnDestroy()
        {
            StopPreview();
        }

        private void OnApplicationQuit()
        {
            StopPreview();
        }

        private void OnValidate()
        {
            _controlPort = Mathf.Clamp(_controlPort, 0, ushort.MaxValue);
            _posePort = Mathf.Clamp(_posePort, 0, ushort.MaxValue);
            if (_sessionId < 0L)
                _sessionId = 0L;
            else if (_sessionId > uint.MaxValue)
                _sessionId = uint.MaxValue;
        }

        private void Update()
        {
            if (!_previewStarted || _writer == null || _converter == null)
                return;
            if (_activeTarget == null)
            {
                FailPreview(new InvalidOperationException(
                    "Motion Bridge runtime target was destroyed."));
                return;
            }

            if (_receiver == null || _controlServer == null)
            {
                if (RealtimeNow() >= _nextTransportRetryAt)
                    TryBindTransport();
                return;
            }

            PumpTransport();
        }

        private void LateUpdate()
        {
            if (!_previewStarted || !_hasPendingEvaluation || _writer == null)
                return;

            try
            {
                _writer.Evaluate(0f);
                _hasPendingEvaluation = false;
            }
            catch (Exception exception)
            {
                FailPreview(exception);
            }
        }

        private void PumpTransport()
        {
            try
            {
                var now = RealtimeNow();
                while (_controlServer.TryDequeueEvent(out var controlEvent))
                {
                    if (controlEvent.Kind == ControlServerEventKind.Error)
                    {
                        RebindTransport(controlEvent.Detail ?? "TCP control error");
                        return;
                    }
                    if (controlEvent.Kind == ControlServerEventKind.Disconnected)
                    {
                        RebindTransport(controlEvent.Detail ?? "TCP peer disconnected");
                        return;
                    }
                    if (controlEvent.Kind == ControlServerEventKind.Streaming)
                    {
                        _lastPoseTime = -1d;
                        _streamStartedAt = now;
                    }
                    else if (controlEvent.Kind == ControlServerEventKind.Paused)
                    {
                        _streamStartedAt = -1d;
                    }
                }

                if (!_controlServer.IsRunning ||
                    _controlServer.State == ControlSessionState.Error ||
                    !_receiver.IsRunning)
                {
                    RebindTransport(
                        _controlServer.Diagnostics.LastError ??
                        "Motion Bridge transport stopped unexpectedly");
                    return;
                }

                if (_controlServer.State == ControlSessionState.Streaming &&
                    _receiver.TryConsumeLatest(_latestFrame))
                {
                    if (!_converter.TryConvert(_latestFrame, out var conversionError))
                    {
                        RebindTransport(
                            $"Canonical pose conversion failed: {conversionError}");
                        return;
                    }

                    try
                    {
                        _writer.SetPose(
                            _converter.HumanPose,
                            _converter.TrajectoryRootPosition,
                            _converter.TrajectoryRootRotation);
                    }
                    catch (Exception exception)
                    {
                        FailPreview(exception);
                        return;
                    }
                    _hasPendingEvaluation = true;
                    _lastPoseTime = now;
                }
                else if (_controlServer.State == ControlSessionState.Streaming &&
                         Math.Max(_lastPoseTime, _streamStartedAt) >= 0d &&
                         now - Math.Max(_lastPoseTime, _streamStartedAt) >
                         PoseTimeoutSeconds)
                {
                    RebindTransport("Blender pose stream timed out");
                }
            }
            catch (Exception exception)
            {
                // Transport and conversion failures are recoverable. The writer and
                // the current target pose stay owned while the loopback ports rebind.
                RebindTransport(FormatError(exception));
            }
        }

        private void TryBindTransport()
        {
            if (!_previewStarted || _receiver != null || _controlServer != null)
                return;

            try
            {
                BindTransport();
                _lastError = null;
            }
            catch (Exception exception)
            {
                DisposeTransport();
                _lastError = FormatError(exception);
                _nextTransportRetryAt = RealtimeNow() + TransportRetrySeconds;
            }
        }

        private void BindTransport()
        {
            PoseUdpReceiver receiver = null;
            ControlServer controlServer = null;
            try
            {
                receiver = new PoseUdpReceiver(new PoseUdpReceiverConfig(
                    SessionId,
                    CanonicalHumanoidV1.JointCount,
                    CanonicalHumanoidV1.RequiredJointMask,
                    _posePort));
                var boundPosePort = receiver.Start();
                var diagnosticsReceiver = receiver;
                controlServer = new ControlServer(
                    new ControlServerConfig(
                        sessionId: SessionId,
                        skeletonId: CanonicalHumanoidV1.SkeletonId,
                        skeletonHash: CanonicalHumanoidV1.SkeletonHash,
                        expectedJointCount: CanonicalHumanoidV1.JointCount,
                        udpPort: boundPosePort,
                        localPort: _controlPort,
                        acceptedPoseHz: 60,
                        unityVersion: Application.unityVersion,
                        packageVersion: MotionBridgeProtocol.PackageVersion,
                        muscleCount: HumanTrait.MuscleCount,
                        muscleNamesHash: ComputeMuscleNamesHash(),
                        targetName: _activeTarget.name,
                        targetHumanScale: _activeTarget.humanScale),
                    poseDiagnosticsProvider: () => diagnosticsReceiver.Diagnostics,
                    poseClockOffsetConsumer:
                        diagnosticsReceiver.TrySetClockSynchronization,
                    poseClockSynchronizationResetter:
                        diagnosticsReceiver.ClearClockSynchronization);
                controlServer.Start();

                _receiver = receiver;
                _controlServer = controlServer;
                receiver = null;
                controlServer = null;
                _lastPoseTime = -1d;
                _streamStartedAt = -1d;
                _nextTransportRetryAt = 0d;
            }
            finally
            {
                DisposeSafely(controlServer);
                DisposeSafely(receiver);
            }
        }

        private void RebindTransport(string reason)
        {
            if (!_previewStarted)
                return;

            _lastError = string.IsNullOrWhiteSpace(reason)
                ? "Motion Bridge transport disconnected"
                : reason;
            DisposeTransport();
            _nextTransportRetryAt = RealtimeNow();
            TryBindTransport();
        }

        private void DisposeTransport()
        {
            var controlServer = _controlServer;
            var receiver = _receiver;
            _controlServer = null;
            _receiver = null;
            _lastPoseTime = -1d;
            _streamStartedAt = -1d;
            DisposeSafely(controlServer);
            DisposeSafely(receiver);
        }

        private void FailPreview(Exception exception)
        {
            var message = FormatError(exception);
            StopPreview();
            _lastError = message;
            Debug.LogError($"Motion Bridge runtime preview stopped: {message}", this);
        }

        private void ValidateConfiguration()
        {
            if (_target == null)
                throw new InvalidOperationException("Select a target Humanoid Animator.");
            if (!_target.gameObject.activeInHierarchy)
                throw new InvalidOperationException("The target Animator must be active.");
            if (_target.avatar == null || !_target.avatar.isValid ||
                !_target.avatar.isHuman || !_target.isHuman)
            {
                throw new InvalidOperationException(
                    "The target Animator requires a valid Humanoid Avatar.");
            }
            if (_sourceAvatar == null || !_sourceAvatar.isValid || !_sourceAvatar.isHuman)
            {
                throw new InvalidOperationException(
                    "Select a valid Humanoid Canonical Source Avatar.");
            }
            ValidatePort(_controlPort, nameof(_controlPort));
            ValidatePort(_posePort, nameof(_posePort));
            if (_sessionId < 0L || _sessionId > uint.MaxValue)
                throw new InvalidOperationException("Session ID must be a uint32 value.");
            if (!HumanoidTargetScale.IsPositiveUniformHierarchy(_target.transform))
            {
                throw new InvalidOperationException(
                    "The target Animator and its parents require positive uniform scale.");
            }
        }

        private static void ValidatePort(int port, string parameterName)
        {
            if (port < 0 || port > ushort.MaxValue)
                throw new ArgumentOutOfRangeException(parameterName);
        }

        private static void ApplyExclusiveAnimatorOverride(Animator target)
        {
            target.runtimeAnimatorController = null;
            target.applyRootMotion = false;
            target.cullingMode = AnimatorCullingMode.AlwaysAnimate;
            target.enabled = true;
        }

        private void AcquireAnimatorOwnership(Animator target)
        {
            var animatorId = target.GetInstanceID();
            if (s_owners.TryGetValue(animatorId, out var owner) && owner != null &&
                !ReferenceEquals(owner, this))
            {
                throw new InvalidOperationException(
                    "Another Motion Bridge runtime preview already owns this Animator.");
            }

            s_owners[animatorId] = this;
            _ownedAnimatorId = animatorId;
        }

        private void ReleaseAnimatorOwnership()
        {
            if (_ownedAnimatorId == 0)
                return;
            if (s_owners.TryGetValue(_ownedAnimatorId, out var owner) &&
                ReferenceEquals(owner, this))
            {
                s_owners.Remove(_ownedAnimatorId);
            }
            _ownedAnimatorId = 0;
        }

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.SubsystemRegistration)]
        private static void ResetOwnership()
        {
            s_owners.Clear();
            s_muscleNamesHash = null;
        }

        private static TransformSnapshot[] CaptureTransforms(Transform root)
        {
            var transforms = root.GetComponentsInChildren<Transform>(true);
            var snapshots = new TransformSnapshot[transforms.Length];
            for (var index = 0; index < transforms.Length; index++)
                snapshots[index] = TransformSnapshot.Capture(transforms[index]);
            return snapshots;
        }

        private static void RestoreTransforms(TransformSnapshot[] snapshots)
        {
            if (snapshots == null)
                return;
            foreach (var snapshot in snapshots)
            {
                try
                {
                    snapshot.Restore();
                }
                catch (Exception exception)
                {
                    Debug.LogException(exception);
                }
            }
        }

        private static string ComputeMuscleNamesHash()
        {
            if (!string.IsNullOrEmpty(s_muscleNamesHash))
                return s_muscleNamesHash;

            var payload = Encoding.UTF8.GetBytes(string.Join("\n", HumanTrait.MuscleName));
            using (var sha256 = SHA256.Create())
            {
                var hash = sha256.ComputeHash(payload);
                var result = new StringBuilder(hash.Length * 2);
                foreach (var value in hash)
                    result.Append(value.ToString("x2"));
                s_muscleNamesHash = result.ToString();
                return s_muscleNamesHash;
            }
        }

        private static double RealtimeNow()
        {
            return Time.realtimeSinceStartupAsDouble;
        }

        private static string FormatError(Exception exception)
        {
            return $"{exception.GetType().Name}: {exception.Message}";
        }

        private static void DisposeSafely(IDisposable disposable)
        {
            if (disposable == null)
                return;
            try
            {
                disposable.Dispose();
            }
            catch (Exception exception)
            {
                Debug.LogException(exception);
            }
        }

        private readonly struct TransformSnapshot
        {
            private readonly Transform _transform;
            private readonly Vector3 _localPosition;
            private readonly Quaternion _localRotation;
            private readonly Vector3 _localScale;

            private TransformSnapshot(Transform transform)
            {
                _transform = transform;
                _localPosition = transform.localPosition;
                _localRotation = transform.localRotation;
                _localScale = transform.localScale;
            }

            public static TransformSnapshot Capture(Transform transform)
            {
                return new TransformSnapshot(transform);
            }

            public void Restore()
            {
                if (_transform == null)
                    return;
                _transform.localPosition = _localPosition;
                _transform.localRotation = _localRotation;
                _transform.localScale = _localScale;
            }
        }

        private readonly struct AnimatorSnapshot
        {
            private readonly bool _enabled;
            private readonly bool _applyRootMotion;
            private readonly AnimatorUpdateMode _updateMode;
            private readonly AnimatorCullingMode _cullingMode;
            private readonly float _speed;
            private readonly RuntimeAnimatorController _controller;
            private readonly Avatar _avatar;
            private readonly bool _fireEvents;
            private readonly bool _stabilizeFeet;
            private readonly float _feetPivotActive;
            private readonly bool _layersAffectMassCenter;
            private readonly bool _logWarnings;
            private readonly bool _keepControllerState;
            private readonly bool _writeDefaultsOnDisable;

            private AnimatorSnapshot(Animator animator)
            {
                _enabled = animator.enabled;
                _applyRootMotion = animator.applyRootMotion;
                _updateMode = animator.updateMode;
                _cullingMode = animator.cullingMode;
                _speed = animator.speed;
                _controller = animator.runtimeAnimatorController;
                _avatar = animator.avatar;
                _fireEvents = animator.fireEvents;
                _stabilizeFeet = animator.stabilizeFeet;
                _feetPivotActive = animator.feetPivotActive;
                _layersAffectMassCenter = animator.layersAffectMassCenter;
                _logWarnings = animator.logWarnings;
                _keepControllerState = animator.keepAnimatorStateOnDisable;
                _writeDefaultsOnDisable = animator.writeDefaultValuesOnDisable;
            }

            public static AnimatorSnapshot Capture(Animator animator)
            {
                return new AnimatorSnapshot(animator);
            }

            public void Restore(Animator animator)
            {
                if (animator == null)
                    return;
                animator.avatar = _avatar;
                animator.runtimeAnimatorController = _controller;
                animator.applyRootMotion = _applyRootMotion;
                animator.updateMode = _updateMode;
                animator.cullingMode = _cullingMode;
                animator.speed = _speed;
                animator.fireEvents = _fireEvents;
                animator.stabilizeFeet = _stabilizeFeet;
                animator.feetPivotActive = _feetPivotActive;
                animator.layersAffectMassCenter = _layersAffectMassCenter;
                animator.logWarnings = _logWarnings;
                animator.keepAnimatorStateOnDisable = _keepControllerState;
                animator.writeDefaultValuesOnDisable = _writeDefaultsOnDisable;
                animator.enabled = _enabled;
            }
        }
    }
}
