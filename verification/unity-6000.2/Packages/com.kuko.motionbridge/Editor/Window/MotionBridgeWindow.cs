using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using Kuko.MotionBridge.Editor.Preview;
using Kuko.MotionBridge.Live;
using Kuko.MotionBridge.Protocol;
using Kuko.MotionBridge.Retargeting;
using Kuko.MotionBridge.Transport;
using UnityEditor;
using UnityEngine;

namespace Kuko.MotionBridge.Editor
{
    public sealed class MotionBridgeWindow : EditorWindow
    {
        private const int DefaultPosePort = 39872;
        private const int DefaultControlPort = MotionBridgeProtocol.DefaultControlPort;
        private const uint DefaultSessionId = 0x12345678;
        private const double PoseTimeoutSeconds = 2d;

        [SerializeField] private Animator _target;
        [SerializeField] private Avatar _sourceAvatar;
        [SerializeField] private int _posePort = DefaultPosePort;
        [SerializeField] private int _controlPort = DefaultControlPort;
        [SerializeField] private long _sessionId = DefaultSessionId;
        [SerializeField] private bool _continueInPlayMode = true;
        [SerializeField] private bool _previewRequested;
        [SerializeField] private MotionBridgeRuntimePreview _runtimePreview;
        [SerializeField] private bool _beginnerMode = true;
        [SerializeField] private bool _advancedFoldout;
        [SerializeField] private Vector2 _scrollPosition;
        private PoseUdpReceiver _receiver;
        private ControlServer _controlServer;
        private CanonicalPoseConverter _converter;
        private MotionBridgeHumanoidPreview _preview;
        private PoseFrame _latestFrame;
        private double _lastPoseTime = -1d;
        private double _streamStartedAt = -1d;
        private double _lastRepaintTime;
        private string _listenerError;
        private string _setupNotice;
        private MessageType _setupNoticeType = MessageType.Info;

        [MenuItem("Window/Motion Bridge")]
        private static void Open()
        {
            GetWindow<MotionBridgeWindow>("Motion Bridge");
        }

        private void OnEnable()
        {
            _latestFrame = new PoseFrame(CanonicalHumanoidV1.JointCount);
            EditorApplication.playModeStateChanged -= OnPlayModeStateChanged;
            EditorApplication.playModeStateChanged += OnPlayModeStateChanged;
            if (_previewRequested && !EditorApplication.isPlayingOrWillChangePlaymode)
                QueueResumeInEditMode();
        }

        private void OnDisable()
        {
            EditorApplication.playModeStateChanged -= OnPlayModeStateChanged;
            EditorApplication.delayCall -= ResumeInEditMode;
            StopEditorTransport();
        }

        private void OnDestroy()
        {
            StopListener();
        }

        private void OnGUI()
        {
            using (new EditorGUILayout.HorizontalScope(EditorStyles.toolbar))
            {
                GUILayout.Label("Motion Bridge", EditorStyles.boldLabel);
                GUILayout.FlexibleSpace();
                _beginnerMode = GUILayout.Toggle(
                    _beginnerMode,
                    "かんたんガイド",
                    EditorStyles.toolbarButton);
            }

            _scrollPosition = EditorGUILayout.BeginScrollView(_scrollPosition);
            if (_beginnerMode)
            {
                DrawBeginnerGUI();
                EditorGUILayout.Space();
                _advancedFoldout = EditorGUILayout.Foldout(
                    _advancedFoldout,
                    "詳細設定 / Diagnostics",
                    true);
                if (_advancedFoldout)
                    DrawAdvancedGUI();
            }
            else
            {
                DrawAdvancedGUI();
            }
            EditorGUILayout.EndScrollView();
        }

