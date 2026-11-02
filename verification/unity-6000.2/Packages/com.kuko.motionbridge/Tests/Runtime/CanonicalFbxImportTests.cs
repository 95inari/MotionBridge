using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Kuko.MotionBridge.Retargeting;
using NUnit.Framework;
using Unity.Collections;
using UnityEditor;
using UnityEngine;

namespace Kuko.MotionBridge.Tests
{
    public sealed class CanonicalFbxImportTests
    {
        private const string ImportFolder = "Assets/__MotionBridgeCanonicalImportTest";
        private const string ImportedFbx = ImportFolder + "/MB_Canonical_Humanoid_v1.fbx";

        [TearDown]
        public void TearDown()
        {
            AssetDatabase.DeleteAsset(ImportFolder);
        }

        [Test]
        public void GeneratedFbxImportsAsHumanoidAndMatchesDescriptorRestPose()
        {
            var package = UnityEditor.PackageManager.PackageInfo.FindForAssembly(
                typeof(CanonicalFbxImportTests).Assembly);
            Assert.That(package, Is.Not.Null);

            var sourceFbx = Path.Combine(
                package.resolvedPath,
                "Samples~",
                "Canonical",
                "MB_Canonical_Humanoid_v1.fbx");
            var descriptorPath = RepositoryFixturePath.Find(
                "generated",
                "canonical_humanoid_v1.json");
            Assert.That(File.Exists(sourceFbx), Is.True, sourceFbx);
            Assert.That(File.Exists(descriptorPath), Is.True, descriptorPath);

            var importDirectory = Path.Combine(
                Directory.GetParent(Application.dataPath).FullName,
                ImportFolder);
            Directory.CreateDirectory(importDirectory);
            File.Copy(sourceFbx, Path.Combine(importDirectory, Path.GetFileName(ImportedFbx)), true);
            AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);

            var importer = AssetImporter.GetAtPath(ImportedFbx) as ModelImporter;
            Assert.That(importer, Is.Not.Null);
            Assert.That(importer.animationType, Is.EqualTo(ModelImporterAnimationType.Human));
            Assert.That(
                importer.avatarSetup,
                Is.EqualTo(ModelImporterAvatarSetup.CreateFromThisModel));
            Assert.That(importer.bakeAxisConversion, Is.False);
            Assert.That(importer.importAnimation, Is.False);

            var avatar = AssetDatabase.LoadAllAssetsAtPath(ImportedFbx)
                .OfType<Avatar>()
                .SingleOrDefault();
            Assert.That(avatar, Is.Not.Null);
            Assert.That(avatar.isValid, Is.True);
            Assert.That(avatar.isHuman, Is.True);

            var descriptor = JsonUtility.FromJson<SkeletonDescriptor>(
                File.ReadAllText(descriptorPath));
            Assert.That(descriptor, Is.Not.Null);
            Assert.That(descriptor.joints, Is.Not.Null.And.Length.GreaterThan(1));
            Assert.That(descriptor.skeletonId, Is.EqualTo(CanonicalHumanoidV1.SkeletonId));
            Assert.That(descriptor.skeletonHash, Is.EqualTo(CanonicalHumanoidV1.SkeletonHash));
            Assert.That(descriptor.joints, Has.Length.EqualTo(CanonicalHumanoidV1.JointCount));

            var prefab = AssetDatabase.LoadAssetAtPath<GameObject>(ImportedFbx);
            Assert.That(prefab, Is.Not.Null);
            var instance = UnityEngine.Object.Instantiate(prefab);
            try
            {
                var mismatches = new List<string>();
                foreach (var joint in descriptor.joints)
                {
                    var transform = string.IsNullOrEmpty(joint.jointPath)
                        ? instance.transform
                        : instance.transform.Find(joint.jointPath);
                    Assert.That(transform, Is.Not.Null, joint.jointPath);
                    var expectedPosition = ToVector3(joint.restLocalPosition);
                    var expectedRotation = ToQuaternion(joint.restLocalRotation);
                    var expectedGlobalRotation = ToQuaternion(joint.restGlobalRotation);
                    if (Vector3.Distance(transform.localPosition, expectedPosition) > 0.0001f)
                    {
                        mismatches.Add(
                            $"{joint.jointPath} position expected={expectedPosition:R} " +
                            $"actual={transform.localPosition:R}");
                    }
                    if (Quaternion.Angle(transform.localRotation, expectedRotation) > 0.1f)
                    {
                        mismatches.Add(
                            $"{joint.jointPath} rotation expected={expectedRotation:R} " +
                            $"actual={transform.localRotation:R}");
                    }
                    if (Quaternion.Angle(transform.rotation, expectedGlobalRotation) > 0.1f)
                    {
                        mismatches.Add(
                            $"{joint.jointPath} global rotation expected={expectedGlobalRotation:R} " +
                            $"actual={transform.rotation:R}");
                    }
                }
                Assert.That(mismatches, Is.Empty, string.Join("\n", mismatches));
                AssertDescriptorProducesImportedHumanPose(avatar, descriptor, instance.transform);
            }
            finally
            {
                UnityEngine.Object.DestroyImmediate(instance);
            }
        }

        private static Vector3 ToVector3(float[] value)
        {
            Assert.That(value, Has.Length.EqualTo(3));
            return new Vector3(value[0], value[1], value[2]);
        }

        private static Quaternion ToQuaternion(float[] value)
        {
            Assert.That(value, Has.Length.EqualTo(4));
            return new Quaternion(value[0], value[1], value[2], value[3]);
        }

        private static void AssertDescriptorProducesImportedHumanPose(
            Avatar avatar,
            SkeletonDescriptor descriptor,
            Transform root)
        {
            var paths = descriptor.joints.Select(joint => joint.jointPath).ToArray();
            using var importedPose = new NativeArray<float>(
                descriptor.joints.Length * 7,
                Allocator.Temp);
            using var descriptorPose = new NativeArray<float>(
                descriptor.joints.Length * 7,
                Allocator.Temp);

            for (var index = 0; index < descriptor.joints.Length; index++)
            {
                var joint = descriptor.joints[index];
                var transform = string.IsNullOrEmpty(joint.jointPath)
                    ? root
                    : root.Find(joint.jointPath);
                Assert.That(transform, Is.Not.Null, joint.jointPath);
                WriteInternalPose(
                    importedPose,
                    index,
                    transform.localPosition,
                    transform.localRotation);
                WriteInternalPose(
                    descriptorPose,
                    index,
                    ToVector3(joint.restLocalPosition),
                    ToQuaternion(joint.restLocalRotation));
            }

            using var handler = new HumanPoseHandler(avatar, paths);
            var expected = new HumanPose();
            handler.SetInternalAvatarPose(importedPose);
            handler.GetInternalHumanPose(ref expected);
            var actual = new HumanPose();
            handler.SetInternalAvatarPose(descriptorPose);
            handler.GetInternalHumanPose(ref actual);

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
                    $"muscle {index}");
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

        [Serializable]
        private sealed class SkeletonDescriptor
        {
            public string skeletonId;
            public string skeletonHash;
            public SkeletonJoint[] joints;
        }

        [Serializable]
        private sealed class SkeletonJoint
        {
            public string jointPath;
            public float[] restLocalPosition;
            public float[] restLocalRotation;
            public float[] restGlobalRotation;
        }
    }
}
