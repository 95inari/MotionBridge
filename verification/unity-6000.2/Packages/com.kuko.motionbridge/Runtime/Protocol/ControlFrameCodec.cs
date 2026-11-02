using System;
using System.Collections.Generic;
using System.Text;

namespace Kuko.MotionBridge.Protocol
{
    public sealed class ControlProtocolException : Exception
    {
        public ControlProtocolException(string message)
            : this("PROTOCOL_ERROR", message)
        {
        }

        public ControlProtocolException(string message, Exception innerException)
            : this("PROTOCOL_ERROR", message, innerException)
        {
        }

        public ControlProtocolException(string code, string message)
            : base(message)
        {
            Code = string.IsNullOrWhiteSpace(code) ? "PROTOCOL_ERROR" : code;
        }

        public ControlProtocolException(
            string code,
            string message,
            Exception innerException)
            : base(message, innerException)
        {
            Code = string.IsNullOrWhiteSpace(code) ? "PROTOCOL_ERROR" : code;
        }

        public string Code { get; }
    }

    /// <summary>
    /// Encodes and incrementally decodes the little-endian uint32 + UTF-8 JSON
    /// framing used by TCP control. One Append may contain a fragment, one frame,
    /// or many coalesced frames.
    /// </summary>
    public sealed class ControlFrameDecoder
    {
        private static readonly UTF8Encoding StrictUtf8 = new UTF8Encoding(false, true);

        private readonly byte[] _buffer = new byte[
            MotionBridgeProtocol.ControlLengthPrefixBytes +
            MotionBridgeProtocol.MaxControlPayloadBytes];
        private readonly Queue<string> _messages = new Queue<string>();
        private int _count;
        private bool _faulted;

        public int BufferedBytes => _count;

        public int PendingMessageCount => _messages.Count;

        public void Append(byte[] source, int offset, int count)
        {
            if (_faulted)
                throw new ControlProtocolException("Decoder is faulted; call Reset before reuse.");
            if (source == null)
                throw new ArgumentNullException(nameof(source));
            if (offset < 0 || count < 0 || offset > source.Length - count)
                throw new ArgumentOutOfRangeException(nameof(offset));

            try
            {
                while (count > 0)
                {
                    var copyCount = Math.Min(count, _buffer.Length - _count);
                    if (copyCount == 0)
                    {
                        if (!ExtractAvailableFrames())
                            throw new ControlProtocolException("Control decoder buffer is full.");
                        continue;
                    }

                    Buffer.BlockCopy(source, offset, _buffer, _count, copyCount);
                    _count += copyCount;
                    offset += copyCount;
                    count -= copyCount;
                    ExtractAvailableFrames();
                }
            }
            catch (ControlProtocolException)
            {
                _faulted = true;
                _count = 0;
                _messages.Clear();
                throw;
            }
        }

        public bool TryDequeue(out string json)
        {
            if (_messages.Count == 0)
            {
                json = null;
                return false;
            }
            json = _messages.Dequeue();
            return true;
        }

        public void Reset()
        {
            _count = 0;
            _faulted = false;
            _messages.Clear();
        }

        private bool ExtractAvailableFrames()
        {
            var extracted = false;
            while (_count >= MotionBridgeProtocol.ControlLengthPrefixBytes)
            {
                var payloadBytes =
                    (uint)_buffer[0] |
                    ((uint)_buffer[1] << 8) |
                    ((uint)_buffer[2] << 16) |
                    ((uint)_buffer[3] << 24);
                if (payloadBytes < 1 ||
                    payloadBytes > MotionBridgeProtocol.MaxControlPayloadBytes)
                {
                    throw new ControlProtocolException(
                        "Control payload length is outside the 1..65536 byte limit.");
                }

                var frameBytes = checked(
                    MotionBridgeProtocol.ControlLengthPrefixBytes + (int)payloadBytes);
                if (_count < frameBytes)
                    break;

                try
                {
                    _messages.Enqueue(StrictUtf8.GetString(
                        _buffer,
                        MotionBridgeProtocol.ControlLengthPrefixBytes,
                        (int)payloadBytes));
                }
                catch (DecoderFallbackException exception)
                {
                    throw new ControlProtocolException(
                        "Control payload is not valid UTF-8.",
                        exception);
                }

                _count -= frameBytes;
                if (_count > 0)
                    Buffer.BlockCopy(_buffer, frameBytes, _buffer, 0, _count);
                extracted = true;
            }
            return extracted;
        }
    }

    public static class ControlFrameEncoder
    {
        private static readonly UTF8Encoding StrictUtf8 = new UTF8Encoding(false, true);

        public static byte[] EncodeJson(string json)
        {
            if (string.IsNullOrEmpty(json))
                throw new ControlProtocolException("Control JSON must not be empty.");
            var trimmed = json.Trim();
            if (trimmed.Length < 2 ||
                trimmed[0] != '{' ||
                trimmed[trimmed.Length - 1] != '}')
                throw new ControlProtocolException("Control JSON root must be an object.");
            byte[] payload;
            try
            {
                payload = StrictUtf8.GetBytes(json);
            }
            catch (EncoderFallbackException exception)
            {
                throw new ControlProtocolException(
                    "Control JSON is not valid Unicode.",
                    exception);
            }
            if (payload.Length < 1 ||
                payload.Length > MotionBridgeProtocol.MaxControlPayloadBytes)
            {
                throw new ControlProtocolException(
                    "Control payload length is outside the 1..65536 byte limit.");
            }

            var frame = new byte[
                MotionBridgeProtocol.ControlLengthPrefixBytes + payload.Length];
            var length = (uint)payload.Length;
            frame[0] = (byte)length;
            frame[1] = (byte)(length >> 8);
            frame[2] = (byte)(length >> 16);
            frame[3] = (byte)(length >> 24);
            Buffer.BlockCopy(
                payload,
                0,
                frame,
                MotionBridgeProtocol.ControlLengthPrefixBytes,
                payload.Length);
            return frame;
        }
    }
}