        private void DrawAdvancedGUI()
        {
            var configurationLocked =
                _previewRequested || EditorApplication.isPlayingOrWillChangePlaymode;
            EditorGUILayout.LabelField("Target", EditorStyles.boldLabel);
            using (new EditorGUI.DisabledScope(configurationLocked))
            {
                _target = (Animator)EditorGUILayout.ObjectField(
                    "Animator",
                    _target,
                    typeof(Animator),
                    true);
                _sourceAvatar = (Avatar)EditorGUILayout.ObjectField(
                    "Canonical Source Avatar",
                    _sourceAvatar,
                    typeof(Avatar),
                    false);
            }

            var message = ValidateTarget(_target);
            EditorGUILayout.HelpBox(message.Text, message.Type);
            var sourceMessage = ValidateSourceAvatar(_sourceAvatar);
            EditorGUILayout.HelpBox(sourceMessage.Text, sourceMessage.Type);

            EditorGUILayout.Space();
            EditorGUILayout.LabelField("Live Pose", EditorStyles.boldLabel);
            using (new EditorGUI.DisabledScope(configurationLocked))
            {
                _controlPort = EditorGUILayout.IntField("TCP Control Port", _controlPort);
                _posePort = EditorGUILayout.IntField("UDP Port", _posePort);
                _sessionId = EditorGUILayout.LongField("Session ID", _sessionId);
                _continueInPlayMode = EditorGUILayout.Toggle(
                    "Continue In Play Mode",
                    _continueInPlayMode);
            }

            using (new EditorGUILayout.HorizontalScope())
            {
                using (new EditorGUI.DisabledScope(
                           _previewRequested || EditorApplication.isPlayingOrWillChangePlaymode))
                {
                    if (GUILayout.Button("Start Unity Receiver"))
                        StartListener();
                }
                using (new EditorGUI.DisabledScope(!_previewRequested))
                {
                    if (GUILayout.Button("Stop"))
                        StopListener();
                }
            }

            using (new EditorGUI.DisabledScope(true))
                EditorGUILayout.Toggle(
                    "Pose Writer Ready",
                    (_preview?.IsRunning ?? false) ||
                    (_runtimePreview != null && _runtimePreview.IsRunning));
            DrawDiagnostics();
        }

        private void DrawBeginnerGUI()
        {
            var configurationLocked =
                _previewRequested || EditorApplication.isPlayingOrWillChangePlaymode;
            EditorGUILayout.HelpBox(
                "上から順に設定します。TCPやSession IDは通常変更しません。",
                MessageType.Info);

            using (new EditorGUILayout.VerticalScope(EditorStyles.helpBox))
            {
                DrawStepTitle(1, "Hierarchyのキャラクターを指定", IsTargetValid(_target));
                using (new EditorGUI.DisabledScope(configurationLocked))
                {
                    var nextTarget = (Animator)EditorGUILayout.ObjectField(
                        "キャラクター",
                        _target,
                        typeof(Animator),
                        true);
                    if (nextTarget != _target)
                    {
                        _target = nextTarget;
                        _setupNotice = null;
                    }
                    if (GUILayout.Button("Hierarchyで選択中のHumanoidを使う"))
                        UseSelectedHumanoid();
                }
                var targetMessage = ValidateTarget(_target);
                EditorGUILayout.HelpBox(
                    FriendlyTargetValidation(_target, targetMessage.Text),
                    targetMessage.Type);
            }

            using (new EditorGUILayout.VerticalScope(EditorStyles.helpBox))
            {
                DrawStepTitle(
                    2,
                    "Canonical Source Avatarを指定",
                    IsSourceAvatarValid(_sourceAvatar));
                using (new EditorGUI.DisabledScope(configurationLocked))
                {
                    var nextSource = (Avatar)EditorGUILayout.ObjectField(
                        "Canonical Avatar",
                        _sourceAvatar,
                        typeof(Avatar),
                        false);
                    if (nextSource != _sourceAvatar)
                    {
                        _sourceAvatar = nextSource;
                        _setupNotice = null;
                    }
                    if (GUILayout.Button("import済みCanonical Avatarを自動設定"))
                        FindCanonicalAvatar();
                }
                var sourceMessage = ValidateSourceAvatar(_sourceAvatar);
                EditorGUILayout.HelpBox(
                    FriendlySourceValidation(_sourceAvatar, sourceMessage.Text),
                    sourceMessage.Type);
            }

            if (!string.IsNullOrEmpty(_setupNotice))
                EditorGUILayout.HelpBox(_setupNotice, _setupNoticeType);

            using (new EditorGUILayout.VerticalScope(EditorStyles.helpBox))
            {
                DrawStepTitle(3, "Unityのプレビュー待受を開始", _previewRequested);
                using (new EditorGUI.DisabledScope(configurationLocked))
                {
                    _continueInPlayMode = EditorGUILayout.ToggleLeft(
                        "Unityを再生してもプレビューを続ける",
                        _continueInPlayMode);
                }

                using (new EditorGUILayout.HorizontalScope())
                {
                    var canStart =
                        !_previewRequested &&
                        !EditorApplication.isPlayingOrWillChangePlaymode &&
                        IsTargetValid(_target) &&
                        IsSourceAvatarValid(_sourceAvatar);
                    using (new EditorGUI.DisabledScope(!canStart))
                    {
                        if (GUILayout.Button("プレビュー待受を開始", GUILayout.Height(32f)))
                            StartListener();
                    }
                    using (new EditorGUI.DisabledScope(!_previewRequested))
                    {
                        if (GUILayout.Button("停止", GUILayout.Height(32f)))
                            StopListener();
                    }
                }
                if (EditorApplication.isPlayingOrWillChangePlaymode && !_previewRequested)
                {
                    EditorGUILayout.HelpBox(
                        "Unityの再生を停止し、Edit Modeで待受を開始してください。",
                        MessageType.Warning);
                }
            }

            using (new EditorGUILayout.VerticalScope(EditorStyles.helpBox))
            {
                var connection = GetBeginnerConnectionMessage(out var messageType);
                var connected = CurrentControlState() == ControlSessionState.Streaming &&
                    CurrentAcceptedPackets() > 0;
                DrawStepTitle(4, "Blenderから接続", connected);
                EditorGUILayout.HelpBox(connection, messageType);
                if (_previewRequested && !connected)
                {
                    EditorGUILayout.LabelField(
                        $"Blender: TCP {_controlPort} / Session ID {_sessionId}",
                        EditorStyles.miniLabel);
                    EditorGUILayout.LabelField(
                        "Blenderの「Unityへ接続」を押します。UDP設定は不要です。",
                        EditorStyles.wordWrappedMiniLabel);
                }
                else if (connected)
                {
                    EditorGUILayout.LabelField(
                        "Blenderでボーンやカーブを直すと、このキャラクターへ即時反映されます。",
                        EditorStyles.wordWrappedMiniLabel);
                }
            }
        }

