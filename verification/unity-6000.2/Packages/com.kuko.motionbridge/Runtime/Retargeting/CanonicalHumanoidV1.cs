using System;
using UnityEngine;

namespace Kuko.MotionBridge.Retargeting
{
    public enum CanonicalHumanoidJoint
    {
        Root = 0,
        Hips = 1,
        Spine = 2,
        Chest = 3,
        UpperChest = 4,
        Neck = 5,
        Head = 6,
        LeftShoulder = 7,
        LeftUpperArm = 8,
        LeftLowerArm = 9,
        LeftHand = 10,
        RightShoulder = 11,
        RightUpperArm = 12,
        RightLowerArm = 13,
        RightHand = 14,
        LeftUpperLeg = 15,
        LeftLowerLeg = 16,
        LeftFoot = 17,
        LeftToes = 18,
        RightUpperLeg = 19,
        RightLowerLeg = 20,
        RightFoot = 21,
        RightToes = 22,
    }

    /// <summary>
    /// Runtime copy of the generated Canonical Humanoid v1 descriptor. All retargeting
    /// code consumes these arrays so paths, hierarchy, and rest transforms have one source.
    /// </summary>
    public static class CanonicalHumanoidV1
    {
        public const string SkeletonId = "mb-canonical-humanoid-v1";
        public const string SkeletonHash =
            "2d09ba30b066ac812b0b04c1eaa02e094faf5e3de7b559f81b4f701510f20930";
        public const int JointCount = 23;
        public const ulong RequiredJointMask = 0x3BF76Ful;

        private static readonly string[] JointNameValues =
        {
            "Root",
            "Hips",
            "Spine",
            "Chest",
            "UpperChest",
            "Neck",
            "Head",
            "LeftShoulder",
            "LeftUpperArm",
            "LeftLowerArm",
            "LeftHand",
            "RightShoulder",
            "RightUpperArm",
            "RightLowerArm",
            "RightHand",
            "LeftUpperLeg",
            "LeftLowerLeg",
            "LeftFoot",
            "LeftToes",
            "RightUpperLeg",
            "RightLowerLeg",
            "RightFoot",
            "RightToes",
        };

        private static readonly string[] JointPathValues =
        {
            "",
            "Hips",
            "Hips/Spine",
            "Hips/Spine/Chest",
            "Hips/Spine/Chest/UpperChest",
            "Hips/Spine/Chest/UpperChest/Neck",
            "Hips/Spine/Chest/UpperChest/Neck/Head",
            "Hips/Spine/Chest/UpperChest/LeftShoulder",
            "Hips/Spine/Chest/UpperChest/LeftShoulder/LeftUpperArm",
            "Hips/Spine/Chest/UpperChest/LeftShoulder/LeftUpperArm/LeftLowerArm",
            "Hips/Spine/Chest/UpperChest/LeftShoulder/LeftUpperArm/LeftLowerArm/LeftHand",
            "Hips/Spine/Chest/UpperChest/RightShoulder",
            "Hips/Spine/Chest/UpperChest/RightShoulder/RightUpperArm",
            "Hips/Spine/Chest/UpperChest/RightShoulder/RightUpperArm/RightLowerArm",
            "Hips/Spine/Chest/UpperChest/RightShoulder/RightUpperArm/RightLowerArm/RightHand",
            "Hips/LeftUpperLeg",
            "Hips/LeftUpperLeg/LeftLowerLeg",
            "Hips/LeftUpperLeg/LeftLowerLeg/LeftFoot",
            "Hips/LeftUpperLeg/LeftLowerLeg/LeftFoot/LeftToes",
            "Hips/RightUpperLeg",
            "Hips/RightUpperLeg/RightLowerLeg",
            "Hips/RightUpperLeg/RightLowerLeg/RightFoot",
            "Hips/RightUpperLeg/RightLowerLeg/RightFoot/RightToes",
        };

