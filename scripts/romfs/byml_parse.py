"""Minimal BYML parser (big- or little-endian, v2-v7) -> JSON.

Handles node types: string(0xA0), binary(0xA1), array(0xC0), dict(0xC1),
string table(0xC2), bool(0xD0), s32(0xD1), f32(0xD2), u32(0xD3), s64(0xD4),
u64(0xD5), f64(0xD6), null(0xFF).

SMBW's GameDataList.Product.100.byml.zs is BYML v7. Decompress the .zs first
(see sarc_extract.py / `zstandard`), then:
    python byml_parse.py GameDataList.Product.100.byml GameDataList.json
"""
import json
import struct
import sys


class Byml:
    def __init__(self, data: bytes):
        self.d = data
        magic = data[0:2]
        if magic == b"BY":
            self.e = ">"
        elif magic == b"YB":
            self.e = "<"
        else:
            raise ValueError(f"bad magic {magic!r}")
        self.version = struct.unpack(self.e + "H", data[2:4])[0]
        hash_off, str_off, root_off = struct.unpack(self.e + "III", data[4:16])
        self.hash_keys = self._read_string_table(hash_off) if hash_off else []
        self.strings = self._read_string_table(str_off) if str_off else []
        self.root_off = root_off

    def u8(self, o):
        return self.d[o]

    def u24(self, o):
        if self.e == ">":
            return (self.d[o] << 16) | (self.d[o + 1] << 8) | self.d[o + 2]
        return self.d[o] | (self.d[o + 1] << 8) | (self.d[o + 2] << 16)

    def u32(self, o):
        return struct.unpack_from(self.e + "I", self.d, o)[0]

    def _read_string_table(self, off):
        if self.u8(off) != 0xC2:
            raise ValueError(f"not a string table at {off:#x}: {self.u8(off):#x}")
        n = self.u24(off + 1)
        out = []
        for i in range(n):
            so = off + self.u32(off + 4 + 4 * i)
            eo = self.d.index(b"\0", so)
            out.append(self.d[so:eo].decode("utf-8"))
        return out

    def parse(self):
        return self._node(self.root_off, self.u8(self.root_off))

    def _value(self, off, t):
        """Parse a 4-byte inline value or follow an offset for containers."""
        if t == 0xA0:
            return self.strings[self.u32(off)]
        if t == 0xD0:
            return bool(self.u32(off))
        if t == 0xD1:
            return struct.unpack_from(self.e + "i", self.d, off)[0]
        if t == 0xD2:
            return struct.unpack_from(self.e + "f", self.d, off)[0]
        if t == 0xD3:
            return self.u32(off)
        if t in (0xC0, 0xC1, 0xA1):
            return self._node(self.u32(off), t)
        if t == 0xD4:
            return struct.unpack_from(self.e + "q", self.d, self.u32(off))[0]
        if t == 0xD5:
            return struct.unpack_from(self.e + "Q", self.d, self.u32(off))[0]
        if t == 0xD6:
            return struct.unpack_from(self.e + "d", self.d, self.u32(off))[0]
        if t == 0xFF:
            return None
        raise ValueError(f"unhandled value type {t:#x} at {off:#x}")

    def _node(self, off, t):
        if t == 0xC0:  # array
            n = self.u24(off + 1)
            types = self.d[off + 4 : off + 4 + n]
            voff = off + 4 + ((n + 3) & ~3)
            return [self._value(voff + 4 * i, types[i]) for i in range(n)]
        if t == 0xC1:  # dict
            n = self.u24(off + 1)
            out = {}
            for i in range(n):
                eo = off + 4 + 8 * i
                key = self.hash_keys[self.u24(eo)]
                et = self.u8(eo + 3)
                out[key] = self._value(eo + 4, et)
            return out
        if t == 0xA1:  # binary
            n = self.u32(off)
            return "hex:" + self.d[off + 4 : off + 4 + n].hex()
        return self._value(off, t)


if __name__ == "__main__":
    data = open(sys.argv[1], "rb").read()
    b = Byml(data)
    out = b.parse()
    json.dump(out, open(sys.argv[2], "w"), indent=1)
    print(f"version={b.version} endian={b.e} keys={len(b.hash_keys)} strings={len(b.strings)}")