        private static void DrawStepTitle(int number, string title, bool complete)
        {
            using (new EditorGUILayout.HorizontalScope())
            {
                GUILayout.Label(
                    complete ? "✓" : number.ToString(),
                    complete ? EditorStyles.boldLabel : EditorStyles.helpBox,
                    GUILayout.Width(24f));
                GUILayout.Label(title, EditorStyles.boldLabel);
            }
        }

        private void UseSelectedHumanoid()
        {
            var selected = Selection.activeGameObject;
            if (selected == null)
            {
                SetSetupNotice(
                    "Hierarchyで配置済みキャラクターを選択してから押してください。",
                    MessageType.Warning);
                return;
            }
            var animator = selected.GetComponent<Animator>() ??
                selected.GetComponentInParent<Animator>() ??
                selected.GetComponentInChildren<Animator>(true);
            var validation = ValidateTarget(animator);
            if (animator == null || validation.Type != MessageType.Info)
            {
                SetSetupNotice(
                    FriendlyTargetValidation(animator, validation.Text),
                    MessageType.Error);
                return;
            }
            _target = animator;
            SetSetupNotice(
                $"キャラクターを設定しました：{animator.name}",
                MessageType.Info);
        }

        private void FindCanonicalAvatar()
        {
            if (!TryFindCanonicalAvatar(out var avatar, out var message))
            {
                SetSetupNotice(message, MessageType.Warning);
                return;
            }
            _sourceAvatar = avatar;
            SetSetupNotice(
                $"Canonical Avatarを設定しました：{avatar.name}",
                MessageType.Info);
        }

        private void SetSetupNotice(string message, MessageType type)
        {
            _setupNotice = message;
            _setupNoticeType = type;
        }

