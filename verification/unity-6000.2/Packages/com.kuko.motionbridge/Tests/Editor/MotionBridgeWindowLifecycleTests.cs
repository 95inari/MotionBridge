using System.Collections;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using Kuko.MotionBridge.Editor;
using Kuko.MotionBridge.Editor.Preview;
using Kuko.MotionBridge.Live;
using Kuko.MotionBridge.Protocol;
using Kuko.MotionBridge.Retargeting;
using Kuko.MotionBridge.Transport;
using NUnit.Framework;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.TestTools;

namespace Kuko.MotionBridge.Tests
{
    public sealed class MotionBridgeWindowLifecycleTests
    {
        private const string ImportFolder = "Assets/__MotionBridgeWindowLifecycleTest";
        private const string ImportedFbx = ImportFolder + "/MB_Canonical_Humanoid_v1.fbx";
        private static readonly BindingFlags PrivateInstance =
            BindingFlags.Instance | BindingFlags.NonPublic;

        [OneTimeSetUp]
        public void OneTimeSetUp()
        {
            var package = UnityEditor.PackageManager.PackageInfo.FindForAssembly(
                typeof(MotionBridgeWindowLifecycleTests).Assembly);
            Assert.That(package, Is.Not.Null);
            var sourceFbx = Path.Combine(
                package.resolvedPath,
                "Samples~",
                "Canonical",
                "MB_Canonical_Humanoid_v1.fbx");
            Assert.That(File.Exists(sourceFbx), Is.True, sourceFbx);

            var importDirectory = Path.Combine(
                Directory.GetParent(Application.dataPath).FullName,
                ImportFolder);
            Directory.CreateDirectory(importDirectory);
            File.Copy(sourceFbx, Path.Combine(importDirectory, Path.GetFileName(ImportedFbx)), true);
            AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
        }

        [OneTimeTearDown]
        public void OneTimeTearDown()
        {
            EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            AssetDatabase.DeleteAsset(ImportFolder);
        }

