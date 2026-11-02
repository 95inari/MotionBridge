using System;

namespace Kuko.MotionBridge.Protocol
{
    public enum PoseSessionError
    {
        None,
        SessionMismatch,
        JointCountMismatch,
        MissingRequiredJoint,
        StaleSequence,
    }

    /// <summary>
    /// Validates decoded frames against the accepted TCP session before publication.
    /// Sequence comparison follows RFC 1982-style uint half-range ordering.
    /// </summary>
    public sealed class PoseSessionValidator
    {
        private readonly uint _expectedSessionId;
        private readonly int _expectedJointCount;
        private readonly ulong _requiredJointMask;
        private bool _hasSequence;
        private uint _lastSequence;

        public PoseSessionValidator(
            uint expectedSessionId,
            int expectedJointCount,
            ulong requiredJointMask)
        {
            if (expectedJointCount < 1 || expectedJointCount > MotionBridgeProtocol.MaxPoseJointCount)
                throw new ArgumentOutOfRangeException(nameof(expectedJointCount));

            var validMask = expectedJointCount == 64
                ? ulong.MaxValue
                : (1ul << expectedJointCount) - 1ul;
            if ((requiredJointMask & ~validMask) != 0)
                throw new ArgumentOutOfRangeException(nameof(requiredJointMask));

            _expectedSessionId = expectedSessionId;
            _expectedJointCount = expectedJointCount;
            _requiredJointMask = requiredJointMask;
        }

        public bool TryAccept(PoseFrame frame, out PoseSessionError error)
        {
            if (frame == null)
                throw new ArgumentNullException(nameof(frame));
            if (frame.SessionId != _expectedSessionId)
                return Fail(PoseSessionError.SessionMismatch, out error);
            if (frame.JointCount != _expectedJointCount)
                return Fail(PoseSessionError.JointCountMismatch, out error);
            if ((frame.PresentMask & _requiredJointMask) != _requiredJointMask)
                return Fail(PoseSessionError.MissingRequiredJoint, out error);
            if (_hasSequence && !IsNewer(frame.Sequence, _lastSequence))
                return Fail(PoseSessionError.StaleSequence, out error);

            _lastSequence = frame.Sequence;
            _hasSequence = true;
            error = PoseSessionError.None;
            return true;
        }

        public void Reset()
        {
            _hasSequence = false;
            _lastSequence = 0;
        }

        internal static bool IsNewer(uint candidate, uint previous)
        {
            var delta = unchecked(candidate - previous);
            return delta != 0 && delta < 0x80000000u;
        }

        private static bool Fail(PoseSessionError failure, out PoseSessionError error)
        {
            error = failure;
            return false;
        }
    }
}
