using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Kuko.MotionBridge.Editor.Preview;
using Kuko.MotionBridge.Protocol;
using Kuko.MotionBridge.Retargeting;
using NUnit.Framework;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Kuko.MotionBridge.Tests
{
    public sealed class HumanoidBodyVariantRetargetingTests
    {
        private const string ImportFolder = "Assets/__MotionBridgeBodyVariantTest";
        private const string ImportedFbx = ImportFolder + "/MB_Canonical_Humanoid_v1.fbx";
        private const string TestSceneAsset = ImportFolder + "/BodyVariants.unity";

        // Synthetic target manifest. Ratios are relative to Canonical Humanoid v1 segment
        // lengths, not GameObject scale, and intentionally span very different silhouettes.
        private static readonly BodyVariantManifest[] VariantManifests =
        {
            new BodyVariantManifest(
                "LongLegs",
                torsoRatio: 0.78f,
                armRatio: 1.18f,
                legRatio: 1.48f,
                shoulderWidthRatio: 0.88f,
                hipWidthRatio: 0.90f,
                anchorPosition: new Vector3(-3.5f, 0.25f, 1.0f),
                anchorYawDegrees: -25f),
            new BodyVariantManifest(
                "LongTorsoShortLimbs",
                torsoRatio: 1.52f,
                armRatio: 0.72f,
                legRatio: 0.76f,
                shoulderWidthRatio: 1.02f,
                hipWidthRatio: 1.05f,
                anchorPosition: new Vector3(0.5f, -0.1f, -1.25f),
                anchorYawDegrees: 18f),
            new BodyVariantManifest(
                "BroadLongArms",
                torsoRatio: 0.96f,
                armRatio: 1.58f,
                legRatio: 0.94f,
                shoulderWidthRatio: 1.42f,
                hipWidthRatio: 1.28f,
                anchorPosition: new Vector3(4.25f, 0.4f, 2.0f),
                anchorYawDegrees: 47f),
        };

        private Avatar _sourceAvatar;
        private GameObject _sourcePrefab;
        private HumanDescription _sourceHumanDescription;

        [OneTimeSetUp]
        public void OneTimeSetUp()
        {
            var package = UnityEditor.PackageManager.PackageInfo.FindForAssembly(
                typeof(HumanoidBodyVariantRetargetingTests).Assembly);
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

            _sourceAvatar = AssetDatabase.LoadAllAssetsAtPath(ImportedFbx)
                .OfType<Avatar>()
                .SingleOrDefault();
            _sourcePrefab = AssetDatabase.LoadAssetAtPath<GameObject>(ImportedFbx);
            var importer = AssetImporter.GetAtPath(ImportedFbx) as ModelImporter;
            Assert.That(_sourceAvatar, Is.Not.Null);
            Assert.That(_sourceAvatar.isValid && _sourceAvatar.isHuman, Is.True);
            Assert.That(_sourcePrefab, Is.Not.Null);
            Assert.That(importer, Is.Not.Null);
            _sourceHumanDescription = importer.humanDescription;
        }

        [OneTimeTearDown]
        public void OneTimeTearDown()
        {
            EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            AssetDatabase.DeleteAsset(ImportFolder);
        }

        [Test]
        public void SameCanonicalPoseRetargetsSafelyAcrossThreeBodyVariants()
        {
            var scene = EditorSceneManager.NewScene(
                NewSceneSetup.EmptyScene,
                NewSceneMode.Single);
            var targets = new List<VariantTarget>(VariantManifests.Length);
            try
            {
                foreach (var manifest in VariantManifests)
                    targets.Add(CreateVariantTarget(manifest, scene));

                Assert.That(EditorSceneManager.SaveScene(scene, TestSceneAsset), Is.True);
                foreach (var target in targets)
                    target.Animator.avatar = target.Avatar;
                Assert.That(scene.isDirty, Is.False);

                var frame = CreateSymmetricWorldRootFrame();
                using var converter = new CanonicalPoseConverter(_sourceAvatar);
                Assert.That(
                    converter.TryConvert(frame, out var conversionError),
                    Is.True,
                    conversionError.ToString());
                AssertSourcePoseIsFiniteAndSymmetric(converter.HumanPose);

                foreach (var target in targets)
                    AssertRetargetsAndRestores(target, converter, scene);

                EditorSceneManager.MarkSceneDirty(scene);
                using (var preview = new MotionBridgeHumanoidPreview())
                {
                    preview.Start(targets[0].Animator);
                    preview.Sample(converter.HumanPose);
                    preview.Stop();
                }
                Assert.That(scene.isDirty, Is.True, "A pre-dirty scene must stay dirty.");
            }
            finally
            {
                foreach (var target in targets)
                    target.Dispose();
            }
        }

        private VariantTarget CreateVariantTarget(BodyVariantManifest manifest, Scene scene)
        {
            var instance = UnityEngine.Object.Instantiate(_sourcePrefab);
            instance.name = manifest.Name;
            SceneManager.MoveGameObjectToScene(instance, scene);
            ApplyBodyVariant(instance.transform, manifest);
            AssertManifestRatios(instance.transform, manifest);

            var avatar = BuildAvatarFromCurrentSkeleton(instance);
            Assert.That(avatar, Is.Not.Null, manifest.Name);
            Assert.That(avatar.isValid, Is.True, manifest.Name);
            Assert.That(avatar.isHuman, Is.True, manifest.Name);

            instance.transform.SetPositionAndRotation(
                manifest.AnchorPosition,
                Quaternion.Euler(0f, manifest.AnchorYawDegrees, 0f));
            var animator = instance.GetComponent<Animator>();
            Assert.That(animator, Is.Not.Null, manifest.Name);
            animator.applyRootMotion = true;
            animator.cullingMode = AnimatorCullingMode.CullCompletely;
            return new VariantTarget(manifest, instance, animator, avatar);
        }

        private Avatar BuildAvatarFromCurrentSkeleton(GameObject root)
        {
            var description = _sourceHumanDescription;
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

        private static void ApplyBodyVariant(
            Transform root,
            BodyVariantManifest manifest)
        {
            ScaleLocalPosition(root, CanonicalHumanoidJoint.Hips, manifest.LegRatio);
            ScaleLocalPosition(root, CanonicalHumanoidJoint.Spine, manifest.TorsoRatio);
            ScaleLocalPosition(root, CanonicalHumanoidJoint.Chest, manifest.TorsoRatio);
            ScaleLocalPosition(root, CanonicalHumanoidJoint.UpperChest, manifest.TorsoRatio);
            ScaleLocalPosition(root, CanonicalHumanoidJoint.Neck, manifest.TorsoRatio);
            ScaleLocalPosition(root, CanonicalHumanoidJoint.Head, manifest.TorsoRatio);

            ScaleShoulder(root, CanonicalHumanoidJoint.LeftShoulder, manifest);
            ScaleShoulder(root, CanonicalHumanoidJoint.RightShoulder, manifest);
            ScaleArm(root, CanonicalHumanoidJoint.LeftUpperArm, manifest.ArmRatio);
            ScaleArm(root, CanonicalHumanoidJoint.LeftLowerArm, manifest.ArmRatio);
            ScaleArm(root, CanonicalHumanoidJoint.LeftHand, manifest.ArmRatio);
            ScaleArm(root, CanonicalHumanoidJoint.RightUpperArm, manifest.ArmRatio);
            ScaleArm(root, CanonicalHumanoidJoint.RightLowerArm, manifest.ArmRatio);
            ScaleArm(root, CanonicalHumanoidJoint.RightHand, manifest.ArmRatio);

            ScaleHipAttachment(root, CanonicalHumanoidJoint.LeftUpperLeg, manifest.HipWidthRatio);
            ScaleHipAttachment(root, CanonicalHumanoidJoint.RightUpperLeg, manifest.HipWidthRatio);
            ScaleLocalPosition(root, CanonicalHumanoidJoint.LeftLowerLeg, manifest.LegRatio);
            ScaleLocalPosition(root, CanonicalHumanoidJoint.LeftFoot, manifest.LegRatio);
            ScaleLocalPosition(root, CanonicalHumanoidJoint.LeftToes, manifest.LegRatio);
            ScaleLocalPosition(root, CanonicalHumanoidJoint.RightLowerLeg, manifest.LegRatio);
            ScaleLocalPosition(root, CanonicalHumanoidJoint.RightFoot, manifest.LegRatio);
            ScaleLocalPosition(root, CanonicalHumanoidJoint.RightToes, manifest.LegRatio);
        }

        private static void ScaleShoulder(
            Transform root,
            CanonicalHumanoidJoint joint,
            BodyVariantManifest manifest)
        {
            var transform = FindJoint(root, joint);
            var position = transform.localPosition;
            position.x *= manifest.ShoulderWidthRatio;
            position.y *= manifest.TorsoRatio;
            position.z *= manifest.TorsoRatio;
            transform.localPosition = position;
        }

        private static void ScaleHipAttachment(
            Transform root,
            CanonicalHumanoidJoint joint,
            float widthRatio)
        {
            var transform = FindJoint(root, joint);
            var position = transform.localPosition;
            position.x *= widthRatio;
            transform.localPosition = position;
        }

        private static void ScaleArm(
            Transform root,
            CanonicalHumanoidJoint joint,
            float ratio)
        {
            ScaleLocalPosition(root, joint, ratio);
        }

        private static void ScaleLocalPosition(
            Transform root,
            CanonicalHumanoidJoint joint,
            float ratio)
        {
            var transform = FindJoint(root, joint);
            transform.localPosition *= ratio;
        }

        private static Transform FindJoint(Transform root, CanonicalHumanoidJoint joint)
        {
            var path = CanonicalHumanoidV1.JointPaths[(int)joint];
            var transform = string.IsNullOrEmpty(path) ? root : root.Find(path);
            Assert.That(transform, Is.Not.Null, path);
            return transform;
        }

        private static void AssertManifestRatios(
            Transform root,
            BodyVariantManifest manifest)
        {
            var torsoIndices = new[]
            {
                CanonicalHumanoidJoint.Spine,
                CanonicalHumanoidJoint.Chest,
                CanonicalHumanoidJoint.UpperChest,
                CanonicalHumanoidJoint.Neck,
                CanonicalHumanoidJoint.Head,
            };
            var armIndices = new[]
            {
                CanonicalHumanoidJoint.LeftUpperArm,
                CanonicalHumanoidJoint.LeftLowerArm,
                CanonicalHumanoidJoint.LeftHand,
            };
            var legIndices = new[]
            {
                CanonicalHumanoidJoint.LeftLowerLeg,
                CanonicalHumanoidJoint.LeftFoot,
                CanonicalHumanoidJoint.LeftToes,
            };

            AssertRatio(root, torsoIndices, manifest.TorsoRatio, manifest.Name + " torso");
            AssertRatio(root, armIndices, manifest.ArmRatio, manifest.Name + " arm");
            AssertRatio(root, legIndices, manifest.LegRatio, manifest.Name + " leg");
        }

        private static void AssertRatio(
            Transform root,
            IReadOnlyList<CanonicalHumanoidJoint> joints,
            float expectedRatio,
            string message)
        {
            var canonicalLength = 0f;
            var actualLength = 0f;
            foreach (var joint in joints)
            {
                canonicalLength += CanonicalHumanoidV1.RestLocalPositions[(int)joint].magnitude;
                actualLength += FindJoint(root, joint).localPosition.magnitude;
            }
            Assert.That(
                actualLength / canonicalLength,
                Is.EqualTo(expectedRatio).Within(0.0001f),
                message);
        }

        private static void AssertRetargetsAndRestores(
            VariantTarget target,
            CanonicalPoseConverter converter,
            Scene scene)
        {
            var transforms = CaptureTransforms(target.Root.transform);
            var anchorPosition = target.Root.transform.position;
            var anchorRotation = target.Root.transform.rotation;
            var applyRootMotion = target.Animator.applyRootMotion;
            var cullingMode = target.Animator.cullingMode;
            var enabled = target.Animator.enabled;

            using var preview = new MotionBridgeHumanoidPreview();
            preview.Start(target.Animator);
            preview.Sample(converter.HumanPose);
            var withoutTrajectory = ReadPose(target.Animator);
            AssertFinite(target, withoutTrajectory);
            AssertNormalizedMuscles(converter.HumanPose, withoutTrajectory, target.Manifest.Name);
            AssertLeftRightSymmetry(target);

            preview.Sample(
                converter.HumanPose,
                converter.TrajectoryRootPosition,
                converter.TrajectoryRootRotation);
            var withTrajectory = ReadPose(target.Animator);
            AssertFinite(target, withTrajectory);
            AssertTrajectoryAppliedOnce(
                target,
                withoutTrajectory,
                withTrajectory,
                converter.TrajectoryRootPosition,
                converter.TrajectoryRootRotation,
                anchorPosition,
                anchorRotation);
            Assert.That(scene.isDirty, Is.False, target.Manifest.Name + " live dirty");

            preview.Stop();
            preview.Stop();
            AssertTransforms(transforms, target.Manifest.Name);
            Assert.That(target.Root.transform.position, Is.EqualTo(anchorPosition));
            Assert.That(
                Quaternion.Angle(target.Root.transform.rotation, anchorRotation),
                Is.LessThan(0.001f));
            Assert.That(target.Animator.applyRootMotion, Is.EqualTo(applyRootMotion));
            Assert.That(target.Animator.cullingMode, Is.EqualTo(cullingMode));
            Assert.That(target.Animator.enabled, Is.EqualTo(enabled));
            Assert.That(scene.isDirty, Is.False, target.Manifest.Name + " stopped dirty");
        }

        private static void AssertTrajectoryAppliedOnce(
            VariantTarget target,
            HumanPose withoutTrajectory,
            HumanPose withTrajectory,
            Vector3 trajectoryPosition,
            Quaternion trajectoryRotation,
            Vector3 anchorPosition,
            Quaternion anchorRotation)
        {
            Assert.That(
                Vector3.Distance(target.Root.transform.position, anchorPosition),
                Is.LessThan(0.0001f),
                target.Manifest.Name + " anchor position");
            Assert.That(
                Quaternion.Angle(target.Root.transform.rotation, anchorRotation),
                Is.LessThan(0.001f),
                target.Manifest.Name + " anchor rotation");

            var expectedDelta = target.Root.transform.TransformVector(trajectoryPosition);
            var actualDelta = withTrajectory.bodyPosition - withoutTrajectory.bodyPosition;
            Assert.That(
                Vector3.Distance(actualDelta, expectedDelta),
                Is.LessThan(0.001f),
                target.Manifest.Name + " trajectory must be applied exactly once");
            Assert.That(
                Vector3.Distance(actualDelta, expectedDelta * 2f),
                Is.GreaterThan(0.1f),
                target.Manifest.Name + " trajectory was doubled");

            var worldTrajectoryRotation =
                anchorRotation * trajectoryRotation * Quaternion.Inverse(anchorRotation);
            Assert.That(
                Quaternion.Angle(
                    withTrajectory.bodyRotation,
                    worldTrajectoryRotation * withoutTrajectory.bodyRotation),
                Is.LessThan(0.1f),
                target.Manifest.Name + " trajectory rotation");
        }

        private static void AssertNormalizedMuscles(
            HumanPose expected,
            HumanPose actual,
            string variantName)
        {
            Assert.That(actual.muscles, Has.Length.EqualTo(expected.muscles.Length));
            for (var index = 0; index < expected.muscles.Length; index++)
            {
                Assert.That(
                    actual.muscles[index],
                    Is.EqualTo(expected.muscles[index]).Within(0.05f),
                    $"{variantName}: {HumanTrait.MuscleName[index]}");
            }
        }

        private static void AssertSourcePoseIsFiniteAndSymmetric(HumanPose pose)
        {
            Assert.That(IsFinite(pose.bodyPosition), Is.True);
            Assert.That(IsFinite(pose.bodyRotation), Is.True);
            foreach (var muscle in pose.muscles)
                Assert.That(IsFinite(muscle), Is.True);

            var nonZeroMuscleCount = pose.muscles.Count(value => Mathf.Abs(value) >= 0.02f);
            var peakMuscle = pose.muscles.Max(value => Mathf.Abs(value));
            Assert.That(
                nonZeroMuscleCount,
                Is.GreaterThanOrEqualTo(8),
                "The synthetic source must exercise multiple non-rest muscle channels.");
            Assert.That(
                peakMuscle,
                Is.GreaterThanOrEqualTo(0.1f),
                "The synthetic source motion is too close to the rest pose.");

            for (var index = 0; index < HumanTrait.MuscleCount; index++)
            {
                var name = HumanTrait.MuscleName[index];
                if (!name.StartsWith("Left ", StringComparison.Ordinal))
                    continue;
                var counterpart = "Right " + name.Substring("Left ".Length);
                var rightIndex = Array.IndexOf(HumanTrait.MuscleName, counterpart);
                if (rightIndex >= 0)
                {
                    Assert.That(
                        pose.muscles[index],
                        Is.EqualTo(pose.muscles[rightIndex]).Within(0.0001f),
                        name);
                }
            }
        }

        private static void AssertLeftRightSymmetry(VariantTarget target)
        {
            AssertMirroredPair(target, HumanBodyBones.LeftShoulder, HumanBodyBones.RightShoulder);
            AssertMirroredPair(target, HumanBodyBones.LeftUpperArm, HumanBodyBones.RightUpperArm);
            AssertMirroredPair(target, HumanBodyBones.LeftLowerArm, HumanBodyBones.RightLowerArm);
            AssertMirroredPair(target, HumanBodyBones.LeftHand, HumanBodyBones.RightHand);
            AssertMirroredPair(target, HumanBodyBones.LeftUpperLeg, HumanBodyBones.RightUpperLeg);
            AssertMirroredPair(target, HumanBodyBones.LeftLowerLeg, HumanBodyBones.RightLowerLeg);
            AssertMirroredPair(target, HumanBodyBones.LeftFoot, HumanBodyBones.RightFoot);
            AssertMirroredPair(target, HumanBodyBones.LeftToes, HumanBodyBones.RightToes);
        }

        private static void AssertMirroredPair(
            VariantTarget target,
            HumanBodyBones leftBone,
            HumanBodyBones rightBone)
        {
            var left = target.Root.transform.InverseTransformPoint(
                target.Animator.GetBoneTransform(leftBone).position);
            var right = target.Root.transform.InverseTransformPoint(
                target.Animator.GetBoneTransform(rightBone).position);
            const float symmetryTolerance = 0.006f;
            var message =
                $"{target.Manifest.Name}: {leftBone}/{rightBone}, left={left:R}, right={right:R}";
            Assert.That(
                left.x,
                Is.EqualTo(-right.x).Within(symmetryTolerance),
                message);
            Assert.That(
                left.y,
                Is.EqualTo(right.y).Within(symmetryTolerance),
                message);
            Assert.That(
                left.z,
                Is.EqualTo(right.z).Within(symmetryTolerance),
                message);
        }

        private static void AssertFinite(VariantTarget target, HumanPose pose)
        {
            Assert.That(IsFinite(pose.bodyPosition), Is.True, target.Manifest.Name);
            Assert.That(IsFinite(pose.bodyRotation), Is.True, target.Manifest.Name);
            foreach (var muscle in pose.muscles)
                Assert.That(IsFinite(muscle), Is.True, target.Manifest.Name);
            foreach (var transform in target.Root.GetComponentsInChildren<Transform>(true))
            {
                Assert.That(IsFinite(transform.localPosition), Is.True, transform.name);
                Assert.That(IsFinite(transform.localRotation), Is.True, transform.name);
                Assert.That(IsFinite(transform.localScale), Is.True, transform.name);
            }
        }

        private static PoseFrame CreateSymmetricWorldRootFrame()
        {
            var jointRotations = new Quaternion[CanonicalHumanoidV1.JointCount];
            for (var index = 0; index < jointRotations.Length; index++)
                jointRotations[index] = Quaternion.identity;

            // Center-line flexion and mirrored limb deltas keep bilateral anatomy symmetric
            // while driving a genuinely non-rest multi-joint HumanPose.
            jointRotations[(int)CanonicalHumanoidJoint.Spine] =
                Quaternion.AngleAxis(6f, Vector3.right);
            jointRotations[(int)CanonicalHumanoidJoint.Chest] =
                Quaternion.AngleAxis(8f, Vector3.right);
            jointRotations[(int)CanonicalHumanoidJoint.Neck] =
                Quaternion.AngleAxis(-4f, Vector3.right);

            SetMirroredJointDeltas(
                jointRotations,
                CanonicalHumanoidJoint.LeftShoulder,
                CanonicalHumanoidJoint.RightShoulder,
                Quaternion.AngleAxis(10f, Vector3.forward));
            SetMirroredJointDeltas(
                jointRotations,
                CanonicalHumanoidJoint.LeftUpperArm,
                CanonicalHumanoidJoint.RightUpperArm,
                Quaternion.AngleAxis(15f, Vector3.forward));
            SetMirroredJointDeltas(
                jointRotations,
                CanonicalHumanoidJoint.LeftLowerArm,
                CanonicalHumanoidJoint.RightLowerArm,
                Quaternion.AngleAxis(12f, Vector3.right));
            SetMirroredJointDeltas(
                jointRotations,
                CanonicalHumanoidJoint.LeftUpperLeg,
                CanonicalHumanoidJoint.RightUpperLeg,
                Quaternion.AngleAxis(10f, Vector3.right));
            SetMirroredJointDeltas(
                jointRotations,
                CanonicalHumanoidJoint.LeftLowerLeg,
                CanonicalHumanoidJoint.RightLowerLeg,
                Quaternion.AngleAxis(-12f, Vector3.right));
            SetMirroredJointDeltas(
                jointRotations,
                CanonicalHumanoidJoint.LeftFoot,
                CanonicalHumanoidJoint.RightFoot,
                Quaternion.AngleAxis(5f, Vector3.right));
            var source = new PosePacketSource(
                1u,
                10u,
                1000ul,
                24,
                PosePacketFlags.WorldRoot,
                new Vector3(0.8f, 0.15f, -0.6f),
                Quaternion.AngleAxis(27f, Vector3.up),
                new Vector3(0f, 0.04f, 0f),
                CanonicalHumanoidV1.JointCount,
                (1ul << CanonicalHumanoidV1.JointCount) - 1ul,
                jointRotations,
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
            var bytes = new byte[
                MotionBridgeProtocol.PosePacketBytes(CanonicalHumanoidV1.JointCount)];
            Assert.That(
                PosePacketEncoder.TryEncode(in source, bytes, out _, out var encodeError),
                Is.True,
                encodeError.ToString());
            var frame = new PoseFrame(CanonicalHumanoidV1.JointCount);
            Assert.That(
                PosePacketDecoder.TryDecode(bytes, frame, out var decodeError),
                Is.True,
                decodeError.ToString());
            return frame;
        }

        private static void SetMirroredJointDeltas(
            Quaternion[] jointRotations,
            CanonicalHumanoidJoint leftJoint,
            CanonicalHumanoidJoint rightJoint,
            Quaternion leftDelta)
        {
            leftDelta = Quaternion.Normalize(leftDelta);
            jointRotations[(int)leftJoint] = leftDelta;
            jointRotations[(int)rightJoint] = Quaternion.Normalize(
                new Quaternion(
                    leftDelta.x,
                    -leftDelta.y,
                    -leftDelta.z,
                    leftDelta.w));
        }

        private static HumanPose ReadPose(Animator animator)
        {
            using var handler = new HumanPoseHandler(animator.avatar, animator.transform);
            var pose = new HumanPose();
            handler.GetHumanPose(ref pose);
            return pose;
        }

        private static Dictionary<Transform, TransformState> CaptureTransforms(Transform root)
        {
            return root.GetComponentsInChildren<Transform>(true).ToDictionary(
                transform => transform,
                transform => new TransformState(transform));
        }

        private static void AssertTransforms(
            IReadOnlyDictionary<Transform, TransformState> expected,
            string variantName)
        {
            foreach (var pair in expected)
            {
                Assert.That(pair.Key, Is.Not.Null, variantName);
                Assert.That(
                    Vector3.Distance(pair.Key.localPosition, pair.Value.LocalPosition),
                    Is.LessThan(0.0001f),
                    variantName + ": " + pair.Key.name + " localPosition");
                Assert.That(
                    Quaternion.Angle(pair.Key.localRotation, pair.Value.LocalRotation),
                    Is.LessThan(0.01f),
                    variantName + ": " + pair.Key.name + " localRotation");
                Assert.That(
                    Vector3.Distance(pair.Key.localScale, pair.Value.LocalScale),
                    Is.LessThan(0.0001f),
                    variantName + ": " + pair.Key.name + " localScale");
            }
        }

        private static bool IsFinite(Vector3 value)
        {
            return IsFinite(value.x) && IsFinite(value.y) && IsFinite(value.z);
        }

        private static bool IsFinite(Quaternion value)
        {
            return IsFinite(value.x) && IsFinite(value.y) &&
                   IsFinite(value.z) && IsFinite(value.w);
        }

        private static bool IsFinite(float value)
        {
            return !float.IsNaN(value) && !float.IsInfinity(value);
        }

        private readonly struct BodyVariantManifest
        {
            public BodyVariantManifest(
                string name,
                float torsoRatio,
                float armRatio,
                float legRatio,
                float shoulderWidthRatio,
                float hipWidthRatio,
                Vector3 anchorPosition,
                float anchorYawDegrees)
            {
                Name = name;
                TorsoRatio = torsoRatio;
                ArmRatio = armRatio;
                LegRatio = legRatio;
                ShoulderWidthRatio = shoulderWidthRatio;
                HipWidthRatio = hipWidthRatio;
                AnchorPosition = anchorPosition;
                AnchorYawDegrees = anchorYawDegrees;
            }

            public string Name { get; }
            public float TorsoRatio { get; }
            public float ArmRatio { get; }
            public float LegRatio { get; }
            public float ShoulderWidthRatio { get; }
            public float HipWidthRatio { get; }
            public Vector3 AnchorPosition { get; }
            public float AnchorYawDegrees { get; }
        }

        private sealed class VariantTarget : IDisposable
        {
            public VariantTarget(
                BodyVariantManifest manifest,
                GameObject root,
                Animator animator,
                Avatar avatar)
            {
                Manifest = manifest;
                Root = root;
                Animator = animator;
                Avatar = avatar;
            }

            public BodyVariantManifest Manifest { get; }
            public GameObject Root { get; }
            public Animator Animator { get; }
            public Avatar Avatar { get; }

            public void Dispose()
            {
                if (Root != null)
                    UnityEngine.Object.DestroyImmediate(Root);
                if (Avatar != null)
                    UnityEngine.Object.DestroyImmediate(Avatar);
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