        private static bool TryFindCanonicalAvatar(out Avatar avatar, out string message)
        {
            var candidates = new List<Avatar>();
            var candidatePaths = new List<string>();
            var seen = new HashSet<int>();
            foreach (var guid in AssetDatabase.FindAssets(
                         CanonicalSourceAvatarPostprocessor.AssetName))
            {
                var path = AssetDatabase.GUIDToAssetPath(guid);
                if (!string.Equals(
                        Path.GetFileNameWithoutExtension(path),
                        CanonicalSourceAvatarPostprocessor.AssetName,
                        StringComparison.OrdinalIgnoreCase))
                    continue;
                foreach (var asset in AssetDatabase.LoadAllAssetsAtPath(path))
                {
                    if (asset is not Avatar candidate ||
                        !candidate.isValid ||
                        !candidate.isHuman ||
                        !seen.Add(candidate.GetInstanceID()))
                        continue;
                    candidates.Add(candidate);
                    candidatePaths.Add(path);
                }
            }

            if (candidates.Count == 1)
            {
                avatar = candidates[0];
                message = null;
                return true;
            }
            if (candidates.Count > 1)
            {
                var preferredIndex = -1;
                for (var index = 0; index < candidatePaths.Count; index++)
                {
                    if (!candidatePaths[index].Contains("/Samples/Motion Bridge/"))
                        continue;
                    if (preferredIndex >= 0)
                    {
                        avatar = null;
                        message =
                            "Canonical Avatarが複数あります。手動で1つ選択してください。";
                        return false;
                    }
                    preferredIndex = index;
                }
                if (preferredIndex >= 0)
                {
                    avatar = candidates[preferredIndex];
                    message = null;
                    return true;
                }
                avatar = null;
                message = "Canonical Avatarが複数あります。手動で1つ選択してください。";
                return false;
            }

            avatar = null;
            message =
                "Package Manager > Motion Bridge > Samples から " +
                "Canonical Source AvatarをImportしてください。";
            return false;
        }

        private static bool IsTargetValid(Animator target)
        {
            var validation = ValidateTarget(target);
            return target != null && validation.Type == MessageType.Info;
        }

        private static bool IsSourceAvatarValid(Avatar avatar)
        {
            var validation = ValidateSourceAvatar(avatar);
            return avatar != null && validation.Type == MessageType.Info;
        }

        private static string FriendlyTargetValidation(Animator target, string rawMessage)
        {
            if (target == null)
                return "ProjectのPrefabではなく、Hierarchyへ配置したキャラクターを指定します。";
            if (rawMessage.Contains("scene instance"))
                return "Prefab/FBXをHierarchyへ配置し、Hierarchy側のAnimatorを指定してください。";
            if (rawMessage.Contains("active in the hierarchy"))
                return "Hierarchyでキャラクターを有効にしてください。";
            if (rawMessage.Contains("no Avatar"))
                return "FBX Inspector > RigをHumanoidにしてApplyしてください。";
            if (rawMessage.Contains("valid Humanoid"))
                return "AvatarがHumanoidとして有効になるようFBXのRig設定を確認してください。";
            if (rawMessage.Contains("scale"))
                return "キャラクターと親のScaleを正の同じ値にします（推奨 1, 1, 1）。";
            return "キャラクターのHumanoid設定は準備OKです。";
        }

        private static string FriendlySourceValidation(Avatar avatar, string rawMessage)
        {
            if (avatar == null)
                return "下の自動設定を押します。見つからない場合はPackageのSampleをImportします。";
            if (rawMessage.Contains("MB_Canonical_Humanoid_v1"))
                return "Motion Bridge付属のMB_Canonical_Humanoid_v1内のAvatarを選択してください。";
            if (rawMessage.Contains("valid Humanoid"))
                return "Canonical Source Avatarを再Importしてください。";
            return "Canonical Source Avatarは準備OKです。";
        }

