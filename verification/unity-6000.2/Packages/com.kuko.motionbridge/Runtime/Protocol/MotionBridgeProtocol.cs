namespace Kuko.MotionBridge.Protocol
{
    public static class MotionBridgeProtocol
    {
        public const string PackageVersion = "0.1.1";
        public const byte Major = 1;
        public const byte Minor = 0;
        public const byte PoseMessageType = 1;
        public const int HeaderBytes = 32;
        public const int FixedPayloadBytes = 208;
        public const int ContactBlockBytes = 160;
        public const int QuaternionBytes = 16;
        public const int MaxDescriptorJointCount = 64;
        public const int MaxPoseJointCount = 60;
        public const int MaxJointCount = MaxDescriptorJointCount;
        public const int MaxPacketBytes = 1200;
        public const int ControlLengthPrefixBytes = 4;
        public const int MaxControlPayloadBytes = 65536;
        public const double MinimumNormalizedNormSquared = 0.9801d;
        public const double MaximumNormalizedNormSquared = 1.0201d;
        public const int DefaultControlPort = 39871;

        public static int PosePacketBytes(int jointCount)
        {
            return HeaderBytes + FixedPayloadBytes + QuaternionBytes * jointCount;
        }
    }
}
