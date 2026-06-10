"""Collect identifier-like strings from text-bearing RomFS files into corpus.txt.

Decompresses .zs in memory, recurses into SARC packs, extracts
[A-Za-z][A-Za-z0-9_.]{2,63} runs. Skips binary-heavy dirs (Model, Bake, Shader,
Sound, ...). Feed corpus.txt (plus the decompressed NSO) to name_hashes.py to
recover flag names from hashes.

Set $SMBW_ROMFS to the extracted RomFS root (default ./romfs); writes ./corpus.txt.
"""
import os
import re
import struct
import sys

import zstandard

ROOT = os.environ.get("SMBW_ROMFS", "romfs")
DIRS = [
    "Pack", "GameData", "Stage", "AI", "AIGameCommon", "Event",
    "Component", "Gyml", "Sequence", "NetSequence", "RSDB", "System",
    "BancMapUnit", "Scene", "Layer", "Phive",
]
IDENT = re.compile(rb"[A-Za-z][A-Za-z0-9_.]{2,63}")
dec = zstandard.ZstdDecompressor()
names = set()


def sarc_files(data):
    e = "<" if data[6:8] == b"\xff\xfe" else ">"
    data_off = struct.unpack_from(e + "I", data, 12)[0]
    count = struct.unpack_from(e + "H", data, 0x1A)[0]
    for i in range(count):
        o = 0x20 + 16 * i
        start = struct.unpack_from(e + "I", data, o + 8)[0]
        end = struct.unpack_from(e + "I", data, o + 12)[0]
        yield data[data_off + start : data_off + end]


def harvest(data, depth=0):
    if data[:4] == b"SARC" and depth < 3:
        for sub in sarc_files(data):
            harvest(sub, depth + 1)
    else:
        for m in IDENT.finditer(data):
            names.add(m.group(0))


nfiles = 0
for d in DIRS:
    base = os.path.join(ROOT, d)
    if not os.path.isdir(base):
        continue
    for dirpath, _, files in os.walk(base):
        for fn in files:
            p = os.path.join(dirpath, fn)
            try:
                data = open(p, "rb").read()
                if fn.endswith(".zs"):
                    data = dec.decompress(data, max_output_size=512 * 1024 * 1024)
                harvest(data)
                nfiles += 1
            except Exception as ex:
                print(f"skip {p}: {ex}", file=sys.stderr)

with open("corpus.txt", "wb") as f:
    for n in sorted(names):
        f.write(n + b"\n")
print(f"{nfiles} files -> {len(names)} unique strings")