        private static string FriendlyListenerError(string rawMessage)
        {
            if (string.IsNullOrEmpty(rawMessage))
                return "Unityの待受を開始できます。";
            var normalized = rawMessage.ToLowerInvariant();
            if (normalized.Contains("address already in use") ||
                normalized.Contains("bind failed"))
                return "同じポートを別のMotion Bridgeが使用中です。そちらを停止してください。";
            if (normalized.Contains("timeout") || normalized.Contains("timed out"))
                return "Poseが届かなかったためキャラクターを復元し、再接続を待っています。";
            if (normalized.Contains("canonical skeleton does not match") ||
                normalized.Contains("skeleton descriptor identity does not match") ||
                normalized.Contains("skeleton_hash_mismatch"))
            {
                return "BlenderとUnityのCanonicalデータが一致しません。" +
                    "同じ配布セットのBlenderファイルとUnity Packageにそろえ、" +
                    "Unityの「プレビュー待受を開始」→Blenderの「Unityへ接続」" +
                    "の順でやり直してください。";
            }
            if (normalized.Contains("session"))
                return "UnityとBlenderのSession IDを同じ値にしてください。";
            if (normalized.Contains("avatar"))
                return "Canonical AvatarとキャラクターのHumanoid設定を確認してください。";
            return "待受を開始できません。下の詳細設定で技術情報を確認してください。";
        }

        private ControlSessionState CurrentControlState()
        {
            if (EditorApplication.isPlaying && _runtimePreview != null)
                return _runtimePreview.ControlState;
            return _controlServer?.State ?? ControlSessionState.Disconnected;
        }

        private long CurrentAcceptedPackets()
        {
            if (EditorApplication.isPlaying && _runtimePreview != null)
                return _runtimePreview.ReceiverDiagnostics.AcceptedPackets;
            return _receiver?.Diagnostics.AcceptedPackets ?? 0L;
        }

        private string GetBeginnerConnectionMessage(out MessageType messageType)
        {
            var error = _listenerError;
            if (EditorApplication.isPlaying && _runtimePreview != null &&
                !string.IsNullOrEmpty(_runtimePreview.LastError))
                error = _runtimePreview.LastError;
            return ResolveBeginnerConnectionMessage(
                _previewRequested,
                EditorApplication.isPlaying,
                _continueInPlayMode,
                CurrentControlState(),
                CurrentAcceptedPackets(),
                error,
                out messageType);
        }

        private static string ResolveBeginnerConnectionMessage(
            bool previewRequested,
            bool playing,
            bool continueInPlayMode,
            ControlSessionState state,
            long acceptedPackets,
            string error,
            out MessageType messageType)
        {
            if (!string.IsNullOrEmpty(error))
            {
                messageType = MessageType.Error;
                return FriendlyListenerError(error);
            }
            if (!previewRequested)
            {
                messageType = MessageType.Info;
                return "上の設定が終わったら、Unityのプレビュー待受を開始します。";
            }
            if (playing && !continueInPlayMode)
            {
                messageType = MessageType.Warning;
                return "再生中の継続がOFFです。Edit Modeへ戻ると待受を再開します。";
            }
            switch (state)
            {
                case ControlSessionState.Listening:
                    messageType = MessageType.Info;
                    return "Unityの準備完了。次にBlenderの「Unityへ接続」を押します。";
                case ControlSessionState.Handshaking:
                    messageType = MessageType.Info;
                    return "Blenderとの接続内容を確認しています。";
                case ControlSessionState.Ready:
                    messageType = MessageType.Info;
                    return "Blenderへ接続済み。Pose送信の開始を待っています。";
                case ControlSessionState.Streaming:
                    if (acceptedPackets <= 0)
                    {
                        messageType = MessageType.Info;
                        return "Blenderへ接続済み。最初のPoseを待っています。";
                    }
                    messageType = MessageType.Info;
                    return "リアルタイム反映中です。";
                case ControlSessionState.Paused:
                    messageType = MessageType.Warning;
                    return "Blender側でリアルタイム反映を一時停止しています。";
                case ControlSessionState.Error:
                    messageType = MessageType.Error;
                    return "接続エラーです。BlenderとUnityの詳細設定を確認してください。";
                default:
                    messageType = MessageType.Info;
                    return "再生切替または再コンパイル後の再接続を待っています。";
            }
        }

