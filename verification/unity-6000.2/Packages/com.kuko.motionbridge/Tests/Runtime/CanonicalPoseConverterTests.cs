using System;
using System.IO;
using System.Linq;
using Kuko.MotionBridge.Protocol;
using Kuko.MotionBridge.Retargeting;
using NUnit.Framework;
using Unity.Collections;
using UnityEditor;
using UnityEngine;

namespace Kuko.MotionBridge.Tests
{
    public sealed class CanonicalPoseConverterTests
    {
        private const string ImportFolder = "Assets/__MotionBridgeCanonicalPoseConverterTest";
        private const string ImportedFbx = ImportFolder + "/MB_Canonical_Humanoid_v1.fbx";

        private Avatar _avatar;
        private GameObject _prefab;

        [OneTimeSetUp]
        public void OneTimeSetUp()
        {
            var package = UnityEditor.PackageManager.PackageInfo.FindForAssembly(
                typeof(CanonicalPoseConverterTests).Assembly);
            Assert.That(package, Is.Not.Null);
            var sourceFbx = Path.Combine(
                package.resolvedPath,
                "Samples~",
                "Canonical",
                "MB_Canonical_Humanoid_v1.fbx");
            var importDirectory = Path.Combine(
                Directory.GetParent(Application.dataPath).FullName,
                ImportFolder);
            Directory.CreateDirectory(importDirectory);
            File.Copy(sourceFbx, Path.Combine(importDirectory, Path.GetFileName(ImportedFbx)), true);
            AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);

            _avatar = AssetDatabase.LoadAllAssetsAtPath(ImportedFbx)
                .OfType<Avatar>()
                .SingleOrDefault();
            _prefab = AssetDatabase.LoadAssetAtPath<GameObject>(ImportedFbx);
            Assert.That(_avatar, Is.Not.Null);
            Assert.That(_avatar.isValid, Is.True);
            Assert.That(_avatar.isHuman, Is.True);
            Assert.That(_prefab, Is.Not.Null);
        }

        [OneTimeTearDown]
        public void OneTimeTearDown()
        {
            AssetDatabase.DeleteAsset(ImportFolder);
        }

        [Test]
        public void IdentityDeltasMatchImportedCanonicalRestPoseWithoutAllocation()
        {
            var instance = UnityEngine.Object.Instantiate(_prefab);
            try
            {
                AssertDescriptorConstantsMatch(instance.transform);
                var expected = ReadHumanPoseFromTransforms(instance.transform);
                using var converter = new CanonicalPoseConverter(_avatar);
                var frame = CreateFrame(IdentityDeltas());

                Assert.That(converter.TryConvert(frame, out var error), Is.True);
                Assert.That(error, Is.EqualTo(CanonicalPoseConversionError.None));
                AssertHumanPoseEqual(converter.HumanPose, expected);

                var allocatedBefore = GC.GetAllocatedBytesForCurrentThread();
                var converted = converter.TryConvert(frame, out error);
                var allocatedAfter = GC.GetAllocatedBytesForCurrentThread();
                Assert.That(converted, Is.True);
                Assert.That(allocatedAfter, Is.EqualTo(allocatedBefore));
            }
            finally
            {
                UnityEngine.Object.DestroyImmediate(instance);
            }
        }

        [Test]
        public void HipsGlobalYawDeltaMapsThroughCanonicalHierarchy()
        {
            var deltas = IdentityDeltas();
            deltas[(int)CanonicalHumanoidJoint.Hips] =
                Quaternion.AngleAxis(90f, Vector3.up);
            using var restConverter = new CanonicalPoseConverter(_avatar);
            using var yawConverter = new CanonicalPoseConverter(_avatar);
            Assert.That(
                restConverter.TryConvert(CreateFrame(IdentityDeltas()), out _),
                Is.True);
            Assert.That(yawConverter.TryConvert(CreateFrame(deltas), out _), Is.True);

            var expected = ReadHumanPoseFromGlobalDeltas(deltas);
            AssertHumanPoseEqual(yawConverter.HumanPose, expected);
            Assert.That(
                Quaternion.Angle(
                    restConverter.HumanPose.bodyRotation,
                    yawConverter.HumanPose.bodyRotation),
                Is.GreaterThan(20f));
        }