        [UnityTest]
        public IEnumerator CleanPeerCloseRebuildsTheWholePreviewSession()
        {
            EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            var prefab = AssetDatabase.LoadAssetAtPath<GameObject>(ImportedFbx);
            var sourceAvatar = AssetDatabase.LoadAllAssetsAtPath(ImportedFbx)
                .OfType<Avatar>()
                .Single(avatar => avatar.isValid && avatar.isHuman);
            var instance = Object.Instantiate(prefab);
            var animator = instance.GetComponent<Animator>();
            var window = ScriptableObject.CreateInstance<MotionBridgeWindow>();
            try
            {
                Assert.That(animator, Is.Not.Null);
                SetField(window, "_target", animator);
                SetField(window, "_sourceAvatar", sourceAvatar);
                SetField(window, "_controlPort", GetFreeTcpPort());
                SetField(window, "_posePort", GetFreeUdpPort());
                Invoke(window, "StartListener");

                var firstServer = GetField<ControlServer>(window, "_controlServer");
                var firstReceiver = GetField<PoseUdpReceiver>(window, "_receiver");
                var firstPreview = GetField<MotionBridgeHumanoidPreview>(window, "_preview");
                Assert.That(firstServer, Is.Not.Null);
                Assert.That(firstReceiver, Is.Not.Null);
                Assert.That(firstPreview?.IsRunning, Is.True);

                using (var sender = new UdpClient(AddressFamily.InterNetwork))
                    SendPose(sender, firstReceiver.BoundPort, 100u);
                for (var frame = 0;
                     frame < 120 && firstReceiver.Diagnostics.AcceptedPackets < 1;
                     frame++)
                    yield return null;
                Assert.That(firstReceiver.Diagnostics.AcceptedPackets, Is.EqualTo(1));
                Assert.That(firstReceiver.Diagnostics.LastAcceptedSequence, Is.EqualTo(100u));

                LogAssert.Expect(
                    LogType.Warning,
                    new System.Text.RegularExpressions.Regex(
                        "Motion Bridge control connection ended.*restarting"));
                using (var client = new TcpClient(AddressFamily.InterNetwork))
                    client.Connect(IPAddress.Loopback, firstServer.BoundPort);

                ControlServer restartedServer = null;
                PoseUdpReceiver restartedReceiver = null;
                for (var frame = 0; frame < 120; frame++)
                {
                    yield return null;
                    restartedServer = GetField<ControlServer>(window, "_controlServer");
                    restartedReceiver = GetField<PoseUdpReceiver>(window, "_receiver");
                    if (restartedServer != null &&
                        !ReferenceEquals(restartedServer, firstServer))
                        break;
                }

                Assert.That(restartedServer, Is.Not.Null);
                Assert.That(restartedServer, Is.Not.SameAs(firstServer));
                Assert.That(restartedReceiver, Is.Not.Null);
                Assert.That(restartedReceiver, Is.Not.SameAs(firstReceiver));
                Assert.That(restartedServer.IsRunning, Is.True);
                Assert.That(restartedServer.State, Is.EqualTo(ControlSessionState.Listening));
                Assert.That(restartedReceiver.IsRunning, Is.True);
                Assert.That(restartedReceiver.Diagnostics.AcceptedPackets, Is.Zero);
                Assert.That(restartedReceiver.Diagnostics.HasAcceptedSequence, Is.False);
                Assert.That(firstServer.IsRunning, Is.False);
                Assert.That(firstReceiver.IsRunning, Is.False);
                var restartedPreview =
                    GetField<MotionBridgeHumanoidPreview>(window, "_preview");
                Assert.That(restartedPreview, Is.Not.SameAs(firstPreview));
                Assert.That(restartedPreview?.IsRunning, Is.True);
                Assert.That(GetField<string>(window, "_listenerError"), Is.Null);

                using (var sender = new UdpClient(AddressFamily.InterNetwork))
                    SendPose(sender, restartedReceiver.BoundPort, 0u);
                for (var frame = 0;
                     frame < 120 && restartedReceiver.Diagnostics.AcceptedPackets < 1;
                     frame++)
                    yield return null;
                var diagnostics = restartedReceiver.Diagnostics;
                Assert.That(diagnostics.AcceptedPackets, Is.EqualTo(1));
                Assert.That(diagnostics.LastAcceptedSequence, Is.Zero);
                Assert.That(diagnostics.StalePackets, Is.Zero);
                Assert.That(diagnostics.SessionRejectedPackets, Is.Zero);
                Assert.That(diagnostics.SequenceGapDrops, Is.Zero);
            }
            finally
            {
                Invoke(window, "StopListener");
                Object.DestroyImmediate(window);
                Object.DestroyImmediate(instance);
            }
        }