        private void StartListener()
        {
            StopEditorTransport();
            _controlPort = Mathf.Clamp(_controlPort, 1, ushort.MaxValue);
            _posePort = Mathf.Clamp(_posePort, 1, ushort.MaxValue);
            _sessionId = Math.Max(0L, Math.Min(uint.MaxValue, _sessionId));
            try
            {
                var targetMessage = ValidateTarget(_target);
                if (targetMessage.Type != MessageType.Info || _target == null)
                    throw new InvalidOperationException(targetMessage.Text);
                var sourceMessage = ValidateSourceAvatar(_sourceAvatar);
                if (sourceMessage.Type != MessageType.Info || _sourceAvatar == null)
                    throw new InvalidOperationException(sourceMessage.Text);
                if (EditorApplication.isPlayingOrWillChangePlaymode)
                    throw new InvalidOperationException(
                        "Edit-mode preview cannot start while Unity is entering or running Play Mode.");

                _previewRequested = true;
                ConfigureRuntimePreview(_continueInPlayMode);

                _converter = new CanonicalPoseConverter(_sourceAvatar);
                var config = new PoseUdpReceiverConfig(
                    (uint)_sessionId,
                    CanonicalHumanoidV1.JointCount,
                    CanonicalHumanoidV1.RequiredJointMask,
                    _posePort);
                _receiver = new PoseUdpReceiver(config);
                var boundPosePort = _receiver.Start();
                _preview = new MotionBridgeHumanoidPreview();
                _preview.Start(_target);
                var controlConfig = new ControlServerConfig(
                    sessionId: (uint)_sessionId,
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
                    targetName: _target.name,
                    targetHumanScale: _target.humanScale);
                var diagnosticsReceiver = _receiver;
                _controlServer = new ControlServer(
                    controlConfig,
                    poseDiagnosticsProvider: () => diagnosticsReceiver.Diagnostics,
                    poseClockOffsetConsumer: diagnosticsReceiver.TrySetClockSynchronization,
                    poseClockSynchronizationResetter:
                        diagnosticsReceiver.ClearClockSynchronization);
                _controlServer.Start();
                _listenerError = null;
                _lastPoseTime = -1d;
                _streamStartedAt = -1d;
                EditorApplication.update += PumpReceiver;
            }
            catch (Exception exception)
            {
                var message = $"{exception.GetType().Name}: {exception.Message}";
                _previewRequested = false;
                StopEditorTransport();
                SetRuntimeAutoStart(false);
                _listenerError = message;
            }
        }

        private void StopListener()
        {
            _previewRequested = false;
            StopEditorTransport();
            if (_runtimePreview != null)
            {
                _runtimePreview.StopPreview();
                SetRuntimeAutoStart(false);
            }
        }

        private void StopEditorTransport()
        {
            EditorApplication.update -= PumpReceiver;
            var controlServer = _controlServer;
            var receiver = _receiver;
            var preview = _preview;
            var converter = _converter;
            _controlServer = null;
            _receiver = null;
            _preview = null;
            _converter = null;
            _lastPoseTime = -1d;
            _streamStartedAt = -1d;
            DisposeSafely(preview);
            DisposeSafely(receiver);
            DisposeSafely(controlServer);
            DisposeSafely(converter);
        }