        [Test]
        public void LeftShoulderDeltaChangesOnlyLeftArmMuscles()
        {
            var deltas = IdentityDeltas();
            deltas[(int)CanonicalHumanoidJoint.LeftShoulder] =
                Quaternion.AngleAxis(90f, Vector3.forward);
            using var restConverter = new CanonicalPoseConverter(_avatar);
            using var shoulderConverter = new CanonicalPoseConverter(_avatar);
            Assert.That(
                restConverter.TryConvert(CreateFrame(IdentityDeltas()), out _),
                Is.True);
            Assert.That(shoulderConverter.TryConvert(CreateFrame(deltas), out _), Is.True);

            var rest = restConverter.HumanPose;
            var shoulder = shoulderConverter.HumanPose;
            var maxLeftDelta = 0f;
            var maxRightDelta = 0f;
            for (var index = 0; index < HumanTrait.MuscleCount; index++)
            {
                var delta = Mathf.Abs(shoulder.muscles[index] - rest.muscles[index]);
                var name = HumanTrait.MuscleName[index];
                if (name.StartsWith("Left Shoulder", StringComparison.Ordinal) ||
                    name.StartsWith("Left Arm", StringComparison.Ordinal))
                    maxLeftDelta = Mathf.Max(maxLeftDelta, delta);
                if (name.StartsWith("Right Shoulder", StringComparison.Ordinal) ||
                    name.StartsWith("Right Arm", StringComparison.Ordinal))
                    maxRightDelta = Mathf.Max(maxRightDelta, delta);
            }

            Assert.That(maxLeftDelta, Is.GreaterThan(0.05f));
            Assert.That(maxRightDelta, Is.LessThan(0.001f));
        }

        [Test]
        public void HipsLocalPositionDeltaMapsToHumanBodyPosition()
        {
            var hipsDelta = new Vector3(0.1f, 0.2f, -0.3f);
            using var converter = new CanonicalPoseConverter(_avatar);
            Assert.That(
                converter.TryConvert(
                    CreateFrame(IdentityDeltas(), hipsDelta: hipsDelta),
                    out _),
                Is.True);
            var expected = ReadHumanPoseFromDescriptorRest(hipsDelta);

            Assert.That(
                Vector3.Distance(converter.HumanPose.bodyPosition, expected.bodyPosition),
                Is.LessThanOrEqualTo(0.0001f));
        }

        [Test]
        public void TrajectoryRootIsKeptSeparateFromHumanPose()
        {
            var rootPosition = new Vector3(1f, 2f, 3f);
            var rootRotation = Quaternion.AngleAxis(35f, Vector3.up);
            using var restConverter = new CanonicalPoseConverter(_avatar);
            using var rootConverter = new CanonicalPoseConverter(_avatar);
            Assert.That(
                restConverter.TryConvert(CreateFrame(IdentityDeltas()), out _),
                Is.True);
            Assert.That(
                rootConverter.TryConvert(
                    CreateFrame(
                        IdentityDeltas(),
                        rootPosition: rootPosition,
                        rootRotation: rootRotation,
                        flags: PosePacketFlags.WorldRoot),
                    out _),
                Is.True);

            Assert.That(rootConverter.TrajectoryRootPosition, Is.EqualTo(rootPosition));
            Assert.That(
                Quaternion.Angle(rootConverter.TrajectoryRootRotation, rootRotation),
                Is.LessThan(0.001f));
            Assert.That(rootConverter.Flags, Is.EqualTo(PosePacketFlags.WorldRoot));
            AssertHumanPoseEqual(rootConverter.HumanPose, restConverter.HumanPose);
        }

        [Test]
        public void InPlacePacketCannotApplyTrajectoryAndSyntheticRootMustBeIdentity()
        {
            var nonZeroPosition = new Vector3(5f, 6f, 7f);
            var nonZeroRotation = Quaternion.AngleAxis(40f, Vector3.up);
            using var converter = new CanonicalPoseConverter(_avatar);
            Assert.That(
                converter.TryConvert(
                    CreateFrame(
                        IdentityDeltas(),
                        rootPosition: nonZeroPosition,
                        rootRotation: nonZeroRotation,
                        flags: PosePacketFlags.None),
                    out _),
                Is.True);
            Assert.That(converter.TrajectoryRootPosition, Is.EqualTo(Vector3.zero));
            Assert.That(
                Quaternion.Angle(converter.TrajectoryRootRotation, Quaternion.identity),
                Is.LessThan(0.001f));

            var invalidDeltas = IdentityDeltas();
            invalidDeltas[(int)CanonicalHumanoidJoint.Root] =
                Quaternion.AngleAxis(1f, Vector3.up);
            Assert.That(
                converter.TryConvert(CreateFrame(invalidDeltas), out var error),
                Is.False);
            Assert.That(error, Is.EqualTo(CanonicalPoseConversionError.InvalidSyntheticRoot));
        }