        [UnityTest]
        public IEnumerator PoseTimeoutRebuildsTheWholePreviewSession()
        {
            EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            var prefab = AssetDatabase.LoadAssetAtPath<GameObject>(ImportedFbx);
            var sourceAvatar = AssetDatabase.LoadAllAssetsAtPath(ImportedFbx)
                .OfType<Avatar>()
                .Single(avatar => avatar.isValid && avatar.isHuman);
            var instance = Object.Instantiate(prefab);
            var animator = instance.GetComponent<Animator>();
            var window = ScriptableObject.CreateInstance<MotionBridgeWindow>();
            try
            {
                Assert.That(animator, Is.Not.Null);
                SetField(window, "_target", animator);
                SetField(window, "_sourceAvatar", sourceAvatar);
                SetField(window, "_controlPort", GetFreeTcpPort());
                SetField(window, "_posePort", GetFreeUdpPort());
                Invoke(window, "StartListener");

                var firstServer = GetField<ControlServer>(window, "_controlServer");
                var firstReceiver = GetField<PoseUdpReceiver>(window, "_receiver");
                var firstPreview = GetField<MotionBridgeHumanoidPreview>(window, "_preview");
                Assert.That(firstServer, Is.Not.Null);
                Assert.That(firstReceiver, Is.Not.Null);
                Assert.That(firstPreview?.IsRunning, Is.True);

                // Negative timestamps are sentinels in MotionBridgeWindow. On a very
                // fast batch-mode startup, now - 3 can still be negative and would
                // make this timeout test depend on editor launch speed.
                while (EditorApplication.timeSinceStartup <= 3.1d)
                    yield return null;
                Invoke(firstServer, "SetState", ControlSessionState.Streaming);
                SetField(window, "_lastPoseTime", -1d);
                SetField(
                    window,
                    "_streamStartedAt",
                    EditorApplication.timeSinceStartup - 3d);
                LogAssert.Expect(
                    LogType.Warning,
                    new System.Text.RegularExpressions.Regex(
                        "Motion Bridge control connection ended.*timed out.*restarting"));
                Invoke(window, "PumpReceiver");

                var restartedServer = GetField<ControlServer>(window, "_controlServer");
                var restartedReceiver = GetField<PoseUdpReceiver>(window, "_receiver");
                var restartedPreview =
                    GetField<MotionBridgeHumanoidPreview>(window, "_preview");
                Assert.That(restartedServer, Is.Not.SameAs(firstServer));
                Assert.That(restartedReceiver, Is.Not.SameAs(firstReceiver));
                Assert.That(restartedPreview, Is.Not.SameAs(firstPreview));
                Assert.That(restartedServer?.IsRunning, Is.True);
                Assert.That(restartedServer?.State, Is.EqualTo(ControlSessionState.Listening));
                Assert.That(restartedReceiver?.IsRunning, Is.True);
                Assert.That(restartedPreview?.IsRunning, Is.True);
                Assert.That(firstServer.IsRunning, Is.False);
                Assert.That(firstReceiver.IsRunning, Is.False);
                Assert.That(firstPreview.IsRunning, Is.False);
                Assert.That(GetField<string>(window, "_listenerError"), Is.Null);
            }
            finally
            {
                Invoke(window, "StopListener");
                Object.DestroyImmediate(window);
                Object.DestroyImmediate(instance);
            }
        }

        [Test]
        public void ExitingEditModeHandsPortsToConfiguredRuntimePreview()
        {
            EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            var prefab = AssetDatabase.LoadAssetAtPath<GameObject>(ImportedFbx);
            var sourceAvatar = AssetDatabase.LoadAllAssetsAtPath(ImportedFbx)
                .OfType<Avatar>()
                .Single(avatar => avatar.isValid && avatar.isHuman);
            var instance = Object.Instantiate(prefab);
            var animator = instance.GetComponent<Animator>();
            var controlPort = GetFreeTcpPort();
            var posePort = GetFreeUdpPort();
            var window = ScriptableObject.CreateInstance<MotionBridgeWindow>();
            try
            {
                Assert.That(animator, Is.Not.Null);
                SetField(window, "_target", animator);
                SetField(window, "_sourceAvatar", sourceAvatar);
                SetField(window, "_controlPort", controlPort);
                SetField(window, "_posePort", posePort);
                Invoke(window, "StartListener");

                var runtimePreview =
                    animator.GetComponent<MotionBridgeRuntimePreview>();
                Assert.That(runtimePreview, Is.Not.Null);
                Assert.That(runtimePreview.AutoStart, Is.True);
                Assert.That(runtimePreview.Target, Is.SameAs(animator));
                Assert.That(runtimePreview.SourceAvatar, Is.SameAs(sourceAvatar));

                Invoke(
                    window,
                    "OnPlayModeStateChanged",
                    PlayModeStateChange.ExitingEditMode);

                Assert.That(GetField<ControlServer>(window, "_controlServer"), Is.Null);
                Assert.That(GetField<PoseUdpReceiver>(window, "_receiver"), Is.Null);
                Assert.That(
                    GetField<MotionBridgeHumanoidPreview>(window, "_preview"),
                    Is.Null);
                Assert.That(GetValue<bool>(window, "_previewRequested"), Is.True);
                Assert.That(runtimePreview.AutoStart, Is.True);

                var listener = new TcpListener(IPAddress.Loopback, controlPort);
                try
                {
                    listener.Start();
                    using (var receiver = new UdpClient(
                               new IPEndPoint(IPAddress.Loopback, posePort)))
                    {
                        Assert.That(
                            ((IPEndPoint)listener.LocalEndpoint).Port,
                            Is.EqualTo(controlPort));
                        Assert.That(
                            ((IPEndPoint)receiver.Client.LocalEndPoint).Port,
                            Is.EqualTo(posePort));
                    }
                }
                finally
                {
                    listener.Stop();
                }
            }
            finally
            {
                Invoke(window, "StopListener");
                Object.DestroyImmediate(window);
                Object.DestroyImmediate(instance);
            }
        }