        private void PumpReceiver()
        {
            if (_receiver == null || _controlServer == null ||
                _converter == null || _preview == null)
                return;

            try
            {
                var now = EditorApplication.timeSinceStartup;
                while (_controlServer.TryDequeueEvent(out var controlEvent))
                {
                    if (controlEvent.Kind == ControlServerEventKind.Error)
                        throw new InvalidOperationException(controlEvent.Detail);
                    if (controlEvent.Kind == ControlServerEventKind.Disconnected)
                    {
                        RestartListener(controlEvent.Detail);
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
                if (_controlServer.State == ControlSessionState.Error)
                {
                    throw new InvalidOperationException(
                        _controlServer.Diagnostics.LastError ?? "TCP control session failed.");
                }

                if (_controlServer.State == ControlSessionState.Streaming &&
                    _receiver.TryConsumeLatest(_latestFrame))
                {
                    if (!_converter.TryConvert(_latestFrame, out var conversionError))
                    {
                        throw new InvalidOperationException(
                            $"Canonical pose conversion failed: {conversionError}");
                    }
                    _preview.Sample(
                        _converter.HumanPose,
                        _converter.TrajectoryRootPosition,
                        _converter.TrajectoryRootRotation);
                    _lastPoseTime = now;
                }
                else if (_controlServer.State == ControlSessionState.Streaming &&
                         Math.Max(_lastPoseTime, _streamStartedAt) >= 0d &&
                         now - Math.Max(_lastPoseTime, _streamStartedAt) > PoseTimeoutSeconds)
                {
                    throw new TimeoutException("Blender pose stream timed out; target restored.");
                }

                if (now - _lastRepaintTime >= 0.1d)
                {
                    _lastRepaintTime = now;
                    Repaint();
                }
            }
            catch (Exception exception)
            {
                var message = $"{exception.GetType().Name}: {exception.Message}";
                if (exception is TimeoutException)
                {
                    RestartListener(message);
                    return;
                }
                _previewRequested = false;
                StopEditorTransport();
                SetRuntimeAutoStart(false);
                _listenerError = message;
                Repaint();
            }
        }

        private void RestartListener(string reason)
        {
            Debug.LogWarning(
                $"Motion Bridge control connection ended ({reason ?? "peer closed"}); " +
                "restarting the preview listener.");
            StartListener();
            if (_receiver != null)
                Debug.Log("Motion Bridge preview listener is ready for Blender to reconnect.");
            Repaint();
        }

        private void OnPlayModeStateChanged(PlayModeStateChange state)
        {
            switch (state)
            {
                case PlayModeStateChange.ExitingEditMode:
                    if (_previewRequested && _continueInPlayMode)
                        ConfigureRuntimePreview(true);
                    StopEditorTransport();
                    break;
                case PlayModeStateChange.EnteredPlayMode:
                    if (_previewRequested && _continueInPlayMode && _runtimePreview != null)
                        _runtimePreview.EnsureStarted();
                    Repaint();
                    break;
                case PlayModeStateChange.ExitingPlayMode:
                    if (_runtimePreview != null)
                        _runtimePreview.StopPreview();
                    break;
                case PlayModeStateChange.EnteredEditMode:
                    if (_previewRequested)
                        QueueResumeInEditMode();
                    break;
            }
        }

        private void QueueResumeInEditMode()
        {
            EditorApplication.delayCall -= ResumeInEditMode;
            EditorApplication.delayCall += ResumeInEditMode;
        }

        private void ResumeInEditMode()
        {
            if (this == null || !_previewRequested ||
                EditorApplication.isPlayingOrWillChangePlaymode || _receiver != null)
                return;

            StartListener();
            Repaint();
        }

        private void ConfigureRuntimePreview(bool autoStart)
        {
            if (_target == null)
                return;

            if (_runtimePreview == null || _runtimePreview.gameObject != _target.gameObject)
                _runtimePreview = _target.GetComponent<MotionBridgeRuntimePreview>();
            if (_runtimePreview == null)
                _runtimePreview = Undo.AddComponent<MotionBridgeRuntimePreview>(_target.gameObject);

            _runtimePreview.Configure(
                _target,
                _sourceAvatar,
                _controlPort,
                _posePort,
                (uint)_sessionId,
                autoStart);
            EditorUtility.SetDirty(_runtimePreview);
        }

        private void SetRuntimeAutoStart(bool autoStart)
        {
            if (_runtimePreview == null)
                return;

            _runtimePreview.Configure(
                _target,
                _sourceAvatar,
                _controlPort,
                _posePort,
                (uint)Math.Max(0L, Math.Min(uint.MaxValue, _sessionId)),
                autoStart);
            EditorUtility.SetDirty(_runtimePreview);
        }

        private void DrawDiagnostics()
        {
            if (!string.IsNullOrEmpty(_listenerError))
            {
                EditorGUILayout.HelpBox(_listenerError, MessageType.Error);
                return;
            }
            if (_receiver == null)
            {
                if (EditorApplication.isPlaying && _runtimePreview != null)
                {
                    if (!string.IsNullOrEmpty(_runtimePreview.LastError))
                    {
                        EditorGUILayout.HelpBox(
                            _runtimePreview.LastError,
                            MessageType.Error);
                    }
                    else
                    {
                        EditorGUILayout.HelpBox(
                            _runtimePreview.IsRunning
                                ? "Runtime streaming is active in Play Mode."
                                : "Runtime receiver is waiting to start.",
                            MessageType.Info);
                    }
                    return;
                }
                EditorGUILayout.HelpBox(
                    "Import the Canonical Source Avatar sample, start Preview, then start Live Pose in Blender with the same TCP port and session ID. UDP is negotiated automatically.",
                    MessageType.Info);
                return;
            }

            var diagnostics = _receiver.Diagnostics;
            EditorGUILayout.LabelField("Control", _controlServer?.State.ToString() ?? "Stopped");
            EditorGUILayout.LabelField("TCP port", _controlServer?.BoundPort.ToString() ?? "-");
            EditorGUILayout.LabelField("UDP port", _receiver.BoundPort.ToString());
            EditorGUILayout.LabelField("Accepted", diagnostics.AcceptedPackets.ToString());
            EditorGUILayout.LabelField("Sequence gaps", diagnostics.SequenceGapDrops.ToString());
            EditorGUILayout.LabelField("Superseded", diagnostics.SupersededFrames.ToString());
            EditorGUILayout.LabelField(
                "Rejected",
                (diagnostics.DecodeRejectedPackets + diagnostics.SessionRejectedPackets).ToString());
            EditorGUILayout.LabelField(
                "Clock sync",
                diagnostics.ClockSynchronized ? "Synchronized" : "Waiting");
            if (diagnostics.HasOneWayAge)
            {
                EditorGUILayout.LabelField(
                    "One-way age p50",
                    $"{diagnostics.OneWayAgeUsecP50 / 1000.0:F2} ms");
                EditorGUILayout.LabelField(
                    "One-way age p95",
                    $"{diagnostics.OneWayAgeUsecP95 / 1000.0:F2} ms");
            }
            if (_lastPoseTime >= 0d)
            {
                var ageMs = (EditorApplication.timeSinceStartup - _lastPoseTime) * 1000d;
                EditorGUILayout.LabelField("Latest sequence", _latestFrame.Sequence.ToString());
                EditorGUILayout.LabelField("Blender frame", _latestFrame.FrameIndex.ToString());
                EditorGUILayout.LabelField("Local age", $"{ageMs:F1} ms");
            }
            else
            {
                EditorGUILayout.LabelField("Latest pose", "Waiting");
            }
        }

        private static (string Text, MessageType Type) ValidateTarget(Animator target)
        {
            if (target == null)
                return ("Select a scene Humanoid Animator.", MessageType.Info);
            if (EditorUtility.IsPersistent(target) || !target.gameObject.scene.IsValid())
                return ("Select a scene instance, not a project asset.", MessageType.Error);
            if (!target.gameObject.activeInHierarchy)
                return ("The target must be active in the hierarchy.", MessageType.Error);
            if (target.avatar == null)
                return ("The Animator has no Avatar.", MessageType.Error);
            if (!target.avatar.isValid || !target.avatar.isHuman || !target.isHuman)
                return ("The Avatar must be a valid Humanoid.", MessageType.Error);
            if (!HumanoidTargetScale.IsPositiveUniformHierarchy(target.transform))
            {
                return (
                    "The target and every parent require positive uniform scale.",
                    MessageType.Error);
            }

            return ("Humanoid target is valid.", MessageType.Info);
        }

        private static (string Text, MessageType Type) ValidateSourceAvatar(Avatar avatar)
        {
            if (avatar == null)
                return ("Select the imported Canonical Source Avatar.", MessageType.Info);
            if (!avatar.isValid || !avatar.isHuman)
                return ("The Source Avatar must be a valid Humanoid.", MessageType.Error);
            var path = AssetDatabase.GetAssetPath(avatar);
            if (!string.Equals(
                    Path.GetFileNameWithoutExtension(path),
                    CanonicalSourceAvatarPostprocessor.AssetName,
                    StringComparison.OrdinalIgnoreCase))
            {
                return (
                    "The Source Avatar must come from MB_Canonical_Humanoid_v1.fbx.",
                    MessageType.Error);
            }
            return ("Canonical Source Avatar is valid.", MessageType.Info);
        }

        private static string ComputeMuscleNamesHash()
        {
            var payload = Encoding.UTF8.GetBytes(string.Join("\n", HumanTrait.MuscleName));
            using (var sha256 = SHA256.Create())
            {
                var hash = sha256.ComputeHash(payload);
                var result = new StringBuilder(hash.Length * 2);
                foreach (var value in hash)
                    result.Append(value.ToString("x2"));
                return result.ToString();
            }
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
    }
}