        [Test]
        public void DisposeIsIdempotentAndPreventsFurtherUse()
        {
            var converter = new CanonicalPoseConverter(_avatar);
            Assert.That(converter.TryConvert(CreateFrame(IdentityDeltas()), out _), Is.True);
            converter.Dispose();
            converter.Dispose();

            Assert.That(converter.HasPose, Is.False);
            Assert.Throws<ObjectDisposedException>(() =>
                converter.TryConvert(CreateFrame(IdentityDeltas()), out _));
            Assert.Throws<ObjectDisposedException>(() =>
            {
                var unused = converter.HumanPose;
            });
        }

        [Test]
        public void InvalidOrNonHumanAvatarIsRejected()
        {
            Assert.Throws<ArgumentNullException>(() => new CanonicalPoseConverter(null));

            var root = new GameObject("GenericAvatarRoot");
            var motion = new GameObject("Motion");
            motion.transform.SetParent(root.transform, false);
            var genericAvatar = AvatarBuilder.BuildGenericAvatar(root, "Motion");
            try
            {
                Assert.That(genericAvatar.isHuman, Is.False);
                Assert.Throws<ArgumentException>(() =>
                    new CanonicalPoseConverter(genericAvatar));
            }
            finally
            {
                UnityEngine.Object.DestroyImmediate(genericAvatar);
                UnityEngine.Object.DestroyImmediate(root);
            }
        }

        private void AssertDescriptorConstantsMatch(Transform root)
        {
            Assert.That(CanonicalHumanoidV1.JointPaths.Length, Is.EqualTo(23));
            for (var index = 0; index < CanonicalHumanoidV1.JointCount; index++)
            {
                var path = CanonicalHumanoidV1.JointPaths[index];
                var transform = string.IsNullOrEmpty(path) ? root : root.Find(path);
                Assert.That(transform, Is.Not.Null, path);
                Assert.That(
                    Vector3.Distance(
                        transform.localPosition,
                        CanonicalHumanoidV1.RestLocalPositions[index]),
                    Is.LessThanOrEqualTo(0.0001f),
                    path);
                Assert.That(
                    Quaternion.Angle(
                        transform.localRotation,
                        CanonicalHumanoidV1.RestLocalRotations[index]),
                    Is.LessThanOrEqualTo(0.1f),
                    path);
                Assert.That(
                    Quaternion.Angle(
                        transform.rotation,
                        CanonicalHumanoidV1.RestGlobalRotations[index]),
                    Is.LessThanOrEqualTo(0.1f),
                    path);
            }
        }

        private HumanPose ReadHumanPoseFromTransforms(Transform root)
        {
            using var pose = new NativeArray<float>(
                CanonicalHumanoidV1.JointCount * 7,
                Allocator.Temp);
            for (var index = 0; index < CanonicalHumanoidV1.JointCount; index++)
            {
                var path = CanonicalHumanoidV1.JointPaths[index];
                var transform = string.IsNullOrEmpty(path) ? root : root.Find(path);
                WriteInternalPose(pose, index, transform.localPosition, transform.localRotation);
            }
            return ReadHumanPose(pose);
        }

        private HumanPose ReadHumanPoseFromDescriptorRest(Vector3 hipsDelta)
        {
            using var pose = new NativeArray<float>(
                CanonicalHumanoidV1.JointCount * 7,
                Allocator.Temp);
            for (var index = 0; index < CanonicalHumanoidV1.JointCount; index++)
            {
                var position = CanonicalHumanoidV1.RestLocalPositions[index];
                if (index == (int)CanonicalHumanoidJoint.Hips)
                    position += hipsDelta;
                WriteInternalPose(
                    pose,
                    index,
                    position,
                    CanonicalHumanoidV1.RestLocalRotations[index]);
            }
            return ReadHumanPose(pose);
        }