        [TestCase(
            false,
            ControlSessionState.Streaming,
            1L,
            "上の設定が終わったら")]
        [TestCase(
            true,
            ControlSessionState.Listening,
            0L,
            "Unityの準備完了")]
        [TestCase(
            true,
            ControlSessionState.Streaming,
            0L,
            "最初のPose")]
        [TestCase(
            true,
            ControlSessionState.Streaming,
            1L,
            "リアルタイム反映中")]
        [TestCase(
            true,
            ControlSessionState.Paused,
            1L,
            "一時停止")]
        public void BeginnerGuideDistinguishesReceiverConnectionAndPose(
            bool requested,
            ControlSessionState state,
            long acceptedPackets,
            string expectedText)
        {
            var arguments = new object[]
            {
                requested,
                false,
                true,
                state,
                acceptedPackets,
                null,
                null,
            };
            var message = (string)InvokeStatic(
                "ResolveBeginnerConnectionMessage",
                arguments);
            Assert.That(message, Does.Contain(expectedText));
            Assert.That(
                arguments[6],
                Is.EqualTo(
                    state == ControlSessionState.Paused
                        ? MessageType.Warning
                        : MessageType.Info));
        }

        [Test]
        public void BeginnerGuideExplainsHowToRecoverFromCanonicalMismatch()
        {
            var arguments = new object[]
            {
                true,
                false,
                true,
                ControlSessionState.Error,
                0L,
                "InvalidOperationException: ControlProtocolException: " +
                    "hello canonical skeleton does not match.",
                null,
            };

            var message = (string)InvokeStatic(
                "ResolveBeginnerConnectionMessage",
                arguments);

            Assert.That(message, Does.Contain("同じ配布セット"));
            Assert.That(message, Does.Contain("プレビュー待受を開始"));
            Assert.That(message, Does.Contain("Unityへ接続"));
            Assert.That(arguments[6], Is.EqualTo(MessageType.Error));
        }

        [Test]
        public void BeginnerGuideFindsAnImportedCanonicalAvatar()
        {
            var arguments = new object[] { null, null };
            var found = (bool)InvokeStatic("TryFindCanonicalAvatar", arguments);
            Assert.That(found, Is.True, arguments[1] as string);
            var avatar = arguments[0] as Avatar;
            Assert.That(avatar, Is.Not.Null);
            Assert.That(avatar.isHuman && avatar.isValid, Is.True);
            Assert.That(
                Path.GetFileNameWithoutExtension(AssetDatabase.GetAssetPath(avatar)),
                Is.EqualTo("MB_Canonical_Humanoid_v1"));
        }

