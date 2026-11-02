using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Kuko.MotionBridge.Editor.Preview;
using NUnit.Framework;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Kuko.MotionBridge.Tests
{
    public sealed class MotionBridgeHumanoidPreviewTests
    {
        private const string ImportFolder = "Assets/__MotionBridgeHumanoidPreviewTest";
        private const string ImportedFbx = ImportFolder + "/MB_Canonical_Humanoid_v1.fbx";
        private const string TestSceneAsset = ImportFolder + "/PreviewTestScene.unity";

        private Scene _testScene;
        private GameObject _instance;
        private Animator _animator;
        private Avatar _runtimeAvatar;
        private MotionBridgeHumanoidPreview _preview;

        [OneTimeSetUp]
        public void OneTimeSetUp()
        {
            var package = UnityEditor.PackageManager.PackageInfo.FindForAssembly(
                typeof(MotionBridgeHumanoidPreviewTests).Assembly);
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
            Assert.That(AssetDatabase.LoadAssetAtPath<GameObject>(ImportedFbx), Is.Not.Null);
        }

        [OneTimeTearDown]
        public void OneTimeTearDown()
        {
            EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            AssetDatabase.DeleteAsset(ImportFolder);
        }

        [SetUp]
        public void SetUp()
        {
            _testScene = EditorSceneManager.NewScene(
                NewSceneSetup.EmptyScene,
                NewSceneMode.Single);

            var prefab = AssetDatabase.LoadAssetAtPath<GameObject>(ImportedFbx);
            _instance = UnityEngine.Object.Instantiate(prefab);
            SceneManager.MoveGameObjectToScene(_instance, _testScene);
            _animator = _instance.GetComponent<Animator>();
            Assert.That(_animator, Is.Not.Null);
            Assert.That(_animator.isHuman, Is.True);
            _preview = new MotionBridgeHumanoidPreview();
            Assert.That(EditorSceneManager.SaveScene(_testScene, TestSceneAsset), Is.True);
        }

        [TearDown]
        public void TearDown()
        {
            _preview?.Dispose();
            if (_runtimeAvatar != null)
                UnityEngine.Object.DestroyImmediate(_runtimeAvatar);
            if (_instance != null)
                UnityEngine.Object.DestroyImmediate(_instance);
        }

        [Test]
        public void SamplesNamedMusclesNormalizedBodyAndAnchoredTrajectory()
        {
            var pose = ReadPose(_animator);
            var muscleIndex = Array.FindIndex(
                HumanTrait.MuscleName,
                name => name.IndexOf("Left Arm", StringComparison.Ordinal) >= 0);
            Assert.That(muscleIndex, Is.GreaterThanOrEqualTo(0));
            pose.muscles[muscleIndex] = 0.35f;
            pose.bodyPosition += new Vector3(0.02f, 0.03f, -0.01f);
            pose.bodyRotation = Quaternion.Euler(4f, 7f, -3f) * pose.bodyRotation;

            var anchorPosition = _animator.transform.position;
            var anchorRotation = _animator.transform.rotation;
            var trajectoryPosition = new Vector3(1f, 0.5f, -2f);
            var trajectoryRotation = Quaternion.Euler(0f, 35f, 0f);

            _preview.Start(_animator);
            _preview.Sample(pose);
            var withoutTrajectory = ReadPose(_animator);
            _preview.Sample(pose, trajectoryPosition, trajectoryRotation);

            Assert.That(
                Vector3.Distance(_animator.transform.position, anchorPosition),
                Is.LessThan(0.0001f));
            Assert.That(
                Quaternion.Angle(_animator.transform.rotation, anchorRotation),
                Is.LessThan(0.01f));

            var actual = ReadPose(_animator);
            Assert.That(actual.muscles[muscleIndex], Is.EqualTo(0.35f).Within(0.01f));
            var expectedWorldDelta =
                _animator.transform.TransformVector(trajectoryPosition);
            var actualWorldDelta = actual.bodyPosition - withoutTrajectory.bodyPosition;
            Assert.That(
                Vector3.Distance(actualWorldDelta, expectedWorldDelta),
                Is.LessThan(0.001f),
                $"actual delta={actualWorldDelta:R}, expected={expectedWorldDelta:R}, " +
                $"humanScale={_animator.humanScale:R}, rootScale={_animator.transform.lossyScale:R}");
            var worldTrajectoryRotation =
                anchorRotation * trajectoryRotation * Quaternion.Inverse(anchorRotation);
            Assert.That(
                Quaternion.Angle(
                    actual.bodyRotation,
                    worldTrajectoryRotation * withoutTrajectory.bodyRotation),
                Is.LessThan(0.1f));
            Assert.That(_testScene.isDirty, Is.False);
        }

        [Test]
        public void StopRestoresAnimatorAllTransformsAndSceneDirtyState()
        {
            _animator.enabled = false;
            _animator.applyRootMotion = true;
            _animator.cullingMode = AnimatorCullingMode.CullCompletely;
            _animator.updateMode = AnimatorUpdateMode.UnscaledTime;
            _animator.speed = 0.65f;
            _animator.fireEvents = false;
            var transforms = CaptureTransforms(_animator.transform);
            Assert.That(EditorSceneManager.SaveScene(_testScene, TestSceneAsset), Is.True);

            var pose = ReadPose(_animator);
            pose.bodyPosition += new Vector3(0.1f, 0.2f, 0.3f);
            _preview.Start(_animator);
            _preview.Sample(pose, new Vector3(4f, 0f, 0f), Quaternion.Euler(0f, 60f, 0f));

            Assert.That(_animator.enabled, Is.False);
            Assert.That(_animator.applyRootMotion, Is.False);
            Assert.That(_animator.cullingMode, Is.EqualTo(AnimatorCullingMode.AlwaysAnimate));
            Assert.That(_testScene.isDirty, Is.False);

            _preview.Stop();
            _preview.Stop();

            Assert.That(_preview.IsRunning, Is.False);
            Assert.That(_animator.enabled, Is.False);
            Assert.That(_animator.applyRootMotion, Is.True);
            Assert.That(_animator.cullingMode, Is.EqualTo(AnimatorCullingMode.CullCompletely));
            Assert.That(_animator.updateMode, Is.EqualTo(AnimatorUpdateMode.UnscaledTime));
            Assert.That(_animator.speed, Is.EqualTo(0.65f));
            Assert.That(_animator.fireEvents, Is.False);
            AssertTransforms(transforms);
            Assert.That(_testScene.isDirty, Is.False);

            EditorSceneManager.MarkSceneDirty(_testScene);
            _preview.Start(_animator);
            _preview.Sample(ReadPose(_animator));
            _preview.Stop();
            Assert.That(_testScene.isDirty, Is.True);
        }

        [Test]
        public void EveryHumanPoseMuscleRoundTripsThroughItsMappedHandle()
        {
            _runtimeAvatar = BuildCompleteAvatar(_instance);
            Assert.That(_runtimeAvatar, Is.Not.Null);
            Assert.That(_runtimeAvatar.isValid, Is.True);
            Assert.That(_runtimeAvatar.isHuman, Is.True);
            _animator.avatar = _runtimeAvatar;
            var pose = ReadPose(_animator);
            Array.Clear(pose.muscles, 0, pose.muscles.Length);
            _preview.Start(_animator);
            var mismatches = new List<string>();

            for (var muscleIndex = 0; muscleIndex < pose.muscles.Length; muscleIndex++)
            {
                Array.Clear(pose.muscles, 0, pose.muscles.Length);
                pose.muscles[muscleIndex] = 0.2f;
                _preview.Sample(pose);
                var actual = ReadPose(_animator);
                if (Mathf.Abs(actual.muscles[muscleIndex] - 0.2f) > 0.02f)
                {
                    mismatches.Add(
                        $"{muscleIndex} {HumanTrait.MuscleName[muscleIndex]}: " +
                        $"expected 0.2, actual {actual.muscles[muscleIndex]:R}");
                }
            }

            Assert.That(mismatches, Is.Empty, string.Join("\n", mismatches));
        }

        [TestCase(1f, 2f, 1f)]
        [TestCase(-1f, -1f, -1f)]
        public void NonUniformOrNegativeTargetScaleIsRejected(float x, float y, float z)
        {
            _animator.transform.localScale = new Vector3(x, y, z);

            Assert.Throws<ArgumentException>(() => _preview.Start(_animator));
            Assert.That(_preview.IsRunning, Is.False);
        }

        [Test]
        public void SecondWriterIsRejectedWithoutStealingFirstDriver()
        {
            using var second = new MotionBridgeHumanoidPreview();
            _preview.Start(_animator);

            Assert.Throws<InvalidOperationException>(() => second.Start(_animator));
            Assert.That(_preview.IsRunning, Is.True);
            Assert.That(second.IsRunning, Is.False);

            _preview.Sample(ReadPose(_animator));
            _preview.Stop();
        }

        [Test]
        public void ExistingAnimationModeOwnerIsNotStoppedOrReplaced()
        {
            var driver = ScriptableObject.CreateInstance<AnimationModeDriver>();
            try
            {
                AnimationMode.StartAnimationMode(driver);
                Assert.Throws<InvalidOperationException>(() => _preview.Start(_animator));
                Assert.That(AnimationMode.InAnimationMode(driver), Is.True);
                Assert.That(_preview.IsRunning, Is.False);
            }
            finally
            {
                if (AnimationMode.InAnimationMode(driver))
                    AnimationMode.StopAnimationMode(driver);
                UnityEngine.Object.DestroyImmediate(driver);
            }
        }

        [Test]
        public void SampleFailureAndDestroyedTargetUseSameIdempotentCleanup()
        {
            var transforms = CaptureTransforms(_animator.transform);
            _preview.Start(_animator);
            var invalidPose = ReadPose(_animator);
            invalidPose.muscles = Array.Empty<float>();

            Assert.Throws<ArgumentException>(() => _preview.Sample(invalidPose));
            Assert.That(_preview.IsRunning, Is.False);
            AssertTransforms(transforms);

            _preview.Start(_animator);
            var validPose = ReadPose(_animator);
            UnityEngine.Object.DestroyImmediate(_instance);
            Assert.Throws<InvalidOperationException>(() => _preview.Sample(validPose));
            Assert.That(_preview.IsRunning, Is.False);
            Assert.DoesNotThrow(() => _preview.Stop());
            Assert.DoesNotThrow(() => _preview.Dispose());
            Assert.DoesNotThrow(() => _preview.Dispose());
        }

        private static HumanPose ReadPose(Animator animator)
        {
            using var handler = new HumanPoseHandler(animator.avatar, animator.transform);
            var pose = new HumanPose();
            handler.GetHumanPose(ref pose);
            return pose;
        }

        private static Avatar BuildCompleteAvatar(GameObject root)
        {
            var importer = AssetImporter.GetAtPath(ImportedFbx) as ModelImporter;
            Assert.That(importer, Is.Not.Null);
            var description = importer.humanDescription;
            var humanBones = description.human.ToList();
            var createdBones = new Dictionary<HumanBodyBones, Transform>();

            var animator = root.GetComponent<Animator>();
            foreach (HumanBodyBones bone in Enum.GetValues(typeof(HumanBodyBones)))
            {
                if (bone == HumanBodyBones.LastBone)
                    continue;
                var transform = animator.GetBoneTransform(bone);
                if (transform != null)
                    createdBones[bone] = transform;
            }

            AddOptionalBone(
                HumanBodyBones.LeftEye,
                createdBones[HumanBodyBones.Head],
                new Vector3(-0.03f, 0.07f, 0.06f),
                humanBones,
                createdBones);
            AddOptionalBone(
                HumanBodyBones.RightEye,
                createdBones[HumanBodyBones.Head],
                new Vector3(0.03f, 0.07f, 0.06f),
                humanBones,
                createdBones);
            AddOptionalBone(
                HumanBodyBones.Jaw,
                createdBones[HumanBodyBones.Head],
                new Vector3(0f, 0.02f, 0.05f),
                humanBones,
                createdBones);

            var firstFingerBone = (int)HumanBodyBones.LeftThumbProximal;
            var lastFingerBone = (int)HumanBodyBones.RightLittleDistal;
            for (var boneValue = firstFingerBone; boneValue <= lastFingerBone; boneValue++)
            {
                var bone = (HumanBodyBones)boneValue;
                var offset = boneValue - firstFingerBone;
                var fingerIndex = offset / 3;
                var segmentIndex = offset % 3;
                var isLeft = fingerIndex < 5;
                var digitIndex = fingerIndex % 5;
                var parent = segmentIndex == 0
                    ? createdBones[isLeft ? HumanBodyBones.LeftHand : HumanBodyBones.RightHand]
                    : createdBones[(HumanBodyBones)(boneValue - 1)];
                var position = segmentIndex == 0
                    ? new Vector3((digitIndex - 2f) * 0.012f, 0.025f, 0f)
                    : new Vector3(0f, 0.025f, 0f);
                AddOptionalBone(bone, parent, position, humanBones, createdBones);
            }

            description.human = humanBones.ToArray();
            description.skeleton = root.GetComponentsInChildren<Transform>(true)
                .Select(transform => new SkeletonBone
                {
                    name = transform.name,
                    position = transform.localPosition,
                    rotation = transform.localRotation,
                    scale = transform.localScale,
                })
                .ToArray();
            return AvatarBuilder.BuildHumanAvatar(root, description);
        }

        private static void AddOptionalBone(
            HumanBodyBones bone,
            Transform parent,
            Vector3 localPosition,
            ICollection<HumanBone> humanBones,
            IDictionary<HumanBodyBones, Transform> createdBones)
        {
            var gameObject = new GameObject(bone.ToString());
            var transform = gameObject.transform;
            transform.SetParent(parent, false);
            transform.localPosition = localPosition;
            transform.localRotation = Quaternion.identity;
            transform.localScale = Vector3.one;
            createdBones.Add(bone, transform);
            humanBones.Add(new HumanBone
            {
                boneName = gameObject.name,
                humanName = HumanTrait.BoneName[(int)bone],
                limit = new HumanLimit { useDefaultValues = true },
            });
        }

        private static Dictionary<Transform, TransformState> CaptureTransforms(Transform root)
        {
            return root.GetComponentsInChildren<Transform>(true).ToDictionary(
                transform => transform,
                transform => new TransformState(transform));
        }

        private static void AssertTransforms(Dictionary<Transform, TransformState> expected)
        {
            foreach (var pair in expected)
            {
                Assert.That(pair.Key, Is.Not.Null);
                Assert.That(
                    Vector3.Distance(pair.Key.localPosition, pair.Value.LocalPosition),
                    Is.LessThan(0.0001f),
                    pair.Key.name + " localPosition");
                Assert.That(
                    Quaternion.Angle(pair.Key.localRotation, pair.Value.LocalRotation),
                    Is.LessThan(0.01f),
                    pair.Key.name + " localRotation");
                Assert.That(
                    Vector3.Distance(pair.Key.localScale, pair.Value.LocalScale),
                    Is.LessThan(0.0001f),
                    pair.Key.name + " localScale");
            }
        }

        private readonly struct TransformState
        {
            public TransformState(Transform transform)
            {
                LocalPosition = transform.localPosition;
                LocalRotation = transform.localRotation;
                LocalScale = transform.localScale;
            }

            public Vector3 LocalPosition { get; }
            public Quaternion LocalRotation { get; }
            public Vector3 LocalScale { get; }
        }
    }
}