        private static readonly int[] ParentIndexValues =
        {
            -1, 0, 1, 2, 3, 4, 5, 4, 7, 8, 9, 4,
            11, 12, 13, 1, 15, 16, 17, 1, 19, 20, 21,
        };

        private static readonly Vector3[] RestLocalPositionValues =
        {
            new Vector3(0f, 0f, 0f),
            new Vector3(0f, 0f, 1f),
            new Vector3(0f, 0.12000000476837158f, 0f),
            new Vector3(0f, 0.22000002861022949f, 0f),
            new Vector3(0f, 0.15999996662139893f, 0f),
            new Vector3(0f, 0.12000000476837158f, 0f),
            new Vector3(0f, 0.13999998569488525f, 0f),
            new Vector3(-0.12999999523162842f, 0.059999942779541016f, 0f),
            new Vector3(0f, 0.10999999940395355f, 0f),
            new Vector3(0f, 0.31000000238418579f, 0f),
            new Vector3(0f, 0.26999998092651367f, 0f),
            new Vector3(0.12999999523162842f, 0.059999942779541016f, 0f),
            new Vector3(0f, 0.10999999940395355f, 0f),
            new Vector3(0f, 0.31000000238418579f, 0f),
            new Vector3(0f, 0.26999998092651367f, 0f),
            new Vector3(-0.094999998807907104f, 0f, 0f),
            new Vector3(0f, 0.43000000715255737f, 0f),
            new Vector3(2.212526384326452e-09f, 0.45044425129890442f, -2.2515687092550252e-09f),
            new Vector3(-1.1915041397969617e-08f, 0.17088010907173157f, -8.3051654442556355e-09f),
            new Vector3(0.094999998807907104f, 0f, 0f),
            new Vector3(0f, 0.43000000715255737f, 0f),
            new Vector3(2.212526384326452e-09f, 0.45044425129890442f, -5.4239901459141038e-10f),
            new Vector3(1.7887280989725696e-08f, 0.17088010907173157f, -6.5959957495920207e-09f),
        };

        private static readonly Quaternion[] RestLocalRotationValues =
        {
            new Quaternion(-0.7071068286895752f, 0f, 0f, 0.7071068286895752f),
            new Quaternion(0.7071068286895752f, 0f, 0f, 0.7071068286895752f),
            new Quaternion(0f, 0f, 0f, 1f),
            new Quaternion(0f, 0f, 0f, 1f),
            new Quaternion(0f, 0f, 0f, 1f),
            new Quaternion(0f, 0f, 0f, 1f),
            new Quaternion(0f, 0f, 0f, 1f),
            new Quaternion(-2.6692541510442425e-08f, 2.6692541510442425e-08f, 0.7071068286895752f, 0.7071068286895752f),
            new Quaternion(0f, 0f, 0f, 1f),
            new Quaternion(0f, 0f, 0f, 1f),
            new Quaternion(0f, 0f, 0f, 1f),
            new Quaternion(-2.6692541510442425e-08f, -2.6692541510442425e-08f, -0.7071068286895752f, 0.7071068286895752f),
            new Quaternion(0f, 0f, 0f, 1f),
            new Quaternion(0f, 0f, 0f, 1f),
            new Quaternion(0f, 0f, 0f, 1f),
            new Quaternion(4.3711388286737929e-08f, 0f, 1f, 0f),
            new Quaternion(0.022205784916877747f, -5.3321202603273405e-15f, -1.9412911278493539e-09f, 0.99975341558456421f),
            new Quaternion(0.55120295286178589f, 3.1934649830133235e-15f, -4.8187700940616196e-08f, 0.83437120914459229f),
            new Quaternion(0.17842477560043335f, 4.3009979577846025e-08f, -7.7991888502992879e-09f, 0.98395359516143799f),
            new Quaternion(4.3711388286737929e-08f, 0f, 1f, 0f),
            new Quaternion(0.022205784916877747f, -5.3321202603273405e-15f, -1.9412911278493539e-09f, 0.99975341558456421f),
            new Quaternion(0.55120295286178589f, 3.1934649830133235e-15f, -4.8187700940616196e-08f, 0.83437120914459229f),
            new Quaternion(0.17842477560043335f, 4.3009979577846025e-08f, -7.7991888502992879e-09f, 0.98395359516143799f),
        };

