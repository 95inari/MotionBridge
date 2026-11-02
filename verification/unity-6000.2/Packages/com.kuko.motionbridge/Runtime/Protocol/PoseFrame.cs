using System;
using UnityEngine;

namespace Kuko.MotionBridge.Protocol
{
    [Flags]
    public enum PosePacketFlags : byte
    {
        None = 0,
        WorldRoot = 1 << 0,
        Contacts = 1 << 1,
        Playing = 1 << 2,
        ScrubOrEdit = 1 << 3,
    }

    public sealed class PoseFrame
    {
        public PoseFrame(int jointCapacity = MotionBridgeProtocol.MaxJointCount)
        {
            if (jointCapacity < 1 || jointCapacity > MotionBridgeProtocol.MaxJointCount)
                throw new ArgumentOutOfRangeException(nameof(jointCapacity));

            JointRotations = new Quaternion[jointCapacity];
            ContactWeights = new float[4];
            ContactEpochs = new uint[4];
            ContactPositions = new Vector3[4];
            ContactRotations = new Quaternion[4];
        }

        public uint SessionId { get; internal set; }
        public uint Sequence { get; internal set; }
        public ulong TimestampUsec { get; internal set; }
        public int FrameIndex { get; internal set; }
        public PosePacketFlags Flags { get; internal set; }
        public int JointCount { get; internal set; }
        public ulong PresentMask { get; internal set; }
        public Vector3 RootPosition { get; internal set; }
        public Quaternion RootRotation { get; internal set; }
        public Vector3 HipsLocalPositionDelta { get; internal set; }
        public Quaternion[] JointRotations { get; }
        public float[] ContactWeights { get; }
        public uint[] ContactEpochs { get; }
        public Vector3[] ContactPositions { get; }
        public Quaternion[] ContactRotations { get; }
        public Vector4 GroundPlane { get; internal set; }
    }
}
