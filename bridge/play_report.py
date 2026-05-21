"""Decoder for Nintendo SDK PlayReport buffers (Nintendo's CBOR-ish format).

PlayReport buffers are captured on Switch from the IPC client hook
(CmifProxyImpl<IPrepoService>::_nn_sf_sync_SaveReport{,WithUser}) at the
moment they're sent to the prepo service. The Python bridge decodes them
into a {field_name: value} dict.

Format (reverse-engineered 2026-05-20 from three full payloads:
world_activity (10 fields), world_result (26 fields), course_result (57
fields) — all from playing through W1-1):

    3-byte header:
        byte 0:    magic = 0xDE
        bytes 1-2: big-endian u16 entry count

    Then `entry_count` (key, value) pairs, flat — NOT wrapped in a CBOR
    map type. Each entry starts with a text-string key, then a value.

    Opcode table (live-confirmed unless marked GUESSED):

      0x00..0x7F      inline unsigned int 0..127         [LIVE — Nintendo
                       extends CBOR's 0..23 range; 0x18..0x1B are NOT
                       reserved here. Seen: 0x1A=26, 0x2A=42, 0x3D=61]
      0x80..0x8F      open struct: N=op&0xF entries follow as (key,value)
                       pairs                              [LIVE]
      0x90..0x9F      open array: N=op&0xF values follow [LIVE]
      0xA0..0xBF      short text string, len = op & 0x1F [LIVE]
      0xC2            false                              [LIVE — Nintendo,
                       not CBOR's 0xF4]
      0xC3            true                               [LIVE — Nintendo,
                       not CBOR's 0xF5]
      0xCC + u8       uint, 128..255                     [LIVE — 0xCC 0x87
                       = 135 for wonder_coin]
      0xCD + u16 BE   uint, 256..65535                   [GUESSED]
      0xCE + u32 BE   uint, larger                       [LIVE — system
                       _report_tag = 2175206328]
      0xCF + u64 BE   uint, larger                       [GUESSED]
      0xD0 + s8       signed int -128..127                [GUESSED — d0/d1
      0xD1 + s16 BE   signed int -32768..32767             not yet observed
      0xD2 + s32 BE   signed int                          [LIVE — W1-2
                       stage_key = 232160011]
      0xD3 + s64 BE   signed int                          [LIVE — W1-1
                       stage_key = 2937190396 (= 0xAF11F7FC, doesn't fit
                       in positive s32 so encoder picks s64)]
                       — Reading: d0..d3 are what Struct::Add(long) emits;
                       the encoder picks the smallest signed width that
                       fits. Top-level PlayReport::Add uses the unsigned
                       0xCC/0xCE etc. path instead.
      0xD7 + u8 + u64 BE
                      Any64BitId: first byte is TypeCode, then 8-byte u64.
                       Decoded as {"TypeCode": <int>, "Value": <int>}.
                       Across captures we've only seen TypeCode=0.        [LIVE]
      0xD9 + u8 + N   medium string, 0..255 chars         [LIVE]
      0xFF            literal -1                          [LIVE — seen for
                       transition_type, last_page, conn_result, etc.]

    Opcodes not yet observed in live captures (decoder raises UnknownOpcode):
      - floats (single / double / half)
      - negative ints other than -1
      - containers (struct/array) with >15 entries — there must be an
        extension opcode but we haven't tripped it yet
      - longer strings (>255 chars)

Three reference payloads from W1-1 (Welcome to the Flower Kingdom) — see
test_play_report.py for the full byte fixtures + assertion sets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


HEADER_MAGIC = 0xDE

# How the decoder labels Any64BitId values in the returned dict. We mirror
# the way Ryujinx's ServicePrepo decoder prints them so logs and decoded
# Python output line up visually.
ANY64_TYPECODE_KEY = "TypeCode"
ANY64_VALUE_KEY = "Value"


class DecodeError(Exception):
    """Raised on malformed input or unknown opcode in strict mode.

    Attributes:
        pos:     byte offset where the error was detected
        op:      the offending opcode byte (or None if EOF)
        message: human-readable context
    """

    def __init__(self, message: str, pos: int, op: int | None = None):
        super().__init__(
            f"{message} (at byte {pos}, op=0x{op:02x})"
            if op is not None
            else f"{message} (at byte {pos}, EOF)")
        self.pos = pos
        self.op = op
        self.message = message


@dataclass
class DecodeResult:
    """Result of decoding a PlayReport buffer.

    fields:        {key: value, ...} for every entry that decoded cleanly.
    entry_count:   the count from the header (the number the buffer claims).
    decoded_count: how many entries we actually parsed before EOF / unknown
                   opcode. In strict mode this equals entry_count.
    error:         None on a clean decode, otherwise the str(DecodeError)
                   explaining why partial decode stopped.
    """

    fields: dict[str, Any] = field(default_factory=dict)
    entry_count: int = 0
    decoded_count: int = 0
    error: str | None = None


class _Reader:
    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes):
        self.buf = buf
        self.pos = 0

    def _need(self, n: int) -> None:
        if self.pos + n > len(self.buf):
            raise DecodeError(
                f"need {n} bytes, only {len(self.buf) - self.pos} remain",
                self.pos, None)

    def u8(self) -> int:
        self._need(1)
        b = self.buf[self.pos]
        self.pos += 1
        return b

    def u16_be(self) -> int:
        self._need(2)
        v = int.from_bytes(self.buf[self.pos:self.pos + 2], "big")
        self.pos += 2
        return v

    def u_be(self, nbytes: int) -> int:
        self._need(nbytes)
        v = int.from_bytes(self.buf[self.pos:self.pos + nbytes], "big")
        self.pos += nbytes
        return v

    def s_be(self, nbytes: int) -> int:
        self._need(nbytes)
        v = int.from_bytes(
            self.buf[self.pos:self.pos + nbytes], "big", signed=True)
        self.pos += nbytes
        return v

    def take(self, n: int) -> bytes:
        self._need(n)
        chunk = self.buf[self.pos:self.pos + n]
        self.pos += n
        return chunk


def _decode_string(rdr: _Reader) -> str:
    """Decode a Nintendo PlayReport text string (key or value).

    Two forms:
      0xA0..0xBF        short, len embedded in low 5 bits (0..31)
      0xD9 + u8 + chars medium, 0..255 chars
    """
    start = rdr.pos
    op = rdr.u8()
    if 0xA0 <= op <= 0xBF:
        length = op & 0x1F
    elif op == 0xD9:
        length = rdr.u8()
    else:
        raise DecodeError("expected string opcode", start, op)
    return rdr.take(length).decode("utf-8")


def _decode_value(rdr: _Reader) -> Any:
    """Decode a single value at the current reader position. Recurses for
    structs and arrays.
    """
    start = rdr.pos
    op = rdr.u8()

    # Inline unsigned int — Nintendo's range is 0..127, not CBOR's 0..23.
    if op <= 0x7F:
        return op

    # Containers: structs (key-value pairs) vs arrays (raw values).
    if 0x80 <= op <= 0x8F:
        n = op & 0x0F
        d: dict[str, Any] = {}
        for _ in range(n):
            key = _decode_string(rdr)
            d[key] = _decode_value(rdr)
        return d
    if 0x90 <= op <= 0x9F:
        n = op & 0x0F
        return [_decode_value(rdr) for _ in range(n)]

    # Strings as values (same encoding as keys).
    if 0xA0 <= op <= 0xBF:
        length = op & 0x1F
        return rdr.take(length).decode("utf-8")
    if op == 0xD9:
        length = rdr.u8()
        return rdr.take(length).decode("utf-8")

    # Booleans.
    if op == 0xC2:
        return False
    if op == 0xC3:
        return True

    # Unsigned int extensions.
    if op == 0xCC:
        return rdr.u_be(1)
    if op == 0xCD:
        return rdr.u_be(2)
    if op == 0xCE:
        return rdr.u_be(4)
    if op == 0xCF:
        return rdr.u_be(8)

    # Signed int extensions — used by Struct::Add(long), which is signed
    # in C++.  The encoder picks the smallest signed width that fits.
    # 0xD0/0xD1 guessed by analogy; 0xD2/0xD3 are live-confirmed.  Decoded
    # via signed=True so values with the high bit set come back negative
    # (we haven't observed one yet, but Struct longs *can* be negative).
    if op == 0xD0:
        return rdr.s_be(1)
    if op == 0xD1:
        return rdr.s_be(2)
    if op == 0xD2:
        return rdr.s_be(4)
    if op == 0xD3:
        return rdr.s_be(8)

    # Any64BitId — tagged 64-bit. Encoding is `0xD7 + u8 TypeCode + u64`,
    # 9 bytes total after the opcode. Surface as the same shape Ryujinx
    # prints so log↔Python output stays comparable.
    if op == 0xD7:
        type_code = rdr.u8()
        value = rdr.u_be(8)
        return {ANY64_TYPECODE_KEY: type_code, ANY64_VALUE_KEY: value}

    # Literal -1. The only negative we've seen across three full payloads.
    if op == 0xFF:
        return -1

    raise DecodeError(
        "unknown value opcode — see play_report.py header for the format "
        "table and reverse-engineering notes", start, op)


def decode_play_report(buf: bytes, *, partial_ok: bool = False) -> DecodeResult:
    """Decode a PlayReport buffer.

    Args:
        buf:        the raw bytes captured at the IPC SaveReport hook.
        partial_ok: when True, an unknown opcode or unexpected EOF returns
                    whatever was decoded with `error` set. When False
                    (default), the same condition raises DecodeError.

    Returns:
        DecodeResult.
    """
    rdr = _Reader(buf)
    result = DecodeResult()

    magic = rdr.u8()
    if magic != HEADER_MAGIC:
        raise DecodeError(
            f"bad magic byte (expected 0x{HEADER_MAGIC:02x})", 0, magic)
    result.entry_count = rdr.u16_be()

    for i in range(result.entry_count):
        try:
            key = _decode_string(rdr)
            value = _decode_value(rdr)
        except DecodeError as e:
            if partial_ok:
                result.error = str(e)
                return result
            raise
        result.fields[key] = value
        result.decoded_count = i + 1

    return result
