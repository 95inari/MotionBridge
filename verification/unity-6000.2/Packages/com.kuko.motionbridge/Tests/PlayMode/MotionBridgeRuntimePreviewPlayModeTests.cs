using System;
using System.Collections;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using Kuko.MotionBridge.Live;
using Kuko.MotionBridge.Protocol;
using Kuko.MotionBridge.Retargeting;
using Kuko.MotionBridge.Transport;
using NUnit.Framework;
#if UNITY_EDITOR
using UnityEditor;
using UnityEditor.Animations;
#endif
using UnityEngine;
using UnityEngine.TestTools;

namespace Kuko.MotionBridge.Tests
{
    public sealed class MotionBridgeRuntimePreviewPlayModeTests :
        IPrebuildSetup,
        IPostBuildCleanup
    {
        private const string ResourceFolder =
            "Assets/Resources/__MotionBridgeRuntimePreviewTest";
        private const string ImportedFbx =
            ResourceFolder + "/MB_Canonical_Humanoid_v1.fbx";
        private const string ResourceKey =
            "__MotionBridgeRuntimePreviewTest/MB_Canonical_Humanoid_v1";
        private const string ControllerAsset =
            ResourceFolder + "/MB_RuntimePreviewTest.controller";
        private const string ControllerResourceKey =
            "__MotionBridgeRuntimePreviewTest/MB_RuntimePreviewTest";
        private const uint SessionId = 0x12345678u;

        private GameObject _instance;
        private Animator _animator;
        private Avatar _sourceAvatar;
        private MotionBridgeRuntimePreview _preview;
        private RuntimeAnimatorController _controller;

        public void Setup()
        {
#if UNITY_EDITOR
            var package = UnityEditor.PackageManager.PackageInfo.FindForAssembly(
                typeof(MotionBridgeRuntimePreviewPlayModeTests).Assembly);
            if (package == null)
                throw new InvalidOperationException("Motion Bridge package was not resolved.");

            var sourceFbx = Path.Combine(
                package.resolvedPath,
                "Samples~",
                "Canonical",
                "MB_Canonical_Humanoid_v1.fbx");
            if (!File.Exists(sourceFbx))
                throw new FileNotFoundException("Canonical sample FBX was not found.", sourceFbx);

            var importDirectory = Path.Combine(
                Directory.GetParent(Application.dataPath).FullName,
                ResourceFolder);
            Directory.CreateDirectory(importDirectory);
            File.Copy(sourceFbx, Path.Combine(importDirectory, Path.GetFileName(ImportedFbx)), true);
            AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);

            AssetDatabase.DeleteAsset(ControllerAsset);
            var controller = AnimatorController.CreateAnimatorControllerAtPath(ControllerAsset);
            if (controller == null)
                throw new InvalidOperationException("Test Animator Controller was not created.");
            AssetDatabase.SaveAssets();

            var imported = AssetDatabase.LoadAssetAtPath<GameObject>(ImportedFbx);
            var avatar = AssetDatabase.LoadAllAssetsAtPath(ImportedFbx)
                .OfType<Avatar>()
                .FirstOrDefault(value => value.isValid && value.isHuman);
            if (imported == null || avatar == null)
                throw new InvalidOperationException(
                    "Canonical sample FBX did not import as a valid Humanoid.");
#endif
        }

        public void Cleanup()
        {
#if UNITY_EDITOR
            AssetDatabase.DeleteAsset(ResourceFolder);
#endif
        }

        [SetUp]
        public void SetUpTest()
        {
            var prefab = Resources.Load<GameObject>(ResourceKey);
            Assert.That(prefab, Is.Not.Null, ResourceKey);
            _sourceAvatar = Resources.LoadAll<Avatar>(ResourceKey)
                .Single(value => value.isValid && value.isHuman);
            _instance = UnityEngine.Object.Instantiate(prefab);
            _animator = _instance.GetComponent<Animator>();
            Assert.That(_animator, Is.Not.Null);
            Assert.That(_animator.isHuman, Is.True);
        }