        private HumanPose ReadHumanPoseFromGlobalDeltas(Quaternion[] deltas)
        {
            var globalRotations = new Quaternion[CanonicalHumanoidV1.JointCount];
            globalRotations[(int)CanonicalHumanoidJoint.Root] =
                CanonicalHumanoidV1.RestGlobalRotations[(int)CanonicalHumanoidJoint.Root];
            for (var index = 1; index < globalRotations.Length; index++)
            {
                globalRotations[index] = Quaternion.Normalize(
                    deltas[index] * CanonicalHumanoidV1.RestGlobalRotations[index]);
            }

            using var pose = new NativeArray<float>(
                CanonicalHumanoidV1.JointCount * 7,
                Allocator.Temp);
            for (var index = 0; index < CanonicalHumanoidV1.JointCount; index++)
            {
                var rotation = index == (int)CanonicalHumanoidJoint.Root
                    ? CanonicalHumanoidV1.RestLocalRotations[index]
                    : Quaternion.Normalize(
                        Quaternion.Inverse(
                            globalRotations[CanonicalHumanoidV1.ParentIndices[index]]) *
                        globalRotations[index]);
                WriteInternalPose(
                    pose,
                    index,
                    CanonicalHumanoidV1.RestLocalPositions[index],
                    rotation);
            }
            return ReadHumanPose(pose);
        }

        private HumanPose ReadHumanPose(NativeArray<float> internalPose)
        {
            var paths = new string[CanonicalHumanoidV1.JointCount];
            for (var index = 0; index < paths.Length; index++)
                paths[index] = CanonicalHumanoidV1.JointPaths[index];
            using var handler = new HumanPoseHandler(_avatar, paths);
            var humanPose = new HumanPose();
            handler.SetInternalAvatarPose(internalPose);
            handler.GetInternalHumanPose(ref humanPose);
            return humanPose;
        }

        private static PoseFrame CreateFrame(
            Quaternion[] jointDeltas,
            Vector3 hipsDelta = default,
            Vector3 rootPosition = default,
            Quaternion? rootRotation = null,
            PosePacketFlags flags = PosePacketFlags.None)
        {
            var source = new PosePacketSource(
                1u,
                1u,
                1ul,
                1,
                flags,
                rootPosition,
                rootRotation ?? Quaternion.identity,
                hipsDelta,
                CanonicalHumanoidV1.JointCount,
                (1ul << CanonicalHumanoidV1.JointCount) - 1ul,
                jointDeltas,
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
            var bytes = new byte[MotionBridgeProtocol.PosePacketBytes(CanonicalHumanoidV1.JointCount)];
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

        private static Quaternion[] IdentityDeltas()
        {
            var deltas = new Quaternion[CanonicalHumanoidV1.JointCount];
            for (var index = 0; index < deltas.Length; index++)
                deltas[index] = Quaternion.identity;
            return deltas;
        }

        private static void AssertHumanPoseEqual(HumanPose actual, HumanPose expected)
        {
            Assert.That(
                Vector3.Distance(actual.bodyPosition, expected.bodyPosition),
                Is.LessThanOrEqualTo(0.0001f));
            Assert.That(
                Quaternion.Angle(actual.bodyRotation, expected.bodyRotation),
                Is.LessThanOrEqualTo(0.1f));
            Assert.That(actual.muscles, Has.Length.EqualTo(expected.muscles.Length));
            for (var index = 0; index < expected.muscles.Length; index++)
            {
                Assert.That(
                    actual.muscles[index],
                    Is.EqualTo(expected.muscles[index]).Within(0.0001f),
                    HumanTrait.MuscleName[index]);
            }
        }

        private static void WriteInternalPose(
            NativeArray<float> pose,
            int index,
            Vector3 position,
            Quaternion rotation)
        {
            var offset = index * 7;
            pose[offset] = position.x;
            pose[offset + 1] = position.y;
            pose[offset + 2] = position.z;
            pose[offset + 3] = rotation.x;
            pose[offset + 4] = rotation.y;
            pose[offset + 5] = rotation.z;
            pose[offset + 6] = rotation.w;
        }
    }
}
