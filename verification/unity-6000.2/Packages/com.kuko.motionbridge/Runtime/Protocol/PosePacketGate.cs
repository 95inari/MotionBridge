using System;

namespace Kuko.MotionBridge.Protocol
{
    /// <summary>
    /// Receiver publication boundary: decode and accepted-session validation must both
    /// succeed before the caller may publish the scratch frame.
    /// </summary>
    public sealed class PosePacketGate
    {
        private readonly PoseSessionValidator _sessionValidator;

        public PosePacketGate(uint sessionId, int jointCount, ulong requiredJointMask)
        {
            _sessionValidator = new PoseSessionValidator(sessionId, jointCount, requiredJointMask);
        }

        public bool TryDecodeAndAccept(
            ReadOnlySpan<byte> packet,
            PoseFrame scratchFrame,
            out PoseDecodeError decodeError,
            out PoseSessionError sessionError)
        {
            sessionError = PoseSessionError.None;
            if (!PosePacketDecoder.TryDecode(packet, scratchFrame, out decodeError))
                return false;
            if (!_sessionValidator.TryAccept(scratchFrame, out sessionError))
                return false;

            return true;
        }

        public void ResetSequence()
        {
            _sessionValidator.Reset();
        }
    }
}