        private static readonly Quaternion[] RestGlobalRotationValues =
        {
            new Quaternion(-0.7071068286895752f, 0f, 0f, 0.7071068286895752f),
            new Quaternion(0f, 0f, 0f, 1f),
            new Quaternion(0f, 0f, 0f, 1f),
            new Quaternion(0f, 0f, 0f, 1f),
            new Quaternion(0f, 0f, 0f, 1f),
            new Quaternion(0f, 0f, 0f, 1f),
            new Quaternion(0f, 0f, 0f, 1f),
            new Quaternion(-2.6692541510442425e-08f, 2.6692541510442425e-08f, 0.7071068286895752f, 0.7071068286895752f),
            new Quaternion(-2.6692541510442425e-08f, 2.6692541510442425e-08f, 0.7071068286895752f, 0.7071068286895752f),
            new Quaternion(-2.6692541510442425e-08f, 2.6692541510442425e-08f, 0.7071068286895752f, 0.7071068286895752f),
            new Quaternion(-2.6692541510442425e-08f, 2.6692541510442425e-08f, 0.7071068286895752f, 0.7071068286895752f),
            new Quaternion(-2.6692541510442425e-08f, -2.6692541510442425e-08f, -0.7071068286895752f, 0.7071068286895752f),
            new Quaternion(-2.6692541510442425e-08f, -2.6692541510442425e-08f, -0.7071068286895752f, 0.7071068286895752f),
            new Quaternion(-2.6692541510442425e-08f, -2.6692541510442425e-08f, -0.7071068286895752f, 0.7071068286895752f),
            new Quaternion(-2.6692541510442425e-08f, -2.6692541510442425e-08f, -0.7071068286895752f, 0.7071068286895752f),
            new Quaternion(4.3711388286737929e-08f, 0f, 1f, 0f),
            new Quaternion(4.3700616458863806e-08f, 0.022205784916877747f, 0.99975341558456421f, 9.706456749469794e-10f),
            new Quaternion(3.5927513408751111e-08f, 0.56959491968154907f, 0.8219255805015564f, 2.4897786943256506e-08f),
            new Quaternion(-2.5121478909004595e-15f, -0.7071068286895752f, -0.7071068286895752f, 3.7682221539880444e-15f),
            new Quaternion(4.3711388286737929e-08f, 0f, 1f, 0f),
            new Quaternion(4.3700616458863806e-08f, 0.022205784916877747f, 0.99975341558456421f, 9.706456749469794e-10f),
            new Quaternion(3.5927513408751111e-08f, 0.56959491968154907f, 0.8219255805015564f, 2.4897786943256506e-08f),
            new Quaternion(-2.5121478909004595e-15f, -0.7071068286895752f, -0.7071068286895752f, 3.7682221539880444e-15f),
        };

        public static ReadOnlySpan<string> JointNames => JointNameValues;
        public static ReadOnlySpan<string> JointPaths => JointPathValues;
        public static ReadOnlySpan<int> ParentIndices => ParentIndexValues;
        public static ReadOnlySpan<Vector3> RestLocalPositions => RestLocalPositionValues;
        public static ReadOnlySpan<Quaternion> RestLocalRotations => RestLocalRotationValues;
        public static ReadOnlySpan<Quaternion> RestGlobalRotations => RestGlobalRotationValues;

        internal static HumanPoseHandler CreateUnboundHandler(Avatar avatar)
        {
            return new HumanPoseHandler(avatar, JointPathValues);
        }
    }
}