        [UnityTearDown]
        public IEnumerator TearDownTest()
        {
            _preview?.StopPreview();
            if (_instance != null)
                UnityEngine.Object.Destroy(_instance);
            _preview = null;
            _controller = null;
            _instance = null;
            _animator = null;
            _sourceAvatar = null;
            yield return null;
        }

        [UnityTest]
        public IEnumerator StartStopBindsPortsAndRestoresAnimatorAndTransforms()
        {
            var controlPort = GetFreeTcpPort();
            var posePort = GetFreeUdpPort();
            _controller = Resources.Load<RuntimeAnimatorController>(ControllerResourceKey);
            Assert.That(_controller, Is.Not.Null, ControllerResourceKey);
            _animator.runtimeAnimatorController = _controller;
            _animator.enabled = false;
            _animator.applyRootMotion = true;
            _animator.cullingMode = AnimatorCullingMode.CullCompletely;
            _animator.updateMode = AnimatorUpdateMode.UnscaledTime;
            _animator.speed = 0.65f;
            _animator.fireEvents = false;

            var rootBefore = TransformState.Capture(_animator.transform);
            var arm = _animator.GetBoneTransform(HumanBodyBones.LeftUpperArm);
            Assert.That(arm, Is.Not.Null);
            var armBefore = TransformState.Capture(arm);

            _preview = _instance.AddComponent<MotionBridgeRuntimePreview>();
            _preview.Configure(
                _animator,
                _sourceAvatar,
                controlPort,
                posePort,
                SessionId,
                autoStart: false);
            _preview.StartPreview();

            Assert.That(_preview.IsRunning, Is.True, _preview.LastError);
            Assert.That(_preview.ControlState, Is.EqualTo(ControlSessionState.Listening));
            Assert.That(_preview.BoundControlPort, Is.EqualTo(controlPort));
            Assert.That(_preview.BoundPosePort, Is.EqualTo(posePort));
            Assert.That(_animator.enabled, Is.True);
            Assert.That(_animator.runtimeAnimatorController, Is.Null);
            Assert.That(_animator.applyRootMotion, Is.False);
            Assert.That(_animator.cullingMode, Is.EqualTo(AnimatorCullingMode.AlwaysAnimate));

            _animator.transform.localPosition += new Vector3(1f, 2f, 3f);
            _animator.transform.localRotation = Quaternion.Euler(13f, 29f, -7f);
            arm.localPosition += new Vector3(0.1f, -0.2f, 0.3f);
            arm.localRotation = Quaternion.Euler(-21f, 9f, 33f);
            arm.localScale = new Vector3(1.1f, 1.1f, 1.1f);

            _preview.StopPreview();
            _preview.StopPreview();

            Assert.That(_preview.IsRunning, Is.False);
            Assert.That(_preview.ControlState, Is.EqualTo(ControlSessionState.Disconnected));
            Assert.That(_preview.BoundControlPort, Is.Zero);
            Assert.That(_preview.BoundPosePort, Is.Zero);
            Assert.That(_animator.enabled, Is.False);
            Assert.That(_animator.runtimeAnimatorController, Is.SameAs(_controller));
            Assert.That(_animator.applyRootMotion, Is.True);
            Assert.That(_animator.cullingMode, Is.EqualTo(AnimatorCullingMode.CullCompletely));
            Assert.That(_animator.updateMode, Is.EqualTo(AnimatorUpdateMode.UnscaledTime));
            Assert.That(_animator.speed, Is.EqualTo(0.65f));
            Assert.That(_animator.fireEvents, Is.False);
            rootBefore.AssertEqual(_animator.transform);
            armBefore.AssertEqual(arm);

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

            yield return null;
        }

