"""Decompress .pack.zs (zstd) and extract the SARC archive contents.

SMBW actor packs (Pack/Actor/WObjCommon*.pack.zs) and many other resources are
zstd-compressed SARC archives. Extract one to a directory:
    python sarc_extract.py WObjCommonPackunCloudWa.pack.zs out_dir/
Then read the inner .ainb (AI graph), .bgyml (gparam), .byml, etc.

Requires the `zstandard` package (pip install zstandard).
"""
import os
import struct
import sys

import zstandard


def extract(path, outdir):
    data = open(path, "rb").read()
    if path.endswith(".zs"):
        data = zstandard.ZstdDecompressor().decompress(data, max_output_size=512 * 1024 * 1024)
    assert data[:4] == b"SARC", data[:4]
    bom = data[6:8]
    e = "<" if bom == b"\xff\xfe" else ">"
    data_off = struct.unpack_from(e + "I", data, 12)[0]
    # SFAT
    assert data[0x14:0x18] == b"SFAT"
    count = struct.unpack_from(e + "H", data, 0x1A)[0]
    nodes = []
    for i in range(count):
        o = 0x20 + 16 * i
        name_off = struct.unpack_from(e + "I", data, o + 4)[0] & 0xFFFFFF
        start = struct.unpack_from(e + "I", data, o + 8)[0]
        end = struct.unpack_from(e + "I", data, o + 12)[0]
        nodes.append((name_off, start, end))
    # SFNT
    sfnt_off = 0x20 + 16 * count
    assert data[sfnt_off : sfnt_off + 4] == b"SFNT"
    names_base = sfnt_off + 8
    for name_off, start, end in nodes:
        so = names_base + name_off * 4
        eo = data.index(b"\0", so)
        name = data[so:eo].decode("utf-8")
        dest = os.path.join(outdir, name.replace("/", os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, "wb").write(data[data_off + start : data_off + end])
        print(name, end - start)


if __name__ == "__main__":
    extract(sys.argv[1], sys.argv[2])
