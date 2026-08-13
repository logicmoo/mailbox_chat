import base64
import struct

from mailbox_channels.websocket_chat import accept_value, encode_frame, read_frame


class FakeConnection:
    def __init__(self, incoming: bytes) -> None:
        self.incoming = bytearray(incoming)

    def recv(self, size: int) -> bytes:
        result = bytes(self.incoming[:size])
        del self.incoming[:size]
        return result


def masked_frame(payload: bytes, opcode: int = 1) -> bytes:
    mask = b"mask"
    length = len(payload)
    header = bytes([0x80 | opcode, 0x80 | length])
    return header + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(payload))


def test_websocket_accept_matches_rfc_example() -> None:
    assert accept_value("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_read_masked_client_frame() -> None:
    opcode, payload = read_frame(FakeConnection(masked_frame(b'{"action":"ping"}')))
    assert opcode == 1
    assert payload == b'{"action":"ping"}'


def test_encode_server_frame_with_extended_length() -> None:
    frame = encode_frame(b"x" * 130)
    assert frame[:4] == bytes([0x81, 126]) + struct.pack("!H", 130)