        [UnityTest]
        public IEnumerator PeerCloseRebindKeepsWriterAndAcceptsSequenceReset()
        {
            var controlPort = GetFreeTcpPort();
            var posePort = GetFreeUdpPort();
            _preview = _instance.AddComponent<MotionBridgeRuntimePreview>();
            _preview.Configure(
                _animator,
                _sourceAvatar,
                controlPort,
                posePort,
                SessionId,
                autoStart: false);
            _preview.StartPreview();
            var writer = GetWriter(_preview);
            Assert.That(writer, Is.Not.Null);

            using (var sender = new UdpClient(AddressFamily.InterNetwork))
                SendPose(sender, posePort, 100u);
            yield return WaitUntil(
                () => _preview.ReceiverDiagnostics.AcceptedPackets == 1,
                "The first runtime UDP receiver did not accept sequence 100.");
            Assert.That(_preview.ReceiverDiagnostics.LastAcceptedSequence, Is.EqualTo(100u));

            using (var client = new TcpClient(AddressFamily.InterNetwork))
                client.Connect(IPAddress.Loopback, controlPort);

            yield return WaitUntil(
                () => _preview.IsRunning &&
                      _preview.ControlState == ControlSessionState.Listening &&
                      _preview.ReceiverDiagnostics.AcceptedPackets == 0 &&
                      !_preview.ReceiverDiagnostics.HasAcceptedSequence,
                "Runtime transport was not rebuilt after the TCP peer closed.");

            Assert.That(GetWriter(_preview), Is.SameAs(writer));
            Assert.That(_preview.BoundControlPort, Is.EqualTo(controlPort));
            Assert.That(_preview.BoundPosePort, Is.EqualTo(posePort));

            using (var sender = new UdpClient(AddressFamily.InterNetwork))
                SendPose(sender, posePort, 0u);
            yield return WaitUntil(
                () => _preview.ReceiverDiagnostics.AcceptedPackets == 1,
                "The rebuilt runtime UDP receiver did not accept sequence 0.");

            var diagnostics = _preview.ReceiverDiagnostics;
            Assert.That(diagnostics.LastAcceptedSequence, Is.Zero);
            Assert.That(diagnostics.StalePackets, Is.Zero);
            Assert.That(diagnostics.SessionRejectedPackets, Is.Zero);
            Assert.That(diagnostics.SequenceGapDrops, Is.Zero);
        }

        private static IEnumerator WaitUntil(Func<bool> predicate, string failure)
        {
            for (var attempt = 0; attempt < 200; attempt++)
            {
                if (predicate())
                    yield break;
                yield return new WaitForSecondsRealtime(0.01f);
            }
            Assert.Fail(failure);
        }

        private static HumanoidPosePlayable GetWriter(MotionBridgeRuntimePreview preview)
        {
            return typeof(MotionBridgeRuntimePreview)
                .GetField("_writer", BindingFlags.Instance | BindingFlags.NonPublic)
                ?.GetValue(preview) as HumanoidPosePlayable;
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
                SessionId,
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
                sender.Send(packet, bytesWritten, new IPEndPoint(IPAddress.Loopback, port)),
                Is.EqualTo(bytesWritten));
        }

        private readonly struct TransformState
        {
            private readonly Vector3 _localPosition;
            private readonly Quaternion _localRotation;
            private readonly Vector3 _localScale;

            private TransformState(Transform transform)
            {
                _localPosition = transform.localPosition;
                _localRotation = transform.localRotation;
                _localScale = transform.localScale;
            }

            public static TransformState Capture(Transform transform)
            {
                return new TransformState(transform);
            }

            public void AssertEqual(Transform transform)
            {
                Assert.That(
                    Vector3.Distance(transform.localPosition, _localPosition),
                    Is.LessThan(0.00001f));
                Assert.That(
                    Quaternion.Angle(transform.localRotation, _localRotation),
                    Is.LessThan(0.001f));
                Assert.That(
                    Vector3.Distance(transform.localScale, _localScale),
                    Is.LessThan(0.00001f));
            }
        }
    }
}