        [Test]
        public void BeginnerGuideRejectsCompensatedNonUniformParentScale()
        {
            EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            var prefab = AssetDatabase.LoadAssetAtPath<GameObject>(ImportedFbx);
            var instance = Object.Instantiate(prefab);
            var parent = new GameObject("NonUniformParent");
            try
            {
                var animator = instance.GetComponent<Animator>();
                Assert.That(animator, Is.Not.Null);
                animator.transform.SetParent(parent.transform, false);
                parent.transform.localScale = new Vector3(2f, 1f, 1f);
                animator.transform.localScale = new Vector3(0.5f, 1f, 1f);
                Assert.That(animator.transform.lossyScale.x, Is.EqualTo(1f).Within(0.0001f));
                Assert.That(animator.transform.lossyScale.y, Is.EqualTo(1f).Within(0.0001f));
                Assert.That(
                    (bool)InvokeStatic("IsTargetValid", new object[] { animator }),
                    Is.False,
                    "The guide must use the same parent-scale gate as preview startup");
            }
            finally
            {
                Object.DestroyImmediate(parent);
                if (instance != null)
                    Object.DestroyImmediate(instance);
            }
        }

        private static int GetFreeTcpPort()
        {
            var listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start();
            try
            {
                return ((IPEndPoint)listener.LocalEndpoint).Port;
            }
            finally
            {
                listener.Stop();
            }
        }

        private static int GetFreeUdpPort()
        {
            using (var client = new UdpClient(new IPEndPoint(IPAddress.Loopback, 0)))
                return ((IPEndPoint)client.Client.LocalEndPoint).Port;
        }

        private static void SendPose(UdpClient sender, int port, uint sequence)
        {
            var joints = Enumerable.Repeat(
                Quaternion.identity,
                CanonicalHumanoidV1.JointCount).ToArray();
            var packet = new byte[
                MotionBridgeProtocol.PosePacketBytes(CanonicalHumanoidV1.JointCount)];
            var source = new PosePacketSource(
                0x12345678u,
                sequence,
                sequence * 1000ul,
                (int)sequence,
                PosePacketFlags.None,
                Vector3.zero,
                Quaternion.identity,
                Vector3.zero,
                CanonicalHumanoidV1.JointCount,
                CanonicalHumanoidV1.RequiredJointMask,
                joints,
                new float[4],
                new uint[4],
                new Vector3[4],
                Enumerable.Repeat(Quaternion.identity, 4).ToArray(),
                new Vector4(0f, 1f, 0f, 0f));
            Assert.That(
                PosePacketEncoder.TryEncode(
                    in source,
                    packet,
                    out var bytesWritten,
                    out var error),
                Is.True,
                error.ToString());
            Assert.That(
                sender.Send(
                    packet,
                    bytesWritten,
                    new IPEndPoint(IPAddress.Loopback, port)),
                Is.EqualTo(bytesWritten));
        }

        private static T GetField<T>(object target, string name) where T : class
        {
            return typeof(MotionBridgeWindow).GetField(name, PrivateInstance)?.GetValue(target) as T;
        }

        private static T GetValue<T>(object target, string name)
        {
            var field = typeof(MotionBridgeWindow).GetField(name, PrivateInstance);
            Assert.That(field, Is.Not.Null, name);
            return (T)field.GetValue(target);
        }

        private static void SetField(object target, string name, object value)
        {
            var field = typeof(MotionBridgeWindow).GetField(name, PrivateInstance);
            Assert.That(field, Is.Not.Null, name);
            field.SetValue(target, value);
        }

        private static void Invoke(object target, string name, params object[] arguments)
        {
            var method = target.GetType().GetMethod(name, PrivateInstance);
            Assert.That(method, Is.Not.Null, name);
            method.Invoke(target, arguments);
        }

        private static object InvokeStatic(string name, object[] arguments)
        {
            var method = typeof(MotionBridgeWindow).GetMethod(
                name,
                BindingFlags.Static | BindingFlags.NonPublic);
            Assert.That(method, Is.Not.Null, name);
            return method.Invoke(null, arguments);
        }
    }
}
